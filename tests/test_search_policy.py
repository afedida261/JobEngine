"""Search settings and opportunity policy, with no model or web requests."""

from types import SimpleNamespace

import pytest

from jobengine.infrastructure import llm
from jobengine.infrastructure.storage import Store
from jobengine.models import (
    AnswerCheck,
    BriefProposal,
    CompanyCandidate,
    Constraint,
    ConstraintAssessment,
    Requirement,
    RequirementAssessment,
    SearchRecord,
    SectorTarget,
)
from jobengine.onboarding.brief import confirm_search_brief, propose_search_brief
from jobengine.search import pipeline
from jobengine.search.ranking import rank_job


@pytest.fixture
def recorded_client(tmp_path, monkeypatch):
    calls = []
    usage = {"input_tokens": 1000, "output_tokens": 200}

    def respond(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="completed",
            usage=SimpleNamespace(model_dump=lambda **_: usage),
            output_parsed=AnswerCheck(disposition="relevant"),
            output_text="Synthetic research",
            model_dump=lambda **_: {"status": "completed", "usage": usage, "output": []},
        )

    monkeypatch.setattr(
        llm,
        "OpenAI",
        lambda **_: SimpleNamespace(responses=SimpleNamespace(parse=respond, create=respond)),
    )
    return llm.ModelClient("synthetic-key", Store(tmp_path)), calls


@pytest.mark.parametrize(
    "operation,model,effort",
    [(operation, "gpt-6.1-sol", "low") for operation in sorted(llm.SEARCH_OPERATIONS)]
    + [
        (operation, "gpt-6.1-terra", "low")
        for operation in (
            "onboarding_interview",
            "onboarding_answer_check",
            "onboarding_grounding_repair",
        )
    ],
)
def test_request_model_and_reasoning_depend_on_workflow(recorded_client, operation, model, effort):
    client, calls = recorded_client
    client.structured(operation, "Synthetic task", {}, AnswerCheck)
    assert calls[0]["model"] == model
    assert calls[0]["reasoning"] == {"effort": effort}


def test_reasoning_change_cannot_reuse_a_cached_response(recorded_client, monkeypatch):
    client, calls = recorded_client
    monkeypatch.setattr(llm, "SEARCH_REASONING_EFFORT", "high")
    client.structured("evaluate_fit", "Synthetic task", {}, AnswerCheck)
    client.structured("evaluate_fit", "Synthetic task", {}, AnswerCheck)
    assert len(calls) == 1
    monkeypatch.setattr(llm, "SEARCH_REASONING_EFFORT", "low")
    client.structured("evaluate_fit", "Synthetic task", {}, AnswerCheck)
    assert [c["reasoning"]["effort"] for c in calls] == ["high", "low"]
    assert calls[0]["model"] == calls[1]["model"]


def test_hosted_search_uses_sol_low_even_for_a_new_operation(recorded_client):
    client, calls = recorded_client
    client.search("Synthetic search", operation="another_search")
    assert calls[0]["model"] == "gpt-6.1-sol"
    assert calls[0]["reasoning"] == {"effort": "low"}
    assert calls[0]["tools"][0]["type"] == "web_search"


@pytest.mark.parametrize(
    "operation,attribute,original_model",
    [
        ("onboarding_interview", "interview_model", "gpt-6.1-terra"),
        ("evaluate_fit", "model", "gpt-6.1-sol"),
    ],
)
def test_stage_model_change_cannot_reuse_cached_response(
    recorded_client, operation, attribute, original_model
):
    client, calls = recorded_client
    client.structured(operation, "Synthetic task", {}, AnswerCheck)
    client.structured(operation, "Synthetic task", {}, AnswerCheck)
    assert len(calls) == 1
    setattr(client, attribute, "gpt-6-luna")
    client.structured(operation, "Synthetic task", {}, AnswerCheck)
    assert [call["model"] for call in calls] == [original_model, "gpt-6-luna"]
    assert calls[0]["reasoning"] == calls[1]["reasoning"]


