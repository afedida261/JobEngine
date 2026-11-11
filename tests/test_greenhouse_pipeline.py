"""Greenhouse → real hybrid scorer → existing evaluation, with offline providers."""

from types import SimpleNamespace

import httpx
import pytest

from jobengine.infrastructure.diagnostics import observe
from jobengine.infrastructure.embeddings import DIMENSIONS, MODEL
from jobengine.infrastructure.storage import Store
from jobengine.onboarding.brief import confirm_search_brief
from jobengine.search import pipeline
from jobengine.terminal.artifacts import Artifacts, read_json, write_json
from jobengine.terminal.cli import main, parser


@pytest.fixture
def setup(case, search_brief, tmp_path, monkeypatch):
    profile = case[0].model_copy(deep=True)
    profile.search_brief = confirm_search_brief(profile, search_brief)
    state = SimpleNamespace(
        boards={"first": [1, 2, 3, 4, 5], "second": [2, 6]},
        http=[],
        embedding_requests=[],
        extraction=[],
        embedding_error=False,
        mismatch=False,
    )

    def get(connection, url, **kwargs):
        state.http.append(url)
        token = url.split("/")[-2]
        value = state.boards[token]
        if value == "failed":
            return httpx.Response(503, request=httpx.Request("GET", url))
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "jobs": [
                    {
                        "id": i,
                        "title": "Unrelated role" if i == 1 else "Software engineer",
                        "absolute_url": f"https://boards.greenhouse.io/first/jobs/{i}",
                        "location": {"name": "New York"},
                        "content": f"<h2>Responsibilities</h2><p>{case[2].text}</p><p>{case[2].text}</p><p>Posting reference {i}.</p>",
                    }
                    for i in value
                ]
            },
        )

    def no_network(*args, **kwargs):
        pytest.fail("Unexpected HTTP request")

    monkeypatch.setattr(httpx.Client, "get", get)
    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(pipeline, "fetch_page", no_network)
    monkeypatch.setattr(pipeline, "discover", no_network)
    monkeypatch.setattr(pipeline, "discover_companies", no_network)

    def embed(**kwargs):
        state.embedding_requests.append(kwargs)
        if state.embedding_error is True or (
            state.embedding_error == "first_jobs" and len(state.embedding_requests) == 2
        ):
            raise RuntimeError("Synthetic embedding failure")

        def vector(text):
            result = [0.0] * DIMENSIONS
            result[1 if "Unrelated" in text else 0] = 1.0
            return result

        return SimpleNamespace(
            model=MODEL,
            usage=SimpleNamespace(prompt_tokens=9),
            data=[
                SimpleNamespace(index=i, embedding=vector(text))
                for i, text in enumerate(kwargs["input"])
            ],
        )

    def structured(operation, prompt, data, schema):
        if operation == "extract_job":
            state.extraction.append(data)
            facts = case[3].model_copy(deep=True)
            if state.mismatch:
                facts.company = "Different employer"
            return facts
        assert operation == "evaluate_fit"
        return case[4].model_copy(deep=True)

    client = SimpleNamespace(
        model="fake-search",
        store=Store(tmp_path / "ledger"),
        client=SimpleNamespace(embeddings=SimpleNamespace(create=embed)),
        structured=structured,
    )
    trace = Artifacts(
        tmp_path / "debug", "synthetic", "search", client.store, output=lambda _: None
    )
    return SimpleNamespace(profile=profile, client=client, state=state, trace=trace)


def run(s, **kwargs):
    with observe(s.trace):
        result = pipeline.run_search(
            s.client,
            s.profile,
            greenhouse_boards={"first": "Synthetic company", "second": "Synthetic company"},
            purpose="validation",
            **kwargs,
        )
    s.trace.finish("partial" if result.search.warnings else "completed")
    return result


