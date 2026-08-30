from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from src.generate import _gemini_thinking_config, parse_model_json, render_user_prompt
from src.output import issue_title
from src.models import Candidate, GeneratedDraft
from tests.conftest import make_event
from src.features import decode_readme_payload, enrich_generation_context, extract_features
from src.github_client import GitHubError
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
        model="gemini-3.7-flash",
        provider="gemini",
        candidate=Candidate(lead=scored),
        word_count=10,
    )
    title = issue_title(draft)
    assert title.startswith("LinkedIn Draft – ")
    assert title.endswith("score 81")


class _FakeThinking:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class _FakeTypes:
    ThinkingConfig = _FakeThinking


def test_gemini_3_uses_thinking_level(cfg) -> None:
    llm = replace(cfg.llm, model="gemini-3.7-flash", thinking_level="medium")
    out = _gemini_thinking_config(llm, _FakeTypes)
    assert out.kwargs == {"thinking_level": "medium"}


def test_gemini_25_uses_thinking_budget_when_level_empty(cfg) -> None:
    llm = replace(cfg.llm, model="gemini-2.5-flash", thinking_level="")
    out = _gemini_thinking_config(llm, _FakeTypes)
    assert out.kwargs == {"thinking_budget": 0}


def test_gemini_3_never_sends_budget_zero(cfg) -> None:
    llm = replace(cfg.llm, model="gemini-3.7-flash", thinking_level="")
    out = _gemini_thinking_config(llm, _FakeTypes)
    assert out.kwargs == {"thinking_level": "medium"}


def test_user_prompt_includes_readme_and_new_repo_flag(cfg, empty_state) -> None:
    event = make_event(
        event_type="CreateEvent",
        title="Created repository hebihime/web3-restaurant-api",
        body="origin-keyed agent-payable menu wrap",
        repo="hebihime/web3-restaurant-api",
    )
    event.payload = {"ref_type": "repository"}
    event.readme = (
        "Restaurants already publish menu feeds to stay in sync with delivery apps. "
        "Wrap those feeds as x402-payable resources so an assistant can order takeout."
    )
    scored = score_event(event, extract_features(event, cfg, empty_state), cfg)
    prompt = render_user_prompt(Candidate(lead=scored), cfg)
    assert "New repository: yes" in prompt
    assert "assistant can order takeout" in prompt
    assert "do not invent a thesis" not in prompt.lower()


def test_user_prompt_empty_readme_forbids_invented_thesis(cfg, empty_state) -> None:
    event = make_event()
    scored = score_event(event, extract_features(event, cfg, empty_state), cfg)
    prompt = render_user_prompt(Candidate(lead=scored), cfg)
    assert "New repository: no" in prompt
    assert "do not invent a thesis" in prompt.lower()


def test_decode_readme_base64() -> None:
    text = "# Why\n\nWrap existing menu feeds as agent-payable resources."
    payload = {
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
    }
    assert "agent-payable resources" in decode_readme_payload(payload)


def test_decode_readme_truncates() -> None:
    payload = {
        "encoding": "base64",
        "content": base64.b64encode(("a" * 5000).encode()).decode(),
    }
    out = decode_readme_payload(payload, max_chars=100)
    assert out.endswith("…")
    assert len(out) <= 110


class _StubGithub:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.paths: list[str] = []

    def get_json(self, path: str, params: dict | None = None) -> object:
        self.paths.append(path)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_enrich_generation_context_attaches_readme(cfg, empty_state) -> None:
    event = make_event(repo="acme/menu-wrap")
    scored = score_event(event, extract_features(event, cfg, empty_state), cfg)
    candidate = Candidate(lead=scored)
    text = "The bet is wrapping existing menu feeds."
    stub = _StubGithub(
        {
            "encoding": "base64",
            "content": base64.b64encode(text.encode()).decode(),
        }
    )
    enrich_generation_context(candidate, stub)  # type: ignore[arg-type]
    assert candidate.lead.event.readme == text
    assert stub.paths == ["/repos/acme/menu-wrap/readme"]


def test_enrich_generation_context_skips_when_readme_set(cfg, empty_state) -> None:
    event = make_event(repo="acme/menu-wrap")
    event.readme = "already here"
    scored = score_event(event, extract_features(event, cfg, empty_state), cfg)
    candidate = Candidate(lead=scored)
    stub = _StubGithub({"encoding": "base64", "content": ""})
    enrich_generation_context(candidate, stub)  # type: ignore[arg-type]
    assert candidate.lead.event.readme == "already here"
    assert stub.paths == []


def test_enrich_generation_context_swallows_404(cfg, empty_state) -> None:
    event = make_event(repo="acme/menu-wrap")
    scored = score_event(event, extract_features(event, cfg, empty_state), cfg)
    candidate = Candidate(lead=scored)
    stub = _StubGithub(GitHubError("missing", status=404))
    enrich_generation_context(candidate, stub)  # type: ignore[arg-type]
    assert candidate.lead.event.readme == ""
