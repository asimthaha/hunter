# Career Command Hub

Local, AI-assisted job search pipeline. Design and rules: `plan.md` (source of truth) and `.claude/CLAUDE.md`.

## Setup (Windows, Python 3.11+)
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
copy .env.example .env          # add ANTHROPIC_API_KEY (the pipeline key, not your Claude Code key)
.venv\Scripts\python -m src.cli init   # creates data\*.yaml from the *.example.yaml files — edit them
```
Fill in `data/facts.yaml`, `contact.yaml`, `profile.yaml`, `preferences.yaml`, `companies.yaml`.
These are git-ignored; never commit them.

## Daily use
```
hub run                 # ingest -> rules -> eligibility (Haiku) -> evaluate (Sonnet) -> digest
hub ui                  # http://127.0.0.1:8765  Inbox / Job / Tracker / Proposals / Health
hub review              # same inbox in the terminal
hub tailor <id>         # resume PDF for a shortlisted job (IDs only; validated)
hub draft <id> -q "..." # grounded referral note / screening answers
hub applied <id>        # after YOU submit; sets follow-up date
hub audit               # weekly sample of rejected jobs
hub calibrate           # precision/recall on tests/golden/ (needs >=50 labelled postings)
```
Without installing the package, use `.venv\Scripts\python -m src.cli <command>` (or `pip install -e .` for `hub`).

## Caching and API usage
Three caches cut cost: Anthropic prompt caching (fact bank shared by evaluate/tailor/drafts), a local response
cache (identical request = no API call), and an HTTP cache for job sources. See what they save with:
```
python -m src.cli usage            # tokens, estimated cost, cache hit rates, warnings (--days N)
python -m src.cli cache clear      # wipe local response + HTTP caches   (cache prune: drop old entries)
python -m src.cli calibrate --fresh   # bypass the response cache when measuring the model itself
```
Tunables are in `src/config.py` (`CACHE_TTL`, `LLM_CACHE_ENABLED`, `HTTP_CACHE_FRESH_SECONDS`).
Set `HUB_LLM_CACHE=0` to disable the response cache for one run.

## Job sources
`hub run` fetches Infopark (Kochi) and Technopark (Trivandrum) first, then the company watchlist and remote boards.
Fetch just one with `python -m src.cli run --only technopark` (or edit `priority_sources` in `data/preferences.yaml`).

## Tests
`pytest` (offline). `HUB_LIVE=1 pytest -m live` runs the golden-set recall check against the real API.

## Scheduling
Task Scheduler: action `.venv\Scripts\python.exe -m src.cli run`, start in the repo folder; enable
"Run task as soon as possible after a scheduled start is missed". `.env` is loaded explicitly by code.
