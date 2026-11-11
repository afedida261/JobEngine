"""Django boundary tests: approvals, browser isolation, recovery, and a complete offline journey."""

import os
import uuid

import pytest


@pytest.fixture
def web(monkeypatch, isolated_app_data, case, search_brief):
    import django

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "jobengine.web.settings")
    monkeypatch.setenv("DJANGO_SECRET_KEY", "offline-tests-only-not-a-production-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-fake-key")
    django.setup()

    from django.test import Client, override_settings

    from jobengine.models import (
        AnswerCheck,
        BriefProposal,
        OnboardingAnalysis,
        Question,
        Run,
        SearchRecord,
    )
    from jobengine.onboarding.brief import require_confirmed_search_brief
    from jobengine.search.ranking import rank_job
    from jobengine.web import tasks

    class FakeClient:
        model = "offline-test"
        calls = []

        def structured(self, operation, instructions, data, schema):
            self.calls.append(operation)
            if operation == "onboarding_answer_check":
                return AnswerCheck(disposition="relevant")
            if operation == "onboarding_search_brief":
                draft = search_brief.model_copy(deep=True)
                draft.target_roles = data["target_roles"]
                return BriefProposal(plan=draft, uncertainties=[])
            assert operation == "onboarding_interview"
            topic = data["requested_topic"]
            return OnboardingAnalysis(
                summary="A synthetic profile for offline testing.",
                evidence=case[0].evidence,
                conflicts=[],
                missing_information=[],
                suggested_roles=["Software engineer"],
                suggested_interests="Build useful systems",
                preference_suggestions=[],
                next_question=Question(
                    question=f"Tell me about {topic}.",
                    purpose="Understand your goals.",
                    topic=topic,
                )
                if topic
                else None,
            )

    fake = FakeClient()

    def get_client(store):
        fake.store = store
        return fake

    def run_search(client, profile, progress):
        require_confirmed_search_brief(profile)
        fake.calls.append("search")
        progress("Inspecting synthetic test postings.")
        job = rank_job(*case)
        unverified = job.model_copy(deep=True)
        unverified.category = "unverified"
        unverified.discovery.title = "Synthetic unverified lead"
        unverified.page.status = "failed"
        unverified.judgment = None
        unverified.facts = None
        unverified.reasons = ["Synthetic retrieval failure."]
        run = Run(
            id=uuid.uuid4().hex,
            model=client.model,
            profile=profile,
            search=SearchRecord(
                query="Synthetic search",
                retrieved_at="2026-11-08",
                summary="Offline synthetic search.",
                sources=[],
                jobs=[case[1]],
                warnings=[],
                stop_reason="sectors_exhausted",
            ),
            jobs=[job],
            inspected_jobs=[job, unverified],
        )
        client.store.save_run(run.id, run.model_dump(mode="json"))
        return run

    monkeypatch.setattr(tasks, "get_client", get_client)
    monkeypatch.setattr(tasks, "run_search", run_search)
    monkeypatch.setattr(
        tasks.POOL, "submit", lambda function, *args, **kwargs: function(*args, **kwargs)
    )
    with override_settings(ALLOWED_HOSTS=["testserver", "localhost"], LOCAL_LIBRARY=True):
        yield Client(), fake


def workspace_for(client):
    from jobengine.web.workspace import Workspace

    return Workspace(client.session["workspace"])


def intake_data(case):
    return {
        "full_name": "Alex Example",
        "background": case[0].documents[0].text,
        "graduation_status": "not_applicable",
        "start_mode": "any",
        "relocation": "no",
        "consent": "on",
    }


def start_profile(client, case):
    client.get("/")
    response = client.post("/start/", intake_data(case))
    assert response.status_code == 302
    workspace = workspace_for(client)
    assert response.url.startswith("/activity/")
    assert workspace.current().session.intake is not None
    assert workspace.current().session.stage == "interview"
    return workspace


def test_intake_starts_preparation_without_a_second_start(web, case):
    client, fake = web
    client.get("/")
    response = client.post("/start/", intake_data(case))
    assert response.status_code == 302
    assert response.url.startswith("/activity/")
    assert workspace_for(client).current().session.stage == "interview"
    assert fake.calls == ["onboarding_interview"]
    loading = client.get(response.url)
    assert b"data-task-url" in loading.content
    assert b"data-typewriter" in loading.content
    assert b"Personalizing Questions" in loading.content
    assert client.get(response.url + "?format=json").json()["destination"] == "/conversation/"
    assert b"Start my interview" not in client.get("/conversation/").content


