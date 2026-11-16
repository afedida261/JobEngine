"""PowerShell-friendly, stage-by-stage access to JobEngine's real workflow."""

import argparse
import math
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

from jobengine.grounding import validate_evidence
from jobengine.infrastructure.diagnostics import observe
from jobengine.infrastructure.llm import DEFAULT_INTERVIEW_MODEL, DEFAULT_SEARCH_MODEL, ModelClient
from jobengine.infrastructure.persistence import PersistenceError
from jobengine.infrastructure.resumes import read_resume
from jobengine.infrastructure.storage import Store
from jobengine.models import (
    DiscoveryItem,
    Document,
    IntakePreferences,
    JobPage,
    OnboardingAnalysis,
    OnboardingSession,
    Profile,
    SavedOnboarding,
    SearchBriefDraft,
)
from jobengine.onboarding.brief import (
    confirm_search_brief,
    propose_search_brief,
    require_confirmed_search_brief,
)
from jobengine.onboarding.guided import continue_guided
from jobengine.onboarding.interview import answer_question, finish_interview, start_onboarding
from jobengine.onboarding.preferences import intake_constraints
from jobengine.search.discovery import public_brief
from jobengine.search.greenhouse_discovery import MAX_BOARDS, MAX_SEARCH_REQUESTS
from jobengine.search.pipeline import evaluate, run_search
from jobengine.terminal.artifacts import Artifacts, identifier, read_json, write_json

ROOT = Path(__file__).resolve().parents[2]


def positive_minutes(value):
    try:
        minutes = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Enter a positive number of minutes.") from exc
    if not math.isfinite(minutes) or minutes <= 0:
        raise argparse.ArgumentTypeError("Enter a positive, finite number of minutes.")
    return minutes


def greenhouse_board(value):
    token, separator, employer = value.partition("=")
    if not separator or not token.strip() or not employer.strip():
        raise argparse.ArgumentTypeError(
            "Use TOKEN=EMPLOYER, for example thenewyorktimes=The New York Times."
        )
    return token.strip(), employer.strip()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--data-dir", type=Path, help="Shared cache/cost ledger; default JOBENGINE_DATA_DIR or data"
    )
    result.add_argument(
        "--root",
        type=Path,
        help="Local terminal candidates and diagnostics; default <data-dir>/debug",
    )
    result.add_argument(
        "--verbose",
        action="store_true",
        help="Also print diagnostic JSON (contains candidate information)",
    )
    commands = result.add_subparsers(dest="command", required=True)
    add = commands.add_parser("candidate", help="Register a named candidate from local sources")
    add.add_argument("candidate", type=identifier)
    add.add_argument("--resume", type=Path)
    add.add_argument("--intake", type=Path, help="Django pre-interview form fields as JSON")
    add.add_argument(
        "--record", type=Path, help="SavedOnboarding JSON export; copied, never activated"
    )
    add.add_argument("--profile", type=Path)
    add.add_argument("--session", type=Path, help="OnboardingSession JSON")
    add.add_argument("--role", default="")
    add.add_argument("--interests", default="")
    add.add_argument("--professional-years", type=float)
    interview = commands.add_parser(
        "interview", help="Resume or start Terra's interactive interview"
    )
    interview.add_argument("candidate", type=identifier)
    interview.add_argument(
        "--answers", type=Path, help="JSON list of answers; /skip, /finish and /pause supported"
    )
    interview.add_argument("--non-interactive", action="store_true")
    interview.add_argument(
        "--restart",
        action="store_true",
        help="Start a fresh interview from registered sources; archive previous state in this run",
    )
    plan = commands.add_parser(
        "plan", help="Review profile, propose/reload a plan, and explicitly approve"
    )
    plan.add_argument("candidate", type=identifier)
    plan.add_argument("--profile", type=Path)
    plan.add_argument(
        "--draft", type=Path, help="Reuse/edit a SearchBriefDraft without calling Sol"
    )
    plan.add_argument("--non-interactive", action="store_true")
    plan.add_argument(
        "--confirm-profile",
        action="store_true",
        help="Explicitly attest that you reviewed this profile",
    )
    plan.add_argument(
        "--approve-plan", action="store_true", help="Explicitly approve this plan after review"
    )
    for name in ("search", "evaluate"):
        sub = commands.add_parser(
            name,
            help="Run Sol from an approved profile"
            if name == "search"
            else "Re-evaluate one discovered job without discovery",
        )
        sub.add_argument("candidate", type=identifier)
        sub.add_argument("--profile", type=Path)
        if name == "search":
            sub.add_argument("--limit", type=int, default=3, choices=range(1, 11))
            sub.add_argument(
                "--max-assessments",
                type=int,
                choices=range(1, 21),
                help="Greenhouse only: maximum postings to assess (default 20; --limit targets recommendations)",
            )
            providers = sub.add_mutually_exclusive_group()
            providers.add_argument(
                "--greenhouse",
                action="append",
                type=greenhouse_board,
                metavar="TOKEN=EMPLOYER",
                help="Use only these Greenhouse boards plus hybrid semantic screening; repeat for multiple boards",
            )
            providers.add_argument(
                "--discover-greenhouse",
                action="store_true",
                help="Discover Greenhouse boards from the existing approved brief, then screen and assess their postings",
            )
            sub.add_argument(
                "--board-search-requests",
                type=int,
                choices=range(1, MAX_SEARCH_REQUESTS + 1),
                help="Dynamic Greenhouse discovery only: maximum web-search requests (default 4)",
            )
            sub.add_argument(
                "--max-boards",
                type=int,
                choices=range(1, MAX_BOARDS + 1),
                help="Dynamic Greenhouse discovery only: maximum board API inspections (default 12; failures count)",
            )
            sub.add_argument(
                "--max-minutes",
                type=positive_minutes,
                help="Stop starting new work after this many minutes; the in-flight step may finish later",
            )
        else:
            sub.add_argument(
                "--job", required=True, type=Path, help="DiscoveryItem or search/jobs/<id>.json"
            )
            sub.add_argument(
                "--page",
                type=Path,
                help="Saved JobPage JSON or search/jobs/<id>.json; skips HTTP retrieval",
            )
    return result


