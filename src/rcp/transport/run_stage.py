from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rcp.limits import (
    PROJECT_TRANSFER_MANIFEST_MAX_BYTES,
    REMOTE_ARTIFACT_READ_TIMEOUT_SECONDS,
    REMOTE_RUN_STAGE_COMMAND_TIMEOUT_SECONDS,
    REMOTE_SOURCE_OPERATION_TIMEOUT_SECONDS,
    RUN_STAGE_RETENTION_DAYS,
)
from rcp.rcp_home import rcp_temp_dir
from rcp.sources import ImportedProviderSourceInventory, ImportedProviderSourceStore
from rcp.transport import remote_stage_root
from rcp.transport.ssh import rsync_ssh_arguments, ssh_arguments
from rcp.transport.state import (
    StateMissing,
    StateUnavailable,
    StateUnreachable,
    _remote_script,
)


class RemoteStageTransportFailure(StateUnavailable):
    """A transport call produced no host verdict; retry without inferring liveness."""


class _SshNoVerdict(subprocess.CompletedProcess):
    """An ssh call that gave no verdict: it could not start, or RCP stopped waiting.

    Its 255 is RCP's own. Every caller still reads it as failed, and none reads
    it as ssh's word that the link is gone.
    """


def _ssh_failure(
    result: subprocess.CompletedProcess,
    default: str,
    *,
    answered: type[StateUnavailable] = StateUnavailable,
) -> StateUnavailable:
    """The exception for a failed ssh call: a lost link only when ssh itself said 255."""

    stderr = result.stderr
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    detail = (stderr or "").strip() or default
    if isinstance(result, _SshNoVerdict):
        return RemoteStageTransportFailure(detail)
    if result.returncode == 255:
        return StateUnreachable(detail)
    return answered(detail)


@dataclass(frozen=True)
class ImportedProviderSourceReadback:
    fingerprint: str
    file_count: int
    payload_size_bytes: int