def test_jane_demo_uses_real_intake_and_starts_with_unanswered_questions(
    web, case, monkeypatch, tmp_path
):
    import json

    from bs4 import BeautifulSoup
    from django.conf import settings

    client, fake = web
    source = tmp_path / ".scratch" / "jane-doe"
    source.mkdir(parents=True)
    values = intake_data(case) | {
        "full_name": "Jane Doe",
        "school": "School University",
        "degree": "undergraduate",
        "program": "Communications",
        "graduation_status": "graduated",
        "professional_years": "0.75",
    }
    values.pop("background")
    (source / "intake.json").write_text(json.dumps(values), encoding="utf-8")
    (source / "resume.md").write_text(case[0].documents[0].text, encoding="utf-8")
    monkeypatch.setattr(settings, "BASE_DIR", tmp_path)
    document = BeautifulSoup(client.get("/start/").content, "html.parser")
    assert document.select_one(".setup-footer [data-jane-demo]")
    assert json.loads(document.select_one("#jane-demo-data").string)["full_name"] == "Jane Doe"
    response = client.post("/start/", {"demo": "jane"})
    assert response.url.startswith("/activity/")
    session = workspace_for(client).current().session
    assert session.general_information.full_name == "Jane Doe"
    assert session.general_information.school == "School University"
    assert session.professional_years == 0.75
    assert session.history == []
    assert session.analysis.next_question
    assert b"data-typewriter" in client.get(response.url).content
    assert fake.calls == ["onboarding_interview"]
    client.post("/start/", {"demo": "jane"})
    assert fake.calls == ["onboarding_interview"]
    assert b"data-answer-form" in client.get("/conversation/").content


def test_jane_demo_is_available_only_in_local_development(web):
    from django.test import override_settings

    client, fake = web
    assert b"data-jane-demo" not in client.get("/start/", REMOTE_ADDR="203.0.113.1").content
    assert client.post("/start/", {"demo": "jane"}, REMOTE_ADDR="203.0.113.1").status_code == 404
    with override_settings(LOCAL_LIBRARY=False):
        assert b"data-jane-demo" not in client.get("/start/").content
        assert client.post("/start/", {"demo": "jane"}).status_code == 404
    assert fake.calls == []


def test_interview_continues_when_personalization_is_unavailable(web, case, monkeypatch):
    from jobengine.grounding import validate_evidence
    from jobengine.web import tasks

    client, _ = web
    client.get("/")

    def unavailable(store):
        raise RuntimeError("secret-api-key private-resume-do-not-leak")

    monkeypatch.setattr(tasks, "get_client", unavailable)
    response = client.post("/start/", intake_data(case))
    workspace = workspace_for(client)
    task = client.get(response.url + "?format=json").json()
    assert task["status"] == "complete", task["message"]
    assert "secret-api-key" not in str(task)
    assert b"couldn't complete" not in client.get("/conversation/").content
    for _ in range(4):
        record = workspace.current()
        assert record.session.stage == "interview"
        response = client.post(
            "/conversation/",
            {
                "revision": record.revision,
                "action": "answer",
                "answer": "I enjoy helping a team solve practical problems.",
            },
        )
        assert client.get(response.url + "?format=json").json()["status"] == "complete"
    session = workspace.current().session
    assert session.stage == "profile"
    assert len(session.history) == 4
    assert not session.pending_answer
    validate_evidence(session.analysis.evidence, session.documents)


@pytest.mark.parametrize("invalid_evidence", ["empty", "unsupported_quote"])
def test_invalid_model_evidence_opens_guided_questions(web, case, monkeypatch, invalid_evidence):
    from jobengine.models import OnboardingAnalysis

    client, fake = web
    operations = []

    def invalid(operation, instructions, data, schema):
        operations.append(operation)
        evidence = []
        if invalid_evidence == "unsupported_quote":
            evidence = [
                case[0]
                .evidence[0]
                .model_copy(update={"quote": "A qualification the applicant never provided."})
            ]
        return OnboardingAnalysis(
            summary="Unusable model response",
            evidence=evidence,
            conflicts=[],
            missing_information=[],
            suggested_roles=[],
            suggested_interests="",
            preference_suggestions=[],
            next_question=None,
        )

    monkeypatch.setattr(fake, "structured", invalid)
    response = client.post("/start/", intake_data(case))
    task = client.get(response.url + "?format=json").json()
    assert operations == ["onboarding_interview", "onboarding_grounding_repair"]
    assert task["status"] == "complete"
    session = workspace_for(client).current().session
    assert session.interview_mode == "guided"
    assert session.analysis.next_question
    assert all("never provided" not in evidence.quote for evidence in session.analysis.evidence)
    assert b"data-answer-form" in client.get("/conversation/").content


def test_saved_intake_resumes_on_loading_screen_without_model_work_on_get(web, case):

    client, fake = web
    workspace = start_profile(client, case)
    record = workspace.current()
    record.session.stage = "intake"
    workspace.save(record)
    calls = len(fake.calls)
    response = client.get("/conversation/")
    assert b"data-auto-prepare" in response.content
    assert b"Start my interview" not in response.content
    assert len(fake.calls) == calls


def prepare_plan(client, case):
    workspace = start_profile(client, case)
    record = workspace.current()
    response = client.post("/conversation/", {"revision": record.revision, "action": "finish"})
    assert response.status_code == 302
    record = workspace.current()
    response = client.post(
        "/profile/",
        {
            "revision": record.revision,
            "roles": "Software engineer",
            "interests": "Build useful systems",
            "professional_years": "0",
            "approved": "on",
            "evidence-TOTAL_FORMS": "1",
            "evidence-INITIAL_FORMS": "1",
            "evidence-0-include": "on",
            "evidence-0-summary": case[0].evidence[0].summary,
        },
    )
    assert response.status_code == 302, response.content.decode()
    assert workspace.current().session.stage == "brief"
    return workspace


