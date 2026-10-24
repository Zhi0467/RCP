"""Stdlib-only client staged into an agent run workspace.

This file is deliberately self-contained. RCP injects the polling limit and
ships its source to a local or SSH stage, where no RCP installation is assumed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import pwd
import re
import socket
import sys
import tempfile
import time
import uuid
from contextlib import redirect_stdout

COMMAND_ASK_POLL_SECONDS = None  # Injected from limits.py when staged.

VERSION = 1
OK = 0
INVALID = 1
UNAVAILABLE = 2
COMMAND_MAILBOX_MAX_REQUEST_BYTES = 16 * 1024 * 1024
# Read the trusted broker's single response through EOF. Guarded Finish hydrates
# its complete durable blocker snapshot outside the compact event ledger. RCP
# bounds the encoded envelope before retaining it; the socket timeout bounds a
# peer that stalls before closing the newline-framed response.
_MAILBOX_ID = re.compile(r"^[a-f0-9]{32}$")
_TOKEN = re.compile(r"^[a-f0-9]{64}$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9._-]+$")
_MUTATING = frozenset(
    (
        "ask",
        "lesson",
        "bulletin",
        "name",
        "flag",
        "apply",
        "spawn",
        "merge",
        "branch",
        "pause",
        "resume",
        "stop",
        "message",
        "watch_graph",
        "episode",
        "inbox",
        "finish",
        "launch",
    )
)


class ClientInputError(Exception):
    pass


_PROFILE_PICK_FLAGS = (("provider", "--provider"), ("model", "--model"), ("reasoning", "--effort"))
# Parser enforcement and root prompt invocations share these option records.
SPAWN_SEAT_OPTIONS = (("seat_node", "--seat-node", "<node-id>"), ("project", "--project", None))
CHILD_TARGET_OPTIONS = (("at", "--at", "<ref@R>"), ("target", "--target", "<branch>"))
CHILD_SEED_OPTION = ("seed_file", "--seed-file", "<filename>")
KICKOFF_NODE = ("node", "--node", "<node-id>")
KICKOFF_GOAL = ("goal_file", "--goal-file", "<filename>")
KICKOFF_LIMIT = ("invocation_limit", "--invocation-limit", "<positive-int>")
# Parser enforcement and every usage renderer share these option records.
KEY_OPTION = ("key", "--key", "<key>")
MERGE_SOURCE = ("branch_id", "<branch-id>")
BRANCH_ARCHIVE = ("archive", "--archive", "<branch-id>")
BULLETIN_READ = ("read", "--read", None)
BULLETIN_ARTIFACT = ("artifact", "--artifact", "<artifact-id>")
BULLETIN_LINK = ("link", "--link", "<artifact-file>")
BULLETIN_CHANNEL = ("channel", "--channel", "<channel>")
BULLETIN_POST = ("post", "--post", None)
BULLETIN_EDIT = ("edit", "--edit", "<post-id>")
BULLETIN_DELETE = ("delete", "--delete", "<post-id>")
BULLETIN_REPLY_TO = ("reply_to", "--reply-to", "<post-id>")
BULLETIN_CURSOR = ("cursor", "--cursor", "<cursor>")
BULLETIN_BODY = ("body_file", "<body-file>")
NAME_VALUE = ("name", "<name>")


def _option_usage(option):
    _, flag, value = option
    return f"{flag} {value}" if value else flag


def merge_usage():
    return f"merge {_option_usage(KEY_OPTION)} {MERGE_SOURCE[1]}"


def branch_usage():
    return f"branch {_option_usage(BRANCH_ARCHIVE)}"


def name_usage():
    return f"name {NAME_VALUE[1]}"


def bulletin_usages(*, turn_artifacts: bool):
    """`turn_artifacts` launches write files that become artifacts, so --link names them."""
    key, body = _option_usage(KEY_OPTION), BULLETIN_BODY[1]
    return (
        f"bulletin {_option_usage(BULLETIN_READ)} [{_option_usage(BULLETIN_CURSOR)}] "
        f"[{_option_usage(BULLETIN_CHANNEL)}]",
        f"bulletin {_option_usage(BULLETIN_POST)} {key} "
        f"[{_option_usage(BULLETIN_REPLY_TO)} | {_option_usage(BULLETIN_CHANNEL)}] {body}",
        f"bulletin {_option_usage(BULLETIN_EDIT)} {key} {body}",
        f"bulletin {_option_usage(BULLETIN_DELETE)} {key}",
        f"bulletin {_option_usage(BULLETIN_ARTIFACT)}",
        *((f"bulletin {_option_usage(BULLETIN_LINK)}",) if turn_artifacts else ()),
    )


def child_target_usage():
    alternatives = " | ".join(f"{flag} {value}" for _, flag, value in CHILD_TARGET_OPTIONS)
    _, seed_flag, seed_value = CHILD_SEED_OPTION
    return f"[{alternatives}] [{seed_flag} {seed_value} (requires --at)]"


def experiment_kickoff_usage(*, goal_required, child_targets):
    """The ``--kick-off-experiment`` arguments, as the orchestrator or a Work turn may use them."""

    _, node_flag, node_value = KICKOFF_NODE
    _, goal_flag, goal_value = KICKOFF_GOAL
    _, limit_flag, limit_value = KICKOFF_LIMIT
    goal = f"{goal_flag} {goal_value}"
    parts = [
        "--kick-off-experiment",
        f"{node_flag} {node_value}",
        *([child_target_usage()] if child_targets else []),
        goal if goal_required else f"[{goal}]",
        f"[{limit_flag} {limit_value}]",
        *(f"[{flag} <{flag[2:]}>]" for _, flag in _PROFILE_PICK_FLAGS),
    ]
    return " ".join(parts)


def spawn_seat_usage():
    return (
        "("
        + " | ".join(f"{flag} {value}" if value else flag for _, flag, value in SPAWN_SEAT_OPTIONS)
        + ")"
    )


def _add_child_target_arguments(parser):
    target = parser.add_mutually_exclusive_group()
    for dest, flag, value in CHILD_TARGET_OPTIONS:
        target.add_argument(flag, dest=dest, metavar=value)
    dest, flag, value = CHILD_SEED_OPTION
    parser.add_argument(flag, dest=dest, metavar=value)


def _child_target_arguments(namespace, workspace):
    arguments = {
        dest: _nonblank(getattr(namespace, dest), flag)
        for dest, flag, _ in CHILD_TARGET_OPTIONS
        if getattr(namespace, dest) is not None
    }
    if namespace.seed_file is not None:
        if namespace.at is None:
            raise ClientInputError("--seed-file requires --at")
        arguments["seed_file"] = _keyed_command_file(workspace, namespace.seed_file, "seed file")
    return arguments


def _add_profile_pick_arguments(parser):
    for dest, flag in _PROFILE_PICK_FLAGS:
        parser.add_argument(flag, dest=dest)


def _profile_pick(namespace):
    """The child provider profile fields the orchestrator named, and no others."""

    return {
        dest: _nonblank(getattr(namespace, dest), flag)
        for dest, flag in _PROFILE_PICK_FLAGS
        if getattr(namespace, dest) is not None
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ClientInputError(message)


def _encoded_request(value):
    content = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(content) > COMMAND_MAILBOX_MAX_REQUEST_BYTES:
        raise ClientInputError(
            "serialized command request exceeds the "
            f"{COMMAND_MAILBOX_MAX_REQUEST_BYTES}-byte command request limit"
        )
    return content


def _atomic_request(path, content):
    directory = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".rcp-command-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _workspace_file_path(workspace, path, label, required_name=None):
    absolute = os.path.abspath(path)
    if os.path.dirname(absolute) != workspace:
        raise ClientInputError(f"{label} must be a direct file in this run workspace")
    name = os.path.basename(absolute)
    if not _SAFE_FILE.match(name):
        raise ClientInputError(f"{label} has an unsafe file name")
    if required_name is not None and name != required_name:
        raise ClientInputError(f"this command accepts only this run workspace's {required_name}")
    return absolute


def _regular_workspace_file(workspace, path, label, required_name=None):
    absolute = _workspace_file_path(workspace, path, label, required_name)
    if os.path.islink(absolute) or not os.path.isfile(absolute):
        raise ClientInputError(f"{label} is unavailable or not a regular file")
    return absolute


def _keyed_command_file(workspace, path, label, required_name=None):
    """Name one keyed command file; RCP, not this client, reads its contents.

    RCP snapshots the file under its key on first admission and may consume it
    (Apply does). A retry of the same key must reach RCP whatever the path now
    holds, because RCP answers a known key from its record.
    """

    return os.path.basename(_workspace_file_path(workspace, path, label, required_name))


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
    parser.add_argument("--ask-wait", type=float)
    parser.add_argument("--workspace", required=True)
    subparsers = parser.add_subparsers(dest="verb", required=True)

    graph_diff = subparsers.add_parser("graph_diff", aliases=["graph-diff"])
    graph_diff.add_argument("from_head", nargs="?")
    graph_diff.add_argument("to_head", nargs="?")
    graph_diff.add_argument("ids", nargs="*")
    graph_diff.add_argument("--branch-changes")
    graph_diff.add_argument("--ids", nargs="+", dest="filter_ids", default=[])
    graph_diff.add_argument("--cursor")
    graph_diff.add_argument("--project")

    graph_log = subparsers.add_parser("graph_log", aliases=["graph-log"])
    graph_log.add_argument("id")
    graph_log.add_argument("--at", default="@head")
    graph_log.add_argument("--cursor")
    graph_log.add_argument("--project")

    flag = subparsers.add_parser("flag")
    flag.add_argument("--key", required=True)
    flag.add_argument("--evidence", required=True)
    flag.add_argument(
        "--kind", required=True, choices=("overclaim", "underclaim", "no_source", "unverified")
    )
    flag.add_argument("--finding", required=True)
    flag.add_argument("--source")

    name = subparsers.add_parser("name")
    name.add_argument(NAME_VALUE[0], metavar=NAME_VALUE[1])

    bulletin = subparsers.add_parser("bulletin")
    bulletin_actions = bulletin.add_mutually_exclusive_group(required=True)
    for dest, option, value in (
        BULLETIN_POST,
        BULLETIN_READ,
        BULLETIN_EDIT,
        BULLETIN_DELETE,
        BULLETIN_ARTIFACT,
        BULLETIN_LINK,
    ):
        if value is None:
            bulletin_actions.add_argument(option, dest=dest, action="store_true")
        else:
            bulletin_actions.add_argument(option, dest=dest, metavar=value)
    for dest, option, value in (KEY_OPTION, BULLETIN_REPLY_TO, BULLETIN_CURSOR, BULLETIN_CHANNEL):
        bulletin.add_argument(option, dest=dest, metavar=value)
    bulletin.add_argument(BULLETIN_BODY[0], nargs="?", metavar=BULLETIN_BODY[1])

    lesson = subparsers.add_parser("lesson")
    lesson_actions = lesson.add_subparsers(dest="lesson_action", required=True)
    for action in ("add", "list", "update", "delete"):
        command = lesson_actions.add_parser(action)
        if action == "list":
            command.add_argument("--cursor")
        else:
            command.add_argument("--key", required=True)
        if action in ("add", "update"):
            command.add_argument("--text", required=True)
        if action in ("update", "delete"):
            command.add_argument("--id", required=True)

    ask = subparsers.add_parser("ask")
    ask.add_argument("--key", required=True)
    ask.add_argument("--question", required=True)
    ask.add_argument("--choice", action="append", default=[])
    ask.add_argument("--multiple", action="store_true")

    validate = subparsers.add_parser("validate")
    validate.add_argument("patch_path")

    merge_answers = subparsers.add_parser("merge-answers")
    answer_commands = merge_answers.add_subparsers(dest="action", required=True)
    check_answers = answer_commands.add_parser("check")
    check_answers.add_argument("answers_path")

    apply = subparsers.add_parser("apply")
    apply.add_argument("--key", required=True)
    apply.add_argument("patch_path")

    status = subparsers.add_parser("status")
    status_target = status.add_mutually_exclusive_group()
    status_target.add_argument("--worker-id")
    status_target.add_argument("--episode-id")

    merge = subparsers.add_parser("merge")
    merge.add_argument(KEY_OPTION[1], dest=KEY_OPTION[0], required=True, metavar=KEY_OPTION[2])
    merge.add_argument(MERGE_SOURCE[0], metavar=MERGE_SOURCE[1])
    branch = subparsers.add_parser("branch")
    # Archive is idempotent; an omitted key gets a fresh one.
    branch.add_argument(KEY_OPTION[1], dest=KEY_OPTION[0], metavar=KEY_OPTION[2])
    dest, option, value = BRANCH_ARCHIVE
    branch.add_argument(option, dest=dest, required=True, metavar=value)

    spawn = subparsers.add_parser("spawn")
    spawn.add_argument("--key", required=True)
    seat = spawn.add_mutually_exclusive_group(required=True)
    for dest, flag, value in SPAWN_SEAT_OPTIONS:
        if value is None:
            seat.add_argument(flag, dest=dest, action="store_true")
        else:
            seat.add_argument(flag, dest=dest, metavar=value)
    _add_child_target_arguments(spawn)
    spawn.add_argument("--instruction-file", required=True)
    _add_profile_pick_arguments(spawn)

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

    episode = subparsers.add_parser("episode")
    episode.add_argument("--key", required=True)
    episode_action = episode.add_mutually_exclusive_group(required=True)
    episode_action.add_argument("--kick-off-experiment", action="store_true")
    episode_action.add_argument("--stop", metavar="EPISODE_ID")
    episode_action.add_argument("--resume", metavar="EPISODE_ID")
    episode.add_argument(KICKOFF_NODE[1])
    episode.add_argument(KICKOFF_GOAL[1])
    episode.add_argument(KICKOFF_LIMIT[1], type=int)
    _add_profile_pick_arguments(episode)
    _add_child_target_arguments(episode)

    inbox = subparsers.add_parser("inbox")
    inbox.add_argument("--key", required=True)
    inbox_action = inbox.add_mutually_exclusive_group(required=True)
    inbox_action.add_argument("--harvest", action="store_true")
    inbox_action.add_argument("--clear", action="store_true")

    launch = subparsers.add_parser("launch")
    launch.add_argument("--key", required=True)
    launch.add_argument("--cwd", required=True)
    launch.add_argument("--label")
    launch.add_argument("argv", nargs=argparse.REMAINDER)

    finish = subparsers.add_parser("finish")
    finish.add_argument("--key", required=True)
    return parser


def _nonblank(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ClientInputError(f"{label} must not be blank")
    return value.strip()


def _request_arguments(namespace, workspace):
    verb = namespace.verb.replace("-", "_")
    if verb in ("validate", "merge_answers"):
        is_patch = verb == "validate"
        label = "patch.json" if is_patch else "answers file"
        path = _regular_workspace_file(
            workspace,
            namespace.patch_path if is_patch else namespace.answers_path,
            label,
            required_name="patch.json" if is_patch else None,
        )
        try:
            with open(path, "rb") as stream:
                if os.fstat(stream.fileno()).st_size > COMMAND_MAILBOX_MAX_REQUEST_BYTES:
                    raise ClientInputError(
                        f"{label} exceeds the "
                        f"{COMMAND_MAILBOX_MAX_REQUEST_BYTES}-byte command request limit"
                    )
                content = stream.read(COMMAND_MAILBOX_MAX_REQUEST_BYTES + 1)
            if len(content) > COMMAND_MAILBOX_MAX_REQUEST_BYTES:
                raise ClientInputError(
                    f"{label} exceeds the "
                    f"{COMMAND_MAILBOX_MAX_REQUEST_BYTES}-byte command request limit"
                )
            arguments = {"patch" if is_patch else "answers": content.decode("utf-8")}
            if not is_patch:
                arguments["action"] = namespace.action
            return verb, None, arguments
        except (OSError, UnicodeError) as exc:
            raise ClientInputError(f"{label} could not be read: {exc}") from exc
    if verb == "name":
        return verb, None, {"name": namespace.name}
    if verb == "status":
        worker_id = namespace.worker_id
        if worker_id is not None:
            worker_id = _nonblank(worker_id, "worker id")
            if len(worker_id) > 200:
                raise ClientInputError("worker id must be at most 200 characters")
        episode_id = namespace.episode_id
        if episode_id is not None:
            episode_id = _nonblank(episode_id, "episode id")
            if len(episode_id) > 200:
                raise ClientInputError("episode id must be at most 200 characters")
        return verb, None, {"worker_id": worker_id, "episode_id": episode_id}
    if verb in ("graph_diff", "graph_log"):
        arguments = {}
        if namespace.project is not None:
            arguments["project"] = _nonblank(namespace.project, "project id")
        if namespace.cursor is not None:
            arguments["cursor"] = _nonblank(namespace.cursor, "cursor")
        if verb == "graph_log":
            arguments.update(id=_nonblank(namespace.id, "entity id"), at=namespace.at)
        elif namespace.branch_changes is not None:
            if namespace.from_head is not None or namespace.to_head is not None:
                raise ClientInputError("--branch-changes cannot be combined with heads")
            arguments["branch_changes"] = namespace.branch_changes
            arguments["ids"] = namespace.filter_ids
        else:
            if namespace.from_head is None or namespace.to_head is None:
                raise ClientInputError("graph_diff requires two heads or --branch-changes")
            arguments.update({"from": namespace.from_head, "to": namespace.to_head})
            arguments["ids"] = namespace.ids + namespace.filter_ids
        return verb, None, arguments
    if verb == "bulletin":
        if namespace.channel is not None and not (
            namespace.read or (namespace.post and not namespace.reply_to)
        ):
            raise ClientInputError("Only Bulletin reads and new posts accept a channel")
        if namespace.link is not None:
            if any(
                value is not None
                for value in (
                    namespace.key,
                    namespace.reply_to,
                    namespace.body_file,
                    namespace.cursor,
                )
            ):
                raise ClientInputError("Artifact links accept only an artifact file name")
            return verb, None, {"action": "link", "file_name": namespace.link}
        if namespace.artifact is not None:
            if any(
                value is not None
                for value in (
                    namespace.key,
                    namespace.reply_to,
                    namespace.body_file,
                    namespace.cursor,
                )
            ):
                raise ClientInputError("Artifact reads accept only an artifact id")
            return verb, None, {"action": "artifact", "artifact_id": namespace.artifact}
        if namespace.read:
            if namespace.key or namespace.reply_to or namespace.body_file:
                raise ClientInputError("Bulletin reads accept only a cursor and channel")
            return (
                verb,
                None,
                {
                    "action": "read",
                    "cursor": namespace.cursor,
                    **(
                        {"channel": _nonblank(namespace.channel, "channel")}
                        if namespace.channel is not None
                        else {}
                    ),
                },
            )
        if namespace.delete:
            if namespace.cursor or namespace.body_file or namespace.reply_to:
                raise ClientInputError("Bulletin deletes accept only a post id and key")
            return (
                verb,
                _nonblank(namespace.key, "idempotency key"),
                {
                    "action": "delete",
                    "post_id": namespace.delete,
                },
            )
        if namespace.edit and namespace.reply_to:
            raise ClientInputError("Bulletin edits accept no reply target")
        if namespace.cursor or not namespace.body_file:
            raise ClientInputError("Bulletin posts require a body file and accept no cursor")
        path = _regular_workspace_file(workspace, namespace.body_file, "Bulletin body")
        try:
            with open(path, encoding="utf-8") as stream:
                text = stream.read(COMMAND_MAILBOX_MAX_REQUEST_BYTES + 1)
        except (OSError, UnicodeError) as exc:
            raise ClientInputError(f"Bulletin body could not be read: {exc}") from exc
        return (
            verb,
            _nonblank(namespace.key, "idempotency key"),
            {
                "action": "edit" if namespace.edit else "post",
                "text": _nonblank(text, "Bulletin body"),
                **(
                    {"channel": _nonblank(namespace.channel, "channel")}
                    if namespace.channel is not None
                    else {}
                ),
                **(
                    {"post_id": namespace.edit}
                    if namespace.edit
                    else {"reply_to": namespace.reply_to}
                ),
            },
        )
    if verb == "lesson":
        action = namespace.lesson_action
        arguments = {"action": action}
        if action == "list":
            if namespace.cursor is not None:
                arguments["cursor"] = _nonblank(namespace.cursor, "cursor")
            return verb, None, arguments
        if action in ("add", "update"):
            arguments["text"] = _nonblank(namespace.text, "lesson text")
        if action in ("update", "delete"):
            arguments["lesson_id"] = _nonblank(namespace.id, "lesson id")
        return verb, _nonblank(namespace.key, "idempotency key"), arguments
    if verb == "branch":
        return (
            verb,
            _nonblank(namespace.key, "idempotency key")
            if namespace.key is not None
            else uuid.uuid4().hex,
            {
                "action": "archive",
                "branch_id": _nonblank(namespace.archive, "branch id"),
            },
        )
    key = _nonblank(namespace.key, "idempotency key")
    if verb == "flag":
        arguments = {
            "evidence_id": _nonblank(namespace.evidence, "evidence id"),
            "kind": namespace.kind,
            "finding": _nonblank(namespace.finding, "finding"),
            "source": namespace.source,
        }
    elif verb == "ask":
        if namespace.multiple and not namespace.choice:
            raise ClientInputError("--multiple requires --choice")
        arguments = {
            "question": _nonblank(namespace.question, "question"),
            "choices": [_nonblank(choice, "choice") for choice in namespace.choice],
            "multiple": namespace.multiple,
        }
    elif verb == "apply":
        patch_file = _keyed_command_file(
            workspace, namespace.patch_path, "patch.json", required_name="patch.json"
        )
        arguments = {"patch_file": patch_file}
    elif verb == "merge":
        arguments = {"branch_id": _nonblank(namespace.branch_id, "branch id")}
    elif verb == "spawn":
        instruction_file = _keyed_command_file(
            workspace, namespace.instruction_file, "instruction file"
        )
        arguments = {
            **(
                {"project": True}
                if namespace.project
                else {"seat_node_id": _nonblank(namespace.seat_node, "seat node")}
            ),
            **_child_target_arguments(namespace, workspace),
            "instruction_file": instruction_file,
            **_profile_pick(namespace),
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
    elif verb == "episode":
        if namespace.kick_off_experiment:
            if namespace.stop is not None or namespace.resume is not None:
                raise ClientInputError("episode action is ambiguous")
            node_id = namespace.node
            if node_id is None:
                raise ClientInputError("Experiment kickoff requires --node")
            invocation_limit = namespace.invocation_limit
            if invocation_limit is not None and invocation_limit <= 0:
                raise ClientInputError("invocation limit must be a positive integer")
            goal_file = namespace.goal_file
            if goal_file is not None:
                goal_file = _keyed_command_file(workspace, goal_file, "goal file")
            arguments = {
                "action": "kick_off_experiment",
                **_child_target_arguments(namespace, workspace),
                "node_id": _nonblank(node_id, "Experiment node"),
                "goal_file": goal_file,
                "invocation_limit": invocation_limit,
                **_profile_pick(namespace),
            }
        else:
            if namespace.node is not None:
                raise ClientInputError("--node is available only for Experiment kickoff")
            if namespace.goal_file is not None:
                raise ClientInputError("--goal-file is available only for Experiment kickoff")
            if namespace.invocation_limit is not None:
                raise ClientInputError(
                    "--invocation-limit is available only for Experiment kickoff"
                )
            for dest, flag, _ in (*CHILD_TARGET_OPTIONS, CHILD_SEED_OPTION):
                if getattr(namespace, dest) is not None:
                    raise ClientInputError(f"{flag} is available only for Experiment kickoff")
            for dest, flag in _PROFILE_PICK_FLAGS:
                if getattr(namespace, dest) is not None:
                    raise ClientInputError(
                        f"{flag} is available only for spawn and Experiment kickoff"
                    )
            if namespace.stop is not None:
                action = "stop"
                episode_id = namespace.stop
            elif namespace.resume is not None:
                action = "resume"
                episode_id = namespace.resume
            else:
                raise ClientInputError("episode action is required")
            arguments = {
                "action": action,
                "episode_id": _nonblank(episode_id, "episode id"),
            }
    elif verb == "inbox":
        if namespace.harvest:
            action = "harvest"
        elif namespace.clear:
            action = "clear"
        else:
            raise ClientInputError("inbox action is required")
        arguments = {"action": action}
    elif verb == "launch":
        if not os.path.isabs(namespace.cwd) or "\x00" in namespace.cwd:
            raise ClientInputError("compute cwd must be an absolute path without NUL")
        argv = namespace.argv
        if not argv or argv[0] != "--":
            raise ClientInputError("launch requires -- before its argv")
        argv = argv[1:]
        if not argv or not argv[0] or any("\x00" in argument for argument in argv):
            raise ClientInputError("launch requires an executable and argv without NUL")
        label = namespace.label
        if label is not None:
            label = _nonblank(label, "compute label")
        arguments = {"cwd": namespace.cwd, "argv": argv, "label": label}
    elif verb == "finish":
        arguments = {}
    else:
        raise ClientInputError("unsupported command verb")
    if verb not in _MUTATING:
        raise ClientInputError("unsupported mutating command verb")
    return verb, key, arguments


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _client_failure(verb, status, message, delivery=None):
    """Print one client-side outcome.

    ``delivery`` tells an agent what an `unavailable` command did: `not_sent`
    never reached RCP; `unknown` reached it or may have, so RCP can still act
    and the exact same command (same key) returns that outcome.
    """

    if verb == "validate":
        _print_json({"status": status, "messages": [message]})
    else:
        result = {"delivery": delivery} if delivery is not None else {}
        _print_json({"status": status, "message": message, "result": result})
    return INVALID if status == "invalid" else UNAVAILABLE


def _not_answered(verb, timeout):
    repeat = (
        "Run the check again."
        if verb in ("validate", "merge_answers", "merge-answers")
        else "Repeat the exact same command, with the same key, to get its result."
    )
    return _client_failure(
        verb,
        "unavailable",
        f"RCP has not answered within {timeout:g} seconds and may still be working on it. {repeat}",
        "unknown",
    )


def _response_exit_code(response):
    status = response.get("status")
    if status == "ok":
        return OK
    if status == "invalid":
        return INVALID
    if status == "unavailable":
        return UNAVAILABLE
    return None


def _handle_response(response, verb, request_id):
    if not isinstance(response, dict) or response.get("request_id") != request_id:
        return _client_failure(
            verb,
            "unavailable",
            "RCP command returned a malformed or mismatched response.",
            "unknown",
        )
    exit_code = _response_exit_code(response)
    if exit_code is None:
        return _client_failure(
            verb,
            "unavailable",
            "RCP command returned an unsupported status.",
            "unknown",
        )
    if (
        verb == "ask"
        and response.get("status") == "ok"
        and isinstance(response.get("result"), dict)
        and response["result"].get("state") in {"pending", "answered"}
    ):
        return response
    _print_json(_display_response(response, verb))
    return exit_code


def _broker_socket_path(path):
    """Resolve the broker's `~/.rcp/sockets/rcp-command-*.sock` as the broker does."""

    prefix = "~/.rcp/sockets/"
    name = path[len(prefix) :] if path.startswith(prefix) else ""
    if not name.startswith("rcp-command-") or not name.endswith(".sock") or "/" in name:
        raise ClientInputError(
            "broker path is outside the RCP socket directory; "
            "copy the supplied command prefix unchanged, including its quotes"
        )
    return os.path.join(_socket_directory(pwd.getpwuid(os.geteuid()).pw_dir), name)


