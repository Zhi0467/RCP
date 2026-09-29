"""Stdlib-only command broker staged beside one provider invocation."""

import argparse
import ctypes
import hashlib
import hmac
import json
import os
import pwd
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress

VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024 * 1024 + 64 * 1024
_MAILBOX_ID = re.compile(r"^[a-f0-9]{32}$")
_TOKEN = re.compile(r"^[a-f0-9]{64}$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9._-]+$")
# Mirrors rcp.limits.COMMAND_REJECTION_NOTICE_MAX_COUNT; this file ships alone.
_REJECTION_NOTICE_MAX_COUNT = 20
# Notice signatures carry this prefix, so a signed request can never pass as a notice.
# Mirrors rcp.agents.command_mailbox.REJECTION_NOTICE_DOMAIN.
_REJECTION_NOTICE_DOMAIN = b"rcp-rejection-notice\n"
_rejection_notices = 0
_rejection_notice_lock = threading.Lock()
# Keyed commands this broker already sent, by exact verb, key, and arguments.
_keyed_commands = {}
_keyed_commands_lock = threading.Lock()


class _KeyedCommand:
    def __init__(self):
        self.done = threading.Event()
        self.response = None
        self.sent = False
        self.delivered = False


def _keyed_signature(value):
    key = value.get("idempotency_key")
    if not isinstance(key, str):
        return None
    return json.dumps(
        [value.get("verb"), key, value.get("arguments")],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _join_keyed_command(signature):
    """Return (entry, owner) for one exact keyed call.

    A client gives up before a slow command finishes and the agent repeats the
    call. That repeat waits for the send still in flight, or takes the answer no
    client received, instead of sending again: over a slow SSH stage a second
    round trip can itself outlast the client. A repeat after a delivered answer
    goes to RCP, which replays and audits it, and so does a repeat of a first
    attempt that never reached RCP.
    """

    with _keyed_commands_lock:
        entry = _keyed_commands.get(signature)
        if entry is not None and not (entry.done.is_set() and (entry.delivered or not entry.sent)):
            return entry, False
        entry = _KeyedCommand()
        _keyed_commands[signature] = entry
        return entry, True


class BrokerError(Exception):
    """A request this broker refuses. Reported to the agent as ``invalid``."""


class BrokerUnavailable(BrokerError):
    """Machinery that failed rather than a request that was wrong.

    Kept distinct because the agent acts on the difference: ``invalid`` means
    rewrite the command, ``unavailable`` means the command was fine and the
    plumbing was not. Collapsing the two sends an agent into a correction loop
    over a request that has nothing wrong with it.
    """


def _parser():
    parser = argparse.ArgumentParser(prog="rcp-command-broker")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--mailbox-id", required=True)
    parser.add_argument("--ready-line", required=True)
    parser.add_argument("--response-timeout", required=True, type=float)
    parser.add_argument("--standalone", action="store_true")
    parser.add_argument("provider", nargs=argparse.REMAINDER)
    return parser


def _bootstrap(mailbox_id):
    try:
        line = sys.stdin.buffer.readline(4096)
        value = json.loads(line)
    except (OSError, UnicodeError, ValueError) as exc:
        raise BrokerError(f"broker bootstrap is invalid: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != VERSION:
        raise BrokerError("broker bootstrap version is unsupported")
    if value.get("mailbox_id") != mailbox_id:
        raise BrokerError("broker bootstrap mailbox does not match")
    token = value.get("token")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise BrokerError("broker bootstrap token is malformed")
    return token


_SOCKET_DIRECTORY = "~/.rcp/sockets/"


def _safe_socket_path(path):
    """Resolve `~/.rcp/sockets/rcp-command-*.sock` against this account's home.

    The home comes from the password database, not `HOME`, so the provider's
    client resolves the same path whatever environment it runs in.
    """

    name = path[len(_SOCKET_DIRECTORY) :] if path.startswith(_SOCKET_DIRECTORY) else ""
    if not name.startswith("rcp-command-") or not name.endswith(".sock") or "/" in name:
        raise BrokerError("broker socket is outside the RCP socket directory")
    directory = _socket_directory(pwd.getpwuid(os.geteuid()).pw_dir)
    absolute = os.path.join(directory, name)
    # The short root sits in shared /tmp, so it must be ours and private too.
    _private_directory(os.path.dirname(directory), exact=directory.startswith("/tmp/"))
    _private_directory(directory, exact=True)
    if os.path.lexists(absolute):
        raise BrokerError("broker socket path is already occupied")
    return absolute


def _socket_directory(home):
    """`~/.rcp/sockets`, or a short `/tmp/rcp-<id>/sockets` when that is too deep.

    Mirrors `rcp.rcp_home.command_socket_directory`; this file ships alone.
    """

    default = os.path.join(home, ".rcp", "sockets")
    if len(os.fsencode(default)) + 1 + len("rcp-command-") + 32 + len(".sock") < 100:
        return default
    root = "/tmp/rcp-" + hashlib.sha256(home.encode("utf-8")).hexdigest()[:12]
    return os.path.join(root, "sockets")


def _private_directory(path, *, exact=False):
    with suppress(FileExistsError):
        os.mkdir(path, 0o700)
    info = os.lstat(path)
    if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o022:
        # Ours but made under a 0002 umask, as `rcp.rcp_home` also repairs.
        os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o022)
        info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or (exact and stat.S_IMODE(info.st_mode) != 0o700)
        # Another user who can write the parent could swap the socket folder.
        or info.st_mode & 0o022
    ):
        raise BrokerError(f"RCP socket directory {path} is unsafe")