def approval_data(record):
    draft = record.brief_draft
    data = {
        "action": "approve",
        "revision": record.revision,
        "summary": draft.summary,
        "work_themes": "\n".join(draft.work_themes),
        "search_queries": "\n".join(draft.search_queries),
        "avoid": "\n".join(draft.avoid),
        "stretch_policy": draft.stretch_policy,
        "unresolved_questions": "\n".join(draft.unresolved_questions),
        "approved": "on",
        "sectors-TOTAL_FORMS": str(len(draft.sectors)),
        "sectors-INITIAL_FORMS": str(len(draft.sectors)),
        "strengths-TOTAL_FORMS": str(len(draft.strengths)),
        "strengths-INITIAL_FORMS": str(len(draft.strengths)),
    }
    for index, sector in enumerate(draft.sectors):
        data.update({f"sectors-{index}-{key}": value for key, value in sector.model_dump().items()})
    for index, strength in enumerate(draft.strengths):
        data[f"strengths-{index}-statement"] = strength.statement
        data[f"strengths-{index}-evidence_ids"] = strength.evidence_ids
    return data


def complete_search(client, case):
    workspace = prepare_plan(client, case)
    assert client.post("/plan/", approval_data(workspace.current())).status_code == 302
    record = workspace.current()
    assert record.session.stage == "ready"
    response = client.post("/plan/", {"action": "search", "revision": record.revision})
    assert response.status_code == 302
    run = workspace.run(workspace.current().last_run_id)
    assert run is not None
    return workspace, run


