# Local Career Command Hub — Build Plan

Revised plan for a local, AI-assisted job search and application pipeline, trimmed from an
original 7-phase / 30-day design down to a 4-week part-time build with weekly go/no-go
checkpoints. Windows laptop, Python only, no Node.

**Status:** ready to build once target roles/stack/seniority are filled in below.

**Revision 2 (2026-10-03):** added a single job state machine (§4), failure handling and
observability (§8), security hardening (§12), India-specific fields, and resolved
contradictions between the anti-fabrication principle and free-text drafting.

---

## 0. Open input (blocks Week 1)

```
Target roles:        <TBD>
Stack / seniority:   <TBD>
Relocation:          open to / not open to <TBD countries>
Priority bucket:     india-based | remote-for-foreign-employer | relocation  (rank order)
Minimum acceptable pay: <TBD>  (per bucket, with currency: INR LPA / USD per year / etc.)
Notice period:       <TBD>  (immediate / serving notice until <date> / N days)
Current CTC:         <TBD>  (local only — see §3)
Expected CTC:        <TBD>  (local only — see §3)
```

This decides the rubric weights, the watchlist, and how the fact bank bullets are written.
Everything below assumes this gets filled in before Week 1 starts.

---

## 1. Principles (what makes this version different from the original)

- **Outcome metrics, not system metrics.** Success is measured by jobs surfaced, % applied to,
  score-vs-your-judgment agreement, and callback rate — not RAM usage or page load time.
- **Nothing acts without you** on anything irreversible or platform-risky: submitting an
  application, sending a LinkedIn message, adding a claim to the fact bank, answering
  visa/salary/legal questions.
- **Fabrication is prevented structurally, not by instruction.** The model selects bullet IDs
  from a pre-approved fact bank; code assembles the resume; a validator rejects anything that
  isn't in the bank. This applies to **every** piece of text about you — including screening
  answer drafts and referral notes (see §7), not just the resume.
- **Contact details never leave your machine.** Split from the fact bank entirely, and enforced
  in code by a single API wrapper (see §3).
- **Stable sources over scraping.** ATS JSON endpoints and remote-board APIs lead; JobSpy/Naukri
  are a slower, secondary source.
- **Deterministic pipeline, not a planning agent.** The sequence of steps is already known
  (ingest → filter → score → shortlist → tailor → render → notify → apply → log), so a fixed
  script with LLM judgment calls at specific points is more predictable and cheaper than an
  open-ended agent deciding its own next action.
- **LLM output has bounded power.** The model never has tools with side effects. Every response
  is schema-constrained JSON with enums where possible, and hard rules (scam checks, pay floor)
  run in code where the model can't override them.
- **Nothing disappears silently.** Every job that is filtered out or rejected keeps a recorded
  reason, and a sample of rejections is reviewed weekly (see §8).

---

## 2. Repo structure

```
career-hub/
  .gitignore              # see §5 — must exist before the first real data is created
  .env.example            # variable names only, no values
  data/
    facts.yaml            # fact bank sent to the API — experience, skills, bullet variants, no PII
    contact.yaml          # phone, email, address, LinkedIn/GitHub URLs — local only
    profile.yaml          # min pay, current/expected CTC, notice period — local only
    companies.yaml        # watchlist: company + canonical name + ATS type + board token
    jobs.db               # SQLite: jobs, scores, events, approvals
  prompts/
    eligibility.md
    evaluate.md
    tailor.md
    drafts.md             # screening answers + referral notes (grounded, see §7)
  src/
    db.py                 # schema, migrations, state-transition helper (the only writer of status)
    llm.py                # the ONLY module that calls the Anthropic API — PII guard lives here
    ingest.py
    rules.py              # deterministic filters: keywords, scam patterns, pay floor
    filter.py
    evaluate.py
    tailor.py
    validate.py           # resume + draft validators
    render.py
    digest.py             # daily summary / notification
    cli.py                # `hub review`, `hub shortlist`, `hub applied`, `hub add-url`, `hub ui`, ...
    actions.py            # shared operations called by BOTH cli.py and the web UI
    web/
      app.py              # minimal local UI (see §11a)
      static/htmx.min.js  # vendored, no CDN
  templates/
    resume.html
    web/                  # server-rendered UI pages (Jinja2)
  tests/
    golden/               # labeled postings from calibration — regression set
    test_validate.py
    test_pii_guard.py
    test_injection.py
  reports/                # one markdown report per evaluated job
  output/                 # rendered PDFs
  logs/
```

