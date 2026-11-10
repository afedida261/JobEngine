"""Bounded local worker pool. No automatic retries or model calls on GET requests."""

from __future__ import annotations

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

from jobengine.infrastructure.llm import DEFAULT_INTERVIEW_MODEL, DEFAULT_SEARCH_MODEL, ModelClient
from jobengine.onboarding.brief import propose_search_brief, require_confirmed_search_brief
from jobengine.onboarding.guided import continue_guided
from jobengine.onboarding.interview import answer_question, start_onboarding
from jobengine.search.pipeline import run_search
from jobengine.web.workspace import Workspace

WORKER = uuid.uuid4().hex
POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jobengine")
logger = logging.getLogger(__name__)


def prepare_interview(workspace, identifier, session, operation):
    answering = operation != "intake"
    workspace.update_task(
        identifier,
        message=(
            "Saving your answer and finding the next useful question."
            if answering
            else "Your details are saved. Reviewing your background and interests."
        ),
    )
    if session.interview_mode == "guided":
        updated = continue_guided(session, answering=answering, skip=operation == "skip")
        workspace.update_task(identifier, message="Your next question is ready.")
        return updated
    try:
        client = get_client(workspace.store)
        workspace.update_task(
            identifier,
            message=(
                "Connecting your answer to your experience."
                if answering
                else "Personalizing questions around your experience and the work you want."
            ),
        )
        if answering:
            updated = answer_question(
                client, session, session.pending_answer or "", skip=operation == "skip"
            )
        else:
            updated = start_onboarding(
                client,
                session.documents,
                session.role_hint,
                session.interest_hint,
                session.professional_years,
                session.intake,
            )
            updated.id = session.id
            updated.general_information = session.general_information
    except Exception as exc:
        # Exception messages and provider payloads can contain private material.
        # A class name is sufficient to diagnose the unavailable dependency safely.
        logger.warning(
            "Interview personalization unavailable (%s); continuing guided interview.",
            type(exc).__name__,
        )
        workspace.update_task(
            identifier,
            message="Preparing a guided conversation about your goals and preferences.",
        )
        updated = continue_guided(session, answering=answering, skip=operation == "skip")
    workspace.update_task(
        identifier,
        message="Your next step is ready."
        if updated.interview_finished
        else "Your questions are ready. Opening your interview.",
    )
    return updated


def describe_progress(message):
    """Turn actual pipeline callbacks into applicant-facing sentences."""
    if ": discovering companies" in message:
        sector = message.split(": discovering companies", 1)[0]
        return f"I'm finding employers in {sector}."
    if " → " in message and ": " in message:
        _, detail = message.split(" → ", 1)
        company, action = detail.split(": ", 1)
        if action.startswith("finding actual jobs"):
            return f"I'm looking for open roles at {company}."
        if action.startswith("retrieving and evaluating "):
            role = action.removeprefix("retrieving and evaluating ").rstrip("…. ")
            return (
                f"I'm reading the {role} posting at {company} and checking it against your profile."
            )
    return message


def get_client(store):
    return ModelClient(
        os.getenv("OPENAI_API_KEY", ""),
        store,
        os.getenv("OPENAI_SEARCH_MODEL", DEFAULT_SEARCH_MODEL),
        interview_model=os.getenv("OPENAI_INTERVIEW_MODEL", DEFAULT_INTERVIEW_MODEL),
    )


def launch(workspace, record, operation, destination):
    identifier = workspace.create_task(record, operation, destination, WORKER)
    try:
        POOL.submit(execute, workspace.id, identifier)
    except RuntimeError:
        workspace.update_task(
            identifier, status="failed", message="Please restart the app and retry."
        )
    return identifier


def execute(workspace_id, identifier):
    workspace = Workspace(workspace_id)
    task = workspace.task(identifier)
    record = workspace.load(task["candidate"])
    try:
        session = record.session
        if task["operation"] in {"intake", "answer", "skip"}:
            record.session = prepare_interview(workspace, identifier, session, task["operation"])
            workspace.save(record, identifier)
        elif task["operation"] == "plan":
            client = get_client(workspace.store)
            workspace.update_task(
                identifier,
                message="I'm building a search plan around your experience and the work you want to do.",
            )
            record.brief_draft = propose_search_brief(client, record.profile)
            record.session.stage = "brief"
            workspace.save(record, identifier)
        elif task["operation"] == "search":
            require_confirmed_search_brief(record.profile)
            client = get_client(workspace.store)
            run = run_search(
                client,
                record.profile,
                progress=lambda message: workspace.update_task(
                    identifier, message=describe_progress(message)
                ),
            )
            workspace.add_run(run, record.session.id)
            record.last_run_id = run.id
            workspace.save(record, identifier)
        else:
            raise ValueError("Unknown step. Please return to your workspace.")
        workspace.update_task(
            identifier,
            status="complete",
            message=(
                "Your search is complete. Your dashboard is ready."
                if task["operation"] == "search"
                else "Your next step is ready."
            ),
        )
    except Exception:
        # SDK exception text may include applicant data or credentials. Never store or display it.
        workspace.update_task(
            identifier,
            status="failed",
            message="We couldn't complete this step. Your saved progress is safe. Check the connection, API configuration, or local budget in Settings, then retry when you're ready.",
        )


def recover_task(workspace, task):
    if task and task["status"] == "running" and task["worker"] != WORKER:
        workspace.update_task(
            task["id"],
            status="failed",
            message="The app restarted during this step. Your saved progress is safe. Review it and retry explicitly; a previous request may have used part of your budget.",
        )
        return workspace.task(task["id"])
    return task
