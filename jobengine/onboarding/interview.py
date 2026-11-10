"""Adaptive applicant interview and grounded evidence updates."""

from __future__ import annotations

import re
import uuid

from jobengine.grounding import contains_quote, validate_evidence
from jobengine.infrastructure.llm import ModelClient
from jobengine.models import (
    AnswerCheck,
    Document,
    IntakePreferences,
    InterviewAttempt,
    InterviewExchange,
    OnboardingAnalysis,
    OnboardingSession,
    utc_now,
)
from jobengine.onboarding.policy import MAX_QUESTIONS, constrain_question, next_topic
from jobengine.onboarding.preferences import validate_intake
from jobengine.prompts import PROMPTS


def analyze_background(
    client: ModelClient,
    documents: list[Document],
    role_hint: str,
    interest_hint: str,
    history: list[InterviewExchange],
    intake: IntakePreferences | None = None,
    stop_projects=False,
    previous_analysis: OnboardingAnalysis | None = None,
    new_source_ids: list[str] | None = None,
) -> OnboardingAnalysis:
    if not documents or sum(len(d.text) for d in documents) > 65000:
        raise ValueError("Provide background material totaling at most 65,000 characters.")
    requested_topic = next_topic(history, stop_projects) if intake else None
    result = client.structured(
        "onboarding_interview",
        PROMPTS["onboarding_interview"],
        {
            "documents": [d.model_dump() for d in documents],
            "role_hint": role_hint,
            "interest_hint": interest_hint,
            "history": [h.model_dump() for h in history],
            "answers_so_far": len(history),
            "maximum_answers": MAX_QUESTIONS,
            "confirmed_intake": intake.model_dump(mode="json") if intake else None,
            "requested_topic": requested_topic,
            "previous_evidence": [e.model_dump() for e in previous_analysis.evidence]
            if previous_analysis
            else None,
            "new_source_ids": new_source_ids if previous_analysis else None,
        },
        OnboardingAnalysis,
    )

    def merge_previous(analysis):
        if previous_analysis is None:
            return
        superseded = set(analysis.superseded_evidence_ids) if new_source_ids else set()
        retained = [
            e.model_copy(deep=True) for e in previous_analysis.evidence if e.id not in superseded
        ]
        added = [e for e in analysis.evidence if e.source_id in (new_source_ids or [])]
        maximum = max(
            (
                int(e.id[1:])
                for e in previous_analysis.evidence
                if e.id.startswith("E") and e.id[1:].isdigit()
            ),
            default=0,
        )
        for index, evidence in enumerate(added, maximum + 1):
            evidence.id = f"E{index}"
        analysis.evidence = retained + added

    merge_previous(result)
    try:
        _validate_analysis(result, documents)
    except ValueError as exc:
        # One bounded, separately metered grounding repair. This is not a
        # transport retry and never relaxes exact-quotation validation.
        result = client.structured(
            "onboarding_grounding_repair",
            PROMPTS["onboarding_grounding_repair"],
            {
                "documents": [d.model_dump() for d in documents],
                "proposed_analysis": result.model_dump(),
                "validation_error": str(exc),
                "history": [h.model_dump() for h in history],
                "previous_evidence": [e.model_dump() for e in previous_analysis.evidence]
                if previous_analysis
                else None,
                "new_source_ids": new_source_ids if previous_analysis else None,
            },
            OnboardingAnalysis,
        )
        merge_previous(result)
        try:
            _validate_analysis(result, documents)
        except ValueError as repair_error:
            raise ValueError(
                "We couldn't link part of the profile to your source text. Your entries are still here. Add a clarification or try another model."
            ) from repair_error
    if intake:
        result.next_question = constrain_question(result.next_question, requested_topic, history)
    if len(history) >= MAX_QUESTIONS:
        result.next_question = None
    if result.next_question and any(
        h.question.strip().casefold() == result.next_question.question.strip().casefold()
        for h in history
    ):
        result.next_question = None
        result.missing_information.append(
            "The interviewer repeated an earlier question; continue with profile review."
        )
    return result


