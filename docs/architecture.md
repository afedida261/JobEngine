# Application structure

The application separates Django presentation from the backend workflows. No backend module imports
Django, Streamlit, `jobengine.web`, or `jobengine.ui`. Applicant-facing changes live in `web/` and call
the existing backend interfaces. `ui/` retains the old Streamlit implementation for manual legacy use.

## Workflow and interfaces

1. `infrastructure/resumes.py`: `read_resume` extracts text from PDF, DOCX, or text input.
2. `onboarding/interview.py`: `start_onboarding`, `answer_question`, and `finish_interview` return
   onboarding state. `policy.py` owns topic order and question limits; `preferences.py` owns structured
   intake validation and constraint semantics. `guided.py` retains verbatim applicant excerpts and
   core questions when personalization is unavailable; it never invents an extracted qualification.
3. `onboarding/brief.py`: proposes and validates a search plan, then fingerprints the reviewed profile
   and approved brief. `require_confirmed_search_brief` enforces approval in the backend as well as UI.
4. `search/pipeline.py`: `run_search` orchestrates sector, company, and posting research; `evaluate`
   extracts and validates employer facts, assesses fit, and calls deterministic ranking. Optional
   `max_minutes` provides a monotonic time budget checked before discovery/evaluation steps. An
   in-flight step finishes; completed results are saved with `time_limit` as the stop reason.
   The terminal `search --max-minutes` option reports that run as partial. Omission changes no limits.
   Optional `greenhouse_boards={token: employer}` selects board discovery instead of the web path.
   Alternatively `discover_greenhouse=True` discovers boards from the same approved profile/brief,
   with `board_search_requests` and `max_boards` overrides (defaults 4 and 12). The two modes are
   mutually exclusive. Dynamic verification responses pass through an invocation-local postings
   cache to the existing collector; no duplicate discovery or board retrieval is performed.
   After the same approval/evidence guards, a small branch collects/deduplicates via `discover_boards`,
   ranks the complete pool with `prioritize_semantically(..., hybrid=True)`, and backfills existing
   `evaluate(..., page=page)` until `limit` strong/weaker recommendations or a budget is reached.
   `max_assessments` defaults to `MAX_INSPECTIONS` (20), accepts 1–20 and counts failed attempts too.
   Stop reasons distinguish target, assessment cap, pool exhaustion, time, cost and source failures.
   No job-page refetch occurs. Its embedding wrapper shares the ModelClient SDK and Store ledger
   without owning the SDK. Greenhouse `Run.jobs` contains confirmed recommendations only; other
   outcomes stay in inspected jobs/diagnostics with dispositions and reasons.
   Board/embedding failures retain partial results with explicit stop reasons/warnings. Existing
   web discovery, Sol assessment and result ranking remain unchanged.
   `search/employer_identity.py` supplies the shared API/web employer identity gate. Exact names
   agree directly; legal-name variants and organizational components require independent posting
   evidence and employer provenance. Explicit subsidiary, division, department, alias and owned-brand
   relationships can reconcile different names. Greenhouse board/post IDs must agree with provenance;
   caller-provided employer headers are excluded as evidence. No corporate-name allowlist or fuzzy
   substring matching is used. Accepted/rejected `employer_identity` diagnostics retain the reason,
   names and supporting evidence; unsupported relationships remain unverified before assessment.
