# Local Career Command Hub — Build Plan

Revised plan for a local, AI-assisted job search and application pipeline, trimmed from an
original 7-phase / 30-day design down to a 4-week part-time build with weekly go/no-go
checkpoints. Windows laptop, Python only, no Node.

**Status:** ready to build once target roles/stack/seniority are filled in below.

---

## 0. Open input (blocks Week 1)

```
Target roles:        <TBD>
Stack / seniority:   <TBD>
Relocation:          open to / not open to <TBD countries>
Priority bucket:     india-based | remote-for-foreign-employer | relocation  (rank order)
Minimum acceptable pay: <TBD>
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
  isn't in the bank.
- **Contact details never leave your machine.** Split from the fact bank entirely (see §3).
- **Stable sources over scraping.** ATS JSON endpoints and remote-board APIs lead; JobSpy/Naukri
  are a slower, secondary source.
- **Deterministic pipeline, not a planning agent.** The sequence of steps is already known
  (ingest → filter → score → tailor → render → notify → wait → apply → log), so a fixed script
  with LLM judgment calls at specific points is more predictable and cheaper than an open-ended
  agent deciding its own next action.

---

## 2. Repo structure

```
career-hub/
  data/
    facts.yaml          # fact bank sent to the API — experience, skills, bullet variants, no PII
    contact.yaml         # phone, email, address — local only, never sent to the API
    companies.yaml        # watchlist: company + ATS type + board token
    jobs.db               # SQLite: jobs, scores, approvals
  prompts/
    eligibility.md
    evaluate.md
    tailor.md
  src/
    ingest.py
    filter.py
    evaluate.py
    tailor.py
    render.py
    approvals.py           # watches jobs.db for status=approved, acts on it
  templates/
    resume.html
  reports/                  # one markdown report per evaluated job
  output/                   # rendered PDFs
```

---

## 3. PII handling

No scrubber/reinjector module. Instead:

- `contact.yaml` — real phone, email, address. Read only by `render.py`, locally, at the final
  PDF-build step. Never included in any API call.
- `facts.yaml` — everything else (roles, dates, projects, skills, bullet variants). This is what
  gets sent to Claude. Safe to send since it's your own public-facing resume content.

---

## 4. Week 0 — Setup (~2 hrs)

- Python 3.11+, venv, install `anthropic`, `python-jobspy`, `playwright`, `pyyaml`, `jinja2`,
  `pytest`. Run `playwright install chromium`.
- API key from `console.anthropic.com` (platform.claude.com), set as `ANTHROPIC_API_KEY`,
  funded with pay-as-you-go credits. Used both for building (Claude Code CLI) and for the
  production pipeline later — one account, one place to watch spend.
- `.env` in `.gitignore`.
- Build with Claude Code CLI authenticated via the API key (not a Pro/Max subscription —
  Code isn't needed for running the daily pipeline, only for writing it).

**Go/no-go:** environment runs, `claude` CLI authenticates, a test API call succeeds.

---

## 5. Week 1 — Fact bank and calibrated scoring (the most important week)

### Fact bank (`facts.yaml`)
Every true claim about you, with an ID, and several bullet variants per role/skill so
tailoring is a *selection* problem, not a rewriting problem:

```yaml
roles:
  - id: acme
    title: Software Engineer
    company: Acme
    dates: 2023-01 to present
    bullets:
      - id: acme-1a
        text: Built a Python ETL pipeline processing 2M records/day
        tags: [python, data, backend]
      - id: acme-1b
        text: Owned a data processing pipeline handling 2M daily records
        tags: [python, data-pipeline, backend]
skills:
  - {name: FastAPI, level: working, evidence: acme-1a}
synonym_map:
  ETL: [data pipeline, data processing]
  # pre-approved substitutions only — vetted once here, never generated live by the model
```

### Eligibility prompt (`prompts/eligibility.md`)
Returns JSON. Extracts, with the exact quoted phrase from the posting or `unclear`:
- allowed countries / regions
- timezone overlap requirement
- work authorization / visa requirement
- employment type (employee / contractor / EOR)
- **auto-reject if the posting asks for money at any stage** (registration fee, deposit, etc.)

### Evaluation prompt (`prompts/evaluate.md`)
Rubric, each dimension 1–5 with a one-line reason: skills match, seniority fit, domain, pay vs.
your minimum, timezone overlap from IST, red flags. Tags the job into one of three buckets:
`india` / `remote-foreign` / `relocation`.

**Job description text is wrapped and marked as untrusted input** — instructions inside a
posting are never followed, only evaluated.

### Calibration
Paste 25 real postings into a Claude project with the prompts + fact bank. Rate each yourself
first, then compare. Revise the prompt until you agree on apply/don't-apply ~80% of the time.
Expect several iteration rounds, not one pass.

**Go/no-go:** calibration reaches ~80% agreement. If it doesn't, fix the rubric — do not start
writing pipeline code on an uncalibrated prompt.

---

## 6. Week 2 — Ingestion pipeline

### Watchlist (`companies.yaml`)
Start with ~30 companies, not 100 — finding which ones hire from India and which ATS each uses
is real research time, budget it explicitly. Record ATS type per company:

- Greenhouse: `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
- Lever: `api.lever.co/v0/postings/{company}?mode=json`
- Ashby: `api.ashbyhq.com/posting-api/job-board/{name}`
- Workday: `/wday/cxs/{tenant}/{site}/jobs` — exists per-tenant but undocumented/inconsistent,
  don't build against it blind.

Most India-based employers run Workday, Darwinbox, Zoho Recruit, or custom portals — none give
clean JSON like the above. Expect the `india` bucket to lean on JobSpy/Naukri and manual
"paste a URL" entries more than the other two buckets.

