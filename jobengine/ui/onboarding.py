from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import pandas as pd
import streamlit as st

from jobengine.grounding import validate_evidence
from jobengine.infrastructure.resumes import read_resume
from jobengine.models import (
    Document,
    Evidence,
    OnboardingAnalysis,
    OnboardingSession,
    Profile,
    SearchBriefDraft,
    SearchStrength,
    SectorTarget,
)
from jobengine.onboarding.brief import (
    confirm_search_brief,
    propose_search_brief,
    propose_sectors,
    require_confirmed_search_brief,
)
from jobengine.onboarding.interview import answer_question, finish_interview, start_onboarding
from jobengine.onboarding.policy import MAX_QUESTIONS
from jobengine.onboarding.preferences import intake_constraints, is_hard_constraint
from jobengine.ui.intake import intake_inputs, parse_intake, show_intake
from jobengine.ui.state import persist_progress


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _invalidate_results():
    st.session_state.pop("run", None)
    st.session_state.pop("review_report", None)
    st.session_state.pop("brief_draft", None)
    st.session_state.pop("profile", None)
    st.session_state.onboarding_last_run = None


def _start_form(get_client, show_error, configured, store):
    st.subheader("1. Tell us where you're starting")
    st.write(
        "Upload your resume versions or describe your background. We'll read them together, then ask a few questions about your work and what you want next."
    )
    st.caption(
        "About 5–10 minutes. You can skip a question or finish early; missing information stays unknown."
    )
    with st.form("onboarding_intake"):
        uploads = st.file_uploader(
            "Resume versions and supporting accounts",
            type=["pdf", "docx", "txt", "md"],
            accept_multiple_files=True,
            key="onboarding_files",
        )
        background = st.text_area(
            "Background or project account",
            height=140,
            placeholder="No resume ready? Describe what you studied, built, or worked on.",
            key="onboarding_background",
        )
        left, right = st.columns(2)
        roles = left.text_input(
            "Roles you're considering (optional)",
            placeholder="Software engineering, data science, ML — or leave open",
            key="onboarding_roles",
        )
        years = right.text_input(
            "Professional experience in years (optional)",
            placeholder="0, 1.5, or leave unknown",
            key="onboarding_years",
        )
        interests = st.text_area(
            "What kind of work interests you? (optional)",
            placeholder="It's fine to be unsure. The conversation can help clarify this.",
            key="onboarding_interests",
        )
        intake_values = intake_inputs("intake_")
        submitted = st.form_submit_button(
            "Start my onboarding", type="primary", disabled=not configured
        )
    if submitted:
        try:
            intake = parse_intake(intake_values)
            professional_years = float(years) if years.strip() else None
            if professional_years is not None and not 0 <= professional_years <= 70:
                raise ValueError("Professional experience must be between 0 and 70 years.")
            documents = [
                Document(id=f"D{i + 1}", label=f.name, text=read_resume(f.name, f.getvalue()))
                for i, f in enumerate(uploads)
            ]
            if background.strip():
                documents.append(
                    Document(
                        id=f"D{len(documents) + 1}",
                        label="Applicant background account",
                        text=background.strip(),
                    )
                )
            if sum(len(d.text.strip()) for d in documents) < 30:
                raise ValueError(
                    "Upload a resume or provide at least 30 characters about your background."
                )
            # Intake and source text survive even a failed first model call.
            pending = OnboardingSession(
                id=uuid.uuid4().hex,
                documents=documents,
                role_hint=roles,
                interest_hint=interests,
                professional_years=professional_years,
                history=[],
                analysis=OnboardingAnalysis(
                    summary="Your sources and intake are saved. Continue to start the conversation.",
                    evidence=[],
                    conflicts=[],
                    missing_information=[],
                    suggested_roles=[r.strip() for r in roles.split(",") if r.strip()],
                    suggested_interests=interests,
                    preference_suggestions=[],
                    next_question=None,
                ),
                intake=intake,
                stage="intake",
            )
            _invalidate_results()
            st.session_state.onboarding = pending
            st.session_state.onboarding_revision = 0
            persist_progress(store)
            with st.spinner("Reading your background and choosing a useful first question…"):
                session = start_onboarding(
                    get_client(), documents, roles, interests, professional_years, intake
                )
            session.id = pending.id
            st.session_state.onboarding = session
            persist_progress(store)
            st.rerun()
        except Exception as exc:
            show_error(exc)
    with st.expander("Resume saved onboarding"):
        st.caption(
            "Restore your interview sources and answers. Profile and search-brief approval must be reviewed again."
        )
        saved = st.file_uploader(
            "Onboarding progress JSON", type=["json"], key="restore_onboarding"
        )
        if st.button("Restore my progress", disabled=saved is None):
            try:
                if saved.size > 2_000_000:
                    raise ValueError("The saved onboarding file is too large.")
                session = OnboardingSession.model_validate_json(saved.getvalue())
                validate_evidence(session.analysis.evidence, session.documents)
                session.id = uuid.uuid4().hex
                session.stage = (
                    "intake"
                    if session.intake is None
                    else "profile"
                    if session.interview_finished
                    else "interview"
                )
                _invalidate_results()
                st.session_state.onboarding = session
                st.session_state.onboarding_revision = 0
                persist_progress(store)
                st.rerun()
            except Exception as exc:
                show_error(exc)


