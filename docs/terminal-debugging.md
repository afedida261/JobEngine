# Terminal workflow diagnostics

Run from the repository root with the Windows virtual environment. These commands use the same
interview, grounded evidence validation, search-plan approval, discovery, retrieval, assessment and
ranking functions as Django. Models, prompts, reasoning settings, limits and budget rules are unchanged.
Interview uses Terra; planning and search use Sol. Provider failures during the interview enter the
same guided recovery path as the application, with an explicit failure event.

## Register and interview a candidate

```powershell
# A fresh fictional candidate; no previous answers are imported.
.\.venv\Scripts\python.exe -m jobengine candidate jane-doe --resume .scratch/jane-doe/resume.md --role "Communications assistant, PR coordinator" --interests "Communications, community outreach" --professional-years 0.75
.\.venv\Scripts\python.exe -m jobengine interview jane-doe

# Another candidate needs only an ID and a supported resume, not application code.
.\.venv\Scripts\python.exe -m jobengine candidate john-doe --resume .scratch/john-doe/John_Doe_Resume.docx
```

Answer at the prompt, or enter `/skip`, `/finish` (explicitly finish early), or `/pause`.
Run `interview` with the same ID to resume a paused interview. If interrupted during a request,
the pending answer remains in `session.json` and the run's transcript; resubmit it explicitly.
An exhausted answer file pauses an unfinished interview, rather than silently finishing it.

Resume-only candidates use structured intake with undeclared fields left unknown, matching Django's
question-coverage policy. Terra returning no question does not skip the required core topics.
An already-completed interview prints that status instead of silently exiting. To restart one
(including an older zero-question session), run:

```powershell
.\.venv\Scripts\python.exe -m jobengine interview jane-doe --restart
```

Restart uses the registered source documents, archives the previous session/profile in the new run,
and clears prior profile/plan approvals. Historical run artifacts remain intact.

`candidate --resume path/to/resume.docx --intake path/to/intake.json` accepts the exact
Django pre-interview form field names and values. It uses `IntakeForm` for parsing, validation,
source documents, and initial session creation; no conversion to `IntakePreferences` is needed.
For example:

```json
{
  "full_name": "John Doe",
  "school": "Westhaven College",
  "degree": "undergraduate",
  "program": "Design",
  "graduation_status": "graduated",
  "graduation_month": "5",
  "graduation_year": "2025",
  "professional_years": "0.75",
  "interests": "Design, Community outreach",
  "locations": "Boston, NYC",
  "work_arrangements": ["on_site", "hybrid", "remote"],
  "start_mode": "date",
  "start_date": "2027-01-02"
}
```

```powershell
.\.venv\Scripts\python.exe -m jobengine candidate john-doe --resume .scratch/john-doe/John_Doe_Resume.docx --intake .scratch/intake.json
```

`full_name`, `graduation_status`, and `start_mode` are required. Optional fields may be omitted or
left blank as on the website; `work_arrangements` is a JSON array (an empty array means unrestricted).
`interests` and `locations` are comma-separated strings, as submitted by the form. Graduation month
and year are optional together; supplied dates use the existing intake consistency checks.
A specific start mode (`date`) requires `start_date`. The form's school, degree, status, location,
and arrangement choices remain authoritative. Optional `background` text is supported as well.
Selected locations limit on-site and hybrid work. If remote is allowed, employers elsewhere may be
included only when the role permits working from the selected areas without relocating. Relocation
defaults to `no`, and older `any` values normalize to `no`. Existing location constraints are updated
on load and affected plans must be regenerated and reviewed.
Do not combine form intake with `--role`, `--interests`, `--professional-years`, or saved-state imports.
Resume-only registration and existing saved-state imports remain supported.

