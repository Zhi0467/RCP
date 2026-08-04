from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from rcp.limits import REMOTE_ARTIFACT_READ_TIMEOUT_SECONDS, RUN_STAGE_RETENTION_DAYS
from rcp.transport.ssh import rsync_ssh_arguments, ssh_arguments
from rcp.transport.state import StateUnavailable

_REMOTE_TREE_HELPERS = """\
import os,shutil
def make_writable(path):
    if os.path.islink(path):
        return
    os.chmod(path, 0o700)
    if not os.path.isdir(path):
        return
    with os.scandir(path) as entries:
        for entry in entries:
            child=entry.path
            if entry.is_dir(follow_symlinks=False):
                make_writable(child)
            elif not entry.is_symlink():
                os.chmod(child, 0o600)
def remove_tree(path):
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
        return
    make_writable(path)
    shutil.rmtree(path)
"""


class RemoteRunStage:
    def __init__(self, host: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host):
            raise ValueError("SSH host contains unsupported characters")
        self.host = host
        self.root: PurePosixPath | None = None
        self._pending_inputs: Path | None = None
        self._reusable_inputs: set[str] = set()

    @property
    def workspace(self) -> PurePosixPath:
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        return self.root / "workspace"

    def sweep(self, *, retain_days: int = RUN_STAGE_RETENTION_DAYS) -> None:
        """Age out stages left behind by failed runs.

        A failed run deliberately keeps its scratch folder so the work is not
        lost, which means nothing else ever deletes it. Best effort: a stage that
        cannot be swept is not worth failing a run over.
        """
        script = (
            _REMOTE_TREE_HELPERS
            + """
import glob,sys,time
cutoff=time.time()-(int(sys.argv[1])*86400)
for target in glob.glob('/tmp/rcp-run.*'):
    try:
        if os.path.isdir(target) and os.path.getmtime(target) < cutoff:
            remove_tree(target)
    except OSError:
        pass
"""
        )
        self._ssh(["python3", "-c", script, str(int(retain_days))])

    def open(self, operation_id: str | None = None, *, reuse: bool = False) -> RemoteRunStage:
        """Create this stage, or with `reuse` adopt it when it already exists.

        A chat conversation keeps one stage across its turns, so opening it a
        second time must land in the same directory rather than fail.
        """
        self.sweep()
        if operation_id is None:
            result = self._ssh(["mktemp", "-d", "/tmp/rcp-run.XXXXXXXX"])
            remote_root = result.stdout.strip()
        else:
            label = _safe_label(operation_id)
            remote_root = f"/tmp/rcp-run.{label}"
            result = self._ssh(
                ["mkdir", "-p", "-m", "700", remote_root]
                if reuse
                else ["mkdir", "-m", "700", remote_root]
            )
        if result.returncode or not _safe_root(remote_root):
            raise StateUnavailable(result.stderr.strip() or "could not create remote run stage")
        self.root = PurePosixPath(remote_root)
        prepared = self._ssh(["mkdir", "-p", str(self.root / "inputs"), str(self.workspace)])
        if prepared.returncode:
            self.close()
            raise StateUnavailable(prepared.stderr.strip() or "could not prepare remote run stage")
        return self

    def attach(self, root: str) -> RemoteRunStage:
        if not _safe_root(root):
            raise ValueError("remote run stage is outside the RCP staging boundary")
        result = self._ssh(["test", "-d", root])
        if result.returncode:
            raise StateUnavailable(
                "The saved remote staging directory is unavailable; retry this operation instead."
            )
        self.root = PurePosixPath(root)
        return self

    def attach_artifact_source(self, root: str) -> RemoteRunStage:
        """Adopt saved provenance; the bounded artifact read performs the SSH check."""
        if not _safe_root(root):
            raise ValueError("remote run stage is outside the RCP staging boundary")
        self.root = PurePosixPath(root)
        return self

    def close(self) -> bool:
        self._clear_pending_inputs()
        if self.root is None:
            return True
        root = str(self.root)
        if not _safe_root(root):
            return False
        script = (
            _REMOTE_TREE_HELPERS
            + "\nimport sys\nremove_tree(sys.argv[1])\n"
            + "if os.path.lexists(sys.argv[1]):\n    raise SystemExit(1)\n"
        )
        result = self._ssh(["python3", "-c", script, root])
        if result.returncode:
            return False
        self.root = None
        return True

    def put_file(self, source: Path, label: str) -> str:
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        safe_label = _safe_label(label)
        remote = self.root / "inputs" / safe_label
        pending = self._pending_input_root() / safe_label
        if pending.exists():
            raise ValueError(f"immutable remote task input already exists: {safe_label}")
        shutil.copyfile(source, pending)
        return str(remote)

    def put_directory(self, source: Path, label: str, *, reuse: bool = False) -> str:
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        safe_label = _safe_label(label)
        remote = self.root / "inputs" / safe_label
        if reuse and self._remote_directory_matches(source, remote):
            return str(remote)
        pending = self._pending_input_root() / safe_label
        if pending.exists():
            raise ValueError(f"immutable remote task input already exists: {safe_label}")
        shutil.copytree(source, pending)
        self._make_pending_tree_writable(pending)
        if reuse:
            self._reusable_inputs.add(safe_label)
        return str(remote)

    def _remote_directory_matches(self, source: Path, remote: PurePosixPath) -> bool:
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        expected = _directory_fingerprint(source)
        script = """
import hashlib,os,stat,sys
root,target,expected=sys.argv[1:4]
def fingerprint(root):
    digest=hashlib.sha256()
    def field(value):
        digest.update(len(value).to_bytes(8,'big')); digest.update(value)
    def visit(path,relative):
        info=os.lstat(path)
        if stat.S_ISLNK(info.st_mode): raise ValueError('reusable staged input contains a symlink')
        if info.st_mode & 0o222: raise ValueError('reusable staged input is writable')
        if stat.S_ISDIR(info.st_mode):
            field(b'd'); field(relative.encode('utf-8'))
            with os.scandir(path) as entries:
                for entry in sorted(entries,key=lambda item:item.name):
                    child=relative+'/'+entry.name if relative else entry.name
                    visit(entry.path,child)
        elif stat.S_ISREG(info.st_mode):
            field(b'f'); field(relative.encode('utf-8')); field(str(info.st_size).encode('ascii'))
            with open(path,'rb') as item:
                while True:
                    chunk=item.read(1024*1024)
                    if not chunk: break
                    digest.update(chunk)
        else:
            raise ValueError('reusable staged input contains a non-regular entry')
    visit(root,''); return digest.hexdigest()
inputs=os.path.join(root,'inputs')
if os.path.islink(root) or not os.path.isdir(root):
    print('remote run stage is unsafe',file=sys.stderr); raise SystemExit(46)
if os.path.islink(inputs) or not os.path.isdir(inputs) or os.path.dirname(target)!=inputs:
    print('remote input root is unsafe',file=sys.stderr); raise SystemExit(46)
if not os.path.lexists(target): raise SystemExit(45)
try: actual=fingerprint(target)
except BaseException as exc:
    print(str(exc),file=sys.stderr); raise SystemExit(46)
if actual!=expected:
    print('reusable staged input does not match its content label',file=sys.stderr)
    raise SystemExit(47)
"""
        result = self._ssh(["python3", "-c", script, str(self.root), str(remote), expected])
        if result.returncode == 0:
            return True
        if result.returncode == 45:
            return False
        if result.returncode in {46, 47}:
            raise ValueError(result.stderr.strip() or "remote reusable input is unsafe")
        raise StateUnavailable(
            result.stderr.strip() or f"could not inspect remote reusable input {remote}"
        )

    def finalize_inputs(self) -> None:
        """Transfer and commit all locally queued inputs as one immutable batch."""

        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        pending = self._pending_inputs
        if pending is None:
            return
        labels = sorted(path.name for path in pending.iterdir())
        if not labels:
            self._clear_pending_inputs()
            return

        batch = self.root / f".input-batch-{uuid.uuid4().hex}"
        reusable_labels = sorted(self._reusable_inputs.intersection(labels))
        script = (
            _REMOTE_TREE_HELPERS
            + """
import hashlib,json,stat,sys
root,batch=map(os.path.abspath,sys.argv[1:3])
labels=json.loads(sys.argv[3]); transferred=sys.argv[4]=='1'
reusable=set(json.loads(sys.argv[5]))
inputs=os.path.join(root,'inputs')
def fingerprint(path,immutable=False):
    info=os.lstat(path)
    if stat.S_ISLNK(info.st_mode): raise ValueError('staged input contains a symlink')
    if immutable and info.st_mode & 0o222:
        raise ValueError('reusable staged input is writable')
    if stat.S_ISDIR(info.st_mode):
        children=[]
        with os.scandir(path) as entries:
            for entry in sorted(entries,key=lambda item:item.name):
                children.append((entry.name,fingerprint(entry.path,immutable)))
        return ('directory',children)
    elif stat.S_ISREG(info.st_mode):
        digest=hashlib.sha256()
        with open(path,'rb') as source:
            while True:
                chunk=source.read(1024*1024)
                if not chunk: break
                digest.update(chunk)
        return ('file',digest.hexdigest())
    else:
        raise ValueError('staged input is not a regular file or directory')
def protect(path):
    info=os.lstat(path)
    if stat.S_ISDIR(info.st_mode):
        with os.scandir(path) as entries:
            for entry in entries: protect(entry.path)
        os.chmod(path,0o500)
    elif stat.S_ISREG(info.st_mode):
        os.chmod(path,0o400)
if not transferred:
    remove_tree(batch); raise SystemExit(44)
if not reusable.issubset(labels):
    remove_tree(batch); raise ValueError('reusable input labels are invalid')
entries=[]
moved=[]
try:
    if os.path.dirname(batch)!=root or not os.path.basename(batch).startswith('.input-batch-'):
        raise ValueError('remote input batch is outside its stage')
    if os.path.islink(root) or not os.path.isdir(root): raise ValueError('run stage is unavailable')
    if os.path.islink(inputs) or not os.path.isdir(inputs):
        raise ValueError('input root is unavailable')
    if sorted(os.listdir(batch))!=labels: raise ValueError('remote input batch is incomplete')
    for label in labels:
        if label!=os.path.basename(label) or label in ('','.','..'):
            raise ValueError('remote input label is unsafe')
        source=os.path.join(batch,label); target=os.path.join(inputs,label)
        source_fingerprint=fingerprint(source)
        if os.path.lexists(target):
            if label not in reusable: raise FileExistsError(target)
            if fingerprint(target,True)!=source_fingerprint:
                raise ValueError('reusable staged input does not match its content label')
        else:
            entries.append((source,target))
    for source,target in entries:
        os.replace(source,target); moved.append((source,target))
    for _source,target in moved:
        protect(target)
    remove_tree(batch)
except BaseException:
    for source,target in reversed(moved):
        if os.path.lexists(target) and not os.path.lexists(source):
            make_writable(target); os.replace(target,source)
    remove_tree(batch)
    raise
"""
        )
        try:
            try:
                result = subprocess.run(
                    [
                        "rsync",
                        "-a",
                        *rsync_ssh_arguments(),
                        f"{pending}/",
                        f"{self.host}:{shlex.quote(str(batch))}/",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                result = subprocess.CompletedProcess([], 255, "", str(exc))
            committed = self._ssh(
                [
                    "python3",
                    "-c",
                    script,
                    str(self.root),
                    str(batch),
                    json.dumps(labels, separators=(",", ":")),
                    "1" if result.returncode == 0 else "0",
                    json.dumps(reusable_labels, separators=(",", ":")),
                ]
            )
            if result.returncode:
                raise StateUnavailable(
                    result.stderr.strip() or "could not transfer remote task inputs"
                )
            if committed.returncode:
                raise StateUnavailable(
                    committed.stderr.strip() or "could not commit remote task inputs"
                )
        finally:
            self._clear_pending_inputs()

    def _pending_input_root(self) -> Path:
        if self._pending_inputs is None:
            self._pending_inputs = Path(tempfile.mkdtemp(prefix="rcp-remote-inputs-"))
        return self._pending_inputs

    def _clear_pending_inputs(self) -> None:
        if self._pending_inputs is None:
            return
        self._make_pending_tree_writable(self._pending_inputs)
        shutil.rmtree(self._pending_inputs)
        self._pending_inputs = None
        self._reusable_inputs.clear()

    @staticmethod
    def _make_pending_tree_writable(root: Path) -> None:
        if not root.exists():
            return
        for directory, _children, files in os.walk(root):
            Path(directory).chmod(0o700)
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o600)

    def find_native_session_files(self, roots: list[str], session_id: str) -> list[str]:
        """Find a provider-owned native transcript without repository matching."""
        if not session_id or any(character in session_id for character in "/\x00"):
            raise ValueError("native session id contains unsupported characters")
        script = """
import json,os,sys
roots=json.loads(sys.argv[1]); session_id=sys.argv[2]
matches=[]
for declared in roots:
    root=os.path.abspath(os.path.expanduser(declared))
    if not os.path.isdir(root): continue
    for directory,_children,files in os.walk(root):
        for name in files:
            if not name.endswith('.jsonl') or session_id not in name[:-6]: continue
            path=os.path.join(directory,name)
            if os.path.isfile(path): matches.append(path)
            if len(matches)>=8: break
        if len(matches)>=8: break
    if len(matches)>=8: break
print(json.dumps(sorted(set(matches))))
"""
        result = self._ssh(["python3", "-c", script, json.dumps(roots), session_id])
        if result.returncode:
            raise StateUnavailable(
                result.stderr.strip() or "could not search native provider sessions"
            )
        try:
            values = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise StateUnavailable("native provider session search returned invalid data") from exc
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise StateUnavailable("native provider session search returned invalid paths")
        return values

    def project_host_files(self, sources: list[str], label: str) -> list[str]:
        """Copy exact execution-host files into one immutable stage directory."""
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        safe_label = _safe_label(label)
        if safe_label != label:
            raise ValueError("remote input label contains unsupported characters")
        target = self.root / "inputs" / safe_label
        script = """
import json,os,shutil,sys,tempfile
sources=json.loads(sys.argv[1]); target=sys.argv[2]
parent=os.path.dirname(target); os.makedirs(parent,mode=0o700,exist_ok=True)
if os.path.lexists(target): raise FileExistsError(target)
staged=tempfile.mkdtemp(prefix='.'+os.path.basename(target)+'-',dir=parent)
try:
    outputs=[]
    for index,source in enumerate(sources):
        if not os.path.isfile(source): raise FileNotFoundError(source)
        destination=os.path.join(staged,f'{index:02d}.jsonl')
        shutil.copy2(source,destination,follow_symlinks=True)
        os.chmod(destination,0o400); outputs.append(destination)
    os.chmod(staged,0o500); os.replace(staged,target); staged=''
    print(json.dumps([os.path.join(target,os.path.basename(item)) for item in outputs]))
finally:
    if staged and os.path.lexists(staged): shutil.rmtree(staged)
"""
        result = self._ssh(["python3", "-c", script, json.dumps(sources), str(target)])
        if result.returncode:
            raise StateUnavailable(result.stderr.strip() or "could not project native transcripts")
        try:
            values = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise StateUnavailable("native transcript projection returned invalid data") from exc
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise StateUnavailable("native transcript projection returned invalid paths")
        return values

    def read_text(self, remote_path: Path | PurePosixPath) -> str:
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        candidate = PurePosixPath(str(remote_path))
        if candidate.parent != self.workspace:
            raise ValueError("remote output must be a direct child of the run workspace")
        return self.read_workspace_text(candidate.name)

    def read_input_text(self, label: str) -> str:
        """Read one immutable direct child of this stage's input directory."""
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        safe_label = _safe_label(label)
        if safe_label != label:
            raise ValueError("remote input label contains unsupported characters")
        candidate = self.root / "inputs" / safe_label
        result = self._ssh(["cat", str(candidate)])
        if result.returncode:
            raise ValueError(result.stderr.strip() or f"missing remote input {safe_label}")
        return result.stdout

    def read_workspace_text(self, name: str) -> str:
        """Read one direct regular workspace file without following symlinks.

        A missing file is normal mailbox state and raises ``FileNotFoundError``.
        An unavailable stage or SSH connection raises ``StateUnavailable`` so a
        caller cannot mistake transport failure for a request that has not arrived.
        """
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        name = _plain_workspace_file_name(name)
        script = """
import os,stat,sys
root,name=sys.argv[1:3]
directory_flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
fds=[]
try:
    try:
        root_fd=os.open(root,directory_flags); fds.append(root_fd)
        workspace_fd=os.open('workspace',directory_flags,dir_fd=root_fd); fds.append(workspace_fd)
    except OSError as exc:
        print(str(exc),file=sys.stderr); raise SystemExit(44)
    try:
        info=os.stat(name,dir_fd=workspace_fd,follow_symlinks=False)
    except FileNotFoundError:
        raise SystemExit(45)
    except OSError as exc:
        print(str(exc),file=sys.stderr); raise SystemExit(46)
    if not stat.S_ISREG(info.st_mode): raise SystemExit(46)
    try:
        flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
        file_fd=os.open(name,flags,dir_fd=workspace_fd); fds.append(file_fd)
    except FileNotFoundError:
        raise SystemExit(45)
    except OSError as exc:
        print(str(exc),file=sys.stderr); raise SystemExit(46)
    if not stat.S_ISREG(os.fstat(file_fd).st_mode): raise SystemExit(46)
    while True:
        chunk=os.read(file_fd,1024*1024)
        if not chunk: break
        sys.stdout.buffer.write(chunk)
finally:
    for item in reversed(fds): os.close(item)
"""
        result = self._ssh_bytes(["python3", "-c", script, str(self.root), name])
        if result.returncode == 44:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise StateUnavailable(
                detail or f"remote run workspace {self.workspace} is unavailable"
            )
        if result.returncode == 45:
            raise FileNotFoundError(f"remote workspace file is absent: {name}")
        if result.returncode == 46:
            raise ValueError(f"remote workspace entry is not a readable regular file: {name}")
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise StateUnavailable(detail or f"could not read remote workspace file {name}")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"remote workspace file is not UTF-8 text: {name}") from exc

    def write_workspace_text(self, name: str, content: str) -> None:
        """Atomically replace one safe, direct regular workspace file."""
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        name = _safe_workspace_file_name(name)
        script = """
import os,stat,sys,uuid
root,name=sys.argv[1:3]
directory_flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
fds=[]; temporary=''
try:
    try:
        root_fd=os.open(root,directory_flags); fds.append(root_fd)
        workspace_fd=os.open('workspace',directory_flags,dir_fd=root_fd); fds.append(workspace_fd)
    except OSError as exc:
        print(str(exc),file=sys.stderr); raise SystemExit(44)
    try:
        current=os.stat(name,dir_fd=workspace_fd,follow_symlinks=False)
    except FileNotFoundError:
        current=None
    except OSError as exc:
        print(str(exc),file=sys.stderr); raise SystemExit(46)
    if current is not None and not stat.S_ISREG(current.st_mode): raise SystemExit(46)
    temporary='.rcp-write-'+uuid.uuid4().hex
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
    file_fd=os.open(temporary,flags,0o600,dir_fd=workspace_fd); fds.append(file_fd)
    while True:
        chunk=sys.stdin.buffer.read(1024*1024)
        if not chunk: break
        view=memoryview(chunk)
        while view:
            view=view[os.write(file_fd,view):]
    os.fsync(file_fd); os.close(file_fd); fds.pop()
    os.replace(temporary,name,src_dir_fd=workspace_fd,dst_dir_fd=workspace_fd); temporary=''
    os.fsync(workspace_fd)
finally:
    if temporary and len(fds)>=2:
        try: os.unlink(temporary,dir_fd=fds[1])
        except OSError: pass
    for item in reversed(fds): os.close(item)
"""
        result = self._ssh_bytes(
            ["python3", "-c", script, str(self.root), name],
            input_data=content.encode("utf-8"),
        )
        if result.returncode == 44:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise StateUnavailable(
                detail or f"remote run workspace {self.workspace} is unavailable"
            )
        if result.returncode == 46:
            raise ValueError(f"remote workspace target is not a regular file: {name}")
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise StateUnavailable(detail or f"could not write remote workspace file {name}")

    def list_workspace_files(self) -> list[str]:
        """Return the base names of regular files directly inside the run workspace.

        A listing that failed is not an empty workspace: reporting one as the other
        would let a previous turn's output survive unseen.
        """
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        script = """
import json,os,stat,sys
root=sys.argv[1]
flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
fds=[]
try:
    try:
        root_fd=os.open(root,flags); fds.append(root_fd)
        workspace_fd=os.open('workspace',flags,dir_fd=root_fd); fds.append(workspace_fd)
        names=[]
        for name in os.listdir(workspace_fd):
            try: info=os.stat(name,dir_fd=workspace_fd,follow_symlinks=False)
            except FileNotFoundError: continue
            if stat.S_ISREG(info.st_mode): names.append(name)
    except OSError as exc:
        print(str(exc),file=sys.stderr); raise SystemExit(44)
    print(json.dumps(sorted(names)))
finally:
    for item in reversed(fds): os.close(item)
"""
        result = self._ssh(["python3", "-c", script, str(self.root)])
        if result.returncode:
            raise StateUnavailable(
                result.stderr.strip() or f"could not list remote run workspace {self.workspace}"
            )
        try:
            names = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise StateUnavailable("remote run workspace listing returned invalid data") from exc
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise StateUnavailable("remote run workspace listing returned invalid names")
        return sorted(names)

    def remove_workspace_file(self, name: str) -> None:
        """Delete one file from the run workspace, e.g. a previous turn's output.

        Raises rather than swallowing a failed delete: a stale file left behind is
        a patch a later turn could apply as if this turn's agent had written it.
        """
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        name = _plain_workspace_file_name(name)
        result = self._ssh(["rm", "-f", str(self.workspace / name)])
        if result.returncode:
            raise StateUnavailable(
                result.stderr.strip() or f"could not remove remote {self.workspace / name}"
            )

    def prepare_artifact_directory(self, scope_id: str, *, reuse: bool) -> PurePosixPath:
        """Create the exact output directory for one logical chat turn."""
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        if _safe_label(scope_id) != scope_id:
            raise ValueError("artifact scope contains unsupported characters")
        target = self.workspace / "turns" / scope_id / "artifacts"
        script = (
            _REMOTE_TREE_HELPERS
            + """
import stat,sys
workspace,scope,reuse=sys.argv[1],sys.argv[2],sys.argv[3]=='1'
if os.path.islink(workspace) or not os.path.isdir(workspace):
    raise SystemExit('workspace is unavailable')
turns=os.path.join(workspace,'turns')
if os.path.lexists(turns) and (os.path.islink(turns) or not os.path.isdir(turns)):
    raise SystemExit('artifact parent is unsafe')
os.makedirs(turns,mode=0o700,exist_ok=True)
scope_path=os.path.join(turns,scope)
target=os.path.join(scope_path,'artifacts')
if reuse:
    if (os.path.islink(scope_path) or not os.path.isdir(scope_path) or
        os.path.islink(target) or not os.path.isdir(target)):
        raise SystemExit('saved artifact directory is unavailable')
else:
    remove_tree(scope_path)
    os.makedirs(target,mode=0o700,exist_ok=False)
"""
        )
        result = self._ssh(
            ["python3", "-c", script, str(self.workspace), scope_id, "1" if reuse else "0"]
        )
        if result.returncode:
            raise StateUnavailable(
                result.stderr.strip() or "could not prepare remote artifact directory"
            )
        return target

    def list_artifact_files(self, scope_id: str) -> list[tuple[str, int]]:
        """List direct, non-symlink regular artifact candidates and their sizes."""
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        if _safe_label(scope_id) != scope_id:
            raise ValueError("artifact scope contains unsupported characters")
        script = """
import json,os,stat,sys
root,scope=sys.argv[1],sys.argv[2]
flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
fds=[]
try:
    fd=os.open(root,flags); fds.append(fd)
    for part in ('workspace','turns',scope,'artifacts'):
        fd=os.open(part,flags,dir_fd=fd); fds.append(fd)
    result=[]
    for name in os.listdir(fd):
        info=os.stat(name,dir_fd=fd,follow_symlinks=False)
        if stat.S_ISREG(info.st_mode): result.append([name,info.st_size])
    print(json.dumps(sorted(result)))
except (FileNotFoundError,NotADirectoryError,OSError) as exc:
    print(str(exc),file=sys.stderr); raise SystemExit(44)
finally:
    for item in reversed(fds): os.close(item)
"""
        result = self._ssh(["python3", "-c", script, str(self.root), scope_id])
        if result.returncode:
            if result.returncode == 44:
                raise FileNotFoundError("remote artifact directory is unavailable")
            raise StateUnavailable(result.stderr.strip() or "could not list remote artifacts")
        try:
            values = json.loads(result.stdout)
            return [(str(name), int(size)) for name, size in values]
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateUnavailable("remote artifact listing was invalid") from exc

    def read_artifact_bytes(self, scope_id: str, name: str, *, max_bytes: int) -> bytes:
        """Read one bounded direct regular child over SSH without making a local copy."""
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        if _safe_label(scope_id) != scope_id:
            raise ValueError("artifact scope contains unsupported characters")
        if PurePosixPath(name).name != name or name in {"", ".", ".."}:
            raise ValueError("artifact name must be a plain base name")
        script = """
import os,stat,sys
root,scope,name,limit=sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4])
flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
fds=[]
try:
    fd=os.open(root,flags); fds.append(fd)
    for part in ('workspace','turns',scope,'artifacts'):
        fd=os.open(part,flags,dir_fd=fd); fds.append(fd)
    file_fd=os.open(name,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0),dir_fd=fd); fds.append(file_fd)
    info=os.fstat(file_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size>limit: raise SystemExit(45)
    remaining=limit+1
    while remaining:
        chunk=os.read(file_fd,min(1024*1024,remaining))
        if not chunk: break
        sys.stdout.buffer.write(chunk); remaining-=len(chunk)
    if remaining==0: raise SystemExit(45)
except (FileNotFoundError,NotADirectoryError,OSError) as exc:
    print(str(exc),file=sys.stderr); raise SystemExit(44)
finally:
    for item in reversed(fds): os.close(item)
"""
        result = self._ssh_bytes(
            ["python3", "-c", script, str(self.root), scope_id, name, str(max_bytes)]
        )
        if result.returncode == 44:
            raise FileNotFoundError("remote artifact is unavailable")
        if result.returncode == 45:
            raise ValueError("remote artifact is not a bounded regular file")
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise StateUnavailable(detail or "could not read remote artifact")
        return result.stdout

    def _ssh(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        command = " ".join(shlex.quote(argument) for argument in arguments)
        try:
            return subprocess.run(
                ssh_arguments(self.host, command),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess([], 255, "", str(exc))

    def _ssh_bytes(
        self,
        arguments: list[str],
        *,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = " ".join(shlex.quote(argument) for argument in arguments)
        try:
            return subprocess.run(
                ssh_arguments(self.host, command),
                capture_output=True,
                input=input_data,
                timeout=REMOTE_ARTIFACT_READ_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess([], 255, b"", str(exc).encode())


def _safe_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    if not label:
        raise ValueError("remote stage label is empty")
    return label


def _directory_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()

    def field(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def visit(path: Path, relative: str) -> None:
        info = path.lstat()
        if path.is_symlink():
            raise ValueError("remote task input contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            field(b"d")
            field(relative.encode("utf-8"))
            with os.scandir(path) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    child = f"{relative}/{entry.name}" if relative else entry.name
                    visit(Path(entry.path), child)
        elif stat.S_ISREG(info.st_mode):
            field(b"f")
            field(relative.encode("utf-8"))
            field(str(info.st_size).encode("ascii"))
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise ValueError("remote task input contains a non-regular entry")

    visit(root, "")
    return digest.hexdigest()


def _plain_workspace_file_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise ValueError("workspace file name must be a plain base name")
    return name


def _safe_workspace_file_name(name: str) -> str:
    name = _plain_workspace_file_name(name)
    if len(name) > 255 or re.fullmatch(r"[A-Za-z0-9._-]+", name) is None:
        raise ValueError("workspace file name contains unsupported characters")
    return name


def _safe_root(value: str) -> bool:
    candidate = PurePosixPath(value)
    return (
        candidate.parent == PurePosixPath("/tmp")
        and re.fullmatch(r"rcp-run\.[A-Za-z0-9_-]+", candidate.name) is not None
    )
