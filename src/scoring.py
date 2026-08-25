"""Deterministic 0–100 scorer. No LLM. Every component is named and tunable."""

from __future__ import annotations

import logging
import math
from datetime import datetime

from .config import AppConfig, LogScaleWeight
from .filter import is_new_repository
from .models import ActivityEvent, Candidate, FeatureVector, ScoreBreakdown, ScoredEvent
from .state import utcnow

log = logging.getLogger("github_to_linkedin.scoring")


def score_event(
    event: ActivityEvent,
    features: FeatureVector,
    cfg: AppConfig,
) -> ScoredEvent:
    components: dict[str, float] = {}

    components["event_type"] = features.event_type_weight
    components["lines_changed"] = log_scale(
        features.lines_changed, cfg.scoring.lines_changed
    )
    components["files_changed"] = log_scale(
        features.files_changed, cfg.scoring.files_changed
    )
    components["conventional"] = _conventional_bonus(features, cfg)
    components["keywords_positive"] = min(
        cfg.scoring.keywords.positive_cap,
        cfg.scoring.keywords.positive_weight * len(features.positive_keywords),
    )
    components["keywords_negative"] = cfg.scoring.keywords.negative_weight * len(
        features.negative_keywords
    )
    components["title_quality"] = (
        cfg.scoring.quality.title_bonus if features.title_quality else 0.0
    )
    components["body_quality"] = (
        cfg.scoring.quality.body_bonus if features.body_quality else 0.0
    )
    components["repo_popularity"] = log_scale(
        features.repo_stars, cfg.scoring.repo_popularity
    )
    components["frequency_penalty"] = -_frequency_penalty(features, cfg)
    if not features.is_default_branch:
        components["non_default_branch"] = -abs(cfg.scoring.non_default_branch_penalty)

    total = sum(components.values())
    clamped = clamp(total, 0.0, 100.0)
    breakdown = ScoreBreakdown(components=components, total=total, clamped=clamped)
    return ScoredEvent(event=event, features=features, breakdown=breakdown)


def score_events(
    events: list[ActivityEvent],
    features_for: list[FeatureVector],
    cfg: AppConfig,
) -> list[ScoredEvent]:
    scored: list[ScoredEvent] = []
    for event, features in zip(events, features_for, strict=True):
        item = score_event(event, features, cfg)
        log.info(
            "score=%5.1f  %-18s  %s — %s",
            item.score,
            item.event.event_type,
            item.event.repo_full_name,
            _truncate(item.event.title, 72),
        )
        scored.append(item)
    return scored


def select_candidates(scored: list[ScoredEvent], cfg: AppConfig) -> list[Candidate]:
    """Cluster above-threshold events by repo; keep the top N clusters."""
    threshold = cfg.scoring.draft_threshold
    above = [s for s in scored if s.score >= threshold]
    above.sort(key=lambda s: (-s.score, s.event.created_at))
    if not above:
        log.info(
            "No events scored ≥ %.0f (draft threshold). Highest was %s.",
            threshold,
            f"{max(scored, key=lambda s: s.score).score:.1f}" if scored else "n/a",
        )
        return []

    by_repo: dict[str, list[ScoredEvent]] = {}
    for item in above:
        by_repo.setdefault(item.event.repo_full_name, []).append(item)

    clusters: list[Candidate] = []
    for repo, items in by_repo.items():
        items.sort(key=lambda s: (-_type_rank(s.event), -s.score))
        lead = items[0]
        supporting = [x for x in items[1:] if x.event.id != lead.event.id]
        clusters.append(Candidate(lead=lead, supporting=supporting))
        log.info(
            "cluster %s lead score=%.1f (%s) + %s supporting",
            repo,
            lead.score,
            lead.event.event_type,
            len(supporting),
        )

    clusters.sort(key=lambda c: -c.score)
    limited = clusters[: max(1, cfg.output.max_drafts_per_run)]
    if len(clusters) > len(limited):
        log.info(
            "Capped to %s draft(s); %s other high-score cluster(s) skipped this run",
            len(limited),
            len(clusters) - len(limited),
        )
    return limited


def log_scale(value: float, spec: LogScaleWeight) -> float:
    """0 at 0; `weight` at `midpoint`; never exceeds `weight`."""
    if value <= 0 or spec.weight == 0 or spec.midpoint <= 0:
        return 0.0
    return min(spec.weight, spec.weight * math.log1p(value) / math.log1p(spec.midpoint))


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _conventional_bonus(features: FeatureVector, cfg: AppConfig) -> float:
    bonus = 0.0
    conv = cfg.scoring.conventional
    if features.conventional_type == "feat":
        bonus += conv.feat
    elif features.conventional_type == "fix":
        bonus += conv.fix
    elif features.conventional_type == "perf":
        bonus += conv.perf
    elif features.conventional_type == "refactor":
        bonus += conv.refactor
    if features.is_breaking:
        bonus += conv.breaking
    return bonus


def _frequency_penalty(features: FeatureVector, cfg: AppConfig) -> float:
    hours = features.hours_since_last_high_score
    window = cfg.scoring.frequency_penalty.hours_window
    max_penalty = cfg.scoring.frequency_penalty.max_penalty
    if hours is None or window <= 0 or hours >= window:
        return 0.0
    return max_penalty * (1.0 - (hours / window))


def _type_rank(event: ActivityEvent) -> int:
    if is_new_repository(event):
        return 25  # above a push, below a merged PR / release
    return {
        "ReleaseEvent": 40,
        "PullRequestEvent": 30,
        "PushEvent": 20,
        "CreateEvent": 10,
    }.get(event.event_type, 0)


def _truncate(text: str, width: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def hours_since(ts: datetime | None, now: datetime | None = None) -> float | None:
    if ts is None:
        return None
    moment = now or utcnow()
    return max(0.0, (moment - ts).total_seconds() / 3600.0)