Validation reports the field, submitted value, and reason before writing any candidate state.
Failed commands still retain diagnostic run artifacts. Registration makes no model requests;
run `interview` separately. Registration saves the same profile-details source document and general
information as Django; interview/restart preserves them, including during guided recovery. The run's
`candidate/input.json` includes the validated `registration_session`, and interview snapshots plus
model request events retain the exact context supplied to Terra. The prototype assumes a US citizen
seeking United States jobs without sponsorship needs. Authorization/sponsorship, remote-work countries,
and compensation preferences are absent from the intake schema and matching. Older candidate working
files are normalized on load; affected profiles and plans require renewed approval. Historical run
artifacts keep their original contents for inspection. Legacy `IntakePreferences` JSON is no longer
the `--intake` input format.
To copy an existing record, use `candidate copied-id --record path/to/onboarding.json`
(`SavedOnboarding` JSON), `--profile path/to/profile.json`, or `--session path/to/session.json`.
Record imports resume that record's interview state, including completed interviews. Registration
refuses to overwrite an existing ID. Imported records are copied locally; they never become the
web application's active candidate. Use a new ID for a fresh interview from the same resume.

## Review the profile and approve a plan

Every command prints its unique artifact directory. Candidate working files default to
`data/debug/candidates/<id>/`; immutable execution snapshots live under
`data/debug/runs/<id>/<run-id>/`. After a completed interview:

```powershell
Get-Content data/debug/candidates/jane-doe/profile.json
Get-Content data/debug/candidates/jane-doe/session.json
.\.venv\Scripts\python.exe -m jobengine plan jane-doe
```

`plan` shows evidence, source references, roles, preferences and constraints. Each approval prompt shows `[Y/N]`: enter `Y` to approve or `N` to decline, in either case.
First confirm an unreviewed profile; then review and approve the proposed search plan.
Invalid input repeats the prompt. Approval calls the production `confirm_search_brief` function.
The resulting `planning/approved-profile.json` and candidate `profile.json` use the ordinary
`Profile` schema and contain the original approval fingerprints, not a CLI-specific authorization.

Declining plan approval preserves the reviewed profile with no approved brief. The proposed plan
and reviewed profile remain in the run directory. Reuse them without another model call:

```powershell
$planRun = 'data/debug/runs/jane-doe/REPLACE_WITH_RUN_ID'
.\.venv\Scripts\python.exe -m jobengine plan jane-doe --profile "$planRun/planning/reviewed-profile.json" --draft "$planRun/planning/proposed-plan.json"
```

Edit a **copy** of `interview/profile.json` to adjust roles, interests or constraints, preserving
the schema. Set `confirmed` to `false` and `search_brief` to `null`, then pass it to `plan --profile`.
Edit a copied `proposed-plan.json` and pass `--draft` to review a different plan. Updated profiles
and briefs require approval again. Backend fingerprint checks reject edits to approved exports.
There is no force-search or approval-bypass flag.

## Run Sol independently and inspect failures

```powershell
.\.venv\Scripts\python.exe -m jobengine search jane-doe --limit 3
# Optional time budget in minutes (decimals are accepted).
.\.venv\Scripts\python.exe -m jobengine search jane-doe --limit 3 --max-minutes 5
# Or use an immutable approved artifact; repeat freely without another interview/plan call.
.\.venv\Scripts\python.exe -m jobengine search jane-doe --profile "$planRun/planning/approved-profile.json" --limit 3

$searchRun = 'data/debug/runs/jane-doe/REPLACE_WITH_SEARCH_RUN_ID'
Get-Content "$searchRun/summary.txt"
Get-Content "$searchRun/failures.json"
Get-Content "$searchRun/search/results.txt"
Get-Content "$searchRun/search/discovery-handoff.json"
```

`--max-minutes` must be a positive finite number; omit it for the existing untimed behavior.
The budget starts when search begins and uses elapsed monotonic time. At the deadline, no new
company discovery, posting discovery, or job evaluation starts. An in-flight discovery/evaluation
step finishes first and may exceed the budget; this is not a hard cancellation or provider timeout.
Completed results and diagnostics are saved with `search.stop_reason=time_limit` and
`manifest.status=partial`. The command exits normally (0), as with other retained partial results.
The existing shortlist, research, inspection, company, and dollar-budget limits still apply.

