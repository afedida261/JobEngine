"""Dynamic discovery uses fake search responses and the real connector over mocked HTTP."""

from types import SimpleNamespace

import httpx
import pytest

from jobengine.infrastructure.llm import SEARCH_OPERATIONS
from jobengine.infrastructure.storage import BudgetExceeded
from jobengine.onboarding.brief import confirm_search_brief
from jobengine.search.greenhouse_batch import discover_boards
from jobengine.search.greenhouse_discovery import (
    EmployerLabels,
    board_token_from_url,
    discover_greenhouse_boards,
)


def source(token, employer, *, host="job-boards.greenhouse.io", path=None):
    return {"url": f"https://{host}/{path or token}", "title": f"Jobs at {employer}"}


class FakeSearch:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.searches = []
        self.normalizations = []
        self.labels = None

    def search(self, prompt, *, operation, max_tool_calls):
        assert operation == "greenhouse_board_discovery" and max_tool_calls == 1
        self.searches.append(prompt)
        batch = next(self.batches)
        if isinstance(batch, Exception):
            raise batch
        return {
            "text": "Search summary is not trusted URL or employer evidence.",
            "raw": {
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {
                            "queries": ["site:job-boards.greenhouse.io applicant work"],
                            "sources": batch,
                        },
                    }
                ],
            },
        }

    def structured(self, operation, instructions, data, schema):
        assert operation == "normalize_greenhouse_employers" and schema is EmployerLabels
        self.normalizations.append(data)
        if isinstance(self.labels, Exception):
            raise self.labels
        return EmployerLabels.model_validate(
            {
                "labels": self.labels
                if self.labels is not None
                else [
                    {
                        "source_url": s["url"],
                        "employer": s["title"].removeprefix("Jobs at "),
                        "employer_quote": s["title"],
                    }
                    for s in data["sources"]
                ]
            }
        )


@pytest.fixture
def approved(case, search_brief):
    profile = case[0].model_copy(deep=True)
    profile.search_brief = confirm_search_brief(profile, search_brief)
    return profile


@pytest.fixture
def api():
    requests = []
    state = SimpleNamespace(responses={})

    def respond(request):
        token = request.url.path.split("/")[-2]
        requests.append(token)
        assert request.url.host == "boards-api.greenhouse.io"
        assert request.method == "GET" and request.url.params["content"] == "true"
        response = state.responses.get(token, {"jobs": []})
        if isinstance(response, Exception):
            raise response
        if isinstance(response, int):
            return httpx.Response(response)
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        yield client, requests, state


@pytest.mark.parametrize(
    "url,token",
    [
        ("https://boards.greenhouse.io/newsroom", "newsroom"),
        ("https://job-boards.greenhouse.io/clinic/jobs/123?gh_src=abc#apply", "clinic"),
        ("https://job-boards.greenhouse.io/Shop_Labs-2/", "Shop_Labs-2"),
        ("https://boards.greenhouse.io/embed/job_board?for=culinary&b=1", "culinary"),
        ("https://boards.greenhouse.io/embed/job_app?for=trades&token=123", "trades"),
    ],
)
def test_tokens_only_from_explicit_greenhouse_url_shapes(url, token):
    assert board_token_from_url(url) == token


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.example/jobs?gh_jid=123",
        "https://greenhouse.io/acme",
        "https://job-boards.greenhouse.io.attacker.example/acme",
        "https://attacker.example/?url=https://boards.greenhouse.io/acme",
        "https://boards.greenhouse.io/",
        "https://boards.greenhouse.io/jobs",
        "https://boards.greenhouse.io/acme/jobs/not-a-number",
        "https://boards.greenhouse.io/acme/jobs/0",
        "https://boards.greenhouse.io/bad.token",
        "https://boards.greenhouse.io/bad%2Ftoken",
        "https://boards.greenhouse.io/acme/../other",
        "https://boards.greenhouse.io/embed/job_board?for=bad.token",
        "https://boards.greenhouse.io/embed/job_board?for=one&for=two",
        "https://name:password@boards.greenhouse.io/acme",
        "https://boards.greenhouse.io:9999/acme",
        "javascript:boards.greenhouse.io/acme",
    ],
)
def test_reject_invalid_tokens_unrelated_urls_and_lookalike_hosts(url):
    with pytest.raises(ValueError):
        board_token_from_url(url)