def test_cost_ledger_uses_each_stages_model_and_rates(recorded_client, monkeypatch):
    client, _ = recorded_client
    reserve = client.store.reserve
    reservations = []

    def record_reservation(operation, model, amount):
        reservations.append((operation, model, amount))
        return reserve(operation, model, amount)

    monkeypatch.setattr(client.store, "reserve", record_reservation)
    client.structured("onboarding_interview", "Synthetic task", {}, AnswerCheck)
    client.structured("evaluate_fit", "Synthetic task", {}, AnswerCheck)
    entries = client.store.usage()["calls"]
    for reservation, entry, model, amount in zip(
        reservations,
        entries,
        ["gpt-6.1-terra", "gpt-6.1-sol"],
        [0.00225, 0.0045],
        strict=True,
    ):
        assert reservation[0] == entry["operation"]
        assert reservation[1] == entry["model"] == model
        assert reservation[2] > amount
        assert entry["amount"] == pytest.approx(amount)
        assert entry["state"] == "estimated"


@pytest.mark.parametrize("overrides", [False, True])
def test_django_client_uses_stage_settings(recorded_client, monkeypatch, overrides):
    from jobengine.web.tasks import get_client

    existing, _ = recorded_client
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-6-luna")
    monkeypatch.delenv("OPENAI_INTERVIEW_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_SEARCH_MODEL", raising=False)
    if overrides:
        monkeypatch.setenv("OPENAI_INTERVIEW_MODEL", "gpt-6-luna")
        monkeypatch.setenv("OPENAI_SEARCH_MODEL", "gpt-5.4-mini")
    client = get_client(existing.store)
    assert client.interview_model == ("gpt-6-luna" if overrides else "gpt-6.1-terra")
    assert client.model == ("gpt-5.4-mini" if overrides else "gpt-6.1-sol")


def test_editorial_search_has_no_software_defaults(case, search_brief):
    from jobengine.search.discovery import discover, discover_companies

    profile = case[0]
    profile.target_roles = search_brief.target_roles = ["Editorial Assistant"]
    profile.interests = "Education reporting and editing"
    search_brief.summary = "Find editorial work using research and writing."
    search_brief.work_themes = ["Research and editing"]
    search_brief.search_queries = ["editorial assistant publishing"]
    search_brief.strengths[0].statement = "Research and writing experience"
    search_brief.sectors = [
        SectorTarget(name="Publishing", relationship="preferred", rationale="Stated interest")
    ]
    prompts = []

    class Client:
        def structured(self, operation, instructions, data, schema):
            prompts.append(instructions)
            return (
                BriefProposal(plan=search_brief, uncertainties=[])
                if schema is BriefProposal
                else search_brief
            )

        def search(self, prompt, **kwargs):
            prompts.append(prompt)
            return {"text": "No established listings", "raw": {"output": []}}

    draft = propose_search_brief(Client(), profile)
    profile.search_brief = confirm_search_brief(profile, draft)
    sector = draft.sectors[0]
    discover_companies(Client(), profile, sector)
    discover(
        Client(),
        profile,
        company=CompanyCandidate(
            name="Example Press",
            official_url="https://example.com",
            careers_url=None,
            sector_rationale="Publisher",
        ),
        sector=sector,
    )
    assert all("software" not in p.lower() and "engineering" not in p.lower() for p in prompts)
    assert "editorial" in prompts[-1].lower()


def test_search_can_reach_a_fifth_sector_within_existing_budget(
    case, search_brief, tmp_path, monkeypatch
):
    profile, item, *_ = case
    search_brief.sectors = [
        SectorTarget(name=f"Field {i}", relationship="related", rationale="Transferable work")
        for i in range(6)
    ]
    profile.search_brief = confirm_search_brief(profile, search_brief)
    visited = []

    def companies(client, profile, sector, **kwargs):
        visited.append(sector.name)
        c = CompanyCandidate(
            name=sector.name,
            official_url=f"https://field{len(visited)}.example",
            careers_url=None,
            sector_rationale="Synthetic",
            sector=sector.name,
        )
        return SearchRecord(
            query="",
            retrieved_at="2026-11-08",
            summary="",
            sources=[],
            jobs=[],
            warnings=[],
            companies=[c],
        )

    def jobs(client, profile, **kwargs):
        company = kwargs["company"]
        listing = item.model_copy(
            update={"url": company.official_url + "/job", "sector": company.sector}
        )
        return SearchRecord(
            query="", retrieved_at="2026-11-08", summary="", sources=[], jobs=[listing], warnings=[]
        )

    def evaluate(client, profile, listing):
        return rank_job(*case).model_copy(
            update={
                "discovery": listing,
                "category": "strong" if listing.sector == "Field 4" else "excluded",
            }
        )

    monkeypatch.setattr(pipeline, "discover_companies", companies)
    monkeypatch.setattr(pipeline, "discover", jobs)
    monkeypatch.setattr(pipeline, "evaluate", evaluate)
    run = pipeline.run_search(
        SimpleNamespace(store=Store(tmp_path), model="synthetic"), profile, limit=1
    )
    assert visited == [f"Field {i}" for i in range(5)]
    assert run.jobs[0].discovery.sector == "Field 4"
    assert run.search.stop_reason == "shortlist_target_reached"