def _peer_identity(connection):
    if sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        pid, uid, _gid = struct.unpack("3i", raw)
        return pid, uid
    if sys.platform == "darwin":
        pid = struct.unpack("i", connection.getsockopt(0, 2, 4))[0]
        credential = connection.getsockopt(0, 1, 8)
        _version, uid = struct.unpack("II", credential[:8])
        return pid, uid
    raise BrokerUnavailable("execution host cannot authenticate Unix-socket peers")


class _DarwinProcessInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("command", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32),
        ("tty_device", ctypes.c_uint32),
        ("tty_pgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_seconds", ctypes.c_uint64),
        ("start_microseconds", ctypes.c_uint64),
    ]


def _process_record(pid):
    """Return a kernel-sourced parent pid and process birth identity."""

    if not isinstance(pid, int) or pid <= 0:
        raise BrokerUnavailable("process identity is malformed")
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as stream:
                content = stream.read(4096)
        except OSError as exc:
            raise BrokerUnavailable(f"process ancestry is unavailable: {exc}") from exc
        closing = content.rfind(b")")
        fields = content[closing + 2 :].split() if closing >= 0 else []
        if len(fields) < 20:
            raise BrokerUnavailable("process ancestry record is malformed")
        try:
            return int(fields[1]), ("linux", int(fields[19]))
        except ValueError as exc:
            raise BrokerUnavailable("process ancestry record is malformed") from exc
    if sys.platform == "darwin":
        try:
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            proc_pidinfo = library.proc_pidinfo
            proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            proc_pidinfo.restype = ctypes.c_int
            record = _DarwinProcessInfo()
            size = proc_pidinfo(
                pid,
                3,  # PROC_PIDTBSDINFO
                0,
                ctypes.byref(record),
                ctypes.sizeof(record),
            )
        except (AttributeError, OSError) as exc:
            raise BrokerUnavailable(f"process ancestry is unavailable: {exc}") from exc
        if size != ctypes.sizeof(record) or record.pid != pid:
            error = ctypes.get_errno()
            detail = os.strerror(error) if error else "kernel process record is incomplete"
            raise BrokerUnavailable(f"process ancestry is unavailable: {detail}")
        return int(record.ppid), (
            "darwin",
            int(record.start_seconds),
            int(record.start_microseconds),
        )
    raise BrokerUnavailable("execution host cannot authenticate process ancestry")


def _is_live_descendant(pid, root_pid, root_birth):
    _parent, current_root_birth = _process_record(root_pid)
    if current_root_birth != root_birth:
        return False
    current = pid
    visited = set()
    for _depth in range(1024):
        if current == root_pid:
            return True
        if current <= 1 or current in visited:
            return False
        visited.add(current)
        current, _birth = _process_record(current)
    return False


def _preflight_peer_identity(server, socket_path):
    """Prove the host kernel contract before any provider can receive its prompt."""

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection = None
    try:
        probe.connect(socket_path)
        connection, _address = server.accept()
        pid, uid = _peer_identity(connection)
        _process_record(pid)
        if pid != os.getpid() or uid != os.getuid():
            raise BrokerError("execution host returned a mismatched broker peer identity")
    finally:
        probe.close()
        if connection is not None:
            connection.close()


