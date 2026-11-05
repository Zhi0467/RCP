"""A card edit forgets what its old route taught, and nothing else."""

from __future__ import annotations

from rcp.machine_cards import BACKEND_MACHINE_ID, LoginKey, host_of, invalidate_machine
from rcp.storage import AppStore
from rcp.storage.models import ProviderReadinessSnapshotRecord


def test_invalidating_a_card_clears_its_caches_and_display_snapshots(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    gpu = store.create_space_machine(name="GPU", host="gpu.example", os_account="alice")
    cpu = store.create_space_machine(name="CPU", host="cpu.example", os_account="alice")
    for card in (gpu, cpu):
        store.save_provider_readiness_snapshot(
            LoginKey("codex", card.machine_id, ""),
            ProviderReadinessSnapshotRecord(
                provider="codex",
                machine_id=card.machine_id,
                binary="/bin/codex",
                version="1",
                readiness_json="{}",
                probed_at=store.now(),
            ),
        )
        with store.connection() as connection:
            connection.execute(
                "INSERT INTO compute_backend_probes VALUES ('project', ?, 'helper', '{}', ?)",
                (card.machine_id, store.now()),
            )
    forgotten: list[str] = []

    invalidate_machine(
        store,
        gpu.machine_id,
        project_ids=["project"],
        forget_display_snapshot=forgotten.append,
    )

    assert forgotten == ["project"]
    assert (
        store.provider_readiness_snapshot(LoginKey("codex", gpu.machine_id, ""), "/bin/codex")
        is None
    )
    assert store.provider_readiness_snapshot(LoginKey("codex", cpu.machine_id, ""), "/bin/codex")
    with store.connection() as connection:
        probes = connection.execute("SELECT machine_id FROM compute_backend_probes").fetchall()
    assert [row[0] for row in probes] == [cpu.machine_id]
    assert host_of(store, gpu.machine_id) == "gpu.example"
    store.close()


def test_the_backend_card_has_its_fixed_id_before_anything_creates_it(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")

    assert host_of(store, BACKEND_MACHINE_ID) == ""
    assert store.create_space_machine(name="GPU", host="gpu.example", os_account="a").machine_id
    assert [card.machine_id for card in store.space_machines() if not card.host] == [
        BACKEND_MACHINE_ID
    ]
    store.close()
