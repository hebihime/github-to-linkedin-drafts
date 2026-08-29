"""CLI entry: collect → filter → features → score → (maybe) generate → output."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from .collect import collect_activity
from .config import AppConfig, load_config
from .features import enrich_diff_stats, enrich_repo_metadata, extract_features
from .filter import apply_hard_filters, apply_size_filters, drop_redundant_tag_creates
from .generate import GenerationError, generate_draft
from .github_client import GitHubClient, GitHubError, gh_username, read_token, write_token
from .output import OutputError, publish_draft
from .scoring import score_events, select_candidates
from .state import PipelineState, format_dt, load_state, locked_state, save_state, utcnow

log = logging.getLogger("github_to_linkedin")


def cli(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="github-to-linkedin",
        description="Turn high-signal GitHub activity into LinkedIn post drafts.",
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and generate, but do not create issues, post, or write state.",
    )
    parser.add_argument(
        "--score-only",
        action="store_true",
        help="Stop after scoring. No LLM call, no outputs, no state write.",
    )
    parser.add_argument("--lookback-hours", type=int, default=None)
    parser.add_argument(
        "--no-state-write",
        action="store_true",
        help="Do not persist processed event IDs (implies no last-success update).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(verbose=args.verbose or os.environ.get("LOG_LEVEL", "").upper() == "DEBUG")
    dry_run = args.dry_run or _env_flag("DRY_RUN")
    score_only = args.score_only or _env_flag("SCORE_ONLY")

    try:
        cfg = load_config(args.config)
        if args.lookback_hours is not None:
            cfg = replace(cfg, github=replace(cfg.github, lookback_hours=args.lookback_hours))
        return run(cfg, dry_run=dry_run, score_only=score_only, write_state=not args.no_state_write)
    except (FileNotFoundError, ValueError, GitHubError, GenerationError, OutputError) as exc:
        log.error("%s", exc)
        return 1


def run(
    cfg: AppConfig,
    *,
    dry_run: bool = False,
    score_only: bool = False,
    write_state: bool = True,
) -> int:
    if not cfg.github.username:
        detected = gh_username()
        if detected:
            log.info("github.username empty; using `gh` login %s", detected)
            cfg = replace(cfg, github=replace(cfg.github, username=detected))
        else:
            log.error("Set github.username in config.yaml or GITHUB_USERNAME.")
            return 1

    token = read_token()
    if not token:
        log.error(
            "No GitHub token. Log in with `gh auth login`, or set GH_PAT / GITHUB_TOKEN."
        )
        return 1

    state_path = Path(cfg.state.path)
    persist = write_state and not dry_run and not score_only

    with locked_state(state_path):
        state = load_state(state_path)
        since = _lookback_since(cfg, state)
        log.info(
            "Run start user=%s since=%s lookback_hours=%s dry_run=%s score_only=%s",
            cfg.github.username,
            format_dt(since),
            cfg.github.lookback_hours,
            dry_run,
            score_only,
        )

        read_client = GitHubClient(token)
        write_client = GitHubClient(write_token() or token)
        try:
            exit_code = _pipeline(
                cfg,
                state,
                read_client,
                write_client,
                since=since,
                dry_run=dry_run,
                score_only=score_only,
            )
        finally:
            read_client.close()
            write_client.close()

        state.last_run_at = utcnow()
        if persist and exit_code == 0:
            state.last_success_at = state.last_run_at
            save_state(state_path, state, cfg.state)
        elif not persist:
            log.info("State not written (dry-run / score-only / --no-state-write)")
        return exit_code


def _pipeline(
    cfg: AppConfig,
    state: PipelineState,
    read_client: GitHubClient,
    write_client: GitHubClient,
    *,
    since: datetime,
    dry_run: bool,
    score_only: bool,
) -> int:
    events = collect_activity(read_client, cfg, since)
    events = drop_redundant_tag_creates(events, cfg)
    collected_ids = [key for event in events for key in event.identity_keys()]

    filtered = apply_hard_filters(events, cfg, state)
    # Events API does not flag private repos or default branch; fill those first.
    enrich_repo_metadata(filtered.kept, read_client)
    after_privacy = apply_hard_filters(filtered.kept, cfg, state)
    enrich_diff_stats(after_privacy.kept, read_client)
    sized = apply_size_filters(after_privacy.kept, cfg)

    features = [extract_features(event, cfg, state) for event in sized.kept]
    scored = score_events(sized.kept, features, cfg)
    candidates = select_candidates(scored, cfg)

    # Always remember what we saw this run so retries don't double-post.
    state.mark_processed(collected_ids, cfg.state.max_processed_ids)

    if not candidates:
        log.info("Nothing to draft this run.")
        return 0

    if score_only:
        for candidate in candidates:
            log.info(
                "[score-only] %s score=%.1f %s",
                candidate.lead.event.repo_full_name,
                candidate.score,
                candidate.lead.event.title,
            )
        return 0

    produced = 0
    for candidate in candidates:
        try:
            draft = generate_draft(candidate, cfg)
        except GenerationError as exc:
            log.error("Generation failed for %s: %s", candidate.lead.event.id, exc)
            return 1
        try:
            publish_draft(draft, cfg, write_client, dry_run=dry_run)
        except OutputError as exc:
            log.error("Output failed: %s", exc)
            return 1
        produced += 1
        if draft.score >= cfg.scoring.high_confidence_threshold:
            state.last_high_score_at = utcnow()
            state.last_high_score = draft.score
        elif state.last_high_score_at is None:
            # Still advance the frequency clock so a burst of 55s doesn't flood.
            state.last_high_score_at = utcnow()
            state.last_high_score = draft.score

    log.info("Finished. drafts=%s events_seen=%s kept=%s", produced, len(events), len(sized.kept))
    return 0


def _lookback_since(cfg: AppConfig, state: PipelineState) -> datetime:
    now = utcnow()
    floor = now - timedelta(hours=cfg.github.lookback_hours)
    # Always scan the full lookback window. GitHub's Events API can lag by
    # hours; clipping to last_success_at then permanently skips those events
    # because their created_at is earlier than the empty run. Duplicates are
    # handled by processed event ids (including push/repo fingerprints).
    if state.last_success_at is not None:
        log.debug(
            "last_success_at=%s (window is lookback_hours=%s, not last success)",
            format_dt(state.last_success_at),
            cfg.github.lookback_hours,
        )
    return floor.replace(tzinfo=floor.tzinfo or timezone.utc)


def _setup_logging(*, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger("github_to_linkedin")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


if __name__ == "__main__":
    sys.exit(cli())