def _interview(session, get_client, show_error, configured, store):
    st.subheader("2. A short conversation about your work")
    st.caption(
        f"{len(session.history)} of up to {MAX_QUESTIONS} questions completed · Finish whenever you're ready to review."
    )
    if session.notice:
        st.info(session.notice)
    with st.expander("What we understand so far", expanded=not session.history):
        st.write(session.analysis.summary)
        for conflict in session.analysis.conflicts:
            st.warning("Needs clarification: " + conflict)
    for exchange in session.history:
        with st.chat_message("assistant"):
            st.write(exchange.question)
        with st.chat_message("user"):
            st.write(exchange.answer or "Skipped — left unknown.")
    question = session.analysis.next_question
    if question:
        with st.chat_message("assistant"):
            st.write(question.question)
            st.caption("Why we're asking: " + question.purpose)
        answer_key = f"interview_answer_{session.id}_{len(session.history)}"
        if answer_key not in st.session_state and session.pending_question == question.question:
            st.session_state[answer_key] = session.pending_answer or ""
        with st.form("form_" + answer_key):
            answer = st.text_area(
                "Your answer",
                height=160,
                placeholder="Tell us what you want, or clarify the experience we're asking about.",
                key=answer_key,
            )
            left, right = st.columns(2)
            submit = left.form_submit_button("Send answer", type="primary", disabled=not configured)
            skip = right.form_submit_button("Skip this question", disabled=not configured)
        if submit or skip:
            try:
                session.pending_answer = answer if not skip else None
                session.pending_question = question.question if not skip else None
                persist_progress(store)
                with st.spinner("Updating your profile and deciding what to ask next…"):
                    updated = answer_question(get_client(), session, answer, skip=skip)
                if len(updated.history) > len(session.history):
                    _invalidate_results()
                st.session_state.onboarding = updated
                persist_progress(store)
                st.rerun()
            except Exception as exc:
                show_error(exc)
    if st.button("Finish interview & review profile", key="finish_interview"):
        st.session_state.onboarding = finish_interview(session)
        persist_progress(store)
        st.rerun()


