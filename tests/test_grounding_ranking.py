import pytest

from jobengine.grounding import contains_quote, validate_evidence, validate_facts
from jobengine.models import Evidence, Requirement, RequirementAssessment
from jobengine.search.ranking import rank_job, sort_jobs


def test_quote_normalization_does_not_accept_paraphrase():
    assert contains_quote("Python\n programming  is required", "Python programming is required")
    assert not contains_quote(
        "Python programming is required", "Expert Python programming is required"
    )
    assert not contains_quote("Python programming is required", "Python…required")


def test_invented_candidate_evidence_rejected(case):
    profile, *_ = case
    validate_evidence(profile.evidence, profile.documents)
    profile.evidence[0].quote = "I worked professionally at an imaginary employer."
    with pytest.raises(ValueError, match="no valid quotation"):
        validate_evidence(profile.evidence, profile.documents)


def test_invented_employer_requirement_rejected(case):
    _, _, page, facts, _ = case
    validate_facts(facts, page.text)
    facts.requirements[0].quote = "Five years of production Python is mandatory."
    with pytest.raises(ValueError, match="unsupported employer quotation"):
        validate_facts(facts, page.text)


def test_grounded_role_specific_match_is_strong(case):
    result = rank_job(*case)
    assert result.category == "strong"


def test_model_identified_professional_experience_gap_excludes(case):
    profile, item, page, facts, judgment = case
    facts.requirements.append(
        Requirement(
            id="R3",
            text="Two professional years",
            quote="Two years of professional experience required",
            importance="required",
            minimum_professional_years=2,
        )
    )
    judgment.requirements.append(
        RequirementAssessment(
            requirement_id="R3",
            status="gap",
            evidence_ids=["E1"],
            explanation="The project does not establish the required professional tenure.",
        )
    )
    result = rank_job(profile, item, page, facts, judgment)
    assert result.category == "excluded"
    assert result.judgment.requirements[-1].status == "gap"


def test_model_identified_preferred_experience_gap_does_not_exclude(case):
    profile, item, page, facts, judgment = case
    facts.requirements.append(
        Requirement(
            id="R3",
            text="Two professional years preferred",
            quote="Two years of professional experience required",
            importance="preferred",
            minimum_professional_years=2,
        )
    )
    judgment.requirements.append(
        RequirementAssessment(
            requirement_id="R3",
            status="gap",
            evidence_ids=["E1"],
            explanation="Unsupported tenure.",
        )
    )
    result = rank_job(profile, item, page, facts, judgment)
    assert result.category == "strong"
    assert result.judgment.requirements[-1].status == "gap"


@pytest.mark.parametrize("professional_years", [None, 0.75])
def test_contextual_experience_assessment_is_not_overwritten(case, professional_years):
    profile, item, page, facts, judgment = case
    profile.professional_years = professional_years
    education = "I completed a bachelor's degree in computer science."
    profile.documents[0].text += " " + education
    profile.evidence.append(
        Evidence(id="E2", kind="education", summary=education, source_id="D1", quote=education)
    )
    requirement = (
        "Two years of professional experience required; relevant completed education may "
        "substitute year for year."
    )
    page.text += " " + requirement
    facts.requirements.append(
        Requirement(
            id="R3",
            text=requirement,
            quote=requirement,
            importance="required",
            minimum_professional_years=2,
        )
    )
    assessment = RequirementAssessment(
        requirement_id="R3",
        status="meets",
        evidence_ids=["E2"],
        explanation="The completed relevant degree satisfies the stated education alternative.",
    )
    judgment.requirements.append(assessment)
    validate_evidence(profile.evidence, profile.documents)
    validate_facts(facts, page.text)

    result = rank_job(profile, item, page, facts, judgment)

    assert result.category == "strong"
    assert result.judgment.requirements[-1] == assessment
    assert result.validation_issues == []


def test_enough_general_tenure_does_not_prove_specialized_experience(case):
    profile, item, page, facts, judgment = case
    profile.professional_years = 3
    facts.requirements[0].minimum_professional_years = 2
    judgment.requirements[0].status = "unknown"
    assert rank_job(profile, item, page, facts, judgment).category == "needs_review"


def test_explicit_applicant_constraint_can_support_eligibility(case):
    profile, item, page, facts, judgment = case
    judgment.requirements[0].evidence_ids = []
    judgment.requirements[0].supporting_constraint_ids = ["C1"]
    facts.requirements[0].text = "Work in New York"
    facts.requirements[0].quote = "This role is based in New York"
    assert rank_job(profile, item, page, facts, judgment).category == "strong"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_requirement",
        "invented_evidence_id",
        "missing_constraint",
        "invented_constraint_quote",
        "unknown_required",
    ],
)
def test_missing_or_unsupported_eligibility_cannot_be_strong(case, mutation):
    profile, item, page, facts, judgment = case
    if mutation == "missing_requirement":
        judgment.requirements.pop(0)
    elif mutation == "invented_evidence_id":
        judgment.requirements[0].evidence_ids = ["FAKE"]
    elif mutation == "missing_constraint":
        judgment.constraints.clear()
    elif mutation == "invented_constraint_quote":
        judgment.constraints[0].job_quote = "The employer permits work from anywhere on Earth."
    else:
        judgment.requirements[0].status = "unknown"
    assert rank_job(profile, item, page, facts, judgment).category == "needs_review"


def test_hard_constraint_conflict_excludes_but_soft_preference_does_not(case):
    profile, item, page, facts, judgment = case
    judgment.constraints[0].status = "conflict"
    assert rank_job(profile, item, page, facts, judgment).category == "excluded"
    profile.constraints[0].hard = False
    assert rank_job(profile, item, page, facts, judgment).category == "strong"


def test_without_distinguishing_strength_result_is_explicitly_weaker(case):
    profile, item, page, facts, judgment = case
    judgment.differentiators.clear()
    assert rank_job(profile, item, page, facts, judgment).category == "weaker"


def test_closed_or_expired_jobs_are_excluded(case):
    profile, item, page, facts, judgment = case
    facts.valid_through = "2020-01-01T00:00:00Z"
    assert rank_job(profile, item, page, facts, judgment).category == "excluded"
    facts.valid_through, facts.availability = None, "closed"
    assert rank_job(profile, item, page, facts, judgment).category == "excluded"


def test_category_wins_over_score(case):
    strong = rank_job(*case)
    weaker = strong.model_copy(update={"category": "weaker", "score": 100})
    assert sort_jobs([weaker, strong])[0].category == "strong"