def test_only_top_n_reach_real_evaluate_with_original_api_descriptions(setup, monkeypatch):
    s = setup
    calls = []
    original = pipeline.evaluate

    def evaluate(client, profile, item, *, page):
        calls.append((item, page))
        return original(client, profile, item, page=page)

    monkeypatch.setattr(pipeline, "evaluate", evaluate)
    result = run(s, limit=3)
    assert len(calls) == len(result.inspected_jobs) == 3
    assert len(result.search.jobs) == 6  # Duplicate #2 retained only once.
    assert [item.url.rsplit("/", 1)[-1] for item, _ in calls] == ["2", "3", "4"]
    pool = read_json(s.trace.path / "search/greenhouse-pool.json")
    assert len(pool) == 6
    assert [row["evaluation_status"] for row in pool].count("completed") == 3
    for row in pool:
        assert "similarity" in row and "adjustment_evidence" in row and "final_score" in row
        if row["status"] == "not_selected_for_batch":
            assert row["deferral_reasons"] and "evaluation" not in row
    for (item, page), extraction in zip(calls, s.state.extraction):
        saved = next(row for row in pool if row["item"]["url"] == item.url)
        assert extraction["source_text"] == page.text == saved["page"]["text"]
        assert "Python programming is required" in page.text
        assert "board=first" in page.extraction_method or "first" in page.extraction_method
    assert len(s.state.http) == 2  # Only board requests, no job-page retrieval.
    assert len(s.state.embedding_requests) == 2
    assert result.search.stop_reason == "shortlist_target_reached"
    assert result.search.strategy == "greenhouse_semantic"
    boards = read_json(s.trace.path / "search/greenhouse-boards.json")
    assert boards[1]["duplicated"] == 1
    assert boards[1]["postings"][0]["board_token"] == "second"
    summary = read_json(s.trace.path / "summary.json")
    assert summary["jobs_discovered"] == summary["jobs_retrieved"] == 6
    assert summary["jobs_assessed"] == 3 and summary["jobs_not_assessed"] == 3
    assert summary["input_tokens"] == 18 and summary["paid_requests"] == 2
    assert all(
        c["operation"].startswith("semantic_embedding:validation:")
        for c in read_json(s.trace.path / "usage.json")
    )


def test_unrelated_employer_rejections_backfill_to_pool_exhaustion(setup):
    setup.state.mismatch = True
    result = run(setup, limit=2)
    assert len(setup.state.extraction) == len(result.inspected_jobs) == 6
    assert result.search.stop_reason == "candidate_pool_exhausted"
    assert not result.jobs
    assert all("Employer identity mismatch:" in job.reasons[0] for job in result.inspected_jobs)


@pytest.fixture
def dynamic(setup):
    """Real discovery/verification/batch/scoring/evaluation with offline model outputs."""
    from jobengine.infrastructure.diagnostics import emit

    s = setup
    s.state.searches = []
    s.state.normalizations = []
    s.state.discovery_sources = [
        {
            "url": "https://job-boards.greenhouse.io/first/jobs/2",
            "title": "Jobs at Synthetic company",
        },
        {"url": "https://boards.greenhouse.io/first", "title": "Jobs at Synthetic company"},
        {"url": "https://boards.greenhouse.io/second", "title": "Jobs at Synthetic company"},
    ]
    s.state.discovery_error = None
    s.state.normalization_error = None
    s.state.simulate_other_run = False
    original = s.client.structured

    def charge(operation):
        receipt = s.client.store.reserve(operation, "fake-search", 0.001)
        emit("model", "request", ledger_receipt=receipt, operation=operation)
        s.client.store.settle(receipt, 0.001, 5, 2, int(operation == "greenhouse_board_discovery"))

    def search(prompt, *, operation, max_tool_calls):
        assert operation == "greenhouse_board_discovery" and max_tool_calls == 1
        s.state.searches.append(prompt)
        if s.state.discovery_error:
            raise s.state.discovery_error
        if s.state.simulate_other_run:
            # A concurrent run may write to the same ledger. Its receipt is not
            # emitted in this diagnostic context and must not enter our costs.
            receipt = s.client.store.reserve(operation, "other-run", 0.25)
            s.client.store.settle(receipt, 0.25, 20, 5, 1)
        charge(operation)
        return {
            "text": "Offline board search",
            "raw": {
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {
                            "queries": ["site:job-boards.greenhouse.io software New York"],
                            "sources": s.state.discovery_sources,
                        },
                    }
                ]
            },
        }

    def structured(operation, prompt, data, schema):
        if operation != "normalize_greenhouse_employers":
            return original(operation, prompt, data, schema)
        s.state.normalizations.append(data)
        if s.state.normalization_error:
            raise s.state.normalization_error
        charge(operation)
        return schema(
            labels=[
                dict(
                    source_url=source["url"],
                    employer="Synthetic company",
                    employer_quote=source["title"],
                )
                for source in data["sources"]
            ]
        )

    s.client.search, s.client.structured = search, structured
    return s


