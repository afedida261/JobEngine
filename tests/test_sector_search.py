import pytest

from jobengine.infrastructure.storage import BudgetExceeded, Store
from jobengine.models import (
    CompanyCandidate,
    CompanyOutput,
    DiscoveryItem,
    SearchRecord,
    SearchStep,
    SectorTarget,
)
from jobengine.onboarding.brief import confirm_search_brief
from jobengine.search import pipeline


def setup_search(case, search_brief, tmp_path, monkeypatch, categories):
    profile = case[0]
    search_brief.sectors = [
        SectorTarget(name="Robotics", relationship="preferred", rationale="Stated interest"),
        SectorTarget(
            name="Industrial software", relationship="related", rationale="Related product work"
        ),
    ]
    profile.search_brief = confirm_search_brief(profile, search_brief)
    calls = []

    class Client:
        store = Store(tmp_path)
        model = "synthetic"

    def companies(client, profile, sector, **kwargs):
        calls.append(("companies", sector.name))
        company = CompanyCandidate(
            name=sector.name + " Co",
            official_url="https://"
            + ("robot" if sector.name == "Robotics" else "industrial")
            + ".example/",
            careers_url=None,
            sector_rationale="Synthetic research",
            sector=sector.name,
        )
        return SearchRecord(
            query="",
            retrieved_at="2026-11-07",
            summary="",
            sources=[],
            jobs=[],
            warnings=[],
            companies=[company],
            steps=[SearchStep(stage="companies", sector=sector.name)],
        )

    def jobs(client, profile, **kwargs):
        company, sector = kwargs["company"], kwargs["sector"]
        calls.append(("jobs", company.name))
        item = DiscoveryItem(
            title="Synthetic role",
            company=company.name,
            url=company.official_url + "job/1",
            sector=sector.name,
            company_source_url=company.official_url,
        )
        return SearchRecord(
            query="", retrieved_at="2026-11-07", summary="", sources=[], jobs=[item], warnings=[]
        )

    def evaluate(client, profile, item):
        calls.append(("evaluate", item.sector))
        result = pipeline.rank_job(*case)
        return result.model_copy(update={"discovery": item, "category": categories[item.sector]})

    monkeypatch.setattr(pipeline, "discover_companies", companies)
    monkeypatch.setattr(pipeline, "discover", jobs)
    monkeypatch.setattr(pipeline, "evaluate", evaluate)
    return Client(), profile, calls


def test_companies_are_found_before_their_jobs_and_rejections_trigger_sector_expansion(
    case, search_brief, tmp_path, monkeypatch
):
    client, profile, calls = setup_search(
        case,
        search_brief,
        tmp_path,
        monkeypatch,
        {"Robotics": "excluded", "Industrial software": "weaker"},
    )
    run = pipeline.run_search(client, profile, limit=1)
    assert calls == [
        ("companies", "Robotics"),
        ("jobs", "Robotics Co"),
        ("evaluate", "Robotics"),
        ("companies", "Industrial software"),
        ("jobs", "Industrial software Co"),
        ("evaluate", "Industrial software"),
    ]
    assert [j.category for j in run.jobs] == ["weaker"]
    assert len(run.inspected_jobs) == 2
    assert run.search.stop_reason == "shortlist_target_reached"


def test_filling_shortlist_does_not_expand_into_less_preferred_sectors(
    case, search_brief, tmp_path, monkeypatch
):
    client, profile, calls = setup_search(
        case,
        search_brief,
        tmp_path,
        monkeypatch,
        {"Robotics": "strong", "Industrial software": "strong"},
    )
    run = pipeline.run_search(client, profile, limit=1)
    assert len(run.jobs) == 1
    assert ("companies", "Industrial software") not in calls


def test_result_cap_is_ten_even_if_a_lower_layer_returns_too_many(
    case, search_brief, tmp_path, monkeypatch
):
    client, profile, _ = setup_search(
        case,
        search_brief,
        tmp_path,
        monkeypatch,
        {"Robotics": "strong", "Industrial software": "strong"},
    )

    def many(client, profile, **kwargs):
        company, sector = kwargs["company"], kwargs["sector"]
        items = [
            DiscoveryItem(
                title="Synthetic role",
                company=company.name,
                url=company.official_url + f"job/{i}",
                sector=sector.name,
            )
            for i in range(25)
        ]
        return SearchRecord(
            query="", retrieved_at="2026-11-07", summary="", sources=[], jobs=items, warnings=[]
        )

    monkeypatch.setattr(pipeline, "discover", many)
    run = pipeline.run_search(client, profile, limit=999)
    assert len(run.jobs) == 10
    assert len(run.inspected_jobs) == 10


