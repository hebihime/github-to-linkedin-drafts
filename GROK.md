# GROK.md

github-to-linkedin-drafts watches one GitHub user’s activity (via the Events API, plus Release / merged-PR workflow payloads), applies deterministic hard filters, scores what remains with a transparent feature-based function, and only then calls an LLM to write a LinkedIn draft. Default behavior is draft-only: a GitHub Issue in this repo. Auto-posting is opt-in and off.

## Architecture

```
user Events API (+ workflow payload)
        → hard filters (bots, chore/docs/ci, size, private, already-processed)
        → feature extraction (lines, files, conventional type, keywords, quality, stars, recency)
        → deterministic scorer (0–100, weights in config.yaml)
        → LLM generation IFF score ≥ draft_threshold
        → GitHub Issue draft (optional markdown / opt-in LinkedIn UGC post)
```

Pipeline files, in order: `src/collect.py` → `src/filter.py` → `src/features.py` → `src/scoring.py` → `src/generate.py` → `src/output.py`. State lives in `src/state.py`. Orchestration is `src/main.py`.

## Design decisions — do not reverse these

1. **Scoring is deterministic and completely separate from the LLM.** The model never decides whether something is worth posting. Do not add “ask the model if this is interesting” shortcuts, embeddings-as-judgment, or hidden LLM classifiers.
2. **Gemini Flash is the default LLM** (`llm.provider: gemini`, `llm.model: gemini-2.5-flash`). Free-tier Gemini is sufficient. Other providers are a config switch, not a new pipeline.
3. **Draft-only by default.** `linkedin.auto_post` must stay `false` unless the operator explicitly enables it. Do not “helpfully” turn it on.
4. **One central repository covers all of the user’s GitHub activity** through `GET /users/{username}/events`. Do not change the collector to “repos listed in this org” as the primary source.
5. **A new public repo is draft-worthy.** `CreateEvent` with `ref_type=repository` is kept and scored at `NewRepository` (default 55). Branch creates stay dropped. Do not treat “I made a repo” as noise.
6. **Keep the scoring layer transparent and tunable.** Every point must show up in the issue’s score-component table. Weights belong in `config.yaml`, not in prompt text.

## Coding standards

- Python 3.11+, type hints on public functions, dataclasses, pathlib.
- One stage per module. No I/O in `scoring.py`. No scoring inside `generate.py`.
- Logging via `logging.getLogger("github_to_linkedin.*")`. Failures should say what to fix (missing token, missing username, LLM JSON).
- Tests in `tests/` must not require network or API keys.

## Commit messages (required)

The operator does not write `feat:` / `fix:` / `perf:` by habit. **You must**, when you create the commit, because github-to-linkedin-drafts scores those prefixes. Do not ask the user to rephrase. Pick the type from the actual diff, then write a normal first line after it.

Format (Conventional Commits):

```
<type>: <imperative summary, ≥20 characters, no trailing period>

Optional body. If the change is worth a LinkedIn draft, write 80+ characters
explaining what shipped and why — not a file list.
```

Types — use the first that is true:

| Type | When | Effect on the scorer |
| --- | --- | --- |
| `feat` | New user-visible capability or API | +12, tiny diffs still allowed |
| `fix` | Bug, incorrect behavior, security hole | +8, tiny diffs still allowed |
| `perf` | Faster, cheaper, less memory, lower latency | +10, tiny diffs still allowed |
| `feat!` / `fix!` | Breaking change. Also add a `BREAKING CHANGE:` footer | type bonus **plus** +18 |
| `refactor` | Same behavior, different structure | +4 |
| `chore` `docs` `ci` `test` `style` `build` `deps` `bump` | That is *all* the commit is | **Dropped before scoring.** Correct. Do not disguise these as `feat`. |

Examples:

```
feat: add payer-keyed daily spend cap and GET /guardrails
fix: reject reused refresh tokens on the auth callback
perf: cut export p95 by batching Dapper read projections
feat!: remove v1 export path in favor of the streaming API

BREAKING CHANGE: clients must send Authorization on /export.
```

Not these:

```
Push
phase 2: payer-keyed daily cap
update
wip
feat: tweak comments          ← not a feat
feat: add github actions      ← that is ci:
```

Do not prefix every commit `feat` to game the scorer. False types pollute drafts. A lockfile bump is `chore` or `deps` and should stay invisible to LinkedIn.

## How to run locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
cp .env.example .env   # set GITHUB_TOKEN, GITHUB_USERNAME, GEMINI_API_KEY
python -m src.main --score-only -v
python -m src.main --dry-run
python -m src.main
pytest
```

## Config and secrets

Important keys in `config.yaml`: `github.username`, `github.lookback_hours`, `scoring.draft_threshold` (55), `scoring.high_confidence_threshold` (75), `scoring.*` weights, `llm.provider` / `llm.model`, `linkedin.auto_post`, `github.allowed_private_repos`.

Secrets (env, never yaml): `GEMINI_API_KEY` or `GOOGLE_API_KEY`; `GITHUB_TOKEN` / `GH_PAT`; optional `OPENAI_API_KEY`, `XAI_API_KEY`, `OPENROUTER_API_KEY`; optional `LINKEDIN_ACCESS_TOKEN` + `LINKEDIN_PERSON_URN`.

Prompts: `prompts/linkedin_system.md` and `prompts/linkedin_user.md`. Edit those files to change voice — not `generate.py`.

## What not to change lightly

- Do not move judgment back into the LLM.
- Do not enable auto-post by default.
- Do not collapse filter / features / scoring into one module.
- Do not scrape notifications, stars, or “all repos in an org” as a replacement for the user Events API.
- Do not silently lower `draft_threshold` or strip the frequency penalty to “get more posts.”