def run_dynamic(s, **options):
    with observe(s.trace):
        run = pipeline.run_search(
            s.client,
            s.profile,
            discover_greenhouse=True,
            board_search_requests=1,
            purpose="validation",
            **options,
        )
    s.trace.finish("partial" if run.search.warnings else "completed")
    return run


def test_dynamic_complete_handoff_reuses_verified_postings_and_shared_costs(dynamic):
    s = dynamic
    s.state.simulate_other_run = True
    before = s.profile.model_dump(mode="json")
    result = run_dynamic(s, limit=3, max_assessments=10, max_boards=2)
    assert result.search.stop_reason == "shortlist_target_reached"
    assert len(result.jobs) == len(result.inspected_jobs) == 3
    assert (
        len(s.state.http) == 2
    )  # Verification responses reused, no batch refetch or job retrieval.
    assert len(s.state.searches) == len(s.state.normalizations) == 1
    assert len(s.state.embedding_requests) == 2 and len(s.state.extraction) == 3
    assert s.profile.model_dump(mode="json") == before  # Existing approvals preserved.
    details = read_json(s.trace.path / "search/greenhouse-discovery.json")
    assert details["boards"] == {"first": "Synthetic company", "second": "Synthetic company"}
    assert len(details["candidates"]["first"]["sources"]) == 2
    assert details["searches"][0]["queries"] == ["site:job-boards.greenhouse.io software New York"]
    assert all(r["status"] == "verified" for r in details["candidates"].values())
    assert details["estimated_or_reserved_usd"] == pytest.approx(0.002)
    assert {c["operation"] for c in details["usage"]} == {
        "greenhouse_board_discovery",
        "normalize_greenhouse_employers",
    }
    assert (
        len(read_json(s.trace.path / "usage.json")) == 4
    )  # Discovery + normalization + two embedding requests.
    pool = read_json(s.trace.path / "search/greenhouse-pool.json")
    assert len(pool) == 6 and all("adjustments" in row for row in pool)
    assert [j.discovery.url.rsplit("/", 1)[-1] for j in result.jobs] == ["2", "3", "4"]


@pytest.mark.parametrize(
    "scenario,stop",
    [
        ("empty_search", "board_discovery_empty"),
        ("failed_search", "board_discovery_error"),
        ("failed_verification", "board_discovery_error"),
        ("empty_boards", "boards_empty"),
    ],
)
def test_dynamic_empty_and_failures_are_distinct_and_never_assessed(dynamic, scenario, stop):
    s = dynamic
    if scenario == "empty_search":
        s.state.discovery_sources = []
    elif scenario == "failed_search":
        s.state.discovery_error = RuntimeError("Offline failure")
    elif scenario == "failed_verification":
        s.state.boards = {"first": "failed", "second": "failed"}
    else:
        s.state.boards = {"first": [], "second": []}
    result = run_dynamic(s, limit=3, max_assessments=10)
    assert result.search.stop_reason == stop
    assert not result.jobs and not result.inspected_jobs
    assert not s.state.embedding_requests and not s.state.extraction
    details = read_json(s.trace.path / "search/greenhouse-discovery.json")
    assert bool(details["boards"]) == (scenario == "empty_boards")
    assert "Stop: " + stop in result.search.summary
    if scenario != "empty_boards":
        assert result.search.warnings
    assert len(s.state.http) == (2 if scenario in {"empty_boards", "failed_verification"} else 0)