def make_client(store):
    return ModelClient(
        os.getenv("OPENAI_API_KEY", ""),
        store,
        os.getenv("OPENAI_SEARCH_MODEL", DEFAULT_SEARCH_MODEL),
        interview_model=os.getenv("OPENAI_INTERVIEW_MODEL", DEFAULT_INTERVIEW_MODEL),
    )


def register(args, directory, trace):
    target = directory / "candidate.json"
    if target.exists():
        raise ValueError(
            "Candidate already exists. Reuse the ID or choose a new ID to register another fixture."
        )
    profile = Profile.model_validate(read_json(args.profile)) if args.profile else None
    session = OnboardingSession.model_validate(read_json(args.session)) if args.session else None
    if args.record:
        record = SavedOnboarding.model_validate(read_json(args.record))
        session, profile = record.session, record.profile
    if args.intake:
        if session or profile:
            raise ValueError(
                "Import existing state unchanged, or create a fresh candidate with --resume and --intake."
            )
        if args.role or args.interests or args.professional_years is not None:
            raise ValueError("With --intake, supply all registration values in the form JSON.")
        session = registration_form_session(args)
    documents = session.documents if session else profile.documents if profile else []
    if args.resume and not args.intake:
        if session or profile:
            raise ValueError(
                "Use a resume for a fresh candidate, or import a profile/session/record."
            )
        documents = [
            Document(
                id="D1",
                label=args.resume.name,
                text=read_resume(args.resume.name, args.resume.read_bytes()),
            )
        ]
    if not documents:
        raise ValueError("Supply --resume, --profile, --session, or --record.")
    intake = session.intake if session else profile.intake if profile else None
    config = dict(
        documents=[d.model_dump(mode="json") for d in documents],
        role_hint=args.role
        or (session.role_hint if session else ", ".join(profile.target_roles) if profile else ""),
        interest_hint=args.interests
        or (session.interest_hint if session else profile.interests if profile else ""),
        professional_years=args.professional_years
        if args.professional_years is not None
        else (
            session.professional_years
            if session
            else profile.professional_years
            if profile
            else None
        ),
        intake=intake.model_dump(mode="json") if intake else None,
    )
    if args.intake:
        config["registration_session"] = session.model_dump(mode="json")
    trace.save("candidate/input.json", config)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if session:
        write_json(directory / "session.json", session.model_dump(mode="json"))
    if profile:
        write_json(directory / "profile.json", profile.model_dump(mode="json"))
    write_json(target, config)