def test_new_workspace_pages_are_honest_and_do_not_call_models(web):
    client, fake = web
    for path in ("/", "/start/", "/plan/", "/opportunities/", "/saved/", "/settings/", "/library/"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "no-store" in response["Cache-Control"]
        assert response["X-Frame-Options"] == "DENY"
        assert "script-src 'self'" in response["Content-Security-Policy"]
        assert b"Synthetic company" not in response.content
    initial = client.get("/").content
    assert b"Start with the essentials" in initial
    assert b"brand-logo.svg" in initial
    assert b"wordmark.png" not in initial
    for removed in (
        b"01 / YOUR PROFILE",
        b"Profile setup",
        b"Saved progress",
        b"setup-steps",
        b"Bring your experience along",
        b"Give your search a direction",
        b"Your interests",
        b"A few practical details before the interview",
        b"Work authorization &amp; sponsorship",
        b"Availability, relocation",
        b"id_consent",
        b"Privacy details",
    ):
        assert removed not in initial
    assert b"Development Page" in initial
    for field in ("resume", "full_name", "school", "degree", "program", "interests"):
        assert f'name="{field}"'.encode() in initial
    assert b"intake-composer" not in initial
    assert b"navigation-menu" not in initial
    assert b"sidebar" not in initial
    assert not fake.calls


def test_full_applicant_journey_and_result_evidence(web, case):
    client, fake = web
    workspace = start_profile(client, case)
    response = client.get("/conversation/")
    assert response.status_code == 200
    assert b"Tell me about desired_work" in response.content
    record = workspace.current()
    response = client.post(
        "/conversation/",
        {
            "revision": record.revision,
            "action": "answer",
            "answer": "I enjoy building useful things with a team.",
        },
    )
    assert response.status_code == 302
    assert len(workspace.current().session.history) == 1
    assert "I enjoy building useful things" in workspace.current().session.history[-1].answer
    assert client.post("/new/").status_code == 302
    workspace, run = complete_search(client, case)
    for path in (
        "/",
        "/profile/",
        "/profile/?edit=1",
        "/preferences/",
        "/plan/",
        "/plan/?edit=1",
        "/opportunities/",
        "/library/",
    ):
        assert client.get(path).status_code == 200, path
    result = client.get("/opportunities/")
    assert b"Synthetic company" in result.content
    assert b"Synthetic unverified lead" not in result.content
    assert b"Synthetic unverified lead" in client.get("/opportunities/?category=unverified").content
    detail = client.get(f"/runs/{run.id}/jobs/0/")
    assert b"The employer's wording" in detail.content
    assert case[0].evidence[0].quote.encode() in detail.content
    assert b"https://example.com/test-job" in detail.content
    assert "search" in fake.calls
    assert workspace.store.usage()["total"] == 0


def test_search_requires_explicit_current_approval(web, case):
    client, fake = web
    workspace = prepare_plan(client, case)
    record = workspace.current()
    client.post("/plan/", {"action": "search", "revision": record.revision})
    assert "search" not in fake.calls
    data = approval_data(record)
    del data["approved"]
    assert client.post("/plan/", data).status_code == 200
    assert workspace.current().session.stage == "brief"
    client.post("/plan/", approval_data(record))
    record = workspace.current()
    record.profile.interests = "Changed without renewed approval"
    workspace.save(record)
    client.post("/plan/", {"action": "search", "revision": workspace.current().revision})
    assert "search" not in fake.calls


def test_preferences_invalidate_profile_brief_and_visible_current_results(web, case):
    from jobengine.web.forms import PreferencesForm

    client, fake = web
    workspace, run = complete_search(client, case)
    record = workspace.current()
    values = PreferencesForm.initial_for(record.session.intake)
    values.update({"revision": record.revision, "locations": "Chicago"})
    values = {key: value for key, value in values.items() if value is not None}
    response = client.post("/preferences/", values)
    assert response.status_code == 302
    updated = workspace.current()
    assert not updated.profile.confirmed
    assert updated.profile.search_brief is None
    assert updated.last_run_id is None
    assert updated.brief_draft is None
    assert updated.session.stage == "profile"
    assert workspace.run(run.id) is not None
    calls = fake.calls.count("search")
    client.post("/plan/", {"action": "search", "revision": updated.revision})
    assert fake.calls.count("search") == calls


def test_stale_forms_cannot_change_progress_or_spend_again(web, case):
    client, fake = web
    workspace = start_profile(client, case)
    revision = workspace.current().revision
    data = {
        "revision": revision,
        "action": "answer",
        "answer": "I like helping teams build useful things.",
    }
    client.post("/conversation/", data)
    calls = len(fake.calls)
    client.post("/conversation/", data)
    assert len(fake.calls) == calls
    assert len(workspace.current().session.history) == 1


def test_bookmarks_filters_and_exports(web, case):
    client, _ = web
    workspace, run = complete_search(client, case)
    path = f"/runs/{run.id}/jobs/0/save/"
    assert client.get(path).status_code == 405
    assert client.post(path).status_code == 302
    assert workspace.bookmarks() == [(run.id, 0)]
    assert b"Synthetic company" in client.get("/saved/").content
    assert b"Synthetic company" not in client.get("/opportunities/?q=no-such-employer").content
    assert b"Synthetic company" in client.get("/opportunities/?q=New+York").content
    export = client.get(f"/export/run/{run.id}/")
    assert export.status_code == 200
    assert "attachment" in export["Content-Disposition"]
    assert export.json()["id"] == run.id
    assert client.post(path).status_code == 302
    assert not workspace.bookmarks()


def test_other_browsers_cannot_read_or_change_saved_material(web, case):
    from django.test import Client

    client, _ = web
    workspace, run = complete_search(client, case)
    candidate_id = workspace.current().session.id
    other = Client()
    other.get("/")
    for path in (
        f"/runs/{run.id}/jobs/0/",
        f"/export/run/{run.id}/",
        f"/export/progress/{candidate_id}/",
        f"/opportunities/?run={run.id}",
    ):
        assert other.get(path).status_code == 404
    assert other.post(f"/runs/{run.id}/jobs/0/save/").status_code == 404
    assert other.post("/library/", {"candidate": candidate_id}).status_code == 200
    assert workspace_for(other).current() is None


def test_running_task_excludes_duplicate_spending_and_edits(web, case, monkeypatch):
    from jobengine.web import tasks

    client, _ = web
    workspace = start_profile(client, case)
    monkeypatch.setattr(tasks.POOL, "submit", lambda *args, **kwargs: None)
    record = workspace.current()
    task_id = tasks.launch(workspace, record, "answer", "/conversation/")
    with pytest.raises(ValueError, match="still running"):
        tasks.launch(workspace, record, "answer", "/conversation/")
    with pytest.raises(ValueError, match="still running"):
        workspace.save(record)
    with pytest.raises(ValueError, match="still running"):
        workspace.activate(None)
    response = client.get(f"/activity/{task_id}/?format=json")
    assert response.json()["status"] == "running"
    assert client.get(f"/activity/{task_id}/").status_code == 200


def test_personalization_failure_saves_answer_and_continues_without_leaking_errors(
    web, case, monkeypatch
):
    from jobengine.web import tasks

    client, _ = web
    workspace = start_profile(client, case)

    def fail(store):
        raise RuntimeError("secret-api-key private-resume-do-not-leak")

    monkeypatch.setattr(tasks, "get_client", fail)
    response = client.post(
        "/conversation/",
        {
            "revision": workspace.current().revision,
            "action": "answer",
            "answer": "This pending answer should survive the failure.",
        },
    )
    task = client.get(response.url + "?format=json").json()
    assert task["status"] == "complete"
    assert "secret-api-key" not in str(task)
    assert "private-resume" not in client.get("/conversation/").content.decode()
    assert workspace.current().session.pending_answer is None
    assert (
        workspace.current().session.history[-1].answer
        == "This pending answer should survive the failure."
    )
    assert workspace.current().session.interview_mode == "guided"


def test_restart_is_visible_and_never_automatically_retries(web, case):
    client, fake = web
    workspace = start_profile(client, case)
    task_id = workspace.create_task(
        workspace.current(), "answer", "/conversation/", "previous-process"
    )
    calls = len(fake.calls)
    response = client.get(f"/activity/{task_id}/?format=json")
    assert response.json()["status"] == "failed"
    assert "restarted" in response.json()["message"]
    assert len(fake.calls) == calls
    assert workspace.task() is None
    workspace.activate(None)
    assert client.post(f"/activity/{task_id}/retry/").status_code == 302
    assert len(fake.calls) == calls


def test_csrf_and_method_guards(web):
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    client.get("/")
    assert client.post("/new/").status_code == 403
    assert client.get("/new/").status_code == 405
    assert client.get("/activity/missing/").status_code == 404


def test_legacy_import_preserves_record_approval_and_active_selection(
    web, case, search_brief, intake
):
    from jobengine.models import OnboardingAnalysis, OnboardingSession, SavedOnboarding
    from jobengine.onboarding.brief import confirm_search_brief

    client, _ = web
    client.get("/")
    workspace = workspace_for(client)
    profile = case[0]
    profile.search_brief = confirm_search_brief(profile, search_brief)
    session = OnboardingSession(
        id="legacytest",
        documents=profile.documents,
        role_hint="",
        interest_hint="",
        professional_years=0,
        history=[],
        intake=intake,
        interview_finished=True,
        stage="ready",
        analysis=OnboardingAnalysis(
            summary="Legacy synthetic profile",
            evidence=profile.evidence,
            conflicts=[],
            missing_information=[],
            suggested_roles=profile.target_roles,
            suggested_interests=profile.interests,
            preference_suggestions=[],
            next_question=None,
        ),
    )
    legacy = workspace.store.save_onboarding(SavedOnboarding(session=session, profile=profile))
    original = workspace.store.load_onboarding().model_dump()
    assert b"Synthetic test account" in client.get("/library/").content
    assert (
        client.post("/library/", {"action": "import", "candidate": legacy.session.id}).status_code
        == 302
    )
    assert workspace.current().model_dump() == original
    assert workspace.store.load_onboarding().model_dump() == original
    assert client.get("/plan/").status_code == 200


def test_legacy_library_is_never_exposed_to_remote_requests(web):
    client, _ = web
    response = client.get("/library/", REMOTE_ADDR="192.0.2.5")
    assert b"previous local app" not in response.content
    assert (
        client.post(
            "/library/", {"action": "import", "candidate": "anything"}, REMOTE_ADDR="192.0.2.5"
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"background": "too short"},
        {"professional_years": "inf"},
        {"full_name": ""},
    ],
)
def test_intake_validation_never_calls_the_model(web, case, changes):
    client, fake = web
    data = intake_data(case) | changes
    response = client.post("/start/", data)
    assert response.status_code == 200
    assert b"errorlist" in response.content
    assert not fake.calls
    assert workspace_for(client).current() is None