def test_dynamic_partial_verification_preserves_success_and_reports_failure(dynamic):
    s = dynamic
    s.state.boards["second"] = "failed"
    result = run_dynamic(s, limit=3, max_assessments=10)
    assert len(result.jobs) == 3 and result.search.stop_reason == "shortlist_target_reached"
    assert result.search.warnings and any("second" in w for w in result.search.warnings)
    details = read_json(s.trace.path / "search/greenhouse-discovery.json")
    assert details["candidates"]["second"]["status"] == "failed"
    assert len(s.state.http) == 2
    assert read_json(s.trace.path / "manifest.json")["status"] == "partial"


@pytest.mark.parametrize("stage", ["search", "normalization"])
def test_dynamic_budget_exhaustion_retains_diagnostics_without_downstream_calls(dynamic, stage):
    s = dynamic
    s.client.store.budget = 0 if stage == "search" else 0.0015
    result = run_dynamic(s, limit=3, max_assessments=10)
    assert result.search.stop_reason == "budget_limit"
    assert not s.state.http and not s.state.embedding_requests and not s.state.extraction
    details = read_json(s.trace.path / "search/greenhouse-discovery.json")
    assert details["stop_reason"] == "budget_limit"
    assert details["estimated_or_reserved_usd"] == (0 if stage == "search" else 0.001)
    assert not result.inspected_jobs


def test_dynamic_assessment_budget_and_deferred_boards(dynamic):
    s = dynamic
    s.state.mismatch = True
    result = run_dynamic(s, limit=3, max_assessments=2, max_boards=1)
    assert result.search.stop_reason == "assessment_limit" and len(result.inspected_jobs) == 2
    assert not result.jobs and len(s.state.http) == 1
    details = read_json(s.trace.path / "search/greenhouse-discovery.json")
    assert details["candidates"]["second"]["status"] == "deferred"
    assert details["limits"]["board_inspections"] == 1


