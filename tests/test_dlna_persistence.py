from __future__ import annotations

import stat
from pathlib import Path

import pytest

from bedside_audio.persistence import StateError, StateStore


def test_private_state_directory_symlink_is_rejected_without_changing_target(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    previous_mode = stat.S_IMODE(elsewhere.stat().st_mode)
    state = tmp_path / "bedside-state"
    state.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(StateError, match="private state directory"):
        StateStore(state, 15).prepare_dir()

    assert stat.S_IMODE(elsewhere.stat().st_mode) == previous_mode
    assert list(elsewhere.iterdir()) == []
