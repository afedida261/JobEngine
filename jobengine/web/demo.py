"""Read-only replay of an explicitly selected, previously recorded local demo."""

from dataclasses import dataclass, replace

from django.conf import settings

from jobengine.models import InterviewExchange, Run, SavedOnboarding
from jobengine.web.presentation import JobCard, project_job, select_dashboard_cards


@dataclass(frozen=True)
class RecordedDemo:
    onboarding: SavedOnboarding
    exchanges: tuple[InterviewExchange, ...]
    run: Run


def load_recorded_demo() -> RecordedDemo | None:
    """Load snapshots only: no providers, imports, approval changes, or writes."""
    try:
        onboarding = SavedOnboarding.model_validate_json(
            settings.RECORDED_DEMO_INTERVIEW.read_text(encoding="utf-8")
        )
        run = Run.model_validate_json(settings.RECORDED_DEMO_RUN.read_text(encoding="utf-8"))
        exchanges = tuple(
            item
            for item in onboarding.session.history
            if item.question.strip() and (item.answer or "").strip()
        )
        if (
            onboarding.session.interview_mode != "adaptive"
            or not exchanges
            or not onboarding.profile
            or not onboarding.profile.confirmed
            or not onboarding.profile.search_brief
            or not onboarding.profile.search_brief.confirmed
            or onboarding.profile != run.profile
            or run.model != "gpt-6.1-sol"
        ):
            return None
        return RecordedDemo(onboarding=onboarding, exchanges=exchanges[:1], run=run)
    except (OSError, UnicodeError, ValueError):
        return None


def select_demo_cards(run: Run) -> list[JobCard]:
    """Replay recommendations plus saved review cases without changing eligibility."""
    cards = select_dashboard_cards(run)
    for position, job in enumerate(run.inspected_jobs or run.jobs):
        if job.category == "needs_review":
            cards.append(replace(project_job(job, run.profile), run_id=run.id, position=position))
    return cards
