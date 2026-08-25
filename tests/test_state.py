from __future__ import annotations

from src.state import PipelineState, load_state, save_state


def test_mark_processed_caps(tmp_path, cfg) -> None:
    state = PipelineState()
    state.mark_processed([f"id-{i}" for i in range(5)], max_ids=3)
    assert state.processed_event_ids == ["id-2", "id-3", "id-4"]
    assert state.is_processed("id-4")
    assert not state.is_processed("id-0")


def test_round_trip(tmp_path, cfg) -> None:
    path = tmp_path / "state.json"
    state = PipelineState(processed_event_ids=["a", "b"])
    save_state(path, state, cfg.state)
    loaded = load_state(path)
    assert loaded.processed_event_ids == ["a", "b"]
    assert loaded.is_processed("a")


def test_missing_file_is_fresh(tmp_path) -> None:
    loaded = load_state(tmp_path / "nope.json")
    assert loaded.processed_event_ids == []
    assert loaded.last_success_at is None
