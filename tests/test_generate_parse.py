from __future__ import annotations

import pytest

from src.generate import parse_model_json
from src.output import issue_title
from src.models import Candidate, GeneratedDraft
from tests.conftest import make_event
from src.features import extract_features
from src.scoring import score_event


def test_parse_plain_json() -> None:
    raw = '{"post_text": "Shipped the export API.", "reasoning": "Release + feat."}'
    post, reason = parse_model_json(raw)
    assert post == "Shipped the export API."
    assert "Release" in reason


def test_parse_fenced_json() -> None:
    raw = """```json
{"post_text": "Hello from the ship.", "reasoning": "high score"}
```"""
    post, reason = parse_model_json(raw)
    assert post.startswith("Hello")
    assert reason == "high score"


def test_parse_plain_text_fallback() -> None:
    post, reason = parse_model_json("Just a post with no JSON at all.")
    assert post.startswith("Just a post")
    assert "not return" in reason.lower() or "JSON" in reason


def test_parse_empty_raises() -> None:
    with pytest.raises(Exception):
        parse_model_json("   ")


def test_issue_title_format(cfg, empty_state) -> None:
    event = make_event()
    scored = score_event(event, extract_features(event, cfg, empty_state), cfg)
    draft = GeneratedDraft(
        post_text="x",
        reasoning="y",
        score=81.4,
        high_confidence=True,
        model="gemini-2.5-flash",
        provider="gemini",
        candidate=Candidate(lead=scored),
        word_count=10,
    )
    title = issue_title(draft)
    assert title.startswith("LinkedIn Draft – ")
    assert title.endswith("score 81")