def _profile_review(session, get_client, show_error, configured, store):
    st.subheader("3. Make sure this represents you")
    st.write(session.analysis.summary)
    for conflict in session.analysis.conflicts:
        st.warning("Check this before continuing: " + conflict)
    previous = st.session_state.get("profile")
    evidence = previous.evidence if previous else session.analysis.evidence
    st.caption(
        "Resume-backed evidence guides capability assessment. Interview answers clarify contribution and preferences; they are additional self-reports. Edit summaries or remove rows, keeping quotations linked to their sources."
    )
    with st.form(f"onboarding_profile_{session.id}"):
        frame = pd.DataFrame([e.model_dump() for e in evidence])
        edited = st.data_editor(
            frame,
            hide_index=True,
            num_rows="dynamic",
            width="stretch",
            key=f"onboarding_evidence_{session.id}_{len(session.history)}",
            column_config={
                "summary": st.column_config.TextColumn(width="large"),
                "quote": st.column_config.TextColumn(width="large"),
                "kind": st.column_config.SelectboxColumn(
                    options=["education", "project", "professional_experience", "skill", "other"]
                ),
            },
        )
        roles = st.text_input(
            "Target roles (comma separated)",
            value=", ".join(
                previous.target_roles if previous else session.analysis.suggested_roles
            ),
            key=f"review_roles_{session.id}",
        )
        interests = st.text_area(
            "The work I want to do",
            value=previous.interests if previous else session.analysis.suggested_interests,
            key=f"review_interests_{session.id}",
        )
        years_value = previous.professional_years if previous else session.professional_years
        years = st.text_input(
            "Professional experience in years",
            value=str(years_value) if years_value is not None else "",
            help="Confirm your actual professional tenure. Projects remain separate. Blank means unknown.",
            key=f"review_years_{session.id}",
        )
        st.markdown("**Your confirmed intake**")
        show_intake(session.intake)
        reviewed = st.checkbox(
            "I reviewed my evidence, interests, experience, and constraints.",
            key=f"profile_approval_{session.id}",
        )
        submit = st.form_submit_button(
            "Confirm profile & prepare search brief", type="primary", disabled=not configured
        )
    if submit:
        try:
            if not reviewed:
                raise ValueError("Review the profile and check the confirmation before continuing.")
            items = [Evidence.model_validate(row) for row in edited.to_dict("records")]
            validate_evidence(items, session.documents)
            target_roles = [r.strip() for r in roles.split(",") if r.strip()]
            if not items or not target_roles or not interests.strip():
                raise ValueError(
                    "Keep supported evidence, at least one target role, and a description of the work you want."
                )
            professional_years = float(years) if years.strip() else None
            if professional_years is not None and not 0 <= professional_years <= 70:
                raise ValueError("Professional experience must be between 0 and 70 years.")
            profile = Profile(
                documents=session.documents,
                evidence=items,
                target_roles=target_roles,
                interests=interests.strip(),
                constraints=intake_constraints(session.intake),
                professional_years=professional_years,
                confirmed=True,
                intake=session.intake,
            )
            # Save the reviewed profile even if brief generation fails; retry need not
            # discard the applicant's edits or repeat the interview.
            st.session_state.profile = profile
            st.session_state.pop("run", None)
            st.session_state.pop("review_report", None)
            st.session_state.onboarding_last_run = None
            persist_progress(store)
            with st.spinner("Turning your profile into a concrete search plan…"):
                brief = propose_search_brief(get_client(), profile)
            st.session_state.brief_draft = brief
            session.stage = "brief"
            persist_progress(store)
            st.rerun()
        except Exception as exc:
            show_error(exc)
    if st.button("Edit intake details", key="edit_intake_details"):
        session.stage = "intake"
        st.session_state.onboarding_last_run = None
        st.session_state.pop("run", None)
        if previous:
            previous.confirmed = False
            previous.search_brief = None
        persist_progress(store)
        st.rerun()
    with st.expander("Remaining unknowns and original sources"):
        for missing in session.analysis.missing_information:
            st.write("• " + missing)
        for document in session.documents:
            st.markdown(f"**{document.id} · {document.label}**")
            st.text(document.text)


