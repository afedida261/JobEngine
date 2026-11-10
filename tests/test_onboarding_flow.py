import pytest

from jobengine.infrastructure.storage import StaleOnboarding, Store
from jobengine.models import (
    AnswerCheck,
    BriefProposal,
    BriefUncertainty,
    OnboardingAnalysis,
    Question,
    SavedOnboarding,
)
from jobengine.onboarding.brief import propose_search_brief
from jobengine.onboarding.interview import answer_question, start_onboarding
from jobengine.onboarding.preferences import intake_constraints, validate_intake


class PolicyClient:
    def __init__(self, case, repeat_project=False):
        self.case, self.calls, self.repeat_project = case, [], repeat_project

    def structured(self, operation, instructions, data, schema):
        self.calls.append((operation, data))
        if operation == "onboarding_answer_check":
            disposition = (
                "off_topic"
                if "capital of France" in data["answer"]
                else "move_on"
                if data["answer"] == "move on"
                else "relevant"
            )
            return AnswerCheck(disposition=disposition)
        topic = data["requested_topic"]
        project_count = sum(h.get("topic") == "project" for h in data["history"])
        question = (
            Question(
                question=f"Tell me about {topic} {project_count}.",
                purpose="Choose suitable work.",
                topic=topic,
                project_key=("same project" if self.repeat_project else f"project {project_count}")
                if topic == "project"
                else None,
            )
            if topic
            else None
        )
        return OnboardingAnalysis(
            summary="Resume-backed experience; interests are self-reported.",
            evidence=self.case[0].evidence,
            conflicts=[],
            missing_information=[],
            suggested_roles=["Software engineer"],
            suggested_interests="Build useful systems",
            preference_suggestions=[],
            next_question=question,
        )


def test_optional_blanks_do_not_create_location_or_pay_restrictions(intake):
    constraints = intake_constraints(intake)
    assert {c.id for c in constraints} == {"C2", "C3"}
    assert "2026-09" in constraints[-1].value
    assert "immediately" in constraints[-1].value
    assert "does not require employer sponsorship" in constraints[0].value


def test_structured_preferences_keep_explicit_restrictions(intake):
    intake.locations = ["New York City", "Long Island"]
    intake.relocation = "no"
    intake.minimum_compensation = 80000
    values = {c.id: c.value for c in intake_constraints(intake)}
    assert "Willing to relocate: no" in values["C1"]
    assert "annual base compensation: USD 80,000" in values["C4"]


def test_graduation_status_requires_a_date_only_when_applicable(intake):
    intake.graduation_month = None
    with pytest.raises(ValueError, match="month and year"):
        validate_intake(intake)
    intake.graduation_status = "not_applicable"
    validate_intake(intake)


def test_goals_and_sector_coverage_precede_project_questions(case, intake):
    client = PolicyClient(case)
    session = start_onboarding(client, case[0].documents, intake=intake)
    topics = []
    while not session.interview_finished:
        topics.append(session.analysis.next_question.topic)
        session = answer_question(
            client, session, "I want to build useful products with a collaborative team."
        )
    assert topics[:4] == ["desired_work", "career_goals", "work_style", "sectors"]
    assert topics.count("project") == 2
    assert len(session.history) == 6
    assert all(h.created_at and h.topic for h in session.history)


def test_same_project_is_not_questioned_repeatedly(case, intake):
    client = PolicyClient(case, repeat_project=True)
    session = start_onboarding(client, case[0].documents, intake=intake)
    while not session.interview_finished:
        session = answer_question(
            client,
            session,
            "I like building products, collaborating, and learning practical skills.",
        )
    assert sum(h.topic == "project" for h in session.history) == 1


def test_move_on_and_off_topic_do_not_become_qualifications(case, intake):
    client = PolicyClient(case)
    session = start_onboarding(client, case[0].documents, intake=intake)
    rejected = answer_question(client, session, "What is the capital of France?")
    assert rejected.history == []
    assert rejected.documents == session.documents
    assert rejected.analysis.next_question == session.analysis.next_question
    assert "job search" in rejected.notice
    assert rejected.attempts[-1].disposition == "off_topic"
    moved = answer_question(client, rejected, "move on")
    assert len(moved.history) == 1 and moved.history[0].answer is None
    assert len(moved.documents) == 1
    assert moved.analysis.next_question.topic == "career_goals"


def test_no_more_project_questions_respects_applicant_request(case, intake):
    client = PolicyClient(case)
    session = start_onboarding(client, case[0].documents, intake=intake)
    session = answer_question(
        client, session, "I like building systems. No more project questions please."
    )
    while not session.interview_finished:
        session = answer_question(
            client, session, "I enjoy collaboration, learning, and physical-impact industries."
        )
    assert len(session.history) == 4
    assert all(h.topic != "project" for h in session.history)


