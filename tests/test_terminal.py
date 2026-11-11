"""Terminal orchestration exercises real services with isolated files and fake providers."""

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from jobengine.infrastructure.diagnostics import observe
from jobengine.infrastructure.llm import ModelClient, _observe_response
from jobengine.infrastructure.storage import Store
from jobengine.models import (
    CompanyCandidate,
    CompanyOutput,
    DiscoveryOutput,
    OnboardingAnalysis,
    Question,
    SavedOnboarding,
)
from jobengine.onboarding.brief import confirm_search_brief
from jobengine.onboarding.interview import start_onboarding
from jobengine.search import pipeline
from jobengine.terminal.artifacts import Artifacts, read_json, write_json
from jobengine.terminal.cli import main


class FakeClient:
    model = "fake-search"

    def __init__(self, store, case, brief):
        self.store, self.case, self.brief = store, case, brief
        self.operations = []
        self.broken = False
        self.mixed = False
        self.no_questions = False
        self.items = [
            case[1].model_copy(update={"url": f"https://example.com/job/{i}"}) for i in range(4)
        ]

    def structured(self, operation, instructions, data, schema):
        self.operations.append(operation)
        if operation == "onboarding_answer_check":
            return schema(disposition="relevant")
        if operation == "onboarding_interview":
            return OnboardingAnalysis(
                summary="Synthetic background",
                evidence=self.case[0].evidence,
                conflicts=[],
                missing_information=[],
                suggested_roles=self.case[0].target_roles,
                suggested_interests=self.case[0].interests,
                preference_suggestions=[],
                next_question=None
                if data["history"] or self.no_questions
                else Question(question="What do you want to work on?", purpose="Goals"),
            )
        if operation == "onboarding_search_brief":
            brief = self.brief.model_copy(deep=True)
            return schema(plan=brief, uncertainties=[]) if "plan" in schema.model_fields else brief
        if operation == "normalize_companies":
            return CompanyOutput(
                companies=[
                    CompanyCandidate(
                        name=self.case[3].company,
                        official_url="https://example.com/",
                        careers_url=None,
                        sector_rationale="Synthetic",
                    )
                ]
            )
        if operation == "normalize_discovery":
            return DiscoveryOutput(jobs=self.items)
        if operation == "extract_job":
            index = data["url"].rsplit("/", 1)[-1]
            if self.mixed and index == "3":
                return schema.model_validate({"invalid": True})
            facts = self.case[3].model_copy(deep=True)
            if self.mixed and index == "2":
                facts.company = "Different employer"
            return facts
        if operation == "evaluate_fit":
            if self.broken:
                raise OSError("Synthetic interrupted evaluation")
            judgment = self.case[4].model_copy(deep=True)
            constraints = {c["id"] for c in data["constraints"]}
            judgment.constraints = [
                c for c in judgment.constraints if c.constraint_id in constraints
            ]
            return judgment
        raise AssertionError(operation)

    def search(self, prompt, *, operation, max_tool_calls):
        self.operations.append(operation)
        sources = (
            [{"url": "https://example.com/", "title": "Synthetic company"}]
            if operation == "company_discovery"
            else [{"url": i.url, "title": i.title} for i in self.items]
        )
        return {
            "text": "Synthetic sources",
            "raw": {
                "output": [
                    {"type": "web_search_call", "action": {"type": "search", "sources": sources}}
                ]
            },
        }


@pytest.fixture
def terminal(tmp_path, case, search_brief, monkeypatch):
    root, data = tmp_path / "terminal", tmp_path / "ledger"
    client = FakeClient(Store(data), case, search_brief)
    output = []

    def invoke(*args, answers=()):
        values = iter(answers)
        return main(
            ["--data-dir", str(data), "--root", str(root), *map(str, args)],
            client_factory=lambda store: client,
            input_fn=lambda prompt: next(values),
            output=output.append,
        )

    def runs(command):
        return [
            p.parent
            for p in (root / "runs").glob("*/*/manifest.json")
            if read_json(p)["command"] == command
        ]

    def fetch(url, store):
        return case[2].model_copy(update={"url": url, "requested_url": url})

    monkeypatch.setattr(pipeline, "fetch_page", fetch)
    profile_path = tmp_path / "initial-profile.json"
    profile = case[0].model_copy(deep=True)
    profile.confirmed = False
    write_json(profile_path, profile.model_dump(mode="json"))
    assert invoke("candidate", "synthetic", "--profile", profile_path) == 0
    return SimpleNamespace(
        root=root,
        data=data,
        client=client,
        invoke=invoke,
        runs=runs,
        profile_path=profile_path,
        output=output,
    )