5. `search/discovery.py` discovers source-backed employers and posting URLs. `retrieval.py` fetches
   public pages, extracts text, and caches provenance. `ranking.py` applies the current category/score
   rules. Requirement assessments retain the model's contextual interpretation, including experience
   alternatives, subject to evidence validation; ranking does not override them by comparing numeric
   years. Model-identified required gaps still exclude. Known weaknesses are in TODO.
   `search/greenhouse.py` is a standalone, read-only Greenhouse Job Board API connector.
   `fetch_board_jobs(board_token)` returns typed `GreenhouseJob` records, preserving post/internal IDs,
   board token, title, location, original URL, and raw description. Empty boards return `[]`;
   HTTP/transport/JSON/schema failures raise `GreenhouseError`. It reuses HTTPX and existing URL-shape
   validation; the connector itself makes no model calls, retries or candidate-state writes.
   `search/greenhouse_adapter.py` exposes `adapt_posting(posting, employer=...)`, returning existing
   `DiscoveryItem`/`JobPage` inputs without I/O. Employer display names are supplied explicitly;
   location is retained in page text because `DiscoveryItem` has no location field. The board API
   URL is the company source, and extraction metadata retains board/post/internal IDs. The adapter
   decodes escaped HTML, reuses `extract_html`, and retains description sections outside main/article.
   Pages use UTC timestamps and SHA-256 text hashes. Missing/short or oversized descriptions become
   explicit failed pages using the existing 250-character / 90,000-character retrieval conventions.
   No shared schemas or evaluation/discovery behavior change.
   `search/greenhouse_batch.py` exposes `discover_boards({token: employer})`, returning unique
   existing item/page pairs and per-board summary dictionaries. It reuses `pipeline.job_identity`,
   keeps first occurrences, and retains every occurrence's provenance in summary posting records.
   Board failures are isolated; failed pages remain candidates. Unknown counts on board failures
   stay `None`. The collector performs no qualification filtering or model calls; the opt-in search
   branch supplies its output to semantic screening and existing evaluation.
   `search/greenhouse_discovery.py` provides `discover_greenhouse_boards(client, profile)`.
   It enforces existing profile/brief fingerprints and searches the complete public brief, with
   rotating query/sector angles and previously seen boards, rather than selecting a few employers
   per sector. Tokens come only from supported Greenhouse URLs in web-search provenance. Each unique
   board is checked once through `fetch_board_jobs`; failures count against the board limit. Separate
   structured employer labeling requires exact source-title evidence. API availability and observed
   employer labels remain distinct; missing labels exclude a board from the returned mapping.
   `BoardDiscoveryResult.boards` is directly compatible with `discover_boards`, while candidates,
   source URLs/labels, searches, failures, deferred boards, limits and counters retain diagnostics.
   Defaults cap search requests at four (one web tool call each), label calls at four, and board
   inspections at twelve; hard maxima are eight and forty. Both new model operations use the existing
   search model/reasoning/cache/ledger configuration. The pipeline calls it only for explicit dynamic
   opt-in. Optional `postings_cache` captures successful API responses; the batch collector accepts
   these via `prefetched_postings`, including empty lists, without refetching. The pipeline supplies
   a deadline callback and stops before new work on time/cost exhaustion. Discovery empty/error/
   unidentified stops are distinct from empty verified boards and exhausted assessed candidate pools.
   `search/prescreen.py` exposes `prioritize_postings(pairs, approved_profile, top_n=10)` for a
   standalone deterministic evaluation queue. It uses normalized token/inflection overlap,
   description corpus frequencies, existing evidence keyword scoring, and conservative location
   signals. It returns selected original pairs plus the entire scored/explained pool. Non-selected
   rows are deferred for this batch, never rejected. Existing profile/plan approval guards apply;
   it makes no HTTP/model calls or qualification decisions and does not modify production ranking.
   `search/semantic.py` is an independent embedding-only alternative. It constructs a bounded query
   from roles, interests, approved work themes and selected evidence summaries, then ranks existing
   item/page pairs by cosine similarity. Job text orders responsibility sections before requirements
   and fallback prose; recognized generic company/benefit sections are omitted. Full pages remain
   intact. Embedding-only remains the default. Optional `hybrid=True` applies
   `search/semantic_priorities.py` to existing similarities without further provider calls. It uses
   source-grounded existing `JobFacts` when supplied, or narrowly explicit requirements/source labels.
   Bounded adjustments cover explicit tenure, location/relocation/arrangement restrictions, declared
   employment preferences and clear non-job/duty-denial evidence. Original similarities, source
   quotes, uncertainty flags, final scores and deferral reasons are retained. Missing or unsupported
   structure is neutral; there is no automatic rejection. The optional Greenhouse branch uses hybrid
   screening for evaluation priority; default web search does not use semantic screening.
   Qualification-context parsing recognizes minimum ranges, N+ years and separate maximum bounds;
   preference/alternative/unrelated wording stays neutral. `search/geography.py` holds offline
   US state/region data and same-city aliases, with no geocoder, commute or metro expansion. Explicit
   role residency statements can supply source-backed soft conflicts; unknown areas stay unverified.
   `infrastructure/embeddings.py` batches `text-embedding-3-small` requests with 1,536 dimensions,
   disables automatic retries, validates response indices/vectors, and uses existing `Store` cache
   and cost reservations/settlements. Cache keys include model, local representation epoch, source
   hash and embedded-text hash; job keys contain no applicant identity. Unknown/broken embeddings
   remain unscored/deferred in the complete pool. No vector database or additional agent is used.
