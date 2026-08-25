"""Deterministic hard filters. Run before enrichment and scoring."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from .config import AppConfig, FilterConfig
from .models import ActivityEvent, CommitSummary
from .state import PipelineState

log = logging.getLogger("github_to_linkedin.filter")

def create_ref_type(event: ActivityEvent) -> str:
    return str(event.payload.get("ref_type") or "").lower()


def is_new_repository(event: ActivityEvent) -> bool:
    return event.event_type == "CreateEvent" and create_ref_type(event) == "repository"


CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?P<scope>\([^)]*\))?(?P<breaking>!)?:\s+",
)
MERGE_PR_RE = re.compile(r"^merge(d)? (pull request|branch|remote-tracking)", re.I)
SIGNAL_TYPES = frozenset({"feat", "fix", "perf", "breaking"})


@dataclass
class FilterResult:
    kept: list[ActivityEvent]
    dropped: Counter[str] = field(default_factory=Counter)

    def log_summary(self) -> None:
        if not self.dropped:
            log.info("Hard filters kept %s events (none dropped)", len(self.kept))
            return
        parts = ", ".join(f"{reason}={count}" for reason, count in sorted(self.dropped.items()))
        log.info("Hard filters kept %s events; dropped %s", len(self.kept), parts)


def apply_hard_filters(
    events: list[ActivityEvent],
    cfg: AppConfig,
    state: PipelineState,
) -> FilterResult:
    """Drop bots, chore/docs/ci noise, private repos, already-processed IDs.

    Size filters that need line/file stats run later via `apply_size_filters`
    after feature enrichment.
    """
    result = FilterResult(kept=[])
    interesting = set(cfg.github.interesting_event_types)
    for event in events:
        reason = _drop_reason(event, cfg, state, interesting)
        if reason:
            result.dropped[reason] += 1
            log.debug("drop %s [%s] %s — %s", event.id, event.event_type, event.title, reason)
            continue
        result.kept.append(event)
    result.log_summary()
    return result


def drop_redundant_tag_creates(
    events: list[ActivityEvent],
    cfg: AppConfig,
) -> list[ActivityEvent]:
    """If a release exists for a tag, drop the matching CreateEvent (tag)."""
    if not cfg.filters.drop_tag_creates_if_release_exists:
        return events
    release_keys: set[tuple[str, str]] = set()
    for event in events:
        if event.event_type != "ReleaseEvent":
            continue
        tag = (event.release_tag or "").removeprefix("refs/tags/")
        if tag:
            release_keys.add((event.repo_full_name.lower(), tag.lower()))
    if not release_keys:
        return events
    kept: list[ActivityEvent] = []
    dropped = 0
    for event in events:
        if event.event_type == "CreateEvent":
            ref = (event.ref or str(event.payload.get("ref") or "")).removeprefix("refs/tags/")
            if (event.repo_full_name.lower(), ref.lower()) in release_keys:
                dropped += 1
                continue
        kept.append(event)
    if dropped:
        log.info("Dropped %s tag CreateEvent(s) already covered by a ReleaseEvent", dropped)
    return kept


def apply_size_filters(events: list[ActivityEvent], cfg: AppConfig) -> FilterResult:
    result = FilterResult(kept=[])
    for event in events:
        reason = _size_drop_reason(event, cfg.filters)
        if reason:
            result.dropped[reason] += 1
            log.debug("drop %s after enrichment — %s", event.id, reason)
            continue
        result.kept.append(event)
    if result.dropped:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(result.dropped.items()))
        log.info("Size filters kept %s events; dropped %s", len(result.kept), parts)
    else:
        log.info("Size filters kept %s events", len(result.kept))
    return result


def _drop_reason(
    event: ActivityEvent,
    cfg: AppConfig,
    state: PipelineState,
    interesting: set[str],
) -> str | None:
    if state.is_processed(event.id):
        return "already_processed"
    if interesting and event.event_type not in interesting:
        return "event_type"
    if _is_bot(event.actor_login, cfg.filters.bot_authors):
        return "bot_author"
    if _any_commit_bot(event.commits, cfg.filters.bot_authors):
        return "bot_author"
    if event.repo_private:
        allowed = {r.lower() for r in cfg.github.allowed_private_repos}
        if not cfg.github.include_private and event.repo_full_name.lower() not in allowed:
            return "private_repo"

    if event.event_type == "PullRequestEvent" and not event.merged:
        return "pr_not_merged"

    if event.event_type == "CreateEvent":
        ref_type = create_ref_type(event)
        if ref_type not in {"tag", "repository"}:
            return "create_not_repo_or_tag"

    if event.event_type == "ReleaseEvent" and cfg.filters.drop_prereleases:
        release = event.payload.get("release") or {}
        if release.get("prerelease"):
            return "prerelease"

    if event.event_type == "PushEvent" and cfg.github.only_default_branch_pushes:
        if event.default_branch and event.ref:
            branch = event.ref.removeprefix("refs/heads/")
            if branch != event.default_branch:
                return "non_default_branch"

    if event.event_type not in {"ReleaseEvent", "CreateEvent"} and _all_dropped_commit_types(
        event, cfg.filters.drop_commit_types
    ):
        return "conventional_noise"

    return None


def _size_drop_reason(event: ActivityEvent, filters: FilterConfig) -> str | None:
    if event.event_type in {"ReleaseEvent", "CreateEvent"}:
        return None
    if event.event_type == "PushEvent" and not event.commits and event.lines_changed == 0:
        return "empty_push"

    stats_known = event.lines_changed > 0 or event.files_changed > 0
    if not stats_known:
        # Compare/PR stats missing — do not fail closed and drop real work.
        return None

    signal = _has_signal_type(event)
    min_lines = filters.min_lines_for_signal_types if signal else filters.min_lines_changed
    min_files = 1 if signal else filters.min_files_changed

    if event.lines_changed < min_lines:
        return "too_small_lines"
    if event.files_changed < min_files:
        return "too_small_files"
    return None


def _is_bot(login: str, bot_authors: tuple[str, ...]) -> bool:
    if not login:
        return False
    lowered = login.lower()
    if lowered.endswith("[bot]") or lowered.endswith("-bot") or lowered.endswith("_bot"):
        return True
    bots = {b.lower() for b in bot_authors}
    return lowered in bots


def _any_commit_bot(commits: list[CommitSummary], bot_authors: tuple[str, ...]) -> bool:
    if not commits:
        return False
    # Only drop when *all* commits are from bots (a human push that includes a
    # lockfile bump from dependabot should still be kept if other filters pass).
    logins = [c.author_login for c in commits if c.author_login]
    if not logins:
        return False
    return all(_is_bot(login, bot_authors) for login in logins)


def _all_dropped_commit_types(event: ActivityEvent, drop_types: tuple[str, ...]) -> bool:
    messages = _messages_for(event)
    if not messages:
        return False
    meaningful = [m for m in messages if m and not MERGE_PR_RE.match(m)]
    if not meaningful:
        # Pure merge commits with no other signal.
        return True
    return all(_is_dropped_type(m, drop_types) for m in meaningful)


def _has_signal_type(event: ActivityEvent) -> bool:
    for message in _messages_for(event):
        parsed = parse_conventional_prefix(message)
        if parsed in SIGNAL_TYPES or parsed == "feat" or parsed == "fix" or parsed == "perf":
            return True
        if parsed and parsed.endswith("!"):
            return True
        if "BREAKING CHANGE" in message.upper():
            return True
    return False


def _messages_for(event: ActivityEvent) -> list[str]:
    messages = [event.title, event.body]
    messages.extend(c.message for c in event.commits)
    return [m.strip() for m in messages if m and m.strip()]


def _is_dropped_type(message: str, drop_types: tuple[str, ...]) -> bool:
    parsed = parse_conventional_prefix(message)
    if parsed is None:
        return False
    return parsed.lower() in {t.lower() for t in drop_types}


def parse_conventional_prefix(message: str) -> str | None:
    """Return the conventional-commit type of the first line, if any."""
    first = message.strip().split("\n", 1)[0]
    match = CONVENTIONAL_RE.match(first)
    if not match:
        return None
    return match.group("type").lower()


def is_breaking_message(message: str) -> bool:
    first = message.strip().split("\n", 1)[0]
    match = CONVENTIONAL_RE.match(first)
    if match and match.group("breaking"):
        return True
    return "BREAKING CHANGE" in message.upper() or "BREAKING-CHANGE" in message.upper()