def _validate_analysis(result: OnboardingAnalysis, documents: list[Document]) -> None:
    validate_evidence(result.evidence, documents)
    if not result.evidence:
        raise ValueError(
            "No supported candidate evidence was extracted. Add more detail about your background."
        )
    sources = {d.id: d.text for d in documents}
    for preference in result.preference_suggestions:
        if not contains_quote(sources.get(preference.source_id, ""), preference.quote):
            raise ValueError(
                "A suggested preference has no valid applicant quotation. Retry or provide clearer background."
            )


def start_onboarding(
    client: ModelClient,
    documents: list[Document],
    role_hint="",
    interest_hint="",
    professional_years=None,
    intake: IntakePreferences | None = None,
) -> OnboardingSession:
    if intake:
        validate_intake(intake)
    analysis = analyze_background(client, documents, role_hint, interest_hint, [], intake)
    finished = analysis.next_question is None
    return OnboardingSession(
        id=uuid.uuid4().hex,
        documents=documents,
        role_hint=role_hint,
        interest_hint=interest_hint,
        professional_years=professional_years,
        history=[],
        analysis=analysis,
        interview_finished=finished,
        stage="profile" if finished else "interview",
        intake=intake,
    )


def answer_question(
    client: ModelClient, session: OnboardingSession, answer: str, skip=False
) -> OnboardingSession:
    if session.interview_finished or not session.analysis.next_question:
        raise ValueError("The interview is complete. Continue to profile review.")
    move_request = bool(
        re.fullmatch(
            r"(?:please )?(?:skip(?: this(?: question)?)?|move on|next(?: question)?)[.! ]*",
            answer.strip(),
            re.I,
        )
    )
    skip = skip or move_request
    if not skip and len(answer.strip()) < 8:
        raise ValueError("Add a little detail to your answer, or choose Skip this question.")
    updated = session.model_copy(deep=True)
    updated.notice = ""
    updated.pending_answer, updated.pending_question = None, None
    question = updated.analysis.next_question
    if move_request:
        updated.attempts.append(
            InterviewAttempt(question=question.question, text=answer.strip(), disposition="move_on")
        )
    if session.intake and not skip:
        check = client.structured(
            "onboarding_answer_check",
            PROMPTS["onboarding_answer_check"],
            {"question": question.question, "topic": question.topic, "answer": answer},
            AnswerCheck,
        )
        if check.disposition == "off_topic":
            updated.attempts.append(
                InterviewAttempt(
                    question=question.question, text=answer.strip(), disposition="off_topic"
                )
            )
            updated.notice = "Let's keep this focused on your job search. Your current question is still below; you can answer it, skip it, or finish the interview."
            return updated
        if check.disposition == "move_on":
            updated.attempts.append(
                InterviewAttempt(
                    question=question.question, text=answer.strip(), disposition="move_on"
                )
            )
            skip = True
    if re.search(
        r"no more (?:project|technical) questions|stop asking (?:about )?(?:my )?projects",
        answer,
        re.I,
    ):
        updated.stop_project_questions = True
    source_id = None
    if not skip:
        source_id = f"A{len(updated.history) + 1}"
        updated.documents.append(
            Document(
                id=source_id,
                label=f"Applicant interview answer {len(updated.history) + 1}",
                text=answer.strip(),
            )
        )
    updated.history.append(
        InterviewExchange(
            question=question.question,
            purpose=question.purpose,
            answer=answer.strip() if not skip else None,
            source_id=source_id,
            topic=question.topic,
            project_key=question.project_key,
            created_at=utc_now(),
        )
    )
    # No state is committed until the model succeeds and its quotations validate.
    updated.analysis = analyze_background(
        client,
        updated.documents,
        updated.role_hint,
        updated.interest_hint,
        updated.history,
        updated.intake,
        updated.stop_project_questions,
        session.analysis if session.intake else None,
        [source_id] if source_id else [],
    )
    if updated.analysis.next_question is None:
        updated.interview_finished, updated.stage = True, "profile"
    return updated


def finish_interview(session: OnboardingSession) -> OnboardingSession:
    updated = session.model_copy(deep=True)
    updated.interview_finished, updated.stage = True, "profile"
    updated.pending_answer, updated.pending_question = None, None
    return updated