def _socket_directory(home):
    """`~/.rcp/sockets`, or a short `/tmp/rcp-<id>/sockets` when that is too deep.

    Mirrors `rcp.rcp_home.command_socket_directory`; this file ships alone.
    """

    default = os.path.join(home, ".rcp", "sockets")
    if len(os.fsencode(default)) + 1 + len("rcp-command-") + 32 + len(".sock") < 100:
        return default
    root = "/tmp/rcp-" + hashlib.sha256(home.encode("utf-8")).hexdigest()[:12]
    return os.path.join(root, "sockets")


def _closed_response(workspace, mailbox_id, request_id):
    path = os.path.join(workspace, f"rcp-command-{mailbox_id}.closed.json")
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.loads(stream.read(COMMAND_MAILBOX_MAX_REQUEST_BYTES + 1))
    except FileNotFoundError:
        return None
    if (
        not isinstance(value, dict)
        or value.get("version") != VERSION
        or value.get("mailbox_id") != mailbox_id
        or not isinstance(value.get("message"), str)
    ):
        raise ClientInputError("command mailbox closure is malformed")
    return {
        "version": VERSION,
        "request_id": request_id,
        "status": "unavailable",
        "message": value["message"],
        "result": {"permanent": True, "delivery": "not_sent"},
    }