def test_budget_stop_preserves_completed_results(case, search_brief, tmp_path, monkeypatch):
    client, profile, _ = setup_search(
        case,
        search_brief,
        tmp_path,
        monkeypatch,
        {"Robotics": "weaker", "Industrial software": "strong"},
    )
    original = pipeline.discover_companies

    def stop(client, profile, sector, **kwargs):
        if sector.relationship == "related":
            raise BudgetExceeded("Synthetic budget stop")
        return original(client, profile, sector, **kwargs)

    monkeypatch.setattr(pipeline, "discover_companies", stop)
    run = pipeline.run_search(client, profile, limit=2)
    assert len(run.jobs) == 1 and run.search.stop_reason == "budget_limit"
    assert client.store.load_run(run.id)["jobs"]


def test_job_identity_deduplicates_tracking_and_ats_aliases():
    assert pipeline.job_identity(
        "https://boards.greenhouse.io/co/jobs/123?utm_source=x"
    ) == pipeline.job_identity("https://job-boards.greenhouse.io/co/jobs/123")
    assert pipeline.job_identity("https://jobs.lever.co/co/abc/apply") == pipeline.job_identity(
        "https://jobs.lever.co/co/abc"
    )


def test_company_urls_require_search_provenance(case, search_brief):
    from jobengine.search.discovery import discover_companies

    case[0].search_brief = confirm_search_brief(case[0], search_brief)

    class Client:
        def search(self, *args, **kwargs):
            return {
                "text": "Synthetic company research",
                "raw": {
                    "output": [
                        {
                            "type": "web_search_call",
                            "action": {
                                "sources": [{"url": "https://real.example/", "title": "Synthetic"}]
                            },
                        }
                    ]
                },
            }

        def structured(self, *args):
            return CompanyOutput(
                companies=[
                    CompanyCandidate(
                        name="Invented",
                        official_url="https://invented.example/",
                        careers_url=None,
                        sector_rationale="Unsupported",
                    )
                ]
            )

    result = discover_companies(Client(), case[0], search_brief.sectors[0])
    assert not result.companies
    assert result.warnings


def test_unconfirmed_sectors_cannot_be_changed_after_approval(case, search_brief):
    from jobengine.onboarding.brief import require_confirmed_search_brief

    profile = case[0]
    profile.search_brief = confirm_search_brief(profile, search_brief)
    profile.search_brief.sectors[0].name = "A different sector"
    with pytest.raises(ValueError, match="changed"):
        require_confirmed_search_brief(profile)


def test_hidden_exclusions_do_not_shift_saved_review_labels(case):
    from jobengine.models import Run

    profile, item, *_ = case
    visible = pipeline.rank_job(*case)
    excluded_item = item.model_copy(update={"url": "https://example.com/excluded"})
    search = SearchRecord(
        query="",
        retrieved_at="2026-11-07",
        summary="",
        sources=[],
        jobs=[excluded_item, item],
        warnings=[],
    )
    run = Run(id="synthetic", model="synthetic", profile=profile, search=search, jobs=[visible])
    assert [i.url for i in run.review_items()] == [item.url]


def test_inspection_cap_stops_a_run_that_finds_no_usable_matches(
    case, search_brief, tmp_path, monkeypatch
):
    client, profile, _ = setup_search(
        case,
        search_brief,
        tmp_path,
        monkeypatch,
        {"Robotics": "excluded", "Industrial software": "excluded"},
    )

    def companies(client, profile, sector, **kwargs):
        values = [
            CompanyCandidate(
                name=f"Synthetic {sector.name} {i}",
                official_url=f"https://{'robot' if sector.name == 'Robotics' else 'industry'}-{i}.example/",
                careers_url=None,
                sector_rationale="Synthetic test",
                sector=sector.name,
            )
            for i in range(2)
        ]
        return SearchRecord(
            query="",
            retrieved_at="2026-11-07",
            summary="",
            sources=[],
            jobs=[],
            warnings=[],
            companies=values,
        )

    def jobs(client, profile, **kwargs):
        c, s = kwargs["company"], kwargs["sector"]
        items = [
            DiscoveryItem(
                title="Synthetic role",
                company=c.name,
                url=c.official_url + f"job/{i}",
                sector=s.name,
            )
            for i in range(6)
        ]
        return SearchRecord(
            query="", retrieved_at="2026-11-07", summary="", sources=[], jobs=items, warnings=[]
        )

    monkeypatch.setattr(pipeline, "discover_companies", companies)
    monkeypatch.setattr(pipeline, "discover", jobs)
    run = pipeline.run_search(client, profile)
    assert len(run.inspected_jobs) == 20
    assert not run.jobs
    assert run.search.stop_reason == "inspection_limit"


