from jobengine.grounding import sanitize_job_facts, validate_facts
from jobengine.search.ranking import rank_job


def test_exact_short_apply_label_does_not_discard_a_job(case):
    _, _, page, facts, _ = case
    facts.availability_quote = "Apply"
    validate_facts(facts, page.text)


def test_title_only_cohort_mismatch_is_visible_without_inventing_a_strict_exclusion(case, intake):
    profile, item, page, facts, judgment = case
    profile.intake = intake
    facts.title = "Software Engineer, New Grad 2027"
    page.text += " " + facts.title
    result = rank_job(profile, item, page, facts, judgment)
    assert result.category == "needs_review"
    assert any("2027" in reason and "2026" in reason for reason in result.reasons)


def test_supported_explicit_graduation_window_takes_precedence_over_title_year(case, intake):
    from jobengine.models import Requirement, RequirementAssessment

    profile, item, page, facts, judgment = case
    profile.intake = intake
    facts.title = "Software Engineer, New Grad 2027"
    quote = "Graduating between September 2026 and June 2027"
    page.text += " " + facts.title + " " + quote
    facts.requirements.append(
        Requirement(
            id="R3", text=quote, quote=quote, importance="required", minimum_professional_years=None
        )
    )
    judgment.requirements.append(
        RequirementAssessment(
            requirement_id="R3",
            status="meets",
            evidence_ids=[],
            supporting_constraint_ids=["C1"],
            explanation="The confirmed September 2026 graduation is inside the stated window.",
        )
    )
    assert rank_job(profile, item, page, facts, judgment).category == "strong"


def test_untraceable_availability_is_unknown_while_requirements_remain_valid(case):
    _, _, page, facts, _ = case
    facts.availability_quote = "Invented acceptance wording"
    cleaned = sanitize_job_facts(facts, page.text)
    validate_facts(cleaned, page.text)
    assert cleaned.availability == "unknown"
    assert cleaned.requirements == facts.requirements


def test_no_sponsorship_needed_does_not_require_employer_sponsorship(case, intake):
    from jobengine.models import ConstraintAssessment, EmployerTerm
    from jobengine.onboarding.preferences import intake_constraints

    profile, item, page, facts, judgment = case
    profile.intake, profile.constraints = intake, intake_constraints(intake)
    page.text += " Work country: United States."
    facts.terms = [EmployerTerm(field="work_country", value="United States", quote="United States")]
    judgment.constraints = [
        ConstraintAssessment(
            constraint_id="C2",
            status="unknown",
            job_quote=None,
            explanation="Employer does not mention sponsorship.",
        )
    ]
    result = rank_job(profile, item, page, facts, judgment)
    assert next(c for c in result.judgment.constraints if c.constraint_id == "C2").status == "meets"
    # Authorization is not evidence of citizenship or clearance.
    facts.terms.append(
        EmployerTerm(field="authorization", value="US citizenship required", quote="United States")
    )
    assert (
        next(
            c
            for c in rank_job(profile, item, page, facts, judgment).judgment.constraints
            if c.constraint_id == "C2"
        ).status
        == "unknown"
    )


def test_evaluation_preserves_description_when_availability_is_ungrounded(
    case, monkeypatch, tmp_path
):
    from jobengine.infrastructure.storage import Store
    from jobengine.search import pipeline

    profile, item, page, facts, judgment = case
    facts.availability_quote = "Not a real quotation"
    monkeypatch.setattr(pipeline, "fetch_page", lambda *args: page)

    class Client:
        store = Store(tmp_path)

        def structured(self, operation, *args):
            return facts if operation == "extract_job" else judgment

    result = pipeline.evaluate(Client(), profile, item)
    assert result.category == "strong"
    assert result.facts.availability == "unknown"


def test_failed_requirement_repair_cannot_silently_remove_a_mandatory_gap(
    case, monkeypatch, tmp_path
):
    from jobengine.infrastructure.storage import Store
    from jobengine.search import pipeline

    profile, item, page, facts, _ = case
    facts.requirements[0].quote = "An invented required qualification"
    operations = []
    monkeypatch.setattr(pipeline, "fetch_page", lambda *args: page)

    class Client:
        store = Store(tmp_path)

        def structured(self, operation, *args):
            operations.append(operation)
            return facts

    result = pipeline.evaluate(Client(), profile, item)
    assert result.category == "unverified"
    assert operations == ["extract_job", "repair_job_facts"]


def test_requirement_removed_by_repair_prevents_a_strong_recommendation(
    case, monkeypatch, tmp_path
):
    from jobengine.infrastructure.storage import Store
    from jobengine.search import pipeline

    profile, item, page, facts, judgment = case
    original = facts.model_copy(deep=True)
    original.requirements[0].quote = "An unsupported quotation"
    repaired = facts.model_copy(deep=True)
    repaired.requirements = [r for r in repaired.requirements if r.id != "R1"]
    repaired.requirements[0].importance = "required"
    judgment.requirements = [a for a in judgment.requirements if a.requirement_id == "R2"]
    monkeypatch.setattr(pipeline, "fetch_page", lambda *args: page)

    class Client:
        store = Store(tmp_path)

        def structured(self, operation, *args):
            return (
                original
                if operation == "extract_job"
                else repaired
                if operation == "repair_job_facts"
                else judgment
            )

    result = pipeline.evaluate(Client(), profile, item)
    assert result.category == "needs_review"
    assert any("disappeared" in issue for issue in result.validation_issues)


def test_job_at_a_different_company_cannot_inherit_the_researched_sector(
    case, monkeypatch, tmp_path
):
    from jobengine.infrastructure.storage import Store
    from jobengine.search import pipeline

    profile, item, page, facts, _ = case
    item.company_source_url = "https://researched.example/"
    item.company = "Researched Employer"
    monkeypatch.setattr(pipeline, "fetch_page", lambda *args: page)

    class Client:
        store = Store(tmp_path)

        def structured(self, *args):
            return facts

    assert pipeline.evaluate(Client(), profile, item).category == "unverified"
