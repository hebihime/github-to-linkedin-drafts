from __future__ import annotations

from src.config import load_config


def test_default_config_loads() -> None:
    cfg = load_config()
    assert cfg.llm.provider == "gemini"
    assert cfg.llm.model.startswith("gemini")
    assert cfg.linkedin.auto_post is False
    assert cfg.scoring.draft_threshold == 55
    assert cfg.scoring.high_confidence_threshold == 75
    assert cfg.scoring.event_type.ReleaseEvent > cfg.scoring.event_type.PullRequestEvent
    assert cfg.scoring.event_type.PullRequestEvent > cfg.scoring.event_type.PushEvent
    assert cfg.scoring.event_type.NewRepository >= cfg.scoring.draft_threshold
