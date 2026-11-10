# JobEngine

A Django job-discovery app for applicants in any field. Share a resume or describe your background,
have a short conversation, review your profile, approve a search plan, and explore real job postings.
The interface includes a personal workspace, saved roles, source-backed assessments, and resumable
background work. It uses Django templates, local CSS, and a small amount of JavaScript; no Node build
or frontend service is required.

The landing page collects six profile fields, a resume or background summary, up to eight short
interests, up to five selected cities/regions, work arrangements, and start availability. Its school
list contains fictional demo schools. Unasked authorization details and graduation dates remain
unknown. **Start my interview** saves the form and immediately launches background preparation.
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
search planning, discovery, extraction, and assessment use `gpt-6.1-sol` with high reasoning.
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

```powershell
# Inspect original posting URLs without model calls; HTTP access is required.
.\.venv\Scripts\python.exe scripts/inspect_postings.py https://employer.example/job

# Run a paid search using a profile exported after in-app approval.
.\.venv\Scripts\python.exe scripts/search_profile.py --profile candidate-profile.json
```

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