def registration_form_session(args):
    """Use the web boundary itself; JSON values follow Django field coercion."""
    import django
    from django.core.exceptions import ValidationError
    from django.core.files.uploadedfile import SimpleUploadedFile

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "jobengine.web.settings")
    django.setup()
    from jobengine.web.forms import IntakeForm

    try:
        values = read_json(args.intake)
    except (ValueError, OSError) as exc:
        raise ValueError(f"intake={str(args.intake)!r}: {exc}") from exc
    if not isinstance(values, dict):
        raise ValueError(f"intake={values!r}: expected a JSON object of form fields.")
    if not args.resume:
        raise ValueError("resume=None: supply --resume with --intake.")
    try:
        upload = SimpleUploadedFile(args.resume.name, args.resume.read_bytes())
    except OSError as exc:
        raise ValueError(f"resume={str(args.resume)!r}: {exc}") from exc
    form = IntakeForm(values, {"resume": upload})
    try:
        valid = form.is_valid()
    except (TypeError, AttributeError) as exc:
        # HTTP fields are strings, but JSON can carry objects/arrays. Let the
        # same Django field parser identify the value it cannot convert.
        for name, field in form.fields.items():
            if name not in values or name == "resume":
                continue
            try:
                field.clean(values[name])
            except (TypeError, AttributeError) as field_exc:
                raise ValueError(
                    f"{name}={values[name]!r}: the form parser cannot convert this value ({field_exc})."
                ) from field_exc
            except (ValueError, ValidationError):
                continue
        raise ValueError(f"intake={values!r}: {exc}") from exc
    if not valid:
        errors = []
        for field, messages in form.errors.items():
            value = str(args.resume) if field == "resume" else values.get(field)
            if field == "__all__":
                value = values
            errors.append(f"{field}={value!r}: {'; '.join(messages)}")
        raise ValueError("\n".join(errors))
    return form.create_session()


def profile_from_session(session):
    return Profile(
        documents=session.documents,
        evidence=session.analysis.evidence,
        target_roles=session.analysis.suggested_roles,
        interests=session.analysis.suggested_interests,
        professional_years=session.professional_years,
        intake=session.intake,
        constraints=intake_constraints(session.intake) if session.intake else [],
        confirmed=False,
    )


def load_candidate(directory):
    """Normalize resumable working files, leaving immutable run artifacts intact."""
    path = directory / "candidate.json"
    original = read_json(path)
    config = dict(original)
    if config.get("intake"):
        config["intake"] = IntakePreferences.model_validate(config["intake"]).model_dump(
            mode="json"
        )
    if config.get("registration_session"):
        config["registration_session"] = OnboardingSession.model_validate(
            config["registration_session"]
        ).model_dump(mode="json")
    if config != original:
        write_json(path, config)
    for name, schema in (("session", OnboardingSession), ("profile", Profile)):
        working = directory / f"{name}.json"
        if working.exists():
            previous = read_json(working)
            current = schema.model_validate(previous).model_dump(mode="json")
            if current != previous:
                write_json(working, current)
    return config


def save_interview(directory, session, trace, *, persist=True):
    payload = session.model_dump(mode="json")
    if persist:
        write_json(directory / "session.json", payload)
    trace.save("interview/session.json", payload)
    trace.save(f"interview/snapshots/{len(trace.events):04d}.json", payload)
    trace.save("interview/transcript.json", [h.model_dump(mode="json") for h in session.history])
    trace.save("interview/attempts.json", [a.model_dump(mode="json") for a in session.attempts])
    text = "\n\n".join(
        f"Q: {h.question}\nA: {h.answer if h.answer is not None else '[skipped]'}"
        for h in session.history
    )
    if session.attempts:
        text += "\n\nOTHER SUBMISSIONS\n" + "\n\n".join(
            f"Q: {a.question}\nA ({a.disposition}): {a.text}" for a in session.attempts
        )
    if session.pending_answer is not None:
        text += f"\n\nPENDING\nQ: {session.pending_question}\nA: {session.pending_answer}"
    (trace.path / "interview/transcript.txt").write_text(trace.redact(text), encoding="utf-8")
    trace.save(
        "interview/evidence.json", [e.model_dump(mode="json") for e in session.analysis.evidence]
    )
    profile = profile_from_session(session)
    trace.save("interview/profile.json", profile.model_dump(mode="json"))
    if persist and session.interview_finished:
        write_json(directory / "profile.json", profile.model_dump(mode="json"))