def _run(namespace):
    if not math.isfinite(namespace.timeout) or namespace.timeout <= 0:
        raise ClientInputError("timeout must be a positive finite number")
    workspace = os.path.abspath(namespace.workspace)
    if os.path.islink(workspace) or not os.path.isdir(workspace):
        raise ClientInputError("run workspace is unavailable")
    if namespace.broker is not None:
        broker = _broker_socket_path(namespace.broker)
        mailbox_id = namespace.mailbox_id
        if not isinstance(mailbox_id, str) or not _MAILBOX_ID.fullmatch(mailbox_id):
            raise ClientInputError("broker mailbox id is malformed")
        token = None
    else:
        if namespace.mailbox_id is not None:
            raise ClientInputError("mailbox id is supplied by the credential")
        broker = None
        mailbox_id, token = _credential(workspace, namespace.credential)
    verb, key, arguments = _request_arguments(namespace, workspace)
    wait = (
        namespace.ask_wait
        if verb == "ask" and namespace.ask_wait is not None
        else namespace.timeout
    )
    if not math.isfinite(wait) or wait <= 0:
        raise ClientInputError("ask wait must be a positive finite number")
    namespace.timeout = wait
    deadline = time.monotonic() + wait
    call_id = uuid.uuid4().hex if verb == "ask" else None
    pending = None
    while True:
        captured = io.StringIO()
        # Once a round returned pending, the card exists: a later round's
        # failure only means this check for an answer did not get through.
        with redirect_stdout(captured if pending is not None else sys.stdout):
            response = _run_round(
                namespace,
                workspace,
                mailbox_id,
                token,
                verb,
                key,
                arguments,
                broker,
                deadline,
                call_id=call_id,
            )
        if not isinstance(response, dict):
            if pending is None:
                return response
            printed = json.loads(captured.getvalue())
            result = printed.get("result")
            if not isinstance(result, dict) or "delivery" not in result or result.get("permanent"):
                # RCP's own outcome, such as dismissed, an ended episode, or a
                # closed mailbox that no repeat can reach.
                sys.stdout.write(captured.getvalue())
                return response
            # This round never reached RCP's handler; the card is still there.
            failure = printed.get("message")
            pending["message"] = (
                "The question is on the human's card. This check for an answer failed: "
                f"{failure} Repeat the exact same command, with the same key, in the "
                "foreground to keep waiting; never ask it again under a new key."
            )
            _print_json(_display_response(pending, verb))
            return OK
        pending = response
        if response["result"].get("state") == "answered":
            receipt_token = response["result"].get("receipt_token")
            # A second authenticated request proves consumption on either
            # transport, including SSH. Its diagnostics must not replace or
            # duplicate the human answer on stdout. Never extend the deadline.
            if (
                isinstance(receipt_token, str)
                and _TOKEN.fullmatch(receipt_token)
                and time.monotonic() < deadline
            ):
                with redirect_stdout(sys.stderr):
                    try:
                        _run_round(
                            namespace,
                            workspace,
                            mailbox_id,
                            token,
                            verb,
                            key,
                            arguments,
                            broker,
                            deadline,
                            receipt_token=receipt_token,
                            call_id=call_id,
                        )
                    except (ClientInputError, OSError) as exc:
                        _client_failure(
                            verb,
                            "unavailable",
                            f"Question acknowledgement failed: {exc}",
                            "unknown",
                        )
            _print_json(_display_response(response, verb))
            return OK
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(COMMAND_ASK_POLL_SECONDS, remaining))
        if time.monotonic() >= deadline:
            response["message"] = _pending_guidance(response["result"])
            _print_json(_display_response(response, verb))
            return OK


