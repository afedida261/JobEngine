"""Streamlit session state is a view of durable local onboarding snapshots."""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from jobengine.models import OnboardingAnalysis, OnboardingSession, Profile, Run, SavedOnboarding


def _local_time(value):
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(ZoneInfo("America/New_York"))
        .strftime("%b %d, %I:%M %p")
    )


def persist_progress(store):
    if "onboarding" not in st.session_state:
        return
    session = OnboardingSession.model_validate(st.session_state.onboarding.model_dump())
    run = st.session_state.get("run")
    profile = st.session_state.get("profile")
    if profile:
        profile = Profile.model_validate(profile.model_dump())
        st.session_state.profile = profile
    brief = st.session_state.get("brief_draft")
    record = SavedOnboarding(
        session=session,
        profile=profile,
        brief_draft=brief.model_dump() if brief else None,
        last_run_id=run.id if run else st.session_state.get("onboarding_last_run"),
        revision=st.session_state.get("onboarding_revision", 0),
    )
    saved = store.save_onboarding(record)
    st.session_state.onboarding_revision = saved.revision
    st.session_state.onboarding_saved_at = saved.updated_at


def _apply_record(store, record):
    st.session_state.onboarding = record.session
    st.session_state.onboarding_revision = record.revision
    st.session_state.onboarding_saved_at = record.updated_at
    for key, value in (("profile", record.profile), ("brief_draft", record.brief_draft)):
        if value is None:
            st.session_state.pop(key, None)
        else:
            st.session_state[key] = value
    st.session_state.onboarding_last_run = record.last_run_id
    st.session_state.pop("run", None)
    st.session_state.pop("viewed_run", None)
    st.session_state.pop("review_report", None)
    if record.last_run_id:
        saved_run = store.load_run(record.last_run_id)
        if saved_run:
            run = Run.model_validate(saved_run)
            st.session_state.run = run
            restore_run_review(store, run)
    if record.session.intake is None:
        record.session.stage = "intake"


def restore_run_review(store, run):
    st.session_state.pop("review_report", None)
    review = store.load_run("review" + run.id)
    if review:
        st.session_state.review_report = review
        labels = {label["url"]: label for label in review["labels"]}
        for index, item in enumerate(run.review_items()):
            if item.url in labels:
                label = labels[item.url]
                st.session_state[f"review-{run.id}-{index}"] = label["label"]
                st.session_state[f"note-{run.id}-{index}"] = label["note"]
                st.session_state[f"unsupported-{run.id}-{index}"] = label["unsupported_claim"]


def view_saved_results(store, identifier):
    """Browse a completed run without importing or modifying the active candidate."""
    run = Run.model_validate(store.load_run(identifier))
    st.session_state.viewed_run = run
    restore_run_review(store, run)


def restore_progress_once(store):
    if st.session_state.get("onboarding_restore_checked"):
        return
    if "onboarding" in st.session_state:
        # Preserve an already-open pre-upgrade browser session and its real transcript.
        st.session_state.onboarding = OnboardingSession.model_validate(
            st.session_state.onboarding.model_dump()
        )
        if st.session_state.onboarding.intake is None:
            st.session_state.onboarding.stage = "intake"
        persist_progress(store)
    else:
        saved = store.load_onboarding()
        if saved:
            _apply_record(store, saved)
    st.session_state.onboarding_restore_checked = True


def saved_progress_controls(store):
    saved = store.list_onboardings()
    previous_runs = store.list_runs()
    if not saved and not previous_runs:
        return
    with st.expander("Saved candidates and previous runs"):
        if saved:
            entries = {entry["id"]: entry for entry in saved}
            choice = st.selectbox(
                "Saved onboarding",
                list(entries),
                index=None,
                format_func=lambda key: (
                    entries[key]["label"] + " · " + _local_time(entries[key]["updated_at"])
                ),
                key="saved_onboarding_choice",
            )
            if st.button("Resume saved candidate", disabled=choice is None):
                record = store.load_onboarding(choice)
                _apply_record(store, record)
                store.activate_onboarding(choice)
                st.rerun()
        if previous_runs:
            entries = {entry["id"]: entry for entry in previous_runs}
            choice = st.selectbox(
                "Earlier search profile",
                list(entries),
                index=None,
                format_func=lambda key: (
                    entries[key]["label"]
                    + " · "
                    + _local_time(entries[key]["created_at"])
                    + " · "
                    + key[:8]
                ),
                key="previous_run_choice",
            )
            if st.button("View saved results", disabled=choice is None):
                view_saved_results(store, choice)
                st.rerun()
            if st.button("Continue from earlier profile", disabled=choice is None):
                run = Run.model_validate(store.load_run(choice))
                profile = run.profile
                # The old run saved answer sources but not a full canonical transcript.
                session = OnboardingSession(
                    id=uuid.uuid4().hex,
                    documents=profile.documents,
                    role_hint=", ".join(profile.target_roles),
                    interest_hint=profile.interests,
                    professional_years=profile.professional_years,
                    history=[],
                    analysis=OnboardingAnalysis(
                        summary="Your previous profile and answer sources are retained. Confirm the new structured intake before reviewing your profile.",
                        evidence=profile.evidence,
                        conflicts=[],
                        missing_information=[],
                        suggested_roles=profile.target_roles,
                        suggested_interests=profile.interests,
                        preference_suggestions=[],
                        next_question=None,
                    ),
                    interview_finished=True,
                    stage="intake",
                )
                record = store.save_onboarding(
                    SavedOnboarding(session=session, profile=profile, last_run_id=run.id)
                )
                _apply_record(store, record)
                st.rerun()
