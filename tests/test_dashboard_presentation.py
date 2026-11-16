"""Offline presentation contracts using synthetic data and an optional local run replay."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jobengine.models import Evidence, Requirement, RequirementAssessment, Run, SearchRecord
from jobengine.search.ranking import rank_job
from jobengine.web.presentation import StretchReview, project_job, select_dashboard_cards

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


@pytest.fixture
def run(case):
    job = rank_job(*case)
    return Run(
        id="syntheticdashboard",
        profile=case[0],
        model="synthetic-offline",
        search=SearchRecord(
            query="Synthetic fixture",
            retrieved_at=NOW.isoformat(),
            summary="",
            sources=[],
            jobs=[job.discovery],
            warnings=[],
        ),
        jobs=[job],
        inspected_jobs=[job],
    )


def copy_job(job, number):
    result = job.model_copy(deep=True)
    result.discovery.url = f"https://example.com/synthetic-job-{number}"
    result.page.requested_url = result.page.url = result.discovery.url
    result.discovery.title = result.facts.title = f"Synthetic engineer {number}"
    return result


@pytest.fixture
def stretch(run):
    run.profile.professional_years = 1.5
    job = copy_job(run.jobs[0], "stretch")
    job.facts.requirements.append(
        Requirement(
            id="R3",
            text="Two years of professional experience",
            quote="Two years of professional experience required",
            importance="required",
            minimum_professional_years=2,
        )
    )
    job.judgment.requirements.append(
        RequirementAssessment(
            requirement_id="R3",
            status="gap",
            evidence_ids=[],
            explanation="Synthetic candidate reports 1.5 years, six months below the requirement.",
        )
    )
    return rank_job(run.profile, job.discovery, job.page, job.facts, job.judgment)


def review_for(run, job):
    return StretchReview(
        run_id=run.id,
        posting_url=job.discovery.url,
        gap_requirement_ids=("R3",),
        rationale="Synthetic human review: the six-month gap is modest and this remains plausible.",
    )


def test_preserves_saved_ranking_order_and_original_positions(run):
    first, second, third = [copy_job(run.jobs[0], n) for n in (3, 1, 2)]
    first.score, second.score, third.score = 10, 99, 70
    first.category, second.category, third.category = "strong", "weaker", "weaker"
    hidden = copy_job(first, "review")
    hidden.category = "needs_review"
    run.inspected_jobs = [first, hidden, second, third]
    cards = select_dashboard_cards(run, now=NOW)
    assert [card.title for card in cards] == [job.facts.title for job in (first, second, third)]
    assert [card.position for card in cards] == [0, 2, 3]
    assert all(card.run_id == run.id for card in cards)
    assert all(card.recommendation_status == "recommended" for card in cards)
    assert all(not card.show_not_recommended for card in cards)


@pytest.mark.parametrize("recommendations", [0, 1, 4, 5, 12])
def test_caps_recommendations_and_only_backfills_to_five(run, stretch, recommendations):
    matches = [copy_job(run.jobs[0], n) for n in range(recommendations)]
    extras = [copy_job(stretch, f"stretch-{n}") for n in range(6)]
    # Recommendations must come first even if an exclusion appears earlier in the audit list.
    run.inspected_jobs = extras[:1] + matches + extras[1:]
    cards = select_dashboard_cards(
        run,
        reviewed_stretches=[review_for(run, job) for job in extras],
        now=NOW,
    )
    count = min(10, recommendations)
    assert len(cards) == max(5, count)
    assert [card.title for card in cards[:count]] == [job.facts.title for job in matches[:10]]
    assert all(card.recommendation_status == "recommended" for card in cards[:count])
    assert all(card.show_not_recommended and card.stretch_review for card in cards[count:])
    assert [card.original_url for card in cards[count:]] == [
        job.discovery.url for job in extras[: max(0, 5 - count)]
    ]


@pytest.mark.parametrize(
    "review_change",
    [
        None,
        {"run_id": "another-run"},
        {"posting_url": "https://example.com/another-job"},
        {"gap_requirement_ids": ("R1",)},
        {"rationale": " "},
    ],
)
def test_no_unestablished_supplementation(run, stretch, review_change):
    stretch.score = 100  # A score cannot establish modestness.
    run.inspected_jobs.append(stretch)
    reviews = [] if review_change is None else [replace(review_for(run, stretch), **review_change)]
    assert len(select_dashboard_cards(run, reviewed_stretches=reviews, now=NOW)) == 1


@pytest.mark.parametrize(
    "damage",
    [
        lambda job: setattr(job.judgment.constraints[0], "status", "conflict"),
        lambda job: setattr(job.judgment.constraints[0], "status", "unknown"),
        lambda job: setattr(job, "judgment", None),
        lambda job: setattr(job.page, "status", "failed"),
        lambda job: setattr(job.page, "error", "Synthetic retrieval failure"),
        lambda job: setattr(job.facts, "company", "Unverified synthetic employer"),
        lambda job: job.validation_issues.append("Synthetic grounding failure"),
        lambda job: setattr(job.facts, "availability", "closed"),
        lambda job: setattr(job.facts, "valid_through", "2026-11-10T23:59:00Z"),
        lambda job: setattr(job.facts, "valid_through", "unparseable"),
        lambda job: setattr(job.facts, "is_job_posting", False),
        lambda job: job.judgment.requirements.pop(),
        lambda job: job.judgment.requirements.append(job.judgment.requirements[0].model_copy()),
        lambda job: setattr(job.judgment.requirements[0], "status", "unknown"),
        lambda job: setattr(job.facts.requirements[0], "quote", "Not present in the source page"),
        lambda job: setattr(job.judgment.requirements[0], "evidence_ids", ["missing"]),
        lambda job: setattr(job, "category", "unverified"),
        lambda job: setattr(job, "category", "needs_review"),
    ],
    ids=[
        "hard-conflict",
        "hard-unknown",
        "unassessed",
        "retrieval-failed",
        "page-error",
        "employer-identity",
        "validation-failure",
        "closed",
        "expired",
        "unknown-expiry",
        "not-a-posting",
        "incomplete",
        "duplicate-assessment",
        "required-unknown",
        "unsupported-requirement",
        "missing-evidence",
        "unverified",
        "needs-review",
    ],
)
def test_review_cannot_override_exclusion_gates(run, stretch, damage):
    review = review_for(run, stretch)
    damage(stretch)
    run.inspected_jobs = [stretch]
    assert select_dashboard_cards(run, reviewed_stretches=[review], now=NOW) == []


def test_review_must_cover_every_gap_and_a_required_qualification_gap(run, stretch):
    run.inspected_jobs = [stretch]
    review = review_for(run, stretch)
    stretch.judgment.requirements[1].status = "gap"
    assert select_dashboard_cards(run, reviewed_stretches=[review], now=NOW) == []
    all_gaps = replace(review, gap_requirement_ids=("R2", "R3"))
    assert len(select_dashboard_cards(run, reviewed_stretches=[all_gaps], now=NOW)) == 1
    stretch.facts.requirements[-1].importance = "preferred"
    assert select_dashboard_cards(run, reviewed_stretches=[all_gaps], now=NOW) == []


def test_rejects_invalid_recommendations_and_legacy_fallback_is_supported(run):
    run.inspected_jobs = []
    assert len(select_dashboard_cards(run, now=NOW)) == 1
    run.jobs[0].judgment = None
    assert select_dashboard_cards(run, now=NOW) == []


def test_joins_by_id_and_preserves_differentiator_evidence_and_declarations(run):
    job = run.jobs[0]
    run.profile.documents[0].text += " I documented the evaluation."
    run.profile.evidence.append(
        Evidence(
            id="E2",
            kind="project",
            summary="Documented the evaluation.",
            source_id="D1",
            quote="I documented the evaluation.",
        )
    )
    job.judgment.differentiators[0].evidence_ids = ["E2"]
    job.judgment.requirements[0].supporting_constraint_ids = ["C1"]
    job.judgment.requirements.reverse()
    card = project_job(job, run.profile)
    assert [item.requirement.id for item in card.qualifications] == ["R1", "R2"]
    assert card.strengths[0].explanation == "The project demonstrates Python."
    assert card.strengths[0].supporting_constraints == (run.profile.constraints[0],)
    assert [e.id for e in card.strengths[1].evidence] == ["E1", "E2"]
    assert card.strengths[1].differentiators == tuple(job.judgment.differentiators)
    assert card.strengths[1].evidence[1].quote == "I documented the evaluation."


@pytest.mark.parametrize("damage", ["missing-id", "unsupported-quote", "declaration-only"])
def test_missing_evidence_never_becomes_a_claimed_strength(run, damage):
    job = run.jobs[0]
    if damage == "unsupported-quote":
        run.profile.evidence[0].quote = "This quote is not in the candidate source."
    else:
        for item in job.judgment.requirements:
            item.evidence_ids = ["missing"] if damage == "missing-id" else []
            item.supporting_constraint_ids = ["C1"] if damage == "declaration-only" else []
        job.judgment.differentiators[0].evidence_ids = ["missing"]
    card = project_job(job, run.profile)
    assert card.strengths == ()
    assert all(item.status == "meets" for item in job.judgment.requirements)
    if damage != "declaration-only":
        assert len(card.unresolved_qualifications) == 2
        assert select_dashboard_cards(run, now=NOW) == []


def test_unknowns_are_distinct_from_confirmed_gaps(run, stretch):
    stretch.judgment.requirements[1].status = "unknown"
    stretch.judgment.uncertainties = ["Synthetic uncertainty: confirm tooling familiarity."]
    card = project_job(stretch, run.profile)
    assert [item.requirement.id for item in card.qualification_gaps] == ["R3"]
    assert [item.requirement.id for item in card.unresolved_qualifications] == ["R2"]
    assert [item.requirement.id for item in card.strengths] == ["R1"]
    assert card.qualification_gaps[0].explanation == stretch.judgment.requirements[-1].explanation
    assert card.uncertainties == tuple(stretch.judgment.uncertainties)


def test_preview_preserves_all_assessments_and_never_mutates_the_run(run, stretch):
    run.inspected_jobs.append(stretch)
    before = run.model_dump_json()
    card = project_job(stretch, run.profile, requirement_limit=1)
    assert card.requirements == (stretch.facts.requirements[0],)
    assert len(card.qualifications) == 3
    assert card.ranked_job is stretch
    assert (
        len(select_dashboard_cards(run, reviewed_stretches=[review_for(run, stretch)], now=NOW))
        == 2
    )
    assert run.model_dump_json() == before
    with pytest.raises(ValueError, match="requirement_limit"):
        project_job(stretch, run.profile, requirement_limit=-1)


def test_optional_data_and_unsafe_urls_do_not_crash_projection(run):
    job = run.jobs[0]
    job.facts.location = None
    job.judgment.differentiators = []
    job.judgment.uncertainties = []
    assert project_job(job, run.profile).location is None
    job.discovery.url = "javascript:alert(1)"
    assert project_job(job, run.profile).original_url == job.page.requested_url
    job.page.requested_url = job.page.url = "invalid"
    assert project_job(job, run.profile).original_url == ""
    job.facts, job.judgment = None, None
    card = project_job(job, run.profile)
    assert card.title == job.discovery.title
    assert card.company == job.discovery.company
    assert card.requirements == card.qualifications == card.strengths == ()
    assert select_dashboard_cards(run, now=NOW) == []


def test_november_11_saved_run_has_zero_eligible_cards():
    # Optional local replay: no applicant content is copied into fixtures or test output.
    path = Path(__file__).resolve().parents[1] / "data/runs/25002a07cfed44c3bf06ba3885fcd23d.json"
    if not path.exists():
        pytest.skip("The optional November 11 local validation artifact is not installed.")
    before = path.read_bytes()
    run = Run.model_validate_json(before)
    reviews = [
        StretchReview(run.id, job.discovery.url, ("req-02",), "Test review cannot override gates.")
        for job in run.inspected_jobs
    ]
    assert select_dashboard_cards(run, now=NOW) == []
    assert select_dashboard_cards(run, reviewed_stretches=reviews, now=NOW) == []
    assert path.read_bytes() == before
