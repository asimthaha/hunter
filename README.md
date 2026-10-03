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

## Tests
`pytest` (offline). `HUB_LIVE=1 pytest -m live` runs the golden-set recall check against the real API.

## Scheduling
Task Scheduler: action `.venv\Scripts\python.exe -m src.cli run`, start in the repo folder; enable
"Run task as soon as possible after a scheduled start is missed". `.env` is loaded explicitly by code.