def test_sector_proposal_orders_any_field_by_declared_priority(case):
    from jobengine.models import SectorProposal
    from jobengine.onboarding.brief import propose_sectors

    class Client:
        def structured(self, *args):
            return SectorProposal(
                sectors=[
                    SectorTarget(
                        name="Communications",
                        relationship="expanded",
                        rationale="Broad related work",
                    ),
                    SectorTarget(
                        name="Publishing",
                        relationship="preferred",
                        rationale="Explicit industry interest",
                    ),
                ]
            )

    result = propose_sectors(Client(), case[0])
    assert [s.name for s in result.sectors] == ["Publishing", "Communications"]
    assert result.sectors[-1].relationship == "expanded"


def test_zero_experience_search_does_not_keep_obviously_senior_candidates(case, search_brief):
    from jobengine.models import DiscoveryOutput
    from jobengine.search.discovery import discover

    profile = case[0]
    profile.search_brief = confirm_search_brief(profile, search_brief)
    company = CompanyCandidate(
        name="Synthetic",
        official_url="https://example.com",
        careers_url=None,
        sector_rationale="Synthetic test",
    )

    class Client:
        def search(self, *args, **kwargs):
            return {
                "text": "Synthetic senior result",
                "raw": {
                    "output": [
                        {
                            "type": "web_search_call",
                            "action": {
                                "sources": [
                                    {
                                        "url": "https://example.com/staff",
                                        "title": "Staff Software Engineer",
                                    }
                                ]
                            },
                        }
                    ]
                },
            }

        def structured(self, *args):
            return DiscoveryOutput(
                jobs=[
                    DiscoveryItem(
                        title="Staff Software Engineer, AI",
                        company="Synthetic",
                        url="https://example.com/staff",
                    )
                ]
            )

    result = discover(Client(), profile, company=company, sector=search_brief.sectors[0])
    assert not result.jobs
    assert any("senior" in warning.casefold() for warning in result.warnings)


def test_unsupported_homepage_can_use_an_actual_sourced_company_subpage(case, search_brief):
    from jobengine.search.discovery import discover_companies

    case[0].search_brief = confirm_search_brief(case[0], search_brief)

    class Client:
        def search(self, *args, **kwargs):
            return {
                "text": "Synthetic company source",
                "raw": {
                    "output": [
                        {
                            "type": "web_search_call",
                            "action": {
                                "sources": [
                                    {"url": "https://real.example/about", "title": "Synthetic"}
                                ]
                            },
                        }
                    ]
                },
            }

        def structured(self, *args):
            return CompanyOutput(
                companies=[
                    CompanyCandidate(
                        name="Synthetic",
                        official_url="https://www.real.example/",
                        careers_url=None,
                        sector_rationale="Synthetic test",
                    )
                ]
            )

    result = discover_companies(Client(), case[0], search_brief.sectors[0])
    assert result.companies[0].official_url == "https://real.example/about"


@pytest.mark.parametrize("stop_after", ["companies", "jobs", "evaluate"])
def test_time_budget_stops_new_work_and_retains_completed_results(
    case, search_brief, tmp_path, monkeypatch, stop_after
):
    from types import SimpleNamespace

    client, profile, calls = setup_search(
        case,
        search_brief,
        tmp_path,
        monkeypatch,
        {"Robotics": "weaker", "Industrial software": "strong"},
    )
    clock = [100.0]
    monkeypatch.setattr(pipeline, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    method = {"companies": "discover_companies", "jobs": "discover", "evaluate": "evaluate"}[
        stop_after
    ]
    original = getattr(pipeline, method)

    def expire(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += 30
        return result

    monkeypatch.setattr(pipeline, method, expire)
    run = pipeline.run_search(client, profile, limit=2, max_minutes=0.5)
    assert run.search.stop_reason == "time_limit"
    assert all("Industrial" not in detail for _, detail in calls)
    expected = {"companies": 1, "jobs": 2, "evaluate": 3}[stop_after]
    assert len(calls) == expected
    assert len(run.inspected_jobs) == (1 if stop_after == "evaluate" else 0)
    assert client.store.load_run(run.id)["search"]["stop_reason"] == "time_limit"
    assert any("completed results are retained" in warning for warning in run.search.warnings)


@pytest.mark.parametrize("minutes", [0, -1, float("inf"), float("nan")])
def test_invalid_search_time_budget_rejected_before_research(case, minutes):
    with pytest.raises(ValueError, match="positive, finite"):
        pipeline.run_search(None, case[0], max_minutes=minutes)