def test_unchanged_resume_evidence_is_not_reextracted_or_overwritten(case, intake):
    from jobengine.models import Evidence

    class IncrementalClient(PolicyClient):
        def structured(self, operation, instructions, data, schema):
            result = super().structured(operation, instructions, data, schema)
            if operation == "onboarding_interview" and data["history"]:
                result.evidence = [
                    case[0]
                    .evidence[0]
                    .model_copy(update={"quote": "An invalid paraphrase of old evidence."}),
                    Evidence(
                        id="E1",
                        kind="other",
                        summary=data["documents"][-1]["text"],
                        source_id="A1",
                        quote=data["documents"][-1]["text"],
                    ),
                ]
            return result

    client = IncrementalClient(case)
    session = start_onboarding(client, case[0].documents, intake=intake)
    updated = answer_question(
        client, session, "I want to build useful products with a collaborative team."
    )
    assert updated.analysis.evidence[0] == session.analysis.evidence[0]
    assert updated.analysis.evidence[1].id == "E2"
    assert updated.analysis.evidence[1].source_id == "A1"


def test_explicit_correction_can_supersede_old_evidence(case, intake):
    from jobengine.models import Evidence

    class CorrectingClient(PolicyClient):
        def structured(self, operation, instructions, data, schema):
            result = super().structured(operation, instructions, data, schema)
            if operation == "onboarding_interview" and data["history"]:
                result.superseded_evidence_ids = ["E1"]
                result.evidence = [
                    Evidence(
                        id="E1",
                        kind="project",
                        summary=data["documents"][-1]["text"],
                        source_id="A1",
                        quote=data["documents"][-1]["text"],
                    )
                ]
            return result

    client = CorrectingClient(case)
    session = start_onboarding(client, case[0].documents, intake=intake)
    updated = answer_question(
        client, session, "Correction: I evaluated on 50 manually labeled queries."
    )
    assert [e.id for e in updated.analysis.evidence] == ["E2"]
    assert updated.analysis.evidence[0].source_id == "A1"


def test_confirmed_intake_does_not_return_as_unanswered_applicant_questions(
    case, intake, search_brief
):
    class BriefClient:
        def structured(self, operation, instructions, data, schema):
            assert data["confirmed_intake"]["graduation_year"] == 2026
            return BriefProposal(
                plan=search_brief,
                uncertainties=[
                    BriefUncertainty(
                        subject="applicant", field="graduation", question="Confirm graduation date"
                    ),
                    BriefUncertainty(
                        subject="applicant",
                        field="availability",
                        question="Confirm start availability",
                    ),
                    BriefUncertainty(
                        subject="applicant",
                        field="sponsorship",
                        question="Does the employer need to offer sponsorship?",
                    ),
                    BriefUncertainty(
                        subject="applicant",
                        field="location",
                        question="Pick a location restriction",
                    ),
                    BriefUncertainty(
                        subject="employer",
                        field="graduation",
                        question="Check the employer's eligible graduation cohort.",
                    ),
                    BriefUncertainty(
                        subject="capability",
                        field="other",
                        question="Production operations experience is not established.",
                    ),
                ],
            )

    profile = case[0].model_copy(
        update={"intake": intake, "constraints": intake_constraints(intake)}
    )
    result = propose_search_brief(BriefClient(), profile)
    assert result.unresolved_questions == [
        "Check the employer's eligible graduation cohort.",
        "Production operations experience is not established.",
    ]


def test_snapshot_restores_after_restart_and_rejects_stale_duplicates(case, intake, tmp_path):
    store = Store(tmp_path)
    session = start_onboarding(PolicyClient(case), case[0].documents, intake=intake)
    first = store.save_onboarding(SavedOnboarding(session=session))
    answered = answer_question(
        PolicyClient(case), first.session, "I want to build useful products and learn from users."
    )
    latest = store.save_onboarding(first.model_copy(update={"session": answered}))
    with pytest.raises(StaleOnboarding):
        store.save_onboarding(first)
    restored = Store(tmp_path).load_onboarding()
    assert restored.revision == latest.revision
    assert len(restored.session.history) == 1
    assert restored.session.documents[-1].text == answered.history[0].answer
    assert restored.session.intake == intake


def fill_required_intake(app, prefix="intake_"):
    app.selectbox(key=prefix + "country").set_value("United States")
    app.selectbox(key=prefix + "authorized").set_value("Yes")
    app.selectbox(key=prefix + "sponsorship_now").set_value("No")
    app.selectbox(key=prefix + "sponsorship_future").set_value("No")
    app.selectbox(key=prefix + "graduation_status").set_value("Graduated")
    app.selectbox(key=prefix + "graduation_month").set_value(9)
    app.number_input(key=prefix + "graduation_year").set_value(2026)
    return app


