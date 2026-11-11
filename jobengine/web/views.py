from __future__ import annotations

import json
import os
import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from jobengine.grounding import validate_evidence
from jobengine.models import (
    Profile,
    SavedOnboarding,
    SearchBriefDraft,
    SearchStrength,
    SectorTarget,
)
from jobengine.onboarding.brief import confirm_search_brief, require_confirmed_search_brief
from jobengine.onboarding.interview import finish_interview
from jobengine.onboarding.policy import MAX_QUESTIONS
from jobengine.onboarding.preferences import intake_constraints
from jobengine.web import forms, tasks
from jobengine.web.workspace import Workspace

STAGES = {"intake": 0, "interview": 1, "profile": 2, "brief": 3, "ready": 4}
CATEGORIES = {
    "strong": "Strong fit",
    "weaker": "Worth exploring",
    "needs_review": "Needs a closer look",
    "unverified": "Source unverified",
    "excluded": "Outside your criteria",
}


def workspace_for(request):
    return Workspace(request.session["workspace"])


def next_url(record):
    if not record:
        return reverse("start")
    if record.session.stage == "intake" and record.session.intake is None:
        return reverse("preferences")
    return reverse(
        {
            "intake": "interview",
            "interview": "interview",
            "profile": "profile",
            "brief": "plan",
            "ready": "plan",
        }[record.session.stage]
    )


def page(request, template, *, nav="overview", title="Your workspace", **context):
    workspace = workspace_for(request)
    current = workspace.current()
    active_task = tasks.recover_task(workspace, workspace.task())
    dashboard_available = bool(workspace.runs()) or bool(
        current and current.session.stage in {"profile", "brief", "ready"}
    )
    stage = STAGES[current.session.stage] if current else 0
    steps = [
        {
            "label": label,
            "description": description,
            "number": index + 1,
            "done": bool(current) and stage > index,
            "active": stage == index,
        }
        for index, (label, description) in enumerate(
            [
                ("Your background", "A resume or a little about you"),
                ("A conversation", "A few questions, chosen for you"),
                ("Your profile", "Make sure we've understood"),
                ("Your search plan", "You're in control of the direction"),
            ]
        )
    ]
    return render(
        request,
        f"jobengine/{template}.html",
        {
            "nav": nav,
            "page_title": title,
            "current": current,
            "steps": steps,
            "continue_url": next_url(current),
            "active_task": active_task,
            "bookmark_count": len(workspace.bookmarks()),
            "application_count": len(workspace.applications()),
            "dashboard_available": dashboard_available,
            "focus_mode": template in {"start", "preferences", "interview", "activity"}
            or (template in {"profile", "plan"} and not dashboard_available),
            "max_questions": MAX_QUESTIONS,
            **context,
        },
    )


def revision_check(request, record):
    form = forms.RevisionForm(request.POST)
    if not form.is_valid() or form.cleaned_data["revision"] != record.revision:
        raise ValueError("This page has older progress. Reload it before saving your changes.")


def launch(request, workspace, record, operation, destination):
    task_id = tasks.launch(workspace, record, operation, reverse(destination))
    if request.headers.get("Accept") == "application/json":
        return JsonResponse(
            {
                "task_url": reverse("activity", args=[task_id]) + "?format=json",
                "activity_url": reverse("activity", args=[task_id]),
            },
            status=202,
        )
    return redirect("activity", identifier=task_id)


@require_GET
def overview(request):
    workspace = workspace_for(request)
    record = workspace.current()
    active = tasks.recover_task(workspace, workspace.task())
    if active:
        return redirect("activity", identifier=active["id"])
    if not record:
        return start(request)
    history = workspace.runs()
    if not history and record.session.stage in {"intake", "interview"}:
        return redirect(next_url(record))
    run = (workspace.run(record.last_run_id) or history[0]) if history else None
    rows = [
        row
        for row in (job_rows(workspace, run) if run else [])
        if row["job"].category in {"strong", "weaker", "needs_review"}
    ]
    return page(
        request,
        "overview",
        title="Your dashboard",
        run=run,
        rows=rows[:3],
        suggested_count=len(rows),
        needs_review=record.session.stage != "ready",
    )


