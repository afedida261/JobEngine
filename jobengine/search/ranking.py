from __future__ import annotations

import re
from datetime import datetime, timezone

from jobengine.grounding import contains_field_quote, contains_quote, keyword_overlap, normalized
from jobengine.models import (
    ConstraintAssessment,
    DiscoveryItem,
    FitJudgment,
    JobFacts,
    JobPage,
    Profile,
    RankedJob,
    RequirementAssessment,
)
from jobengine.onboarding.preferences import is_hard_constraint

CATEGORY_ORDER = {"strong": 0, "weaker": 1, "needs_review": 2, "excluded": 3, "unverified": 4}


def rank_job(
    profile: Profile, item: DiscoveryItem, page: JobPage, facts: JobFacts, judgment: FitJudgment
) -> RankedJob:
    judgment = judgment.model_copy(deep=True)
    evidence = {e.id: e for e in profile.evidence}
    requirements = {r.id: r for r in facts.requirements}
    constraints = {c.id: c for c in profile.constraints}
    issues: list[str] = []
    assessments: dict[str, RequirementAssessment] = {}
    for a in judgment.requirements:
        if a.requirement_id not in requirements or a.requirement_id in assessments:
            issues.append("Unknown or duplicate requirement assessment discarded.")
            continue
        if (
            any(e not in evidence for e in a.evidence_ids)
            or any(c not in constraints for c in a.supporting_constraint_ids)
            or (a.status == "meets" and not a.evidence_ids and not a.supporting_constraint_ids)
        ):
            issues.append(
                f"{a.requirement_id}: unsupported candidate evidence; changed to unknown."
            )
            a.status, a.evidence_ids, a.supporting_constraint_ids = "unknown", [], []
        assessments[a.requirement_id] = a
    for req in facts.requirements:
        if req.id not in assessments:
            issues.append(f"{req.id}: missing assessment; treated as unknown.")
            assessments[req.id] = RequirementAssessment(
                requirement_id=req.id,
                status="unknown",
                evidence_ids=[],
                explanation="The model did not assess this requirement.",
            )
    judgment.requirements = list(assessments.values())

    checks: dict[str, ConstraintAssessment] = {}
    for c in judgment.constraints:
        if c.constraint_id in {"C2", "C4"} and c.constraint_id not in constraints:
            continue  # Retired checks in saved assessments cannot affect current ranking.
        if c.constraint_id not in constraints or c.constraint_id in checks:
            issues.append("Unknown or duplicate constraint assessment discarded.")
            continue
        cited_term = any(
            t.quote == c.job_quote
            and contains_field_quote(page.text, t.quote, 2 if t.field == "work_country" else 3)
            for t in facts.terms
        )
        if c.status != "unknown" and not contains_quote(page.text, c.job_quote) and not cited_term:
            c.status = "unknown"
            c.explanation = "No valid employer quotation supports this constraint assessment."
            issues.append(f"{c.constraint_id}: unsupported constraint quotation.")
        checks[c.constraint_id] = c
    for c in profile.constraints:
        if c.id not in checks:
            checks[c.id] = ConstraintAssessment(
                constraint_id=c.id,
                status="unknown",
                job_quote=None,
                explanation="Not established by the posting.",
            )
            issues.append(f"{c.id}: missing constraint assessment; treated as unknown.")
    judgment.constraints = list(checks.values())

    judgment.differentiators = [
        d
        for d in judgment.differentiators
        if d.requirement_id in requirements
        and d.evidence_ids
        and all(e in evidence for e in d.evidence_ids)
        and assessments[d.requirement_id].status == "meets"
    ]
    if not contains_quote(page.text, judgment.interest_alignment.job_quote):
        judgment.interest_alignment.level = "unknown"
        issues.append("Interest alignment lacks a valid employer quotation.")

    required = [a for rid, a in assessments.items() if requirements[rid].importance == "required"]
    # Keep graduation-window stretches visible without asserting that eligibility is met.
    cohort_ids = {
        r.id
        for r in facts.requirements
        if (
            r.kind == "graduation_cohort"
            or (
                re.search(r"graduat\w*|class of|cohort", r.quote, re.I)
                and re.search(r"20\d{2}|spring|summer|fall|autumn|winter", r.quote, re.I)
            )
        )
        and not re.search(r"citizen|clearance|export control|professional years", r.quote, re.I)
    }
    cohort_gaps = [a for a in required if a.status == "gap" and a.requirement_id in cohort_ids]
    gaps = [a for a in required if a.status == "gap" and a.requirement_id not in cohort_ids]
    unknown = [a for a in required if a.status == "unknown"]
    date_ids = {c.id for c in profile.constraints if c.field == "Graduation / start dates"}
    conflicts = [
        c
        for cid, c in checks.items()
        if is_hard_constraint(constraints[cid]) and c.status == "conflict"
    ]
    unknown_constraints = [
        c
        for cid, c in checks.items()
        if is_hard_constraint(constraints[cid]) and c.status == "unknown"
    ]
    date_conflicts = [c for cid, c in checks.items() if cid in date_ids and c.status == "conflict"]
    reasons: list[str] = []
    reasons.extend(
        f"Graduation-cohort stretch: {requirements[a.requirement_id].text}. Employer eligibility is not confirmed."
        for a in cohort_gaps
    )
    reasons.extend("Graduation/start-date difference: " + c.explanation for c in date_conflicts)
    cohort_mismatch = bool(cohort_gaps or date_conflicts)
    cohort = re.search(r"new[\s-]*grad(?:uate)?[^\d]{0,12}(20\d{2})", facts.title, re.I)
    if (
        cohort
        and profile.intake
        and profile.intake.graduation_year
        and normalized(facts.title) in normalized(page.text)
    ):
        cohort_year = int(cohort.group(1))
        explicit_windows = [
            r
            for r in facts.requirements
            if r.importance == "required"
            and re.search(r"graduat", r.quote, re.I)
            and re.search(r"20\d{2}", r.quote)
        ]
        window_supported = bool(explicit_windows) and all(
            assessments[r.id].status == "meets" for r in explicit_windows
        )
        if cohort_year != profile.intake.graduation_year and not window_supported:
            cohort_mismatch = True
            reasons.append(
                f"Title names the {cohort_year} new-grad cohort; applicant graduation is {profile.intake.graduation_year}. Verify eligibility; the title alone is not a mandatory exclusion."
            )
    closed = facts.availability == "closed"
    if facts.valid_through:
        try:
            expiry = datetime.fromisoformat(facts.valid_through.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            closed = closed or expiry < datetime.now(timezone.utc)
        except ValueError:
            issues.append("Posting expiry could not be parsed; verify manually.")

    if not facts.is_job_posting:
        category = "unverified"
        reasons.append("Retrieved page could not be established as an individual job posting.")
    elif closed or gaps or conflicts:
        category = "excluded"
        if closed:
            reasons.append("Posting is closed or its stated deadline has passed.")
        reasons.extend(
            f"Required qualification gap: {requirements[a.requirement_id].text}" for a in gaps
        )
        reasons.extend(
            f"Hard constraint conflict: {constraints[c.constraint_id].field}" for c in conflicts
        )
    elif not required or unknown or unknown_constraints or issues or cohort_mismatch:
        category = "needs_review"
        reasons.append("Eligibility has unresolved questions; this is not a confirmed match.")
        if not required:
            reasons.append("No explicit essential requirements were extracted.")
        reasons.extend(
            f"Unknown requirement: {requirements[a.requirement_id].text}" for a in unknown
        )
        reasons.extend(
            f"Unresolved constraint: {constraints[c.constraint_id].field}"
            for c in unknown_constraints
        )
    elif judgment.differentiators and judgment.interest_alignment.level == "high":
        category = "strong"
        reasons.append(
            "Stated essentials are supported, with role-specific strengths and high interest alignment."
        )
    else:
        category = "weaker"
        reasons.append(
            "Stated essentials are supported, but distinctive strengths or interest alignment are less convincing."
        )
    if facts.availability == "unknown" and not closed:
        reasons.append("Application availability is unconfirmed; check the original posting.")

    coverage = sum(a.status == "meets" for a in required) / len(required) if required else 0
    preferred = [a for rid, a in assessments.items() if requirements[rid].importance == "preferred"]
    preferred_coverage = (
        sum(a.status == "meets" for a in preferred) / len(preferred) if preferred else 0
    )
    alignment = {"high": 1, "medium": 0.5, "low": 0, "unknown": 0}[
        judgment.interest_alignment.level
    ]
    # Category ordering always wins over this transparent within-category score.
    score = round(
        45 * coverage
        + 25 * alignment
        + 20 * min(len(judgment.differentiators), 2) / 2
        + 10 * preferred_coverage,
        1,
    )
    return RankedJob(
        discovery=item,
        page=page,
        facts=facts,
        judgment=judgment,
        category=category,
        score=score,
        reasons=reasons,
        validation_issues=issues,
        keyword_score=keyword_overlap(profile.evidence, page.text),
    )


def sort_jobs(jobs: list[RankedJob]) -> list[RankedJob]:
    return sorted(
        jobs, key=lambda j: (CATEGORY_ORDER[j.category], -j.score, j.discovery.title.casefold())
    )