def test_resume_only_interview_still_asks_when_model_returns_no_question(terminal):
    t = terminal
    t.client.no_questions = True
    assert t.invoke("interview", "synthetic", answers=["/pause"]) == 0
    assert any("[terra]" in line and "Question 1:" in line for line in t.output)
    session = read_json(t.root / "candidates/synthetic/session.json")
    assert not session["interview_finished"]
    assert session["analysis"]["next_question"]["topic"] == "desired_work"


def test_core_questions_continue_when_model_never_proposes_one(terminal):
    t = terminal
    t.client.no_questions = True
    assert (
        t.invoke(
            "interview",
            "synthetic",
            answers=[
                "I want to write useful content.",
                "I want to learn campaign planning.",
                "I enjoy collaborating with colleagues.",
                "I am interested in community organizations.",
            ],
        )
        == 0
    )
    session = read_json(t.root / "candidates/synthetic/session.json")
    assert [exchange["topic"] for exchange in session["history"]] == [
        "desired_work",
        "career_goals",
        "work_style",
        "sectors",
    ]
    assert session["interview_finished"]
    assert not read_json(t.root / "candidates/synthetic/profile.json")["confirmed"]


def test_restart_recovers_empty_legacy_interview_and_invalidates_approval(terminal, case):
    t = terminal
    t.client.no_questions = True
    legacy = start_onboarding(t.client, case[0].documents)
    assert legacy.interview_finished and not legacy.history and legacy.intake is None
    path = t.root / "candidates/synthetic"
    write_json(path / "session.json", legacy.model_dump(mode="json"))
    assert (
        t.invoke("plan", "synthetic", "--non-interactive", "--confirm-profile", "--approve-plan")
        == 0
    )
    approved = read_json(path / "profile.json")
    assert t.invoke("interview", "synthetic", answers=["/pause"]) == 0
    assert any("--restart" in line for line in t.output)
    assert read_json(path / "profile.json") == approved
    assert t.invoke("interview", "synthetic", "--restart", answers=["/pause"]) == 0
    session = read_json(path / "session.json")
    assert session["analysis"]["next_question"]["topic"] == "desired_work"
    assert "authorized_to_work" not in session["intake"]
    assert not session["interview_finished"]
    assert read_json(path / "profile.json")["search_brief"] is None
    assert not read_json(path / "profile.json")["confirmed"]
    assert any((run / "interview/previous-session.json").exists() for run in t.runs("interview"))


def test_interactive_interview_plan_and_repeated_search(terminal):
    t = terminal
    assert (
        t.invoke(
            "interview", "synthetic", answers=["I want to build better search systems.", "/finish"]
        )
        == 0
    )
    interview = t.runs("interview")[0]
    transcript = read_json(interview / "interview/transcript.json")
    assert transcript[0]["answer"] == "I want to build better search systems."
    assert read_json(interview / "interview/session.json")["documents"][-1]["id"] == "A1"
    assert read_json(interview / "interview/evidence.json")
    assert not read_json(interview / "interview/profile.json")["confirmed"]
    assert t.invoke("plan", "synthetic", answers=["y", "Y"]) == 0
    approved = t.root / "candidates/synthetic/profile.json"
    payload = read_json(approved)
    assert payload["search_brief"]["confirmation_fingerprint"]
    terra_calls = t.client.operations.count("onboarding_interview")
    for _ in range(2):
        assert t.invoke("search", "synthetic", "--profile", approved, "--limit", "1") == 0
    assert t.client.operations.count("onboarding_interview") == terra_calls
    assert len(t.runs("search")) == 2
    for run in t.runs("search"):
        summary = read_json(run / "summary.json")
        assert summary["jobs_discovered"] == 4
        # Undeclared intake remains unknown, so the production matcher keeps
        # inspecting instead of claiming a confirmed shortlist match.
        assert summary["jobs_assessed"] == 4
        assert summary["jobs_not_assessed"] == 0
        assert (run / "interview/transcript.txt").exists()
        assert read_json(run / "search/input-profile.json") == payload
        assert "documents" not in read_json(run / "search/discovery-handoff.json")
        assert read_json(run / "search/run.json")["purpose"] == "validation"