@require_http_methods(["GET", "POST"])
def start(request):
    workspace = workspace_for(request)
    if workspace.current():
        return redirect(next_url(workspace.current()))
    demo_values = jane_demo_values(request)
    demo_requested = request.method == "POST" and request.POST.get("demo") == "jane"
    if demo_requested and not demo_values:
        raise Http404
    form = forms.IntakeForm(
        demo_values if demo_requested else request.POST or None,
        None if demo_requested else request.FILES or None,
    )
    if request.method == "POST" and form.is_valid():
        session = form.create_session()
        try:
            record = workspace.save(SavedOnboarding(session=session))
            return launch(request, workspace, record, "intake", "interview")
        except (ValueError, RuntimeError) as exc:
            form.add_error(None, str(exc))
    return page(
        request,
        "start",
        nav="profile",
        title="Let's start with you",
        form=form,
        landing_page=True,
        schools=forms.SCHOOLS,
        areas=forms.AREAS,
        jane_demo=demo_values,
    )


def jane_demo_values(request):
    """Local development convenience; fictional source material stays in .scratch."""
    if not local_library_allowed(request):
        return None
    source = settings.BASE_DIR / ".scratch" / "jane-doe"
    try:
        values = json.loads((source / "intake.json").read_text(encoding="utf-8"))
        values["background"] = (source / "resume.md").read_text(encoding="utf-8")
        return values
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


@require_http_methods(["GET", "POST"])
def interview(request):
    workspace = workspace_for(request)
    record = workspace.current()
    if not record:
        return redirect("start")
    if record.session.stage == "intake" and record.session.intake is None:
        return redirect("preferences")
    if record.session.stage not in {"intake", "interview"}:
        return redirect("overview")
    pending = tasks.recover_task(workspace, workspace.task())
    if pending and pending["operation"] == "intake":
        return redirect("activity", identifier=pending["id"])
    if request.method == "POST":
        try:
            revision_check(request, record)
            action = request.POST.get("action", "answer")
            if action == "finish" and record.session.analysis.evidence:
                record.session = finish_interview(record.session)
                workspace.save(record)
                return redirect("overview")
            if record.session.stage == "intake":
                return launch(request, workspace, record, "intake", "interview")
            answer = request.POST.get("answer", "").strip()
            if action not in {"answer", "skip"}:
                raise ValueError(
                    "Choose an answer, skip this question, or finish the conversation."
                )
            if action == "answer" and not 8 <= len(answer) <= 8000:
                raise ValueError("Add a little detail (8–8,000 characters), or skip this question.")
            record.session.pending_answer = answer if action == "answer" else None
            record.session.pending_question = record.session.analysis.next_question.question
            record = workspace.save(record)
            return launch(request, workspace, record, action, "interview")
        except (ValueError, RuntimeError) as exc:
            messages.error(request, str(exc))
    if record.session.stage == "intake":
        # Older saved intake records use the same loading screen. Preparing still
        # requires a CSRF-protected POST, never a paid request on GET.
        return page(
            request,
            "activity",
            title="Preparing your interview",
            preparing_record=record,
            events=[],
        )
    return page(
        request,
        "interview",
        nav="profile",
        title="Interview questions",
        record=record,
        question=record.session.analysis.next_question,
        answer=request.POST.get("answer", record.session.pending_answer or ""),
        pending_task=pending,
    )