def _pending_guidance(result):
    attempt = result.get("attempt")
    max_attempts = result.get("max_attempts")
    counted = isinstance(attempt, int) and isinstance(max_attempts, int)
    progress = f"Attempt {attempt} of {max_attempts}. " if counted else ""
    stop = (
        "Stop asking and end your turn; the question stays on the human's card "
        "and their answer resumes this conversation."
    )
    if counted and attempt >= max_attempts:
        return progress + stop
    guidance = (
        "The question is still pending. If the answer blocks your work, repeat the exact "
        "same call to keep waiting. If it does not block, carry on with other work and "
        "repeat the call later to check. "
    )
    if isinstance(max_attempts, int):
        guidance += f"After attempt {max_attempts} with no answer: " + stop
    return progress + guidance


def _run_round(
    namespace,
    workspace,
    mailbox_id,
    token,
    verb,
    key,
    arguments,
    broker,
    deadline,
    *,
    receipt_token=None,
    call_id=None,
):
    request_id = uuid.uuid4().hex
    closed = _closed_response(workspace, mailbox_id, request_id)
    if closed is not None:
        return _handle_response(closed, namespace.verb, request_id)
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
    if call_id is not None:
        request["call_id"] = call_id
    if receipt_token is not None:
        request["receipt_token"] = receipt_token
    request_content = _encoded_request(request)
    if namespace.broker is not None:
        return _run_brokered(namespace, broker, request_content, request_id, deadline)

    request_path = os.path.join(workspace, prefix + ".request.json")
    response_path = os.path.join(workspace, prefix + ".response.json")
    try:
        _atomic_request(request_path, request_content)
    except OSError as exc:
        return _client_failure(
            namespace.verb,
            "unavailable",
            f"RCP command request could not be written: {exc}",
            "not_sent",
        )

    while time.monotonic() < deadline:
        try:
            with open(response_path, encoding="utf-8") as stream:
                response = json.load(stream)
        except FileNotFoundError:
            closed = _closed_response(workspace, mailbox_id, request_id)
            if closed is not None:
                closed["result"]["delivery"] = "unknown"
                return _handle_response(closed, namespace.verb, request_id)
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            return _client_failure(
                namespace.verb,
                "unavailable",
                f"RCP command response could not be read: {exc}",
                "unknown",
            )
        return _handle_response(response, namespace.verb, request_id)
    return _not_answered(namespace.verb, namespace.timeout)