def test_noninteractive_approval_and_draft_reuse_do_not_bypass_review(terminal):
    t = terminal
    assert t.invoke("plan", "synthetic", "--non-interactive") == 0
    assert t.client.operations == []
    assert t.invoke("search", "synthetic") == 1
    assert t.client.operations == []
    assert t.invoke("plan", "synthetic", "--non-interactive", "--confirm-profile") == 0
    proposed = next(
        p / "planning/proposed-plan.json"
        for p in t.runs("plan")
        if (p / "planning/proposed-plan.json").exists()
    )
    assert t.invoke("search", "synthetic") == 1
    calls = list(t.client.operations)
    assert (
        t.invoke("plan", "synthetic", "--draft", proposed, "--non-interactive", "--approve-plan")
        == 0
    )
    assert t.client.operations == calls
    assert read_json(t.root / "candidates/synthetic/profile.json")["search_brief"]["confirmed"]


@pytest.mark.parametrize("edit", ["profile", "brief", "unconfirm"])
def test_search_rejects_stale_or_missing_approvals_before_client(terminal, edit):
    t = terminal
    assert (
        t.invoke("plan", "synthetic", "--non-interactive", "--confirm-profile", "--approve-plan")
        == 0
    )
    path = t.root / "candidates/synthetic/profile.json"
    value = read_json(path)
    if edit == "profile":
        value["interests"] += " edited"
    elif edit == "brief":
        value["search_brief"]["search_queries"] = ["a different query"]
    else:
        value["confirmed"] = False
    write_json(path, value)
    calls = list(t.client.operations)
    assert t.invoke("search", "synthetic") == 1
    assert t.client.operations == calls
    assert read_json(t.runs("search")[0] / "manifest.json")["status"] == "failed"
    assert (t.runs("search")[0] / "summary.txt").exists()


def test_success_retrieval_validation_and_invalid_model_outputs(terminal, monkeypatch, case):
    t = terminal
    t.client.mixed = True

    def fetch(url, store):
        return case[2].model_copy(
            update={
                "url": url,
                "requested_url": url,
                "status": "failed" if url.endswith("/1") else "retrieved",
                "error": "HTTP 403" if url.endswith("/1") else None,
            }
        )

    monkeypatch.setattr(pipeline, "fetch_page", fetch)
    t.invoke("plan", "synthetic", "--non-interactive", "--confirm-profile", "--approve-plan")
    assert t.invoke("search", "synthetic", "--limit", "10") == 0
    run = t.runs("search")[0]
    summary = read_json(run / "summary.json")
    assert summary["jobs_discovered"] == 4
    assert summary["jobs_retrieved"] == 3
    assert summary["jobs_extracted"] == 2
    assert summary["jobs_assessed"] == 1
    assert summary["jobs_rejected_by_validation"] == 1
    failures = read_json(run / "failures.json")
    assert any(f["kind"] == "retrieval" and f["reason"] == "HTTP 403" for f in failures)
    assert any(f["reason"].startswith("Employer identity mismatch:") for f in failures)
    assert any(f.get("error_type") == "ValidationError" for f in failures)
    jobs = [read_json(p) for p in (run / "search/jobs").glob("*.json")]
    assert len({j["job_id"] for j in jobs}) == 4
    assert sum(j["assessment_status"] == "not assessed" for j in jobs) == 3
    assert all(j["result"]["category"] == "unverified" for j in jobs if not j["assessed"])


