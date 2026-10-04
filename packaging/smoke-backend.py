#!/usr/bin/env python3
"""Exercise the frozen desktop backend without a developer toolchain on PATH."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import smoke_agent

LAUNCH_TIMEOUT_SECONDS = 30.0
HEALTH_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 20.0


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "backend",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent / "dist" / "rcp-backend",
    )
    return parser.parse_args()


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _environment(data_dir: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("RCP_")}
    environment["RCP_DATA_DIR"] = str(data_dir)
    # The release may see ordinary macOS utilities, but none of RCP's build or
    # language toolchains. A hidden npm/Python/uv dependency therefore fails.
    environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    for name in (
        "NODE_PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "UV_PROJECT_ENVIRONMENT",
    ):
        environment.pop(name, None)
    return environment


def _command(backend: Path, port: int) -> list[str]:
    return [
        str(backend),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--reuse-existing",
        "--machine-readable",
        "--owner",
        "desktop",
        "--web-assets",
        "prebuilt",
    ]


def _launch_outcome(process: subprocess.Popen[str]) -> tuple[dict[str, Any], str]:
    """Return the launch JSON and its one-time sign-in code."""
    assert process.stdout is not None
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put("")

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + LAUNCH_TIMEOUT_SECONDS
    while True:
        try:
            line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            raise RuntimeError("Timed out waiting for the backend launch outcome.") from None
        if not line:
            raise RuntimeError(
                f"The backend exited before reporting an outcome (status {process.poll()})."
            )
        try:
            outcome = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("The backend emitted invalid launch JSON.") from exc
        if not isinstance(outcome, dict):
            raise RuntimeError("The backend launch outcome is not an object.")
        code = outcome.pop("owner_sign_in_code", None)
        if not isinstance(code, str) or not code:
            raise RuntimeError("The backend launch outcome omitted its sign-in code.")
        return outcome, code


def _sign_in(base_url: str, code: str) -> None:
    """Redeem the printed code, as the desktop app does, and keep its cookie for later calls."""
    request = urllib.request.Request(
        f"{base_url}/api/owner/redeem",
        data=json.dumps({"code": code}).encode(),
        headers={"Content-Type": "application/json", "Origin": base_url},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5.0) as response:
        cookie = response.headers.get("Set-Cookie", "").split(";", 1)[0]
    if not cookie.startswith("rcp_owner_session="):
        raise RuntimeError("Redeeming the sign-in code returned no owner session.")
    opener = urllib.request.build_opener()
    opener.addheaders = [("Cookie", cookie)]
    urllib.request.install_opener(opener)


def _request(url: str) -> tuple[bytes, str]:
    with urllib.request.urlopen(url, timeout=5.0) as response:
        return response.read(), response.headers.get_content_type()


def _wait_for_health(base_url: str) -> dict[str, Any]:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            body, _ = _request(f"{base_url}/api/health")
            payload = json.loads(body)
            if isinstance(payload, dict) and payload.get("status") == "ok":
                return payload
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"The packaged backend never became healthy: {last_error}")


def _verify_web_bundle(base_url: str) -> None:
    index, content_type = _request(f"{base_url}/")
    if content_type != "text/html" or b'id="root"' not in index:
        raise RuntimeError("The packaged project index is not the built React application.")
    asset_paths = re.findall(rb'(?:src|href)="(/assets/[^\"]+)"', index)
    if not asset_paths:
        raise RuntimeError("The packaged project index does not reference built assets.")
    asset, asset_type = _request(f"{base_url}{asset_paths[0].decode('utf-8')}")
    if not asset or asset_type == "text/html":
        raise RuntimeError("A packaged frontend asset could not be loaded.")


def _stderr(process: subprocess.Popen[str]) -> str:
    if process.stderr is None or process.poll() is None:
        return ""
    return process.stderr.read().strip()


def main() -> None:
    backend = _arguments().backend.expanduser().resolve()
    if not backend.is_file() or not os.access(backend, os.X_OK):
        raise SystemExit(f"Packaged backend is not executable: {backend}")

    with tempfile.TemporaryDirectory(prefix="rcp-packaging-smoke-") as temporary:
        data_dir = Path(temporary) / "data"
        environment = _environment(data_dir)
        environment.update(smoke_agent.prepare(Path(temporary)))
        owner = subprocess.Popen(
            _command(backend, _unused_port()),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            outcome, code = _launch_outcome(owner)
            if outcome.get("outcome") != "owned" or outcome.get("owned") is not True:
                raise RuntimeError(f"First launch did not own the backend: {outcome}")
            base_url = outcome.get("base_url")
            instance_id = outcome.get("instance_id")
            version = outcome.get("version")
            if not all(isinstance(item, str) and item for item in (base_url, instance_id, version)):
                raise RuntimeError(f"First launch omitted its identity: {outcome}")

            health = _wait_for_health(base_url)
            expected = {
                "version": version,
                "instance_id": instance_id,
                "owner_kind": "desktop",
            }
            if any(health.get(key) != value for key, value in expected.items()):
                raise RuntimeError(f"Health identity disagrees with launch identity: {health}")
            metadata_path = data_dir / "rcp-server.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "The packaged backend did not publish ownership metadata."
                ) from exc
            owner_pid = metadata.get("pid")
            if (
                not isinstance(owner_pid, int)
                or isinstance(owner_pid, bool)
                or owner_pid <= 0
                or metadata.get("instance_id") != instance_id
            ):
                raise RuntimeError(f"The packaged backend published invalid ownership: {metadata}")
            if health.get("pid") != owner_pid or health.get("data_dir_id") != metadata.get(
                "data_dir_id"
            ):
                raise RuntimeError(
                    f"Health does not identify the metadata-owning process: {health}"
                )
            _sign_in(base_url, code)
            projects, project_type = _request(f"{base_url}/api/projects")
            if project_type != "application/json" or json.loads(projects) != []:
                raise RuntimeError("The packaged backend project index API is unavailable.")
            _verify_web_bundle(base_url)

            reused = subprocess.run(
                _command(backend, _unused_port()),
                env=environment,
                capture_output=True,
                text=True,
                timeout=LAUNCH_TIMEOUT_SECONDS,
                check=False,
            )
            if reused.returncode != 0:
                raise RuntimeError(
                    f"Second launch failed ({reused.returncode}): {reused.stderr.strip()}"
                )
            try:
                reused_outcome = json.loads(reused.stdout.strip())
            except json.JSONDecodeError as exc:
                raise RuntimeError("Second launch emitted invalid JSON.") from exc
            if isinstance(reused_outcome, dict) and "owner_sign_in_code" in reused_outcome:
                raise RuntimeError("Second launch unexpectedly returned a sign-in code.")
            if reused_outcome != {**outcome, "outcome": "reused", "owned": False}:
                raise RuntimeError(
                    f"Second launch did not reuse the exact running backend: {reused_outcome}"
                )

            smoke_agent.verify(base_url, Path(temporary))

            # A PyInstaller one-file executable has a supervising bootloader
            # process. The lock metadata names the Python server process, which
            # is the authority that the desktop Quit path must signal.
            os.kill(owner_pid, signal.SIGTERM)
            try:
                return_code = owner.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("The packaged backend did not shut down gracefully.") from exc
            shutdown_log = _stderr(owner)
            if (data_dir / "rcp-server.json").exists():
                raise RuntimeError(
                    f"The packaged backend left stale ownership metadata "
                    f"(exit {return_code}): {shutdown_log}"
                )
            expected_termination = return_code == 0 or (
                return_code == -signal.SIGTERM and "Application shutdown complete." in shutdown_log
            )
            if not expected_termination:
                raise RuntimeError(
                    f"The packaged backend exited with {return_code}: {shutdown_log}"
                )
        finally:
            if owner.poll() is None:
                os.killpg(owner.pid, signal.SIGKILL)
                owner.wait()

    print(
        json.dumps(
            {
                "backend": str(backend),
                "result": "passed",
                "toolchain_path": "/usr/bin:/bin:/usr/sbin:/sbin",
                "remote_agent": "passed",
                "remote_repository_preview": "passed",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