@pytest.mark.parametrize(
    "interests",
    ["one, two, three, four, five, six, seven, eight, nine", "a very long interest"],
)
def test_setup_rejects_invalid_interest_lists_without_model_calls(web, case, interests):
    client, fake = web
    response = client.post("/start/", intake_data(case) | {"interests": interests})
    assert response.status_code == 200
    assert b"errorlist" in response.content
    assert workspace_for(client).current() is None
    assert not fake.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"school": "An unlisted school"},
        {"locations": "An unlisted region"},
        {"locations": "NYC, Long Island, Boston, Chicago, Seattle, Austin"},
    ],
)
def test_setup_rejects_unlisted_choices_and_more_than_five_areas(web, case, changes):
    client, fake = web
    response = client.post("/start/", intake_data(case) | changes)
    assert response.status_code == 200
    assert b"errorlist" in response.content
    assert not fake.calls


def test_setup_saves_picker_values_and_decimal_experience_without_removed_fields(web, case):
    from jobengine.onboarding.preferences import intake_constraints

    client, fake = web
    values = {
        "full_name": "Alex Example",
        "school": "Westhaven College",
        "degree": "undergraduate",
        "program": "Design",
        "graduation_status": "studying",
        "professional_years": "0.75",
        "background": case[0].documents[0].text,
        "interests": "User experience design, Healthcare, Software Engineering",
        "locations": "NYC Metropolitan Area, NYC, Long Island, Seattle, Puget Sound, ",
        "work_arrangements": ["remote", "hybrid"],
        "start_mode": "asap",
    }
    response = client.post("/start/", values)
    assert response.status_code == 302
    record = workspace_for(client).current()
    assert record.session.professional_years == 0.75
    assert len(record.session.intake.locations) == 5
    assert record.session.intake.work_arrangements == ["remote", "hybrid"]
    assert record.session.intake.start_mode == "asap"
    assert all(constraint.id != "C2" for constraint in intake_constraints(record.session.intake))
    assert fake.calls == ["onboarding_interview"]


def test_general_information_survives_interview_and_backup(web, case):
    from jobengine.models import SavedOnboarding

    client, fake = web
    values = intake_data(case) | {
        "school": "Westhaven College",
        "degree": "undergraduate",
        "program": "Design",
        "graduation_status": "graduated",
        "graduation_month": "6",
        "graduation_year": "2025",
        "interests": "Design, healthcare, DESIGN, Education, Research, Writing, Art, Music, Travel",
    }
    response = client.post("/start/", values)
    assert response.url.startswith("/activity/")
    workspace = workspace_for(client)
    record = workspace.current()
    assert (
        record.session.interest_hint
        == "Design, healthcare, Education, Research, Writing, Art, Music, Travel"
    )
    assert record.session.general_information.school == "Westhaven College"
    assert record.session.intake.graduation_status == "graduated"
    assert record.session.general_information.qualification == "Undergraduate / Design"
    assert "Program: Design" in record.session.documents[-1].text
    assert fake.calls == ["onboarding_interview"]
    assert workspace.current().session.general_information == record.session.general_information
    client.post("/conversation/", {"revision": workspace.current().revision, "action": "finish"})
    assert b"Westhaven College" in client.get("/profile/").content
    backup = client.get(f"/export/progress/{record.session.id}/").json()
    assert (
        SavedOnboarding.model_validate(backup).session.general_information
        == record.session.general_information
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"graduation_status": "graduated"},
        {"start_mode": "date"},
    ],
)
def test_preference_errors_retain_uploaded_background_without_model_work(web, case, changes):
    client, fake = web
    response = client.post("/start/", intake_data(case))
    assert response.status_code == 302
    workspace = workspace_for(client)
    record = workspace.current()
    calls = len(fake.calls)
    data = intake_data(case) | {"revision": record.revision} | changes
    response = client.post("/preferences/", data)
    assert response.status_code == 200
    assert b"errorlist" in response.content
    assert len(fake.calls) == calls
    assert workspace.current().session.documents == record.session.documents
    assert workspace.current().session.intake == record.session.intake