def test_unexpected_failure_retains_all_discovered_jobs_and_summary(terminal):
    t = terminal
    t.client.broken = True
    t.invoke("plan", "synthetic", "--non-interactive", "--confirm-profile", "--approve-plan")
    assert t.invoke("search", "synthetic") == 0
    run = t.runs("search")[0]
    assert read_json(run / "manifest.json")["status"] == "partial"
    assert read_json(run / "summary.json")["jobs_discovered"] == 4
    assert read_json(run / "summary.json")["jobs_assessed"] == 0
    assert read_json(run / "failures.json")[-1]["reason"] == "Synthetic interrupted evaluation"
    assert read_json(run / "search/run.json")["search"]["stop_reason"] == "research_error"


def test_saved_page_re_evaluation_never_discovers_or_retrieves(
    terminal, case, monkeypatch, tmp_path
):
    t = terminal
    t.invoke("plan", "synthetic", "--non-interactive", "--confirm-profile", "--approve-plan")
    job = tmp_path / "job.json"
    write_json(job, dict(discovery=case[1].model_dump(), page=case[2].model_dump()))
    monkeypatch.setattr(pipeline, "fetch_page", lambda *a: pytest.fail("Unexpected HTTP"))
    t.client.operations.clear()
    assert t.invoke("evaluate", "synthetic", "--job", job, "--page", job) == 0
    assert t.client.operations == ["extract_job", "evaluate_fit"]
    assert read_json(t.runs("evaluate")[0] / "summary.json")["jobs_assessed"] == 1


def test_named_candidates_and_legacy_active_record_remain_isolated(terminal, case):
    t = terminal
    ledger = Store(t.data)
    record = ledger.save_onboarding(
        SavedOnboarding(session=start_onboarding(t.client, case[0].documents))
    )
    before = ledger.load_onboarding().model_dump(mode="json")
    assert t.invoke("candidate", "second", "--profile", t.profile_path) == 0
    assert t.invoke("interview", "second", "--non-interactive") == 0
    assert not (t.root / "candidates/synthetic/session.json").exists()
    assert ledger.load_onboarding(record.session.id).model_dump(mode="json") == before
    assert ledger.load_onboarding().model_dump(mode="json") == before


def test_resume_registration_and_duplicate_protection(terminal, tmp_path):
    t = terminal
    resume = tmp_path / "resume.txt"
    resume.write_text(
        "Fictional applicant with experience coordinating public events.", encoding="utf-8"
    )
    assert t.invoke("candidate", "new-fixture", "--resume", resume, "--role", "Coordinator") == 0
    config = read_json(t.root / "candidates/new-fixture/candidate.json")
    assert config["role_hint"] == "Coordinator"
    assert config["documents"][0]["text"] == resume.read_text()
    assert t.invoke("candidate", "new-fixture", "--resume", resume) == 1


def test_guided_fallback_is_visible_and_pending_answer_is_saved(terminal):
    t = terminal

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic provider outage")

    t.client.structured = fail
    assert (
        t.invoke("interview", "synthetic", answers=["I enjoy outreach and writing.", "/finish"])
        == 0
    )
    run = t.runs("interview")[0]
    assert read_json(run / "interview/session.json")["interview_mode"] == "guided"
    assert read_json(run / "failures.json")[0]["reason"] == "Synthetic provider outage"
    assert (
        read_json(run / "interview/transcript.json")[0]["answer"] == "I enjoy outreach and writing."
    )