6. `web/views.py` composes applicant pages and enforces HTTP methods, revision checks, ownership,
   and approval. `web/forms.py` validates uploads, preferences, editable profiles, plans, and backups.
   Django templates and static assets provide the interface without a frontend framework or build.
   `web/presentation.py` supplies pure result-card data. `project_job(job, profile)`
   joins requirements, assessments, differentiators, evidence and declarations against the saved run
   profile. It preserves all qualifications and the original `RankedJob` alongside a five-requirement
   preview; gaps and unknowns remain separate. `select_dashboard_cards(run)` preserves saved order and
   original run/position references, selecting up to ten grounded `strong`/`weaker` recommendations.
   It supplements to five only with explicit `StretchReview` records bound to the run, discovery URL
   and every gap ID. Reviewers establish modestness/plausibility; no score threshold does so. Reviews
   cannot override hard conflicts/unknowns, incomplete or ungrounded assessments, employer identity
   failures, validation issues, required unknowns, or closed/expired postings. Unknown availability
   retains its saved caveats. These helpers perform no I/O. Interview completion targets `/results/`;
   `overview()` redirects there after interview setup, and `results()` renders `results_dashboard.html`
   with `select_dashboard_cards(run)` for owned saved runs, or an empty list when none exists.
   Native details/summary cards
   expose requirements, supported strengths, gaps, unresolved qualifications and the original link.
   No stretch reviews are supplied implicitly. `web/demo.py` loads a fixed, matching approved onboarding/run pair for the local-only
   `/demo/` → `/demo/interview/` → `/demo/results/`
   replay linked from `/start/`. It reuses existing templates, displays actual saved questions and
   answers, and renders the original saved assessment through the same presentation helper.
   The replay uses John's first answered exchange and saved Wirecutter weaker recommendation.
   A demo-only selector also projects the two saved `needs_review` cases as **Needs review**,
   retaining original run positions, gaps and uncertainties without changing normal eligibility.
   His completed profile and approved plan remain available through the demo navigation.
   Dedicated GET-only views bypass workspace/task helpers; navigation, logo and restart links stay
   inside the replay. No mutable demo progress is stored: the URL identifies the current step.
   Missing/invalid/mismatched recordings are unavailable and never replaced with invented content.

7. `web/workspace.py` scopes compatible onboarding snapshots and runs to browser workspaces, persists
   bookmarks, and locks edits while a task is running. `web/tasks.py` runs existing workflows in a
   bounded local thread pool; SQLite records progress, completion, failure, and restart recovery.
   Polling updates the progress page; no model calls run on GET requests or implicit retries.
   Intake POST starts preparation immediately. Its minimal typing screen polls task status and opens the
   question deck automatically. Legacy intake records render that same screen with a CSRF-protected
   preparation POST. Questions submit asynchronously with Continue or Ctrl+Enter. Only the current
   question is rendered; earlier exchanges remain in storage without navigation or editing controls.

The backend accepts its model client/storage dependencies explicitly. Tests supply fake clients and
temporary storage. Root `manage.py` launches Django; `web/wsgi.py` provides its WSGI entry.

## Django workspace and execution boundaries