def test_dynamic_backfills_to_recommendation_target(dynamic, monkeypatch):
    original = pipeline.evaluate
    calls = []

    def evaluate(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(args[2].url)
        return result.model_copy(update={"category": "excluded" if len(calls) <= 3 else "weaker"})

    monkeypatch.setattr(pipeline, "evaluate", evaluate)
    result = run_dynamic(dynamic, limit=3, max_assessments=10)
    assert len(calls) == 6 and len(result.jobs) == 3
    assert result.search.stop_reason == "shortlist_target_reached"


@pytest.mark.parametrize("scenario", ["success", "empty", "failure", "budget"])
def test_dynamic_cli_reuses_approvals_and_writes_diagnostics(dynamic, tmp_path, scenario):
    s = dynamic
    if scenario == "empty":
        s.state.discovery_sources = []
    elif scenario == "failure":
        s.state.boards = {"first": "failed", "second": "failed"}
    elif scenario == "budget":
        s.client.store.budget = 0
    root = tmp_path / "dynamic-cli"
    write_json(root / "candidates/synthetic/candidate.json", {"id": "synthetic"})
    profile_path = root / "candidates/synthetic/profile.json"
    write_json(profile_path, s.profile.model_dump(mode="json"))
    before = profile_path.read_bytes()
    output = []
    code = main(
        [
            "--root",
            str(root),
            "--data-dir",
            str(s.client.store.root),
            "search",
            "synthetic",
            "--discover-greenhouse",
            "--limit",
            "3",
            "--max-assessments",
            "10",
            "--board-search-requests",
            "1",
            "--max-boards",
            "2",
        ],
        client_factory=lambda store: s.client,
        input_fn=lambda _: pytest.fail("No approval prompts"),
        output=output.append,
    )
    assert code == 0, output
    assert profile_path.read_bytes() == before
    path = next((root / "runs/synthetic").iterdir())
    assert read_json(path / "manifest.json")["status"] == (
        "completed" if scenario == "success" else "partial"
    )
    saved = read_json(path / "search/run.json")
    assert len(saved["jobs"]) == (3 if scenario == "success" else 0)
    assert (
        saved["search"]["stop_reason"]
        == {
            "success": "shortlist_target_reached",
            "empty": "board_discovery_empty",
            "failure": "board_discovery_error",
            "budget": "budget_limit",
        }[scenario]
    )
    assert any(saved["search"]["stop_reason"] in line for line in output)
    assert read_json(path / "search/greenhouse-discovery.json")["limits"]["search_requests"] == 1
    assert len(s.state.searches) == 1
    assert len(s.state.http) == (2 if scenario in {"success", "failure"} else 0)


def test_dynamic_time_budget_stops_before_api_or_normalization(dynamic, monkeypatch):
    clock = [0]
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])
    original = dynamic.client.search

    def search(*args, **kwargs):
        value = original(*args, **kwargs)
        clock[0] = 61
        return value

    dynamic.client.search = search
    result = run_dynamic(dynamic, max_minutes=1, max_assessments=10)
    assert result.search.stop_reason == "time_limit"
    assert not dynamic.state.http and not dynamic.state.normalizations
    assert not dynamic.state.embedding_requests
    details = read_json(dynamic.trace.path / "search/greenhouse-discovery.json")
    assert details["stop_reason"] == "time_limit"
    assert all(r["status"] == "deferred" for r in details["candidates"].values())


def test_dynamic_backend_option_validation_precedes_work(dynamic):
    with pytest.raises(ValueError, match="not both"):
        pipeline.run_search(
            dynamic.client,
            dynamic.profile,
            discover_greenhouse=True,
            greenhouse_boards={"first": "Synthetic company"},
        )
    with pytest.raises(ValueError, match="require discover_greenhouse"):
        pipeline.run_search(dynamic.client, dynamic.profile, board_search_requests=2)
    with pytest.raises(ValueError, match="max_boards"):
        pipeline.run_search(
            dynamic.client, dynamic.profile, discover_greenhouse=True, max_boards=41
        )
    assert not dynamic.state.searches and not dynamic.state.http


def test_dynamic_flags_are_explicit_and_mutually_exclusive():
    args = parser().parse_args(
        [
            "search",
            "john-doe-v3",
            "--discover-greenhouse",
            "--limit",
            "3",
            "--max-assessments",
            "10",
        ]
    )
    assert args.discover_greenhouse and args.greenhouse is None
    assert args.board_search_requests is None and args.max_boards is None
    with pytest.raises(SystemExit):
        parser().parse_args(
            ["search", "synthetic", "--discover-greenhouse", "--greenhouse", "first=First"]
        )


@pytest.mark.parametrize("failure", ["board", "embedding", "empty", "budget"])
def test_failures_are_explicit_and_discovered_pool_is_retained(setup, failure):
    s = setup
    if failure == "board":
        s.state.boards["second"] = "failed"
    elif failure == "embedding":
        s.state.embedding_error = True
    elif failure == "empty":
        s.state.boards = {"first": [], "second": []}
    else:
        s.client.store.budget = 0
    result = run(s, limit=2)
    assert (
        result.search.stop_reason
        == {
            "board": "shortlist_target_reached",
            "embedding": "embedding_error",
            "empty": "boards_empty",
            "budget": "budget_limit",
        }[failure]
    )
    pool = read_json(s.trace.path / "search/greenhouse-pool.json")
    assert len(pool) == len(result.search.jobs)
    assert bool(result.search.warnings) == (failure != "empty")
    assert len(result.inspected_jobs) == (2 if failure == "board" else 0)
    if failure in {"embedding", "budget"}:
        assert len(pool) == 6 and all(row["similarity"] is None for row in pool)