def test_wire_output_survives_schema_failure_and_secrets_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-synthetic-secret-123456")
    store = Store(tmp_path / "ledger")
    trace = Artifacts(tmp_path / "debug", "fake", "test", store, output=lambda value: None)
    payload = {
        "id": "resp_fake",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": "gpt-6.1-terra",
        "output": [
            {
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"invalid": "sk-synthetic-secret-123456"}',
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with observe(trace):
        client = ModelClient("fake-api-key", store)
        client.client.close()
        client.client = OpenAI(
            api_key="fake-api-key",
            max_retries=0,
            http_client=httpx.Client(
                transport=transport, event_hooks={"response": [_observe_response]}
            ),
        )
        with pytest.raises(ValueError):
            client.structured(
                "onboarding_interview",
                "Synthetic instructions",
                {"text": "synthetic"},
                OnboardingAnalysis,
            )
        client.client.close()
    trace.finish("failed")
    events = [json.loads(line) for line in (trace.path / "events.jsonl").read_text().splitlines()]
    assert any(e["outcome"] == "wire_response" for e in events)
    assert any(e["outcome"] == "failed" and e["call_id"] for e in events)
    assert "sk-synthetic-secret-123456" not in (trace.path / "events.jsonl").read_text()
    usage = read_json(trace.path / "usage.json")
    assert len(usage) == 1 and usage[0]["state"] == "reserved"
    assert read_json(trace.path / "summary.json")["paid_requests"] == 1


def test_existing_approval_format_is_reused(case, search_brief, terminal):
    t = terminal
    profile = case[0]
    profile.search_brief = confirm_search_brief(profile, search_brief)
    write_json(t.profile_path, profile.model_dump(mode="json"))
    assert t.invoke("search", "synthetic", "--profile", t.profile_path, "--limit", "1") == 0
    assert "onboarding_search_brief" not in t.client.operations


def test_replanning_invalidates_previous_approval_until_new_plan_is_approved(terminal):
    t = terminal
    assert (
        t.invoke("plan", "synthetic", "--non-interactive", "--confirm-profile", "--approve-plan")
        == 0
    )
    assert t.invoke("plan", "synthetic", "--non-interactive") == 0
    assert read_json(t.root / "candidates/synthetic/profile.json")["search_brief"] is None
    calls = list(t.client.operations)
    assert t.invoke("search", "synthetic") == 1
    assert t.client.operations == calls


def test_noninteractive_answer_file_pauses_then_resumes(terminal, tmp_path):
    t = terminal
    answers = tmp_path / "answers.json"
    write_json(answers, [])
    assert t.invoke("interview", "synthetic", "--non-interactive", "--answers", answers) == 0
    assert read_json(t.runs("interview")[0] / "manifest.json")["status"] == "paused"
    write_json(answers, ["I enjoy building useful software.", "/finish"])
    assert t.invoke("interview", "synthetic", "--non-interactive", "--answers", answers) == 0
    assert read_json(t.root / "candidates/synthetic/session.json")["interview_finished"]
    assert not read_json(t.root / "candidates/synthetic/profile.json")["confirmed"]


@pytest.mark.parametrize("status", [200, 403])
def test_real_retrieval_records_http_status_without_authentication_headers(
    tmp_path, monkeypatch, status
):
    from jobengine.infrastructure.diagnostics import job_context
    from jobengine.models import DiscoveryItem
    from jobengine.search import retrieval

    trace = Artifacts(
        tmp_path / "debug", "fake", "test", Store(tmp_path / "ledger"), output=lambda value: None
    )
    http_client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status,
            text="Synthetic job description with useful qualifications. " * 20,
            headers={"content-type": "text/plain", "authorization": "never-log-this-header"},
        )
    )
    monkeypatch.setattr(retrieval, "validate_public_url", lambda url: url)
    monkeypatch.setattr(
        retrieval.httpx, "Client", lambda **kwargs: http_client(transport=transport, **kwargs)
    )
    with (
        observe(trace),
        job_context(
            DiscoveryItem(url="https://example.com/job", title="Synthetic", company="Synthetic")
        ),
    ):
        page = retrieval.fetch_page("https://example.com/job")
    assert page.status == ("retrieved" if status == 200 else "failed")
    assert any(e.get("http_status") == status and e["job_id"] for e in trace.events)
    assert "never-log-this-header" not in (trace.path / "events.jsonl").read_text()


@pytest.mark.parametrize(
    "problem, field",
    [
        ("malformed", "intake"),
        ("array", "intake"),
        ("missing-resume", "resume"),
        ("unsupported-resume", "resume"),
        ("corrupt-docx", "resume"),
        ("empty-resume", "resume"),
        ("missing-required", "full_name"),
        ("date-object", "start_date"),
    ],
)
def test_form_registration_source_errors_are_atomic(terminal, tmp_path, case, problem, field):
    values = {
        "full_name": "Alex Example",
        "graduation_status": "not_applicable",
        "start_mode": "any",
    }
    if problem == "missing-required":
        del values["full_name"]
    if problem == "date-object":
        values["start_date"] = {"invalid": "date"}
    path = tmp_path / "intake.json"
    write_json(path, [] if problem == "array" else values)
    if problem == "malformed":
        path.write_text('{"full_name":', encoding="utf-8")
    suffix = {"unsupported-resume": ".exe", "corrupt-docx": ".docx"}.get(problem, ".txt")
    resume = tmp_path / ("resume" + suffix)
    if problem != "missing-resume":
        resume.write_text(
            "" if problem == "empty-resume" else case[0].documents[0].text, encoding="utf-8"
        )
    assert terminal.invoke("candidate", "bad-form", "--resume", resume, "--intake", path) == 1
    assert not (terminal.root / "candidates/bad-form").exists()
    assert field + "=" in "\n".join(terminal.output)
    assert terminal.client.operations == []