def interview(args, directory, config, trace, client_factory, input_fn):
    session_path = directory / "session.json"
    if session_path.exists() and not args.restart:
        session = OnboardingSession.model_validate(read_json(session_path))
        if session.interview_finished:
            trace(
                "terra",
                "already_finished",
                reason=f"Saved interview has {len(session.history)} answers. Use interview {args.candidate} --restart to start again, or plan {args.candidate} to review the profile.",
            )
            save_interview(directory, session, trace, persist=False)
            return "completed"
    if not session_path.exists() or args.restart or session.stage == "intake":
        if session_path.exists():
            trace.save("interview/previous-session.json", read_json(session_path))
        profile_path = directory / "profile.json"
        if profile_path.exists():
            previous = Profile.model_validate(read_json(profile_path))
            trace.save("interview/previous-profile.json", previous.model_dump(mode="json"))
            previous.confirmed, previous.search_brief = False, None
            write_json(profile_path, previous.model_dump(mode="json"))
        if config.get("registration_session"):
            session = OnboardingSession.model_validate(config["registration_session"])
        elif session_path.exists() and not args.restart:
            # Imported, unprepared website sessions already contain their metadata.
            session = OnboardingSession.model_validate(read_json(session_path))
        else:
            session = OnboardingSession(
                id=uuid.uuid4().hex,
                documents=[Document.model_validate(d) for d in config["documents"]],
                role_hint=config["role_hint"],
                interest_hint=config["interest_hint"],
                professional_years=config["professional_years"],
                # Django always supplies structured intake. Unknown values activate its
                # question-coverage policy without inventing applicant preferences.
                intake=IntakePreferences.model_validate(config["intake"])
                if config["intake"]
                else IntakePreferences(
                    graduation_status="unknown",
                ),
                history=[],
                analysis=OnboardingAnalysis(
                    summary="",
                    evidence=[],
                    conflicts=[],
                    missing_information=[],
                    suggested_roles=[],
                    suggested_interests="",
                    preference_suggestions=[],
                    next_question=None,
                ),
            )
        save_interview(directory, session, trace)
        try:
            initial_session = session
            session = start_onboarding(
                client_factory(),
                session.documents,
                session.role_hint,
                session.interest_hint,
                session.professional_years,
                session.intake,
            )
            session.id = initial_session.id
            session.general_information = initial_session.general_information
        except Exception as exc:
            trace(
                "terra",
                "failed",
                error_type=type(exc).__name__,
                reason=str(exc),
                recovery="Production guided interview",
                retried=False,
            )
            session = continue_guided(session)
    answers = read_json(args.answers) if args.answers else None
    if answers is not None and (
        not isinstance(answers, list) or not all(isinstance(a, str) for a in answers)
    ):
        raise ValueError("--answers must contain a JSON list of strings.")
    supplied = iter(answers) if answers is not None else None
    save_interview(directory, session, trace)
    while not session.interview_finished:
        question = session.analysis.next_question.question
        trace("terra", "question", question=f"Question {len(session.history) + 1}: {question}")
        try:
            answer = (
                next(supplied)
                if supplied is not None
                else (
                    "/pause"
                    if args.non_interactive
                    else input_fn("Answer (/skip, /finish, /pause): ")
                )
            )
        except (StopIteration, EOFError):
            answer = "/pause"
        if answer == "/pause":
            trace("terra", "paused", reason="Resume with the same candidate ID.")
            break
        if answer == "/finish":
            session = finish_interview(session)
            save_interview(directory, session, trace)
            break
        skip = answer == "/skip"
        trace("terra", "answer_received", question=question, answer=answer)
        if not skip and len(answer.strip()) < 8:
            trace.output("[terra] Add a little detail (at least eight characters), or /skip.")
            continue
        session.pending_answer = "" if skip else answer
        session.pending_question = question
        save_interview(directory, session, trace)
        before = {e.id for e in session.analysis.evidence}
        try:
            updated = (
                continue_guided(session, answering=True, skip=skip)
                if session.interview_mode == "guided"
                else answer_question(client_factory(), session, answer, skip=skip)
            )
        except Exception as exc:
            trace(
                "terra",
                "failed",
                error_type=type(exc).__name__,
                reason=str(exc),
                recovery="Production guided interview",
                retried=False,
            )
            updated = continue_guided(session, answering=True, skip=skip)
        session = updated
        trace(
            "terra",
            "profile_updated",
            evidence_added=len({e.id for e in session.analysis.evidence} - before),
            notice=session.notice,
            interview_mode=session.interview_mode,
        )
        save_interview(directory, session, trace)
    return "completed" if session.interview_finished else "paused"