def _brief_review(session, show_error, store):
    profile = st.session_state.profile
    brief = st.session_state.brief_draft
    st.subheader("4. Review what we'll actually search for")
    st.write(
        "This is the plan discovery will receive. Adjust it until it describes work you want and a credible reason your background fits. Nothing has been searched yet."
    )
    st.caption(
        "Roles: "
        + ", ".join(profile.target_roles)
        + " · Change roles and constraints through Edit profile below."
    )
    with st.form(f"brief_review_{session.id}"):
        summary = st.text_area(
            "Search objective", value=brief.summary, key=f"brief_summary_{session.id}"
        )
        themes = st.text_area(
            "Work themes (one per line)",
            value="\n".join(brief.work_themes),
            key=f"brief_themes_{session.id}",
        )
        st.markdown("**Sectors to search, in order**")
        st.caption(
            "Preferred sectors first, then related and broader alternatives. We discover companies in each sector before searching their jobs. Sector interest does not imply sector experience."
        )
        sector_frame = pd.DataFrame(
            [s.model_dump() for s in brief.sectors], columns=["name", "relationship", "rationale"]
        )
        sector_rows = st.data_editor(
            sector_frame,
            num_rows="dynamic",
            hide_index=True,
            width="stretch",
            key=f"brief_sectors_{session.id}",
            column_config={
                "name": st.column_config.TextColumn("Sector"),
                "relationship": st.column_config.SelectboxColumn(
                    "Priority", options=["preferred", "related", "expanded"]
                ),
                "rationale": st.column_config.TextColumn("Why this sector", width="large"),
            },
        )
        st.markdown("**Experience to search around**")
        strengths_frame = pd.DataFrame(
            [
                {"statement": s.statement, "evidence_ids": ", ".join(s.evidence_ids)}
                for s in brief.strengths
            ]
        )
        edited_strengths = st.data_editor(
            strengths_frame,
            num_rows="dynamic",
            hide_index=True,
            width="stretch",
            key=f"brief_strengths_{session.id}",
            column_config={
                "statement": st.column_config.TextColumn(
                    "Relevant strength / experience", width="large"
                ),
                "evidence_ids": st.column_config.TextColumn("Supporting evidence IDs"),
            },
        )
        queries = st.text_area(
            "Proposed web searches (one per line, up to four)",
            value="\n".join(brief.search_queries),
            height=130,
            key=f"brief_queries_{session.id}",
        )
        avoid = st.text_area(
            "Exclude or avoid (one per line)",
            value="\n".join(brief.avoid),
            key=f"brief_avoid_{session.id}",
        )
        stretch_policy = st.text_area(
            "How to handle stretches",
            value=brief.stretch_policy,
            key=f"brief_stretches_{session.id}",
        )
        unknowns = st.text_area(
            "Still unresolved (one per line)",
            value="\n".join(brief.unresolved_questions),
            key=f"brief_unknowns_{session.id}",
        )
        if profile.constraints:
            st.markdown("**Your constraints travel with this brief**")
            for c in profile.constraints:
                st.write(
                    f"{'Required' if is_hard_constraint(c) else 'Preference'} · {c.field}: {c.value}"
                )
        st.caption(
            "Your role, interest, location, experience level, and the plan above inform discovery. Your raw resume is not included in the discovery request. Full constraints are checked against retrieved descriptions."
        )
        approved = st.checkbox(
            "I approve this search brief and understand what it will look for.",
            key=f"brief_approval_{session.id}",
        )
        submit = st.form_submit_button("Confirm search brief", type="primary")
    if submit:
        try:
            if not approved:
                raise ValueError(
                    "Review the brief and check its confirmation before unlocking discovery."
                )
            strengths = [
                SearchStrength(
                    statement=str(row["statement"]),
                    evidence_ids=[
                        e.strip() for e in str(row["evidence_ids"]).split(",") if e.strip()
                    ],
                )
                for row in edited_strengths.to_dict("records")
            ]
            sectors = [SectorTarget.model_validate(row) for row in sector_rows.to_dict("records")]
            if not sectors:
                raise ValueError("Choose at least one sector before approving the search brief.")
            revised = SearchBriefDraft(
                summary=summary.strip(),
                target_roles=profile.target_roles,
                work_themes=_lines(themes),
                strengths=strengths,
                search_queries=_lines(queries),
                avoid=_lines(avoid),
                stretch_policy=stretch_policy.strip(),
                unresolved_questions=_lines(unknowns),
                sectors=sectors,
            )
            profile.search_brief = confirm_search_brief(profile, revised)
            st.session_state.brief_draft = revised
            st.session_state.pop("run", None)
            session.stage = "ready"
            persist_progress(store)
            st.rerun()
        except Exception as exc:
            show_error(exc)