def _remaining(deadline):
    """Seconds left for the next blocking step of one invocation."""

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("invocation deadline reached")
    return remaining


def _run_brokered(namespace, broker, request_content, request_id, deadline=None):
    # One deadline for the whole invocation, not one allowance per socket step.
    if deadline is None:
        deadline = time.monotonic() + namespace.timeout
    connection = None
    content = bytearray()
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(_remaining(deadline))
        connection.connect(broker)
    except (OSError, TimeoutError) as exc:
        if connection is not None:
            connection.close()
        return _client_failure(
            namespace.verb,
            "unavailable",
            f"RCP command was not sent; its broker is unavailable: {exc}",
            "not_sent",
        )
    try:
        connection.settimeout(_remaining(deadline))
        connection.sendall(request_content)
        while True:
            connection.settimeout(_remaining(deadline))
            chunk = connection.recv(65536)
            if not chunk:
                break
            content.extend(chunk)
    except (TimeoutError, socket.timeout):  # noqa: UP041 (distinct on Python 3.9)
        return _not_answered(namespace.verb, namespace.timeout)
    except OSError as exc:
        return _client_failure(
            namespace.verb,
            "unavailable",
            f"RCP command connection failed after sending; it may still take effect: {exc}",
            "unknown",
        )
    finally:
        connection.close()
    if not content.endswith(b"\n") or b"\n" in content[:-1]:
        return _client_failure(
            namespace.verb,
            "unavailable",
            "RCP command broker did not return one complete newline-delimited response.",
            "unknown",
        )
    try:
        response = json.loads(content[:-1])
    except (UnicodeError, ValueError) as exc:
        return _client_failure(
            namespace.verb,
            "unavailable",
            f"RCP command broker returned invalid JSON: {exc}",
            "unknown",
        )
    return _handle_response(response, namespace.verb, request_id)