@pytest.mark.parametrize(
    "role,interest,sector,employer,token",
    [
        ("Reporter", "Public-interest journalism", "Media", "Harbor News", "harbornews"),
        (
            "Physical therapist",
            "Sports rehabilitation",
            "Healthcare",
            "Valley Clinic",
            "valleyclinic",
        ),
        ("Electrician", "Renewable energy", "Skilled trades", "Solar Works", "solarworks"),
        ("Pastry chef", "Artisan baking", "Hospitality", "Morning Bakery", "morningbakery"),
    ],
)
def test_applicant_led_search_across_industries(
    case, search_brief, api, role, interest, sector, employer, token
):
    from jobengine.models import Constraint, SectorTarget

    profile = case[0].model_copy(deep=True)
    profile.target_roles, profile.interests = [role], interest
    profile.constraints = [
        Constraint(
            id="C1",
            field="Location / remote / relocation",
            value="Boston only; no relocation",
            hard=True,
        )
    ]
    draft = search_brief.model_copy(deep=True)
    draft.target_roles, draft.search_queries, draft.work_themes = (
        [role],
        [f"{role} Boston"],
        [interest],
    )
    draft.sectors = [SectorTarget(name=sector, relationship="preferred", rationale=interest)]
    profile.search_brief = confirm_search_brief(profile, draft)
    fake = FakeSearch([[source(token, employer)]])
    http, requests, _ = api
    result = discover_greenhouse_boards(fake, profile, max_search_requests=1, http_client=http)
    assert result.boards == {token: employer}
    assert requests == [token]
    assert role in fake.searches[0] and interest in fake.searches[0] and sector in fake.searches[0]
    assert "Boston only; no relocation" in fake.searches[0]
    assert "SYNTHETIC TEST CANDIDATE" not in fake.searches[0]  # No raw resume.
    assert result.candidates[token]["job_count"] == 0  # Empty is verified, not failed.
    assert "normalization" not in result.candidates[token]
    # Direct compatibility with the existing collector; no pipeline edits needed.
    assert discover_boards(result.boards, client=http)[1][0]["employer"] == employer


def test_dedup_preserves_every_url_and_employer_label_without_company_quota(approved, api):
    first = [source(f"org{i}", f"Organization {i}") for i in range(7)]
    duplicate = source(
        "org0", "Organization Zero", host="boards.greenhouse.io", path="org0/jobs/10"
    )
    fake = FakeSearch([first, [duplicate, first[0]]])
    http, requests, _ = api
    result = discover_greenhouse_boards(fake, approved, max_search_requests=2, http_client=http)
    assert len(result.boards) == 7  # A single sector is not capped at two companies.
    assert requests == [f"org{i}" for i in range(7)]
    assert len(result.candidates["org0"]["sources"]) == 2
    assert {c["employer"] for c in result.candidates["org0"]["employer_labels"]} == {
        "Organization 0",
        "Organization Zero",
    }
    assert result.boards["org0"] == "Organization 0"
    assert any("multiple observed employer labels" in w for w in result.warnings)
    assert result.search_requests == result.normalization_requests == 2
    assert '"already_seen_boards": ["org0", "org1"' in fake.searches[1]


def test_unrelated_results_invalid_tokens_and_api_failures_remain_explicit(approved, api):
    fake = FakeSearch(
        [
            [
                source("good", "Good Clinic"),
                source("gone", "Gone Clinic"),
                source("broken", "Broken Clinic"),
                source("timeout", "Timeout Clinic"),
                source("bad.token", "Bad Clinic"),
                {"url": "https://example.com/guide", "title": "Greenhouse guide"},
            ]
        ]
    )
    http, requests, state = api
    state.responses = {
        "gone": 404,
        "broken": {"unexpected": []},
        "timeout": httpx.ReadTimeout("offline"),
    }
    result = discover_greenhouse_boards(fake, approved, max_search_requests=1, http_client=http)
    assert result.boards == {"good": "Good Clinic"}
    assert requests == ["good", "gone", "broken", "timeout"]
    assert len(result.rejected_sources) == 2
    assert all(result.candidates[t]["status"] == "failed" for t in ["gone", "broken", "timeout"])
    assert "HTTP 404" in result.candidates["gone"]["error"]
    assert "malformed" in result.candidates["broken"]["error"]
    assert "timed out" in result.candidates["timeout"]["error"]