def test_invalid_or_scanned_resume_has_recoverable_feedback(web, case):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client, fake = web
    data = intake_data(case)
    data["resume"] = SimpleUploadedFile("broken.pdf", b"This is not a PDF")
    response = client.post("/start/", data)
    assert response.status_code == 200
    assert b"paste your background" in response.content
    assert not fake.calls


def test_html_from_applicant_and_sources_is_escaped(web, case):
    client, _ = web
    workspace = prepare_plan(client, case)
    record = workspace.current()
    record.profile.interests = '<script>alert("not trusted")</script>'
    workspace.save(record)
    response = client.get("/profile/")
    assert b"<script>alert" not in response.content
    assert b"&lt;script&gt;" in response.content
    assert os.environ["JOBENGINE_DATA_DIR"] in str(workspace.store.root)


def test_backup_restores_into_a_new_browser_without_old_approvals_or_spending(web, case):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import Client

    client, fake = web
    workspace, run = complete_search(client, case)
    original = workspace.current()
    backup = client.get(f"/export/progress/{original.session.id}/").content
    calls = len(fake.calls)
    other = Client()
    response = other.post("/restore/", {"backup": SimpleUploadedFile("progress.json", backup)})
    assert response.status_code == 302
    restored = workspace_for(other).current()
    assert restored.session.id != original.session.id
    assert restored.session.documents == original.session.documents
    assert restored.session.stage == "profile"
    assert not restored.profile.confirmed
    assert restored.profile.search_brief is None
    assert restored.last_run_id is None
    assert len(fake.calls) == calls
    assert client.get(f"/runs/{run.id}/jobs/0/").status_code == 200
    assert other.get(f"/runs/{run.id}/jobs/0/").status_code == 404
    assert workspace.current() == original


def test_legacy_session_backup_and_bad_backup_feedback(web, case):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import Client

    client, fake = web
    workspace = start_profile(client, case)
    backup = workspace.current().session.model_dump_json().encode()
    other = Client()
    assert (
        other.post("/restore/", {"backup": SimpleUploadedFile("legacy.json", backup)}).status_code
        == 302
    )
    assert workspace_for(other).current().session.analysis.evidence
    calls = len(fake.calls)
    response = other.post(
        "/restore/", {"backup": SimpleUploadedFile("broken.json", b'{"private":"invalid"}')}
    )
    assert response.status_code == 200
    assert b"couldn&#x27;t validate" in response.content
    assert len(fake.calls) == calls


def test_historical_runs_stay_accessible_after_profile_changes(web, case):
    client, _ = web
    workspace, run = complete_search(client, case)
    record = workspace.current()
    record.last_run_id = None
    workspace.save(record)
    response = client.get("/opportunities/")
    assert f"?run={run.id}".encode() in response.content
    assert b"previous searches" in response.content
    response = client.get(f"/opportunities/?run={run.id}")
    assert b"viewing an earlier search" in response.content


def test_skip_is_recorded_without_invented_answer_and_recovery_can_retry_it(web, case, monkeypatch):

    client, _ = web
    workspace = start_profile(client, case)
    documents = workspace.current().session.documents
    response = client.post(
        "/conversation/", {"revision": workspace.current().revision, "action": "skip"}
    )
    assert response.status_code == 302
    assert workspace.current().session.history[-1].answer is None
    assert workspace.current().session.documents == documents
    record = workspace.current()
    record.session.pending_question = record.session.analysis.next_question.question
    record = workspace.save(record)
    task_id = workspace.create_task(record, "skip", "/conversation/", "previous-process")
    client.get(f"/activity/{task_id}/")
    assert client.post(f"/activity/{task_id}/retry/").status_code == 302
    assert len(workspace.current().session.history) == 2
    assert workspace.current().session.history[-1].answer is None


def test_only_current_question_is_rendered_and_answers_remain_saved(web, case):
    from bs4 import BeautifulSoup

    client, _ = web
    workspace = start_profile(client, case)
    client.post(
        "/conversation/",
        {
            "revision": workspace.current().revision,
            "action": "answer",
            "answer": "I enjoy helping communities access meaningful programs.",
        },
    )
    document = BeautifulSoup(client.get("/conversation/").content, "html.parser")
    assert len(document.select("[data-current]")) == 1
    assert not document.select(
        "[data-slide], [data-previous], [data-next], [data-slide-target], .question-purpose, .finish-interview"
    )
    assert "communities" not in document.get_text()
    assert "communities" in workspace.current().session.history[0].answer


def test_async_answer_returns_owned_task_and_new_question_without_reposting(web, case):
    from django.test import Client

    client, fake = web
    workspace = start_profile(client, case)
    response = client.post(
        "/conversation/",
        {
            "revision": workspace.current().revision,
            "action": "answer",
            "answer": "I want a collaborative role helping communities thrive.",
        },
        HTTP_ACCEPT="application/json",
    )
    assert response.status_code == 202
    task = response.json()
    status = client.get(task["task_url"]).json()
    assert status["status"] == "complete"
    assert status["destination"] == "/conversation/"
    assert len(status["events"]) >= 2
    calls = len(fake.calls)
    assert client.get("/conversation/").status_code == 200
    client.get(task["task_url"])
    assert len(fake.calls) == calls
    assert Client().get(task["task_url"]).status_code == 404


