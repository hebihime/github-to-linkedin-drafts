# github-to-linkedin-drafts

Turn high-signal GitHub activity into LinkedIn post drafts.

A scheduled GitHub Action (or a local CLI) reads **your** recent GitHub events — across every repository you push to, not just this one — and runs a **deterministic scorer**. Only activity that clears a threshold is sent to an LLM, which writes a first-person post. The default output is a GitHub Issue in this repo. Nothing is published to LinkedIn unless you opt in.

```
GitHub Events API  →  hard filters  →  feature scoring (0–100)  →  LLM (only if ≥ 55)  →  Issue draft
```

The LLM does not decide whether something is worth posting. It only writes prose.

## Why one repo is enough

GitHub’s [user Events API](https://docs.github.com/en/rest/activity/events) (`GET /users/{username}/events`) returns *your* recent activity across public repos, and private ones too if the token belongs to you. Install this project once, set `github.username` to your login, and it covers everything you ship.

The workflow also listens for `release` published and `pull_request` closed (merged) on **this** repository, so those triggers are not lost to Events API latency. They are extra, not the primary source.

Events older than ~30 days, or beyond the most recent 300, are not available from GitHub. The default lookback is 48 hours. Processed event IDs (and push/repo fingerprints) prevent double-drafts; the window is **not** clipped to last success, because the Events API can lag by hours and a new public repo may be missing from the timeline entirely. A second pass lists recently pushed owner repos and reads their commits.

## Quick start (GitHub Actions)

1. Fork or copy this repo (keep it private if you want).
2. Set `github.username` in [`config.yaml`](config.yaml) to your GitHub login.
3. Repo **Settings → Secrets and variables → Actions**, add:
   - `GEMINI_API_KEY` — free key from [Google AI Studio](https://aistudio.google.com/apikey)
   - Optional `GH_PAT` — a personal access token, if you want private events (see [Tokens](#tokens-and-permissions))
4. Enable Actions. Run **GitHub to LinkedIn Drafts** via *Run workflow*, or wait for the daily schedule.
5. When something scores ≥ 55, an issue titled `LinkedIn Draft – YYYY-MM-DD – score XX` appears. Edit, copy, post.

Required workflow permissions (already set in the YAML): `contents: write` (state file) and `issues: write` (drafts).

## How to get a free Gemini API key

1. Open [Google AI Studio](https://aistudio.google.com/apikey) and sign in with a Google account.
2. Create an API key. The free tier is enough for this project (one short JSON generation on days you actually shipped).
3. Store it as `GEMINI_API_KEY` (Actions secret, or `.env` locally). `GOOGLE_API_KEY` is accepted as an alias.

Default model: `gemini-3.7-flash` (configured in `config.yaml`), with `thinking_level: medium`. Gemini 3.7 rejects `thinking_budget=0` / `minimal`. Older Flash ids still work if you pin `llm.model`.

## Run locally

Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: GITHUB_TOKEN, GITHUB_USERNAME, GEMINI_API_KEY
```

```bash
# Score recent activity; no LLM, no issues, no state write
python -m src.main --score-only -v

# Call Gemini, print the draft, do not create an issue
python -m src.main --dry-run

# Full run (creates an issue in github.output_repo / GITHUB_REPOSITORY)
python -m src.main
```

`scripts/run_local.sh` loads `.env` and forwards extra args:

```bash
./scripts/run_local.sh --score-only -v
./scripts/run_local.sh --lookback-hours 72 --dry-run
```

A GitHub PAT (`repo` for private activity, or public-only access plus `issues` on this repo) plus a Gemini key is enough to dogfood the full path.

## Pipeline

| Stage | Module | What it does |
| --- | --- | --- |
| Collect | `src/collect.py` | User Events API, paginated, rate-limit aware. Also reads `GITHUB_EVENT_PATH` for Release / merged PR. |
| Hard filters | `src/filter.py` | Bots, `chore`/`docs`/`ci`/`test`/`style`/`build`/`deps`/`bump`, tiny diffs, private repos, already-processed IDs. **No LLM.** |
| Features | `src/features.py` | Lines/files (compare API), conventional-commit type, keywords, title/body quality, stars/forks, recency. |
| Score | `src/scoring.py` | Weighted sum, clamped to 0–100. Pure function of features + `config.yaml`. |
| Generate | `src/generate.py` | Called only if score ≥ `draft_threshold`. Gemini by default. |
| Output | `src/output.py` | GitHub Issue, optional markdown file, optional LinkedIn UGC post. |
| State | `src/state.py` | JSON file of processed event IDs + timestamps. File lock against concurrent runs. |

### Scoring (defaults)

Components are added, then clamped to `[0, 100]`. Log-scaled terms reach full weight at a configurable midpoint.

| Component | Default | Notes |
| --- | --- | --- |
| Event type | Release 40 · merged PR 30 · **new public repo 55** · push 18 · tag 10 | New repos draft by default; branch creates are dropped |
| Lines changed | 15 at ~200 lines (log) | |
| Files changed | 8 at ~10 files (log) | |
| Conventional type | feat 12 · fix 8 · perf 10 · breaking 18 · refactor 4 | Breaking stacks with the type |
| Keywords | +3 each (cap 9) · −8 each negative | Lists in `config.yaml` |
| Title / body quality | +4 / +6 | Length + not a weak title like “update” |
| Repo stars | 10 at ~500 stars (log) | |
| Frequency penalty | up to −15 inside 24h of last draft | Stops a ship-day flood |
| Non-default branch | −6 | Push to `main`/`master` preferred |

Thresholds: **55** draft generation, **75** high confidence (shown on the issue). One draft per run by default (`output.max_drafts_per_run`), clustered by repo.

## How to tune the scorer

Edit [`config.yaml`](config.yaml) under `scoring`. You do not need to touch Python.

- **Too few drafts:** lower `draft_threshold` (try 45), lower `min_lines_changed`, or raise event-type weights. Run `--score-only -v` and read the `score=` lines.
- **Too many drafts:** raise `draft_threshold`, raise `frequency_penalty.max_penalty`, or add negative keywords.
- **Releases always, pushes rarely:** that is already the default shape. Increase `event_type.ReleaseEvent` or cut `event_type.PushEvent`.
- **Small security fixes should count:** they already skip the large-diff floor when the conventional type is `feat` / `fix` / `perf` / breaking (`min_lines_for_signal_types: 1`).
- After changing weights, `pytest tests/test_scoring.py` still checks that a fat release clears 75 and that scoring is deterministic.

Every issue includes a **score components** table. Tune against real drafts, not guesses.

## How to change the LinkedIn prompt

The model is a ghostwriter, not a judge. Voice lives in markdown, not code:

- [`prompts/linkedin_system.md`](prompts/linkedin_system.md) — persona, constraints, banned phrases, JSON shape
- [`prompts/linkedin_user.md`](prompts/linkedin_user.md) — the brief (score, events, features, README)

Paths are `llm.system_prompt_path` / `llm.user_prompt_path` in config. Keep the JSON contract (`post_text`, `reasoning`) or update `src/generate.py`’s parser to match.

Target: 150–350 words, first person, **thesis-first** (especially for new public repos). The README is fetched after scoring and attached only to the generation brief so the model can explain why the project exists instead of concatenating keywords. Scoring still never sees the README.

If a draft reads like a Wikipedia intro to a protocol named in the repo, the prompt — not the scorer — is wrong.

## How to switch LLM providers

`config.yaml`:

```yaml
llm:
  provider: gemini          # gemini | openai | grok | openrouter | openai_compatible
  model: gemini-3.7-flash   # change this when you change provider
```

| `provider` | Env var | Default base URL |
| --- | --- | --- |
| `gemini` (default) | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Google Gen AI SDK (`google-genai`) |
| `openai` | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| `grok` | `XAI_API_KEY` | `https://api.x.ai/v1` |
| `openrouter` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` |
| `openai_compatible` | `llm.api_key_env` | `llm.base_url` (required) |

Example — Grok:

```yaml
llm:
  provider: grok
  model: grok-4
```

```bash
export XAI_API_KEY=...
```

Example — any OpenAI-compatible proxy:

```yaml
llm:
  provider: openai_compatible
  model: your-model-id
  base_url: https://your-proxy.example/v1
  api_key_env: YOUR_API_KEY
```

Always change `llm.model` when you change `llm.provider`. A Gemini model id against Grok’s API will fail.

## Output

Primary: a GitHub Issue in `github.output_repo` (or `GITHUB_REPOSITORY` in Actions).

- Title: `LinkedIn Draft – YYYY-MM-DD – score XX`
- Body: full post, score, source events, feature table, component breakdown, reasoning
- Label: `linkedin-draft`

Optional markdown files: set `output.write_markdown: true` (`drafts/` is gitignored).

### Auto-post to LinkedIn (opt-in, off by default)

1. Create a LinkedIn app, request `w_member_social`, complete OAuth, get a member access token.
2. Set `LINKEDIN_ACCESS_TOKEN` (and optionally `LINKEDIN_PERSON_URN`).
3. Set `linkedin.auto_post: true` in `config.yaml`.

This uses the [UGC Posts API](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin) (`POST https://api.linkedin.com/v2/ugcPosts`). Leave it off until you have read a few drafts and trust the scorer. There is no undo.

## Tokens and permissions

| Secret / env | Used for |
| --- | --- |
| `GITHUB_TOKEN` (Actions built-in) | Open issues in this repo; public Events API |
| `GH_PAT` | User Events API **including private events**. Classic PAT: `repo`. Fine-grained: Events read + access to those repos |
| `GEMINI_API_KEY` | Draft generation |
| `LINKEDIN_ACCESS_TOKEN` | Only if auto-post is enabled |

Locally, a single PAT in `GITHUB_TOKEN` is enough if it can read your events and open issues on the output repo.

Private repos are dropped unless `github.include_private: true` or the repo is listed in `github.allowed_private_repos`.

## State

`.github-to-linkedin-state.json` stores processed event IDs and the last-success / last-high-score timestamps so the same push is never drafted twice. The Action commits it back to the repo. Concurrent runs take a file lock.

To replay a window: delete IDs from the file (or the file itself) and run with `--lookback-hours`. `--dry-run` and `--score-only` do not write state.

## Development

```bash
pip install -r requirements.txt pytest
pytest
```

No network and no API keys are required for tests.

## Configuration reference

See [`config.yaml`](config.yaml) for the full set. High-signal keys:

- `github.username` / `github.lookback_hours` / `github.include_private`
- `filters.min_lines_changed` / `filters.drop_commit_types` / `filters.bot_authors`
- `scoring.draft_threshold` / `scoring.high_confidence_threshold` / `scoring.event_type.*`
- `llm.provider` / `llm.model` / prompt paths
- `output.max_drafts_per_run` / `output.create_github_issue`
- `linkedin.auto_post` (keep `false` unless you mean it)

CLI flags: `--config`, `--dry-run`, `--score-only`, `--lookback-hours`, `--no-state-write`, `-v`.

Env overlays: `GITHUB_USERNAME`, `LOOKBACK_HOURS`, `CONFIG_PATH`, `DRY_RUN`, `SCORE_ONLY`, `LINKEDIN_AUTO_POST`.

## License

MIT
