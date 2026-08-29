from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.main import _lookback_since
from src.state import PipelineState


def test_lookback_uses_full_window_not_last_success(cfg) -> None:
    now = datetime.now(timezone.utc)
    last_success = now - timedelta(hours=2)
    state = PipelineState(last_success_at=last_success)
    since = _lookback_since(cfg, state)
    expected_floor = now - timedelta(hours=cfg.github.lookback_hours)
    assert since < last_success
    assert abs((since - expected_floor).total_seconds()) < 2