def _read_message(connection):
    content = bytearray()
    while len(content) <= MAX_REQUEST_BYTES:
        chunk = connection.recv(min(65536, MAX_REQUEST_BYTES + 1 - len(content)))
        if not chunk:
            break
        content.extend(chunk)
        if content.endswith(b"\n"):
            break
    if not content.endswith(b"\n") or len(content) > MAX_REQUEST_BYTES:
        raise BrokerError("broker request is missing, incomplete, or oversized")
    try:
        value = json.loads(content)
    except (UnicodeError, ValueError) as exc:
        raise BrokerError(f"broker request is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BrokerError("broker request must be one object")
    return value


def _atomic_json(workspace, name, value):
    if not _SAFE_FILE.fullmatch(name):
        raise BrokerError("broker request file name is unsafe")
    descriptor, temporary = tempfile.mkstemp(prefix=".rcp-command-", dir=workspace)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, os.path.join(workspace, name))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_response(path, request_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raise BrokerUnavailable("command response is not a regular file")
            with open(path, encoding="utf-8") as stream:
                value = json.load(stream)
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            raise BrokerUnavailable(f"command response is unavailable: {exc}") from exc
        if not isinstance(value, dict) or value.get("request_id") != request_id:
            raise BrokerUnavailable("command response identity does not match")
        return value
    raise BrokerUnavailable("command response timed out")


def _error(request_id, message, status, delivery):
    return {
        "version": VERSION,
        "request_id": request_id
        if isinstance(request_id, str) and len(request_id) == 32
        else "0" * 32,
        "status": status,
        "message": message[:2000],
        # `not_sent`: RCP never received it. `unknown`: RCP has it and may still act.
        "result": {"delivery": delivery},
    }


def _signed(value, token, domain=b""):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    signature = hmac.new(token.encode("ascii"), domain + payload, hashlib.sha256).hexdigest()
    return {**value, "credential": signature}


def _publish_rejection(workspace, mailbox_id, token, status, message):
    """Leave a signed refusal notice so RCP can record a command it never received."""

    global _rejection_notices
    with _rejection_notice_lock:
        if _rejection_notices >= _REJECTION_NOTICE_MAX_COUNT:
            return
        _rejection_notices += 1
    notice_id = os.urandom(16).hex()
    notice = {
        "version": VERSION,
        "mailbox_id": mailbox_id,
        "notice_id": notice_id,
        "status": status,
        "message": message[:2000],
    }
    with suppress(BrokerError, OSError):
        _atomic_json(
            workspace,
            f"rcp-command-{mailbox_id}-{notice_id}.rejected.json",
            _signed(notice, token, _REJECTION_NOTICE_DOMAIN),
        )


def _unavailable(request_id, error, written):
    if written:
        return _error(
            request_id,
            f"RCP has this command but its answer did not arrive ({error}); "
            "it may still take effect. Repeat the exact same command to get the result.",
            "unavailable",
            "unknown",
        )
    return _error(request_id, f"RCP command was not sent: {error}", "unavailable", "not_sent")


def _joined_response(entry, request_id, response_timeout):
    """Return (response, from_entry); only an entry's own answer can mark it delivered."""

    if not entry.done.wait(response_timeout):
        return _unavailable(request_id, "command response timed out", True), False
    return {**entry.response, "request_id": request_id}, True


def _handle(
    connection,
    *,
    root_pid,
    root_birth,
    expected_session,
    mailbox_id,
    token,
    workspace,
    response_timeout,
):
    request_id = None
    written = False
    entry = None
    owner = False
    answers_entry = False
    try:
        pid, uid = _peer_identity(connection)
        value = _read_message(connection)
        request_id = value.get("request_id")
        if uid != os.getuid() or not _is_live_descendant(pid, root_pid, root_birth):
            raise BrokerError("command client is outside the current provider invocation")
        if expected_session is not None and os.getsid(pid) != expected_session:
            raise BrokerError("command client is outside the current provider invocation")
        if value.get("version") != VERSION or value.get("mailbox_id") != mailbox_id:
            raise BrokerError("command request mailbox identity does not match")
        signature = _keyed_signature(value)
        entry, owner = _join_keyed_command(signature) if signature else (None, True)
        if not owner:
            written = True  # the first send of this exact keyed call carries it
            response, answers_entry = _joined_response(entry, request_id, response_timeout)
        else:
            unsigned = dict(value)
            unsigned.pop("credential", None)
            name = f"rcp-command-{mailbox_id}-{request_id}.request.json"
            response_name = name.removesuffix(".request.json") + ".response.json"
            _atomic_json(workspace, name, _signed(unsigned, token))
            written = True
            response = _read_response(
                os.path.join(workspace, response_name), request_id, response_timeout
            )
    except BrokerUnavailable as exc:
        response = _unavailable(request_id, exc, written)
    except (BrokerError, ValueError) as exc:
        response = _error(request_id, f"RCP command rejected: {exc}", "invalid", "not_sent")
    except OSError as exc:
        # Sockets and the workspace filesystem. The request itself was fine.
        response = _unavailable(request_id, exc, written)
    if owner and entry is not None:
        entry.response = response
        entry.sent = written
        entry.done.set()
        answers_entry = True
    if not written:
        _publish_rejection(workspace, mailbox_id, token, response["status"], response["message"])
    try:
        connection.sendall(
            json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        if answers_entry:
            entry.delivered = True
    except OSError:
        pass
    finally:
        connection.close()


def _serve(
    server,
    stop,
    *,
    root_pid,
    root_birth,
    expected_session,
    mailbox_id,
    token,
    workspace,
    response_timeout,
):
    server.settimeout(0.2)
    workers = []
    while not stop.is_set():
        try:
            connection, _address = server.accept()
        # Python 3.9 keeps socket.timeout distinct from builtin TimeoutError.
        except socket.timeout:  # noqa: UP041
            workers = [worker for worker in workers if worker.is_alive()]
            continue
        except OSError:
            break
        worker = threading.Thread(
            target=_handle,
            kwargs={
                "connection": connection,
                "root_pid": root_pid,
                "root_birth": root_birth,
                "expected_session": expected_session,
                "mailbox_id": mailbox_id,
                "token": token,
                "workspace": workspace,
                "response_timeout": response_timeout,
            },
            daemon=True,
        )
        worker.start()
        workers = [worker, *(existing for existing in workers if existing.is_alive())]
    for worker in workers:
        worker.join(timeout=1)


def _copy(source, destination, close_destination=False):
    try:
        while True:
            # `read` waits for a full buffer, which strands the short lines of an
            # interactive provider protocol until 64 KiB accumulates or the pipe
            # closes. `read1` hands over whatever one raw read produced.
            chunk = source.read1(65536)
            if not chunk:
                break
            destination.write(chunk)
            destination.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        if close_destination:
            with suppress(OSError):
                destination.close()


def main(argv=None):
    namespace = _parser().parse_args(argv)
    mailbox_id = namespace.mailbox_id
    if not _MAILBOX_ID.fullmatch(mailbox_id):
        print("broker mailbox id is malformed", file=sys.stderr)
        return 2
    response_timeout = namespace.response_timeout
    if not (response_timeout > 0) or response_timeout == float("inf"):
        print("broker response timeout must be a positive finite number", file=sys.stderr)
        return 2
    workspace = os.getcwd()
    if os.path.islink(workspace) or not os.path.isdir(workspace):
        print("broker workspace is unavailable", file=sys.stderr)
        return 2
    socket_path = None
    server = None
    child = None
    stop = threading.Event()
    try:
        token = _bootstrap(mailbox_id)
        socket_path = _safe_socket_path(namespace.socket)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(socket_path)
        os.chmod(socket_path, 0o600)
        server.listen(16)
        _preflight_peer_identity(server, socket_path)
        if namespace.standalone:
            if namespace.provider:
                raise BrokerError("standalone broker does not accept a provider command")
            root_pid = os.getppid()
            expected_session = os.getsid(0)
        else:
            provider = list(namespace.provider)
            if provider and provider[0] == "--":
                provider.pop(0)
            if not provider:
                raise BrokerError("broker provider command is missing")
            child = subprocess.Popen(
                provider,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            root_pid = child.pid
            expected_session = None
        _parent_pid, root_birth = _process_record(root_pid)
        serving = threading.Thread(
            target=_serve,
            args=(server, stop),
            kwargs={
                "root_pid": root_pid,
                "root_birth": root_birth,
                "expected_session": expected_session,
                "response_timeout": response_timeout,
                "mailbox_id": mailbox_id,
                "token": token,
                "workspace": workspace,
            },
            daemon=True,
        )
        serving.start()
        if namespace.standalone:
            print(namespace.ready_line, flush=True)
            sys.stdin.buffer.read()
            return 0
        assert child is not None
        assert child.stdin is not None and child.stdout is not None and child.stderr is not None
        print(namespace.ready_line, flush=True)
        stdin_thread = threading.Thread(
            target=_copy, args=(sys.stdin.buffer, child.stdin, True), daemon=True
        )
        stderr_thread = threading.Thread(
            target=_copy, args=(child.stderr, sys.stderr.buffer), daemon=True
        )
        stdin_thread.start()
        stderr_thread.start()
        _copy(child.stdout, sys.stdout.buffer)
        return child.wait()
    except (BrokerError, OSError, ValueError) as exc:
        print(f"RCP command broker failed: {exc}", file=sys.stderr)
        return 2
    finally:
        stop.set()
        if server is not None:
            server.close()
        if child is not None and child.poll() is None:
            with suppress(OSError):
                child.terminate()
            with suppress(subprocess.TimeoutExpired):
                child.wait(timeout=2)
            if child.poll() is None:
                with suppress(OSError):
                    child.kill()
                child.wait()
        if socket_path is not None:
            with suppress(FileNotFoundError):
                os.unlink(socket_path)


if __name__ == "__main__":
    raise SystemExit(main())
