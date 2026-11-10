"""Clearly synthetic fixtures for adversarial checks; never demo job data."""

import pytest

from jobengine.models import (
    Constraint,
    ConstraintAssessment,
    Differentiator,
    DiscoveryItem,
    Document,
    Evidence,
    FitJudgment,
    InterestAlignment,
    JobFacts,
    JobPage,
    Profile,
    Requirement,
    RequirementAssessment,
)


@pytest.fixture(autouse=True)
def isolated_app_data(monkeypatch, tmp_path):
    # UI tests must never change the user's saved candidates or active session.
    monkeypatch.setenv("JOBENGINE_DATA_DIR", str(tmp_path / "app-data"))


@pytest.fixture
def intake():
    from jobengine.models import IntakePreferences

    return IntakePreferences(
        authorization_country="United States",
        authorized_to_work="yes",
        sponsorship_now="no",
        sponsorship_future="no",
        graduation_status="graduated",
        graduation_month=9,
        graduation_year=2026,
        start_mode="asap",
    )


@pytest.fixture
def case():
    quote = (
        "I built a Python ranking service and evaluated retrieval quality on a held-out dataset."
    )
    source = "SYNTHETIC TEST CANDIDATE. " + quote
    profile = Profile(
        documents=[Document(id="D1", label="Synthetic test account", text=source)],
        evidence=[Evidence(id="E1", kind="project", summary=quote, source_id="D1", quote=quote)],
        target_roles=["Software engineer"],
        interests="Ranking and search systems",
        constraints=[Constraint(id="C1", field="Location", value="New York", hard=True)],
        professional_years=0,
        confirmed=True,
    )
    text = "SYNTHETIC TEST JOB. Python programming is required. Build ranking systems for search. This role is based in New York. Apply for this job. Two years of professional experience required."
    page = JobPage(
        requested_url="https://example.com/test-job",
        url="https://example.com/test-job",
        retrieved_at="2026-11-07T23:00:00+00:00",
        text=text,
        content_hash="synthetic",
        extraction_method="synthetic fixture",
        status="retrieved",
    )
    facts = JobFacts(
        is_job_posting=True,
        title="Synthetic engineer",
        company="Synthetic company",
        location="New York",
        posted_at=None,
        valid_through=None,
        availability="open",
        availability_quote="Apply for this job",
        requirements=[
            Requirement(
                id="R1",
                text="Python programming",
                quote="Python programming is required",
                importance="required",
                minimum_professional_years=None,
            ),
            Requirement(
                id="R2",
                text="Build ranking systems",
                quote="Build ranking systems for search",
                importance="responsibility",
                minimum_professional_years=None,
            ),
        ],
        extraction_notes=[],
    )
    judgment = FitJudgment(
        requirements=[
            RequirementAssessment(
                requirement_id="R1",
                status="meets",
                evidence_ids=["E1"],
                explanation="The project demonstrates Python.",
            ),
            RequirementAssessment(
                requirement_id="R2",
                status="meets",
                evidence_ids=["E1"],
                explanation="The project involved ranking.",
            ),
        ],
        constraints=[
            ConstraintAssessment(
                constraint_id="C1",
                status="meets",
                job_quote="This role is based in New York",
                explanation="Location agrees.",
            )
        ],
        differentiators=[
            Differentiator(
                requirement_id="R2",
                evidence_ids=["E1"],
                explanation="Built and evaluated a ranking service.",
            )
        ],
        interest_alignment=InterestAlignment(
            level="high",
            job_quote="Build ranking systems for search",
            explanation="Ranking and search match the stated interests.",
        ),
        uncertainties=[],
    )
    item = DiscoveryItem(title=facts.title, company=facts.company, url=page.url)
    return profile, item, page, facts, judgment


@pytest.fixture
def search_brief(case):
    from jobengine.models import SearchBriefDraft, SearchStrength, SectorTarget

    return SearchBriefDraft(
        summary="Find entry-level work on search and retrieval systems.",
        target_roles=case[0].target_roles,
        work_themes=["Search and retrieval"],
        strengths=[
            SearchStrength(
                statement="Built and evaluated a Python ranking service.", evidence_ids=["E1"]
            )
        ],
        search_queries=["new graduate Python search retrieval software engineer"],
        avoid=["Mandatory multi-year professional experience"],
        stretch_policy="Consider missing preferences; keep essential gaps out of strong matches.",
        unresolved_questions=["Some skills may not yet be documented."],
        sectors=[
            SectorTarget(
                name="Software products",
                relationship="preferred",
                rationale="Broad search/product work fits the stated interests.",
            )
        ],
    )