Opt in to supplied Greenhouse boards instead of web discovery:

```powershell
.\.venv\Scripts\python.exe -m jobengine search john-doe-v3 --greenhouse "thenewyorktimes=The New York Times" --greenhouse "propublica=ProPublica" --limit 3 --max-assessments 10
```

Repeat `--greenhouse TOKEN=EMPLOYER` for each board; names remain subject to existing employer
identity validation. This runs the existing batch collector and hybrid semantic scorer, then
evaluates postings in hybrid order, using their original API descriptions, until `--limit` strong/weaker
recommendations (1–10). Other outcomes cause backfilling within `--max-assessments` (default 20;
allowed 1–20, capped by the existing global inspection limit). Failed attempts consume this budget.
`shortlist_target_reached` means the target was met; `assessment_limit` is a partial stop; and
`candidate_pool_exhausted` means no more scored candidates remain. Unverified/review-needed jobs
do not count as recommendations. The pool records `disposition`, `evaluation_status` and reasons.
It does not combine API and web results. `--max-assessments` requires `--greenhouse` or
`--discover-greenhouse`. Omitting both retains existing web discovery and its shortlist/research/company limits. Both paths
retain approval/evidence checks, the shared dollar budget and evaluation behavior.
The Greenhouse path checks time before/after collection/screening and before each evaluation;
in-flight batches finish before stopping. Embeddings and Sol can incur cost; CLI runs use the
existing shared ledger and `validation` label. No automatic fallback or retry is performed.
`discovery_error` and `embedding_error` stops are explicitly partial, including total source/provider
failure; `boards_empty` means the supplied boards genuinely returned no postings.

To discover boards from the candidate's existing approved plan instead of supplying tokens:

```powershell
.\.venv\Scripts\python.exe -m jobengine search john-doe-v3 --discover-greenhouse --limit 3 --max-assessments 10
# Optional discovery limits, shown at their defaults:
.\.venv\Scripts\python.exe -m jobengine search john-doe-v3 --discover-greenhouse --limit 3 --max-assessments 10 --board-search-requests 4 --max-boards 12
```

This flag is mutually exclusive with `--greenhouse`. It reuses approved profile/brief fingerprints;
it never regenerates the plan or prompts for renewed approval of unchanged inputs. Discovery-limit
flags require the dynamic flag. Hard maxima are eight search requests and forty board inspections;
failed requests/inspections count. Each search request permits one web tool call and at most one
separate employer-label normalization. Both use the existing search model and shared cost ledger.
The collector reuses verification responses (including empty boards), then the same hybrid screening,
assessment and backfilling run. No duplicate board or individual job-page requests are needed.

`search/greenhouse-discovery.json` retains prompts, actual search queries, sources, candidate board
tokens, observed employer labels, API verification status/errors, deferred boards, limits and discovery
ledger receipts/costs. `usage.json` retains whole-run costs. `board_discovery_empty` means no usable
source boards were found; `board_discovery_error` records search/normalization/API failure;
`board_discovery_unidentified` means boards lack supported employer labels. With no usable mapping,
screening and assessment are skipped. These are not findings that the candidate is unqualified.
Successful boards survive partial verification failures, with warnings and `manifest.status=partial`.
Time/cost exhaustion during discovery remains `time_limit`/`budget_limit`, with the discovery stage
identified in diagnostics. All retained stops exit cleanly (0); inspect `search/run.json` and manifest.

Search checks profile/brief approval, fingerprints and ordered sectors **before constructing the
model client**. The production search checks them again. Search runs are labeled `validation` in
the normal store. Existing candidates, approvals, reviews, and the active-candidate pointer are
retained. The shared ledger continues to enforce the existing budget.

Each run contains:

JSON writes use unique temporary files and atomic replacement. Replacement permission/sharing failures receive
six attempts with 10–160 ms delays (310 ms maximum waiting); no delete-and-rewrite fallback is used.
An exhausted optional diagnostic write is reported on the console and in `failures.json` as
`kind=persistence`, with details in `persistence-failures.json` and the manifest. The research workflow
continues with its actual results, and incomplete diagnostics make the manifest `partial`.
Required search/evaluation exports and saved candidate state remain strict: an exhausted write fails
explicitly, and the terminal command exits nonzero. A final-manifest failure is also reported as failure.