def test_approval_guard_precedes_all_api_and_embedding_requests(setup):
    setup.profile.interests = "Changed without approval"
    with pytest.raises(ValueError):
        run(setup, limit=1)
    assert not setup.state.http and not setup.state.embedding_requests


def test_partial_job_embedding_failure_evaluates_only_scored_jobs(setup):
    s = setup
    s.state.boards = {"first": list(range(1, 41)), "second": []}
    s.state.embedding_error = "first_jobs"
    result = run(s, limit=3)
    assert result.search.stop_reason == "shortlist_target_reached"
    assert result.search.warnings
    assert len(result.inspected_jobs) == 3
    pool = read_json(s.trace.path / "search/greenhouse-pool.json")
    assert len(pool) == 40 and sum(row["similarity"] is None for row in pool) == 32
    assert all(int(job.discovery.url.rsplit("/", 1)[-1]) > 32 for job in result.inspected_jobs)


def test_all_boards_failing_is_not_reported_as_empty_results(setup):
    setup.state.boards = {"first": "failed", "second": "failed"}
    result = run(setup, limit=3)
    assert result.search.stop_reason == "discovery_error"
    assert len(result.search.warnings) == 2 and not result.inspected_jobs
    assert not setup.state.embedding_requests


def test_evaluation_budget_stop_preserves_completed_results_and_pending_pool(setup, monkeypatch):
    from jobengine.infrastructure.storage import BudgetExceeded

    s = setup
    original = pipeline.evaluate
    calls = []

    def evaluate(*args, **kwargs):
        calls.append(args[2].url)
        if len(calls) == 2:
            raise BudgetExceeded("Synthetic budget limit")
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "evaluate", evaluate)
    result = run(s, limit=3)
    assert result.search.stop_reason == "budget_limit" and len(result.inspected_jobs) == 1
    pool = read_json(s.trace.path / "search/greenhouse-pool.json")
    assert len(pool) == 6
    assert pool[0]["evaluation_status"] == "completed"
    assert pool[1]["evaluation_status"] == "failed"
    assert pool[1]["evaluation_error"] == "BudgetExceeded"
    assert pool[2]["evaluation_status"] == "not_evaluated"


def test_time_limit_after_screening_preserves_pool_without_evaluations(setup, monkeypatch):
    s = setup
    ticks = iter([0, 0, 0, 61])
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: next(ticks, 61))
    result = run(s, limit=2, max_minutes=1)
    assert result.search.stop_reason == "time_limit"
    assert not result.inspected_jobs
    pool = read_json(s.trace.path / "search/greenhouse-pool.json")
    assert len(pool) == 6 and any(row["similarity"] is not None for row in pool)
    assert all(
        "time_limit" in " ".join(row["deferral_reasons"])
        for row in pool
        if row["status"] == "selected_for_evaluation"
    )


@pytest.mark.parametrize("partial", [False, True])
def test_cli_repeated_board_option_reaches_pipeline_and_saves_artifacts(setup, tmp_path, partial):
    s = setup
    if partial:
        s.state.boards["second"] = "failed"
    root = tmp_path / "terminal"
    write_json(root / "candidates/synthetic/candidate.json", {"id": "synthetic"})
    write_json(root / "candidates/synthetic/profile.json", s.profile.model_dump(mode="json"))
    output = []
    code = main(
        [
            "--root",
            str(root),
            "--data-dir",
            str(s.client.store.root),
            "search",
            "synthetic",
            "--limit",
            "3",
            "--max-assessments",
            "10",
            "--greenhouse",
            "first=Synthetic company",
            "--greenhouse",
            "second=Synthetic company",
        ],
        client_factory=lambda store: s.client,
        output=output.append,
    )
    assert code == 0, output
    path = next((root / "runs/synthetic").iterdir())
    assert read_json(path / "manifest.json")["status"] == ("partial" if partial else "completed")
    assert len(read_json(path / "search/greenhouse-pool.json")) == (5 if partial else 6)
    assert len(read_json(path / "search/run.json")["inspected_jobs"]) == 3
    assert read_json(path / "invocation.json")["max_assessments"] == 10


