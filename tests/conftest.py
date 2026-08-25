from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.config import load_config
from src.models import ActivityEvent, CommitSummary
from src.state import PipelineState


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def empty_state() -> PipelineState:
    return PipelineState()


def make_event(
    *,
    event_id: str = "1",
    event_type: str = "PullRequestEvent",
    title: str = "feat: add streaming export API",
    body: str = (
        "Adds a streaming export endpoint so large workspaces can download "
        "audit logs without loading the full dataset into memory. Includes "
        "backpressure and a documented error contract."
    ),
    repo: str = "acme/audit-export",
    actor: str = "jane",
    merged: bool = True,
    additions: int = 240,
    deletions: int = 12,
    files_changed: int = 8,
    stars: int = 320,
    forks: int = 18,
    private: bool = False,
    commits: list[CommitSummary] | None = None,
    ref: str | None = "refs/heads/main",
    default_branch: str | None = "main",
    release_tag: str | None = None,
    created_at: datetime | None = None,
) -> ActivityEvent:
    return ActivityEvent(
        id=event_id,
        event_type=event_type,
        created_at=created_at or datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc),
        repo_full_name=repo,
        actor_login=actor,
        title=title,
        body=body,
        html_url=f"https://github.com/{repo}/pull/12",
        commits=commits or [],
        payload={},
        merged=merged,
        ref=ref,
        default_branch=default_branch,
        release_tag=release_tag,
        repo_private=private,
        repo_stars=stars,
        repo_forks=forks,
        additions=additions,
        deletions=deletions,
        files_changed=files_changed,
        enriched=True,
        repo_description="Export audit logs at scale",
    )
