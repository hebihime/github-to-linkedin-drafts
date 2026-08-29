"""Collect recent GitHub activity.

Primary source: GET /users/{username}/events (one install covers all of a
user's activity). Also ingest the Actions workflow payload so Release and
merged-PR triggers are not lost to Events API latency.

GitHub documents Events API delay of up to several hours, and in practice
new public repos can be missing entirely. A second pass lists the user's
recently pushed owner repos and synthesizes CreateEvent / PushEvent from
the commits API so a ship is not dropped because the timeline is stale.
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
    events.extend(_collect_recent_repo_activity(client, username, since, cfg))
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
    newest: tuple[datetime, str, str] | None = None
    scanned = 0
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
            scanned += 1
            event_type = str(raw.get("type") or "")
            repo_name = str((raw.get("repo") or {}).get("name") or "")
            if newest is None:
                newest = (created, event_type, repo_name)
            if created < since:
                stopped_early = True
                break
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
    extra = " (stopped at lookback boundary)" if stopped_early else ""
    log.info("Events API returned %s interesting events%s", len(collected), extra)
    if newest is not None:
        created, event_type, repo_name = newest
        log.info(
            "Events API newest=%s type=%s repo=%s scanned=%s",
            created.isoformat(),
            event_type,
            repo_name or "?",
            scanned,
        )
        if created < since:
            log.info(
                "Events API is behind since=%s; scanning recently pushed repos next",
                since.isoformat(),
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


def _collect_recent_repo_activity(
    client: GitHubClient,
    username: str,
    since: datetime,
    cfg: AppConfig,
) -> list[ActivityEvent]:
    """Owner repos pushed (or created) since `since`, via the repos + commits APIs."""
    collected: list[ActivityEvent] = []
    scanned = 0
    try:
        for raw in client.paginate(
            f"/users/{username}/repos",
            params={"type": "owner", "sort": "pushed", "direction": "desc"},
            max_pages=2,
            per_page=50,
        ):
            if not isinstance(raw, dict):
                continue
            scanned += 1
            pushed_at = parse_dt(raw.get("pushed_at"))
            created_at = parse_dt(raw.get("created_at"))
            if pushed_at is not None and pushed_at < since:
                break
            parsed = _events_from_owner_repo(client, raw, username, since, cfg, created_at)
            collected.extend(parsed)
    except GitHubError as exc:
        log.warning("Recent-repo scan failed (%s); continuing with Events API results", exc)
        return collected
    log.info(
        "Recent-repo scan returned %s events from %s repos",
        len(collected),
        scanned,
    )
    return collected


def _events_from_owner_repo(
    client: GitHubClient,
    raw: dict[str, Any],
    username: str,
    since: datetime,
    cfg: AppConfig,
    created_at: datetime | None,
) -> list[ActivityEvent]:
    if raw.get("fork"):
        return []
    full_name = str(raw.get("full_name") or "")
    if not full_name:
        return []
    if raw.get("private"):
        allowed = {r.lower() for r in cfg.github.allowed_private_repos}
        if not cfg.github.include_private and full_name.lower() not in allowed:
            return []

    events: list[ActivityEvent] = []
    if created_at is not None and created_at >= since:
        created = _from_new_repo(raw, created_at)
        if created is not None:
            events.append(created)
    events.extend(_push_from_recent_commits(client, raw, username, since))
    return events


def _from_new_repo(raw: dict[str, Any], created_at: datetime) -> ActivityEvent | None:
    full_name = str(raw.get("full_name") or "")
    if not full_name:
        return None
    owner = (raw.get("owner") or {}).get("login") or ""
    description = str(raw.get("description") or "")
    return ActivityEvent(
        id=f"created-repo:{full_name.lower()}",
        event_type="CreateEvent",
        created_at=created_at,
        repo_full_name=full_name,
        actor_login=str(owner),
        title=f"Created repository {full_name}",
        body=description,
        html_url=str(raw.get("html_url") or f"https://github.com/{full_name}"),
        payload={"ref": None, "ref_type": "repository", "description": description},
        repo_private=bool(raw.get("private")),
        repo_stars=int(raw.get("stargazers_count") or 0),
        repo_forks=int(raw.get("forks_count") or 0),
        repo_description=description,
        default_branch=str(raw.get("default_branch") or "") or None,
    )


def _push_from_recent_commits(
    client: GitHubClient,
    repo: dict[str, Any],
    username: str,
    since: datetime,
) -> list[ActivityEvent]:
    full_name = str(repo.get("full_name") or "")
    default_branch = str(repo.get("default_branch") or "main")
    if not full_name:
        return []
    raw_commits: list[dict[str, Any]] = []
    since_param = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        for raw in client.paginate(
            f"/repos/{full_name}/commits",
            params={"since": since_param, "sha": default_branch},
            max_pages=2,
            per_page=100,
        ):
            if isinstance(raw, dict):
                raw_commits.append(raw)
    except GitHubError as exc:
        if exc.status in {404, 409}:
            return []
        log.warning("Commits listing failed for %s: %s", full_name, exc)
        return []
    event = push_event_from_commits(
        raw_commits,
        repo_full_name=full_name,
        username=username,
        default_branch=default_branch,
        html_url=str(repo.get("html_url") or f"https://github.com/{full_name}"),
        repo_private=bool(repo.get("private")),
        repo_stars=int(repo.get("stargazers_count") or 0),
        repo_forks=int(repo.get("forks_count") or 0),
        repo_description=str(repo.get("description") or ""),
    )
    return [event] if event is not None else []


def push_event_from_commits(
    raw_commits: list[dict[str, Any]],
    *,
    repo_full_name: str,
    username: str,
    default_branch: str,
    html_url: str,
    repo_private: bool = False,
    repo_stars: int = 0,
    repo_forks: int = 0,
    repo_description: str = "",
) -> ActivityEvent | None:
    """Build one PushEvent from commits API rows (newest first). Public for tests."""
    wanted = username.lower()
    summaries: list[tuple[CommitSummary, datetime, str | None]] = []
    for raw in raw_commits:
        parsed = _commit_from_api(raw, wanted)
        if parsed is not None:
            summaries.append(parsed)
    if not summaries:
        return None
    head_summary, head_at, _head_parent = summaries[0]
    _oldest_summary, _oldest_at, oldest_parent = summaries[-1]
    commits = [item[0] for item in summaries]
    messages = [c.message for c in reversed(commits)]
    head_msg = commits[0].message
    title = head_msg.split("\n", 1)[0][:200] if head_msg else "Push"
    return ActivityEvent(
        id=f"push:{repo_full_name.lower()}:{head_summary.sha[:40]}",
        event_type="PushEvent",
        created_at=head_at,
        repo_full_name=repo_full_name,
        actor_login=username,
        title=title,
        body="\n\n".join(messages),
        html_url=f"{html_url}/commits/{default_branch}" if html_url else "",
        commits=commits,
        payload={"ref": f"refs/heads/{default_branch}", "commits": []},
        ref=f"refs/heads/{default_branch}",
        before_sha=oldest_parent,
        head_sha=head_summary.sha[:40],
        default_branch=default_branch,
        repo_private=repo_private,
        repo_stars=repo_stars,
        repo_forks=repo_forks,
        repo_description=repo_description,
    )


def _commit_from_api(
    raw: dict[str, Any], wanted_login: str
) -> tuple[CommitSummary, datetime, str | None] | None:
    login = ""
    author = raw.get("author")
    if isinstance(author, dict):
        login = str(author.get("login") or "")
    committer = raw.get("committer")
    if not login and isinstance(committer, dict):
        login = str(committer.get("login") or "")
    if login.lower() != wanted_login:
        return None
    commit = raw.get("commit") if isinstance(raw.get("commit"), dict) else {}
    message = str(commit.get("message") or "")
    created = parse_dt((commit.get("committer") or {}).get("date") if isinstance(commit, dict) else None)
    if created is None:
        created = parse_dt((commit.get("author") or {}).get("date") if isinstance(commit, dict) else None)
    if created is None:
        return None
    parents = raw.get("parents") if isinstance(raw.get("parents"), list) else []
    parent_sha = None
    if parents and isinstance(parents[0], dict):
        parent_sha = _sha(parents[0].get("sha"))
    return (
        CommitSummary(
            sha=str(raw.get("sha") or "")[:40],
            message=message,
            author_login=login or None,
        ),
        created,
        parent_sha,
    )


def _dedupe(events: list[ActivityEvent]) -> list[ActivityEvent]:
    seen: set[str] = set()
    out: list[ActivityEvent] = []
    # Prefer GitHub Events API ids, then workflow payloads, then synthesized events.
    ordered = sorted(events, key=_source_rank)
    for event in ordered:
        keys = event.identity_keys()
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
        out.append(event)
    return out


def _source_rank(event: ActivityEvent) -> tuple[int, datetime]:
    if event.id.isdigit():
        rank = 0
    elif event.id.startswith("workflow:"):
        rank = 1
    else:
        rank = 2
    return (rank, event.created_at)


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
