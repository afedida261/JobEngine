"""Batch tests exercise the real connector/adapter through mocked HTTP responses."""

import httpx
import pytest

from jobengine.models import DiscoveryItem, JobPage
from jobengine.search.greenhouse_batch import discover_boards


def posting(token, identity=1, **changes):
    return {
        "id": identity,
        "internal_job_id": identity + 100,
        "title": "Synthetic senior editor",
        "location": {"name": "California"},
        "absolute_url": f"https://job-boards.greenhouse.io/{token}/jobs/{identity}",
        "content": "<p>"
        + "Synthetic responsibilities, qualifications and application instructions. " * 8
        + "</p>",
    } | changes


def test_multiple_boards_keep_all_titles_locations_descriptions_and_make_no_model_calls(
    monkeypatch,
):
    from jobengine.infrastructure.llm import ModelClient

    monkeypatch.setattr(ModelClient, "structured", lambda *a, **k: pytest.fail("No model calls"))
    monkeypatch.setattr(ModelClient, "search", lambda *a, **k: pytest.fail("No model calls"))
    requests = []

    def respond(request):
        token = request.url.path.split("/")[-2]
        requests.append(token)
        assert request.method == "GET" and request.url.params["content"] == "true"
        return httpx.Response(200, json={"jobs": [posting(token, 1), posting(token, 2)]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        candidates, summaries = discover_boards(
            {"alpha": "Alpha News", "beta": "Beta News"}, client=client
        )
        assert not client.is_closed
    assert requests == ["alpha", "beta"]  # No per-posting retrieval.
    assert len(candidates) == 4
    assert [s["fetched"] for s in summaries] == [2, 2]
    assert [s["converted"] for s in summaries] == [2, 2]
    assert all(s["failed"] == s["duplicated"] == 0 and s["status"] == "ok" for s in summaries)
    for item, page in candidates:
        DiscoveryItem.model_validate(item.model_dump())
        JobPage.model_validate(page.model_dump())
        assert page.status == "retrieved" and "application instructions" in page.text
        assert "California" in page.text and "senior editor" in item.title
        assert page.requested_url == item.url


def test_canonical_urls_and_greenhouse_host_aliases_deduplicate_and_retain_every_source():
    responses = {
        "alpha": [
            posting("alpha", 1),
            posting(
                "alpha", 1, absolute_url="https://boards.greenhouse.io/alpha/jobs/1?utm_source=test"
            ),
        ],
        "beta": [
            posting(
                "beta", 99, absolute_url="https://job-boards.greenhouse.io/alpha/jobs/1?gh_src=test"
            ),
            posting("beta", 2),
        ],
    }
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"jobs": responses[r.url.path.split("/")[-2]]})
        )
    ) as client:
        candidates, summaries = discover_boards({"alpha": "Alpha", "beta": "Beta"}, client=client)
    assert len(candidates) == 2
    assert [s["duplicated"] for s in summaries] == [1, 1]
    assert [s["converted"] for s in summaries] == [1, 1]
    duplicate = summaries[1]["postings"][0]
    assert duplicate["posting_id"] == 99 and duplicate["board_token"] == "beta"
    assert duplicate["original_url"].endswith("?gh_src=test")
    assert duplicate["identity"] == "greenhouse:alpha:1"
    assert candidates[0][0].url.endswith("/alpha/jobs/1")
    assert "board_token=alpha" in candidates[0][1].extraction_method


def test_empty_board_and_empty_input():
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"jobs": []}))
    ) as client:
        assert discover_boards({}, client=client) == ([], [])
        candidates, summaries = discover_boards({"empty": "Empty"}, client=client)
    assert candidates == []
    assert summaries[0]["status"] == "empty"
    assert all(summaries[0][key] == 0 for key in ("fetched", "converted", "duplicated", "failed"))


@pytest.mark.parametrize("problem", ["http", "timeout", "malformed"])
def test_one_failed_board_does_not_prevent_later_boards(problem):
    def respond(request):
        token = request.url.path.split("/")[-2]
        if token == "bad":
            if problem == "timeout":
                raise httpx.ReadTimeout("Synthetic timeout", request=request)
            return (
                httpx.Response(503)
                if problem == "http"
                else httpx.Response(200, json={"jobs": None})
            )
        return httpx.Response(200, json={"jobs": [posting(token)]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        candidates, summaries = discover_boards(
            {"before": "Before", "bad": "Bad", "after": "After"}, client=client
        )
    assert len(candidates) == 2
    failure = summaries[1]
    assert failure["status"] == "failed" and failure["error"]
    assert failure["fetched"] is None and failure["failed"] is None
    assert summaries[2]["converted"] == 1


def test_failed_descriptions_remain_candidates_and_counts_partition_fetched_posts():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"jobs": [posting("alpha", 1, content=None), posting("alpha", 2)]}
            )
        )
    ) as client:
        candidates, summaries = discover_boards({"alpha": "Alpha"}, client=client)
    assert len(candidates) == 2
    assert candidates[0][1].status == "failed" and candidates[0][1].error
    assert candidates[1][1].status == "retrieved"
    summary = summaries[0]
    assert summary["fetched"] == 2 and summary["converted"] == summary["failed"] == 1
    assert summary["status"] == "partial"
    assert summary["postings"][0]["error"]


def test_invalid_board_configuration_is_reported_without_request_and_other_boards_continue():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"jobs": []})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        _, summaries = discover_boards(
            {"bad/token": "Bad", "unnamed": "", "valid": "Valid"}, client=client
        )
    assert len(requests) == 1
    assert [s["status"] for s in summaries] == ["failed", "failed", "empty"]
