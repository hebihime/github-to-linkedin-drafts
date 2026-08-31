"""Load and validate config.yaml with environment-variable overlays."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class GitHubConfig:
    username: str
    lookback_hours: int = 48
    include_private: bool = False
    allowed_private_repos: tuple[str, ...] = ()
    output_repo: str = ""  # owner/name; empty = GITHUB_REPOSITORY / infer
    only_default_branch_pushes: bool = True
    interesting_event_types: tuple[str, ...] = (
        "PushEvent",
        "PullRequestEvent",
        "ReleaseEvent",
        "CreateEvent",
    )


@dataclass(frozen=True)
class FilterConfig:
    bot_authors: tuple[str, ...] = (
        "dependabot",
        "dependabot[bot]",
        "renovate",
        "renovate[bot]",
        "github-actions",
        "github-actions[bot]",
        "greenkeeper",
        "greenkeeper[bot]",
        "imgbot",
        "imgbot[bot]",
        "snyk-bot",
        "codecov",
        "pre-commit-ci",
        "pre-commit-ci[bot]",
        "mergify",
        "mergify[bot]",
        "semantic-release-bot",
    )
    drop_commit_types: tuple[str, ...] = (
        "chore",
        "docs",
        "ci",
        "test",
        "style",
        "build",
        "deps",
        "bump",
    )
    min_lines_changed: int = 15
    min_files_changed: int = 1
    min_lines_for_signal_types: int = 1
    drop_prereleases: bool = False
    drop_tag_creates_if_release_exists: bool = True


@dataclass(frozen=True)
class EventTypeWeights:
    ReleaseEvent: float = 40.0
    PullRequestEvent: float = 30.0
    PushEvent: float = 18.0
    CreateEvent: float = 10.0  # tags
    NewRepository: float = 55.0  # CreateEvent ref_type=repository; clears draft threshold
    default: float = 5.0


@dataclass(frozen=True)
class LogScaleWeight:
    weight: float
    midpoint: float  # value at which the component reaches `weight`


@dataclass(frozen=True)
class ConventionalWeights:
    feat: float = 12.0
    fix: float = 8.0
    perf: float = 10.0
    breaking: float = 18.0
    refactor: float = 4.0


@dataclass(frozen=True)
class KeywordWeights:
    positive: tuple[str, ...] = (
        "launch",
        "release",
        "shipped",
        "shipping",
        "performance",
        "security",
        "breaking",
        "api",
        "feature",
        "milestone",
        "production",
        "opensource",
        "open-source",
        "open source",
        "rfc",
        "benchmark",
        "latency",
        "throughput",
        "migration",
        "announcing",
    )
    negative: tuple[str, ...] = (
        "wip",
        "tmp",
        "temp",
        "typo",
        "formatting",
        "lint",
        "rebase",
        "trivial",
        "nit",
        "bump version",
        "bump the",
        "lockfile",
        "gitignore",
    )
    positive_weight: float = 3.0
    negative_weight: float = -8.0
    positive_cap: float = 9.0


@dataclass(frozen=True)
class QualityWeights:
    title_min_chars: int = 20
    body_min_chars: int = 80
    title_bonus: float = 4.0
    body_bonus: float = 6.0
    weak_titles: tuple[str, ...] = (
        "update",
        "updates",
        "fix",
        "fixes",
        "wip",
        "misc",
        "changes",
        "minor",
    )


@dataclass(frozen=True)
class FrequencyPenalty:
    hours_window: float = 24.0
    max_penalty: float = 15.0


@dataclass(frozen=True)
class ScoringConfig:
    event_type: EventTypeWeights = field(default_factory=EventTypeWeights)
    lines_changed: LogScaleWeight = field(
        default_factory=lambda: LogScaleWeight(weight=15.0, midpoint=200.0)
    )
    files_changed: LogScaleWeight = field(
        default_factory=lambda: LogScaleWeight(weight=8.0, midpoint=10.0)
    )
    conventional: ConventionalWeights = field(default_factory=ConventionalWeights)
    keywords: KeywordWeights = field(default_factory=KeywordWeights)
    quality: QualityWeights = field(default_factory=QualityWeights)
    repo_popularity: LogScaleWeight = field(
        default_factory=lambda: LogScaleWeight(weight=10.0, midpoint=500.0)
    )
    frequency_penalty: FrequencyPenalty = field(default_factory=FrequencyPenalty)
    non_default_branch_penalty: float = 6.0
    draft_threshold: float = 55.0
    high_confidence_threshold: float = 75.0


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "gemini"
    model: str = "gemini-3.7-flash"
    temperature: float = 0.65
    max_output_tokens: int = 8192
    thinking_level: str = "medium"  # Gemini 3.x: low | medium | high; empty = SDK default
    base_url: str = ""
    api_key_env: str = ""
    system_prompt_path: str = "prompts/linkedin_system.md"
    user_prompt_path: str = "prompts/linkedin_user.md"


@dataclass(frozen=True)
class OutputConfig:
    create_github_issue: bool = True
    issue_labels: tuple[str, ...] = ("linkedin-draft",)
    write_markdown: bool = False
    markdown_dir: str = "drafts"


@dataclass(frozen=True)
class LinkedInConfig:
    auto_post: bool = False
    person_urn: str = ""  # urn:li:person:...  (optional; resolved from token if empty)


@dataclass(frozen=True)
class StateConfig:
    path: str = ".github-to-linkedin-state.json"
    max_processed_ids: int = 2000
    commit_in_ci: bool = True


@dataclass(frozen=True)
class AppConfig:
    github: GitHubConfig
    filters: FilterConfig = field(default_factory=FilterConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    linkedin: LinkedInConfig = field(default_factory=LinkedInConfig)
    state: StateConfig = field(default_factory=StateConfig)


def default_config_path() -> Path:
    env_path = os.environ.get("CONFIG_PATH")
    if env_path:
        return Path(env_path)
    cwd = Path.cwd() / "config.yaml"
    if cwd.exists():
        return cwd
    return Path(__file__).resolve().parent.parent / "config.yaml"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or default_config_path()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. Copy config.yaml and set github.username."
        )
    with config_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config.yaml must be a mapping, got {type(raw).__name__}")
    return _parse_config(raw)


def _parse_config(raw: dict[str, Any]) -> AppConfig:
    gh = raw.get("github") or {}
    username = (
        str(gh.get("username") or "").strip()
        or os.environ.get("GITHUB_USERNAME", "").strip()
        or os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    )
    lookback = _first_int(os.environ.get("LOOKBACK_HOURS"), gh.get("lookback_hours"), 48)
    github = GitHubConfig(
        username=username,
        lookback_hours=lookback,
        include_private=bool(gh.get("include_private", False)),
        allowed_private_repos=_str_tuple(gh.get("allowed_private_repos")),
        output_repo=str(gh.get("output_repo") or os.environ.get("GITHUB_REPOSITORY") or ""),
        only_default_branch_pushes=bool(gh.get("only_default_branch_pushes", True)),
        interesting_event_types=_str_tuple(
            gh.get("interesting_event_types"),
            GitHubConfig.interesting_event_types,
        ),
    )

    fl = raw.get("filters") or {}
    filters = FilterConfig(
        bot_authors=_str_tuple(fl.get("bot_authors"), FilterConfig.bot_authors),
        drop_commit_types=_str_tuple(fl.get("drop_commit_types"), FilterConfig.drop_commit_types),
        min_lines_changed=int(fl.get("min_lines_changed", 15)),
        min_files_changed=int(fl.get("min_files_changed", 1)),
        min_lines_for_signal_types=int(fl.get("min_lines_for_signal_types", 1)),
        drop_prereleases=bool(fl.get("drop_prereleases", False)),
        drop_tag_creates_if_release_exists=bool(
            fl.get("drop_tag_creates_if_release_exists", True)
        ),
    )

    sc = raw.get("scoring") or {}
    et = sc.get("event_type") or {}
    lines = sc.get("lines_changed") or {}
    files = sc.get("files_changed") or {}
    conv = sc.get("conventional") or {}
    kw = sc.get("keywords") or {}
    q = sc.get("quality") or {}
    pop = sc.get("repo_popularity") or {}
    freq = sc.get("frequency_penalty") or {}
    scoring = ScoringConfig(
        event_type=EventTypeWeights(
            ReleaseEvent=float(et.get("ReleaseEvent", 40.0)),
            PullRequestEvent=float(et.get("PullRequestEvent", 30.0)),
            PushEvent=float(et.get("PushEvent", 18.0)),
            CreateEvent=float(et.get("CreateEvent", 10.0)),
            NewRepository=float(et.get("NewRepository", 55.0)),
            default=float(et.get("default", 5.0)),
        ),
        lines_changed=LogScaleWeight(
            weight=float(lines.get("weight", 15.0)),
            midpoint=float(lines.get("midpoint", 200.0)),
        ),
        files_changed=LogScaleWeight(
            weight=float(files.get("weight", 8.0)),
            midpoint=float(files.get("midpoint", 10.0)),
        ),
        conventional=ConventionalWeights(
            feat=float(conv.get("feat", 12.0)),
            fix=float(conv.get("fix", 8.0)),
            perf=float(conv.get("perf", 10.0)),
            breaking=float(conv.get("breaking", 18.0)),
            refactor=float(conv.get("refactor", 4.0)),
        ),
        keywords=KeywordWeights(
            positive=_str_tuple(kw.get("positive"), KeywordWeights.positive),
            negative=_str_tuple(kw.get("negative"), KeywordWeights.negative),
            positive_weight=float(kw.get("positive_weight", 3.0)),
            negative_weight=float(kw.get("negative_weight", -8.0)),
            positive_cap=float(kw.get("positive_cap", 9.0)),
        ),
        quality=QualityWeights(
            title_min_chars=int(q.get("title_min_chars", 20)),
            body_min_chars=int(q.get("body_min_chars", 80)),
            title_bonus=float(q.get("title_bonus", 4.0)),
            body_bonus=float(q.get("body_bonus", 6.0)),
            weak_titles=_str_tuple(q.get("weak_titles"), QualityWeights.weak_titles),
        ),
        repo_popularity=LogScaleWeight(
            weight=float(pop.get("weight", 10.0)),
            midpoint=float(pop.get("midpoint", 500.0)),
        ),
        frequency_penalty=FrequencyPenalty(
            hours_window=float(freq.get("hours_window", 24.0)),
            max_penalty=float(freq.get("max_penalty", 15.0)),
        ),
        non_default_branch_penalty=float(sc.get("non_default_branch_penalty", 6.0)),
        draft_threshold=float(sc.get("draft_threshold", 55.0)),
        high_confidence_threshold=float(sc.get("high_confidence_threshold", 75.0)),
    )

    llm_raw = raw.get("llm") or {}
    llm = LLMConfig(
        provider=str(llm_raw.get("provider") or "gemini").strip().lower(),
        model=str(llm_raw.get("model") or "gemini-3.7-flash").strip(),
        temperature=float(llm_raw.get("temperature", 0.65)),
        max_output_tokens=int(llm_raw.get("max_output_tokens", 8192)),
        thinking_level=str(llm_raw.get("thinking_level") or "medium").strip().lower(),
        base_url=str(llm_raw.get("base_url") or ""),
        api_key_env=str(llm_raw.get("api_key_env") or ""),
        system_prompt_path=str(llm_raw.get("system_prompt_path") or "prompts/linkedin_system.md"),
        user_prompt_path=str(llm_raw.get("user_prompt_path") or "prompts/linkedin_user.md"),
    )

    out = raw.get("output") or {}
    output = OutputConfig(
        create_github_issue=bool(out.get("create_github_issue", True)),
        issue_labels=_str_tuple(out.get("issue_labels"), OutputConfig.issue_labels),
        write_markdown=bool(out.get("write_markdown", False)),
        markdown_dir=str(out.get("markdown_dir") or "drafts"),
    )

    li = raw.get("linkedin") or {}
    auto_post_env = os.environ.get("LINKEDIN_AUTO_POST", "").strip().lower()
    auto_post = bool(li.get("auto_post", False))
    if auto_post_env in {"1", "true", "yes"}:
        auto_post = True
    if auto_post_env in {"0", "false", "no"}:
        auto_post = False
    linkedin = LinkedInConfig(
        auto_post=auto_post,
        person_urn=str(li.get("person_urn") or os.environ.get("LINKEDIN_PERSON_URN") or ""),
    )

    st = raw.get("state") or {}
    state = StateConfig(
        path=str(st.get("path") or ".github-to-linkedin-state.json"),
        max_processed_ids=int(st.get("max_processed_ids", 2000)),
        commit_in_ci=bool(st.get("commit_in_ci", True)),
    )

    return AppConfig(
        github=github,
        filters=filters,
        scoring=scoring,
        llm=llm,
        output=output,
        linkedin=linkedin,
        state=state,
    )


def _str_tuple(value: Any, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _first_int(*candidates: Any) -> int:
    for c in candidates:
        if c is None or c == "":
            continue
        return int(c)
    raise ValueError("no integer value provided")
