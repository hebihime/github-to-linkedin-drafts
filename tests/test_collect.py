from __future__ import annotations

from src.collect import _from_events_api


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
