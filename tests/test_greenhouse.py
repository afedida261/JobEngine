"""Greenhouse connector checks use HTTPX's in-memory transport; no network or models."""

import httpx
import pytest

from jobengine.search.greenhouse import GreenhouseError, GreenhouseJob, fetch_board_jobs


def posting(**changes):
    return {
        "id": 123,
        "internal_job_id": 456,
        "title": "Synthetic editor",
        "location": {"name": "New York, NY"},
        "absolute_url": "https://job-boards.greenhouse.io/synthetic/jobs/123?gh_src=test",
        "content": "&lt;p&gt;Synthetic description with <strong>original HTML</strong>.&lt;/p&gt;",
        "departments": [{"id": 1, "name": "Editorial"}],
    } | changes


def test_success_preserves_posting_fields_and_uses_only_public_get():
    original = posting()
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert (
            str(request.url)
            == "https://boards-api.greenhouse.io/v1/boards/synthetic/jobs?content=true"
        )
        assert "authorization" not in request.headers
        assert request.extensions["timeout"]["read"] == 20
        return httpx.Response(200, json={"jobs": [original], "meta": {"total": 1}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        jobs = fetch_board_jobs("synthetic", client=client)
        assert not client.is_closed
    assert len(requests) == 1
    assert jobs[0].model_dump() == {
        "board_token": "synthetic",
        "id": 123,
        "internal_job_id": 456,
        "title": original["title"],
        "location": original["location"]["name"],
        "url": original["absolute_url"],
        "description": original["content"],
    }
    assert GreenhouseJob.model_validate(jobs[0].model_dump()) == jobs[0]


def test_empty_board_is_a_successful_empty_list():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"jobs": [], "meta": {"total": 0}})
        )
    ) as client:
        assert fetch_board_jobs("synthetic", client=client) == []


@pytest.mark.parametrize("content", [None, ""])
def test_unavailable_descriptions_and_prospect_posts_are_preserved(content):
    item = posting(internal_job_id=None)
    if content is None:
        del item["content"]
    else:
        item["content"] = content
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"jobs": [item]}))
    ) as client:
        job = fetch_board_jobs("synthetic", client=client)[0]
    assert job.description == content
    assert job.internal_job_id is None


@pytest.mark.parametrize("status", [302, 404, 429, 500])
def test_http_errors_are_not_mistaken_for_empty_boards(status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://example.com/redirect"})

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        with pytest.raises(GreenhouseError, match=f"HTTP {status}"):
            fetch_board_jobs("synthetic", client=client)
    assert len(requests) == 1  # Neither retries nor redirects.


@pytest.mark.parametrize(
    "exception, message",
    [
        (httpx.ReadTimeout, "timed out"),
        (httpx.ConnectTimeout, "timed out"),
        (httpx.ConnectError, "request failed"),
    ],
)
def test_transport_failures_are_explicit(exception, message):
    def respond(request):
        raise exception("Synthetic transport failure", request=request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(GreenhouseError, match=message):
            fetch_board_jobs("synthetic", client=client)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"jobs": None},
        {"jobs": {}},
        {"jobs": [None]},
        {"jobs": [posting(id="123")]},
        {"jobs": [posting(title=None)]},
        {"jobs": [posting(location=None)]},
        {"jobs": [posting(location={})]},
        {"jobs": [posting(absolute_url="file:///invalid")]},
        {"jobs": [posting(content={"invalid": "description"})]},
        {"jobs": [posting(), posting(id=None)]},
    ],
)
def test_malformed_responses_fail_the_whole_board_instead_of_silently_dropping_jobs(payload):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(GreenhouseError, match="malformed response"):
            fetch_board_jobs("synthetic", client=client)


def test_invalid_json_is_explicit():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=b'{"jobs":', headers={"content-type": "application/json"}
            )
        )
    ) as client:
        with pytest.raises(GreenhouseError, match="malformed response"):
            fetch_board_jobs("synthetic", client=client)


@pytest.mark.parametrize(
    "token", ["", "../other", "a/b", "token?key=value", "https://example.com", None]
)
def test_invalid_tokens_fail_before_request(token):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected request"))
    ) as client:
        with pytest.raises(ValueError, match="board_token"):
            fetch_board_jobs(token, client=client)
