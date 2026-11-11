"""Adapter checks use synthetic posts and mocked HTTP only; never provider requests."""

import hashlib
from datetime import datetime
from html import escape

import httpx
import pytest

from jobengine.models import DiscoveryItem, JobPage
from jobengine.search.greenhouse import GreenhouseJob, fetch_board_jobs
from jobengine.search.greenhouse_adapter import adapt_posting

DESCRIPTION = """<header><h2>Responsibilities</h2></header>
<main><p>Write and edit accurate stories for the local community.</p>
<ul><li>Research complex topics and verify claims using primary sources.</li>
<li>Collaborate with editors and incorporate constructive feedback.</li></ul></main>
<article><h2>Qualifications</h2><p>Strong writing and research skills are required.</p></article>
<footer><h2>How to apply</h2><p>Submit a resume, cover letter, and three published writing samples.</p></footer>
<script>SHOULD NOT APPEAR</script><style>SHOULD NOT APPEAR</style>"""


def posting(**changes):
    return GreenhouseJob(
        **(
            {
                "board_token": "synthetic",
                "id": 123,
                "internal_job_id": 456,
                "title": "Synthetic editor",
                "location": "New York, NY",
                "url": "https://job-boards.greenhouse.io/synthetic/jobs/123?gh_src=test",
                "description": DESCRIPTION,
            }
            | changes
        )
    )


def test_success_preserves_fields_source_timestamp_and_hash_without_io(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Adapter must not make HTTP requests or model calls")

    from jobengine.infrastructure.llm import ModelClient

    monkeypatch.setattr(httpx.Client, "send", unexpected)
    monkeypatch.setattr(ModelClient, "structured", unexpected)
    monkeypatch.setattr(ModelClient, "search", unexpected)
    job = posting()
    timestamp = "2026-11-10T15:00:00+00:00"
    item, page = adapt_posting(
        job, employer="Synthetic News", sector="News", retrieved_at=timestamp
    )
    assert item.title == job.title and item.company == "Synthetic News"
    assert item.url == page.url == page.requested_url == job.url
    assert item.sector == "News"
    assert (
        item.company_source_url
        == "https://boards-api.greenhouse.io/v1/boards/synthetic/jobs?content=true"
    )
    assert "Location: New York, NY" in page.text
    assert "posting_id=123" in page.extraction_method
    assert "internal_job_id=456" in page.extraction_method
    assert "board_token=synthetic" in page.extraction_method
    assert page.retrieved_at == timestamp
    assert page.status == "retrieved" and page.error is None
    assert page.content_hash == hashlib.sha256(page.text.encode()).hexdigest()
    assert DiscoveryItem.model_validate(item.model_dump()) == item
    assert JobPage.model_validate(page.model_dump()) == page
    assert job.description == DESCRIPTION  # Source content stays untouched.


@pytest.mark.parametrize("html", [DESCRIPTION, escape(DESCRIPTION), escape(escape(DESCRIPTION))])
def test_html_sections_lists_and_application_requirements_survive(html):
    _, page = adapt_posting(posting(description=html), employer="Synthetic News")
    assert page.status == "retrieved"
    for text in [
        "Responsibilities",
        "Research complex topics",
        "Qualifications",
        "How to apply",
        "three published writing samples",
    ]:
        assert text in page.text
    assert "SHOULD NOT APPEAR" not in page.text
    assert "<li>" not in page.text and "&lt;" not in page.text


def test_optional_metadata_can_be_missing_without_inventing_values():
    item, page = adapt_posting(
        posting(internal_job_id=None, location=""), employer="Synthetic News"
    )
    assert item.sector is None
    assert "internal_job_id=" not in page.extraction_method
    assert page.status == "retrieved"
    assert datetime.fromisoformat(page.retrieved_at).tzinfo is not None
    assert "Location: \n" in page.text


@pytest.mark.parametrize(
    "description",
    [None, "", " ", "<script>not a job description</script>", "<p>Too short</p>", "a" * 90001],
)
def test_missing_or_unusable_description_is_a_failed_page_not_metadata_only_success(description):
    item, page = adapt_posting(posting(description=description), employer="Synthetic News")
    assert item.url == page.requested_url
    assert page.status == "failed" and page.error
    assert page.text == page.content_hash == ""
    assert "posting_id=123" in page.extraction_method


def test_malformed_html_is_tolerated_without_discarding_text():
    html = (
        "<div><h2>Qualifications<p>"
        + "Research and writing experience. " * 15
        + "<ul><li>Send a portfolio"
    )
    _, page = adapt_posting(posting(description=html), employer="Synthetic News")
    assert page.status == "retrieved"
    assert "Qualifications" in page.text and "Send a portfolio" in page.text


def test_entities_inside_real_html_remain_readable_text():
    html = (
        "<p>Use R&amp;D tools, compare x &lt; y, and explain &lt;script&gt; tags.</p>" + DESCRIPTION
    )
    _, page = adapt_posting(posting(description=html), employer="Synthetic News")
    assert "R&D" in page.text and "x < y" in page.text and "<script> tags" in page.text


@pytest.mark.parametrize("employer", [None, "", "  "])
def test_employer_is_explicit_not_inferred_from_board_token(employer):
    with pytest.raises(ValueError, match="employer"):
        adapt_posting(posting(), employer=employer)


def test_mocked_connector_output_can_be_adapted_directly():
    payload = {
        "jobs": [
            {
                "id": 123,
                "title": "Synthetic editor",
                "location": {"name": "NYC"},
                "absolute_url": "https://example.com/careers?gh_jid=123",
                "content": escape(DESCRIPTION),
            }
        ]
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        job = fetch_board_jobs("synthetic", client=client)[0]
    item, page = adapt_posting(job, employer="Synthetic News")
    assert page.status == "retrieved" and "Qualifications" in page.text
    assert "posting_id=123" in page.extraction_method
    assert item.url == "https://example.com/careers?gh_jid=123"
