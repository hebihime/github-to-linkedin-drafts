"""Shared dataclasses for the collection → filter → score → generate pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class CommitSummary:
    sha: str
    message: str
    author_login: str | None = None


@dataclass
class ActivityEvent:
    """Normalized GitHub activity (Events API or workflow payload)."""

    id: str
    event_type: str
    created_at: datetime
    repo_full_name: str
    actor_login: str
    title: str
    body: str
    html_url: str
    commits: list[CommitSummary] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    # Filled during collection when present on the payload
    action: str | None = None
    merged: bool = False
    ref: str | None = None
    before_sha: str | None = None
    head_sha: str | None = None
    pr_number: int | None = None
    release_tag: str | None = None
    default_branch: str | None = None

    # Filled during feature enrichment
    repo_private: bool | None = None
    repo_stars: int = 0
    repo_forks: int = 0
    repo_description: str = ""
    readme: str = ""  # generation-only; never scored
    additions: int = 0
    deletions: int = 0
    files_changed: int = 0
    enriched: bool = False

    @property
    def lines_changed(self) -> int:
        return self.additions + self.deletions

    def identity_keys(self) -> list[str]:
        """Stable ids so a delayed Events API hit is not drafted twice.

        GitHub event ids are numeric and unique per delivery. Pushes also
        fingerprint as push:{repo}:{head}; new repos as created-repo:{repo}.
        """
        keys = [self.id]
        repo = self.repo_full_name.lower()
        if self.event_type == "PushEvent" and self.head_sha and repo:
            fingerprint = f"push:{repo}:{self.head_sha[:40]}"
            if fingerprint not in keys:
                keys.append(fingerprint)
        ref_type = str(self.payload.get("ref_type") or "").lower()
        if self.event_type == "CreateEvent" and ref_type == "repository" and repo:
            fingerprint = f"created-repo:{repo}"
            if fingerprint not in keys:
                keys.append(fingerprint)
        return keys


@dataclass
class FeatureVector:
    """Transparent, inspectable features used by the scorer."""

    event_type: str
    event_type_weight: float
    lines_changed: int
    files_changed: int
    conventional_type: str | None
    is_breaking: bool
    positive_keywords: list[str]
    negative_keywords: list[str]
    title_length: int
    body_length: int
    title_quality: bool
    body_quality: bool
    repo_stars: int
    repo_forks: int
    hours_since_last_high_score: float | None
    is_default_branch: bool
    commit_count: int


@dataclass
class ScoreBreakdown:
    """Per-component contributions that sum (then clamp) to the final score."""

    components: dict[str, float]
    total: float
    clamped: float

    def as_rows(self) -> list[tuple[str, float]]:
        return list(self.components.items())


@dataclass
class ScoredEvent:
    event: ActivityEvent
    features: FeatureVector
    breakdown: ScoreBreakdown

    @property
    def score(self) -> float:
        return self.breakdown.clamped


@dataclass
class Candidate:
    """One or more related high-score events that will become a single draft."""

    lead: ScoredEvent
    supporting: list[ScoredEvent] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.lead.score

    @property
    def all_events(self) -> list[ScoredEvent]:
        return [self.lead, *self.supporting]


@dataclass
class GeneratedDraft:
    post_text: str
    reasoning: str
    score: float
    high_confidence: bool
    model: str
    provider: str
    candidate: Candidate
    word_count: int
