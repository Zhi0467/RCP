from __future__ import annotations

from types import SimpleNamespace

from rcp.runs.episode_mailbox import (
    deliver_pending_chat_mail,
    reconcile_pending_chat_mail,
)


class MailboxStore:
    def __init__(self):
        self.pending = [SimpleNamespace(message_id="item")]
        self.tasks = {}
        self.admissions = 0

    def pending_episode_mail(self, kind, recipient):
        assert (kind, recipient) == ("chat", "chat")
        return self.pending

    def admit_chat_wake(self, project, chat, cause, ids):
        assert (project, chat, cause, ids) == ("project", "chat", "episode_mail", ["item"])
        self.admissions += 1
        task = SimpleNamespace(operation_id="wake", status="queued")
        self.tasks[task.operation_id] = task
        self.pending = []
        return task

    def pending_chat_wakes(self):
        return [task for task in self.tasks.values() if task.status == "queued"]

    def pending_chat_mail_recipients(self):
        return [("project", "chat")] if self.pending else []


class MailboxBackground:
    def __init__(self, store):
        self.store = store
        self.launched = []
        self.fail = False

    def launch_admitted(self, operation_id):
        if self.fail:
            raise OSError("launch_unavailable")
        task = self.store.tasks[operation_id]
        if task.status == "queued":
            self.launched.append(operation_id)
            task.status = "running"
        return task


def test_reconcile_recovers_claim_before_launch_without_another_admission():
    store = MailboxStore()
    background = MailboxBackground(store)
    background.fail = True
    assert reconcile_pending_chat_mail(background) == {"chat": "launch_unavailable"}
    assert store.pending == []
    restarted = MailboxBackground(store)
    assert reconcile_pending_chat_mail(restarted) == {"wake": "running"}
    assert reconcile_pending_chat_mail(restarted) == {}
    assert restarted.launched == ["wake"]
    assert store.admissions == 1


def test_empty_mailbox_does_not_launch():
    store = MailboxStore()
    store.pending = []
    background = MailboxBackground(store)
    assert deliver_pending_chat_mail(background, "project", "chat") is None
    assert background.launched == []
    assert store.admissions == 0


def test_cooldown_arms_one_later_pass_that_wakes_the_chat():
    from rcp.storage.models import AgentTaskAdmissionConflict

    store = MailboxStore()
    admit = store.admit_chat_wake
    cooling = [True]

    def cooled_admission(*args):
        if cooling[0]:
            raise AgentTaskAdmissionConflict("chat_wake_cooldown")
        return admit(*args)

    store.admit_chat_wake = cooled_admission
    background = MailboxBackground(store)
    armed = []
    background.schedule_deferred_pass = lambda key, delay, dispatch: armed.append(dispatch)
    assert reconcile_pending_chat_mail(background) == {"chat": "chat_wake_cooldown"}
    [later] = armed
    cooling[0] = False
    later()
    assert background.launched == ["wake"]