def test_app_restores_interview_profile_and_brief_without_repeating_calls(
    case, search_brief, monkeypatch
):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from jobengine.infrastructure.llm import ModelClient

    client = PolicyClient(case)
    calls = []

    def structured(self, operation, instructions, data, schema):
        calls.append(operation)
        if operation == "onboarding_search_brief":
            return BriefProposal(plan=search_brief, uncertainties=[])
        return client.structured(operation, instructions, data, schema)

    monkeypatch.setattr(ModelClient, "structured", structured)
    monkeypatch.setattr(ModelClient, "search", lambda *args: pytest.fail("Run 1 must not search"))
    path = Path(__file__).resolve().parents[1] / "jobengine" / "ui" / "legacy_app.py"

    def fresh():
        app = AppTest.from_file(path, default_timeout=30)
        app.session_state["api_key"] = "synthetic-test-key"
        return app.run()

    app = fresh()
    app.text_area(key="onboarding_background").set_value(case[0].documents[0].text)
    fill_required_intake(app)
    next(b for b in app.button if b.label == "Start my onboarding").click().run()
    assert not app.exception and not app.error
    session_id = app.session_state["onboarding"].id
    app.text_area(key=f"interview_answer_{session_id}_0").set_value(
        "I like building products that have tangible real-world effects."
    )
    next(b for b in app.button if b.label == "Send answer").click().run()
    assert not app.exception and not app.error
    count = len(calls)
    restored = fresh()
    assert len(calls) == count
    assert len(restored.session_state["onboarding"].history) == 1
    assert restored.session_state["onboarding"].history[0].answer.startswith("I like building")
    assert restored.session_state["onboarding"].analysis.next_question.topic == "career_goals"
    restored.button(key="finish_interview").click().run()
    restored.checkbox(key=f"profile_approval_{session_id}").check()
    next(
        b for b in restored.button if b.label == "Confirm profile & prepare search brief"
    ).click().run()
    assert not restored.exception and not restored.error
    count = len(calls)
    resumed_brief = fresh()
    assert len(calls) == count
    assert resumed_brief.session_state["onboarding"].stage == "brief"
    assert resumed_brief.session_state["profile"].intake.graduation_year == 2026
    resumed_brief.checkbox(key=f"brief_approval_{session_id}").check()
    next(b for b in resumed_brief.button if b.label == "Confirm search brief").click().run()
    assert not resumed_brief.exception and not resumed_brief.error
    ready = fresh()
    assert ready.session_state["onboarding"].stage == "ready"
    assert ready.session_state["profile"].search_brief.confirmed
    assert any(b.label == "Discover & evaluate jobs" for b in ready.button)
    assert not any(c.label == "Required" for c in ready.checkbox)


def test_intake_validation_blocks_model_and_saves_sources_if_provider_fails(case, monkeypatch):
    import os
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from jobengine.infrastructure.llm import ModelClient

    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("Synthetic provider failure")

    monkeypatch.setattr(ModelClient, "structured", fail)
    path = Path(__file__).resolve().parents[1] / "jobengine" / "ui" / "legacy_app.py"
    app = AppTest.from_file(path, default_timeout=30)
    app.session_state["api_key"] = "synthetic-test-key"
    app.run()
    app.text_area(key="onboarding_background").set_value(case[0].documents[0].text)
    next(b for b in app.button if b.label == "Start my onboarding").click().run()
    assert not calls
    assert any("authorization and graduation" in e.value for e in app.error)
    fill_required_intake(app)
    next(b for b in app.button if b.label == "Start my onboarding").click().run()
    assert len(calls) == 1
    saved = Store(os.environ["JOBENGINE_DATA_DIR"]).load_onboarding()
    assert saved.session.documents[0].text == case[0].documents[0].text
    assert saved.session.intake.graduation_year == 2026
    assert saved.session.stage == "intake"


def test_submitted_answer_survives_provider_failure_and_refresh(case, monkeypatch):
    import os
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from jobengine.infrastructure.llm import ModelClient

    client = PolicyClient(case)

    def structured(self, operation, instructions, data, schema):
        if operation == "onboarding_answer_check":
            raise RuntimeError("Synthetic network failure")
        return client.structured(operation, instructions, data, schema)

    monkeypatch.setattr(ModelClient, "structured", structured)
    path = Path(__file__).resolve().parents[1] / "jobengine" / "ui" / "legacy_app.py"
    app = AppTest.from_file(path, default_timeout=30)
    app.session_state["api_key"] = "synthetic-test-key"
    app.run()
    app.text_area(key="onboarding_background").set_value(case[0].documents[0].text)
    fill_required_intake(app)
    next(b for b in app.button if b.label == "Start my onboarding").click().run()
    session_id = app.session_state["onboarding"].id
    answer = "I want to build useful physical products and collaborate with users."
    key = f"interview_answer_{session_id}_0"
    app.text_area(key=key).set_value(answer)
    next(b for b in app.button if b.label == "Send answer").click().run()
    assert any("Synthetic network failure" in e.value for e in app.error)
    restored = AppTest.from_file(path, default_timeout=30).run()
    assert not restored.exception
    assert restored.text_area(key=key).value == answer
    assert restored.session_state["onboarding"].history == []
    assert (
        Store(os.environ["JOBENGINE_DATA_DIR"]).load_onboarding().session.pending_answer == answer
    )
