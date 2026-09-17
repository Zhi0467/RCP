"""Keep one provider turn's evidence on the machine that runs it.

RCP ships this module with `TurnFence` prepended and runs the result with
``python -c``. Keeping the executable source in a real module lets ruff, the
formatter, and `tests/test_remote_turn_supervisor.py` see it.

Stdin stays the live control channel and is never persisted. Stdout is drained
and journalled whether or not the uplink is still there, under explicit storage
and memory ceilings.

Nothing here judges the turn. The journal is evidence; `outcome.json` records
mechanical facts about the pass -- what exited, what was fenced, what was
truncated -- and RCP's own decoder reads `events.jsonl` to decide what the turn
was worth.
"""

import argparse
import hashlib
import json
import os
import select
import signal
import stat
import subprocess
import time
from pathlib import Path

if "TurnFence" not in globals():
    from rcp.agents.remote_turn_fence import TurnFence


class Lines:
    """Feed whole JSON lines to an observer under a per-line ceiling."""

    def __init__(self, limit, consume):
        self.limit = limit
        self.consume = consume
        self.buffer = bytearray()
        self.overflowed = False

    def flush(self):
        if self.buffer and not self.overflowed:
            self._line(bytes(self.buffer))
        self.buffer.clear()
        self.overflowed = False

    def _line(self, raw):
        try:
            # A provider may emit a byte that is not valid UTF-8. Reading it the
            # way the live pipe reads it keeps one wire from being understood two
            # different ways depending on which end is listening.
            self.consume(json.loads(raw.decode("utf-8", "surrogateescape")))
        except ValueError:
            return

    def feed(self, data, stop_when=None):
        consumed = 0
        for chunk in memoryview(data).tobytes().splitlines(keepends=True):
            consumed += len(chunk)
            if not chunk.endswith(b"\n"):
                if len(self.buffer) + len(chunk) > self.limit:
                    self.overflowed = True
                    self.buffer.clear()
                else:
                    self.buffer.extend(chunk)
                continue
            if self.overflowed or len(self.buffer) + len(chunk) > self.limit:
                self.buffer.clear()
                self.overflowed = False
                continue
            self.buffer.extend(chunk)
            line = bytes(self.buffer)
            self.buffer.clear()
            self._line(line)
            if stop_when is not None and stop_when():
                break
        return consumed


