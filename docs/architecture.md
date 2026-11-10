# Application structure

The application separates Django presentation from the backend workflows. No backend module imports
Django, Streamlit, `jobengine.web`, or `jobengine.ui`. Applicant-facing changes live in `web/` and call
the existing backend interfaces. `ui/` retains the old Streamlit implementation for regression checks.

## Workflow and interfaces

1. `infrastructure/resumes.py`: `read_resume` extracts text from PDF, DOCX, or text input.
2. `onboarding/interview.py`: `start_onboarding`, `answer_question`, and `finish_interview` return
   onboarding state. `policy.py` owns topic order and question limits; `preferences.py` owns structured
   intake validation and constraint semantics. `guided.py` retains verbatim applicant excerpts and
   core questions when personalization is unavailable; it never invents an extracted qualification.
3. `onboarding/brief.py`: proposes and validates a search plan, then fingerprints the reviewed profile
   and approved brief. `require_confirmed_search_brief` enforces approval in the backend as well as UI.
4. `search/pipeline.py`: `run_search` orchestrates sector, company, and posting research; `evaluate`
   extracts and validates employer facts, assesses fit, and calls deterministic ranking.
5. `search/discovery.py` discovers source-backed employers and posting URLs. `retrieval.py` fetches
   public pages, extracts text, and caches provenance. `ranking.py` applies the current category/score
   rules. Requirement assessments retain the model's contextual interpretation, including experience
   alternatives, subject to evidence validation; ranking does not override them by comparing numeric
   years. Model-identified required gaps still exclude. Known weaknesses are in TODO.
6. `web/views.py` composes applicant pages and enforces HTTP methods, revision checks, ownership,
   and approval. `web/forms.py` validates uploads, preferences, editable profiles, plans, and backups.
   Django templates and static assets provide the interface without a frontend framework or build.
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
as a labeled source document. Intake can retain education status without a graduation date and leave
authorization undeclared; blank authorization creates no restriction. Existing snapshots remain
loadable, and profile/brief fingerprints retain their existing structure.

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
  high reasoning. Each stage has its own model setting (see README). The selected model determines
  cache identity, cost rates, and ledger entries; `ModelClient.model` identifies the search model
  recorded on exported runs. It reserves costs before requests and disables automatic retries.
- `infrastructure/storage.py` owns SQLite caching, the cost ledger, atomic onboarding snapshots,
  revision checks, active-candidate selection, and JSON run/review files.

Discovery caches last one hour, source text six hours, and other model responses one day. Cache keys
include the prompt, schema, model, and reasoning effort. Source validation failures and provider
failures do not fabricate replacement results.

## Verification and developer tools

`requirements.txt` contains application dependencies. `requirements-dev.txt` adds pytest, Ruff, and
Streamlit for retained regression tests;
their configuration is in `pyproject.toml`. Use absolute `jobengine.*` imports and explicit imports
from the owning module; package initializers do not re-export a second compatibility interface.

Tests are organized by behavior rather than historical implementation runs:

- `test_onboarding.py` and `test_onboarding_flow.py`: interview behavior, approval, saving, recovery,
  and actual form flows with fake model clients.
- `test_sector_search.py` and `test_discovery_retrieval.py`: coverage, provenance, limits, and retrieval.
- `test_grounding_ranking.py` and `test_job_evaluation.py`: quotations, evidence, evaluation, and ranking.
- `test_search_policy.py`: reasoning settings, general-applicant searches, cohorts, and plan regeneration.
- `test_storage.py` and `test_ui.py`: cost/cache behavior and rendering/saved-result integration.
- `test_web.py`: the complete Django HTTP journey with fake model clients, approvals, stale forms,
  task failures/restarts, bookmarks, exports, backups, legacy import, CSRF, and browser isolation.
- `test_web_browser.py`: runs the shipped interview event handlers with Node when available,
  including answer/skip submission with named form controls and keyboard submission.

Tests do not depend on the fictional resumes under `.scratch/` or on saved real applicant
data. Keep local experiments and live-validation artifacts in `.scratch/`, not under `tests/`.

The only maintained standalone scripts are `scripts/inspect_postings.py` for HTTP retrieval diagnostics
and `scripts/search_profile.py` for searching an exported approved profile. Neither runs during tests.
Historical candidate-specific scripts and superseded design docs are archived in `.scratch/archive/`;
their old imports and instructions are historical, not supported entry points.
