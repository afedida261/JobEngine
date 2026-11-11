"""Deterministic prioritization uses synthetic sources and no network/model calls."""

import hashlib

import httpx
import pytest

from jobengine.models import DiscoveryItem, IntakePreferences, JobPage
from jobengine.onboarding.brief import confirm_search_brief
from jobengine.onboarding.preferences import intake_constraints
from jobengine.search.prescreen import prioritize_postings


@pytest.fixture
def approved(case, search_brief):
    profile = case[0].model_copy(deep=True)
    profile.target_roles = ["Local news reporter", "Staff writer"]
    profile.interests = "Education, culture, community reporting"
    profile.intake = IntakePreferences(
        graduation_status="unknown",
        locations=["NYC"],
        work_arrangements=["on_site", "hybrid", "remote"],
    )
    profile.constraints = intake_constraints(profile.intake)
    draft = search_brief.model_copy(deep=True)
    draft.target_roles = profile.target_roles
    profile.search_brief = confirm_search_brief(profile, draft)
    return profile


def candidate(title, description="", location=None, status="retrieved", identity=1):
    text = (
        (f"Location: {location}\n" if location is not None else "")
        + "\nJOB DESCRIPTION\n"
        + description
    )
    item = DiscoveryItem(
        title=title, company="Synthetic", url=f"https://example.com/jobs/{identity}"
    )
    return item, JobPage(
        requested_url=item.url,
        url=item.url,
        retrieved_at="2026-11-10T00:00:00+00:00",
        text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        extraction_method="synthetic",
        status=status,
    )


def test_direct_and_adjacent_roles_outrank_unrelated_without_assessing_qualifications(
    approved, monkeypatch
):
    from jobengine.infrastructure.llm import ModelClient

    def fail(*args, **kwargs):
        pytest.fail("Pre-screening must not make network/model calls")

    monkeypatch.setattr(httpx.Client, "send", fail)
    monkeypatch.setattr(ModelClient, "structured", fail)
    monkeypatch.setattr(ModelClient, "search", fail)
    direct = candidate(
        "Local News Reporter",
        "Report on education and community culture. Write local news stories.",
        "New York, NY",
        identity=1,
    )
    adjacent = candidate(
        "Editorial Assistant",
        "Research education topics and support writers reporting on community culture.",
        identity=2,
    )
    unrelated = candidate(
        "Payroll Accountant",
        "Reconcile payroll balances and process supplier invoices.",
        "New York, NY",
        identity=3,
    )
    before = approved.model_dump()
    selected, ranked = prioritize_postings([unrelated, adjacent, direct], approved, top_n=2)
    assert [item.title for item, _ in selected] == [direct[0].title, adjacent[0].title]
    assert ranked[0]["score"] > ranked[1]["score"] > ranked[2]["score"]
    assert ranked[1]["breakdown"]["description_roles"] > 0
    assert ranked[2]["status"] == "not_selected_for_batch"
    assert all(row["score"] == round(sum(row["breakdown"].values()), 2) for row in ranked)
    assert all(0 <= row["score"] <= 100 and row["reasons"] for row in ranked)
    assert approved.model_dump() == before


def test_missing_location_arrangement_experience_and_failed_descriptions_are_retained(approved):
    missing = candidate("Staff writer", "Write articles about education.", identity=1)
    failed = candidate("Local news reporter", status="failed", identity=2)
    selected, ranked = prioritize_postings([missing, failed], approved, top_n=10)
    assert len(selected) == len(ranked) == 2
    assert all(row["status"] == "selected_for_evaluation" for row in ranked)
    assert any("location is missing" in reason for row in ranked for reason in row["reasons"])
    assert any("failed retrieval" in reason for row in ranked for reason in row["reasons"])
    assert any(page is failed[1] for _, page in selected)


def test_location_signals_do_not_treat_headquarters_boilerplate_as_job_location(approved):
    local = candidate(
        "Staff writer", "Write and report on community news.", "New York, NY", identity=1
    )
    remote = candidate(
        "Staff writer", "Write and report on community news.", "Remote, United States", identity=2
    )
    away = candidate(
        "Staff writer",
        "Write and report on community news. Headquarters is in New York City.",
        "San Francisco, CA",
        identity=3,
    )
    _, ranked = prioritize_postings([away, remote, local], approved, top_n=3)
    rows = {row["item"].url: row for row in ranked}
    assert rows[local[0].url]["breakdown"]["location_signal"] == 10
    assert rows[remote[0].url]["breakdown"]["location_signal"] == 5
    assert rows[away[0].url]["breakdown"]["location_signal"] == 0
    assert any("unverified" in reason for reason in rows[remote[0].url]["reasons"])
    assert len(ranked) == 3


def test_complete_pool_order_is_stable_and_top_n_only_selects_a_batch(approved):
    pool = [candidate("Unusual role", identity=i) for i in range(5)]
    selected, ranked = prioritize_postings(pool, approved, top_n=2)
    assert selected == pool[:2]
    assert [(r["item"], r["page"]) for r in ranked] == pool
    assert [r["status"] for r in ranked] == ["selected_for_evaluation"] * 2 + [
        "not_selected_for_batch"
    ] * 3
    assert [r["rank"] for r in ranked] == [1, 2, 3, 4, 5]
    assert prioritize_postings(pool, approved, top_n=2)[1] == ranked
    assert prioritize_postings([], approved) == ([], [])
    assert prioritize_postings(pool, approved, top_n=0)[0] == []


@pytest.mark.parametrize("top_n", [-1, 1.5, True])
def test_invalid_batch_size(top_n, approved):
    with pytest.raises(ValueError, match="nonnegative integer"):
        prioritize_postings([], approved, top_n=top_n)


def test_unapproved_or_edited_profiles_are_rejected(approved):
    approved.confirmed = False
    with pytest.raises(ValueError, match="confirm"):
        prioritize_postings([], approved)
    approved.confirmed = True
    approved.interests = "Changed after approval"
    with pytest.raises(ValueError, match="changed"):
        prioritize_postings([], approved)


def test_short_acronyms_keep_title_context_and_reporting_is_not_equated_with_ux_management(
    approved,
):
    reporter = candidate(
        "Reporting Fellow", "Report on education and community culture.", "NYC", identity=1
    )
    ux = candidate(
        "Senior Manager, UX Writing",
        "Write product interfaces and manage design systems.",
        "NYC",
        identity=2,
    )
    selected, ranked = prioritize_postings([ux, reporter], approved, top_n=1)
    assert selected[0][0] == reporter[0]
    assert ranked[1]["breakdown"]["title_roles"] == 15


def test_inflection_matching_and_titles_without_exact_role_phrases(approved):
    selected, ranked = prioritize_postings(
        [
            candidate(
                "Community Reporting Fellow", "Reporting on education and culture.", identity=1
            ),
            candidate("Writing Intern", "Write community news articles.", identity=2),
        ],
        approved,
        top_n=2,
    )
    assert len(selected) == 2
    assert all(row["breakdown"]["title_roles"] > 0 for row in ranked)


def test_description_metadata_and_employer_name_do_not_supply_role_keyword_hits(approved):
    pair = candidate(
        "Payroll Accountant", "Balance financial ledgers and supplier invoices.", "NYC"
    )
    pair[
        1
    ].text = "Title: Local news reporter\nEmployer: Community Writer\nLocation: NYC\n\nJOB DESCRIPTION\nBalance financial ledgers and supplier invoices."
    _, ranked = prioritize_postings([pair], approved)
    assert ranked[0]["breakdown"]["description_roles"] == 0