@require_http_methods(["GET", "POST"])
def profile(request):
    workspace = workspace_for(request)
    record = workspace.current()
    if not record:
        return redirect("start")
    if record.session.stage in {"intake", "interview"}:
        return redirect("interview")
    previous = record.profile
    selected_evidence = {item.id: item for item in previous.evidence} if previous else {}
    evidence = [selected_evidence.get(item.id, item) for item in record.session.analysis.evidence]
    analysis_ids = {item.id for item in evidence}
    evidence.extend(item for key, item in selected_evidence.items() if key not in analysis_ids)
    editing = (
        request.method == "POST"
        or request.GET.get("edit") == "1"
        or record.session.stage == "profile"
    )
    form = forms.ProfileForm(
        request.POST or None,
        initial={
            "revision": record.revision,
            "roles": ", ".join(
                previous.target_roles if previous else record.session.analysis.suggested_roles
            ),
            "interests": previous.interests
            if previous
            else record.session.analysis.suggested_interests,
            "professional_years": previous.professional_years
            if previous
            else record.session.professional_years,
        },
    )
    evidence_forms = forms.EvidenceFormSet(
        request.POST or None,
        prefix="evidence",
        initial=[
            {"include": not previous or item.id in selected_evidence, "summary": item.summary}
            for item in evidence
        ],
    )
    if request.method == "POST":
        valid = form.is_valid() & evidence_forms.is_valid()
        if valid:
            try:
                revision_check(request, record)
                if len(evidence_forms) != len(evidence):
                    raise ValueError("Your experience changed. Reload this page before continuing.")
                selected = [
                    item.model_copy(update={"summary": row.cleaned_data["summary"]})
                    for item, row in zip(evidence, evidence_forms)
                    if row.cleaned_data.get("include")
                ]
                roles = [
                    role.strip() for role in form.cleaned_data["roles"].split(",") if role.strip()
                ]
                if not selected or not roles:
                    raise ValueError(
                        "Include at least one experience and a role you'd like to explore."
                    )
                validate_evidence(selected, record.session.documents)
                record.profile = Profile(
                    documents=record.session.documents,
                    evidence=selected,
                    target_roles=roles,
                    interests=form.cleaned_data["interests"],
                    professional_years=form.cleaned_data["professional_years"],
                    intake=record.session.intake,
                    constraints=intake_constraints(record.session.intake)
                    if record.session.intake
                    else (previous.constraints if previous else []),
                    confirmed=True,
                )
                record.brief_draft = None
                record.last_run_id = None
                record.session.stage = "profile"
                record = workspace.save(record)
                return launch(request, workspace, record, "plan", "plan")
            except (ValueError, RuntimeError) as exc:
                form.add_error(None, str(exc))
    return page(
        request,
        "profile",
        nav="profile",
        title="A little more you, on paper",
        record=record,
        form=form,
        evidence_forms=evidence_forms,
        evidence_rows=list(zip(evidence, evidence_forms)),
        editing=editing,
        profile_evidence=previous.evidence if previous else evidence,
    )


@require_http_methods(["GET", "POST"])
def preferences(request):
    workspace = workspace_for(request)
    record = workspace.current()
    if not record:
        return redirect("start")
    onboarding = record.session.stage == "intake" and not record.session.analysis.evidence
    form = forms.PreferencesForm(
        request.POST or None, initial=forms.PreferencesForm.initial_for(record.session.intake)
    )
    if request.method == "POST" and form.is_valid():
        try:
            revision_check(request, record)
            record.session.intake = form.intake
            if record.profile:
                record.profile.intake = form.intake
                record.profile.constraints = intake_constraints(form.intake)
                record.profile.confirmed = False
                record.profile.search_brief = None
            record.brief_draft = None
            record.last_run_id = None
            if record.session.interview_finished:
                record.session.stage = "profile"
            record = workspace.save(record)
            if onboarding:
                return launch(request, workspace, record, "intake", "interview")
            messages.success(
                request,
                "Preferences saved. Review your profile and a fresh search plan before searching.",
            )
            return redirect(next_url(record))
        except (ValueError, RuntimeError) as exc:
            form.add_error(None, str(exc))
    return page(
        request,
        "preferences",
        nav="profile",
        title="Make room for what matters",
        form=form,
        record=record,
        onboarding=onboarding,
    )


