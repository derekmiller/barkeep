"""The package entry point must not execute the poller twice.

``python -m barkeep.ingest.imap`` triggers a runpy ``RuntimeWarning`` because
``barkeep.ingest.__init__`` imports the module eagerly, so it is already in
``sys.modules`` when runpy executes it as ``__main__``. Guard against a
regression back to that invocation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_package_entrypoint_runs_without_an_import_warning() -> None:
    env = {
        **os.environ,
        "IMAP_HOST": "127.0.0.1",
        "IMAP_USER": "owner@example.com",
        "IMAP_PASSWORD": "secret",
        "IMAP_SENDER": "reports@example.com",
        "IMAP_SINCE_DAYS": "1",
        "DATABASE_URL": "postgresql://barkeep:barkeep@127.0.0.1:1/none",
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }

    completed = subprocess.run(
        [sys.executable, "-m", "barkeep.ingest"],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )

    assert "RuntimeWarning" not in completed.stderr
    assert completed.returncode == 1, completed.stderr
