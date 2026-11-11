# JobEngine agent context

JobEngine is a local job-discovery prototype for applicants in any field. Its workflow is resume
intake, a short interview, profile review, search-plan approval, discovery, assessment, and results.

## Start here

- Read [TODO.md](TODO.md) before choosing work. It is the current priority list and includes the
  user's latest ideas; proposals there are not automatically requests to implement them.
- Before changing module interfaces, imports, or the UI framework, read
  [docs/architecture.md](docs/architecture.md). The codebase cleanup is complete; use the new package
  paths rather than restoring the old flat modules.
- For setup, local storage, and diagnostic commands, read [README.md](README.md).

## Current direction

- The current functional step is the interview/search model split in TODO. Test that configuration
  and discuss the next step before broader agent separation or a reflection loop.
- The applicant UI now uses Django. Finish visual verification and a credible applicant demo;
  public-hosting requirements remain in TODO and README.
- Search/matching fixes and time limits are explicitly deferred unless the user redirects the task.
  Separate interview/search agents and a reflection loop are exploratory ideas, not settled designs.
- The company-facing demo remains wanted after the applicant demo. Its scope is retained in TODO.
- Search quality is unresolved. Jane's synthetic validation produced no strong/weaker matches and
  took about thirteen minutes. Passing engineering checks does not establish recommendation quality.
  For search-quality work, read the failure list in TODO and the
  [Jane validation report](.scratch/jane-doe/report.md) before selecting a fix.

## Implementation conventions

- Keep Django views, forms, templates, assets, and browser workspaces in `jobengine/web/`; backend
  modules remain independent of the UI. Root `manage.py` launches Django. Retain `jobengine/ui/` for
  migration regression checks; its Streamlit entry point is `ui/legacy_app.py`.
- Put model instructions in [jobengine/prompts.py](jobengine/prompts.py), with request/reasoning/cache
  configuration in [llm.py](jobengine/infrastructure/llm.py). Preserve Terra with low interview
  reasoning and Sol with low search reasoning during unrelated work. Templates must support
  general applicants.
- Preserve saved JSON compatibility and profile/brief approval fingerprints when changing schemas
  or workflow state. Profile or brief edits require renewed approval before discovery.
- Keep current documents concise. Historical plans and candidate-specific scripts in
  `.scratch/archive/` are reference material with potentially obsolete imports and instructions.

## Data and validation

- Preserve `.env`, the existing `data/` ledger, saved candidates, runs, and reviews. Keep credentials
  and private candidate content out of logs and source files.
- Use fake model clients and isolated `JOBENGINE_DATA_DIR` storage for automated tests. For requested
  live validation, isolate candidate state, share the existing cost ledger/budget, label the run
  `validation`, and retain the real candidate's active record.
- Keep fictional resumes, live-run artifacts, and throwaway scripts under `.scratch/`; tests should
  be self-contained. John's supplied resume is at
  [.scratch/john-doe/John_Doe_Resume.docx](.scratch/john-doe/John_Doe_Resume.docx).
  Jane's resume, exact interview transcript, approved plan, and run export are in `.scratch/jane-doe/`.
- Report retrieval failures, uncertain eligibility, and ranking disagreements separately. Retain
  source links and caveats; neither fluent explanations nor a numerical score proves a good match.

## Checks

Use the existing Windows virtual environment. Development dependencies are in `requirements-dev.txt`.
After application changes, run the relevant tests and these repository checks:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

These checks are offline. Pytest collects `tests/`; Ruff excludes `.scratch/`. For documentation-only
changes, verify links and consistency with the current code and TODO. Update the owning documentation
when interfaces or priorities change rather than adding another status snapshot.
