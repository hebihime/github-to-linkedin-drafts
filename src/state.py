"""JSON state: processed event IDs, last-success timestamp, high-score clock.

A lock file makes concurrent runs safe (Actions + a local run, overlapping
schedules). Processed IDs are capped so the file stays small enough to commit.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import StateConfig

log = logging.getLogger("github_to_linkedin.state")

STATE_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class PipelineState:
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_high_score_at: datetime | None = None
    last_high_score: float | None = None
    processed_event_ids: list[str] = field(default_factory=list)
    version: int = STATE_VERSION

    def is_processed(self, event_id: str) -> bool:
        return event_id in self._id_set

    def mark_processed(self, event_ids: list[str], max_ids: int) -> None:
        for eid in event_ids:
            if eid not in self._id_set:
                self.processed_event_ids.append(eid)
                self._id_set.add(eid)
        overflow = len(self.processed_event_ids) - max_ids
        if overflow > 0:
            dropped = self.processed_event_ids[:overflow]
            self.processed_event_ids = self.processed_event_ids[overflow:]
            for eid in dropped:
                self._id_set.discard(eid)

    def __post_init__(self) -> None:
        self._id_set: set[str] = set(self.processed_event_ids)


def load_state(path: Path) -> PipelineState:
    if not path.exists():
        log.info("No state file at %s; starting fresh", path)
        return PipelineState()
    try:
        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to read state file %s (%s); starting fresh", path, exc)
        return PipelineState()
    if not isinstance(raw, dict):
        log.warning("State file %s is not an object; starting fresh", path)
        return PipelineState()
    return PipelineState(
        last_run_at=parse_dt(raw.get("last_run_at")),
        last_success_at=parse_dt(raw.get("last_success_at")),
        last_high_score_at=parse_dt(raw.get("last_high_score_at")),
        last_high_score=_opt_float(raw.get("last_high_score")),
        processed_event_ids=[str(x) for x in raw.get("processed_event_ids") or []],
        version=int(raw.get("version") or STATE_VERSION),
    )


def save_state(path: Path, state: PipelineState, cfg: StateConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": STATE_VERSION,
        "last_run_at": format_dt(state.last_run_at),
        "last_success_at": format_dt(state.last_success_at),
        "last_high_score_at": format_dt(state.last_high_score_at),
        "last_high_score": state.last_high_score,
        "processed_event_ids": state.processed_event_ids[-cfg.max_processed_ids :],
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    log.info(
        "Wrote state to %s (%s processed ids)",
        path,
        len(payload["processed_event_ids"]),
    )


@contextmanager
def locked_state(path: Path) -> Iterator[None]:
    """Exclusive lock around a state read-modify-write cycle."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        _lock_exclusive(fd)
        yield
    finally:
        _unlock(fd)
        os.close(fd)


def _lock_exclusive(fd: int) -> None:
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
    except ImportError:
        # Windows: best-effort; GitHub Actions and local macOS/Linux use fcntl.
        pass


def _unlock(fd: int) -> None:
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except ImportError:
        pass


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
