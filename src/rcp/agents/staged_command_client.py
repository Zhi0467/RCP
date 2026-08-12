"""Stdlib-only client staged into an agent run workspace.

This file is deliberately self-contained. RCP ships its source verbatim to a
local or SSH execution stage, where no RCP installation is assumed.
"""

import argparse
import json
import math
import os
import re
import socket
import tempfile
import time
import uuid

VERSION = 1
OK = 0
INVALID = 1
UNAVAILABLE = 2
COMMAND_MAILBOX_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_MAILBOX_ID = re.compile(r"^[a-f0-9]{32}$")
_TOKEN = re.compile(r"^[a-f0-9]{64}$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9._-]+$")
_MUTATING = frozenset(("spawn", "pause", "resume", "stop", "message", "watch_graph", "finish"))


class ClientInputError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ClientInputError(message)


def _atomic_json(path, value):
    directory = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".rcp-command-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _regular_workspace_file(workspace, path, label):
    absolute = os.path.abspath(path)
    if os.path.dirname(absolute) != workspace:
        raise ClientInputError(f"{label} must be a direct file in this run workspace")
    name = os.path.basename(absolute)
    if not _SAFE_FILE.match(name):
        raise ClientInputError(f"{label} has an unsafe file name")
    if os.path.islink(absolute) or not os.path.isfile(absolute):
        raise ClientInputError(f"{label} is unavailable or not a regular file")
    return absolute


def _read_json(path, label):
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ClientInputError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise ClientInputError(f"{label} must contain one JSON object")
    return value


def _credential(workspace, path):
    path = _regular_workspace_file(workspace, path, "credential")
    value = _read_json(path, "credential")
    if value.get("version") != VERSION:
        raise ClientInputError("credential protocol version is unsupported")
    mailbox_id = value.get("mailbox_id")
    token = value.get("token")
    if not isinstance(mailbox_id, str) or not _MAILBOX_ID.match(mailbox_id):
        raise ClientInputError("credential mailbox id is malformed")
    if not isinstance(token, str) or not _TOKEN.match(token):
        raise ClientInputError("credential token is malformed")
    return mailbox_id, token


