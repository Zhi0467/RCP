"""Disposable real-route backend for the Space composer browser journey.

Standard test isolation keeps the host out of reach and provider dispatch is
stubbed. Authentication, uploads, deletion, and turn admission use production
routes and stores.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import uvicorn

from tests.helpers import create_named_app, fake_launch_admitted, isolate_host, signed_in_client


def serve(root: Path) -> None:
    with pytest.MonkeyPatch.context() as patch:
        engines = isolate_host(patch, root)
        app = create_named_app(data_dir=root / "data")
        tasks = app.state.background_tasks
        fake_launch_admitted(patch, tasks)
        client = signed_in_client(app)
        thread = client.get("/api/agent-threads").json()["threads"][0]
        client.put(
            f"/api/agent-threads/{thread['thread_id']}/browser",
            json={"browser_requested": False},
        )
        client.close()
        code = tasks.store.create_owner_sign_in_code()
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                print(
                    json.dumps(
                        {
                            "base": f"http://127.0.0.1:{listener.getsockname()[1]}",
                            "code": code,
                            "thread_id": thread["thread_id"],
                        }
                    ),
                    flush=True,
                )
                # The fixture owns startup and shutdown; background schedules stay off.
                server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
                server.run(sockets=[listener])
        finally:
            for engine in engines:
                engine.shutdown()


if __name__ == "__main__":
    with TemporaryDirectory(prefix="rcp-space-composer-") as directory:
        serve(Path(directory))
