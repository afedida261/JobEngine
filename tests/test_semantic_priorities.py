"""Pure deterministic priorities: all sources/profiles are synthetic, no API calls."""

from copy import deepcopy

import httpx
import pytest

from jobengine.models import Constraint, DiscoveryItem, IntakePreferences, JobPage, Requirement
from jobengine.onboarding.brief import confirm_search_brief
from jobengine.onboarding.preferences import intake_constraints
from jobengine.search.semantic_priorities import apply_priority_adjustments


def review(profile, brief):
    draft = brief.model_copy(deep=True)
    draft.target_roles = profile.target_roles
    profile.search_brief = confirm_search_brief(profile, draft)
    return profile


@pytest.fixture
def approved(case, search_brief):
    profile = case[0].model_copy(deep=True)
    profile.professional_years = 0
    profile.intake = IntakePreferences(
        graduation_status="graduated",
        locations=["Washington DC"],
        work_arrangements=["remote", "hybrid", "on_site"],
    )
    profile.constraints = intake_constraints(profile.intake)
    return review(profile, search_brief)


@pytest.fixture(autouse=True)
def no_calls(monkeypatch):
    from openai.resources.embeddings import Embeddings

    from jobengine.infrastructure.llm import ModelClient

    def fail(*args, **kwargs):
        pytest.fail("Priority adjustments must never call a provider")

    monkeypatch.setattr(httpx.Client, "send", fail)
    monkeypatch.setattr(Embeddings, "create", fail)
    monkeypatch.setattr(ModelClient, "structured", fail)


def row(body, similarity=0.6, identity=1, header="", title="Synthetic role"):
    item = DiscoveryItem(title=title, company="Synthetic", url=f"https://example.com/{identity}")
    page = JobPage(
        requested_url=item.url,
        url=item.url,
        retrieved_at="2026-11-10T00:00:00+00:00",
        text=header + "\n\nJOB DESCRIPTION\n" + body,
        content_hash="synthetic",
        extraction_method="synthetic",
        status="retrieved",
    )
    return {
        "item": item,
        "page": page,
        "similarity": similarity,
        "rank": identity,
        "status": "selected_for_evaluation",
        "embedding_status": "scored" if similarity is not None else "failed",
    }


@pytest.mark.parametrize(
    "role", ["Software engineer", "Registered nurse", "Data scientist", "Local news reporter"]
)
def test_explicit_experience_priorities_across_professions_and_career_levels(
    approved, search_brief, role
):
    approved.target_roles = [role]
    review(approved, search_brief)
    pool = [
        row("Requires twelve years of professional tenure.", identity=1, title="Senior " + role),
        row("Requires two years of experience.", identity=2),
        row("Experience preferred; bring transferable skills.", identity=3),
    ]
    selected, ranked = apply_priority_adjustments(pool, approved, top_n=3)
    byid = {r["item"].url: r for r in ranked}
    assert byid[pool[0]["item"].url]["adjustments"]["experience"] == -0.08
    assert byid[pool[1]["item"].url]["adjustments"]["experience"] == -0.015
    assert byid[pool[2]["item"].url]["adjustments"]["experience"] == 0
    assert len(selected) == 3
    approved.professional_years = 15
    review(approved, search_brief)
    _, experienced = apply_priority_adjustments(pool, approved)
    assert (
        next(r for r in experienced if r["item"].url == pool[0]["item"].url)["adjustments"][
            "experience"
        ]
        == 0.005
    )


@pytest.mark.parametrize(
    "body",
    [
        "Requires three years of experience or equivalent education.",
        "At least three years of experience preferred.",
        "Prior experience not required.",
        "No experience of software engineer duties required.",
    ],
)
def test_ambiguous_alternatives_and_no_experience_required_stay_neutral(approved, body):
    _, rows = apply_priority_adjustments([row(body)], approved)
    assert rows[0]["adjustments"]["experience"] == 0
    assert rows[0]["adjustments"]["misleading"] == 0