### Sources
- Company ATS endpoints (primary)
- 1–2 remote boards with APIs (Remotive, Himalayas)
- JobSpy (Naukri + 1–2 Indeed countries), run slowly, secondary

### `ingest.py`
Normalize every job to one shape, dedupe on hash(company, normalized title, location), insert
into SQLite with `status = new`.

### `filter.py`
Cheap rules first (title keywords, obvious seniority mismatch, "US only" type phrases).
Survivors → `claude-haiku-4-5-20251001` for the eligibility gate. Only passes/`unclear` →
`claude-sonnet-5-5` for the full rubric.

### Cost controls
- Prompt caching on the fact bank + instructions (identical every call)
- Batch API for the daily run (50% discount, stacks with caching)
- Structured output / tool use so responses are always valid JSON

### `evaluate.py`
Writes results to SQLite, saves a markdown report per job to `reports/`. Scheduled daily via
Windows Task Scheduler.

**Go/no-go:** after a week of daily runs, count how many surfaced jobs you'd actually apply to.
Near-zero means fix the watchlist/sources, not the code.

---

## 7. Week 3 — Tailoring and PDF

### `tailor.py`
Sends Sonnet the job + fact bank. Model returns **only IDs**: which bullets, in what order,
which summary variant, which skills to list — plus optional substitutions drawn only from
`synonym_map` for keyword matching (e.g. "ETL" ↔ "data pipeline"). No free-text generation
about your history.

### Validator
Before a resume is built, check:
- every ID exists in `facts.yaml`
- dates/titles copied verbatim
- no substitution used outside `synonym_map`
Fails → resume is not generated.

### `render.py`
Jinja2 single-column HTML template (no tables/icons/columns — ATS-readable) → Playwright
Python `page.pdf()`. Extract PDF text afterward and diff against source to confirm a clean,
accurate parse.

### Screening questions
Draft answers go into the job's report for you to copy. **Never auto-fill or auto-answer**
work authorization, salary expectations, or legal questions.

---

## 8. Week 4 — Use it and measure

- **Applications:** Simplify for autofill where it works (mainly Western ATSs); expect manual
  form-filling on Indian portals (Naukri, Zoho Recruit) — test this directly rather than
  assuming coverage.
- **LinkedIn:** fully manual. You search for the contact and send the connection request
  yourself; the pipeline only drafts the note (`referral_note` output in the job's report).
  Self-enforce ~15–20/day, spread out, same as any automated cap would.
- **Tracking:** SQLite status column (new / applied / interviewing / rejected) + follow-up date,
  or export to a spreadsheet if that's easier to work from.
- **Metrics to check at end of week:**
  - jobs surfaced per week
  - % actually applied to
  - score-vs-judgment agreement rate
  - callback rate vs. your prior manual baseline (expect this one to need 2–3 months of data,
    not one week — responses take 2–6 weeks to arrive)

---

## 9. Human-in-the-loop design (approvals)

Add one table to `jobs.db`:

```sql
approvals (item_id, type, status, payload)
-- type: apply | contact | fact_bank_edit
-- status: pending | approved | rejected
```

The pipeline writes `pending` rows instead of acting directly — a scored job gets its tailored
resume generated and attached, a referral note gets drafted, a new fact you want to add gets
queued — but nothing is submitted, sent, or written to `facts.yaml` until you flip the status.

`approvals.py` watches for `status = approved` and performs the next step (finalize PDF, log a
contact as sent once you've actually sent it, append to the fact bank). Review can be as simple
as editing the SQLite table directly, a spreadsheet, or a small local HTML page with checkboxes.

| Autonomous | Needs your approval |
|---|---|
| Ingest, dedupe, filter | Submitting any application |
| Eligibility check | Sending any LinkedIn message |
| Scoring | Adding a new fact to `facts.yaml` |
| Tailoring (ID selection) | Answering visa/salary/legal questions |
| PDF rendering | Adding a company to the watchlist |
| Drafting (not sending) referral notes | Anything scored `unclear` on eligibility |

This is a fixed pipeline with approval gates, not an open-ended planning agent — the sequence
of steps is already known, so there's nothing for an agent to plan. If a continuously-running,
phone-notifiable version is wanted later, revisit with the Claude Agent SDK (hooks) or an
open-source runtime like OpenClaw — not needed for the first four weeks.

---

## 10. Explicit safeguards

- Automatic reject on any posting that asks for money at any stage.
- Job description text is always treated as untrusted input, never as instructions.
- No auto-answering of work authorization, salary, or legal screening questions — draft only.
- No LinkedIn automation of any kind — search and send are manual, always.
- No auto-submission of any application, ever.

---

## 11. Deferred — only if Week 4 numbers justify it

- Upwork/freelance pipeline (check real Apify actor pricing first; add client payment-currency
  checks — prefer clients paying in USD/EUR/GBP for GST export treatment, confirm with an
  accountant)
- Gmail parsing to auto-update application status
- A real UI (only once scrolling markdown reports becomes the actual bottleneck)
- Skill-tree sync from roadmap.sh — just edit `facts.yaml` by hand instead
- Agent SDK / OpenClaw-based continuous runtime with phone notifications

---

## 12. Known weak spots to watch

- India-bucket sourcing is the least reliable part of the pipeline (see §6) — keep the
  "paste a URL" manual fallback easy to use.
- ID-only tailoring limits keyword rewording to what's in `synonym_map` — expand it as gaps
  show up, don't let the model free-generate substitutions.
- Four weeks is enough to judge process metrics, not callback rate — don't treat an early low
  callback count as a verdict on the system.
- Simplify's ATS coverage for Indian job portals is unverified — test on 2–3 real postings in
  Week 4 before relying on it.