def _edit_intake(session, get_client, show_error, configured, store):
    st.subheader("Confirm your intake details")
    st.caption(
        "Your resumes and existing answers are retained. These explicit values guide the interview and brief."
    )
    previous = st.session_state.get("profile")
    if session.intake is None and previous and previous.constraints:
        st.info(
            "Your earlier declarations are below. Confirm them in the explicit fields; leaving an optional preference blank now means no restriction."
        )
        for constraint in previous.constraints:
            st.write(f"**Previously entered {constraint.field}:** {constraint.value}")
    with st.form(f"edit_intake_{session.id}"):
        values = intake_inputs(f"edit_intake_{session.id}_", session.intake)
        submit = st.form_submit_button("Save intake & continue", type="primary")
    if submit:
        try:
            intake = parse_intake(values)
            session.intake = intake
            previous = st.session_state.get("profile")
            if previous:
                previous.intake = intake
                previous.constraints = intake_constraints(intake)
                previous.confirmed, previous.search_brief = False, None
            st.session_state.pop("brief_draft", None)
            st.session_state.pop("run", None)
            st.session_state.onboarding_last_run = None
            persist_progress(store)
            if session.interview_finished:
                session.stage = "profile"
            else:
                from jobengine.onboarding.interview import analyze_background

                session.analysis = analyze_background(
                    get_client(),
                    session.documents,
                    session.role_hint,
                    session.interest_hint,
                    session.history,
                    intake,
                    session.stop_project_questions,
                )
                session.stage = "interview" if session.analysis.next_question else "profile"
                session.interview_finished = session.analysis.next_question is None
            persist_progress(store)
            st.rerun()
        except Exception as exc:
            show_error(exc)


