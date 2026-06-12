"""Single-instance lock — two processes on one data dir must not boot."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaudir.config import Config
from pyclaudir.startup import _acquire_instance_lock


def _cfg(tmp_path: Path) -> Config:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    return cfg


def test_lock_acquired_and_blocks_second_instance(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    first = _acquire_instance_lock(cfg)
    assert (cfg.data_dir / ".lock").exists(), "lock file must be created"

    # flock is per open-file-description, so a second open in the same
    # process conflicts exactly like a second process would.
    with pytest.raises(SystemExit, match="already running"):
        _acquire_instance_lock(cfg)

    first.close()


def test_lock_released_on_close_allows_reacquire(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    first = _acquire_instance_lock(cfg)
    first.close()  # process exit releases the flock

    second = _acquire_instance_lock(cfg)
    assert second is not None, "a stopped instance must not block the next boot"
    second.close()