| Artifact | Meaning |
|---|---|
| `manifest.json`, `invocation.json` | Format version, unique execution ID, command, inputs, times and status |
| `events.jsonl`, `events/*.json` | Incrementally written observations in execution order; model calls share a `call_id` |
| `interview/session.json`, `snapshots/*.json` | Original `OnboardingSession` schema; analysis, questions, evidence, attempts, pending answer and sources |
| `interview/transcript.json`, `transcript.txt`, `attempts.json` | Accepted exchanges; text also labels other submissions and pending answers; events retain exact submission order |
| `interview/profile.json`, `evidence.json` | Unconfirmed interview output and extracted evidence |
| `planning/input-profile.json`, `reviewed-profile.json`, `proposed-plan.json`, `approved-profile.json` | Actual planning inputs and review/approval outputs, where completed |
| `search/input-profile.json`, `approved-plan.json` | Exact approved profile and brief used by this execution |
| `search/discovery-handoff.json` | Actual reduced `public_brief` supplied to discovery; raw resumes are excluded from this handoff |
| `search/jobs/<stable-id>.json` | Discovered item, stage-event references, retrieved text/provenance, facts, judgment, ranking and assessment/skip status |
| `search/discovered-jobs.json`, `retrieval-results.json`, `extracted-job-facts.json`, `fit-assessments.json` | Stage indexes as `{job_id, value}` entries; null means that stage produced no output |
| `search/run.json` | Unmodified production `Run` export, including search trace, warnings, stop reason and all inspected jobs |
| `search/greenhouse-discovery.json` | Dynamic mode: search queries/sources, board tokens/labels, verification and deferred statuses, failures, limits, discovery stop reason and ledger costs |
| `search/greenhouse-boards.json` | API path only: per-board fetched/converted/duplicate/failure counts and all original posting provenance |
| `search/greenhouse-pool.json` | API path only: every unique original item/page, raw similarity, adjustment evidence/uncertainty, final rank, selection/deferral reasons and evaluation outcome |
| `search/embedding-usage.json` | API path only, after screening: embedding inputs, token/cost/cache/batch diagnostics; ledger receipts also enter ordinary `usage.json` |
| `failures.json` | Retrieval, validation, model-output validation, provider/model and pipeline events; bounded grounding repairs are recorded too |
| `summary.json`, `summary.txt`, `search/results.txt`, `usage.json` | Counts, per-job classifications, runtime and this execution's ledger receipts |

The candidate `profile.json` and `session.json` are resumable working inputs. The approved
`Profile` export is the reusable search input; plans alone cannot authorize a search. Diagnostic
indexes and event files are not alternative profile formats. Later runs also copy an available
candidate interview session/transcript, so search inputs and prior exchanges can be read together.
An imported profile alone cannot reconstruct questions that were never included in that export.

Model events distinguish the actual provider request payload/schema, raw HTTP response before SDK
parsing (`wire_response`), SDK response (`raw_response`), and schema-validated result (`validated`).
They retain model, reasoning effort, output/tool limits, instructions, stage input, duration and
errors. Requests are correlated with ledger receipt IDs. Provider automatic retries remain disabled;
semantic grounding/citation repairs are separate named calls. Cached calls have a `cache_hit`
configuration/result but no new provider response or charge. Existing caches do not retain the raw
structured response that originally populated them; this cannot be recovered retroactively.

Jobs have stable IDs derived from the production URL identity. Even normalized candidates rejected
by provenance/title validation are retained in diagnostics; accepted candidates beyond a search
limit are marked **not assessed** with a skip reason. Thus diagnostic discovered counts can exceed
the default web path's `Run.search.jobs` count (that list contains only inspected candidates).
The Greenhouse path's `Run.search.jobs` retains the complete unique pool; `inspected_jobs` contains
only evaluated jobs, and per-job `priority` fields retain screening diagnostics. A retrieval
or model failure is never labeled unqualified; the production `unverified` category is preserved.
Validation and technical counts may overlap when different events affect the same job. Failure-event
counts include repairs and multiple events for one root cause; job counts deduplicate by stable ID.

