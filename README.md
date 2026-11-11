# JobEngine

A Django job-discovery app for applicants in any field. Share a resume or describe your background,
have a short conversation, review your profile, approve a search plan, and explore real job postings.
The interface includes a personal workspace, saved roles, source-backed assessments, and resumable
background work. It uses Django templates, local CSS, and a small amount of JavaScript; no Node build
or frontend service is required.

The landing page collects six profile fields, a resume or background summary, up to eight short
interests, up to five selected cities/regions, work arrangements, and start availability. Its school
list contains fictional demo schools. This prototype assumes a US citizen seeking US jobs with no
sponsorship needed. It does not collect or assess immigration status, remote-work countries, or
compensation preferences. Selected locations limit on-site and hybrid jobs; permitted remote jobs
elsewhere must allow work from those areas without moving. Relocation defaults to no, and selecting
remote does not imply relocation consent. Unprovided graduation dates remain unknown. **Start my interview** saves the form and immediately launches background preparation.
A typing screen displays “Personalizing Questions” and opens the first question automatically. The interview
shows only the current question: **Continue** or **Ctrl+Enter** (**Cmd+Enter** on Mac) submits an
answer. Accepted answers remain saved; reviewing or editing earlier answers is not exposed yet.
Every page uses the same logo and size as the start page.
In local development, **Try Jane Doe** beside **Development Page** fills the form with the fictional
applicant from `.scratch/jane-doe/intake.json` and `resume.md`, then starts a fresh interview. Previous
interview answers are never imported. The shortcut is hidden outside local development or when its
fixture files are absent.