@require_http_methods(["GET", "POST"])
def plan(request):
    workspace = workspace_for(request)
    record = workspace.current()
    if not record or not record.profile or not record.profile.confirmed:
        return page(
            request,
            "locked",
            nav="plan",
            title="Your search, with a direction",
            locked_title="First, let's get to know you.",
            locked_text="Your search plan grows out of your background and the work you'd like to do.",
        )
    draft = record.brief_draft or record.profile.search_brief
    ready = False
    try:
        ready = bool(require_confirmed_search_brief(record.profile).sectors)
    except ValueError:
        pass
    editing = (
        request.method == "POST"
        and request.POST.get("action") == "approve"
        or request.GET.get("edit") == "1"
        or not ready
    )
    if request.method == "POST" and request.POST.get("action") in {"search", "regenerate"}:
        try:
            revision_check(request, record)
            if request.POST["action"] == "search":
                if record.session.stage != "ready" or not ready:
                    raise ValueError("Review and approve your current search plan first.")
                return launch(request, workspace, record, "search", "overview")
            record.profile.search_brief = None
            record.brief_draft = None
            record.last_run_id = None
            record.session.stage = "profile"
            record = workspace.save(record)
            return launch(request, workspace, record, "plan", "plan")
        except (ValueError, RuntimeError) as exc:
            messages.error(request, str(exc))
    if not draft:
        return page(
            request,
            "plan",
            nav="plan",
            title="Your search, with a direction",
            record=record,
            draft=None,
        )
    initial = draft.model_dump()
    for name in ("work_themes", "search_queries", "avoid", "unresolved_questions"):
        initial[name] = "\n".join(initial[name])
    initial["revision"] = record.revision
    data = (
        request.POST
        if request.method == "POST" and request.POST.get("action") == "approve"
        else None
    )
    form = forms.BriefForm(data, initial=initial)
    sectors = forms.SectorFormSet(
        data,
        prefix="sectors",
        initial=[item.model_dump() for item in draft.sectors] or [{"relationship": "preferred"}],
    )
    strengths = forms.StrengthFormSet(
        data,
        prefix="strengths",
        initial=[
            {"statement": item.statement, "evidence_ids": item.evidence_ids}
            for item in draft.strengths
        ],
        form_kwargs={"evidence": record.profile.evidence},
    )
    if data is not None and (form.is_valid() & sectors.is_valid() & strengths.is_valid()):
        try:
            revision_check(request, record)
            values = form.cleaned_data
            revised = SearchBriefDraft(
                summary=values["summary"],
                target_roles=record.profile.target_roles,
                strengths=[
                    SearchStrength(
                        statement=row.cleaned_data["statement"],
                        evidence_ids=row.cleaned_data["evidence_ids"],
                    )
                    for row in strengths
                    if row.cleaned_data and not row.cleaned_data.get("DELETE")
                ],
                sectors=[
                    SectorTarget(
                        **{
                            key: row.cleaned_data[key]
                            for key in ("name", "relationship", "rationale")
                        }
                    )
                    for row in sectors
                    if row.cleaned_data and not row.cleaned_data.get("DELETE")
                ],
                work_themes=forms.lines(values["work_themes"]),
                search_queries=forms.lines(values["search_queries"]),
                avoid=forms.lines(values["avoid"]),
                stretch_policy=values["stretch_policy"],
                unresolved_questions=forms.lines(values["unresolved_questions"]),
            )
            if not revised.sectors:
                raise ValueError("Choose at least one industry or sector to explore.")
            record.profile.search_brief = confirm_search_brief(record.profile, revised)
            record.brief_draft = revised
            record.last_run_id = None
            record.session.stage = "ready"
            record = workspace.save(record)
            if request.POST.get("discover") == "1":
                return launch(request, workspace, record, "search", "overview")
            messages.success(
                request, "Your search plan is approved. You're ready to discover opportunities."
            )
            return redirect("plan")
        except (ValueError, RuntimeError) as exc:
            form.add_error(None, str(exc))
    return page(
        request,
        "plan",
        nav="plan",
        title="Your search, with a direction",
        record=record,
        form=form,
        sectors=sectors,
        strengths=strengths,
        draft=draft,
        ready=ready,
        editing=editing,
    )


def safe_url(value):
    try:
        parts = urlsplit(value)
        return value if parts.scheme in {"http", "https"} and parts.netloc else ""
    except ValueError:
        return ""


def job_rows(workspace, run):
    saved = set(workspace.bookmarks())
    applied = {row["url"] for row in workspace.applications()}
    return [
        {
            "job": job,
            "position": index,
            "run_id": run.id,
            "label": CATEGORIES[job.category],
            "saved": (run.id, index) in saved,
            "applied": job.discovery.url in applied,
            "source_url": safe_url(job.page.url or job.discovery.url),
            "company_initial": (job.facts.company if job.facts else job.discovery.company)[
                :1
            ].upper(),
        }
        for index, job in enumerate(run.inspected_jobs or run.jobs)
    ]