def test_approved_plan_can_start_search_and_land_on_dashboard(web, case):
    client, fake = web
    workspace = prepare_plan(client, case)
    values = approval_data(workspace.current()) | {"discover": "1"}
    response = client.post("/plan/", values)
    assert response.status_code == 302
    status = client.get(response.url + "?format=json").json()
    assert status["destination"] == "/"
    assert "search" in fake.calls
    assert workspace.current().last_run_id
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert b"YOUR DASHBOARD" in dashboard.content
    assert b"navigation-menu" in dashboard.content
    for href in (b"/profile/", b"/saved/", b"/applied/", b"/opportunities/"):
        assert href in dashboard.content
    assert b"sidebar" not in dashboard.content


def test_dashboard_follows_interview_before_any_search(web, case):
    client, fake = web
    client.post("/start/", intake_data(case))
    assert client.get("/").url == "/conversation/"
    workspace = workspace_for(client)
    client.post("/conversation/", {"revision": workspace.current().revision})
    assert client.get("/").url == "/conversation/"
    response = client.post(
        "/conversation/", {"revision": workspace.current().revision, "action": "finish"}
    )
    assert response.url == "/"
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert b"YOUR DASHBOARD" in dashboard.content
    assert b"Review my profile" in dashboard.content
    assert b"navigation-menu" in dashboard.content
    assert b"No confirmed fits this time" not in dashboard.content
    for path in ("/profile/", "/saved/", "/applied/", "/opportunities/"):
        assert client.get(path).status_code == 200
    assert "search" not in fake.calls


def test_applied_roles_are_explicit_idempotent_private_and_persistent(web, case):
    from django.test import Client

    client, _ = web
    workspace, run = complete_search(client, case)
    path = f"/runs/{run.id}/jobs/0/application/"
    original_export = workspace.store.load_run(run.id)
    assert client.get(path).status_code == 405
    assert client.post(path, {"action": "invalid"}).status_code == 400
    assert Client().post(path, {"action": "mark"}).status_code == 404
    for _ in range(2):
        assert client.post(path, {"action": "mark"}).status_code == 302
    assert len(workspace.applications()) == 1
    assert len(workspace_for(client).applications()) == 1
    assert b"Synthetic company" in client.get("/applied/").content
    assert b"Applied - undo" in client.get(f"/runs/{run.id}/jobs/0/").content
    assert b"Synthetic company" not in Client().get("/applied/").content
    assert workspace.store.load_run(run.id) == original_export
    client.post(path, {"action": "remove", "destination": "applied"})
    assert not workspace.applications()


def test_agent_status_uses_actual_callbacks_as_readable_sentences(web, case):
    from jobengine.web.tasks import WORKER, describe_progress

    client, _ = web
    workspace = start_profile(client, case)
    task_id = workspace.create_task(workspace.current(), "search", "/", WORKER)
    progress_messages = [
        describe_progress(
            "Education: discovering companies (0/10 confirmed shortlist matches so far)…"
        ),
        describe_progress("Education → Example Org: finding actual jobs…"),
        describe_progress(
            "Education → Example Org: retrieving and evaluating Program Coordinator…"
        ),
    ]
    for message in progress_messages:
        workspace.update_task(task_id, message=message)
    workspace.update_task(task_id, message=progress_messages[-1])
    response = client.get(f"/activity/{task_id}/?format=json").json()
    assert (
        response["events"][-1]["message"]
        == "I'm reading the Program Coordinator posting at Example Org and checking it against your profile."
    )
    assert response["events"][-2]["message"] == "I'm looking for open roles at Example Org."
    assert len(response["events"]) == 4


