"""Embedding requests and semantic ranking use mocked OpenAI HTTP, never paid calls."""

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from jobengine.infrastructure.embeddings import DIMENSIONS, MAX_INPUT_BYTES, MODEL, EmbeddingClient
from jobengine.infrastructure.storage import Store
from jobengine.models import DiscoveryItem, JobPage, SearchBriefDraft
from jobengine.onboarding.brief import confirm_search_brief
from jobengine.search.semantic import (
    applicant_text,
    cosine_similarity,
    job_text,
    prioritize_semantically,
)


def vector(axis=0):
    result = [0.0] * DIMENSIONS
    result[axis] = 1.0
    return result


@pytest.fixture
def approved(case, search_brief):
    profile = case[0].model_copy(deep=True)
    profile.search_brief = confirm_search_brief(profile, search_brief)
    return profile


@pytest.fixture
def embedding_factory(tmp_path):
    clients = []

    def make(handler=None, batch_size=32):
        requests = []
        state = SimpleNamespace(handler=handler)

        def respond(request):
            assert request.url.path == "/v1/embeddings"
            body = json.loads(request.content)
            requests.append(body)
            assert body["model"] == MODEL and body["dimensions"] == DIMENSIONS
            assert body["encoding_format"] == "float"
            assert isinstance(body["input"], list)
            if state.handler:
                return state.handler(body, len(requests))
            data = [
                {
                    "object": "embedding",
                    "index": i,
                    "embedding": vector(1 if "Unrelated" in text else 0),
                }
                for i, text in enumerate(body["input"])
            ]
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": MODEL,
                    "data": list(reversed(data)),
                    "usage": {"prompt_tokens": 7, "total_tokens": 7},
                },
            )

        sdk = OpenAI(
            api_key="offline-fake-key",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        )
        clients.append(sdk)
        store = Store(tmp_path / str(len(clients)))
        return EmbeddingClient(store, client=sdk, batch_size=batch_size), requests, store, state

    yield make
    for client in clients:
        client.close()


def pair(title, identity):
    item = DiscoveryItem(title=title, company="Synthetic", url=f"https://example.com/{identity}")
    text = f"Location: Unknown\n\nJOB DESCRIPTION\nResponsibilities\nBuild and test reliable systems for users. {title}"
    return item, JobPage(
        requested_url=item.url,
        url=item.url,
        retrieved_at="2026-11-10T00:00:00+00:00",
        text=text,
        content_hash="intentionally-stale-hash",
        extraction_method="synthetic",
        status="retrieved",
    )


def test_batched_requests_usage_cache_and_response_indices(embedding_factory):
    embeddings, requests, store, _ = embedding_factory(batch_size=2)
    texts = ["Relevant original input", "Unrelated original input", "Third original input"]
    vectors, usage = embeddings.embed(texts, namespace="jobs", purpose="validation")
    assert [len(r["input"]) for r in requests] == [2, 1]
    assert vectors[0] == vector(0) and vectors[1] == vector(1)
    assert usage["requests"] == 2 and usage["input_tokens"] == 14
    assert usage["estimated_usd"] == pytest.approx(14 * 0.02 / 1_000_000)
    assert usage["unsettled_reserved_usd"] == 0
    assert len(store.usage()["calls"]) == 2
    assert all(
        c["state"] == "estimated" and c["operation"].startswith("semantic_embedding:validation")
        for c in store.usage()["calls"]
    )
    again, cached = embeddings.embed(texts, namespace="jobs")
    assert again == vectors and cached["requests"] == 0 and cached["cache_hits"] == 3
    assert cached["input_tokens"] == cached["estimated_usd"] == 0


def test_ranking_preserves_pool_and_job_cache_is_shared_across_applicants(
    embedding_factory, approved
):
    embeddings, requests, _, _ = embedding_factory()
    pool = [pair("Unrelated", 1), pair("Relevant", 2)]
    before = approved.model_dump()
    selected, ranked, report = prioritize_semantically(pool, approved, embeddings, top_n=1)
    assert selected == [pool[1]]
    assert len(ranked) == 2 and ranked[0]["similarity"] == 1 and ranked[1]["similarity"] == 0
    assert ranked[1]["status"] == "not_selected_for_batch"
    assert ranked[0]["page"] is pool[1][1]
    assert report["requests"] == 2
    assert approved.model_dump() == before
    draft = SearchBriefDraft.model_validate(
        approved.search_brief.model_dump(include=set(SearchBriefDraft.model_fields))
    )
    approved.interests += " and accessibility"
    approved.search_brief = confirm_search_brief(approved, draft)
    _, _, again = prioritize_semantically(pool, approved, embeddings)
    assert (
        len(requests) == 3 and again["cache_hits"] == 2
    )  # Only the new applicant query was embedded.
    assert "Desired roles:" in requests[0]["input"][0]
    assert pool[0][0].company not in requests[1]["input"][0]