@require_GET
def opportunities(request):
    workspace = workspace_for(request)
    record = workspace.current()
    identifier = request.GET.get("run") or (record.last_run_id if record else None)
    run = workspace.run(identifier)
    if identifier and not run:
        raise Http404
    rows = job_rows(workspace, run) if run else []
    category = request.GET.get("category", "shortlist")
    query = request.GET.get("q", "").strip()[:200]
    counts = {key: sum(row["job"].category == key for row in rows) for key in CATEGORIES}
    shortlist = sum(counts[key] for key in ("strong", "weaker", "needs_review"))
    filters = [
        ("shortlist", "Your shortlist", shortlist),
        ("strong", "Strong fit", counts["strong"]),
        ("weaker", "Worth exploring", counts["weaker"]),
        ("needs_review", "Needs review", counts["needs_review"]),
        ("unverified", "Unverified leads", counts["unverified"]),
        ("excluded", "Outside criteria", counts["excluded"]),
    ]
    if category not in {item[0] for item in filters}:
        category = "shortlist"
    rows = [
        row
        for row in rows
        if (
            row["job"].category in {"strong", "weaker", "needs_review"}
            if category == "shortlist"
            else row["job"].category == category
        )
    ]
    if query:
        rows = [
            row
            for row in rows
            if query.casefold()
            in (
                row["job"].discovery.title
                + " "
                + row["job"].discovery.company
                + " "
                + (row["job"].facts.location or "" if row["job"].facts else "")
            ).casefold()
        ]
    return page(
        request,
        "opportunities",
        nav="opportunities",
        title="Find your reason to apply",
        run=run,
        rows=rows,
        category=category,
        query=query,
        filters=filters,
        counts=counts,
        shortlist=shortlist,
        run_history=workspace.runs(),
        historical=bool(run and (not record or record.last_run_id != run.id)),
    )


@require_GET
def saved(request):
    workspace = workspace_for(request)
    rows = []
    for run_id, position in workspace.bookmarks():
        run = workspace.run(run_id)
        if run:
            rows.extend(row for row in job_rows(workspace, run) if row["position"] == position)
    return page(request, "saved", nav="saved", title="Keep the possibilities close", rows=rows)


@require_GET
def applied(request):
    workspace = workspace_for(request)
    rows = []
    for application in workspace.applications():
        run = workspace.run(application["run"])
        if run:
            rows.extend(
                {**row, "applied_at": application["applied_at"]}
                for row in job_rows(workspace, run)
                if row["position"] == application["position"]
            )
    return page(request, "applied", nav="applied", title="Applied roles", rows=rows)


@require_POST
def application(request, identifier, position):
    action = request.POST.get("action")
    if action not in {"mark", "remove"}:
        return HttpResponse("Choose a valid application action.", status=400)
    try:
        workspace_for(request).set_application(identifier, position, applied=action == "mark")
    except ValueError:
        raise Http404 from None
    if request.POST.get("destination") == "applied":
        return redirect("applied")
    return redirect("job_detail", identifier=identifier, position=position)


@require_GET
def job_detail(request, identifier, position):
    workspace = workspace_for(request)
    run = workspace.run(identifier)
    if not run:
        raise Http404
    row = next((row for row in job_rows(workspace, run) if row["position"] == position), None)
    if row is None:
        raise Http404
    job = row["job"]
    requirements = {item.id: item for item in job.facts.requirements} if job.facts else {}
    evidence = {item.id: item for item in run.profile.evidence}
    assessments = (
        [
            {
                "assessment": item,
                "requirement": requirements.get(item.requirement_id),
                "evidence": [evidence[key] for key in item.evidence_ids if key in evidence],
            }
            for item in job.judgment.requirements
        ]
        if job.judgment
        else []
    )
    return page(
        request,
        "job_detail",
        nav="opportunities",
        title=job.discovery.title,
        row=row,
        run=run,
        assessments=assessments,
    )


@require_POST
def bookmark(request, identifier, position):
    try:
        workspace_for(request).toggle_bookmark(identifier, position)
    except ValueError:
        raise Http404 from None
    destination = request.POST.get("destination")
    if destination == "saved":
        return redirect("saved")
    if destination == "detail":
        return redirect("job_detail", identifier=identifier, position=position)
    return redirect(reverse("opportunities") + f"?run={identifier}")


