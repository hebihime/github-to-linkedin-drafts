"""Collect recent GitHub activity.

Primary source: GET /users/{username}/events (one install covers all of a
user's activity). Also ingest the Actions workflow payload so Release and
merged-PR triggers are not lost to Events API latency.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .github_client import GitHubClient, GitHubError, parse_repo_full_name_from_url
from .models import ActivityEvent, CommitSummary
from .state import parse_dt

log = logging.getLogger("github_to_linkedin.collect")

ZERO_SHA = "0" * 40


def collect_activity(
    client: GitHubClient,
    cfg: AppConfig,
    since: datetime,
) -> list[ActivityEvent]:
    username = cfg.github.username
    if not username:
        raise ValueError(
            "github.username is empty. Set it in config.yaml or GITHUB_USERNAME."
        )

    events: list[ActivityEvent] = []
    events.extend(_collect_user_events(client, username, since, cfg))
    events.extend(_collect_workflow_payload(since, cfg))
    events = _dedupe(events)
    events.sort(key=lambda e: e.created_at, reverse=True)
    log.info(
        "Collected %s unique events since %s for %s",
        len(events),
        since.isoformat(),
        username,
    )
    return events


def _collect_user_events(
    client: GitHubClient,
    username: str,
    since: datetime,
    cfg: AppConfig,
) -> list[ActivityEvent]:
    interesting = set(cfg.github.interesting_event_types)
    collected: list[ActivityEvent] = []
    stopped_early = False
    try:
        for raw in client.paginate(
            f"/users/{username}/events",
            max_pages=3,
            per_page=100,
        ):
            if not isinstance(raw, dict):
                continue
            created = parse_dt(raw.get("created_at"))
            if created is None:
                continue
            if created < since:
                stopped_early = True
                break
            event_type = str(raw.get("type") or "")
            if interesting and event_type not in interesting:
                continue
            parsed = _from_events_api(raw)
            if parsed is not None:
                collected.append(parsed)
    except GitHubError as exc:
        if exc.status == 404:
            raise GitHubError(
                f"GitHub user '{username}' not found, or the token cannot see their events.",
                status=404,
            ) from exc
        raise
    log.info(
        "Events API returned %s interesting events%s",
        len(collected),
        " (stopped at lookback boundary)" if stopped_early else "",
    )
    return collected


def _from_events_api(raw: dict[str, Any]) -> ActivityEvent | None:
    event_id = str(raw.get("id") or "")
    event_type = str(raw.get("type") or "")
    created = parse_dt(raw.get("created_at"))
    if not event_id or not event_type or created is None:
        return None

    repo_obj = raw.get("repo") or {}
    repo_name = str(repo_obj.get("name") or "")
    if not repo_name and repo_obj.get("url"):
        repo_name = parse_repo_full_name_from_url(str(repo_obj["url"]))
    actor = (raw.get("actor") or {}).get("login") or ""
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    title, body, url = _title_body_url(event_type, repo_name, payload)
    commits = _commits_from_push(payload) if event_type == "PushEvent" else []

    pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
    release = payload.get("release") if isinstance(payload.get("release"), dict) else {}

    ref = payload.get("ref")
    if event_type == "PushEvent":
        ref = payload.get("ref")
    elif event_type == "CreateEvent":
        ref = payload.get("ref")

    return ActivityEvent(
        id=event_id,
        event_type=event_type,
        created_at=created,
        repo_full_name=repo_name,
        actor_login=str(actor),
        title=title,
        body=body,
        html_url=url,
        commits=commits,
        payload=payload,
        action=str(payload.get("action") or "") or None,
        merged=bool(pr.get("merged")) if pr else False,
        ref=str(ref) if ref else None,
        before_sha=_sha(payload.get("before")),
        head_sha=_sha(payload.get("head") or payload.get("after")),
        pr_number=_int_or_none(pr.get("number") if pr else payload.get("number")),
        release_tag=str(release.get("tag_name") or "") or None,
    )


def _collect_workflow_payload(since: datetime, cfg: AppConfig) -> list[ActivityEvent]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if not event_path or not Path(event_path).exists():
        return []
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read GITHUB_EVENT_PATH (%s)", exc)
        return []
    if not isinstance(payload, dict):
        return []

    parsed: ActivityEvent | None = None
    if event_name == "release":
        parsed = _from_release_workflow(payload)
    elif event_name == "pull_request":
        parsed = _from_pull_request_workflow(payload)
    elif event_name in {"workflow_dispatch", "schedule"}:
        return []
    else:
        log.info("No extra collector for GITHUB_EVENT_NAME=%s", event_name)
        return []

    if parsed is None:
        return []
    if parsed.created_at < since:
        log.info("Workflow payload %s is older than lookback; ignoring", parsed.id)
        return []
    log.info("Ingested workflow payload as %s (%s)", parsed.event_type, parsed.id)
    return [parsed]


def _from_release_workflow(payload: dict[str, Any]) -> ActivityEvent | None:
    release = payload.get("release") or {}
    repo = payload.get("repository") or {}
    if not release or not repo:
        return None
    repo_name = str(repo.get("full_name") or "")
    tag = str(release.get("tag_name") or "")
    created = parse_dt(release.get("published_at") or release.get("created_at")) or datetime.now(
        timezone.utc
    )
    release_id = release.get("id") or tag
    return ActivityEvent(
        id=f"workflow:release:{repo_name}:{release_id}",
        event_type="ReleaseEvent",
        created_at=created,
        repo_full_name=repo_name,
        actor_login=str((payload.get("sender") or {}).get("login") or ""),
        title=str(release.get("name") or tag or "Release"),
        body=str(release.get("body") or ""),
        html_url=str(release.get("html_url") or ""),
        payload=payload,
        action=str(payload.get("action") or "published"),
        release_tag=tag or None,
        repo_private=bool(repo.get("private")),
        repo_stars=int(repo.get("stargazers_count") or 0),
        repo_forks=int(repo.get("forks_count") or 0),
        repo_description=str(repo.get("description") or ""),
        default_branch=str(repo.get("default_branch") or "") or None,
    )


def _from_pull_request_workflow(payload: dict[str, Any]) -> ActivityEvent | None:
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    if not pr or not repo:
        return None
    if payload.get("action") != "closed" or not pr.get("merged"):
        log.info("pull_request workflow event is not a merge; skipping")
        return None
    repo_name = str(repo.get("full_name") or "")
    number = pr.get("number")
    created = parse_dt(pr.get("merged_at") or pr.get("closed_at")) or datetime.now(timezone.utc)
    return ActivityEvent(
        id=f"workflow:pr:{repo_name}:{number}",
        event_type="PullRequestEvent",
        created_at=created,
        repo_full_name=repo_name,
        actor_login=str((pr.get("user") or {}).get("login") or ""),
        title=str(pr.get("title") or f"PR #{number}"),
        body=str(pr.get("body") or ""),
        html_url=str(pr.get("html_url") or ""),
        payload=payload,
        action="closed",
        merged=True,
        pr_number=_int_or_none(number),
        additions=int(pr.get("additions") or 0),
        deletions=int(pr.get("deletions") or 0),
        files_changed=int(pr.get("changed_files") or 0),
        repo_private=bool(repo.get("private")),
        repo_stars=int(repo.get("stargazers_count") or 0),
        repo_forks=int(repo.get("forks_count") or 0),
        repo_description=str(repo.get("description") or ""),
        default_branch=str(repo.get("default_branch") or "") or None,
        ref=str((pr.get("base") or {}).get("ref") or "") or None,
    )


def _title_body_url(
    event_type: str, repo_name: str, payload: dict[str, Any]
) -> tuple[str, str, str]:
    if event_type == "PullRequestEvent":
        pr = payload.get("pull_request") or {}
        title = str(pr.get("title") or f"PR #{pr.get('number', '')}")
        body = str(pr.get("body") or "")
        url = str(pr.get("html_url") or "")
        return title, body, url
    if event_type == "ReleaseEvent":
        release = payload.get("release") or {}
        title = str(release.get("name") or release.get("tag_name") or "Release")
        body = str(release.get("body") or "")
        url = str(release.get("html_url") or "")
        return title, body, url
    if event_type == "PushEvent":
        commits = payload.get("commits") or []
        messages = [str(c.get("message") or "") for c in commits if isinstance(c, dict)]
        head = messages[-1] if messages else "Push"
        title = head.split("\n", 1)[0][:200]
        body = "\n\n".join(messages)
        ref = str(payload.get("ref") or "")
        branch = ref.removeprefix("refs/heads/")
        url = f"https://github.com/{repo_name}/commits/{branch}" if repo_name else ""
        return title, body, url
    if event_type == "CreateEvent":
        ref = str(payload.get("ref") or "")
        ref_type = str(payload.get("ref_type") or "")
        body = str(payload.get("description") or "")
        if ref_type == "repository":
            title = f"Created repository {repo_name}".strip()
        else:
            title = f"Created {ref_type} {ref}".strip()
        url = f"https://github.com/{repo_name}" if repo_name else ""
        return title, body, url
    title = event_type
    return title, "", f"https://github.com/{repo_name}" if repo_name else ""


def _commits_from_push(payload: dict[str, Any]) -> list[CommitSummary]:
    out: list[CommitSummary] = []
    for raw in payload.get("commits") or []:
        if not isinstance(raw, dict):
            continue
        author = raw.get("author") or {}
        login = None
        if isinstance(author, dict):
            login = author.get("username") or author.get("login")
        out.append(
            CommitSummary(
                sha=str(raw.get("sha") or "")[:40],
                message=str(raw.get("message") or ""),
                author_login=str(login) if login else None,
            )
        )
    return out


def _dedupe(events: list[ActivityEvent]) -> list[ActivityEvent]:
    seen: set[str] = set()
    out: list[ActivityEvent] = []
    for event in events:
        if event.id in seen:
            continue
        seen.add(event.id)
        out.append(event)
    return out


def _sha(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    if text == ZERO_SHA:
        return None
    return text


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
