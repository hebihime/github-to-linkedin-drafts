from __future__ import annotations

from datetime import datetime, timezone

from src.collect import _dedupe, _from_events_api, _from_new_repo, push_event_from_commits
from src.models import ActivityEvent


def test_parse_push_event() -> None:
    raw = {
        "id": "123",
        "type": "PushEvent",
        "created_at": "2026-08-20T12:00:00Z",
        "actor": {"login": "jane"},
        "repo": {"name": "acme/app"},
        "payload": {
            "ref": "refs/heads/main",
            "before": "a" * 40,
            "head": "b" * 40,
            "commits": [
                {
                    "sha": "b" * 40,
                    "message": "feat: add export API\n\nStreaming download path.",
                    "author": {"username": "jane"},
                }
            ],
        },
    }
    event = _from_events_api(raw)
    assert event is not None
    assert event.event_type == "PushEvent"
    assert event.repo_full_name == "acme/app"
    assert event.title.startswith("feat: add export API")
    assert event.ref == "refs/heads/main"
    assert event.head_sha == "b" * 40
    assert len(event.commits) == 1


def test_parse_merged_pull_request_event() -> None:
    raw = {
        "id": "456",
        "type": "PullRequestEvent",
        "created_at": "2026-08-20T12:00:00Z",
        "actor": {"login": "jane"},
        "repo": {"name": "acme/app"},
        "payload": {
            "action": "closed",
            "pull_request": {
                "number": 9,
                "merged": True,
                "title": "feat: export API",
                "body": "Ships streaming export.",
                "html_url": "https://github.com/acme/app/pull/9",
                "additions": 100,
                "deletions": 4,
                "changed_files": 3,
            },
        },
    }
    event = _from_events_api(raw)
    assert event is not None
    assert event.merged is True
    assert event.pr_number == 9
    assert event.html_url.endswith("/pull/9")


def test_parse_release_event() -> None:
    raw = {
        "id": "789",
        "type": "ReleaseEvent",
        "created_at": "2026-08-20T12:00:00Z",
        "actor": {"login": "jane"},
        "repo": {"name": "acme/app"},
        "payload": {
            "action": "published",
            "release": {
                "tag_name": "v1.2.0",
                "name": "v1.2.0",
                "body": "Production launch.",
                "html_url": "https://github.com/acme/app/releases/tag/v1.2.0",
            },
        },
    }
    event = _from_events_api(raw)
    assert event is not None
    assert event.release_tag == "v1.2.0"
    assert "Production launch" in event.body


def test_parse_new_repository_create() -> None:
    raw = {
        "id": "101",
        "type": "CreateEvent",
        "created_at": "2026-08-20T12:00:00Z",
        "actor": {"login": "jane"},
        "repo": {"name": "acme/new-app"},
        "payload": {
            "ref": None,
            "ref_type": "repository",
            "description": "A CQRS kitchen dashboard.",
        },
    }
    event = _from_events_api(raw)
    assert event is not None
    assert event.title == "Created repository acme/new-app"
    assert event.body == "A CQRS kitchen dashboard."
    assert event.payload["ref_type"] == "repository"


def test_skips_malformed() -> None:
    assert _from_events_api({"type": "PushEvent"}) is None


def test_from_new_repo_is_create_event() -> None:
    created = datetime(2026, 8, 29, 2, 34, 24, tzinfo=timezone.utc)
    event = _from_new_repo(
        {
            "full_name": "hebihime/web3-restaurant-api",
            "owner": {"login": "hebihime"},
            "description": "x402 pizza demo",
            "html_url": "https://github.com/hebihime/web3-restaurant-api",
            "private": False,
            "stargazers_count": 0,
            "forks_count": 0,
            "default_branch": "main",
        },
        created,
    )
    assert event is not None
    assert event.event_type == "CreateEvent"
    assert event.payload["ref_type"] == "repository"
    assert event.id == "created-repo:hebihime/web3-restaurant-api"
    assert "created-repo:hebihime/web3-restaurant-api" in event.identity_keys()


def test_push_event_from_commits_keeps_user_commits_only() -> None:
    raw = [
        {
            "sha": "aa" * 20,
            "commit": {
                "message": "feat: show menu placeholders",
                "committer": {"date": "2026-08-29T08:58:33Z"},
            },
            "author": {"login": "hebihime"},
            "parents": [{"sha": "bb" * 20}],
        },
        {
            "sha": "cc" * 20,
            "commit": {
                "message": "chore: update linkedin-drafts state",
                "committer": {"date": "2026-08-29T08:44:08Z"},
            },
            "author": {"login": "github-actions[bot]"},
            "parents": [{"sha": "dd" * 20}],
        },
        {
            "sha": "ee" * 20,
            "commit": {
                "message": "feat: make order a normal menu",
                "committer": {"date": "2026-08-29T07:55:39Z"},
            },
            "author": {"login": "hebihime"},
            "parents": [{"sha": "ff" * 20}],
        },
    ]
    event = push_event_from_commits(
        raw,
        repo_full_name="hebihime/web3-restaurant-api",
        username="hebihime",
        default_branch="main",
        html_url="https://github.com/hebihime/web3-restaurant-api",
    )
    assert event is not None
    assert event.event_type == "PushEvent"
    assert event.head_sha == "aa" * 20
    assert event.before_sha == "ff" * 20
    assert [c.author_login for c in event.commits] == ["hebihime", "hebihime"]
    assert event.title.startswith("feat: show menu placeholders")
    assert event.id.startswith("push:hebihime/web3-restaurant-api:")
    assert event.id in event.identity_keys()


def test_dedupe_prefers_events_api_over_synthetic_push() -> None:
    created = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)
    api = ActivityEvent(
        id="555",
        event_type="PushEvent",
        created_at=created,
        repo_full_name="acme/app",
        actor_login="jane",
        title="feat: add export API",
        body="",
        html_url="https://github.com/acme/app",
        head_sha="b" * 40,
    )
    synthetic = ActivityEvent(
        id=f"push:acme/app:{'b' * 40}",
        event_type="PushEvent",
        created_at=created,
        repo_full_name="acme/app",
        actor_login="jane",
        title="feat: add export API",
        body="",
        html_url="https://github.com/acme/app",
        head_sha="b" * 40,
    )
    out = _dedupe([synthetic, api])
    assert len(out) == 1
    assert out[0].id == "555"