For a retrieval failure, inspect its page error and HTTP event (status/type when an HTTP response
exists). For an extraction failure, inspect the job's model request, wire response and validation
error. For zero assessments, inspect `jobs_not_assessed`, per-job skip reasons, and `stop_reason`.
The terminal reports stage transitions, evidence additions and failures as they happen; global
`--verbose` also prints diagnostic JSON.

## Rerun extraction and assessment without discovery

```powershell
$job = "$searchRun/search/jobs/REPLACE_WITH_STABLE_JOB_ID.json"
$approved = "$searchRun/search/input-profile.json"
# Reuse retrieved content: no discovery or HTTP retrieval. Normal model caches still apply.
.\.venv\Scripts\python.exe -m jobengine evaluate jane-doe --profile $approved --job $job --page $job
# Omit --page to retrieve the job again, using the ordinary six-hour source cache.
.\.venv\Scripts\python.exe -m jobengine evaluate jane-doe --profile $approved --job $job
```

`--job` also accepts standalone `DiscoveryItem` JSON; `--page` accepts standalone `JobPage` JSON.
The page's requested URL must match the job. This reruns the existing combined extraction,
assessment and ranking function; assessment-only replay and forced cache bypass are not provided.
The smallest backend interface addition is an optional saved `JobPage` on `evaluate`; the default
application behavior still fetches normally. No alternate extraction or ranking implementation exists.

## Non-interactive use and storage

```powershell
@('I want to write and coordinate community communications.', '/finish') |
    ConvertTo-Json | Set-Content -Encoding UTF8 .scratch/answers.json
.\.venv\Scripts\python.exe -m jobengine interview jane-doe --answers .scratch/answers.json --non-interactive
# Explicit review/approval attestations, intended only after you inspect these files:
.\.venv\Scripts\python.exe -m jobengine plan jane-doe --profile .scratch/reviewed-profile.json --draft .scratch/reviewed-plan.json --non-interactive --confirm-profile --approve-plan
.\.venv\Scripts\python.exe -m jobengine --help
```

`--non-interactive` never implies approval. Without the explicit flags or an existing confirmed
profile, planning stops at the relevant review gate. Search itself never confirms anything.
Exit 0 means the command completed, paused, or reached a review gate; check `manifest.status`
for `completed`, `paused`, `awaiting_profile_approval`, `awaiting_plan_approval`, or `partial`.
A production research-error/budget stop is `partial`, preserving completed work. Exit 1 is a command
failure and 130 is interruption. Individual posting failures can coexist with a completed search;
inspect the summary and failure records. Argument-parser errors exit before a run is created.

Global options precede the subcommand. `--root .scratch/terminal-debug` relocates candidate state
and diagnostics while preserving the default shared ledger. `--data-dir` or `JOBENGINE_DATA_DIR`
also changes the ledger/cache directory; reserve that for offline tests, not bypassing live budgets.
Tests use fake providers and temporary storage, and never require API access.

Diagnostic files intentionally contain private resume text, answers and model inputs. Defaults are
local, Git-ignored storage. Files request owner-only permissions on systems that implement POSIX
modes; Windows inherits the parent directory's ACL, so use a private local directory. These files are
not encrypted or automatically anonymized. Known secret environment values and secret-shaped keys
are redacted from diagnostics; authentication headers are never collected. Do not publish entire
run folders. Hard termination/power loss may leave `manifest.status=running`; incremental events,
job files and saved pending answers remain available even when final summaries cannot be written.

This interface deliberately leaves retrieval fallback, search quality, latency, missing qualifications
in resume summarization, and the combined extraction/assessment architecture unchanged. Passing
offline checks establishes orchestration behavior, not recommendation quality.