def _parser():
    parser = _Parser(prog="rcp-agent-client")
    authority = parser.add_mutually_exclusive_group(required=True)
    authority.add_argument("--credential")
    authority.add_argument("--broker")
    parser.add_argument("--mailbox-id")
    parser.add_argument("--timeout", required=True, type=float)
    parser.add_argument("--workspace", required=True)
    subparsers = parser.add_subparsers(dest="verb", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("patch_path")

    status = subparsers.add_parser("status")
    status.add_argument("--worker-id")

    spawn = subparsers.add_parser("spawn")
    spawn.add_argument("--key", required=True)
    spawn.add_argument("--seat-node", required=True)
    spawn.add_argument("--instruction", required=True)

    for verb in ("pause", "resume", "stop"):
        control = subparsers.add_parser(verb)
        control.add_argument("--key", required=True)
        control.add_argument("worker_id")

    message = subparsers.add_parser("message")
    message.add_argument("--key", required=True)
    message.add_argument("--recipient")
    message.add_argument("body")

    watch = subparsers.add_parser("watch-graph")
    watch.add_argument("--key", required=True)
    watch.add_argument("--condition-json", required=True)
    watch.add_argument("--reason", required=True)

    finish = subparsers.add_parser("finish")
    finish.add_argument("--key", required=True)
    return parser


def _nonblank(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ClientInputError(f"{label} must not be blank")
    return value.strip()


def _request_arguments(namespace, workspace):
    verb = namespace.verb.replace("-", "_")
    if verb == "validate":
        patch_path = _regular_workspace_file(workspace, namespace.patch_path, "patch.json")
        if os.path.basename(patch_path) != "patch.json":
            raise ClientInputError("validation accepts only this run workspace's patch.json")
        try:
            with open(patch_path, "rb") as stream:
                if os.fstat(stream.fileno()).st_size > COMMAND_MAILBOX_MAX_REQUEST_BYTES:
                    raise ClientInputError(
                        "patch.json exceeds the "
                        f"{COMMAND_MAILBOX_MAX_REQUEST_BYTES}-byte command request limit"
                    )
                content = stream.read(COMMAND_MAILBOX_MAX_REQUEST_BYTES + 1)
            if len(content) > COMMAND_MAILBOX_MAX_REQUEST_BYTES:
                raise ClientInputError(
                    "patch.json exceeds the "
                    f"{COMMAND_MAILBOX_MAX_REQUEST_BYTES}-byte command request limit"
                )
            return verb, None, {"patch": content.decode("utf-8")}
        except (OSError, UnicodeError) as exc:
            raise ClientInputError(f"patch.json could not be read: {exc}") from exc
    if verb == "status":
        worker_id = namespace.worker_id
        if worker_id is not None:
            worker_id = _nonblank(worker_id, "worker id")
            if len(worker_id) > 200:
                raise ClientInputError("worker id must be at most 200 characters")
        return verb, None, {"worker_id": worker_id}
    key = _nonblank(namespace.key, "idempotency key")
    if verb == "spawn":
        arguments = {
            "seat_node_id": _nonblank(namespace.seat_node, "seat node"),
            "instruction": _nonblank(namespace.instruction, "instruction"),
        }
    elif verb in ("pause", "resume", "stop"):
        arguments = {"worker_id": _nonblank(namespace.worker_id, "worker id")}
    elif verb == "message":
        recipient = namespace.recipient
        if recipient is not None:
            recipient = _nonblank(recipient, "recipient")
        arguments = {
            "recipient_task_id": recipient,
            "body": _nonblank(namespace.body, "message body"),
        }
    elif verb == "watch_graph":
        try:
            condition = json.loads(namespace.condition_json)
        except ValueError as exc:
            raise ClientInputError(f"graph condition is not valid JSON: {exc}") from exc
        if not isinstance(condition, dict):
            raise ClientInputError("graph condition must be one JSON object")
        arguments = {
            "condition": condition,
            "reason": _nonblank(namespace.reason, "watch reason"),
        }
    elif verb == "finish":
        arguments = {}
    else:
        raise ClientInputError("unsupported command verb")
    if verb not in _MUTATING:
        raise ClientInputError("unsupported mutating command verb")
    return verb, key, arguments


def _run(namespace):
    if not math.isfinite(namespace.timeout) or namespace.timeout <= 0:
        raise ClientInputError("timeout must be a positive finite number")
    workspace = os.path.abspath(namespace.workspace)
    if os.path.islink(workspace) or not os.path.isdir(workspace):
        raise ClientInputError("run workspace is unavailable")
    if namespace.broker is not None:
        broker = os.path.abspath(namespace.broker)
        if not broker.startswith("/tmp/rcp-command-") or not broker.endswith(".sock"):
            raise ClientInputError("broker path is outside the bounded temporary namespace")
        mailbox_id = namespace.mailbox_id
        if not isinstance(mailbox_id, str) or not _MAILBOX_ID.fullmatch(mailbox_id):
            raise ClientInputError("broker mailbox id is malformed")
        token = None
    else:
        if namespace.mailbox_id is not None:
            raise ClientInputError("mailbox id is supplied by the credential")
        mailbox_id, token = _credential(workspace, namespace.credential)
    verb, key, arguments = _request_arguments(namespace, workspace)
    request_id = uuid.uuid4().hex
    prefix = f"rcp-command-{mailbox_id}-{request_id}"
    request = {
        "version": VERSION,
        "mailbox_id": mailbox_id,
        "request_id": request_id,
        "credential": token or ("0" * 64),
        "verb": verb,
        "idempotency_key": key,
        "arguments": arguments,
    }
    if namespace.broker is not None:
        return _run_brokered(namespace, broker, request, request_id)

    request_path = os.path.join(workspace, prefix + ".request.json")
    response_path = os.path.join(workspace, prefix + ".response.json")
    try:
        _atomic_json(request_path, request)
    except OSError as exc:
        print(f"RCP command request could not be written: {exc}")
        return UNAVAILABLE

    deadline = time.monotonic() + namespace.timeout
    while time.monotonic() < deadline:
        try:
            with open(response_path, encoding="utf-8") as stream:
                response = json.load(stream)
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"RCP command response could not be read: {exc}")
            return UNAVAILABLE
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            print("RCP command returned a malformed or mismatched response.")
            return UNAVAILABLE
        status = response.get("status")
        displayed = _display_response(response, namespace.verb)
        print(json.dumps(displayed, ensure_ascii=False, indent=2, sort_keys=True))
        if status == "ok":
            return OK
        if status == "invalid":
            return INVALID
        return UNAVAILABLE
    print("RCP command did not answer before the timeout.")
    return UNAVAILABLE


def _run_brokered(namespace, broker, request, request_id):
    connection = None
    content = bytearray()
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(namespace.timeout)
        connection.connect(broker)
        connection.sendall(
            json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        while len(content) <= 64 * 1024:
            chunk = connection.recv(65536)
            if not chunk:
                break
            content.extend(chunk)
            if content.endswith(b"\n"):
                break
    except (OSError, TimeoutError) as exc:
        print(f"RCP command broker is unavailable: {exc}")
        return UNAVAILABLE
    finally:
        if connection is not None:
            connection.close()
    try:
        response = json.loads(content)
    except (UnicodeError, ValueError) as exc:
        print(f"RCP command broker returned invalid JSON: {exc}")
        return UNAVAILABLE
    if not isinstance(response, dict) or response.get("request_id") != request_id:
        print("RCP command returned a malformed or mismatched response.")
        return UNAVAILABLE
    status = response.get("status")
    displayed = _display_response(response, namespace.verb)
    print(json.dumps(displayed, ensure_ascii=False, indent=2, sort_keys=True))
    if status == "ok":
        return OK
    if status == "invalid":
        return INVALID
    return UNAVAILABLE


def _display_response(response, verb):
    """Keep the established validator stdout shape over the generic envelope."""

    if verb != "validate":
        return response
    result = response.get("result")
    if isinstance(result, dict) and result.get("status") in (
        "valid",
        "invalid",
        "unavailable",
    ):
        return result
    status = response.get("status")
    validation_status = "valid" if status == "ok" else status
    message = response.get("message")
    messages = [message] if isinstance(message, str) and message else []
    return {"status": validation_status, "messages": messages}


def main(argv=None):
    try:
        namespace = _parser().parse_args(argv)
        return _run(namespace)
    except ClientInputError as exc:
        print(f"RCP command is invalid: {exc}")
        return INVALID


if __name__ == "__main__":
    raise SystemExit(main())