---

## 3. PII handling

No scrubber/reinjector module. Instead:

- `contact.yaml` — real phone, email, address, profile URLs. Read only by `render.py`, locally,
  at the final PDF-build step. Never included in any API call.
- `profile.yaml` — minimum pay, current/expected CTC, notice period. Read only by `rules.py`
  and `cli.py`. Pay comparison is done **in code** against the pay range the model extracts,
  so your salary figures never need to be sent to the API.
- `facts.yaml` — everything else (roles, dates, projects, skills, bullet variants). This is what
  gets sent to Claude. Safe to send since it's your own public-facing resume content.

Enforcement, not convention:

- All API calls go through `src/llm.py`. Before sending, it serializes the full request and
  refuses to send if any value from `contact.yaml` or `profile.yaml` appears in it (normalized:
  phone digits only, lowercase email). `tests/test_pii_guard.py` covers this.
- Logs record request IDs, token counts and job IDs — never full request bodies.
- `data/`, `reports/`, `output/`, `logs/` and `.env` are git-ignored (see §5).

---

## 4. Data model and job state machine

One state machine for jobs. `status` is changed only through `db.transition(job_id, new_status,
reason)`, which rejects transitions not listed below and appends a row to `job_events`.

```
new ──► filtered_out          (rules.py; reason recorded)
    ──► ineligible            (eligibility gate; reason + quoted phrase recorded)
    ──► needs_review          (eligibility = unclear, or injection/scam suspicion flagged)
    ──► scored
needs_review ──► scored | ineligible            (your decision)
scored ──► shortlisted | skipped                 (your decision via `hub review`)
shortlisted ──► tailored ──► applied             (`hub applied <id>` after you submit manually)
applied ──► interviewing ──► offer | rejected
applied ──► rejected | ghosted                   (ghosted = no response after follow-up date)
any pre-applied state ──► closed                 (job disappeared from source feed)
filtered_out | ineligible | needs_review | scored ──► new   (reopen: posting text changed upstream)
scored ──► ineligible                            (your decision, Inbox "Ineligible" button)
```

**Implementation notes (Revision 3):**
- Reopen (`→ new`) is used only by ingest when `description_hash` changes; later states just log an event.
- `scores` also has `description_hash`; stages are `rules | eligibility | evaluate | tailor | drafts`.
  A step is skipped when a score row exists for the same job + stage + hash (idempotency).
- Extra tables: `artifacts` (PDF/report paths, served by job ID) and `llm_usage` (tokens + cost estimate).
  `jobs` gained `missed_runs` (closure counter) and `last_error`.
- Pay is extracted once, in the eligibility step (Haiku); the bucket comes from the evaluate step, so
  the pay-vs-floor comparison runs in code after both.
- Local settings that are not PII live in `data/preferences.yaml` (target roles, title/seniority
  keywords, eligible regions, page limit, cooldown). They must not go in `profile.yaml`, because
  every `profile.yaml` value is blocked by the PII guard.
- `profile.yaml` stores money as absolute annual amounts (e.g. `2500000`), notice as `notice_days`.
  Guard limits: strings < 5 chars and numbers < 5 digits are exempt (too generic to match safely);
  a posting that states exactly your floor figure will be refused by the guard and routed to
  `needs_review`.
- Batch API is **not** implemented yet: the daily run uses the synchronous API with prompt caching.
  Revisit once volume makes the 50% discount matter.

Core tables:

```sql
jobs (
  id INTEGER PRIMARY KEY,
  source TEXT, external_id TEXT,          -- UNIQUE(source, external_id)
  company_canonical TEXT, title TEXT, location_raw TEXT, location_norm TEXT,
  url TEXT, description TEXT, description_hash TEXT,
  posted_at TEXT, first_seen TEXT, last_seen TEXT,   -- all timestamps UTC ISO-8601
  dup_of INTEGER NULL,                    -- cross-source duplicate pointer
  bucket TEXT,                            -- india | remote-foreign | relocation
  status TEXT, status_reason TEXT,
  follow_up_on TEXT NULL
)
scores (
  job_id, stage,                          -- eligibility | evaluate
  result_json, decision,
  model, prompt_version, facts_version,   -- so comparisons across weeks are meaningful
  created_at
)
job_events (job_id, from_status, to_status, reason, at)
approvals (item_id, type, status, payload, created_at, decided_at)
  -- type: fact_bank_edit | watchlist_add | contact_sent
  -- status: pending | approved | rejected
runs (run_id, started_at, finished_at, source, jobs_fetched, jobs_new, errors)
```

Approvals now cover only things the pipeline *proposes* (new facts, new watchlist companies,
referral notes awaiting you). Job progress lives entirely in `jobs.status` — no second status
system.

SQLite in WAL mode with a busy timeout, since the UI and a scheduled run can write at the same
time. Nothing edits `jobs.db` by hand — review happens through `cli.py` or the web UI, which
both call `actions.py` and so go through the same transition helper.

---

## 5. Week 0 — Setup (~2 hrs)

- **First, before any real data exists:** add `.gitignore` covering `.env`, `data/*.yaml`
  (except committed example files like `facts.example.yaml`), `data/*.db*`, `reports/`,
  `output/`, `logs/`. Untrack anything sensitive already committed (`git rm --cached .env`).
  The repo has a GitHub remote — assume anything committed is public.
- Python 3.11+, venv, install `anthropic`, `python-jobspy`, `playwright`, `pyyaml`, `jinja2`,
  `pytest`, `bleach` (or equivalent HTML sanitizer), `fastapi`, `uvicorn`, `python-multipart`.
  Run `playwright install chromium`. Download `htmx.min.js` once into `src/web/static/`.
- API account at `platform.claude.com`, funded with pay-as-you-go credits and a **monthly spend
  limit** set in the console. Create **two keys** in the same account: one for Claude Code
  (building), one for the pipeline (`ANTHROPIC_API_KEY` in `.env`). Same billing view, but each
  can be revoked and tracked independently.
