from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.config import LogScaleWeight
from src.features import extract_features
from src.scoring import clamp, log_scale, score_event, select_candidates
from src.state import PipelineState
from tests.conftest import make_event


def test_log_scale_bounds() -> None:
    spec = LogScaleWeight(weight=15.0, midpoint=200.0)
    assert log_scale(0, spec) == 0.0
    assert log_scale(200, spec) == 15.0
    assert log_scale(10_000, spec) == 15.0
    assert 0 < log_scale(20, spec) < 15


def test_clamp() -> None:
    assert clamp(-4, 0, 100) == 0
    assert clamp(140, 0, 100) == 100
    assert clamp(61.2, 0, 100) == 61.2


def test_release_scores_above_draft_threshold(cfg, empty_state) -> None:
    event = make_event(
        event_type="ReleaseEvent",
        title="v2.0.0 — streaming export and a breaking auth change",
        body=(
            "Major release. New streaming export API, p95 latency down in production "
            "benchmarks, and a breaking change to the auth header. See the notes."
        ),
        additions=1800,
        deletions=220,
        files_changed=34,
        stars=1500,
        release_tag="v2.0.0",
        commits=[],
    )
    features = extract_features(event, cfg, empty_state)
    scored = score_event(event, features, cfg)
    assert scored.score >= cfg.scoring.draft_threshold
    assert scored.score >= cfg.scoring.high_confidence_threshold
    assert scored.breakdown.components["event_type"] == cfg.scoring.event_type.ReleaseEvent


def test_wip_keyword_penalizes(cfg, empty_state) -> None:
    clean = make_event(event_id="a", title="feat: add export API")
    noisy = make_event(event_id="b", title="feat: add export API (wip tmp)")
    clean_score = score_event(clean, extract_features(clean, cfg, empty_state), cfg)
    noisy_score = score_event(noisy, extract_features(noisy, cfg, empty_state), cfg)
    assert noisy_score.score < clean_score.score


def test_frequency_penalty_applies(cfg) -> None:
    now = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)
    state = PipelineState(last_high_score_at=now - timedelta(hours=2))
    event = make_event()
    features = extract_features(event, cfg, state, now=now)
    scored = score_event(event, features, cfg)
    assert scored.breakdown.components["frequency_penalty"] < 0


def test_frequency_penalty_zero_after_window(cfg) -> None:
    now = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)
    state = PipelineState(last_high_score_at=now - timedelta(hours=48))
    event = make_event()
    features = extract_features(event, cfg, state, now=now)
    scored = score_event(event, features, cfg)
    assert scored.breakdown.components["frequency_penalty"] == 0


def test_select_candidates_clusters_by_repo_and_caps(cfg, empty_state) -> None:
    lead = make_event(
        event_id="1",
        event_type="ReleaseEvent",
        title="v1.0.0 launch of the streaming export API",
        repo="acme/one",
        additions=900,
        files_changed=20,
        stars=800,
        release_tag="v1.0.0",
    )
    support = make_event(
        event_id="2",
        event_type="PullRequestEvent",
        title="feat: streaming export",
        repo="acme/one",
        additions=400,
        files_changed=9,
    )
    other = make_event(
        event_id="3",
        event_type="ReleaseEvent",
        title="v3.0.0 production launch of the billing API",
        repo="acme/two",
        additions=700,
        files_changed=16,
        stars=400,
        release_tag="v3.0.0",
    )
    scored = []
    for event in (lead, support, other):
        scored.append(score_event(event, extract_features(event, cfg, empty_state), cfg))
    cfg_one = cfg
    # max_drafts_per_run is 1 in config.yaml
    candidates = select_candidates(scored, cfg_one)
    assert len(candidates) == 1
    assert candidates[0].lead.event.event_type == "ReleaseEvent"
    assert candidates[0].lead.event.repo_full_name in {"acme/one", "acme/two"}


def test_new_repository_clears_draft_threshold(cfg, empty_state) -> None:
    event = make_event(
        event_id="new",
        event_type="CreateEvent",
        title="Created repository hebihime/x402-angular",
        body="",
        additions=0,
        deletions=0,
        files_changed=0,
        stars=0,
        commits=[],
    )
    event.payload = {"ref_type": "repository", "description": ""}
    scored = score_event(event, extract_features(event, cfg, empty_state), cfg)
    assert scored.breakdown.components["event_type"] == cfg.scoring.event_type.NewRepository
    assert scored.score >= cfg.scoring.draft_threshold


def test_scoring_is_deterministic(cfg, empty_state) -> None:
    event = make_event()
    a = score_event(event, extract_features(event, cfg, empty_state), cfg)
    b = score_event(event, extract_features(event, cfg, empty_state), cfg)
    assert a.score == b.score
    assert a.breakdown.components == b.breakdown.components
