import pytest

from jobengine.models import Evidence, OnboardingAnalysis, Question
from jobengine.onboarding.brief import confirm_search_brief, require_confirmed_search_brief
from jobengine.onboarding.interview import answer_question, finish_interview, start_onboarding
from jobengine.search.discovery import discover
from jobengine.search.pipeline import run_search


def analysis_for(case, *, question=None):
    return OnboardingAnalysis(
        summary="Synthetic candidate built and evaluated a ranking service.",
        evidence=[e.model_copy(deep=True) for e in case[0].evidence],
        conflicts=[],
        missing_information=["Professional tenure needs confirmation."],
        suggested_roles=["Software engineer"],
        suggested_interests="Search and ranking systems",
        preference_suggestions=[],
        next_question=question,
    )


class FakeInterviewer:
    def __init__(self, case):
        self.case = case
        self.calls = []

    def structured(self, operation, instructions, data, schema):
        self.calls.append((operation, data))
        if not data["history"]:
            return analysis_for(
                self.case,
                question=Question(
                    question="How did you evaluate the ranking service?",
                    purpose="Distinguish evaluation experience from merely using a library.",
                ),
            )
        result = analysis_for(self.case)
        answer = (
            next(d for d in data["documents"] if d["id"].startswith("A"))
            if data["history"][-1]["answer"]
            else None
        )
        if answer:
            result.evidence.append(
                Evidence(
                    id="E2",
                    kind="project",
                    summary=answer["text"],
                    source_id=answer["id"],
                    quote=answer["text"],
                )
            )
        return result


def test_interview_uses_answers_as_sources_and_preserves_raw_resumes(case):
    client = FakeInterviewer(case)
    session = start_onboarding(client, case[0].documents)
    updated = answer_question(
        client, session, "I compared recall on 100 manually labeled search queries."
    )
    assert session.history == []  # failed/retried calls cannot partially mutate the active session
    assert updated.documents[0] == session.documents[0]
    assert updated.documents[-1].text == updated.history[0].answer
    assert updated.analysis.evidence[-1].source_id == "A1"
    assert updated.interview_finished
    assert updated.stage == "profile"
    assert (
        client.calls[-1][1]["history"][0]["question"] == "How did you evaluate the ranking service?"
    )


def test_skip_is_not_candidate_evidence(case):
    client = FakeInterviewer(case)
    session = start_onboarding(client, case[0].documents)
    updated = answer_question(client, session, "", skip=True)
    assert updated.history[0].answer is None
    assert updated.history[0].source_id is None
    assert len(updated.documents) == 1


def test_finish_early_does_not_invent_answers(case):
    session = start_onboarding(FakeInterviewer(case), case[0].documents)
    updated = finish_interview(session)
    assert updated.stage == "profile" and updated.interview_finished
    assert updated.history == []


def test_model_failure_preserves_session_and_typed_answer_can_be_retried(case):
    client = FakeInterviewer(case)
    session = start_onboarding(client, case[0].documents)

    def fail(*args):
        raise RuntimeError("Synthetic provider failure")

    client.structured = fail
    with pytest.raises(RuntimeError):
        answer_question(client, session, "I evaluated the ranking service on held-out queries.")
    assert len(session.documents) == 1 and session.history == []


def test_invalid_quotation_gets_one_strict_repair(case):
    operations = []

    class RepairingClient:
        def structured(self, operation, instructions, data, schema):
            operations.append(operation)
            result = analysis_for(case)
            if len(operations) == 1:
                result.evidence[0].quote = "An invented first-person sentence."
            return result

    session = start_onboarding(RepairingClient(), case[0].documents)
    assert operations == ["onboarding_interview", "onboarding_grounding_repair"]
    assert session.analysis.evidence[0].quote == case[0].evidence[0].quote


def test_repair_cannot_weaken_grounding_or_retry_indefinitely(case):
    operations = []

    class InvalidClient:
        def structured(self, operation, instructions, data, schema):
            operations.append(operation)
            result = analysis_for(case)
            result.evidence[0].quote = "An invented qualification that is absent from the source."
            return result

    with pytest.raises(ValueError, match="couldn't link"):
        start_onboarding(InvalidClient(), case[0].documents)
    assert len(operations) == 2


def test_approved_profile_survives_export_and_import(case, search_brief):
    from jobengine.models import Profile

    profile = case[0]
    profile.search_brief = confirm_search_brief(profile, search_brief)
    restored = Profile.model_validate_json(profile.model_dump_json())
    assert require_confirmed_search_brief(restored).search_queries == search_brief.search_queries


def test_resume_reader_preserves_project_text_in_docx_and_plaintext():
    import io
    import zipfile

    from jobengine.infrastructure.resumes import read_resume

    text = "I personally implemented the ingestion pipeline and evaluated retrieval recall."
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
                + text
                + "</w:t></w:r></w:p></w:body></w:document>",
            )
        assert text == read_resume("resume.docx", buffer.getvalue())
    assert text == read_resume("project.txt", text.encode())


def test_unconfirmed_brief_blocks_both_discovery_entrypoints_before_network(case):
    class NoNetwork:
        def search(self, *args):
            raise AssertionError("No search should be attempted")

    with pytest.raises(ValueError, match="search brief"):
        discover(NoNetwork(), case[0])
    with pytest.raises(ValueError, match="search brief"):
        run_search(NoNetwork(), case[0])


@pytest.mark.parametrize("change", ["evidence", "constraints", "queries", "interests"])
def test_profile_or_brief_edits_invalidate_search_approval(case, search_brief, change):
    profile = case[0]
    profile.search_brief = confirm_search_brief(profile, search_brief)
    require_confirmed_search_brief(profile)
    if change == "evidence":
        profile.evidence[0].summary = "A revised, still grounded summary."
    elif change == "constraints":
        profile.constraints[0].value = "Boston"
    elif change == "queries":
        profile.search_brief.search_queries = ["a changed search direction"]
    else:
        profile.interests = "A changed career interest"
    with pytest.raises(ValueError, match="changed"):
        require_confirmed_search_brief(profile)


def test_brief_strength_cannot_reference_invented_candidate_evidence(case, search_brief):
    search_brief.strengths[0].evidence_ids = ["FAKE"]
    with pytest.raises(ValueError, match="existing candidate evidence"):
        confirm_search_brief(case[0], search_brief)