def test_cli_board_argument_requires_mapping():
    with pytest.raises(SystemExit):
        parser().parse_args(["search", "synthetic", "--greenhouse", "missing-employer"])


def test_backfill_beyond_three_to_three_recommendations(setup, monkeypatch):
    original = pipeline.evaluate
    calls = []

    def evaluate(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(args[2].url)
        return result.model_copy(update={"category": "excluded" if len(calls) <= 3 else "weaker"})

    monkeypatch.setattr(pipeline, "evaluate", evaluate)
    result = run(setup, limit=3, max_assessments=10)
    assert len(calls) == 6 and len(result.jobs) == 3
    assert result.search.stop_reason == "shortlist_target_reached"
    pool = read_json(setup.trace.path / "search/greenhouse-pool.json")
    assert [r["disposition"] for r in pool].count("recommended") == 3
    assert [r["disposition"] for r in pool].count("excluded") == 3


def test_assessment_cap_is_distinct_from_recommendation_target(setup):
    setup.state.mismatch = True
    result = run(setup, limit=3, max_assessments=4)
    assert len(result.inspected_jobs) == 4 and not result.jobs
    assert result.search.stop_reason == "assessment_limit"
    pool = read_json(setup.trace.path / "search/greenhouse-pool.json")
    assert sum(r["disposition"] == "deferred" for r in pool) == 2
    assert all(
        "assessment_limit" in " ".join(r["deferral_reasons"])
        for r in pool
        if r["disposition"] == "deferred"
    )


@pytest.mark.parametrize("invalid", [0, -1, 21, True, 1.5])
def test_global_assessment_cap_cannot_be_exceeded(setup, invalid):
    with pytest.raises(ValueError, match="max_assessments"):
        run(setup, max_assessments=invalid)
    assert not setup.state.http


def test_default_assessment_cap_is_global_inspection_limit(setup):
    setup.state.boards = {"first": list(range(1, 26)), "second": []}
    setup.state.mismatch = True
    result = run(setup, limit=3)
    assert len(result.inspected_jobs) == pipeline.MAX_INSPECTIONS == 20
    assert result.search.stop_reason == "assessment_limit" and not result.jobs


def test_unverified_review_and_exception_never_count_as_recommendations(setup, monkeypatch):
    original = pipeline.evaluate
    count = 0

    def evaluate(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise OSError("Synthetic posting failure")
        return original(*args, **kwargs).model_copy(
            update={
                "category": "unverified"
                if count == 2
                else "needs_review"
                if count == 3
                else "strong"
            }
        )

    monkeypatch.setattr(pipeline, "evaluate", evaluate)
    result = run(setup, limit=3, max_assessments=6)
    assert count == 6 and len(result.jobs) == 3
    assert result.search.stop_reason == "shortlist_target_reached" and result.search.warnings
    pool = read_json(setup.trace.path / "search/greenhouse-pool.json")
    assert pool[0]["disposition"] == pool[1]["disposition"] == "failed"
    assert pool[2]["disposition"] == "not_recommended"


def test_time_budget_stops_backfill_after_inflight_evaluation(setup, monkeypatch):
    clock = [0]
    original = pipeline.evaluate
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])

    def evaluate(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 61
        return result.model_copy(update={"category": "excluded"})

    monkeypatch.setattr(pipeline, "evaluate", evaluate)
    result = run(setup, limit=3, max_assessments=10, max_minutes=1)
    assert len(result.inspected_jobs) == 1 and not result.jobs
    assert result.search.stop_reason == "time_limit"