def test_loading_working_candidate_removes_retired_fields_and_invalidates_old_approval(
    terminal, case
):
    from jobengine.models import RETIRED_INTAKE_FIELDS
    from jobengine.terminal.cli import load_candidate

    directory = terminal.root / "candidates/synthetic"
    config = read_json(directory / "candidate.json")
    legacy = {
        "graduation_status": "not_applicable",
        "authorization_country": "Canada",
        "authorized_to_work": "unknown",
        "sponsorship_now": "yes",
        "sponsorship_future": "yes",
        "remote_countries": ["Canada"],
        "minimum_compensation": 999999,
        "currency": "CAD",
        "compensation_basis": "total",
    }
    config["intake"] = legacy
    write_json(directory / "candidate.json", config)
    profile = case[0].model_dump(mode="json")
    profile["intake"] = legacy
    profile["confirmed"] = True
    profile["constraints"].append(
        {
            "id": "C2",
            "field": "Work authorization / sponsorship",
            "value": "Needs sponsorship",
            "hard": True,
        }
    )
    write_json(directory / "profile.json", profile)
    historical = terminal.runs("candidate")[0] / "unchanged-history.json"
    write_json(historical, profile)
    result = load_candidate(directory)
    assert not RETIRED_INTAKE_FIELDS.intersection(result["intake"])
    cleaned = read_json(directory / "profile.json")
    assert not cleaned["confirmed"] and cleaned["search_brief"] is None
    assert all(c["id"] != "C2" for c in cleaned["constraints"])
    assert read_json(historical) == profile
    assert load_candidate(directory) == result


@pytest.mark.parametrize("answer, expected", [("Y", True), ("y", True), ("N", False), ("n", False)])
def test_approval_displays_options_and_accepts_either_case(answer, expected):
    from jobengine.terminal.cli import approve

    prompts = []

    def reply(prompt):
        prompts.append(prompt)
        return answer

    assert approve("Approve?", False, False, reply) is expected
    assert prompts == ["Approve? [Y/N]: "]


def test_approval_reprompts_for_invalid_input():
    from jobengine.terminal.cli import approve

    replies = iter(["", "approve", " n "])
    prompts = []

    def reply(prompt):
        prompts.append(prompt)
        return next(replies)

    assert not approve("Approve?", False, False, reply)
    assert len(prompts) == 3
    assert "Please enter Y or N" in prompts[-1]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "invalid"])
def test_search_parser_rejects_invalid_time_limits(value):
    from jobengine.terminal.cli import parser

    with pytest.raises(SystemExit):
        parser().parse_args(["search", "synthetic", "--max-minutes", value])


def test_terminal_time_budget_is_forwarded_and_saves_partial_results(terminal, monkeypatch):
    from jobengine.terminal import cli

    t = terminal
    assert (
        t.invoke("plan", "synthetic", "--non-interactive", "--confirm-profile", "--approve-plan")
        == 0
    )
    original = cli.run_search

    def stopped(client, profile, **kwargs):
        assert kwargs["max_minutes"] == 0.5
        run = original(client, profile, **kwargs)
        run.search.stop_reason = "time_limit"
        return run

    monkeypatch.setattr(cli, "run_search", stopped)
    assert t.invoke("search", "synthetic", "--max-minutes", "0.5") == 0
    run = t.runs("search")[0]
    assert read_json(run / "manifest.json")["status"] == "partial"
    assert read_json(run / "invocation.json")["max_minutes"] == 0.5
    assert read_json(run / "search/run.json")["search"]["stop_reason"] == "time_limit"