def approve(prompt, explicit, non_interactive, input_fn):
    if explicit:
        return True
    if non_interactive:
        return False
    question = prompt + " [Y/N]: "
    while True:
        answer = input_fn(question).strip().lower()
        if answer in {"y", "n"}:
            return answer == "y"
        question = "Please enter Y or N. " + prompt + " [Y/N]: "


def plan(args, directory, profile, trace, client_factory, input_fn):
    # The same explicit profile review as the application; no plan can bless an unreviewed profile.
    trace.save("planning/input-profile.json", profile.model_dump(mode="json"))
    trace.output("[profile] Review the profile, evidence, preferences and source references:")
    trace.output("Roles: " + ", ".join(profile.target_roles))
    trace.output("Interests: " + profile.interests)
    trace.output(f"Professional years: {profile.professional_years}")
    for evidence in profile.evidence:
        trace.output(
            f"  {evidence.id}: {evidence.summary} [{evidence.source_id}: {evidence.quote}]"
        )
    for constraint in profile.constraints:
        trace.output(f"  {constraint.field}: {constraint.value} (hard={constraint.hard})")
    if profile.intake:
        for key, value in profile.intake.model_dump(mode="json").items():
            trace.output(f"  {key}: {value}")
    trace.output(f"Full profile and sources: {trace.path / 'planning/input-profile.json'}")
    validate_evidence(profile.evidence, profile.documents)
    if not profile.evidence or not profile.target_roles:
        raise ValueError(
            "Include at least one supported evidence item and a target role before approval."
        )
    if not profile.confirmed:
        if not approve(
            "[profile] Confirm this profile?", args.confirm_profile, args.non_interactive, input_fn
        ):
            return "awaiting_profile_approval"
        profile.confirmed = True
    profile.search_brief = None
    trace.save("planning/reviewed-profile.json", profile.model_dump(mode="json"))
    write_json(directory / "profile.json", profile.model_dump(mode="json"))
    draft = (
        SearchBriefDraft.model_validate(read_json(args.draft))
        if args.draft
        else propose_search_brief(client_factory(), profile)
    )
    trace.save("planning/proposed-plan.json", draft.model_dump(mode="json"))
    trace.output("[plan] Proposed search plan:")
    for key, value in draft.model_dump(mode="json").items():
        trace.output(f"  {key}: {value}")
    if not approve(
        "[plan] Approve this search plan?", args.approve_plan, args.non_interactive, input_fn
    ):
        return "awaiting_plan_approval"
    profile.search_brief = confirm_search_brief(profile, draft)
    require_confirmed_search_brief(profile)
    trace.save("planning/approved-profile.json", profile.model_dump(mode="json"))
    write_json(directory / "profile.json", profile.model_dump(mode="json"))
    trace(
        "plan",
        "approved",
        profile_fingerprint=profile.search_brief.profile_fingerprint,
        confirmation_fingerprint=profile.search_brief.confirmation_fingerprint,
    )
    return "completed"


