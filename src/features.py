"""Extract a transparent feature vector from each remaining event.

Enrichment (repo metadata, compare stats) lives here so collect.py stays a
pure fetch layer and scoring.py stays a pure function of features + weights.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime
from typing import Any

from .config import AppConfig, QualityWeights
from .filter import is_breaking_message, is_new_repository, parse_conventional_prefix
from .github_client import GitHubClient, GitHubError
from .models import ActivityEvent, Candidate, FeatureVector
from .state import PipelineState, utcnow

log = logging.getLogger("github_to_linkedin.features")

README_MAX_CHARS = 4000

TYPE_RANK = {
    "breaking": 100,
    "feat": 80,
    "perf": 70,
    "fix": 60,
    "refactor": 40,
}

WEAK_MERGE_RE = re.compile(r"^merge(d)? (pull request|branch|remote-tracking)", re.I)


def enrich_repo_metadata(
    events: list[ActivityEvent],
    client: GitHubClient,
) -> dict[str, dict[str, Any] | None]:
    """Stars, forks, private flag, default branch. Call this before the second filter pass."""
    repo_cache: dict[str, dict[str, Any] | None] = {}
    for event in events:
        if not event.repo_full_name:
            continue
        try:
            repo = _repo_metadata(client, event.repo_full_name, repo_cache)
        except GitHubError as exc:
            log.warning("Repo lookup failed for %s: %s", event.repo_full_name, exc)
            continue
        if repo is None:
            event.repo_private = True
            continue
        event.repo_private = bool(repo.get("private"))
        event.repo_stars = int(repo.get("stargazers_count") or 0)
        event.repo_forks = int(repo.get("forks_count") or 0)
        event.repo_description = str(repo.get("description") or "")
        event.default_branch = str(repo.get("default_branch") or "") or event.default_branch
    return repo_cache


def enrich_diff_stats(events: list[ActivityEvent], client: GitHubClient) -> list[ActivityEvent]:
    """Compare/PR line+file stats. Call after privacy / default-branch filters."""
    for event in events:
        try:
            if event.event_type == "PushEvent":
                _enrich_push(event, client)
            elif event.event_type == "PullRequestEvent":
                _enrich_pull(event, client)
        except GitHubError as exc:
            log.warning(
                "Diff enrichment failed for %s (%s %s): %s",
                event.id,
                event.event_type,
                event.repo_full_name,
                exc,
            )
        event.enriched = True
    return events


def enrich_events(
    events: list[ActivityEvent],
    client: GitHubClient,
) -> list[ActivityEvent]:
    """Fill repo metadata + line/file stats. Failures skip the extra data, not the event."""
    enrich_repo_metadata(events, client)
    return enrich_diff_stats(events, client)


def enrich_generation_context(candidate: Candidate, client: GitHubClient) -> None:
    """Attach the repo README for the LLM. Does not affect scoring."""
    event = candidate.lead.event
    if event.readme.strip() or not event.repo_full_name:
        return
    try:
        data = client.get_json(f"/repos/{event.repo_full_name}/readme")
    except GitHubError as exc:
        if exc.status == 404:
            log.info("No README for %s", event.repo_full_name)
            return
        log.warning("README fetch failed for %s: %s", event.repo_full_name, exc)
        return
    if not isinstance(data, dict):
        return
    event.readme = decode_readme_payload(data, max_chars=README_MAX_CHARS)
    if event.readme:
        log.info(
            "Attached README for %s (%s chars)",
            event.repo_full_name,
            len(event.readme),
        )


def decode_readme_payload(data: dict[str, Any], *, max_chars: int = README_MAX_CHARS) -> str:
    """Decode GitHub's /readme JSON (base64) and trim for the prompt."""
    encoding = str(data.get("encoding") or "").lower()
    content = data.get("content")
    if encoding == "base64" and isinstance(content, str) and content.strip():
        try:
            raw = base64.b64decode(content).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            raw = ""
    elif isinstance(content, str):
        raw = content
    else:
        raw = str(data.get("text") or "")
    text = raw.strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n…"


def extract_features(
    event: ActivityEvent,
    cfg: AppConfig,
    state: PipelineState,
    *,
    now: datetime | None = None,
) -> FeatureVector:
    conventional, breaking = _conventional_signals(event)
    text = _search_text(event)
    positive = _matched_keywords(text, cfg.scoring.keywords.positive)
    negative = _matched_keywords(text, cfg.scoring.keywords.negative)
    title_quality, body_quality = _quality_flags(event, cfg.scoring.quality)

    hours: float | None = None
    moment = now or utcnow()
    if state.last_high_score_at is not None:
        delta = moment - state.last_high_score_at
        hours = max(0.0, delta.total_seconds() / 3600.0)

    is_default = True
    if event.event_type == "PushEvent" and event.ref and event.default_branch:
        is_default = event.ref.removeprefix("refs/heads/") == event.default_branch

    event_weight = _event_type_weight(event, cfg)

    return FeatureVector(
        event_type=event.event_type,
        event_type_weight=event_weight,
        lines_changed=event.lines_changed,
        files_changed=event.files_changed,
        conventional_type=conventional,
        is_breaking=breaking,
        positive_keywords=positive,
        negative_keywords=negative,
        title_length=len(event.title.strip()),
        body_length=len((event.body or "").strip()),
        title_quality=title_quality,
        body_quality=body_quality,
        repo_stars=event.repo_stars,
        repo_forks=event.repo_forks,
        hours_since_last_high_score=hours,
        is_default_branch=is_default,
        commit_count=len(event.commits),
    )