def test_search_and_board_caps_include_failures_and_keep_deferred_sources(approved, api):
    fake = FakeSearch([[source(f"org{i}", f"Organization {i}") for i in range(5)]])
    http, requests, state = api
    state.responses = {"org0": 404}
    result = discover_greenhouse_boards(
        fake, approved, max_search_requests=4, max_boards=2, http_client=http
    )
    assert len(fake.searches) == 1 and requests == ["org0", "org1"]
    assert result.boards == {"org1": "Organization 1"}
    assert result.boards_inspected == 2 and len(result.candidates) == 5
    assert result.candidates["org4"]["status"] == "deferred"
    assert result.stop_reason == "board_inspection_limit"
    assert result.limits == {
        "search_requests": 4,
        "web_tool_calls_per_request": 1,
        "board_inspections": 2,
    }


def test_unrelated_greenhouse_source_omitted_by_normalizer_is_not_a_verified_employer(
    approved, api
):
    fake = FakeSearch([[source("unrelated", "Unrelated Employer")]])
    fake.labels = []  # A real normalizer receives the approved brief and can omit unrelated leads.
    http, requests, _ = api
    result = discover_greenhouse_boards(fake, approved, max_search_requests=1, http_client=http)
    assert requests == ["unrelated"]
    assert not result.boards
    assert result.candidates["unrelated"]["status"] == "verified"
    assert result.candidates["unrelated"]["employer_labels"] == []


def test_search_failures_count_against_limit_and_do_not_erase_partial_results(approved, api):
    fake = FakeSearch([RuntimeError("private provider response"), [source("clinic", "Clinic")], []])
    http, requests, _ = api
    result = discover_greenhouse_boards(fake, approved, max_search_requests=3, http_client=http)
    assert result.search_requests == 3 and requests == ["clinic"]
    assert result.boards == {"clinic": "Clinic"}
    assert result.searches[0]["error"] == "Web discovery failed (RuntimeError)."
    assert "private provider response" not in str(result)
    assert "No supported" in result.searches[2]["warning"]


@pytest.mark.parametrize(
    "labels",
    [
        [
            {
                "source_url": "https://job-boards.greenhouse.io/invented",
                "employer": "Invented",
                "employer_quote": "Invented",
            }
        ],
        [
            {
                "source_url": "https://job-boards.greenhouse.io/actual",
                "employer": "Invented",
                "employer_quote": "Jobs at Actual",
            }
        ],
        [
            {
                "source_url": "https://job-boards.greenhouse.io/actual",
                "employer": "Actual",
                "employer_quote": "A fabricated passage",
            }
        ],
        ValueError("bad model output"),
    ],
)
def test_hallucinated_tokens_and_employer_names_never_enter_mapping(approved, api, labels):
    fake = FakeSearch([[source("actual", "Actual")]])
    fake.labels = labels
    http, requests, _ = api
    result = discover_greenhouse_boards(fake, approved, max_search_requests=1, http_client=http)
    assert requests == ["actual"] and result.boards == {}
    assert result.candidates["actual"]["status"] == "verified"
    assert any("no source-backed employer label" in w for w in result.warnings)


def test_budget_stop_and_stale_approval_do_not_trigger_more_calls(approved, api):
    fake = FakeSearch([BudgetExceeded("budget")])
    http, requests, _ = api
    result = discover_greenhouse_boards(fake, approved, http_client=http)
    assert result.stop_reason == "budget_limit" and result.search_requests == 1
    assert not requests
    approved.interests += " changed"
    with pytest.raises(ValueError, match="changed"):
        discover_greenhouse_boards(fake, approved, http_client=http)
    assert len(fake.searches) == 1


@pytest.mark.parametrize(
    "argument,value",
    [
        ("max_search_requests", 0),
        ("max_search_requests", 9),
        ("max_search_requests", True),
        ("max_boards", 0),
        ("max_boards", 41),
        ("max_boards", 1.5),
    ],
)
def test_limits_validated_before_io(approved, argument, value):
    with pytest.raises(ValueError, match=argument):
        discover_greenhouse_boards(None, approved, **{argument: value})


def test_new_operations_use_existing_search_model_configuration():
    assert {"greenhouse_board_discovery", "normalize_greenhouse_employers"} <= SEARCH_OPERATIONS