def render_onboarding(get_client: Callable, show_error: Callable, configured: bool, store):
    session = st.session_state.get("onboarding")
    previous = st.session_state.get("profile")
    if session is None and previous:
        # Existing app sessions can review the new brief without re-entering sources.
        session = OnboardingSession(
            id=uuid.uuid4().hex,
            documents=previous.documents,
            role_hint=", ".join(previous.target_roles),
            interest_hint=previous.interests,
            professional_years=previous.professional_years,
            history=[],
            analysis=OnboardingAnalysis(
                summary="Review your existing candidate profile, then prepare and approve a search brief.",
                evidence=previous.evidence,
                conflicts=[],
                missing_information=[],
                suggested_roles=previous.target_roles,
                suggested_interests=previous.interests,
                preference_suggestions=[],
                next_question=None,
            ),
            interview_finished=True,
            stage="intake",
            intake=previous.intake,
        )
        st.session_state.onboarding = session
        persist_progress(store)
    if session is None:
        _start_form(get_client, show_error, configured, store)
        return

    stages = {
        "intake": (1, "Intake details"),
        "interview": (2, "Conversation"),
        "profile": (3, "Profile review"),
        "brief": (4, "Search brief"),
        "ready": (5, "Ready to discover"),
    }
    step, label = stages[session.stage]
    st.progress(step / 5, text=f"Step {step} of 5 · {label}")
    if session.stage == "intake":
        _edit_intake(session, get_client, show_error, configured, store)
    elif session.stage == "interview":
        _interview(session, get_client, show_error, configured, store)
    elif session.stage == "profile":
        _profile_review(session, get_client, show_error, configured, store)
    elif session.stage == "brief":
        _brief_review(session, show_error, store)
    else:
        profile = st.session_state.profile
        try:
            brief = require_confirmed_search_brief(profile)
        except ValueError as exc:
            st.warning(str(exc))
            session.stage = "profile"
            persist_progress(store)
            st.rerun()
        st.success("Onboarding complete. Your profile and search brief are confirmed.")
        st.subheader("Your approved search plan")
        st.write(brief.summary)
        st.write("Work themes: " + ", ".join(brief.work_themes))
        if brief.sectors:
            st.write(
                "Sector order: "
                + " → ".join(
                    s.name
                    for s in sorted(
                        brief.sectors,
                        key=lambda s: {"preferred": 0, "related": 1, "expanded": 2}[s.relationship],
                    )
                )
            )
        else:
            st.info(
                "This earlier brief needs an ordered sector plan for the new discovery procedure. Your profile and interview are retained."
            )
            if st.button("Prepare sector plan for review", disabled=not configured):
                try:
                    with st.spinner("Proposing sectors from your stated interests…"):
                        proposal = propose_sectors(get_client(), profile)
                    revised = SearchBriefDraft.model_validate(
                        brief.model_dump(include=set(SearchBriefDraft.model_fields))
                    )
                    revised.sectors = proposal.sectors
                    st.session_state.brief_draft = revised
                    profile.search_brief = None
                    session.stage = "brief"
                    persist_progress(store)
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
        for strength in brief.strengths:
            st.write("• " + strength.statement + " (" + ", ".join(strength.evidence_ids) + ")")
        st.code("\n".join(brief.search_queries), language=None)
        st.write("Stretch policy: " + brief.stretch_policy)
        for unknown in brief.unresolved_questions:
            st.caption("Unresolved: " + unknown)
        if st.button(
            "Regenerate search plan",
            disabled=not configured,
            help="Use your reviewed profile to prepare a fresh plan. Review it before searching again.",
        ):
            try:
                with st.spinner("Preparing a fresh search plan from your reviewed profile…"):
                    revised = propose_search_brief(get_client(), profile)
                st.session_state.brief_draft = revised
                profile.search_brief = None
                st.session_state.pop("run", None)
                st.session_state.onboarding_last_run = None
                session.stage = "brief"
                persist_progress(store)
                st.rerun()
            except Exception as exc:
                show_error(exc)
        st.download_button(
            "Export profile and approved search brief",
            profile.model_dump_json(indent=2),
            file_name="candidate-profile.json",
            mime="application/json",
        )

    if session.stage in {"brief", "ready"}:
        left, right = st.columns(2)
        if left.button("Edit profile", key="edit_onboarding_profile"):
            st.session_state.profile.search_brief = None
            st.session_state.profile.confirmed = False
            st.session_state.pop("run", None)
            st.session_state.onboarding_last_run = None
            session.stage = "profile"
            persist_progress(store)
            st.rerun()
        if session.stage == "ready" and right.button("Edit search brief", key="edit_search_brief"):
            st.session_state.profile.search_brief = None
            st.session_state.pop("run", None)
            st.session_state.onboarding_last_run = None
            session.stage = "brief"
            persist_progress(store)
            st.rerun()
    if session.history:
        with st.expander("Your onboarding conversation"):
            for exchange in session.history:
                st.markdown("**" + exchange.question + "**")
                st.write(exchange.answer or "Skipped — left unknown.")
    if session.attempts:
        with st.expander("Redirected and skipped messages"):
            for attempt in session.attempts:
                st.write(attempt.text)
                st.caption(
                    "Redirected to onboarding"
                    if attempt.disposition == "off_topic"
                    else "Requested to move on"
                )
    st.download_button(
        "Save interview sources & answers",
        json.dumps(session.model_dump(mode="json"), indent=2),
        file_name="onboarding-progress.json",
        mime="application/json",
    )
    st.caption("Progress is saved automatically on this computer after each accepted step.")