def test_structured_job_facts_require_source_quotes(approved, case):
    original = row("Applicants must demonstrate four years in professional practice.")
    facts = case[3].model_copy(deep=True)
    facts.requirements = [
        Requirement(
            id="R1",
            text="Required professional practice",
            quote="Applicants must demonstrate four years in professional practice.",
            importance="required",
            minimum_professional_years=4,
        )
    ]
    _, rows = apply_priority_adjustments(
        [original], approved, job_facts={original["item"].url: facts}
    )
    assert rows[0]["adjustments"]["experience"] == -0.04
    facts.requirements[0].quote = "Invented requirement absent from source."
    _, rows = apply_priority_adjustments(
        [original], approved, job_facts={original["item"].url: facts}
    )
    assert rows[0]["adjustments"]["experience"] == 0
    assert any("Untraceable" in flag for flag in rows[0]["uncertainty_flags"])


def test_title_seniority_and_missing_applicant_experience_are_not_invented_requirements(
    approved, search_brief
):
    _, rows = apply_priority_adjustments(
        [row("No explicit experience requirement.", title="Senior manager")], approved
    )
    assert rows[0]["adjustments"]["experience"] == 0
    approved.professional_years = None
    review(approved, search_brief)
    _, rows = apply_priority_adjustments(
        [row("Requires ten years of professional experience.")], approved
    )
    assert rows[0]["adjustments"]["experience"] == 0


def test_location_normalization_unknown_aliases_and_remote_eligibility(approved):
    pool = [
        row("Duties.", identity=1, header="Location: Washington, DC\nWork arrangement: hybrid\n"),
        row(
            "Duties.",
            identity=2,
            header="Location: Remote, United States\nWork arrangement: remote\n",
        ),
        row(
            "Duties.",
            identity=3,
            header="Location: Remote\nWork arrangement: remote\nEligible work locations: Washington DC\n",
        ),
        row("Duties.", identity=4, header="Location: Various offices\n"),
        row("Duties.", identity=5, header="Location: California\nRelocation required: yes\n"),
    ]
    _, rows = apply_priority_adjustments(pool, approved)
    values = {r["embedding_rank"]: r for r in rows}
    assert values[1]["adjustments"]["location"] == 0.01
    assert values[2]["adjustments"]["location"] == 0
    assert values[3]["adjustments"]["location"] == 0.01
    assert values[4]["adjustments"]["location"] == 0
    assert values[5]["adjustments"]["location"] == -0.04
    assert any("unverified" in flag for flag in values[2]["uncertainty_flags"])


def test_explicit_remote_only_and_geographic_exclusions(approved, search_brief):
    approved.intake.work_arrangements = ["remote"]
    approved.constraints = intake_constraints(approved.intake)
    review(approved, search_brief)
    _, rows = apply_priority_adjustments(
        [
            row(
                "Duties.",
                header="Location: California\nWork arrangement: on_site\nExcluded work locations: Washington DC\n",
            )
        ],
        approved,
    )
    assert rows[0]["adjustments"]["location"] == -0.04


def test_employment_preferences_are_explicit_and_hours_are_not_internship_preferences(
    approved, search_brief
):
    posting = row("Duties.", header="Employment type: Full-time internship\n")
    _, rows = apply_priority_adjustments([posting], approved)
    assert rows[0]["adjustments"]["employment_type"] == 0
    approved.constraints.append(
        Constraint(id="P1", field="Employment type", value="Full-time only", hard=True)
    )
    review(approved, search_brief)
    _, rows = apply_priority_adjustments([posting], approved)
    assert rows[0]["adjustments"]["employment_type"] == 0.005
    approved.constraints.append(
        Constraint(id="P2", field="Other exclusions", value="No internships", hard=True)
    )
    review(approved, search_brief)
    _, rows = apply_priority_adjustments([posting], approved)
    assert rows[0]["adjustments"]["employment_type"] == -0.03
    _, missing = apply_priority_adjustments([row("Duties.")], approved)
    assert missing[0]["adjustments"]["employment_type"] == 0


@pytest.mark.parametrize("employment", ["Temporary", "Contract"])
def test_permanent_preferences_can_deprioritize_explicit_alternatives(
    approved, search_brief, employment
):
    approved.constraints.append(
        Constraint(id="P1", field="Employment type", value="permanent", hard=True)
    )
    review(approved, search_brief)
    _, rows = apply_priority_adjustments(
        [row("Duties.", header=f"Employment type: {employment}\n")], approved
    )
    assert rows[0]["adjustments"]["employment_type"] == -0.03