Signed cookies contain an opaque workspace ID only. Candidate data remains in SQLite and JSON.
The `web_*` SQLite tables link owned candidates, historical runs, bookmarks, and tasks without changing
the existing Pydantic schemas. Web snapshot writes use the same revision semantics as `Store` and leave
the legacy active-candidate pointer untouched. Each workspace can run one AI task at a time. Pending
answers and reviewed profiles are saved before model work so failures retain the applicant's entries.
Backend approval fingerprints are checked before search, including after edits and on retries.

Landing-page details are retained in the optional `OnboardingSession.general_information` field and
as a labeled source document. Intake can retain education status without a graduation date.
The prototype assumes a US citizen seeking US jobs who never needs sponsorship. Intake and matching
omit authorization, sponsorship, remote-work countries, and compensation preferences. Discovery uses
United States as its work country; every model stage receives the same product scope instructions.
Current intake schemas and exports omit retired keys. Compatibility validators discard those keys,
retired constraints/suggestions/terms, and remote-country clauses from older JSON. Affected profiles
lose their approvals, and saved web workspaces return to profile review with stale plan drafts cleared.
Other profile/brief fingerprints retain their existing structure. Terminal commands normalize
resumable working files on load; immutable historical artifacts remain intact.
Selected locations are hard limits for on-site/hybrid work. Permitted remote roles elsewhere must
allow work from those selected areas without moving. `IntakePreferences.location_constraint()` owns
this shared wording for forms, profiles, planning, discovery, and assessment. Relocation defaults to
`no`; legacy `any` values load as `no`. Even explicit relocation consent does not expand the selected
areas. Older location constraints are rebuilt on profile load, clearing affected profile/plan
approvals so an outdated nationwide plan cannot authorize discovery.

The optional `OnboardingSession.interview_mode` defaults to `adaptive` for existing JSON. A failed
interview model call or invalid extraction switches the session to `guided`, saving only supported
prior evidence plus labeled verbatim source excerpts. Further interview answers use core questions
without provider calls. Profile review and plan approval remain required. Plan and search failures
retain their explicit retry behavior.

Source quotes, provider output, and applicant text are escaped by templates. External posting links
allow only HTTP(S). Mutation routes require POST and CSRF. Responses are private and never cached.
Only loopback requests in development can import unclaimed legacy records; remote requests cannot
use that library. Backup restore clones a record into the current workspace and clears old approvals.

This is a local deployment model: browser ownership is not account authentication. The process-local
worker identity detects interrupted tasks after restart; multi-process hosting requires a durable
queue and a different recovery model. See README for public-deployment requirements. Matching,
retrieval limits, and the cost ledger retain their existing behavior.

## Shared data, prompts, and infrastructure

- `models.py` defines Pydantic schemas for applicant evidence, onboarding, search briefs, job facts,
  assessments, saved snapshots, and exported runs. Keep schema changes deliberate: old JSON must load.
- `grounding.py` validates quotations and evidence references against original sources. It is shared
  by onboarding and job assessment.
- `prompts.py` is the source of truth for shared and stage-specific instructions. Data is supplied
  separately or inserted into discovery templates. Avoid copying prompts into Markdown snapshots.
- `infrastructure/llm.py` owns request construction, reasoning settings, cache keys, usage estimates,
  and model-provider interaction. Interview calls default to GPT-6.1 Terra with low reasoning;
  search planning, discovery, extraction, assessment, and their repairs default to GPT-6.1 Sol with
  low reasoning. Each stage has its own model setting (see README). The selected model determines
  cache identity, cost rates, and ledger entries; `ModelClient.model` identifies the search model
  recorded on exported runs. It reserves costs before requests and disables automatic retries.
- `infrastructure/storage.py` owns SQLite caching, the cost ledger, atomic onboarding snapshots,
  revision checks, active-candidate selection, and JSON run/review files.
  Run JSON exports use `infrastructure/persistence.py`: unique temporary files, atomic replacement,
  and six bounded attempts for permission/sharing errors. Essential export errors remain explicit.
