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


def test_optional_blanks_do_not_create_location_restrictions(intake):
    constraints = intake_constraints(intake)
    assert {c.id for c in constraints} == {"C3"}
    assert "2026-09" in constraints[-1].value
    assert "immediately" in constraints[-1].value


def test_structured_preferences_keep_explicit_restrictions(intake):
    intake.locations = ["New York City", "Long Island"]
    intake.relocation = "no"
    values = {c.id: c.value for c in intake_constraints(intake)}
    assert "Willing to relocate: no" in values["C1"]
    assert set(values) == {"C1", "C3"}


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


def test_retired_intake_is_readable_but_absent_from_current_exports(intake):
    from jobengine.models import RETIRED_INTAKE_FIELDS, IntakePreferences, current_constraints

    legacy = intake.model_dump(mode="json") | {
        "authorization_country": "Canada",
        "authorized_to_work": "no",
        "sponsorship_now": "yes",
        "sponsorship_future": "unknown",
        "remote_countries": ["Canada"],
        "minimum_compensation": 999999,
        "currency": "CAD",
        "compensation_basis": "total",
    }
    restored = IntakePreferences.model_validate(legacy)
    assert restored == intake
    assert not RETIRED_INTAKE_FIELDS.intersection(restored.model_dump())
    assert not RETIRED_INTAKE_FIELDS.intersection(
        IntakePreferences.model_json_schema()["properties"]
    )
    assert current_constraints(
        [
            {
                "id": "C1",
                "field": "Location / remote / relocation",
                "value": "Acceptable locations: Boston; Remote work countries: Canada; Willing to relocate: no",
                "hard": True,
            }
        ]
    ) == [
        {
            "id": "C1",
            "field": "Location / remote / relocation",
            "value": "Acceptable locations: Boston; Willing to relocate: no",
            "hard": True,
        }
    ]


def test_fixed_us_scope_is_in_every_model_stage(case, search_brief):
    from jobengine.onboarding.brief import confirm_search_brief
    from jobengine.prompts import APPLICANT_SCOPE, PROMPTS
    from jobengine.search.discovery import public_brief

    profile = case[0]
    profile.search_brief = confirm_search_brief(profile, search_brief)
    assert public_brief(profile)["work_country"] == "United States"
    assert all(APPLICANT_SCOPE in instructions for instructions in PROMPTS.values())
    assert "never needs employer sponsorship" in APPLICANT_SCOPE
    assert "must not affect discovery, fit, ranking, or exclusions" in APPLICANT_SCOPE


def test_saved_approved_profile_with_retired_preferences_returns_to_review(
    case, intake, search_brief
):
    from jobengine.models import SavedOnboarding
    from jobengine.onboarding.brief import confirm_search_brief

    profile = case[0]
    profile.intake = intake
    profile.search_brief = confirm_search_brief(profile, search_brief)
    session = start_onboarding(PolicyClient(case), profile.documents, intake=intake)
    session.stage = "ready"
    original = SavedOnboarding(
        session=session, profile=profile, brief_draft=search_brief, last_run_id="historical"
    ).model_dump(mode="json")
    original["profile"]["intake"]["minimum_compensation"] = 999999
    restored = SavedOnboarding.model_validate(original)
    assert restored.session.stage == "profile"
    assert not restored.profile.confirmed and restored.profile.search_brief is None
    assert restored.brief_draft is None and restored.last_run_id is None
    assert restored.session.documents == session.documents


def test_selected_locations_never_default_to_unrestricted_relocation(case, search_brief):
    from jobengine.models import IntakePreferences, Profile
    from jobengine.onboarding.brief import confirm_search_brief
    from jobengine.search.discovery import public_brief

    intake = IntakePreferences(
        graduation_status="not_applicable",
        locations=["NYC"],
        work_arrangements=["on_site", "hybrid", "remote"],
    )
    assert intake.relocation == "no"
    location = next(c for c in intake_constraints(intake) if c.id == "C1")
    assert location.hard
    assert "On-site and hybrid work must be in: NYC" in location.value
    assert "without relocating" in location.value
    profile = Profile.model_validate(
        case[0].model_dump()
        | {
            "intake": intake,
            "constraints": intake_constraints(intake),
        }
    )
    profile.search_brief = confirm_search_brief(profile, search_brief)
    handoff = public_brief(profile)
    assert handoff["location_preferences"] == [location.value]


@pytest.mark.parametrize("relocation", [None, "any", "no", "yes"])
def test_saved_location_constraints_are_rebuilt_and_old_plan_requires_review(
    case, search_brief, relocation
):
    from jobengine.models import IntakePreferences, Profile
    from jobengine.onboarding.brief import confirm_search_brief, require_confirmed_search_brief

    profile = case[0]
    profile.search_brief = confirm_search_brief(profile, search_brief)
    saved = profile.model_dump(mode="json")
    saved["intake"] = {
        "graduation_status": "not_applicable",
        "locations": ["NYC"],
        "work_arrangements": ["remote", "hybrid"],
    }
    if relocation is not None:
        saved["intake"]["relocation"] = relocation
    saved["constraints"] = [
        {
            "id": "C1",
            "field": "Location / remote / relocation",
            "value": "Acceptable locations: NYC; Work arrangements: remote, hybrid",
            "hard": True,
        }
    ]
    restored = Profile.model_validate(saved)
    assert restored.intake.relocation == ("yes" if relocation == "yes" else "no")
    assert "On-site and hybrid work must be in: NYC" in restored.constraints[0].value
    assert "without relocating" in restored.constraints[0].value
    assert not restored.confirmed and restored.search_brief is None
    with pytest.raises(ValueError, match="confirm"):
        require_confirmed_search_brief(restored)
    assert Profile.model_validate(restored.model_dump()) == restored
    assert IntakePreferences(graduation_status="unknown").location_constraint() is None


def test_sol_planning_receives_nyc_limits_and_remote_exception(case, search_brief):
    from jobengine.models import BriefProposal, IntakePreferences, Profile
    from jobengine.onboarding.brief import propose_search_brief

    intake = IntakePreferences(
        graduation_status="not_applicable",
        locations=["NYC"],
        work_arrangements=["on_site", "hybrid", "remote"],
    )
    profile = Profile.model_validate(
        case[0].model_dump() | {"intake": intake, "constraints": intake_constraints(intake)}
    )

    class Client:
        def structured(self, operation, instructions, data, schema):
            assert operation == "onboarding_search_brief"
            assert data["confirmed_intake"]["relocation"] == "no"
            location = next(c for c in data["constraints"] if c["id"] == "C1")
            assert location["hard"]
            assert "On-site and hybrid work must be in: NYC" in location["value"]
            assert "without relocating" in location["value"]
            assert "Never infer nationwide relocation" in instructions
            return BriefProposal(plan=search_brief, uncertainties=[])

    assert propose_search_brief(Client(), profile).target_roles == profile.target_roles
