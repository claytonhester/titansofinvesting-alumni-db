"""finalize_pass.sh must abort BEFORE the embed/sync/display steps when any of
the batch-level steps (1-4) failed — never ship a half-finalized snapshot."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "finalize_pass.sh"


def _sandbox(tmp_path: Path, fail_on: str = "") -> tuple[Path, Path]:
    """A throwaway dir with the script, a stub .venv activate, and fake `python`
    / `npm` on PATH that log every call (python exits 1 when its argv mentions
    `fail_on`)."""
    shutil.copy(_SCRIPT, tmp_path / "finalize_pass.sh")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "activate").write_text("# stub\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("python", "npm"):
        stub = bin_dir / tool
        stub.write_text(
            "#!/bin/sh\n"
            f'echo "{tool} $*" >> "{log}"\n'
            + (f'case "$*" in *{fail_on}*) exit 1;; esac\n' if fail_on else "")
            + "exit 0\n"
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return bin_dir, log


def _run(tmp_path: Path, bin_dir: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(tmp_path / "finalize_pass.sh")],
        capture_output=True, text=True, env=env, timeout=30,
    )


@pytest.mark.integration
def test_failed_batch_step_aborts_before_embed(tmp_path: Path) -> None:
    bin_dir, log = _sandbox(tmp_path, fail_on="compute_completeness.py")
    res = _run(tmp_path, bin_dir)
    calls = log.read_text()

    assert res.returncode != 0
    assert "ABORTED" in res.stdout
    assert "2/7 profile completeness" in res.stdout  # names the failed step
    assert "npm" not in calls                        # embed + sync never ran
    assert "make_display_db.py" not in calls
    # Steps 3-4 still ran (independent of 2) — only the ship-side steps are gated.
    assert "reclassify_levels.py" in calls and "phase3_insights.py --llm" in calls


@pytest.mark.integration
def test_clean_pass_runs_everything_and_points_at_the_blob(tmp_path: Path) -> None:
    bin_dir, log = _sandbox(tmp_path)
    res = _run(tmp_path, bin_dir)
    calls = log.read_text()

    assert res.returncode == 0, res.stdout + res.stderr
    assert calls.count("npm") == 2 and "make_display_db.py" in calls
    assert "titans_display.db" in res.stdout and "Vercel Blob" in res.stdout
    assert "commit web/data/titans.db" not in res.stdout  # the stale instruction