@pytest.mark.parametrize(
    "details",
    [
        {
            "school": "Westhaven College",
            "degree": "undergraduate",
            "program": "Design",
            "graduation_status": "graduated",
            "graduation_month": "5",
            "graduation_year": "2025",
            "professional_years": "0.75",
            "interests": "Design, design, Community outreach",
            "locations": "boston, NYC",
            "work_arrangements": ["remote", "hybrid"],
            "start_mode": "date",
            "start_date": "2027-01-02",
        },
        {
            "school": "",
            "degree": "",
            "program": "",
            "professional_years": "",
            "graduation_month": "",
            "graduation_year": "",
            "interests": "",
            "locations": "",
            "work_arrangements": [],
            "start_date": "",
        },
        {},
    ],
)
@pytest.mark.parametrize("guided", [False, True])
def test_terminal_form_registration_matches_django_terra_state(
    web, case, tmp_path, monkeypatch, details, guided
):
    from copy import deepcopy

    from django.core.files.uploadedfile import SimpleUploadedFile

    from jobengine.terminal.artifacts import read_json, write_json
    from jobengine.terminal.cli import main

    client, fake = web
    received = []
    structured = fake.structured

    def capture(operation, instructions, data, schema):
        received.append((operation, instructions, deepcopy(data), schema))
        if guided and operation == "onboarding_interview":
            raise OSError("Synthetic provider failure")
        return structured(operation, instructions, data, schema)

    monkeypatch.setattr(fake, "structured", capture)
    values = {
        "full_name": "Alex Example",
        "graduation_status": "not_applicable",
        "start_mode": "any",
    } | details
    resume = tmp_path / "resume.txt"
    resume.write_text(case[0].documents[0].text, encoding="utf-8")
    client.get("/")
    response = client.post(
        "/start/", values | {"resume": SimpleUploadedFile(resume.name, resume.read_bytes())}
    )
    assert response.status_code == 302
    website = workspace_for(client).current().session
    website_request = received.pop()
    root = tmp_path / "terminal"
    intake_path = tmp_path / "intake.json"
    write_json(intake_path, values)
    output = []

    def invoke(*args, answers=()):
        replies = iter(answers)
        return main(
            ["--root", str(root), *map(str, args)],
            client_factory=lambda store: fake,
            input_fn=lambda prompt: next(replies),
            output=output.append,
        )

    assert invoke("candidate", "parity", "--resume", resume, "--intake", intake_path) == 0
    assert received == []  # Registration retains the staged workflow: no model call.
    assert invoke("interview", "parity", "--non-interactive") == 0
    assert received.pop() == website_request
    saved = read_json(root / "candidates/parity/session.json")
    assert {k: v for k, v in saved.items() if k != "id"} == website.model_dump(
        mode="json", exclude={"id"}
    )
    assert invoke("interview", "parity", "--restart", "--non-interactive") == 0
    assert received.pop() == website_request
    assert read_json(root / "candidates/parity/session.json") == saved
    assert invoke("interview", "parity", answers=["/finish"]) == 0
    profile = read_json(root / "candidates/parity/profile.json")
    assert profile["intake"] == saved["intake"]
    assert profile["documents"] == saved["documents"]
    assert profile["professional_years"] == saved["professional_years"]
    assert list(root.glob("runs/parity/*/interview/session.json"))
    if guided:
        return
    # The real planning path receives these same preserved profile fields.
    assert invoke("plan", "parity", "--non-interactive", "--confirm-profile") == 0
    plan_input = next(
        data for operation, _, data, _ in received if operation == "onboarding_search_brief"
    )
    assert plan_input["confirmed_intake"] == saved["intake"]


@pytest.mark.parametrize(
    "changes, field",
    [
        ({"full_name": ""}, "full_name"),
        ({"full_name": None}, "full_name"),
        ({"degree": "invalid"}, "degree"),
        ({"school": "invalid"}, "school"),
        ({"graduation_status": "invalid"}, "graduation_status"),
        ({"work_arrangements": ["invalid"]}, "work_arrangements"),
        ({"work_arrangements": "remote"}, "work_arrangements"),
        ({"professional_years": -1}, "professional_years"),
        ({"professional_years": "invalid"}, "professional_years"),
        ({"graduation_month": 13}, "graduation_month"),
        ({"graduation_year": 1800}, "graduation_year"),
        ({"graduation_status": "studying", "graduation_month": 5}, "__all__"),
        (
            {"graduation_status": "graduated", "graduation_month": 5, "graduation_year": 2100},
            "__all__",
        ),
        ({"start_mode": "date", "start_date": ""}, "__all__"),
        ({"start_date": "bad-date"}, "start_date"),
        ({"locations": "invalid"}, "locations"),
        ({"interests": "one two three four"}, "interests"),
    ],
)
def test_terminal_form_rejects_same_invalid_inputs_without_candidate_state(
    web, case, tmp_path, changes, field
):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from jobengine.terminal.artifacts import write_json
    from jobengine.terminal.cli import main
    from jobengine.web.forms import IntakeForm

    _, fake = web
    values = {
        "full_name": "Alex Example",
        "graduation_status": "not_applicable",
        "start_mode": "any",
    } | changes
    resume = tmp_path / "resume.txt"
    resume.write_text(case[0].documents[0].text, encoding="utf-8")
    form = IntakeForm(values, {"resume": SimpleUploadedFile(resume.name, resume.read_bytes())})
    assert not form.is_valid()
    assert field in form.errors
    intake_path = tmp_path / "intake.json"
    write_json(intake_path, values)
    root = tmp_path / "terminal"
    output = []
    assert (
        main(
            [
                "--root",
                str(root),
                "candidate",
                "invalid",
                "--resume",
                str(resume),
                "--intake",
                str(intake_path),
            ],
            output=output.append,
        )
        == 1
    )
    assert not (root / "candidates/invalid").exists()
    error = "\n".join(output)
    assert field + "=" in error
    assert str(form.errors[field][0]) in error
    assert fake.calls == []


def test_retired_preferences_are_not_exposed_by_either_web_form(web, case):
    from jobengine.models import RETIRED_INTAKE_FIELDS
    from jobengine.web.forms import IntakeForm, PreferencesForm

    for form in (IntakeForm(), PreferencesForm()):
        assert not RETIRED_INTAKE_FIELDS.intersection(form.fields)
    client, _ = web
    start_profile(client, case)
    response = client.get("/preferences/")
    assert response.status_code == 200
    for name in RETIRED_INTAKE_FIELDS:
        assert f'name="{name}"'.encode() not in response.content
    assert b'name="locations"' in response.content
    assert b'name="graduation_status"' in response.content