def main(argv=None, *, client_factory=make_client, input_fn=input, output=print):
    args = parser().parse_args(argv)
    load_dotenv(ROOT / ".env")
    data_dir = args.data_dir or Path(os.getenv("JOBENGINE_DATA_DIR", str(ROOT / "data")))
    root = args.root or data_dir / "debug"
    store = Store(data_dir, float(os.getenv("JOBENGINE_BUDGET_USD", "10")))
    trace = Artifacts(root, args.candidate, args.command, store, output, args.verbose)
    trace.save(
        "invocation.json", {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
    )
    directory = root / "candidates" / args.candidate
    client = None

    def get_client():
        nonlocal client
        if client is None:
            client = client_factory(store)
        return client

    status = "failed"
    with observe(trace):
        try:
            if args.command == "candidate":
                register(args, directory, trace)
                status = "completed"
            else:
                config = load_candidate(directory)
                trace.save("candidate/input.json", config)
                if (directory / "session.json").exists() and args.command != "interview":
                    save_interview(
                        directory,
                        OnboardingSession.model_validate(read_json(directory / "session.json")),
                        trace,
                        persist=False,
                    )
                if args.command == "interview":
                    status = interview(args, directory, config, trace, get_client, input_fn)
                else:
                    profile = Profile.model_validate(
                        read_json(args.profile or directory / "profile.json")
                    )
                    if args.command == "plan":
                        status = plan(args, directory, profile, trace, get_client, input_fn)
                    else:
                        trace.save("search/input-profile.json", profile.model_dump(mode="json"))
                        brief = require_confirmed_search_brief(profile)
                        if not brief.sectors:
                            raise ValueError("Review and approve ordered sectors before search.")
                        trace.save("search/approved-plan.json", brief.model_dump(mode="json"))
                        trace.save("search/discovery-handoff.json", public_brief(profile))
                        if args.command == "search":
                            options = {}
                            if args.discover_greenhouse:
                                options["discover_greenhouse"] = True
                            for name in ("board_search_requests", "max_boards"):
                                if getattr(args, name) is not None:
                                    if not args.discover_greenhouse:
                                        raise ValueError(
                                            "Board-discovery limits require --discover-greenhouse."
                                        )
                                    options[name] = getattr(args, name)
                            if args.greenhouse is not None:
                                boards = dict(args.greenhouse)
                                if len(boards) != len(args.greenhouse):
                                    raise ValueError(
                                        "Supply each Greenhouse board token only once."
                                    )
                                options["greenhouse_boards"] = boards
                            if args.max_assessments is not None:
                                if args.greenhouse is None and not args.discover_greenhouse:
                                    raise ValueError(
                                        "--max-assessments requires --greenhouse or --discover-greenhouse."
                                    )
                                options["max_assessments"] = args.max_assessments
                            run = run_search(
                                get_client(),
                                profile,
                                limit=args.limit,
                                max_minutes=args.max_minutes,
                                purpose="validation",
                                progress=lambda message: output("[search] " + message),
                                **options,
                            )
                            trace.save(
                                "search/run.json", run.model_dump(mode="json"), required=True
                            )
                            trace.output("[search] " + run.search.summary)
                            status = (
                                "partial"
                                if run.search.stop_reason
                                in {
                                    "research_error",
                                    "budget_limit",
                                    "time_limit",
                                    "discovery_error",
                                    "embedding_error",
                                    "assessment_limit",
                                    "evaluation_error",
                                    "board_discovery_error",
                                    "board_discovery_unidentified",
                                }
                                or (
                                    run.search.strategy == "greenhouse_semantic"
                                    and run.search.warnings
                                )
                                else "completed"
                            )
                        else:
                            value = read_json(args.job)
                            item = DiscoveryItem.model_validate(value.get("discovery", value))
                            trace("discover", "accepted", item=item.model_dump(mode="json"))
                            page = None
                            if args.page:
                                value = read_json(args.page)
                                page = JobPage.model_validate(value.get("page", value))
                                if page.requested_url != item.url:
                                    raise ValueError(
                                        "Saved page requested_url must match the discovered job URL."
                                    )
                            result = evaluate(get_client(), profile, item, page=page)
                            trace.save(
                                "search/evaluation.json",
                                result.model_dump(mode="json"),
                                required=True,
                            )
                            status = "completed"
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            trace(
                "persist" if isinstance(exc, PersistenceError) else "cli",
                "failed",
                error_type=type(exc).__name__,
                reason=str(exc),
                retried=False,
            )
            output(f"[error] {type(exc).__name__}: {trace.redact(str(exc))}")
            return 130 if isinstance(exc, KeyboardInterrupt) else 1
        finally:
            final_status = trace.finish(status)
            if final_status == "failed" and status != "failed":
                return 1