@require_GET
def activity(request, identifier):
    workspace = workspace_for(request)
    task = tasks.recover_task(workspace, workspace.task(identifier))
    if not task:
        raise Http404
    if request.GET.get("format") == "json":
        return JsonResponse(
            {
                **{key: task[key] for key in ("status", "message", "destination")},
                "events": workspace.task_events(identifier),
            }
        )
    if task["status"] == "complete" and task["operation"] != "intake":
        return redirect(task["destination"])
    return page(
        request,
        "activity",
        title="Working on your search",
        task=task,
        events=workspace.task_events(identifier),
    )


@require_POST
def retry(request, identifier):
    workspace = workspace_for(request)
    task = tasks.recover_task(workspace, workspace.task(identifier))
    record = workspace.load(task["candidate"]) if task else None
    if not task or task["status"] != "failed" or not record:
        raise Http404
    valid_stage = {
        "intake": {"intake"},
        "answer": {"interview"},
        "skip": {"interview"},
        "plan": {"profile"},
        "search": {"ready"},
    }
    try:
        active = workspace.current()
        if (
            active is None
            or active.session.id != record.session.id
            or record.session.stage not in valid_stage[task["operation"]]
        ):
            raise ValueError("Your profile has moved on. Continue from your current step.")
        if task["operation"] == "search":
            require_confirmed_search_brief(record.profile)
        task_id = tasks.launch(workspace, record, task["operation"], task["destination"])
        return redirect("activity", identifier=task_id)
    except (ValueError, RuntimeError) as exc:
        messages.error(request, str(exc))
        return redirect("overview")


def local_library_allowed(request):
    return settings.LOCAL_LIBRARY and request.META.get("REMOTE_ADDR") in {"127.0.0.1", "::1"}


@require_http_methods(["GET", "POST"])
def library(request):
    workspace = workspace_for(request)
    local = local_library_allowed(request)
    if request.method == "POST":
        try:
            identifier = request.POST.get("candidate", "")
            if request.POST.get("action") == "import":
                if not local:
                    raise Http404
                workspace.import_legacy(identifier)
            else:
                workspace.activate(identifier)
            selected = workspace.current()
            return redirect("overview" if selected.last_run_id else next_url(selected))
        except (ValueError, RuntimeError) as exc:
            messages.error(request, str(exc))
    return page(
        request,
        "library",
        title="Pick up where you left off",
        candidates=workspace.candidates(),
        legacy=workspace.legacy_candidates() if local else [],
        local_library=local,
        restore_form=forms.RestoreForm(),
    )


@require_POST
def restore(request):
    form = forms.RestoreForm(request.POST, request.FILES)
    workspace = workspace_for(request)
    if form.is_valid():
        try:
            record = form.cleaned_data["backup"]
            record.session.id = uuid.uuid4().hex
            workspace.save(record)
            messages.success(
                request,
                "Your progress is restored. Review your profile and approve a new plan before searching.",
            )
            return redirect(next_url(record))
        except (ValueError, RuntimeError) as exc:
            form.add_error(None, str(exc))
    return page(
        request,
        "library",
        title="Restore your progress",
        candidates=workspace.candidates(),
        restore_form=form,
        restore_open=True,
    )


@require_POST
def new_profile(request):
    try:
        workspace_for(request).activate(None)
        return redirect("start")
    except (ValueError, RuntimeError) as exc:
        messages.error(request, str(exc))
        return redirect("overview")


@require_GET
def configuration(request):
    workspace = workspace_for(request)
    return page(
        request,
        "settings",
        nav="settings",
        title="A few things behind the scenes",
        configured=bool(os.getenv("OPENAI_API_KEY", "").strip()),
        usage=workspace.store.usage(),
    )


@require_GET
def export(request, kind, identifier):
    workspace = workspace_for(request)
    if kind == "profile":
        record = workspace.load(identifier)
        payload = record.profile if record else None
    elif kind == "progress":
        payload = workspace.load(identifier)
    elif kind == "run":
        payload = workspace.run(identifier)
    else:
        payload = None
    if payload is None:
        raise Http404
    response = HttpResponse(payload.model_dump_json(indent=2), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="jobengine-{kind}.json"'
    return response