- Build with Claude Code CLI authenticated via its own key (not a Pro/Max subscription —
  Code isn't needed for running the daily pipeline, only for writing it).

**Go/no-go:** environment runs, `claude` CLI authenticates, a test API call through `llm.py`
succeeds, and `git status` shows no data files.

---

## 6. Week 1 — Fact bank and calibrated scoring (the most important week)

### Fact bank (`facts.yaml`)
Every true claim about you, with an ID, and several wording variants per fact so tailoring is
a *selection* problem, not a rewriting problem. Variants are grouped under a parent fact so
the same claim can never appear twice on one resume:

```yaml
version: 3                      # bump on every edit; recorded with every score
summaries:
  - id: sum-backend
    text: Backend engineer focused on Python data systems...
roles:
  - id: acme
    title: Software Engineer
    company: Acme
    dates: 2023-01 to present
    facts:
      - id: acme-1               # the underlying claim
        variants:
          - id: acme-1a
            text: Built a Python ETL pipeline processing 2M records/day
            tags: [python, data, backend]
          - id: acme-1b
            text: Owned a data processing pipeline handling 2M daily records
            tags: [python, data-pipeline, backend]
skills:
  - {name: FastAPI, level: working, evidence: [acme-1]}   # every skill must cite a fact
synonym_map:
  ETL: [data pipeline, data processing]
  # pre-approved substitutions only — vetted once here, never generated live by the model
```

### Eligibility prompt (`prompts/eligibility.md`) — Haiku
Returns JSON. Extracts, each with the exact quoted phrase from the posting or `unclear`:
- allowed countries / regions
- timezone overlap requirement
- work authorization / visa sponsorship
- employment type (employee / contractor / EOR)
- notice period / joining requirement ("immediate joiners only", etc.)
- pay range as stated (amount, currency, period) — or `not_stated`
- posting language (non-English → `needs_review`)
- **auto-reject if the posting asks for money at any stage** (also enforced in `rules.py`)

**Quote verification in code:** every returned quote must be a (whitespace-normalized)
substring of the job description. A quote that isn't found is treated as `unclear` and the job
goes to `needs_review`. This catches hallucinated extractions for free.

### Evaluation prompt (`prompts/evaluate.md`) — Sonnet
Rubric, each dimension 1–5 with a one-line reason and a supporting quote: skills match,
seniority fit, domain, timezone overlap from IST, red flags. Tags the job into one of three
buckets: `india` / `remote-foreign` / `relocation`.

- **Pay is not scored by the model.** The model extracts it; `rules.py` normalizes currency
  and units (LPA ↔ annual USD, ranges, monthly vs annual) and compares to `profile.yaml`.
  `not_stated` is its own state — it is **not** treated as below-minimum.
- **Decision rule is written down and implemented in code**, not left to the model, e.g.:
  `apply` if skills ≥ 4 and seniority ≥ 3 and no red flag = 1 and pay ≠ below_min;
  `maybe` for borderline; `skip` otherwise. Tune during calibration.
- **Job description text is wrapped and marked as untrusted input** — instructions inside a
  posting are never followed, only evaluated. See §12 for why this alone isn't the defense.

### Calibration
Run calibration **through the actual pipeline code** (`llm.py` + the real prompts + Haiku and
Sonnet via the API) — not in a claude.ai project, which has a different system prompt and
model behaviour.

- Label at least **50 real postings** yourself first (apply / maybe / skip), with a deliberate
  mix — enough genuine "apply" cases that the metric means something.
- Measure **precision and recall on the `apply` class**, not raw agreement. A model that says
  "skip" to everything can hit 80% agreement on a skewed set.
- Pay most attention to **false negatives** (jobs you'd apply to that the model skipped) —
  in production these are invisible.
- Save the labeled set to `tests/golden/` as a pytest regression suite. Any change to prompts,
  rubric or decision rule must not reduce recall on it.

**Go/no-go:** apply-class recall ≥ ~85% and precision ≥ ~70% on the golden set. If it doesn't
get there, fix the rubric — do not start writing pipeline code on an uncalibrated prompt.

---

## 7. Week 2 — Ingestion pipeline

### Watchlist (`companies.yaml`)
Start with ~30 companies, not 100 — finding which ones hire from India and which ATS each uses
is real research time, budget it explicitly. Record ATS type and a **canonical company name**
per company (used for cross-source dedupe):

- Greenhouse: `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
- Lever: `api.lever.co/v0/postings/{company}?mode=json`
- Ashby: `api.ashbyhq.com/posting-api/job-board/{name}`
- Workday: `/wday/cxs/{tenant}/{site}/jobs` — exists per-tenant but undocumented/inconsistent,
  don't build against it blind.

Most India-based employers run Workday, Darwinbox, Zoho Recruit, or custom portals — none give
clean JSON like the above. Expect the `india` bucket to lean on JobSpy/Naukri and manual
"paste a URL" entries more than the other two buckets.

### Sources
- **Priority sources (run first, in order; `priority_sources` in preferences.yaml):**
  1. **Infopark** (`infopark.in/companies-job`): server-rendered HTML table, paginated until an empty
     page, one detail page per job (fetched once, then reused from the DB).
  2. **Technopark** (`technopark.in/job-search`): the page is an Inertia app; the list comes from the
     public JSON endpoint `/api/paginated-jobs`, details from the `data-page` JSON of
     `/job-details/<id>` (description, contact email, walk-in drive info).
  Both are complete feeds, so the 2-missed-runs closure rule applies. Technopark has published a
  fraud alert about fake jobs using its name; the scam rules (§7) apply to these sources like any other.
  Jobs are fetched politely (0.3-0.9 s between requests); `robots.txt` on both allows crawling.
- Company ATS endpoints (primary)
- 1–2 remote boards with APIs (Remotive, Himalayas)
- JobSpy (Naukri + 1–2 Indeed countries), run slowly, secondary
- Manual: `hub add-url <url>` — `http`/`https` only, never `file://` or local addresses

### `ingest.py`
Normalize every job to one shape and upsert into SQLite.

- **Identity:** `UNIQUE(source, external_id)`. Never dedupe on title/location hash alone —
  "Bangalore" vs "Bengaluru" vs "Remote – India" breaks it, and genuinely separate openings
  with the same title get merged.
- **Cross-source duplicates:** second pass matching on canonical company + normalized title +
  normalized location (alias table for cities) + description similarity. Matches set `dup_of`
  rather than being deleted; only the primary is scored.
- **Reposts / edits:** if `description_hash` is unchanged, update `last_seen` only — no
  re-scoring. If it changed, re-run eligibility.
- **Closures:** a job missing from its ATS feed for 2 consecutive successful runs → `closed`.
  Check a job is still open before tailoring.
- **HTML:** ATS descriptions (e.g. Greenhouse `content=true`) are HTML — convert to plain text
  for the model and sanitize anything that will be displayed.

### `rules.py` + `filter.py`
Cheap deterministic rules first, every rejection recorded with a reason:
- title keywords, obvious seniority mismatch
- location phrases — written carefully so "US or India" isn't caught by a "US only" rule
- **scam patterns** (hard reject or `needs_review`):
  - asks for money at any stage (registration fee, deposit, training fee, equipment cheque)
  - interview/contact moved to Telegram / WhatsApp / Signal
  - recruiter on a free email domain (gmail/outlook/yahoo) for a named large company, or
    contact domain doesn't match the company domain
  - early requests for Aadhaar, PAN, bank details, or ID scans
  - pay wildly above market for the role with minimal requirements
- notice period incompatible with `profile.yaml`

Survivors → `claude-haiku-4-5-20251001` for the eligibility gate. Passes →
`claude-sonnet-5-5` for the full rubric. `unclear` → `needs_review`, not scored automatically.

### Cost controls
- Prompt caching on the fact bank + instructions (identical every call). Check each cached
  prefix meets the model's minimum cacheable length, or caching silently doesn't apply — the
  eligibility prompt (no fact bank) may be too short to benefit.
- Batch API for the scheduled daily run (50% discount, stacks with caching). Note eligibility →
  evaluate is two sequential batches, so worst-case latency is longer than a day.
- `hub add-url` uses the synchronous API — a job you found by hand shouldn't wait for a batch.
- Structured output / tool use so responses are always valid JSON matching a schema.

### `evaluate.py`
Writes results to `scores` (with model / prompt_version / facts_version), saves a markdown
report per job to `reports/`.

### Scheduling — Windows Task Scheduler
- Enable "Run task as soon as possible after a scheduled start is missed" (laptop asleep/off).
- Decide "run only when logged on" vs not — if not, make sure the task can read `.env`
  (load it explicitly in code; don't rely on user-session env vars).
- Uncheck "start only if on AC power" or accept skipped runs on battery.

### `digest.py` — notify
End of each run, write `reports/digest-YYYY-MM-DD.md` (new `apply`/`maybe` jobs, `needs_review`
queue, source health warnings) and fire a Windows toast pointing at it.

**Go/no-go:** after a week of daily runs, count how many surfaced jobs you'd actually apply to.
Near-zero means fix the watchlist/sources, not the code.

---

## 8. Failure handling and observability

- **Idempotent runs.** Every step is keyed on job ID + stage; re-running after a crash skips
  completed work and never double-inserts or double-scores. Batch IDs are persisted so an
  interrupted run can collect results from an in-flight batch instead of resubmitting.
- **Source isolation.** Each source is fetched in its own try/except with timeout and
  exponential backoff; one failing source never aborts the run. Results go to the `runs` table.
- **Silent-breakage detection.** A source returning 0 jobs for 2 consecutive runs (board token
  changed, endpoint moved, JobSpy blocked) is flagged in the digest.
- **API failures.** Retry on rate-limit / overload with backoff; a job whose call fails
  permanently stays in its current state with the error recorded, and is retried next run.
- **Schema failures.** A response that doesn't validate against the expected schema is retried
  once, then the job goes to `needs_review` — never guessed into a decision.
- **Rejection audit.** Each week, `hub audit` shows a random sample (~10) of `filtered_out` and
  `ineligible` jobs with their reasons. If you'd have applied to any, fix the rule or prompt
  and add the case to `tests/golden/`.
- **Spend.** Log input/output/cached tokens per run; the digest shows daily and month-to-date
  cost estimate alongside the console spend limit.

---

## 9. Week 3 — Shortlist, tailoring and PDF

### Shortlist first
Tailoring runs only on jobs you've moved to `shortlisted` via `hub review` — not on every
scored job. Saves cost and keeps the review queue small.

### `tailor.py`
Sends Sonnet the job + fact bank. Model returns **only IDs**: which variant IDs, in what order,
which summary variant, which skills to list — plus optional substitutions drawn only from
`synonym_map` for keyword matching (e.g. "ETL" ↔ "data pipeline"). No free-text generation
about your history.

It may also return `missing_evidence`: requirements in the JD that no fact covers, phrased as
a suggestion ("JD asks for Kafka — no fact mentions it"). These become `fact_bank_edit`
approvals — **you** write the bullet if it's true, or reject it. The model never writes the
fact itself.

### Validator (`validate.py`)
Before a resume is built, check:
- every ID exists in `facts.yaml`
- **at most one variant per parent fact**
- every listed skill has evidence among the selected facts (or in `skills.evidence`)
- substitutions only from `synonym_map`, applied as whole-word, case-preserving matches
- titles, companies, dates are taken by code from `facts.yaml` — the model never outputs them
Fails → resume is not generated, error goes into the job's report.

### `render.py`
Jinja2 single-column HTML template with **autoescape on** (no tables/icons/columns —
ATS-readable). No job-posting text ever enters the resume template. Render with Playwright
`page.set_content()` with **JavaScript disabled and all network requests blocked** (route
abort), then `page.pdf()`.

After rendering:
- **Page count check** — if over the limit (1 or 2 pages, set in config), drop the
  lowest-ranked variant in code and re-render until it fits.
- Extract PDF text and diff against source to confirm a clean, accurate parse.

### Screening answers and referral notes (`prompts/drafts.md`)
These are free text, so they get the same anti-fabrication treatment as the resume:
- The model must cite the fact IDs each sentence relies on.
- The validator rejects any draft containing a number, company, technology, or credential
  that doesn't appear in the cited facts.
- Drafts go into the job's report for you to copy and edit. **Never auto-fill or
  auto-answer** work authorization, salary/CTC, notice period, or legal questions — those are
  shown as "answer yourself" with the relevant value from `profile.yaml` filled in locally.

---

## 10. Week 4 — Use it and measure

- **Applications:** Simplify for autofill where it works (mainly Western ATSs); expect manual
  form-filling on Indian portals (Naukri, Zoho Recruit) — test this directly rather than
  assuming coverage. After submitting, `hub applied <id>` records it and sets a follow-up date.
- **Company cooldown:** the digest warns when you're about to apply to a company you've applied
  to within the last N days (configurable) — avoid several applications to one company in a
  week, and respect companies that limit repeat applications.
- **LinkedIn:** fully manual. You search for the contact and send the connection request
  yourself; the pipeline only drafts the note (`referral_note` in the job's report, grounded per
  §9). `hub contact-sent <id>` logs it. Self-enforce ~15–20/day, spread out.
- **Tracking:** `jobs.status` per §4 + `follow_up_on`; `hub export` to CSV if a spreadsheet is
  easier to work from. Jobs past follow-up with no response → `ghosted`.
- **Metrics to check at end of week:**
  - jobs surfaced per week, per bucket, per source
  - % actually applied to
  - score-vs-judgment agreement (precision/recall on `apply`) on live jobs you reviewed
  - rejection-audit miss rate (§8)
  - callback rate vs. your prior manual baseline (expect this one to need 2–3 months of data,
    not one week — responses take 2–6 weeks to arrive)

---

## 11. Human-in-the-loop design

Two mechanisms, no overlap:

1. **Job progress** — `jobs.status` (§4). The pipeline moves jobs up to `scored`; every step
   after that (`shortlisted`, `applied`, ...) is your action via `cli.py`.
2. **Proposals** — `approvals` table, for things the pipeline suggests but can't do:
   - `fact_bank_edit` — from `tailor.py`'s `missing_evidence`; approving opens the entry for
     you to write, it doesn't append model text
   - `watchlist_add` — companies seen repeatedly in board/JobSpy results that aren't on the
     watchlist yet
   - `contact_sent` — referral notes awaiting you to send manually

Review runs on demand — in the minimal web UI (§11a) or `hub review` — not via a background
watcher process.

| Autonomous | Needs your action |
|---|---|
| Ingest, dedupe, filter | Shortlisting a job for tailoring |
| Eligibility check | Submitting any application |
| Scoring | Sending any LinkedIn message |
| Tailoring (ID selection) for shortlisted jobs | Adding a new fact to `facts.yaml` |
| PDF rendering | Answering visa/salary/notice/legal questions |
| Drafting (not sending) grounded notes/answers | Adding a company to the watchlist |
| Daily digest | Anything in `needs_review` |

This is a fixed pipeline with approval gates, not an open-ended planning agent — the sequence
of steps is already known, so there's nothing for an agent to plan. If a continuously-running,
phone-notifiable version is wanted later, revisit with the Claude Agent SDK (hooks) or an
open-source runtime like OpenClaw — not needed for the first four weeks.

---

## 11a. Minimal local UI

Purpose: make the daily review loop fast — read the digest, decide on jobs, grab the PDF and
drafts, mark things done. It's a thin layer over `actions.py`; anything the UI can do, the CLI
can do too, and neither contains business logic of its own.

### Stack
FastAPI + Jinja2 server-rendered pages + htmx (one vendored JS file) for button actions without
full page reloads. No Node, no build step, no frontend framework. Plain CSS in one file. Started
with `hub ui`, which runs uvicorn on `127.0.0.1` and opens the browser.

### Screens (five, nothing more)
1. **Inbox** (home) — today's new `apply` / `maybe` jobs and the `needs_review` queue. One row
   per job: company, title, bucket, decision, top rubric scores, pay (or `not stated`), age.
   Row buttons: **Shortlist**, **Skip**, **Ineligible**. Filter by bucket/decision.
2. **Job detail** — rubric with reasons and supporting quotes, eligibility extraction, scam /
   injection flags, the posting as escaped plain text, link to the original URL. Once tailored:
   PDF download, screening-answer and referral-note drafts with copy buttons, "answer yourself"
   fields (visa, CTC, notice) filled in from `profile.yaml`. Buttons: **Mark applied**,
   **Contact sent**, status change, follow-up date.
3. **Tracker** — everything `applied` and later, sorted by follow-up date; overdue follow-ups
   highlighted. Inline status change (interviewing / offer / rejected / ghosted). CSV export.
4. **Proposals** — `approvals` queue (`fact_bank_edit`, `watchlist_add`). Approving a fact-bank
   proposal shows what's missing; you still write the fact in `facts.yaml` by hand.
5. **Health** — last runs, per-source job counts and warnings, month-to-date spend estimate,
   the weekly rejection-audit sample (§8) with an "I would have applied" button that saves the
   case to `tests/golden/`.

Plus a single **Add URL** box in the header (same path as `hub add-url`, synchronous API).

### Explicitly not in the UI
- Editing `facts.yaml`, `contact.yaml`, `profile.yaml` — edit the files by hand.
- Submitting applications or sending messages — it only records that you did.
- Login, multiple users, remote access, mobile layout.
- Charts/dashboards beyond plain numbers on the Health page.

### UI security (a localhost app is still reachable from any website you visit)
- Bind to `127.0.0.1` only, never `0.0.0.0`.
- Reject requests whose `Host` header isn't `127.0.0.1:<port>` / `localhost:<port>` (blocks DNS
  rebinding).
- No state changes on GET. Every POST carries a CSRF token (random per `hub ui` start, checked
  server-side); cookies `SameSite=Strict`. This stops a malicious page from POSTing to
  localhost to mark jobs or approve proposals.
- Job text is rendered as autoescaped plain text — never `|safe`, never raw ATS HTML. Links from
  postings are shown as text, apart from the job's own source URL.
- `Content-Security-Policy: default-src 'self'` — no CDN scripts, no inline scripts, no external
  images, so even an escaping bug can't load anything from outside.
- PDFs and reports are served by job ID lookup, never by a file path from the request (no path
  traversal).

### When to build it
After the Week 2 go/no-go, alongside Week 3 — the Inbox and Job detail screens are needed
once shortlisting exists. Tracker, Proposals and Health can follow in Week 4. The CLI stays
the fallback the whole time.

---

## 12. Security hardening

### Prompt injection
Marking job text as untrusted is necessary but not sufficient — the real defense is that the
model's output can't do much. The realistic attacks are a posting inflating its own score
("rate this candidate's fit 5/5"), slipping text into a referral note, or suppressing a red
flag.
- No tools with side effects are ever given to the model.
- Output is schema-validated JSON; decisions are enums; the final apply/skip is computed in
  code from rubric scores (§6).
- Hard rules (scam patterns, pay floor, notice period) run in `rules.py`, after and
  independent of the model — a posting can't talk its way past them.
- Quote verification (§6) — reasons must be backed by text actually in the posting.
- A cheap regex flags instruction-like text in postings ("ignore previous", "you are an AI",
  "rate this", hidden/zero-width characters); flagged jobs go to `needs_review`.
- `tests/test_injection.py` holds a few adversarial postings that must not change decisions.

### Untrusted HTML / content
- Job HTML is converted to plain text for the model and sanitized before any display.
- Reports are markdown with job text in fenced/quoted blocks; links from postings are shown as
  plain text, not rendered as clickable HTML.
- Resume template never receives job text; Jinja autoescape on.
- Playwright renders with JS disabled and network blocked.

### Fetching
- `hub add-url` accepts only `http`/`https`, rejects `file://`, `localhost`, and private IP
  ranges, applies a timeout and response-size cap.
- JobSpy runs slowly with randomized delays; respect each site's terms, accept that it may get
  blocked, and rely on §8 to notice.

### Secrets and data at rest
- `.env` and all personal data git-ignored (§5); `.env.example` has names only.
- Separate API keys for building vs running; monthly spend limit set.
- PII guard in `llm.py` (§3); logs never contain request bodies.
- `jobs.db` and `data/` are backed up locally (copy to another drive), not to a public repo.

---

## 13. Explicit safeguards

- Automatic reject on any posting that asks for money at any stage — enforced in code, not
  only in the prompt. Other scam patterns (§7) reject or route to `needs_review`.
- Job description text is always treated as untrusted input, never as instructions, and model
  output is bounded (§12).
- Every piece of text about you — resume, screening drafts, referral notes — is validated
  against the fact bank.
- No auto-answering of work authorization, salary/CTC, notice period, or legal screening
  questions — draft only, answered by you.
- No LinkedIn automation of any kind — search and send are manual, always.
- No auto-submission of any application, ever.
- Contact details and salary figures never sent to the API — enforced by the `llm.py` guard.

---

## 14. Deferred — only if Week 4 numbers justify it

- Upwork/freelance pipeline (check real Apify actor pricing first; add client payment-currency
  checks — prefer clients paying in USD/EUR/GBP for GST export treatment, confirm with an
  accountant)
- Gmail parsing to auto-update application status
- A richer UI beyond §11a (only once the minimal one becomes the actual bottleneck)
- Skill-tree sync from roadmap.sh — just edit `facts.yaml` by hand instead
- Agent SDK / OpenClaw-based continuous runtime with phone notifications
- Non-English posting support (currently routed to `needs_review`)

---

## 15. Known weak spots to watch

- India-bucket sourcing is the least reliable part of the pipeline (see §7) — keep the
  `hub add-url` manual fallback easy to use.
- ID-only tailoring limits keyword rewording to what's in `synonym_map` — expand it as gaps
  show up, and use `missing_evidence` proposals to grow the fact bank truthfully. Don't let the
  model free-generate substitutions.
- False negatives are invisible by default — the weekly rejection audit (§8) is the only thing
  that surfaces them. Don't skip it.
- Most Indian postings don't state pay; `not_stated` jobs will be common and need a human
  judgment call, not an automatic skip.
- Four weeks is enough to judge process metrics, not callback rate — don't treat an early low
  callback count as a verdict on the system.
- Simplify's ATS coverage for Indian job portals is unverified — test on 2–3 real postings in
  Week 4 before relying on it.