Search quality and latency remain unresolved. Current priorities and concrete failures are recorded
in [TODO.md](TODO.md); the latest live validation is
[Jane Doe's Terra/Sol rerun](.scratch/jane-doe-terra-sol-2026-11-09/report.md), with a
[visual report and full interview](.scratch/jane-doe-terra-sol-2026-11-09/report.html).

## Run on Windows

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Set OPENAI_API_KEY in .env.
.\.venv\Scripts\python.exe manage.py runserver
```

Open http://127.0.0.1:8000. `manage.py` is the Django entry point. No Django database migrations
are needed: browser cookies contain only a signed workspace identifier, while candidate data stays
in the existing local store. **Settings & privacy** shows connection and budget status; set provider
credentials in `.env` and restart the server. The application uses a billed API account; a ChatGPT
subscription does not pay for its requests. Model choices and cost estimates live in
[llm.py](jobengine/infrastructure/llm.py). Interview calls use `gpt-6.1-terra` with low reasoning;
search planning, discovery, extraction, and assessment use `gpt-6.1-sol` with low reasoning.
Optional `OPENAI_INTERVIEW_MODEL` and `OPENAI_SEARCH_MODEL` settings override those defaults.
They replace the former shared `OPENAI_MODEL` setting, which is no longer read. Existing `.env`
files can keep that old entry; the stage-specific defaults apply without editing credentials.

## Code layout

| Location | Responsibility |
|---|---|
| `manage.py` | Django development and management entry point |
| `jobengine/web/` | Django settings, routes, forms, workspace access, and background tasks |
| `jobengine/web/templates/`, `static/` | Applicant screens, responsive styles, and browser interactions |
| `jobengine/ui/` | Retained Streamlit implementation for migration regression checks |
| `jobengine/onboarding/` | Interview, topic policy, preferences, and search-plan approval |
| `jobengine/search/` | Discovery, retrieval, assessment, and ranking |
| `jobengine/infrastructure/` | Model requests, SQLite/file storage, and resume reading |
| `jobengine/terminal/`, `jobengine/__main__.py` | Stage-by-stage terminal workflow and local diagnostic artifacts |
| `jobengine/models.py` | Shared data and export schemas |
| `jobengine/grounding.py` | Source-quotation and evidence validation |
| `jobengine/prompts.py` | All current model instructions |
| `tests/` | Offline automated checks with isolated storage |
| `scripts/` | Two reusable diagnostic commands |
| `.scratch/` | Local experiments, fictional resumes, reports, and archived material |

See [docs/architecture.md](docs/architecture.md) for the workflow, module interfaces, and development
conventions. Historical design plans and one-off scripts are under `.scratch/archive/`.
John Doe's resume is at `.scratch/john-doe/John_Doe_Resume.docx`.

## Development checks

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

Tests do not make paid model requests or change the real candidate data. Pytest collects only `tests/`;
Ruff excludes `.scratch/`. To format an edit, run `python -m ruff format` on the changed files.
`python manage.py check` validates Django configuration; `node --check jobengine/web/static/jobengine/app.js`
checks JavaScript syntax when Node is installed. Legacy Streamlit tests use
`jobengine/ui/legacy_app.py`; Streamlit is now a development dependency.
When Node is available, pytest also runs offline checks of interview submission and navigation.

## Diagnostic commands

Use the [terminal debugging guide](docs/terminal-debugging.md) to register candidates, interview
with Terra, review and approve a plan, run Sol independently, and replay saved job evaluations.
Every execution saves structured stage outputs, actual model payloads/responses, per-job failure
traces and a summary. The CLI preserves the application's profile/brief approvals and fingerprints.

```powershell
.\.venv\Scripts\python.exe -m jobengine candidate jane-doe --resume .scratch/jane-doe/resume.md --professional-years 0.75
.\.venv\Scripts\python.exe -m jobengine interview jane-doe
.\.venv\Scripts\python.exe -m jobengine plan jane-doe
.\.venv\Scripts\python.exe -m jobengine search jane-doe --limit 3 --max-minutes 5
# Opt in to Greenhouse instead of web discovery (requires an approved profile/plan):
.\.venv\Scripts\python.exe -m jobengine search john-doe-v3 --greenhouse "thenewyorktimes=The New York Times" --greenhouse "propublica=ProPublica" --limit 3 --max-assessments 10
```

Type `Y` to approve or `N` to decline at each review gate (lowercase also works). Run artifacts default to `data/debug/runs/<candidate>/<run-id>/`;
candidate working files are under `data/debug/candidates/`. Calls use the shared cost ledger.
Optional `--max-minutes` stops new search work after the specified minutes and saves partial results.
An in-flight step finishes first, so the elapsed time can exceed that budget.
These local files contain candidate information; see the guide's storage and redaction limitations.
Older working records discard retired intake fields when loaded. Plans affected by this migration
require fresh profile and plan approval; historical run artifacts remain intact.
The earlier standalone diagnostic commands remain supported:

```powershell
# Inspect original posting URLs without model calls; HTTP access is required.
.\.venv\Scripts\python.exe scripts/inspect_postings.py https://employer.example/job

# Run a paid search using a profile exported after in-app approval.
.\.venv\Scripts\python.exe scripts/search_profile.py --profile candidate-profile.json
```

The standalone read-only Greenhouse connector can be checked without starting Sol or creating a
candidate. Its public interface is `jobengine.search.greenhouse.fetch_board_jobs`:

```powershell
.\.venv\Scripts\python.exe -c "from jobengine.search.greenhouse import fetch_board_jobs; jobs = fetch_board_jobs('thenewyorktimes'); print('Postings:', len(jobs)); print('Descriptions available:', sum(bool(j.description and j.description.strip()) for j in jobs))"
```

Each returned record exposes `id`, `internal_job_id`, `board_token`, `title`, `location`, `url`, and
`description`. Descriptions preserve the original API HTML/entities; missing content remains `None`.
The connector includes prospect/general-application posts if returned by the board (identified by
`internal_job_id=None`). It returns `[]` for an empty board, raises `GreenhouseError` for HTTP errors,
timeouts, other transport failures, and malformed responses, and rejects invalid tokens with
`ValueError`. It does not follow redirects or retry requests. The opt-in search path calls it
through the batch collector.

Opt in to dynamic board discovery from the existing approved applicant plan, without a predefined
employer list or a new plan/approval step:

```powershell
.\.venv\Scripts\python.exe -m jobengine search john-doe-v3 --discover-greenhouse --limit 3 --max-assessments 10
# Optional discovery limits (defaults shown):
.\.venv\Scripts\python.exe -m jobengine search john-doe-v3 --discover-greenhouse --limit 3 --max-assessments 10 --board-search-requests 4 --max-boards 12
```

`--discover-greenhouse` and explicit `--greenhouse TOKEN=EMPLOYER` are mutually exclusive.
Omitting both retains the default web-search workflow. `--max-assessments` works with either
Greenhouse mode; discovery-limit flags require `--discover-greenhouse`. Verified boards feed the
same batch collector, hybrid screening, Sol assessment and bounded backfilling as explicit boards.
Successful verification responses are reused within the invocation, so collection makes no second
board request. No individual job-page retrieval is added. These commands can incur search/model costs.

The standalone callable also remains available:

```python
from jobengine.search.greenhouse_discovery import discover_greenhouse_boards
from jobengine.search.greenhouse_batch import discover_boards

# Explicit opt-in: model_client is the existing cost-ledger-backed ModelClient.
# This call can incur model/web-search costs; it is not run automatically.
postings_cache = {}
result = discover_greenhouse_boards(
    model_client,
    approved_profile,
    max_search_requests=4,
    max_boards=12,
    postings_cache=postings_cache,
)
print(result.boards)  # {verified_board_token: source_backed_employer_label}
candidates, board_summaries = discover_boards(result.boards, prefetched_postings=postings_cache)
```

Defaults permit four model search requests (one web tool call each), up to four separate employer-label
normalizations, and twelve unique board API inspections, including failed inspections. Hard maxima
are eight search requests and forty boards. Sources beyond the board limit remain deferred in
`result.candidates`; API/search/normalization failures and rejected URLs are retained in the result.
Later search passes receive previously seen tokens to encourage broader coverage, without a per-sector
company quota. Only exact supported Greenhouse URLs from search provenance supply tokens. Board and
job URLs, plus Greenhouse embed URLs with an explicit `for` token, are supported. Custom-domain links
with only `gh_jid`, guessed tokens, and lookalike hosts are not accepted.

API verification establishes board availability, including empty boards, **not legal employer
identity or job fit**. Employer labels require exact search-title evidence; unidentified boards are
omitted from the mapping. All observed labels remain available, with a warning and first-source label
when they differ. Existing posting-level employer validation still applies downstream. Search is
incomplete, and titles lacking employer names can leave valid boards unidentified.

Backend callers use `run_search(..., discover_greenhouse=True, board_search_requests=4, max_boards=12)`
for the integrated path. Discovery respects the existing shared cost budget and optional search time
budget; in-flight requests finish before stopping. No automatic retry or fallback is added.
Debug runs save `search/greenhouse-discovery.json` with queries, source URLs, observed labels, verified,
failed and deferred boards, limits, stop reason and discovery ledger usage/costs. The existing
`greenhouse-boards.json`, `greenhouse-pool.json`, `embedding-usage.json` and whole-run `usage.json`
remain available under `data/debug/runs/<candidate>/<run-id>/`.
`board_discovery_empty`, `board_discovery_error` and `board_discovery_unidentified` stop before
assessment when no usable boards are available; they are not candidate non-match findings.
`boards_empty` means verified boards returned no postings. Partial discovery failures retain successful
boards and explicit warnings. Commands save these statuses and exit normally, with partial runs marked
in the manifest. The hybrid scorer, ranking, qualification assessment and validation are unchanged.
An offline illustration using John's approved brief and a fictional physical therapist is in
[the board-discovery demo](.scratch/greenhouse-board-discovery/report.md).

Convert an already-fetched post to existing evaluation inputs without further HTTP/model calls:

```python
from jobengine.search.greenhouse import fetch_board_jobs
from jobengine.search.greenhouse_adapter import adapt_posting

jobs = fetch_board_jobs("thenewyorktimes")
item, page = adapt_posting(jobs[0], employer="The New York Times")
print(item.model_dump())
print(page.status, page.text[:500])
```

The employer name is explicit because a board token is not an authoritative display name.
Optional `sector` and `retrieved_at` arguments retain a known sector/fetch timestamp; otherwise the
sector stays unset and the timestamp is the UTC conversion time. Location appears in page text;
board/posting/internal IDs appear in `page.extraction_method`, and the board API is recorded in
`item.company_source_url`. Descriptions are decoded to readable text without truncation. Missing,
too-short, or oversized descriptions return `page.status="failed"` with an error, retaining the
posting for debugging rather than treating metadata alone as a usable job description.

Collect several boards without running Sol:

```python
from jobengine.search.greenhouse_batch import discover_boards

candidates, summaries = discover_boards(
    {
        "thenewyorktimes": "The New York Times",
        "propublica": "ProPublica",
    }
)
for summary in summaries:
    print({key: value for key, value in summary.items() if key != "postings"})
# Each candidate is an existing (DiscoveryItem, JobPage) pair with its description retained.
```

The batch uses existing canonical job identities and retains the first occurrence. Per-board
`fetched` counts partition into usable unique `converted` pages, `duplicated` occurrences, and
`failed` pages/conversions. Failed pages remain in the candidate collection. Each summary's
`postings` list retains every occurrence's board token, post/internal IDs, original URL, identity,
and conversion/duplicate/error status. Board request/configuration failures are isolated and
reported with `status="failed"` and `error`; fetched/failed posting counts are `None` when unknown.
Empty boards report zero counts and `status="empty"`. The batch makes no LLM calls, assessments,
relevance filters, or candidate-state writes. `run_search(..., greenhouse_boards={token: employer})`
uses this batch instead of web discovery, then calls `prioritize_semantically(..., hybrid=True)`.
`limit` targets 1–10 confirmed recommendations (`strong`/`weaker`). Existing `evaluate(..., page=page)`
processes postings in priority order and backfills excluded, unverified, failed and review-needed
results until that target or a budget is reached. `max_assessments` / CLI `--max-assessments` defaults
to 20 and accepts 1–20, never exceeding the global inspection cap. Attempts, including failures,
consume this cap. Descriptions are reused without job-page HTTP retrieval. Default web search is unchanged.
`shortlist_target_reached`, `assessment_limit`, and `candidate_pool_exhausted` distinguish completion
from assessment-budget exhaustion and source exhaustion. Time/cost stops remain explicit. Technical
failures remain warnings/partial runs even when enough recommendations are subsequently found.
Profile/plan approval, evidence validation, employer identity checks and the shared cost ledger remain
required. Board or embedding failures produce explicit warnings/partial stops, not no-match claims.
Debug runs retain the complete pool in `search/greenhouse-pool.json`, per-board provenance and failures
in `search/greenhouse-boards.json`, and token/cost/cache diagnostics in `search/embedding-usage.json`.
Time limits are checked before/after board collection and screening and before each evaluation;
an in-flight batch finishes. This is a paid search when embeddings or Sol responses are not cached.

Prioritize the collected pool locally before choosing an evaluation batch:

```python
from jobengine.search.prescreen import prioritize_postings

selected, ranked = prioritize_postings(candidates, approved_profile, top_n=10)
```

This requires the existing profile/plan approvals and fingerprints. `selected` contains original
item/page pairs; `ranked` retains every input pair with rank, score, component breakdown, reasons,
matched title terms, and `selected_for_evaluation` or `not_selected_for_batch` status. A low score
never labels a posting unqualified or rejected. `top_n=0` produces an audit without selecting any.
The 100-point evaluation-priority scale allocates 45 points to title-role overlap, 20 to description
role vocabulary, 20 to interests, 5 to the existing evidence-keyword baseline, and 10 to location
signals. Token inflections are normalized; common pool vocabulary is downweighted in descriptions.
Scores are relative to this pool, not fit probabilities. Missing locations and unverified remote
permission are neutral (5 location points), selected-area mentions get 10, and other locations get
0 while remaining eligible. Only the explicit `Location:` source field supplies location signals;
employer/headquarters boilerplate does not. Arrangement, experience, skills, eligibility, and current
availability still need later assessment. The function makes no network/model calls and does not
modify the profile, source pages, existing ranking, or `run_search()`.

A separate embedding-only alternative uses OpenAI `text-embedding-3-small` and the same cost ledger:

```python
import os
from dotenv import load_dotenv
from jobengine.infrastructure.embeddings import EmbeddingClient
from jobengine.infrastructure.storage import Store
from jobengine.search.semantic import prioritize_semantically

load_dotenv()
store = Store(os.getenv("JOBENGINE_DATA_DIR", "data"))
embeddings = EmbeddingClient(store, os.environ["OPENAI_API_KEY"])
try:
    selected, ranked, usage = prioritize_semantically(
        candidates, approved_profile, embeddings, top_n=10
    )
finally:
    embeddings.close()
```

This is a paid standalone operation; it does not call Sol or start `run_search()`. The applicant
representation uses approved roles, interests, work themes and up to six selected evidence summaries,
not raw resumes. Job representations contain titles and responsibility/requirement/fallback text,
with recognized generic company sections omitted. Inputs are bounded to 6,000 UTF-8 bytes and requests
to 32 inputs, conservatively below both model input limits without a tokenizer dependency. Unrecognized
headings use fallback text, so boilerplate removal is heuristic and English-heading dependent.

`ranked` preserves every item/page pair, cosine similarity (not a probability), embedding status,
selection status, and the exact embedding text/hash for inspection. These diagnostics can contain
applicant information. Failures leave similarity `None` and defer affected postings; no scores,
qualification judgments, lexical fallbacks, or deterministic location/seniority filters are invented.
`usage` records request token counts, estimated USD, unsettled reservations, cache hits, receipt IDs,
and errors. Provider exception messages are not logged. Known usage settles the ledger even if a
vector response is malformed; ambiguous failures retain reservations. Requests are batched ordinary
Embeddings API calls, not discounted asynchronous Batch API jobs.

Job embeddings are reusable across applicants in the existing SQLite cache for up to a year. Keys
include model ID, 1,536 dimensions, representation/cache version, source-content hash and actual
embedded-text hash. The published model currently has no dated snapshot ID; bump the local version
when representation rules or upstream embedding behavior change. Failed batches are not automatically
retried, and completed batches/cache entries survive later failures. No additional dependencies,
vector database, or provider framework is required.

The same scorer has an optional deterministic priority layer (use it inside the client's open
`try` block above):

```python
selected, ranked, usage = prioritize_semantically(
    candidates, approved_profile, embeddings, top_n=10, hybrid=True
)
```

It adds no API calls. For already-scored rows, call
`jobengine.search.semantic_priorities.apply_priority_adjustments(ranked, approved_profile, top_n=10)`
directly; it returns selected pairs and all adjusted rows. Optional `job_facts` maps original posting
URLs to existing `JobFacts`; relevant requirement numbers and quotations must be present in the source.
Embedding inputs/cache keys do not change. The lexical scorer remains independent; the opt-in
Greenhouse search path uses the hybrid scorer without changing its algorithm or assessment behavior.

Each row retains `similarity` and `embedding_rank`, and adds `adjustments`, `adjustment_evidence`,
`adjustment_total`, `uncertainty_flags`, `final_score` and `deferral_reasons`. The final priority score
is similarity plus a total adjustment capped to +/-0.12; it is not itself a cosine or probability.
Explicit experience bounds produce +0.005 when within the declared total, or -0.015/-0.04/-0.08 for gaps
up to 2/up to 5/over 5 years. Titles alone, preferred/alternative requirements, and unknown experience
remain neutral; total experience is not proof of specialty-specific tenure.
Qualification sections support numeric/word minima, ranges (`3–5`, `3 to 5`), `5+ years' experience`,
and upper bounds (`no more than five years`). Ranges supply a minimum, not an experience ceiling.
Bare counts outside qualification context, unrelated durations, preferred wording and ambiguous
alternatives remain neutral. A maximum is never interpreted as a minimum. Adjustment weights/caps
are unchanged; matching one numeric bound does not establish qualification.

Location matching uses structured intake plus source `Location:`, `Work arrangement:`,
`Eligible work locations:`, `Excluded work locations:` and `Relocation required:` labels, or grounded
existing location terms. The offline geographic vocabulary normalizes US state names/codes and a
small set of city aliases (e.g. NYC/New York, NY and SF/San Francisco, CA). Explicit own-role residency
requirements in descriptions can produce a -0.04 conflict using known city/state/Census-region
relationships, including remote roles with a required base. This is not a geocoder or metro/commute
model; ambiguous or unknown areas stay unverified. A known matching area adds 0.01;
an explicit arrangement, excluded-area or relocation conflict lowers priority by 0.04. Remote does not
imply eligibility from the applicant's area. Free-text-only legacy geography remains neutral for review.

Employment adjustments require explicit approved `Constraint` entries with field `Employment type`
(e.g. `full_time` or `permanent`) or recognized exclusions such as `No internships` in approved other
exclusions. No default permanent/full-time/internship preference is inferred. Source `Employment type:`
labels distinguish hours from engagement type: a full-time preference alone does not exclude full-time
internships. Known conflicts lower priority by 0.03; explicit matches add 0.005. Unsupported wording
and missing types remain neutral.

Explicit non-job statements lower priority by 0.10. Narrowly stated duty denials with at least two
normalized terms from the applicant's approved work/experience lower priority by 0.08. These are
inspectable, industry-independent textual heuristics, not a semantic classifier or qualification
assessment. Ambiguous wording stays neutral. All postings remain in the pool; only the batch cutoff
defers them. Conservative adjustments can leave senior roles or misleading advertisements near the top,
and do not certify geography, eligibility, or overall fit.





Both commands support `--help`. The search command uses the normal cost ledger and search limits.
Candidate-specific live walkthroughs are historical experiments in `.scratch/`, not application code.

## Saved data and limitations

`.env`, `data/`, and `.scratch/` are ignored by Git. `data/` contains the SQLite cache/cost ledger,
resumable onboarding snapshots, and JSON search runs/reviews. The app saves extracted resume text and
accepted answers locally; it does not retain original uploaded resume files. Candidate content is sent
to the model provider. Discovery receives a reduced approved brief rather than raw resumes.

Use **Your workspace** to switch profiles, start a fresh profile, or restore an exported progress
backup. Profiles from the previous Streamlit app can be imported through its local library when
accessing the development server on loopback. Existing JSON, approvals, runs, reviews, and the ledger
remain compatible; importing a local record leaves the legacy active-candidate pointer unchanged.
Restoring a JSON backup creates a new profile and requires fresh approval. **Previous searches**
retains earlier results, including after profile edits. Downloads include profile, progress, and
search exports. Bookmarks persist across restarts.

Keep this browser's cookies to reconnect to its workspace. Download progress from **Your profile**
for backup or transfer to another browser. Clearing cookies disconnects the workspace without deleting
its records. `JOBENGINE_DATA_DIR` selects an alternate local data directory. The local development
ledger caps its budget at $10; estimates and reservations are not an account-wide billing limit.

AI work runs in a bounded local thread pool, with progress saved to SQLite and polled by the page.
Leaving the page does not cancel work. A server restart marks interrupted tasks for an explicit retry;
requests are never automatically repeated. Profile or preference edits require profile review and a
newly approved plan before another search. Qualification assessments retain the model's contextual
judgment; ranking no longer overwrites them by comparing one total-years value with a tenure minimum.

Interview personalization is optional for completing intake. If configuration, provider access, or
evidence extraction fails, the interview continues with four guided questions about the applicant's
goals and preferences. This path saves verbatim source excerpts for profile review, preserves supported
existing evidence, and makes no further model calls during that interview. It does not infer missing
qualifications or bypass the approvals required for search. Operator logs record only the exception
class; provider messages and applicant content are never included.

Open-web search is incomplete. Pages may be stale, blocked, or require JavaScript. Exact quotations
establish traceability, not correct interpretation or complete qualification coverage. Scores are
ranking heuristics, not hiring probabilities. Unverified leads and excluded postings are available
in separate result filters, with their caveats intact.

This version is designed for local use and demos. Browser workspace isolation is not account
authentication. Public deployment still needs accounts, operator access control for budget and
configuration, a durable task queue, an application server, and a static-file server. The worker
recovery model assumes one server process. Before deployment, follow the
[Django deployment checklist](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/), set
`DJANGO_DEBUG=0`, a strong `DJANGO_SECRET_KEY`, and explicit `DJANGO_ALLOWED_HOSTS`, and serve over HTTPS.
The legacy library is disabled outside development mode and can also be disabled locally with
`JOBENGINE_LOCAL_LIBRARY=0`. The development secret is generated in the selected data directory;
keep that file to retain signed browser workspaces across restarts.
