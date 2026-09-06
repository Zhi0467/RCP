"""Startup observations never infer completion from an unreachable backend."""

from __future__ import annotations

import logging
from pathlib import Path

from rcp.compute_jobs.jobs import refresh_compute_job
from rcp.compute_jobs.text import safe_compute_diagnostic
from rcp.config import Manifest
from rcp.storage import AppStore

logger = logging.getLogger(__name__)


def reconcile_compute_jobs(
    store: AppStore,
    manifest: Manifest,
    *,
    project_id: str,
    data_dir: Path,
) -> None:
    for record in store.running_compute_jobs():
        if record.project_id != project_id:
            continue
        try:
            refreshed = refresh_compute_job(store, manifest, record.job_id, data_dir=data_dir)
            if refreshed.diagnostic:
                logger.warning("Compute job %s: %s", record.job_id, refreshed.diagnostic)
        except Exception as exc:
            logger.warning("Compute reconciliation failed: %s", safe_compute_diagnostic(str(exc)))