def test_cache_invalidates_on_content_title_or_version_change(
    embedding_factory, approved, monkeypatch
):
    from jobengine.infrastructure import embeddings as module

    embeddings, requests, _, _ = embedding_factory()
    pool = [pair("Relevant", 1)]
    prioritize_semantically(pool, approved, embeddings)
    pool[0][1].text += "\nNew responsibilities."
    prioritize_semantically(pool, approved, embeddings)
    assert len(requests) == 3
    pool[0][0].title = "Relevant updated title"
    prioritize_semantically(pool, approved, embeddings)
    assert len(requests) == 4
    monkeypatch.setattr(module, "VERSION", module.VERSION + 1)
    prioritize_semantically(pool, approved, embeddings)
    assert len(requests) == 6


def test_representation_prefers_duties_and_omits_generic_sections(approved):
    item, page = pair("Relevant", 1)
    page.text = (
        "\n\nJOB DESCRIPTION\nAbout the company\n"
        + "GENERICMISSION " * 3000
        + "\nResponsibilities\nImplement endpoints and maintain storage.\nRequirements\nPython skills.\nBenefits\nFREEPARKING"
    )
    text = job_text(item, page)
    assert "Implement endpoints" in text and "Python skills" in text
    assert "GENERICMISSION" not in text and "FREEPARKING" not in text
    assert len(text.encode()) <= MAX_INPUT_BYTES
    approved.documents[0].text += " SECRETRESUMEONLY"
    # Representation itself does not read resumes (approval enforcement stays in scorer).
    # Reapprove unchanged evidence and brief to retain the normal guards.
    draft = SearchBriefDraft.model_validate(
        approved.search_brief.model_dump(include=set(SearchBriefDraft.model_fields))
    )
    approved.search_brief = confirm_search_brief(approved, draft)
    assert "SECRETRESUMEONLY" not in applicant_text(approved)
    assert "Desired work themes:" in applicant_text(approved)
    assert approved.evidence[0].summary in applicant_text(approved)


def test_partial_failures_preserve_cached_and_successful_batches(embedding_factory):
    embeddings, requests, store, state = embedding_factory(batch_size=1)

    def fail_middle(body, call):
        if call == 2:
            return httpx.Response(503, json={"error": {"message": "do not log this provider text"}})
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": MODEL,
                "data": [{"object": "embedding", "index": 0, "embedding": vector()}],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            },
        )

    state.handler = fail_middle
    vectors, usage = embeddings.embed(
        ["First original text", "Second original text", "Third original text"], namespace="jobs"
    )
    assert vectors[0] and vectors[1] is None and vectors[2]
    assert usage["requests"] == 3 and usage["failed_inputs"] == 1
    assert usage["unsettled_reserved_usd"] > 0
    assert "provider text" not in str(usage)
    assert [c["state"] for c in store.usage()["calls"]].count("reserved") == 1
    assert len(requests) == 3