def _display_response(response, verb):
    """Keep the established validator stdout shape over the generic envelope.

    A waiting inbox is printed first, so a long reply cannot truncate it away.
    """

    result = response.get("result")
    pending = result.get("inbox_pending") if isinstance(result, dict) else None
    display = _display_body(response, verb)
    if pending is None:
        return display
    return {"status": display.pop("status"), "inbox_pending": pending, **display}


def _display_body(response, verb):
    if verb == "validate":
        result = response.get("result")
        if isinstance(result, dict) and result.get("status") in (
            "valid",
            "invalid",
            "unavailable",
        ):
            messages = result.get("messages")
            if not isinstance(messages, list) or not all(
                isinstance(message, str) for message in messages
            ):
                messages = []
            return {"status": result["status"], "messages": messages}
        status = response.get("status")
        validation_status = "valid" if status == "ok" else status
        message = response.get("message")
        messages = [message] if isinstance(message, str) and message else []
        return {"status": validation_status, "messages": messages}
    message = response.get("message")
    if message is not None and not isinstance(message, str):
        message = "RCP returned a response with an invalid message."
    result = response.get("result")
    if not isinstance(result, dict):
        result = {}
    result = {
        key: value
        for key, value in result.items()
        if key != "inbox_pending" and not (verb == "ask" and key == "receipt_token")
    }
    return {
        "status": response["status"],
        "message": message,
        "result": result,
    }


def _requested_verb(argv):
    for argument in argv:
        if argument in (
            "ask",
            "lesson",
            "bulletin",
            "name",
            "flag",
            "validate",
            "merge-answers",
            "graph_diff",
            "graph_log",
            "graph-diff",
            "graph-log",
            "apply",
            "status",
            "spawn",
            "merge",
            "branch",
            "pause",
            "resume",
            "stop",
            "message",
            "watch-graph",
            "episode",
            "inbox",
            "finish",
            "launch",
        ):
            return argument
    return None


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    verb = _requested_verb(arguments)
    try:
        namespace = _parser().parse_args(arguments)
        return _run(namespace)
    except ClientInputError as exc:
        return _client_failure(verb, "invalid", f"RCP command is invalid: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
