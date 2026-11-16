"""Pure projections for future result cards; no storage, model calls, or view wiring.

Use select_dashboard_cards for display eligibility. project_job only joins saved data,
including incomplete records, and never changes the underlying assessment.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from jobengine.grounding import contains_field_quote, contains_quote, validate_facts
from jobengine.models import (
    Constraint,
    Differentiator,
    Evidence,
    Profile,
    RankedJob,
    Requirement,
    RequirementAssessment,
    Run,
)
from jobengine.onboarding.preferences import is_hard_constraint
from jobengine.search.employer_identity import validate_employer_identity


@dataclass(frozen=True)
class StretchReview:
    """A human affirms that ALL listed gaps are modest and the job remains plausible.

    Reviews apply only to this saved run and exact discovery URL, not future searches.
    They cannot override the selector's grounding, constraint, or availability gates.
    """

    run_id: str
    posting_url: str
    gap_requirement_ids: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class Qualification:
    requirement: Requirement
    assessment: RequirementAssessment | None
    status: Literal["meets", "gap", "unknown"]
    evidence: tuple[Evidence, ...]
    supporting_constraints: tuple[Constraint, ...]
    differentiators: tuple[Differentiator, ...]

    @property
    def explanation(self) -> str | None:
        return self.assessment.explanation if self.assessment else None


@dataclass(frozen=True)
class JobCard:
    title: str
    company: str
    location: str | None
    original_url: str
    recommendation_status: Literal["recommended", "not_recommended"]
    show_not_recommended: bool
    requirements: tuple[Requirement, ...]
    strengths: tuple[Qualification, ...]
    qualification_gaps: tuple[Qualification, ...]
    unresolved_qualifications: tuple[Qualification, ...]
    uncertainties: tuple[str, ...]
    qualifications: tuple[Qualification, ...]
    ranked_job: RankedJob
    run_id: str | None = None
    position: int | None = None
    stretch_review: StretchReview | None = None


def _original_url(job: RankedJob) -> str:
    for value in (job.discovery.url, job.page.requested_url, job.page.url):
        try:
            parts = urlsplit(value)
            if parts.scheme in {"http", "https"} and parts.netloc:
                return value
        except ValueError:
            continue
    return ""


def project_job(job: RankedJob, profile: Profile, *, requirement_limit: int = 5) -> JobCard:
    """Join against the RUN's profile, retaining original wording and all qualifications.

    requirements is a short employer-worded preview, essentials first. qualifications
    and ranked_job retain the complete assessment. Unknown or unsupported claims never
    become strengths; declarations alone are not evidence-backed strengths.
    """
    if requirement_limit < 0:
        raise ValueError("requirement_limit must be nonnegative.")
    documents = {document.id: document.text for document in profile.documents}
    evidence = {
        item.id: item
        for item in profile.evidence
        if contains_quote(documents.get(item.source_id, ""), item.quote)
    }
    constraints = {item.id: item for item in profile.constraints}
    requirements = job.facts.requirements if job.facts else []
    assessments = job.judgment.requirements if job.judgment else []
    counts = Counter(item.requirement_id for item in assessments)
    by_id = {item.requirement_id: item for item in assessments if counts[item.requirement_id] == 1}
    qualifications = []
    for requirement in requirements:
        assessment = by_id.get(requirement.id)
        status = "unknown"
        if assessment:
            references_valid = all(key in evidence for key in assessment.evidence_ids) and all(
                key in constraints for key in assessment.supporting_constraint_ids
            )
            has_support = bool(assessment.evidence_ids or assessment.supporting_constraint_ids)
            if references_valid and (assessment.status != "meets" or has_support):
                status = assessment.status
        differentiators = tuple(
            item
            for item in (job.judgment.differentiators if job.judgment else [])
            if item.requirement_id == requirement.id
            and status == "meets"
            and item.evidence_ids
            and all(key in evidence for key in item.evidence_ids)
        )
        evidence_ids = list(assessment.evidence_ids) if assessment else []
        for item in differentiators:
            evidence_ids.extend(item.evidence_ids)
        qualifications.append(
            Qualification(
                requirement=requirement,
                assessment=assessment,
                status=status,
                evidence=tuple(
                    evidence[key] for key in dict.fromkeys(evidence_ids) if key in evidence
                ),
                supporting_constraints=tuple(
                    constraints[key]
                    for key in (assessment.supporting_constraint_ids if assessment else [])
                    if key in constraints
                ),
                differentiators=differentiators,
            )
        )
    recommended = job.category in {"strong", "weaker"}
    importance = {"required": 0, "preferred": 1, "responsibility": 2}
    return JobCard(
        title=(job.facts.title if job.facts else "") or job.discovery.title,
        company=(job.facts.company if job.facts else "") or job.discovery.company,
        location=job.facts.location if job.facts else None,
        original_url=_original_url(job),
        recommendation_status="recommended" if recommended else "not_recommended",
        show_not_recommended=not recommended,
        requirements=tuple(
            sorted(requirements, key=lambda item: importance[item.importance])[:requirement_limit]
        ),
        strengths=tuple(
            item for item in qualifications if item.status == "meets" and item.evidence
        ),
        qualification_gaps=tuple(item for item in qualifications if item.status == "gap"),
        unresolved_qualifications=tuple(
            item for item in qualifications if item.status == "unknown"
        ),
        uncertainties=tuple(job.judgment.uncertainties) if job.judgment else (),
        qualifications=tuple(qualifications),
        ranked_job=job,
    )


def _grounded_and_available(card: JobCard, profile: Profile, now: datetime) -> bool:
    job = card.ranked_job
    facts, judgment = job.facts, job.judgment
    if (
        not facts
        or not judgment
        or not facts.is_job_posting
        or job.page.status != "retrieved"
        or job.page.error
        or job.validation_issues
        or not card.original_url
        or facts.availability == "closed"
    ):
        return False
    if facts.valid_through:
        try:
            expiry = datetime.fromisoformat(facts.valid_through.replace("Z", "+00:00"))
            if expiry.replace(tzinfo=expiry.tzinfo or timezone.utc) <= now:
                return False
        except ValueError:
            return False
    try:
        validate_facts(facts, job.page.text)
    except ValueError:
        return False
    if not validate_employer_identity(job.discovery, job.page, facts.company).accepted:
        return False
    if not any(item.importance == "required" for item in facts.requirements):
        return False
    if len(judgment.requirements) != len(card.qualifications):
        return False
    for item in card.qualifications:
        assessment = item.assessment
        if (
            not assessment
            or not assessment.explanation.strip()
            or item.status != assessment.status
            or not set(assessment.evidence_ids).issubset(e.id for e in item.evidence)
            or not set(assessment.supporting_constraint_ids).issubset(
                constraint.id for constraint in item.supporting_constraints
            )
            or (item.requirement.importance == "required" and item.status == "unknown")
        ):
            return False
    checks = {item.constraint_id: item for item in judgment.constraints}
    if len(checks) != len(judgment.constraints):
        return False
    for constraint in profile.constraints:
        check = checks.get(constraint.id)
        if is_hard_constraint(constraint) and (not check or check.status != "meets"):
            return False
    for check in judgment.constraints:
        if check.status != "unknown" and not (
            contains_quote(job.page.text, check.job_quote)
            or any(
                term.quote == check.job_quote
                and contains_field_quote(
                    job.page.text, term.quote, 2 if term.field == "work_country" else 3
                )
                for term in facts.terms
            )
        ):
            return False
    return True


def select_dashboard_cards(
    run: Run,
    *,
    reviewed_stretches: Iterable[StretchReview] = (),
    now: datetime | None = None,
) -> list[JobCard]:
    """Keep saved ranking order: <=10 recommendations, then reviewed stretches to five.

    No review means no supplementation. Reviewers establish modestness/plausibility;
    this function checks saved grounding and every gap ID, never scores gap severity.
    Unknown required qualifications/hard constraints fail closed. Unknown availability
    retains its saved caveats. No source, profile, or run is mutated.
    """
    now = now or datetime.now(timezone.utc)
    now = now.replace(tzinfo=now.tzinfo or timezone.utc)
    reviews = {
        item.posting_url: item
        for item in reviewed_stretches
        if item.run_id == run.id and item.rationale.strip()
    }
    recommendations, stretches = [], []
    seen = set()
    for position, job in enumerate(run.inspected_jobs or run.jobs):
        if job.category not in {"strong", "weaker", "excluded"} or job.discovery.url in seen:
            continue
        review = reviews.get(job.discovery.url)
        if job.category == "excluded" and not review:
            continue
        card = project_job(job, run.profile)
        if not _grounded_and_available(card, run.profile, now):
            continue
        required = [
            item for item in card.qualifications if item.requirement.importance == "required"
        ]
        if job.category in {"strong", "weaker"}:
            if not all(item.status == "meets" for item in required):
                continue
            recommendations.append(replace(card, run_id=run.id, position=position))
        else:
            gaps = {item.requirement.id for item in card.qualification_gaps}
            if (
                not any(item.status == "gap" for item in required)
                or gaps != set(review.gap_requirement_ids)
                or any(item.requirement.kind != "qualification" for item in card.qualification_gaps)
            ):
                continue
            stretches.append(replace(card, run_id=run.id, position=position, stretch_review=review))
        seen.add(job.discovery.url)
    recommendations = recommendations[:10]
    return recommendations + stretches[: max(0, 5 - len(recommendations))]
