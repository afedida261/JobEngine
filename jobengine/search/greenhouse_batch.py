"""Collect Greenhouse candidates without relevance filtering or model calls."""

from collections.abc import Mapping
from contextlib import nullcontext

import httpx

from jobengine.models import DiscoveryItem, JobPage, utc_now
from jobengine.search.greenhouse import GreenhouseError, GreenhouseJob, fetch_board_jobs
from jobengine.search.greenhouse_adapter import adapt_posting
from jobengine.search.pipeline import job_identity


def discover_boards(
    boards: Mapping[str, str],
    *,
    client: httpx.Client | None = None,
    prefetched_postings: Mapping[str, list[GreenhouseJob]] | None = None,
) -> tuple[list[tuple[DiscoveryItem, JobPage]], list[dict]]:
    """Return unique item/page pairs and per-board summaries, in input order.

    Boards maps tokens to employer display names. First occurrence wins on duplicate
    job identities; every occurrence's original provenance stays in summary.postings.
    Failed pages remain candidates. Counts partition fetched posts into converted
    (usable unique pages), duplicated, and failed (unusable pages/conversion errors).
    On a board request failure, fetched/failed are None because counts are unknown.
    An optional HTTP client remains owned by its caller. No retries or assessments.
    Invocation-local prefetched API responses may be supplied by verified discovery;
    keys present in that mapping (including empty boards) are never fetched again.
    """
    candidates, summaries, seen = [], [], set()
    context = httpx.Client(trust_env=False) if client is None else nullcontext(client)
    with context as connection:
        for token, employer in boards.items():
            summary = {
                "board_token": token,
                "employer": employer,
                "status": "failed",
                "fetched": None,
                "converted": 0,
                "duplicated": 0,
                "failed": None,
                "error": None,
                "postings": [],
            }
            summaries.append(summary)
            try:
                if not isinstance(employer, str) or not employer.strip():
                    raise ValueError("Provide the employer's display name.")
                postings = (
                    prefetched_postings[token]
                    if prefetched_postings is not None and token in prefetched_postings
                    else fetch_board_jobs(token, client=connection)
                )
            except (GreenhouseError, ValueError) as exc:
                summary["error"] = str(exc)
                continue
            timestamp = utc_now()
            summary.update(fetched=len(postings), failed=0)
            for posting in postings:
                source = {
                    "board_token": posting.board_token,
                    "posting_id": posting.id,
                    "internal_job_id": posting.internal_job_id,
                    "original_url": posting.url,
                }
                summary["postings"].append(source)
                try:
                    identity = job_identity(posting.url)
                    source["identity"] = identity
                    if identity in seen:
                        summary["duplicated"] += 1
                        source["status"] = "duplicated"
                        continue
                    item, page = adapt_posting(posting, employer=employer, retrieved_at=timestamp)
                except ValueError as exc:
                    summary["failed"] += 1
                    source.update(status="failed", error=str(exc))
                    continue
                seen.add(identity)
                candidates.append((item, page))
                if page.status == "failed":
                    summary["failed"] += 1
                    source.update(status="failed", error=page.error)
                else:
                    summary["converted"] += 1
                    source["status"] = "converted"
            summary["status"] = "partial" if summary["failed"] else "ok" if postings else "empty"
    return candidates, summaries