@pytest.mark.parametrize(
    "problem", ["zero", "nan", "wrong_dimension", "duplicate_index", "missing_usage"]
)
def test_malformed_embedding_responses_are_not_scored_or_cached(embedding_factory, problem):
    def respond(body, count):
        values = [0.0] * DIMENSIONS if problem == "zero" else vector()
        if problem == "nan":
            values[0] = float("inf")
            # Valid JSON may carry a string instead of a numeric component.
            values[0] = "nan"
        if problem == "wrong_dimension":
            values = [1, 0]
        data = [{"object": "embedding", "index": 0, "embedding": values}]
        if problem == "duplicate_index":
            data += data
        payload = {"object": "list", "model": MODEL, "data": data}
        if problem != "missing_usage":
            payload["usage"] = {"prompt_tokens": 7, "total_tokens": 7}
        return httpx.Response(200, json=payload)

    embeddings, _, store, _ = embedding_factory(respond)
    vectors, report = embeddings.embed(["Synthetic input text"], namespace="jobs")
    assert vectors == [None] and report["errors"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM cache").fetchone()[0] == 0


def test_applicant_failure_defers_complete_pool_without_job_requests(embedding_factory, approved):
    embeddings, requests, _, _ = embedding_factory(lambda *args: httpx.Response(500))
    pool = [pair("Relevant", 1), pair("Unrelated", 2)]
    selected, ranked, usage = prioritize_semantically(pool, approved, embeddings)
    assert selected == [] and len(ranked) == 2
    assert all(r["similarity"] is None and r["embedding_status"] == "failed" for r in ranked)
    assert len(requests) == 1 and usage["unscored"] == 2


def test_budget_guard_failed_pages_empty_pool_and_invalid_batch_size(
    embedding_factory, approved, monkeypatch
):
    from jobengine.infrastructure.storage import BudgetExceeded

    embeddings, requests, store, _ = embedding_factory()
    assert prioritize_semantically([], approved, embeddings)[0] == []
    failed = pair("Relevant", 1)
    failed[1].status = "failed"
    assert prioritize_semantically([failed], approved, embeddings)[1][0]["similarity"] is None
    assert requests == []

    def stop(*args):
        raise BudgetExceeded("Synthetic budget cap")

    monkeypatch.setattr(store, "reserve", stop)
    _, rows, report = prioritize_semantically([pair("Relevant", 2)], approved, embeddings)
    assert rows[0]["similarity"] is None and requests == []
    assert report["embedding_calls"][0]["errors"][0]["error_type"] == "BudgetExceeded"
    with pytest.raises(ValueError):
        prioritize_semantically([], approved, embeddings, top_n=-1)


def test_cosine_similarity():
    assert cosine_similarity([2, 0], [3, 0]) == 1
    assert cosine_similarity([1, 0], [0, 1]) == 0
    assert cosine_similarity([1, 0], [-1, 0]) == -1
    with pytest.raises(ValueError):
        cosine_similarity([0, 0], [1, 0])


def test_job_batch_failure_keeps_every_pair_and_successful_scores(embedding_factory, approved):
    def respond(body, count):
        if count == 3:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": MODEL,
                "data": [{"object": "embedding", "index": 0, "embedding": vector()}],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            },
        )

    embeddings, requests, _, _ = embedding_factory(respond, batch_size=1)
    pool = [pair("Relevant", 1), pair("Unrelated", 2)]
    selected, rows, report = prioritize_semantically(pool, approved, embeddings, top_n=2)
    assert selected == [pool[0]] and len(rows) == 2
    assert rows[0]["similarity"] == 1 and rows[1]["similarity"] is None
    assert rows[1]["page"] is pool[1][1]
    assert rows[1]["status"] == "not_selected_for_batch"
    assert report["requests"] == 3 and report["unsettled_reserved_usd"] > 0


def test_representation_byte_bounds_unicode_and_single_request_deduplication(embedding_factory):
    from jobengine.infrastructure.embeddings import bounded_text

    text = bounded_text("????????" * 2000)
    assert len(text.encode("utf-8")) <= MAX_INPUT_BYTES
    embeddings, requests, _, _ = embedding_factory()
    vectors, report = embeddings.embed([text, text], namespace="jobs")
    assert len(requests) == 1 and len(requests[0]["input"]) == 1
    assert vectors[0] == vectors[1] and report["failed_inputs"] == 0


def test_approval_guards_run_before_any_embedding_request(embedding_factory, approved):
    embeddings, requests, store, _ = embedding_factory()
    approved.interests += " Changed after approval"
    with pytest.raises(ValueError, match="changed"):
        prioritize_semantically([pair("Relevant", 1)], approved, embeddings)
    assert requests == [] and store.usage()["calls"] == []


def test_hybrid_option_reuses_embeddings_without_additional_requests(embedding_factory, approved):
    embeddings, requests, _, _ = embedding_factory()
    pool = [pair("Relevant", 1), pair("Unrelated", 2)]
    _, original, _ = prioritize_semantically(pool, approved, embeddings)
    request_count = len(requests)
    selected, hybrid, report = prioritize_semantically(pool, approved, embeddings, hybrid=True)
    assert len(requests) == request_count and report["requests"] == 0
    assert report["prioritization"] == "hybrid"
    assert {r["item"].url: r["similarity"] for r in original} == {
        r["item"].url: r["similarity"] for r in hybrid
    }
    assert len(hybrid) == len(pool) and len(selected) == len(pool)
    assert all("adjustments" in r and "final_score" in r for r in hybrid)