def test_clear_nonjobs_duty_denials_and_uncertain_advertisements(approved):
    pool = [
        row("This is not a job opening. Buy our products.", identity=1),
        row("No software engineer duties. Sell subscriptions instead.", identity=2),
        row("Discuss career courses with customers.", identity=3),
    ]
    _, rows = apply_priority_adjustments(pool, approved)
    values = {r["embedding_rank"]: r for r in rows}
    assert values[1]["adjustments"]["misleading"] == -0.10
    assert values[2]["adjustments"]["misleading"] == -0.08
    assert values[3]["adjustments"]["misleading"] == 0
    assert values[2]["adjustment_evidence"][0]["quote"] in pool[1]["page"].text


def test_modest_capped_adjustments_preserve_pool_scores_deferrals_and_semantic_dominance(approved):
    pool = [
        row("Requires twelve years of professional tenure. This is not a job opening.", 0.9, 1),
        row("Ordinary work.", 0.4, 2),
        row("Unscored source.", None, 3),
    ]
    before = deepcopy(pool)
    selected, rows = apply_priority_adjustments(pool, approved, top_n=1)
    assert selected == [(pool[0]["item"], pool[0]["page"])]
    assert rows[0]["similarity"] == 0.9 and rows[0]["final_score"] == 0.78
    assert rows[0]["adjustment_total"] == -0.12
    assert sum(rows[0]["adjustments"].values()) == pytest.approx(-0.12)
    assert len(rows) == 3 and rows[-1]["similarity"] is None and rows[-1]["final_score"] is None
    assert rows[1]["status"] == "not_selected_for_batch" and rows[1]["deferral_reasons"]
    assert pool == before


def test_numeric_structured_requirement_cannot_invent_years_not_in_quotation(approved, case):
    original = row("Applicants must demonstrate four years in professional practice.")
    facts = case[3].model_copy(deep=True)
    facts.requirements = [
        Requirement(
            id="R1",
            text="Required experience",
            quote=original["page"].text.split("JOB DESCRIPTION\n")[1],
            importance="required",
            minimum_professional_years=20,
        )
    ]
    _, rows = apply_priority_adjustments(
        [original], approved, job_facts={original["item"].url: facts}
    )
    assert rows[0]["adjustments"]["experience"] == 0
    assert any("numeric source evidence" in flag for flag in rows[0]["uncertainty_flags"])


def test_intake_exclusions_are_honored_without_inventing_a_full_time_preference(
    approved, search_brief
):
    approved.intake.other_exclusions = "No internships"
    review(approved, search_brief)
    _, rows = apply_priority_adjustments(
        [row("Duties.", header="Employment type: internship\n")], approved
    )
    assert rows[0]["adjustments"]["employment_type"] == -0.03


def test_adjustments_are_idempotent_and_do_not_accumulate(approved):
    original = [
        row("Requires twelve years of professional tenure.", 0.6, 1),
        row("Ordinary work.", 0.59, 2),
    ]
    _, once = apply_priority_adjustments(original, approved, top_n=1)
    _, twice = apply_priority_adjustments(once, approved, top_n=1)
    assert [(r["similarity"], r["final_score"], r["embedding_rank"], r["rank"]) for r in once] == [
        (r["similarity"], r["final_score"], r["embedding_rank"], r["rank"]) for r in twice
    ]


@pytest.mark.parametrize(
    "body",
    [
        "Requires five years of experience, but this is negotiable.",
        "Requires five years of experience, which may be waived.",
    ],
)
def test_negotiable_experience_minima_are_not_treated_as_strict(approved, body):
    _, rows = apply_priority_adjustments([row(body)], approved)
    assert rows[0]["adjustments"]["experience"] == 0


def test_negated_employment_type_is_not_a_positive_type_fact(approved, search_brief):
    approved.constraints.append(
        Constraint(id="P1", field="Other exclusions", value="No internships", hard=True)
    )
    review(approved, search_brief)
    _, rows = apply_priority_adjustments(
        [row("Duties.", header="Employment type: Not an internship\n")], approved
    )
    assert rows[0]["adjustments"]["employment_type"] == 0
    assert any("unambiguous structured value" in flag for flag in rows[0]["uncertainty_flags"])


