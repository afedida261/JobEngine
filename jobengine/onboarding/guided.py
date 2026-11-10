"""Keep intake usable without a model, retaining only verbatim applicant evidence."""

from jobengine.grounding import contains_quote, validate_evidence
from jobengine.models import Document, Evidence, InterviewExchange, OnboardingSession, utc_now
from jobengine.onboarding.policy import CORE_TOPICS, constrain_question


def continue_guided(session: OnboardingSession, *, answering=False, skip=False):
    updated = session.model_copy(deep=True)
    updated.interview_mode = "guided"
    updated.notice = ""
    if answering:
        question = updated.analysis.next_question
        answer = (updated.pending_answer or "").strip()
        source_id = None
        if answer and not skip:
            source_id = f"A{len(updated.history) + 1}"
            updated.documents.append(
                Document(id=source_id, label="Applicant interview answer", text=answer)
            )
        updated.history.append(
            InterviewExchange(
                question=question.question,
                purpose=question.purpose,
                answer=answer if source_id else None,
                source_id=source_id,
                topic=question.topic,
                project_key=question.project_key,
                created_at=utc_now(),
            )
        )
    updated.pending_answer, updated.pending_question = None, None
    # Do not infer qualifications or turn a failed extraction into invented experience.
    # Keep supported prior analysis and label new source excerpts for applicant review.
    evidence = updated.analysis.evidence
    known_sources = {item.source_id for item in evidence}
    used_ids = {item.id for item in evidence}
    for document in updated.documents:
        if document.id in known_sources or not document.text.strip():
            continue
        quote = document.text.strip()[:1200]
        if not contains_quote(document.text, quote):
            continue
        number = len(used_ids) + 1
        while f"E{number}" in used_ids:
            number += 1
        identifier = f"E{number}"
        evidence.append(
            Evidence(
                id=identifier,
                kind="other",
                summary=f"Applicant-provided excerpt: {quote}",
                source_id=document.id,
                quote=quote,
            )
        )
        used_ids.add(identifier)
    validate_evidence(evidence, updated.documents)
    covered = {item.topic for item in updated.history}
    topic = next((topic for topic in CORE_TOPICS if topic not in covered), None)
    # Core coverage is enough for this recovery path; optional project follow-ups need AI.
    updated.analysis.next_question = constrain_question(None, topic, updated.history)
    updated.analysis.summary = "Your background and answers are saved for your review."
    updated.analysis.suggested_interests = (
        updated.analysis.suggested_interests or updated.interest_hint
    )
    note = "Background excerpts are saved verbatim. Review them before approving your profile."
    if note not in updated.analysis.missing_information:
        updated.analysis.missing_information.append(note)
    updated.interview_finished = topic is None
    updated.stage = "profile" if updated.interview_finished else "interview"
    return updated