- `terminal/cli.py` exposes candidate registration, interview, plan review/approval, search, and
  saved-page evaluation through `python -m jobengine`. It calls the same backend functions and
  approval guards, with candidate working state separate from the web/legacy active record and a
  shared cost ledger. Resume-plus-form-JSON registration calls Django `IntakeForm` and its shared
  `create_session` initializer; backend onboarding remains independent of Django. Optional graduation
  month/year use the existing intake date validator. `terminal/artifacts.py` writes run snapshots,
  stage events and per-job traces.
  Repeatable `search --greenhouse TOKEN=EMPLOYER` selects only the supplied boards.
  `search --discover-greenhouse` selects dynamic discovery instead, with `--board-search-requests`
  and `--max-boards` limits. Existing profile/brief approvals are reused. Dynamic details and discovery
  ledger costs are saved to `search/greenhouse-discovery.json`; failures remain visible even when
  successful boards later satisfy the target. No usable boards means no screening/assessment.
  Diagnostic observations save `search/greenhouse-boards.json`, `greenhouse-pool.json` and `embedding-usage.json`,
  including deferred original pages, similarity/adjustment evidence, evaluation outcomes and ledger
  receipts. Shared saved-data schemas and approval fingerprints are unchanged.
- `infrastructure/diagnostics.py` provides opt-in context-local observations, inert outside a
  diagnostic run. Model calls expose requests, pre-parse HTTP responses, validated results and
  failures; discovery/retrieval/evaluation expose rejected candidates and stage outcomes. `evaluate`
  optionally accepts a saved `JobPage` for replay; its default still retrieves normally. No prompt,
  schema, validation, matching, or search-strategy changes are involved. Artifact formats and
  Windows commands are documented in [terminal-debugging.md](terminal-debugging.md).
  Terminal observations retain events in memory when optional diagnostic files cannot be saved;
  these failures are recorded as persistence failures and mark the artifact manifest partial.
  Required result exports still raise `PersistenceError`; diagnostic I/O does not change search outcomes.

Discovery caches last one hour, source text six hours, and other model responses one day. Cache keys
include the prompt, schema, model, and reasoning effort. Source validation failures and provider
failures do not fabricate replacement results.

## Verification and developer tools

`requirements.txt` contains application dependencies. `requirements-dev.txt` adds pytest, Ruff, and
Streamlit for manual legacy use; legacy Streamlit UI tests are retired;
their configuration is in `pyproject.toml`. Use absolute `jobengine.*` imports and explicit imports
from the owning module; package initializers do not re-export a second compatibility interface.

Tests are organized by behavior rather than historical implementation runs:

- `test_onboarding.py` and `test_onboarding_flow.py`: interview behavior, approval, saving, recovery,
  with fake model clients.
- `test_sector_search.py` and `test_discovery_retrieval.py`: coverage, provenance, limits, and retrieval.
- `test_grounding_ranking.py` and `test_job_evaluation.py`: quotations, evidence, evaluation, and ranking.
- `test_search_policy.py`: reasoning settings, general-applicant searches, cohorts, and plan regeneration.
- `test_storage.py`: cost/cache behavior and saved-data integration.
- `test_web.py`: the complete Django HTTP journey with fake model clients, approvals, stale forms,
  task failures/restarts, bookmarks, exports, backups, legacy import, CSRF, and browser isolation.
- `test_web_browser.py`: runs the shipped interview event handlers with Node when available,
  including answer/skip submission with named form controls and keyboard submission.
- `test_terminal.py`: named candidates, interactive and non-interactive orchestration, approval
  gates/fingerprints, independent search and saved-page replay, partial failures, raw invalid model
  output, redaction and state isolation, using fake providers and isolated storage.

Tests do not depend on the fictional resumes under `.scratch/` or on saved real applicant
data. Keep local experiments and live-validation artifacts in `.scratch/`, not under `tests/`.

The only maintained standalone scripts are `scripts/inspect_postings.py` for HTTP retrieval diagnostics
and `scripts/search_profile.py` for searching an exported approved profile. Neither runs during tests.
Historical candidate-specific scripts and superseded design docs are archived in `.scratch/archive/`;
their old imports and instructions are historical, not supported entry points.