def run_stage_partition(host: str, root: str | PurePosixPath | None) -> str | None:
    """The SSH master one stage keeps to itself, named by the stage root.

    One stage root, one master. That is the stage the work runs in, not the run
    itself: a conversation keeps one stage across its turns, so a later turn
    reaching the same root reuses the same connection. Which is what is wanted,
    because those turns are the same conversation and nobody else is on it. What
    matters is that nothing outside the stage is, so a lost link ends the work
    in that stage and nothing else. The provider turn of the same stage names
    its master here too, so the turn and its file work share one fate. Before
    the root exists there is nothing to isolate, and creation shares the
    default.
    """

    return None if root is None else f"{host}:{root}"


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

    @property
    def transport_partition(self) -> str | None:
        return run_stage_partition(self.host, self.root)

    def sweep(
        self,
        *,
        retain_days: int = RUN_STAGE_RETENTION_DAYS,
        protected_roots: Iterable[str] | None = (),
    ) -> None:
        """Age out stages left behind by failed runs.

        A failed run deliberately keeps its scratch folder so the work is not
        lost, which means nothing else ever deletes it. Best effort: a stage that
        cannot be swept is not worth failing a run over.
        """
        if protected_roots is None:
            return
        protected = tuple(dict.fromkeys(protected_roots))
        if any(not _safe_root(root) for root in protected):
            raise ValueError("protected remote run stage is outside the staging boundary")
        self._ssh(
            [
                "python3",
                "-c",
                _remote_script("remote_stage_root.py"),
                "sweep",
                str(int(retain_days)),
                json.dumps(protected, separators=(",", ":")),
            ]
        )

    def open(
        self,
        operation_id: str | None = None,
        *,
        reuse: bool = False,
        protected_roots: Iterable[str] | None = (),
    ) -> RemoteRunStage:
        """Create this stage, or with `reuse` adopt it when it already exists.

        A chat conversation keeps one stage across its turns, so opening it a
        second time must land in the same directory rather than fail.
        """
        self.sweep(protected_roots=protected_roots)
        label = "" if operation_id is None else _safe_label(operation_id)
        result = self._ssh(
            [
                "python3",
                "-c",
                _remote_script("remote_stage_root.py"),
                "create",
                label,
                "1" if reuse else "0",
            ]
        )
        remote_root = result.stdout.strip()
        if result.returncode or not _safe_root(remote_root):
            raise _ssh_failure(result, "could not create remote run stage")
        safe = self._directory_probe(remote_root)
        if safe.returncode:
            raise _ssh_failure(safe, "could not safely adopt remote run stage")
        self.root = PurePosixPath(remote_root)
        prepared = self._ssh(["mkdir", "-p", str(self.root / "inputs"), str(self.workspace)])
        if prepared.returncode:
            self.close()
            raise _ssh_failure(prepared, "could not prepare remote run stage")
        return self

    def attach(self, root: str) -> RemoteRunStage:
        if not _safe_root(root):
            raise ValueError("remote run stage is outside the RCP staging boundary")
        result = self._directory_probe(root)
        if result.returncode:
            # 255 is ssh saying it could not ask. Anything else is the host
            # answering that this stage is gone, replaced, or not ours, and a
            # caller that waits on an answer waits forever.
            detail = (
                "The saved remote staging directory is unavailable; retry this operation instead."
            )
            if isinstance(result, _SshNoVerdict):
                raise StateUnavailable(detail)
            raise (StateUnreachable if result.returncode == 255 else StateMissing)(detail)
        self.root = PurePosixPath(root)
        return self

    def directory_exists(self, root: str) -> bool | None:
        """Probe a staging directory, separating "gone" from "could not ask".

        A wake that cannot reach the host must retry later; one whose stage was
        actually removed must say so instead. `attach` collapses both into
        unavailability, so a preflight that has to tell them apart probes here.
        """

        if not _safe_root(root):
            return False
        result = self._directory_probe(root)
        if result.returncode == 0:
            return True
        return None if result.returncode == 255 else False

    def _directory_probe(self, root: str) -> subprocess.CompletedProcess[str]:
        """Check the saved root itself without following an unsafe replacement."""

        return self._ssh(["python3", "-c", _remote_script("remote_stage_root.py"), "check", root])

    def canonical_directories(
        self,
        paths: list[str],
        *,
        require_writable: bool,
    ) -> tuple[dict[str, str], str]:
        """Resolve exact directories on the execution account without broadening them."""

        declared = list(dict.fromkeys(paths))
        script = """
import json,os,sys
declared=json.loads(sys.argv[1]); require_writable=sys.argv[2]=='1'
resolved={}
for raw in declared:
    expanded=os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        print('project repository root must be absolute: '+raw,file=sys.stderr)
        raise SystemExit(40)
    target=os.path.realpath(expanded)
    if not os.path.isdir(target):
        print('project repository root is unavailable: '+raw,file=sys.stderr)
        raise SystemExit(41)
    if require_writable and not os.access(target,os.W_OK):
        print('project repository root is not writable: '+raw,file=sys.stderr)
        raise SystemExit(42)
    resolved[raw]=target
print(json.dumps({'home':os.path.realpath(os.path.expanduser('~')),'paths':resolved},sort_keys=True))
"""
        result = self._ssh(
            [
                "python3",
                "-c",
                script,
                json.dumps(declared, separators=(",", ":")),
                "1" if require_writable else "0",
            ]
        )
        if result.returncode == 255:
            raise _ssh_failure(result, "could not inspect remote project repository roots")
        if result.returncode:
            raise ValueError(
                result.stderr.strip() or "remote project repository roots are unavailable"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise StateUnavailable(
                "remote repository root inspection returned invalid data"
            ) from exc
        values = payload.get("paths") if isinstance(payload, dict) else None
        home = payload.get("home") if isinstance(payload, dict) else None
        if (
            not isinstance(values, dict)
            or set(values) != set(declared)
            or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in values.items()
            )
            or not isinstance(home, str)
            or not home
        ):
            raise StateUnavailable("remote repository root inspection returned invalid paths")
        return values, home

    def legacy_stage_roots(self) -> list[str]:
        """Canonical paths of the `/tmp/rcp-run.*` stages left from before `~/.rcp`.

        A later release removes this with the legacy stage location.
        """

        result = self._ssh(["python3", "-c", _remote_script("remote_stage_root.py"), "legacy"])
        if result.returncode:
            raise _ssh_failure(result, "could not list legacy remote run stages")
        try:
            roots = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise StateUnavailable("legacy remote run stage listing returned invalid data") from exc
        if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
            raise StateUnavailable("legacy remote run stage listing returned invalid paths")
        return roots

    def attach_artifact_source(self, root: str) -> RemoteRunStage:
        """Adopt saved provenance; the bounded artifact read performs the SSH check."""
        if not _safe_root(root):
            raise ValueError("remote run stage is outside the RCP staging boundary")
        self.root = PurePosixPath(root)
        return self

    def stage_last_touch(self) -> float:
        """Read the retained stage's mtime without refreshing its retention clock."""
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        result = self._ssh(
            ["python3", "-c", _remote_script("remote_stage_root.py"), "last-touch", str(self.root)]
        )
        if result.returncode == 44:
            raise FileNotFoundError("remote run stage is missing")
        if result.returncode == 1:
            raise ValueError(result.stderr.strip() or "remote run stage is unsafe")
        if result.returncode:
            raise _ssh_failure(result, "could not inspect remote run stage retention")
        try:
            modified = float(result.stdout)
            if not math.isfinite(modified):
                raise ValueError("non-finite stage timestamp")
        except ValueError as exc:
            raise StateUnavailable("remote run stage returned an invalid timestamp") from exc
        return modified

    def close(self) -> bool:
        self._clear_pending_inputs()
        if self.root is None:
            return True
        root = str(self.root)
        if not _safe_root(root):
            return False
        result = self._ssh(
            ["python3", "-c", _remote_script("remote_stage_root.py"), "remove", root]
        )
        if result.returncode:
            return False
        self.root = None
        return True

    def put_file(self, source: Path, label: str, *, reuse: bool = False) -> str:
        """Queue one input; `reuse` accepts an identical file already committed.

        Content-addressed inputs are staged again whenever RCP cannot read the
        existing copy, and a link that dropped during that read is one such
        time. Without `reuse` the commit would then reject a file whose content
        it is about to prove identical, turning a blip into a failed turn.
        """

        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        safe_label = _safe_label(label)
        remote = self.root / "inputs" / safe_label
        pending = self._pending_input_root() / safe_label
        if pending.exists():
            raise ValueError(f"immutable remote task input already exists: {safe_label}")
        shutil.copyfile(source, pending)
        if reuse:
            self._reusable_inputs.add(safe_label)
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

    def put_imported_provider_sources(
        self,
        source_store: ImportedProviderSourceStore,
        inventory: ImportedProviderSourceInventory,
        label: str,
    ) -> str:
        """Queue exactly one validated project-owned provider-history inventory."""

        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        if not inventory.files:
            raise ValueError("imported provider source inventory is empty")
        safe_label = _safe_label(label)
        if safe_label != label:
            raise ValueError("remote input label contains unsupported characters")
        remote = self.root / "inputs" / safe_label
        pending = self._pending_input_root() / safe_label
        if pending.exists():
            raise ValueError(f"immutable remote task input already exists: {safe_label}")
        _copy_imported_provider_sources(source_store.root, pending, inventory)
        return str(remote)

    def verify_imported_provider_sources(
        self,
        inventory: ImportedProviderSourceInventory,
        label: str,
    ) -> ImportedProviderSourceReadback:
        """Read back one immutable staged inventory without returning its contents."""

        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        if not inventory.files:
            raise ValueError("imported provider source inventory is empty")
        safe_label = _safe_label(label)
        if safe_label != label:
            raise ValueError("remote input label contains unsupported characters")
        files = [item.model_dump(mode="json") for item in inventory.files]
        encoded_inventory = json.dumps(files, separators=(",", ":")).encode()
        if len(encoded_inventory) > PROJECT_TRANSFER_MANIFEST_MAX_BYTES:
            raise ValueError("imported provider source inventory exceeds its byte bound")
        result = self._ssh_bytes(
            [
                "python3",
                "-c",
                _remote_script("remote_verify_imported_sources.py"),
                str(self.root),
                safe_label,
                inventory.project_id,
                inventory.fingerprint,
                str(PROJECT_TRANSFER_MANIFEST_MAX_BYTES),
            ],
            input_data=encoded_inventory,
            timeout_seconds=REMOTE_SOURCE_OPERATION_TIMEOUT_SECONDS,
        )
        error = result.stderr.decode("utf-8", errors="replace").strip()
        if result.returncode == 255:
            raise _ssh_failure(result, "could not verify staged provider sources")
        if result.returncode:
            raise ValueError(error or "staged provider sources differ from their inventory")
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
            readback = ImportedProviderSourceReadback(
                fingerprint=payload["fingerprint"],
                file_count=payload["file_count"],
                payload_size_bytes=payload["payload_size_bytes"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateUnavailable(
                "staged provider source verification returned invalid data"
            ) from exc
        if (
            readback.fingerprint != inventory.fingerprint
            or readback.file_count != len(inventory.files)
            or readback.payload_size_bytes != inventory.payload_size_bytes
        ):
            raise ValueError("staged provider source readback differs from its inventory")
        return readback

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
        raise _ssh_failure(result, f"could not inspect remote reusable input {remote}")

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
        try:
            spawned = True
            try:
                result = subprocess.run(
                    [
                        "rsync",
                        "-a",
                        *rsync_ssh_arguments(partition=self.transport_partition),
                        f"{pending}/",
                        f"{self.host}:{shlex.quote(str(batch))}/",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                # The commit script needs a failed code to clean the batch up.
                # This code is RCP's own, not ssh's, so it never names a lost link.
                spawned = False
                result = subprocess.CompletedProcess([], 255, "", str(exc))
            committed = self._ssh(
                [
                    "python3",
                    "-c",
                    _remote_script("remote_stage_root.py"),
                    "commit-inputs",
                    str(self.root),
                    str(batch),
                    json.dumps(labels, separators=(",", ":")),
                    "1" if result.returncode == 0 else "0",
                    json.dumps(reusable_labels, separators=(",", ":")),
                ]
            )
            # rsync and ssh both exit 255 only when the connection itself failed;
            # any other code is the host refusing or rejecting the inputs, and a
            # reattempt would meet the same answer.
            if result.returncode:
                unavailable = (
                    StateUnreachable if spawned and result.returncode == 255 else StateUnavailable
                )
                raise unavailable(result.stderr.strip() or "could not transfer remote task inputs")
            if committed.returncode:
                raise _ssh_failure(committed, "could not commit remote task inputs")
        finally:
            self._clear_pending_inputs()

    def _pending_input_root(self) -> Path:
        if self._pending_inputs is None:
            self._pending_inputs = Path(
                tempfile.mkdtemp(prefix="rcp-remote-inputs-", dir=rcp_temp_dir())
            )
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

    def read_workspace_text(self, name: str, *, max_bytes: int | None = None) -> str:
        """Read one direct regular workspace file without following symlinks.

        A missing file is normal mailbox state and raises ``FileNotFoundError``.
        An unavailable stage or SSH connection raises ``StateUnavailable`` so a
        caller cannot mistake transport failure for a request that has not arrived.
        When ``max_bytes`` is set, the remote process checks the opened file and
        transfers at most one byte beyond that limit before failing.
        """
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        name = _plain_workspace_file_name(name)
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("remote workspace byte limit must not be negative")
        script = """
import os,stat,sys
root,name,limit=sys.argv[1],sys.argv[2],int(sys.argv[3])
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
    opened=os.fstat(file_fd)
    if not stat.S_ISREG(opened.st_mode): raise SystemExit(46)
    if limit>=0 and opened.st_size>limit: raise SystemExit(47)
    remaining=None if limit<0 else limit+1
    while remaining is None or remaining:
        amount=1024*1024 if remaining is None else min(1024*1024,remaining)
        chunk=os.read(file_fd,amount)
        if not chunk: break
        sys.stdout.buffer.write(chunk)
        if remaining is not None: remaining-=len(chunk)
    if remaining==0: raise SystemExit(47)
finally:
    for item in reversed(fds): os.close(item)
"""
        result = self._ssh_bytes(
            [
                "python3",
                "-c",
                script,
                str(self.root),
                name,
                str(-1 if max_bytes is None else max_bytes),
            ]
        )
        if result.returncode == 44:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise StateUnavailable(
                detail or f"remote run workspace {self.workspace} is unavailable"
            )
        if result.returncode == 45:
            raise FileNotFoundError(f"remote workspace file is absent: {name}")
        if result.returncode == 46:
            raise ValueError(f"remote workspace entry is not a readable regular file: {name}")
        if result.returncode == 47:
            raise ValueError(f"mailbox file exceeds {max_bytes} bytes: {name}")
        if result.returncode:
            raise _ssh_failure(result, f"could not read remote workspace file {name}")
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
            raise _ssh_failure(result, f"could not write remote workspace file {name}")

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
            raise _ssh_failure(result, f"could not list remote run workspace {self.workspace}")
        try:
            names = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise StateUnavailable("remote run workspace listing returned invalid data") from exc
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise StateUnavailable("remote run workspace listing returned invalid names")
        return sorted(names)

    def list_workspace_entries(self) -> list[str]:
        """Return every direct entry name without following the workspace or its children.

        Mailbox preparation needs to see a stale symlink, directory, or special
        entry rather than mistake it for an empty workspace. Removal can then
        either clear the exact safe entry or fail closed.
        """
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        script = """
import json,os,sys
root=sys.argv[1]
flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
fds=[]
try:
    try:
        root_fd=os.open(root,flags); fds.append(root_fd)
        workspace_fd=os.open('workspace',flags,dir_fd=root_fd); fds.append(workspace_fd)
        names=sorted(os.listdir(workspace_fd))
    except OSError as exc:
        print(str(exc),file=sys.stderr); raise SystemExit(44)
    print(json.dumps(names))
finally:
    for item in reversed(fds): os.close(item)
"""
        result = self._ssh(["python3", "-c", script, str(self.root)])
        if result.returncode:
            raise _ssh_failure(result, f"could not list remote run workspace {self.workspace}")
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
            raise _ssh_failure(result, f"could not remove remote {self.workspace / name}")

    def remove_workspace_file_if_sha256(self, name: str, expected_sha256: str) -> bool:
        """Delete one direct regular file only while its bytes still match a snapshot."""

        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        name = _plain_workspace_file_name(name)
        if len(expected_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in expected_sha256
        ):
            raise ValueError("expected workspace digest must be lowercase SHA-256")
        script = """
import hashlib,os,stat,sys,uuid
root,name,expected=sys.argv[1],sys.argv[2],sys.argv[3]
directory_flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
fds=[]; quarantine='.rcp-consume-'+uuid.uuid4().hex+'-'+name; quarantined=False
def restore():
    global quarantined
    if not quarantined: return
    try:
        os.link(quarantine,name,src_dir_fd=workspace_fd,dst_dir_fd=workspace_fd,follow_symlinks=False)
    except FileExistsError:
        os.fsync(workspace_fd); return
    os.unlink(quarantine,dir_fd=workspace_fd); os.fsync(workspace_fd); quarantined=False
try:
    root_fd=os.open(root,directory_flags); fds.append(root_fd)
    workspace_fd=os.open('workspace',directory_flags,dir_fd=root_fd); fds.append(workspace_fd)
    try: before=os.stat(name,dir_fd=workspace_fd,follow_symlinks=False)
    except FileNotFoundError: raise SystemExit(45)
    if not stat.S_ISREG(before.st_mode): raise SystemExit(46)
    os.rename(name,quarantine,src_dir_fd=workspace_fd,dst_dir_fd=workspace_fd); quarantined=True
    file_fd=os.open(quarantine,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0),dir_fd=workspace_fd)
    fds.append(file_fd); digest=hashlib.sha256()
    while True:
        chunk=os.read(file_fd,1024*1024)
        if not chunk: break
        digest.update(chunk)
    if digest.hexdigest()!=expected:
        restore(); raise SystemExit(47)
    os.unlink(quarantine,dir_fd=workspace_fd); quarantined=False; os.fsync(workspace_fd)
finally:
    if quarantined:
        try: restore()
        except OSError: pass
    for item in reversed(fds): os.close(item)
"""
        result = self._ssh(["python3", "-c", script, str(self.root), name, expected_sha256])
        if result.returncode == 0:
            return True
        if result.returncode in {45, 47}:
            return False
        raise _ssh_failure(result, f"could not conditionally remove remote {self.workspace / name}")

    def prepare_artifact_directory(self, scope_id: str, *, reuse: bool) -> PurePosixPath:
        """Create the exact output directory for one logical chat turn."""
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        if _safe_label(scope_id) != scope_id:
            raise ValueError("artifact scope contains unsupported characters")
        target = self.workspace / "turns" / scope_id / "artifacts"
        result = self._ssh(
            [
                "python3",
                "-c",
                _remote_script("remote_stage_root.py"),
                "prepare-artifacts",
                str(self.workspace),
                scope_id,
                "1" if reuse else "0",
            ]
        )
        if result.returncode:
            raise _ssh_failure(result, "could not prepare remote artifact directory")
        return target

    def prepare_artifact_edit_directory(self, scope_id: str, *, staged: bool) -> PurePosixPath:
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        result = self._ssh(
            [
                "python3",
                "-c",
                _remote_script("remote_stage_root.py"),
                "prepare-edit-artifacts",
                str(self.workspace),
                scope_id,
                "1" if staged else "0",
            ]
        )
        if result.returncode:
            raise _ssh_failure(result, "could not prepare the retained artifact edit directory")
        return self.workspace / "turns" / scope_id / "artifacts"

    def stage_artifact_bytes(self, scope_id: str, name: str, data: bytes) -> None:
        """Stage editable bytes once; a recovered edit keeps its retained file."""
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        result = self._ssh_bytes(
            [
                "python3",
                "-c",
                _remote_script("remote_stage_root.py"),
                "stage-artifact",
                str(self.workspace),
                scope_id,
                name,
            ],
            input_data=data,
        )
        if result.returncode:
            raise _ssh_failure(result, "could not stage remote artifact edit")

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
            raise _ssh_failure(result, "could not list remote artifacts")
        try:
            values = json.loads(result.stdout)
            return [(str(name), int(size)) for name, size in values]
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateUnavailable("remote artifact listing was invalid") from exc

    def read_live_file(self, path: str, *, max_bytes: int, tail: bool = False) -> bytes:
        """Use the same bounded, symlink-refusing reader as local artifact reads."""
        script = (Path(__file__).parent.parent / "regular_file_reader.py").read_text(
            encoding="utf-8"
        )
        result = self._ssh_bytes(
            ["python3", "-c", script, path, str(max_bytes), "tail" if tail else "whole"]
        )
        if result.returncode:
            raise _ssh_failure(result, "could not read live artifact source")
        if len(result.stdout) > max_bytes:
            raise ValueError("live artifact source exceeds byte limit")
        return result.stdout

    def read_artifact_bytes(self, scope_id: str, name: str, *, max_bytes: int) -> bytes:
        """Read one bounded direct regular child over SSH without making a local copy."""
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        if _safe_label(scope_id) != scope_id:
            raise ValueError("artifact scope contains unsupported characters")
        if PurePosixPath(name).name != name or name in {"", ".", ".."}:
            raise ValueError("artifact name must be a plain base name")
        script = """
import errno,os,stat,sys
root,scope,name,limit=sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4])
flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
fds=[]
try:
    fd=os.open(root,flags); fds.append(fd)
    for part in ('workspace','turns',scope,'artifacts'):
        fd=os.open(part,flags,dir_fd=fd); fds.append(fd)
    file_fd=os.open(name,os.O_RDONLY|os.O_NONBLOCK|getattr(os,'O_NOFOLLOW',0),dir_fd=fd); fds.append(file_fd)
    info=os.fstat(file_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size>limit: raise SystemExit(45)
    remaining=limit+1
    while remaining:
        chunk=os.read(file_fd,min(1024*1024,remaining))
        if not chunk: break
        sys.stdout.buffer.write(chunk); remaining-=len(chunk)
    if remaining==0: raise SystemExit(45)
except FileNotFoundError as exc:
    print(str(exc),file=sys.stderr); raise SystemExit(44)
except NotADirectoryError as exc:
    print(str(exc),file=sys.stderr); raise SystemExit(45)
except OSError as exc:
    print(str(exc),file=sys.stderr)
    raise SystemExit(45 if exc.errno in (errno.ELOOP,errno.ENOTDIR) else 46)
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
            raise _ssh_failure(result, "could not read remote artifact")
        return result.stdout

    def touch(self) -> None:
        """Refresh this conversation stage's rolling retention timestamp."""
        if self.root is None:
            raise RuntimeError("remote run stage is not open")
        script = """
import os,sys
root=sys.argv[1]
flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
fd=None
try:
    fd=os.open(root,flags)
    os.utime(fd,None)
except OSError as exc:
    print(str(exc),file=sys.stderr); raise SystemExit(44)
finally:
    if fd is not None: os.close(fd)
"""
        result = self._ssh(["python3", "-c", script, str(self.root)])
        if result.returncode:
            raise _ssh_failure(result, f"could not touch remote run stage {self.root}")

    def _ssh(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        command = " ".join(shlex.quote(argument) for argument in arguments)
        try:
            return subprocess.run(
                ssh_arguments(self.host, command, partition=self.transport_partition),
                capture_output=True,
                text=True,
                timeout=REMOTE_RUN_STAGE_COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _SshNoVerdict([], 255, "", str(exc))

    def _ssh_bytes(
        self,
        arguments: list[str],
        *,
        input_data: bytes | None = None,
        timeout_seconds: float = REMOTE_ARTIFACT_READ_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[bytes]:
        command = " ".join(shlex.quote(argument) for argument in arguments)
        try:
            return subprocess.run(
                ssh_arguments(self.host, command, partition=self.transport_partition),
                capture_output=True,
                input=input_data,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _SshNoVerdict([], 255, b"", str(exc).encode())


def _safe_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    if not label:
        raise ValueError("remote stage label is empty")
    return label


def _copy_imported_provider_sources(
    source_root: Path,
    destination: Path,
    inventory: ImportedProviderSourceInventory,
) -> None:
    """Copy only inventory-named files while rechecking every byte and parent."""

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    provider_descriptors: dict[str, int] = {}
    try:
        root_descriptor = os.open(source_root, directory_flags)
        descriptors.append(root_descriptor)
        root_info = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_IMODE(root_info.st_mode) != 0o700:
            raise ValueError("imported provider source root must be a private directory")
        destination.mkdir(mode=0o700)
        for item in inventory.files:
            provider_descriptor = provider_descriptors.get(item.provider)
            if provider_descriptor is None:
                provider_descriptor = os.open(
                    item.provider,
                    directory_flags,
                    dir_fd=root_descriptor,
                )
                descriptors.append(provider_descriptor)
                provider_info = os.fstat(provider_descriptor)
                if (
                    not stat.S_ISDIR(provider_info.st_mode)
                    or stat.S_IMODE(provider_info.st_mode) != 0o700
                ):
                    raise ValueError(
                        "imported provider source provider root must be a private directory"
                    )
                provider_descriptors[item.provider] = provider_descriptor
                (destination / item.provider).mkdir(mode=0o700)
            source_descriptor = os.open(
                item.sha256,
                file_flags,
                dir_fd=provider_descriptor,
            )
            target_descriptor = -1
            try:
                source_info = os.fstat(source_descriptor)
                if (
                    not stat.S_ISREG(source_info.st_mode)
                    or stat.S_IMODE(source_info.st_mode) != 0o400
                ):
                    raise ValueError("imported provider source is not a read-only regular file")
                target = destination / item.provider / item.sha256
                target_descriptor = os.open(
                    target,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = os.read(source_descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = os.write(target_descriptor, view)
                        if written <= 0:
                            raise OSError("short imported provider source stage write")
                        view = view[written:]
                    digest.update(chunk)
                    size += len(chunk)
                if (digest.hexdigest(), size) != (item.sha256, item.size_bytes):
                    raise ValueError("imported provider source changed during remote staging")
            finally:
                os.close(source_descriptor)
                if target_descriptor >= 0:
                    os.close(target_descriptor)
        observed = {
            f"{provider.name}/{item.name}"
            for provider in destination.iterdir()
            for item in provider.iterdir()
        }
        expected = {f"{item.provider}/{item.sha256}" for item in inventory.files}
        if observed != expected:
            raise ValueError("staged provider source copy differs from its inventory")
    except BaseException:
        if destination.exists():
            shutil.rmtree(destination)
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


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
    return remote_stage_name(value) is not None


def remote_stage_name(root: str) -> str | None:
    """The stage name in a remote stage root, or None when it is not one.

    New stages live at `<remote home>/.rcp/stages/rcp-run.<name>`; the home is
    only known on that host, so the directory probe there checks it exactly.
    """

    candidate = PurePosixPath(root)
    # The same names the shipped stage creator makes, dots included.
    if remote_stage_root.STAGE_NAME.fullmatch(candidate.name) is None:
        return None
    name = candidate.name.removeprefix("rcp-run.")
    parent = candidate.parent
    # Legacy: stages saved before RCP left /tmp. A later release removes this.
    if parent == PurePosixPath("/tmp"):
        return name
    if (
        parent.is_absolute()
        and len(parent.parts) > 3
        and parent.parts[-2:] == (".rcp", "stages")
        and ".." not in parent.parts
    ):
        return name
    return None
