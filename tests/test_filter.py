from __future__ import annotations

from src.filter import (
    apply_hard_filters,
    apply_size_filters,
    drop_redundant_tag_creates,
    is_breaking_message,
    parse_conventional_prefix,
)
from src.models import CommitSummary
from src.state import PipelineState
from tests.conftest import make_event


def test_parse_conventional_prefix_variants() -> None:
    assert parse_conventional_prefix("feat: add login") == "feat"
    assert parse_conventional_prefix("fix(api)!: reject empty tokens") == "fix"
    assert parse_conventional_prefix("chore: bump deps") == "chore"
    assert parse_conventional_prefix("just a title") is None


def test_is_breaking_message() -> None:
    assert is_breaking_message("feat!: remove v1 endpoint")
    assert is_breaking_message("fix: x\n\nBREAKING CHANGE: auth header required")
    assert not is_breaking_message("feat: add extra field")


def test_drops_bots(cfg) -> None:
    event = make_event(actor="dependabot[bot]", title="chore(deps): bump lodash")
    result = apply_hard_filters([event], cfg, PipelineState())
    assert result.kept == []
    assert result.dropped["bot_author"] == 1


def test_drops_already_processed(cfg) -> None:
    event = make_event(event_id="abc")
    state = PipelineState(processed_event_ids=["abc"])
    result = apply_hard_filters([event], cfg, state)
    assert result.dropped["already_processed"] == 1


def test_drops_unmerged_pr(cfg) -> None:
    event = make_event(merged=False, title="feat: not merged yet")
    result = apply_hard_filters([event], cfg, PipelineState())
    assert result.dropped["pr_not_merged"] == 1


def test_drops_private_repo_unless_allowed(cfg) -> None:
    event = make_event(private=True, repo="acme/secret")
    result = apply_hard_filters([event], cfg, PipelineState())
    assert result.dropped["private_repo"] == 1


def test_drops_chore_only_push(cfg) -> None:
    event = make_event(
        event_type="PushEvent",
        title="chore: update lockfile",
        body="",
        commits=[CommitSummary(sha="aaa", message="chore: update lockfile")],
    )
    result = apply_hard_filters([event], cfg, PipelineState())
    assert result.dropped["conventional_noise"] == 1


def test_keeps_feat_push(cfg) -> None:
    event = make_event(
        event_type="PushEvent",
        title="feat: ship export API",
        commits=[CommitSummary(sha="aaa", message="feat: ship export API")],
    )
    result = apply_hard_filters([event], cfg, PipelineState())
    assert len(result.kept) == 1


def test_size_filter_drops_tiny_non_signal_push(cfg) -> None:
    event = make_event(
        event_type="PushEvent",
        title="tweak comments in handler",
        body="",
        additions=3,
        deletions=1,
        files_changed=1,
        commits=[CommitSummary(sha="aaa", message="tweak comments in handler")],
    )
    result = apply_size_filters([event], cfg)
    assert result.dropped["too_small_lines"] == 1


def test_size_filter_keeps_small_fix(cfg) -> None:
    event = make_event(
        event_type="PushEvent",
        title="fix: prevent token replay",
        body="Reject reused refresh tokens.",
        additions=4,
        deletions=0,
        files_changed=1,
        commits=[CommitSummary(sha="aaa", message="fix: prevent token replay")],
    )
    result = apply_size_filters([event], cfg)
    assert len(result.kept) == 1


def test_size_filter_skips_releases(cfg) -> None:
    event = make_event(
        event_type="ReleaseEvent",
        title="v2.0.0",
        additions=0,
        deletions=0,
        files_changed=0,
        release_tag="v2.0.0",
    )
    assert apply_size_filters([event], cfg).kept == [event]


def test_drop_redundant_tag_creates(cfg) -> None:
    release = make_event(
        event_id="r1",
        event_type="ReleaseEvent",
        title="v1.2.0",
        release_tag="v1.2.0",
    )
    tag = make_event(
        event_id="c1",
        event_type="CreateEvent",
        title="Created tag v1.2.0",
        ref="v1.2.0",
        release_tag=None,
    )
    tag.payload = {"ref": "v1.2.0", "ref_type": "tag"}
    kept = drop_redundant_tag_creates([release, tag], cfg)
    assert [e.id for e in kept] == ["r1"]


def test_keeps_new_repository_create(cfg) -> None:
    event = make_event(
        event_id="repo1",
        event_type="CreateEvent",
        title="Created repository hebihime/x402-angular",
        body="CQRS-first restaurant ordering.",
        additions=0,
        deletions=0,
        files_changed=0,
    )
    event.payload = {"ref_type": "repository", "ref": None, "description": event.body}
    result = apply_hard_filters([event], cfg, PipelineState())
    assert [e.id for e in result.kept] == ["repo1"]


def test_drops_branch_create(cfg) -> None:
    event = make_event(
        event_id="br1",
        event_type="CreateEvent",
        title="Created branch main",
        ref="main",
        additions=0,
        files_changed=0,
    )
    event.payload = {"ref_type": "branch", "ref": "main"}
    result = apply_hard_filters([event], cfg, PipelineState())
    assert result.dropped["create_not_repo_or_tag"] == 1