def _atomic_write(path, data):
    temporary = path.with_name(path.name + ".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _patch_snapshot(source, destination, limit):
    """Copy the turn's Patch beside its journal, or report that there was none."""

    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (FileNotFoundError, NotADirectoryError):
        return False, None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("The provider's patch file is not a regular file.")
        if info.st_size > limit:
            raise ValueError("The provider's patch file exceeds its storage limit.")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("The provider's patch file exceeds its storage limit.")
    _atomic_write(destination, content)
    return True, hashlib.sha256(content).hexdigest()


def run(args):
    if os.getpid() != os.getpgrp():
        raise ValueError("The turn supervisor requires its own process group.")
    directory = Path(args.pid_file + ".turn")
    directory.mkdir(mode=0o700)  # Never overwrite another pass's evidence.
    fence = TurnFence(args.runtime_id)
    inputs = Lines(args.max_event_bytes, fence.input)
    outputs = Lines(args.max_event_bytes, fence.output)
    pending = {1: bytearray(), 2: bytearray()}
    upstream_open = {1: True, 2: True}
    input_pending = bytearray()
    accepted = False
    detached = False
    terminal_uplinked = False
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

    def accept():
        """Say, on the host, that this pass now owns the prompt RCP handed it.

        Written before a single byte reaches the provider, and never unwritten.
        After this file exists the work may have begun, and no controller that
        cannot find its own receipt may launch a replacement -- a missing receipt
        is RCP's silence about the pass, not the host's answer about it.
        """

        nonlocal accepted
        if accepted:
            return
        _atomic_write(
            directory / "accepted.json",
            json.dumps(
                {"version": 1, "pid_file": args.pid_file, "at": time.time()}, sort_keys=True
            ).encode(),
        )
        accepted = True

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
            if fence.terminal and terminal_at is None:
                terminal_at = now
                input_pending.clear()
                child.stdin.close()  # Fence all later input before forwarding completion.
            if external_stop or error or terminal_at is not None:
                if not child.stdin.closed:
                    child.stdin.close()
                stdin_open = False
                if signalled_at is None and now - started >= args.stop_hold_seconds:
                    # This process owns the group; ignore only our own TERM while
                    # every provider descendant takes it in the same delivery.
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    os.killpg(os.getpgrp(), signal.SIGTERM)
                    signalled_at = now
                elif signalled_at is not None and now - signalled_at >= args.stop_grace_seconds:
                    # A stubborn descendant must not outlive the original fence.
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
                            (args.close_input_after_initial or not fence.prompt_started)
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
                    if channel == 1:
                        outputs.flush()
                        # A completion only surfaces here once this channel's
                        # bytes were forwarded, so it travelled with them.
                        terminal_uplinked = terminal_uplinked or (fence.terminal and not detached)
                    continue
                if channel == 1:
                    if fence.terminal:
                        continue  # Drain post-turn output without replaying it.
                    consumed = outputs.feed(data, stop_when=lambda: fence.terminal)
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
                if fence.terminal and terminal_at is None:
                    terminal_at = time.monotonic()
                    input_pending.clear()
                    child.stdin.close()
                if not detached:
                    if sum(map(len, pending.values())) + len(data) > args.max_uplink_bytes:
                        detached = True
                        terminal_uplinked = terminal_uplinked and not pending[1]
                        pending[1].clear()
                        pending[2].clear()
                    else:
                        pending[channel].extend(data)
                        # The completion travels at the tail of this chunk: the
                        # output reader stops feeding at the terminal event, and
                        # channel 1 is drained without forwarding after it.
                        terminal_uplinked = terminal_uplinked or (channel == 1 and fence.terminal)
            for descriptor in writable:
                if descriptor in pending:
                    buffer = pending[descriptor]
                elif child.stdin.closed:
                    continue
                else:
                    # Once this batch contains the prompt, say so on the host
                    # before any of it moves. Earlier protocol setup is not a
                    # delivered turn and must not block a safe runtime fallback.
                    if fence.prompt_started:
                        accept()
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
                        # A failure on either channel drops both buffers, so the
                        # completion can be lost to a write error on the other.
                        terminal_uplinked = terminal_uplinked and not pending[1]
                        pending[1].clear()
                        pending[2].clear()
                    else:
                        input_pending.clear()
                        child.stdin.close()
            if (
                (args.close_input_after_initial or not fence.prompt_started)
                and not stdin_open
                and not input_pending
                and not child.stdin.closed
            ):
                child.stdin.close()
            # These run every pass, including the passes after an earlier failure
            # has already begun the stop. The first cause is the one that explains
            # the turn, so a later condition that failure produced never
            # overwrites it.
            if detached and not fence.prompt_started:
                error = error or "Provider uplink closed before prompt delivery."
            if (
                len(fence.message_ids) + len(fence.requests) + len(fence.steer_requests)
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
    if fence.terminal and journal_complete and not error and not external_stop:
        try:
            patch_snapshot = _patch_snapshot(
                args.patch_path, directory / "patch.json", args.max_patch_bytes
            )
            patch_present, patch_sha256 = patch_snapshot
        except (OSError, ValueError) as exc:
            error = str(exc)
    if error and not detached:
        # A failure of this supervisor's own is otherwise recorded only in
        # `outcome.json`, which nothing reads while the link is up. Stderr is the
        # channel a human already sees, and this rides out on it with the
        # provider's own, under the same grace period.
        pending[2].extend(error.encode("utf-8", "replace") + b"\n")
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
    _atomic_write(
        directory / "outcome.json",
        json.dumps(
            {
                "version": 1,
                "pid_file": args.pid_file,
                "provider": args.provider,
                "runtime_id": args.runtime_id,
                "provider_version": args.provider_version,
                "return_code": return_code,
                "accepted": accepted,
                "terminal_event": fence.terminal,
                "journal_complete": journal_complete,
                "error": error,
                "stopped": external_stop,
                "uplink_detached": detached,
                "events_sha256": events_hash.hexdigest(),
                "patch_present": patch_present,
                "patch_sha256": patch_sha256,
                "stderr_truncated": stderr_truncated,
                "root_thread_id": fence.thread_id,
                "root_turn_id": fence.turn_id,
                "input_message_ids": list(fence.message_ids),
                "steer_requests": {str(key): value for key, value in fence.steer_requests.items()},
            },
            sort_keys=True,
        ).encode(),
    )
    if bool(error) or external_stop:
        return _exit_status(return_code)
    if fence.terminal:
        # Zero says the reader already has the whole turn, not that the turn went
        # well -- whoever read those events decides that, as they always have. A
        # controller can stop draining while the link stays up, backing the uplink
        # past its ceiling; the turn still ended and the journal still holds it,
        # but RCP never saw the end. 255 is what RCP already reads as a link that
        # ended a turn rather than work that did.
        return 0 if terminal_uplinked and not pending[1] else 255
    # The provider ended without reaching its own terminal event, so its exit
    # status is the only account of the pass and it travels unaltered. The
    # supervisor's own fence never ran here, so nothing of ours is in it.
    return _exit_status(return_code)


def _exit_status(return_code):
    """A child's outcome as a status this process can actually exit with.

    A signalled child reports a negative number that would wrap into an
    unrelated status on the way out, and zero here would claim a success nobody
    established.
    """

    return return_code if return_code > 0 else 1


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
        # A disk or write failure must not strand a provider behind an undrained
        # pipe. The group is this supervisor's exact ownership boundary. Preserve
        # the credential startup hold even on this exceptional cleanup path.
        if os.getpid() == os.getpgrp():
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(args.stop_hold_seconds)
            os.killpg(os.getpgrp(), signal.SIGKILL)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