@pytest.mark.parametrize(
    "phrase,minimum",
    [
        ("3–5 years of experience", 3),
        ("3 to 5 years of professional experience", 3),
        ("5+ years' experience at a media company or other relevant organization", 5),
        ("at least 3 years", 3),
        ("Minimum of three years of experience", 3),
    ],
)
def test_qualification_minimums_and_ranges(approved, phrase, minimum):
    _, rows = apply_priority_adjustments([row("Basic Qualifications:\n" + phrase)], approved)
    signal = next(e for e in rows[0]["adjustment_evidence"] if e["signal"] == "experience")
    assert signal["minimum_years"] == minimum and signal["maximum_years"] is None
    assert rows[0]["adjustments"]["experience"] == -0.04


@pytest.mark.parametrize(
    "body",
    [
        "Preferred Qualifications:\n3-5 years of experience.",
        "Requirements:\n5+ years of experience preferred.",
        "Responsibilities:\n3-5 years of experience.",
        "Our company has 150 years of experience.",
        "Qualifications:\nThe contract lasts 3 years.",
        "Requirements:\nAt least 3 years of experience or a degree.",
        "Requirements:\n3 years of experience or 2 years and a degree.",
        "Requirements:\n5-3 years of experience.",
    ],
)
def test_preferred_unrelated_and_ambiguous_numbers_stay_neutral(approved, body):
    _, rows = apply_priority_adjustments([row(body)], approved)
    assert rows[0]["adjustments"]["experience"] == 0


def test_experience_upper_bound_is_not_a_minimum(approved, search_brief):
    posting = row("Basic Qualifications:\nNo more than five years of professional experience.")
    _, rows = apply_priority_adjustments([posting], approved)
    assert rows[0]["adjustments"]["experience"] == 0.005
    evidence = rows[0]["adjustment_evidence"][0]
    assert evidence["maximum_years"] == 5 and evidence["minimum_years"] is None
    approved.professional_years = 8
    review(approved, search_brief)
    _, rows = apply_priority_adjustments([posting], approved)
    assert rows[0]["adjustments"]["experience"] == -0.04


@pytest.mark.parametrize(
    "allowed,label",
    [
        ("NYC", "New York, NY"),
        ("SF", "San Francisco, CA"),
        ("Boston", "Boston, Massachusetts"),
        ("Washington DC", "Washington, DC"),
        ("Seattle", "Seattle, WA"),
    ],
)
def test_geographic_aliases_across_cities(approved, search_brief, allowed, label):
    approved.intake.locations = [allowed]
    review(approved, search_brief)
    _, rows = apply_priority_adjustments([row("Duties.", header=f"Location: {label}\n")], approved)
    assert rows[0]["adjustments"]["location"] == 0.01


@pytest.mark.parametrize(
    "allowed,body,expected",
    [
        ("NYC", "Applicants must be based in the Midwest.", -0.04),
        ("Chicago, IL", "Applicants must be based in the Midwest.", 0),
        ("Boston", "Applicants must reside in Louisiana, Mississippi, or Arkansas.", -0.04),
        ("Baton Rouge, LA", "Applicants must reside in Louisiana, Mississippi, or Arkansas.", 0),
        ("NYC", "This is a remote position based in the Midwest.", -0.04),
        ("NYC", "This is a NY Guild role based in Louisiana, Mississippi, or Arkansas.", -0.04),
        ("Unknown City", "Applicants must be based in the Midwest.", 0),
        ("NYC", "Applicants must reside in the reporting community.", 0),
        ("NYC", "Our company is based in the Midwest. Work remotely.", 0),
        ("NYC", "Applicants may be based in the Midwest.", 0),
        ("Boston", "Applicants must reside in the greater New York area.", 0),
        ("NYC", "Applicants must reside in Louisiana or an unspecified location.", 0),
    ],
)
def test_residency_restrictions_are_source_backed_and_uncertain_coverage_neutral(
    approved, search_brief, allowed, body, expected
):
    approved.intake.locations = [allowed]
    review(approved, search_brief)
    _, rows = apply_priority_adjustments([row(body)], approved)
    assert rows[0]["adjustments"]["location"] == expected
    if expected < 0:
        signal = next(
            e for e in rows[0]["adjustment_evidence"] if e["signal"] == "residency_restriction"
        )
        assert signal["quote"] in body
