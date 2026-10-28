from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from rcp.paper import engines


def test_verified_install_is_atomic_and_reusable_offline(tmp_path, monkeypatch):
    data = b"pinned browser runtime"
    asset = engines.Asset(
        "runtime.js", "https://example.invalid/runtime", hashlib.sha256(data).hexdigest()
    )
    monkeypatch.setattr(engines, "TYPST_ASSETS", (asset,))
    calls = []

    def download(url: str, path: Path):
        calls.append(url)
        path.write_bytes(data)
        assert not (tmp_path / "paper-engines" / "typst-0.7.0-v1").exists()

    async def run():
        monkeypatch.setattr(engines, "_download", download)
        store = engines.EngineStore(tmp_path)
        assert (store.status("typst"), store.error("typst")) == ("unavailable", "")
        store.start_install("typst")
        assert store.status("typst") == "installing"
        assert await store.ensure("typst")
        assert store.status("typst") == "ready"
        assert store.asset("runtime.js").read_bytes() == data
        monkeypatch.setattr(engines, "_download", lambda *_: pytest.fail("offline"))
        restarted = engines.EngineStore(tmp_path)
        assert await restarted.ensure("typst")
        assert len(calls) == 1
        with pytest.raises(ValueError):
            restarted.asset("../manifest.json")
        restarted.asset("runtime.js").write_bytes(b"tampered")
        assert restarted.status("typst") == "unavailable"
        with pytest.raises(ValueError, match="checksum"):
            restarted.asset("runtime.js")
        await store.close()

    asyncio.run(run())


def test_hash_mismatch_never_publishes_and_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        engines,
        "TYPST_ASSETS",
        (engines.Asset("runtime.js", "https://example.invalid/runtime", "0" * 64),),
    )

    async def run():
        monkeypatch.setattr(engines, "_download", lambda _, path: path.write_bytes(b"wrong"))
        store = engines.EngineStore(tmp_path)
        assert not await store.ensure("typst")
        assert store.status("typst") == "unavailable"
        assert "checksum" in store.error("typst")
        assert not (tmp_path / "paper-engines" / "typst-0.7.0-v1").exists()
        assert not list((tmp_path / "paper-engines").glob(".install-*"))

    asyncio.run(run())
