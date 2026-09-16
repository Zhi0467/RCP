"""Stdlib execution-host durability for one provider pass; no graph authority.

Stdin remains the live control channel and is never persisted. Stdout is drained
independently of the SSH uplink, with explicit storage and memory ceilings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import signal
import stat
import subprocess
import time
from contextlib import suppress
from pathlib import Path


class WireCompletion:
    """Observe completion identity only; the provider runtime decodes answers."""

    def __init__(self, runtime_id: str):
        self.runtime_id = runtime_id
        self.requests = {}
        self.message_ids = set()
        self.outstanding = set()
        self.steer_requests = {}
        self.thread_id = None
        self.turn_id = None
        self.terminal = False
        self.complete = False

    @property
    def prompt_started(self):
        if self.runtime_id == "codex.app-server-stdio.v1":
            return "turn/start" in self.requests.values()
        if self.runtime_id == "claude.stream-json.v1":
            return bool(self.message_ids)
        return True

    def input(self, value):
        if not isinstance(value, dict):
            return
        if value.get("type") == "user" and isinstance(value.get("uuid"), str):
            self.message_ids.add(value["uuid"])
        method = value.get("method")
        identifier = value.get("id")
        if method in {"thread/start", "thread/resume", "turn/start"}:
            self.requests[identifier] = method
        elif method == "turn/steer":
            params = value.get("params", {})
            self.steer_requests[identifier] = params.get("expectedTurnId")

    def output(self, value):
        if not isinstance(value, dict) or self.terminal:
            return
        if self.runtime_id == "codex.app-server-stdio.v1":
            result = value.get("result")
            method = self.requests.get(value.get("id"))
            if isinstance(result, dict):
                if method in {"thread/start", "thread/resume"}:
                    self.thread_id = result.get("thread", {}).get("id")
                elif method == "turn/start":
                    self.turn_id = result.get("turn", {}).get("id")
            if value.get("error") is not None and method is not None:
                self.terminal = True
                return
            params = value.get("params")
            thread = params.get("threadId") if isinstance(params, dict) else None
            if isinstance(thread, str) and self.thread_id is not None and thread != self.thread_id:
                return
            # The canonical decoder fences any server-to-client request before
            # it inspects params or the thread, because an unattended turn
            # cannot answer one. Checking it later here let a request carrying
            # no params, or arriving before thread/start replied, keep stdin
            # open on a link this wrapper exists to survive.
            if "id" in value and "method" in value:
                self.terminal = True
                return
            if not isinstance(params, dict) or self.thread_id is None:
                return
            if value.get("method") == "error" and params.get("willRetry") is not True:
                self.terminal = True
                return
            turn = params.get("turn")
            if (
                value.get("method") == "turn/completed"
                and isinstance(turn, dict)
                and self.turn_id is not None
                and turn.get("id") == self.turn_id
            ):
                self.terminal = True
                self.complete = turn.get("status") == "completed"
            return
        kind = value.get("type")
        if self.runtime_id == "claude.stream-json.v1":
            identifier = value.get("command_uuid")
            if kind == "command_lifecycle" and identifier in self.message_ids:
                if value.get("state") in {"queued", "started"}:
                    self.outstanding.add(identifier)
                elif value.get("state") == "completed":
                    self.outstanding.discard(identifier)
            if kind == "error":
                self.terminal = True
                return
            if kind != "result":
                return
            identifiers = value.get("user_message_uuids")
            finished = (
                {item for item in identifiers if isinstance(item, str) and item.strip()}
                if isinstance(identifiers, list)
                else set()
            )
            identifier = value.get("user_message_uuid")
            if not finished and isinstance(identifier, str) and identifier.strip():
                finished.add(identifier)
            self.outstanding.difference_update(finished)
            failed = (
                value.get("is_error") is True
                or "error" in str(value.get("subtype") or "").casefold()
            )
            if finished and self.outstanding and not failed:
                return
            self.terminal = True
            self.complete = not failed
        elif kind in {"turn.completed", "turn.failed", "error"}:
            self.terminal = True
            self.complete = kind == "turn.completed"


class Lines:
    def __init__(self, limit, consume):
        self.limit = limit
        self.consume = consume
        self.pending = bytearray()
        self.discarding = False

    def feed(self, data, stop_when=None):
        pieces = data.split(b"\n")
        offset = 0
        for index, piece in enumerate(pieces):
            if self.discarding or len(self.pending) + len(piece) > self.limit:
                self.pending.clear()
                self.discarding = True
            else:
                self.pending.extend(piece)
            if index < len(pieces) - 1:
                offset += len(piece) + 1
                if not self.discarding:
                    with suppress(ValueError, UnicodeError, TypeError, AttributeError):
                        self.consume(json.loads(self.pending))
                self.pending.clear()
                self.discarding = False
                if stop_when is not None and stop_when():
                    return offset
        return len(data)


def _atomic_write(path, data):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _patch_snapshot(source, destination, limit):
    source = Path(source)
    try:
        directory_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False, None
    try:
        try:
            descriptor = os.open(
                source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
        except FileNotFoundError:
            return False, None
    finally:
        os.close(directory_fd)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Completed Patch is not a bounded regular file.")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Completed Patch exceeds the snapshot limit.")
    _atomic_write(destination, data)
    return True, hashlib.sha256(data).hexdigest()


def run(args):
    if os.getpid() != os.getpgrp():
        raise ValueError("Turn journal requires its own process group.")
    directory = Path(args.pid_file + ".turn")
    directory.mkdir(mode=0o700)  # Never overwrite another pass's evidence.
    observer = WireCompletion(args.runtime_id)
    inputs = Lines(args.max_event_bytes, observer.input)
    outputs = Lines(args.max_event_bytes, observer.output)
    pending = {1: bytearray(), 2: bytearray()}
    upstream_open = {1: True, 2: True}
    input_pending = bytearray()
    detached = False
    stdin_open = True
    terminal_at = None
    signalled_at = None
    external_stop = False
    error = None
    journal_bytes = 0
    stderr_bytes = 0
    stderr_truncated = False
    journal_complete = True
    events_hash = hashlib.sha256()
    started = time.monotonic()

    def stop_requested(_signal, _frame):
        nonlocal external_stop
        external_stop = True

    signal.signal(signal.SIGTERM, stop_requested)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    child = subprocess.Popen(
        args.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert child.stdin and child.stdout and child.stderr
    for descriptor in (0, 1, 2, child.stdin.fileno(), child.stdout.fileno(), child.stderr.fileno()):
        os.set_blocking(descriptor, False)
    child_streams = {child.stdout.fileno(): 1, child.stderr.fileno(): 2}
    with (
        (directory / "events.jsonl").open("xb") as events,
        (directory / "stderr.txt").open("xb") as errors,
    ):
        while child_streams or child.poll() is None:
            now = time.monotonic()
            if observer.terminal and terminal_at is None:
                terminal_at = now
                input_pending.clear()
                child.stdin.close()  # Fence all later input before forwarding completion.
            if external_stop or error or terminal_at is not None:
                if not child.stdin.closed:
                    child.stdin.close()
                stdin_open = False
                if signalled_at is None and now - started >= args.stop_hold_seconds:
                    # Wrapper owns this group; ignore only our own TERM while all
                    # provider/broker descendants receive it in the same delivery.
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    os.killpg(os.getpgrp(), signal.SIGTERM)
                    signalled_at = now
                elif signalled_at is not None and now - signalled_at >= args.stop_grace_seconds:
                    # A stubborn descendant must not outlive the original fence.
                    # No receipt is published unless the group drains cleanly.
                    os.killpg(os.getpgrp(), signal.SIGKILL)
            read_fds = list(child_streams)
            if stdin_open and not detached and len(input_pending) < args.max_event_bytes:
                read_fds.append(0)
            write_fds = [fd for fd, data in pending.items() if data and upstream_open[fd]]
            if input_pending and not child.stdin.closed:
                write_fds.append(child.stdin.fileno())
            readable, writable, _ = select.select(read_fds, write_fds, [], args.poll_seconds)
            for descriptor in readable:
                try:
                    data = os.read(descriptor, 65536)
                except BlockingIOError:
                    continue
                if descriptor == 0:
                    if terminal_at is not None:
                        stdin_open = False
                        continue
                    if not data:
                        stdin_open = False
                        # EOF after link loss is not an instruction to stop.
                        if (
                            (args.close_input_after_initial or not observer.prompt_started)
                            and not input_pending
                            and not child.stdin.closed
                        ):
                            child.stdin.close()
                    else:
                        inputs.feed(data)
                        input_pending.extend(data)
                    continue
                channel = child_streams[descriptor]
                if not data:
                    del child_streams[descriptor]
                    continue
                if channel == 1:
                    if observer.terminal:
                        continue  # Drain post-turn output without authorizing or replaying it.
                    consumed = outputs.feed(data, stop_when=lambda: observer.terminal)
                    data = data[:consumed]
                    if journal_bytes + len(data) <= args.max_journal_bytes and journal_complete:
                        events.write(data)
                        events.flush()
                        events_hash.update(data)
                        journal_bytes += len(data)
                    else:
                        journal_complete = False
                        error = "Provider journal exceeded its storage limit."
                else:
                    kept = data[: max(0, args.max_stderr_bytes - stderr_bytes)]
                    errors.write(kept)
                    errors.flush()
                    stderr_bytes += len(kept)
                    stderr_truncated |= len(kept) != len(data)
                # Stop accepting input as soon as a terminal event is read,
                # before that same event can reach the live consumer.
                if observer.terminal and terminal_at is None:
                    terminal_at = time.monotonic()
                    input_pending.clear()
                    child.stdin.close()
                if not detached:
                    if sum(map(len, pending.values())) + len(data) > args.max_uplink_bytes:
                        detached = True
                        pending[1].clear()
                        pending[2].clear()
                    else:
                        pending[channel].extend(data)
            for descriptor in writable:
                if descriptor in pending:
                    buffer = pending[descriptor]
                elif child.stdin.closed:
                    continue
                else:
                    buffer = input_pending
                try:
                    written = os.write(descriptor, buffer)
                    del buffer[:written]
                except BlockingIOError:
                    pass
                except (BrokenPipeError, OSError):
                    if descriptor in pending:
                        detached = True
                        upstream_open[descriptor] = False
                        pending[1].clear()
                        pending[2].clear()
                    else:
                        input_pending.clear()
                        child.stdin.close()
            if (
                (args.close_input_after_initial or not observer.prompt_started)
                and not stdin_open
                and not input_pending
                and not child.stdin.closed
            ):
                child.stdin.close()
            # These run every pass of the loop, including the passes after an
            # earlier failure has already begun the stop. The first cause is the
            # one that explains the turn, so it is never overwritten by a later
            # condition that failure itself produced.
            if detached and not observer.prompt_started:
                error = error or "Provider uplink closed before prompt delivery."
            # Input identities are bounded as well as payload buffers.
            if (
                len(observer.message_ids) + len(observer.requests) + len(observer.steer_requests)
                > args.max_control_messages
            ):
                error = error or "Provider control metadata exceeded its storage limit."
        events.flush()
        errors.flush()
        os.fsync(events.fileno())
        os.fsync(errors.fileno())
    return_code = child.wait()
    patch_present = False
    patch_sha256 = None
    if observer.complete and journal_complete and not error and not external_stop:
        try:
            patch_present, patch_sha256 = _patch_snapshot(
                args.patch_path, directory / "patch.json", args.max_patch_bytes
            )
        except (OSError, ValueError) as exc:
            error = str(exc)
    flush_deadline = time.monotonic() + args.stop_grace_seconds
    while any(pending.values()) and not detached and time.monotonic() < flush_deadline:
        ready = select.select(
            [], [fd for fd, data in pending.items() if data], [], args.poll_seconds
        )[1]
        for descriptor in ready:
            try:
                written = os.write(descriptor, pending[descriptor])
                del pending[descriptor][:written]
            except BlockingIOError:
                pass
            except OSError:
                detached = True
    detached |= any(pending.values())
    outcome = {
        "version": 1,
        "pid_file": args.pid_file,
        "provider": args.provider,
        "runtime_id": args.runtime_id,
        "provider_version": args.provider_version,
        "return_code": return_code,
        "protocol_complete": observer.complete,
        "terminal_event": observer.terminal,
        "journal_complete": journal_complete,
        "error": error,
        "stopped": external_stop,
        "uplink_detached": detached,
        "events_sha256": events_hash.hexdigest(),
        "patch_present": patch_present,
        "patch_sha256": patch_sha256,
        "stderr_truncated": stderr_truncated,
        "root_thread_id": observer.thread_id,
        "root_turn_id": observer.turn_id,
        "input_message_ids": sorted(observer.message_ids),
        "steer_requests": observer.steer_requests,
    }
    _atomic_write(directory / "outcome.json", json.dumps(outcome, sort_keys=True).encode())
    return 0 if observer.complete and not error and not external_stop else (return_code or 1)


def main(argv=None):
    parser = argparse.ArgumentParser()
    for name in ("pid-file", "provider", "runtime-id", "patch-path"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--provider-version")
    for name in (
        "max-journal-bytes",
        "max-event-bytes",
        "max-stderr-bytes",
        "max-patch-bytes",
        "max-uplink-bytes",
        "max-control-messages",
    ):
        parser.add_argument("--" + name, type=int, required=True)
    for name in ("stop-hold-seconds", "stop-grace-seconds", "poll-seconds"):
        parser.add_argument("--" + name, type=float, required=True)
    parser.add_argument("--close-input-after-initial", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command.pop(0)
    try:
        return run(args)
    except Exception:
        # A disk/write failure must not strand a provider behind an undrained
        # pipe. The group is this wrapper's exact ownership boundary. Preserve
        # the credential startup hold even on this exceptional cleanup path.
        if os.getpid() == os.getpgrp():
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(args.stop_hold_seconds)
            os.killpg(os.getpgrp(), signal.SIGKILL)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