@pytest.mark.parametrize(
    "quote,kind",
    [
        ("Must graduate in Spring 2027.", "qualification"),  # Older saved extraction.
        ("Must have graduated within the past two years.", "graduation_cohort"),
    ],
)
def test_explicit_cohort_mismatch_is_a_visible_stretch_with_original_gap(case, quote, kind):
    profile, item, page, facts, judgment = case
    page.text += " " + quote
    facts.requirements.append(
        Requirement(
            id="R3",
            text=quote,
            quote=quote,
            importance="required",
            minimum_professional_years=None,
            kind=kind,
        )
    )
    judgment.requirements.append(
        RequirementAssessment(
            requirement_id="R3",
            status="gap",
            evidence_ids=[],
            explanation="Applicant graduated in 2026.",
        )
    )
    # Older saved profiles also marked dates hard.
    profile.constraints.append(
        Constraint(id="C3", field="Graduation / start dates", value="Graduated 2026", hard=True)
    )
    judgment.constraints.append(
        ConstraintAssessment(
            constraint_id="C3",
            status="conflict",
            job_quote=quote,
            explanation="Employer specifies a different cohort.",
        )
    )
    result = rank_job(profile, item, page, facts, judgment)
    assert result.category == "needs_review"
    assert result.judgment.requirements[-1].status == "gap"
    assert any("Graduation-cohort stretch" in reason for reason in result.reasons)
    # A separate mandatory skills gap must still be honored.
    judgment.requirements[0].status = "gap"
    assert rank_job(profile, item, page, facts, judgment).category == "excluded"


def test_silent_posting_does_not_make_graduation_a_blocker(case, intake):
    from jobengine.onboarding.preferences import intake_constraints

    profile, item, page, facts, judgment = case
    dates = next(c for c in intake_constraints(intake) if c.id == "C3")
    assert dates.hard is False
    profile.constraints.append(dates)
    judgment.constraints.append(
        ConstraintAssessment(
            constraint_id="C3", status="unknown", job_quote=None, explanation="No dates stated."
        )
    )
    assert rank_job(profile, item, page, facts, judgment).category == "strong"


def test_fit_uses_grounded_facts_without_resending_whole_page(case, monkeypatch, tmp_path):
    profile, item, page, facts, judgment = case
    sent = {}
    monkeypatch.setattr(pipeline, "fetch_page", lambda *args: page)

    class Client:
        store = Store(tmp_path)

        def structured(self, operation, instructions, data, schema):
            sent[operation] = data
            return facts if operation == "extract_job" else judgment

    assert pipeline.evaluate(Client(), profile, item).category == "strong"
    assert sent["extract_job"]["source_text"] == page.text
    assert "source_text" not in sent["evaluate_fit"]
    assert sent["evaluate_fit"]["job"]["requirements"][0]["quote"] in page.text


def test_search_cost_counts_only_completed_tool_calls(recorded_client):
    client, _ = recorded_client
    raw = {
        "status": "completed",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "output": [
            {"type": "web_search_call", "status": "completed", "action": {"type": "search"}},
            {"type": "web_search_call", "status": "searching", "action": {"type": "search"}},
            {"type": "web_search_call", "action": {"type": "search"}},
        ],
    }
    client.client.responses.create = lambda **kwargs: SimpleNamespace(
        status="completed",
        usage=SimpleNamespace(model_dump=lambda **_: raw["usage"]),
        output_text="",
        model_dump=lambda **_: raw,
    )
    client.search(
        "Synthetic accounting test", operation="greenhouse_board_discovery", max_tool_calls=1
    )
    call = client.store.usage()["calls"][-1]
    assert call["searches"] == 1
    assert call["amount"] == pytest.approx(0.01)