def _repo_metadata(
    client: GitHubClient,
    full_name: str,
    cache: dict[str, dict[str, Any] | None],
) -> dict[str, Any] | None:
    if full_name in cache:
        return cache[full_name]
    try:
        data = client.get_json(f"/repos/{full_name}")
    except GitHubError as exc:
        if exc.status == 404:
            log.info("Repo %s not found or not visible; treating as inaccessible", full_name)
            cache[full_name] = None
            return None
        raise
    cache[full_name] = data if isinstance(data, dict) else None
    return cache[full_name]


def _enrich_push(event: ActivityEvent, client: GitHubClient) -> None:
    if event.additions or event.deletions or event.files_changed:
        return
    if not event.before_sha or not event.head_sha:
        return
    try:
        compare = client.get_json(
            f"/repos/{event.repo_full_name}/compare/{event.before_sha}...{event.head_sha}"
        )
    except GitHubError as exc:
        if exc.status in {404, 422}:
            log.info(
                "No compare for %s %s...%s (%s)",
                event.repo_full_name,
                event.before_sha[:7],
                event.head_sha[:7],
                exc.status,
            )
            return
        raise
    if not isinstance(compare, dict):
        return
    files = compare.get("files") or []
    event.files_changed = len(files)
    event.additions = sum(int(f.get("additions") or 0) for f in files if isinstance(f, dict))
    event.deletions = sum(int(f.get("deletions") or 0) for f in files if isinstance(f, dict))
    if not event.commits:
        for raw in compare.get("commits") or []:
            if not isinstance(raw, dict):
                continue
            commit = raw.get("commit") or {}
            event.commits.append(
                # Imported lazily to avoid a circular type-check issue at runtime.
                _commit_from_compare(raw, commit)
            )


def _commit_from_compare(raw: dict[str, Any], commit: dict[str, Any]) -> Any:
    from .models import CommitSummary

    author = raw.get("author") or {}
    login = author.get("login") if isinstance(author, dict) else None
    return CommitSummary(
        sha=str(raw.get("sha") or "")[:40],
        message=str(commit.get("message") or ""),
        author_login=str(login) if login else None,
    )


def _enrich_pull(event: ActivityEvent, client: GitHubClient) -> None:
    pr = event.payload.get("pull_request") or {}
    if event.additions == 0 and pr.get("additions") is not None:
        event.additions = int(pr.get("additions") or 0)
        event.deletions = int(pr.get("deletions") or 0)
        event.files_changed = int(pr.get("changed_files") or 0)
        return
    if event.additions or event.files_changed:
        return
    number = event.pr_number
    if number is None:
        return
    try:
        data = client.get_json(f"/repos/{event.repo_full_name}/pulls/{number}")
    except GitHubError as exc:
        if exc.status == 404:
            return
        raise
    if not isinstance(data, dict):
        return
    event.additions = int(data.get("additions") or 0)
    event.deletions = int(data.get("deletions") or 0)
    event.files_changed = int(data.get("changed_files") or 0)
    if not event.html_url:
        event.html_url = str(data.get("html_url") or "")
    if not event.title:
        event.title = str(data.get("title") or event.title)
    if not event.body:
        event.body = str(data.get("body") or "")


def _event_type_weight(event: ActivityEvent, cfg: AppConfig) -> float:
    weights = cfg.scoring.event_type
    if is_new_repository(event):
        return float(weights.NewRepository)
    mapping = {
        "ReleaseEvent": weights.ReleaseEvent,
        "PullRequestEvent": weights.PullRequestEvent,
        "PushEvent": weights.PushEvent,
        "CreateEvent": weights.CreateEvent,
    }
    return float(mapping.get(event.event_type, weights.default))


def _conventional_signals(event: ActivityEvent) -> tuple[str | None, bool]:
    messages = [event.title, event.body, *(c.message for c in event.commits)]
    best: str | None = None
    best_rank = -1
    breaking = False
    for message in messages:
        if not message:
            continue
        if is_breaking_message(message):
            breaking = True
        parsed = parse_conventional_prefix(message)
        if parsed is None:
            continue
        rank = TYPE_RANK.get(parsed, 0)
        if rank > best_rank:
            best = parsed
            best_rank = rank
    if breaking and (best is None or best_rank < TYPE_RANK["breaking"]):
        # Keep the original type for display; scoring adds the breaking bonus separately.
        pass
    return best, breaking


def _search_text(event: ActivityEvent) -> str:
    parts = [
        event.title,
        event.body,
        event.repo_description,
        event.release_tag or "",
        *(c.message for c in event.commits),
    ]
    return "\n".join(p for p in parts if p).lower()


def _matched_keywords(text: str, keywords: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        needle = kw.lower()
        if needle in seen:
            continue
        if needle in text:
            hits.append(kw)
            seen.add(needle)
    return hits


def _quality_flags(event: ActivityEvent, quality: QualityWeights) -> tuple[bool, bool]:
    title = event.title.strip()
    body = (event.body or "").strip()
    first_line = title.split("\n", 1)[0].strip()
    weak = {w.lower() for w in quality.weak_titles}
    title_ok = (
        len(first_line) >= quality.title_min_chars
        and first_line.lower() not in weak
        and not WEAK_MERGE_RE.match(first_line)
    )
    body_ok = len(body) >= quality.body_min_chars
    return title_ok, body_ok
