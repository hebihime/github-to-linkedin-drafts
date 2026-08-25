"""LLM generation — only called for candidates that already cleared the scorer.

Default provider is Google Gemini Flash via the official `google-genai` client.
OpenAI-compatible providers (OpenAI, Grok / xAI, OpenRouter, custom) share one
httpx path. Judgment stays in scoring.py; this module only writes prose.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import httpx

from .config import AppConfig, LLMConfig, repo_root
from .models import Candidate, GeneratedDraft
from .state import format_dt

log = logging.getLogger("github_to_linkedin.generate")

JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
    "grok": {
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "model": "grok-4",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "openai/gpt-4o-mini",
    },
    "openai_compatible": {
        "base_url": "",
        "api_key_env": "OPENAI_API_KEY",
        "model": "",
    },
}


class GenerationError(RuntimeError):
    pass


def generate_draft(candidate: Candidate, cfg: AppConfig) -> GeneratedDraft:
    system_prompt = _read_prompt(cfg.llm.system_prompt_path)
    user_prompt = render_user_prompt(candidate, cfg)
    raw = _complete(cfg.llm, system_prompt, user_prompt)
    post_text, reasoning = parse_model_json(raw)
    post_text = post_text.strip()
    words = _word_count(post_text)
    if words < 120 or words > 420:
        log.warning(
            "Generated post is %s words (target 150–350). Using it anyway.",
            words,
        )
    high = candidate.score >= cfg.scoring.high_confidence_threshold
    draft = GeneratedDraft(
        post_text=post_text,
        reasoning=reasoning.strip(),
        score=candidate.score,
        high_confidence=high,
        model=cfg.llm.model,
        provider=cfg.llm.provider,
        candidate=candidate,
        word_count=words,
    )
    log.info(
        "Generated %s-word draft (provider=%s model=%s score=%.1f)",
        words,
        draft.provider,
        draft.model,
        draft.score,
    )
    return draft


def render_user_prompt(candidate: Candidate, cfg: AppConfig) -> str:
    template = _read_prompt(cfg.llm.user_prompt_path)
    lead = candidate.lead
    event = lead.event
    features = lead.features
    commits = "\n".join(
        f"- {c.sha[:7]} {c.message.splitlines()[0][:160]}" for c in event.commits[:12]
    ) or "(none listed)"
    breakdown = "\n".join(
        f"- {name}: {value:+.1f}" for name, value in lead.breakdown.as_rows()
    )
    supporting = _format_supporting(candidate)
    values = {
        "username": cfg.github.username,
        "score": f"{candidate.score:.1f}",
        "high_confidence_threshold": f"{cfg.scoring.high_confidence_threshold:.0f}",
        "draft_threshold": f"{cfg.scoring.draft_threshold:.0f}",
        "high_confidence": "yes" if candidate.score >= cfg.scoring.high_confidence_threshold else "no",
        "lead_type": event.event_type,
        "lead_repo": event.repo_full_name,
        "lead_title": event.title,
        "lead_url": event.html_url,
        "lead_when": format_dt(event.created_at) or "",
        "lead_additions": str(event.additions),
        "lead_deletions": str(event.deletions),
        "lead_files": str(event.files_changed),
        "lead_stars": str(event.repo_stars),
        "lead_forks": str(event.repo_forks),
        "lead_conventional": features.conventional_type or "none",
        "lead_breaking": "yes" if features.is_breaking else "no",
        "lead_positive_keywords": ", ".join(features.positive_keywords) or "none",
        "lead_negative_keywords": ", ".join(features.negative_keywords) or "none",
        "lead_body": (event.body or "").strip()[:4000] or "(empty)",
        "lead_commits": commits,
        "score_breakdown": breakdown,
        "supporting": supporting or "(none)",
        "repo_description": event.repo_description or "(none)",
    }
    try:
        return template.format(**values)
    except KeyError as exc:
        raise GenerationError(f"User prompt template is missing placeholder {exc}") from exc


def parse_model_json(raw: str) -> tuple[str, str]:
    text = raw.strip()
    if not text:
        raise GenerationError("LLM returned an empty response")
    candidate = text
    fenced = JSON_FENCE_RE.search(text)
    if fenced:
        candidate = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            candidate = text[start : end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        log.warning("LLM response was not JSON (%s); treating as plain post text", exc)
        return text, "Model did not return structured JSON."
    if not isinstance(data, dict):
        raise GenerationError("LLM JSON was not an object")
    post = str(data.get("post_text") or data.get("post") or "").strip()
    reasoning = str(data.get("reasoning") or "").strip()
    if not post:
        raise GenerationError("LLM JSON missing post_text")
    return post, reasoning or "No reasoning provided."


def _complete(cfg: LLMConfig, system_prompt: str, user_prompt: str) -> str:
    provider = cfg.provider.strip().lower()
    if provider in {"gemini", "google", "google-genai"}:
        return _complete_gemini(cfg, system_prompt, user_prompt)
    if provider != "gemini" and cfg.model.startswith("gemini"):
        log.warning(
            "llm.provider=%s but llm.model=%s looks like a Gemini id; "
            "update llm.model when you switch providers.",
            cfg.provider,
            cfg.model,
        )
    if provider in PROVIDER_DEFAULTS or provider == "openai_compatible":
        return _complete_openai_compatible(cfg, system_prompt, user_prompt)
    raise GenerationError(
        f"Unknown llm.provider '{cfg.provider}'. "
        "Use gemini, openai, grok, openrouter, or openai_compatible."
    )


def _complete_gemini(cfg: LLMConfig, system_prompt: str, user_prompt: str) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise GenerationError(
            "google-genai is not installed. pip install google-genai"
        ) from exc

    api_key = _api_key(cfg, ("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    client = genai.Client(api_key=api_key)
    log.info("Calling Gemini model=%s", cfg.model)
    try:
        response = client.models.generate_content(
            model=cfg.model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=cfg.temperature,
                max_output_tokens=cfg.max_output_tokens,
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
    except Exception as exc:  # google-genai raises a variety of types
        raise GenerationError(f"Gemini request failed: {exc}") from exc
    text = getattr(response, "text", None)
    if not text:
        raise GenerationError("Gemini returned no text")
    return text


def _complete_openai_compatible(
    cfg: LLMConfig, system_prompt: str, user_prompt: str
) -> str:
    defaults = PROVIDER_DEFAULTS.get(cfg.provider, PROVIDER_DEFAULTS["openai_compatible"])
    base_url = (cfg.base_url or defaults.get("base_url") or "").rstrip("/")
    if not base_url:
        raise GenerationError(
            f"llm.base_url is required for provider '{cfg.provider}'."
        )
    env_name = cfg.api_key_env or defaults.get("api_key_env") or "OPENAI_API_KEY"
    api_key = _api_key(cfg, (env_name,))
    model = cfg.model or defaults.get("model") or ""
    if not model:
        raise GenerationError("llm.model is required for this provider.")

    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_output_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    log.info("Calling OpenAI-compatible provider=%s model=%s", cfg.provider, model)
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise GenerationError(f"LLM HTTP error: {exc}") from exc
    if response.status_code >= 400:
        raise GenerationError(
            f"LLM {response.status_code} from {url}: {response.text[:500]}"
        )
    data = response.json()
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise GenerationError(f"Unexpected LLM response shape: {data!r}"[:500]) from exc


def _api_key(cfg: LLMConfig, names: tuple[str, ...]) -> str:
    ordered = []
    if cfg.api_key_env:
        ordered.append(cfg.api_key_env)
    ordered.extend(names)
    seen: set[str] = set()
    for name in ordered:
        if name in seen:
            continue
        seen.add(name)
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise GenerationError(
        "Missing LLM API key. Set " + " or ".join(ordered) + "."
    )


def _read_prompt(relative: str) -> str:
    path = Path(relative)
    if not path.is_absolute():
        candidate = Path.cwd() / path
        path = candidate if candidate.exists() else repo_root() / path
    if not path.exists():
        raise GenerationError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def _format_supporting(candidate: Candidate) -> str:
    lines: list[str] = []
    for item in candidate.supporting:
        event = item.event
        lines.append(
            f"- [{item.score:.0f}] {event.event_type} {event.repo_full_name}: "
            f"{event.title} ({event.html_url})"
        )
    return "\n".join(lines)


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))
