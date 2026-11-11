"""Bounded, applicant-led Greenhouse board discovery."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from jobengine.infrastructure.llm import ModelClient
from jobengine.infrastructure.storage import BudgetExceeded
from jobengine.models import Profile
from jobengine.onboarding.brief import require_confirmed_search_brief
from jobengine.prompts import PROMPTS
from jobengine.search.discovery import public_brief, search_queries, search_sources
from jobengine.search.greenhouse import GreenhouseError, GreenhouseJob, fetch_board_jobs
from jobengine.search.retrieval import canonical_url

MAX_SEARCH_REQUESTS = 8
MAX_BOARDS = 40
DEFAULT_SEARCH_REQUESTS = 4
DEFAULT_MAX_BOARDS = 12
_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
_TOKEN = r"[A-Za-z0-9_-]+"


class EmployerLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: str
    employer: str
    employer_quote: str


class EmployerLabels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    labels: list[EmployerLabel]


@dataclass
class BoardDiscoveryResult:
    # Direct input to greenhouse_batch.discover_boards; only API-verified boards
    # with a source-backed display label appear here.
    boards: dict[str, str] = field(default_factory=dict)
    candidates: dict[str, dict] = field(default_factory=dict)
    searches: list[dict] = field(default_factory=list)
    rejected_sources: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    limits: dict[str, int] = field(default_factory=dict)
    search_requests: int = 0
    normalization_requests: int = 0
    boards_inspected: int = 0
    stop_reason: str = "search_request_limit"


def board_token_from_url(url: str) -> str:
    """Extract an exact token from supported Greenhouse URLs, never custom domains.

    Reject encoded/ambiguous paths, credentials and lookalike hosts. Query strings
    cannot supply a token except on Greenhouse's explicit embed routes.
    """
    canonical_url(url)
    parts = urlsplit(url.strip())
    if parts.hostname not in _HOSTS:
        raise ValueError("Not a supported Greenhouse-hosted URL.")
    path = parts.path.rstrip("/")
    if path in {"/embed/job_board", "/embed/job_app"}:
        values = parse_qs(parts.query).get("for", [])
        if len(values) == 1 and re.fullmatch(_TOKEN, values[0]):
            return values[0]
        raise ValueError("Missing or invalid Greenhouse embed board token.")
    match = re.fullmatch(rf"/({_TOKEN})(?:/jobs/([0-9]+))?", path)
    if not match or match[1].casefold() in {"embed", "jobs", "privacy", "security"}:
        raise ValueError("Unsupported Greenhouse path or invalid board token.")
    if match[2] is not None and int(match[2]) <= 0:
        raise ValueError("Invalid Greenhouse posting ID.")
    return match[1]


def _limit(value, maximum, name):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}.")


def _label_key(value):
    return " ".join(re.findall(r"\w+", value.casefold()))


def _record_labels(result, extracted, sources):
    allowed = {s["url"]: s for s in sources}
    for label in extracted.labels:
        source = allowed.get(label.source_url)
        employer, quote = label.employer.strip(), label.employer_quote.strip()
        if (
            source is None
            or not employer
            or not quote
            or quote not in source["title"]
            or not _label_key(employer)
            or not re.search(rf"(?<!\w){re.escape(_label_key(employer))}(?!\w)", _label_key(quote))
        ):
            result.warnings.append(
                "Rejected an employer label without exact search-title evidence."
            )
            continue
        token = board_token_from_url(source["url"])
        record = result.candidates[token]
        claim = {"employer": employer, "source_url": label.source_url, "quote": quote}
        if claim not in record["employer_labels"]:
            record["employer_labels"].append(claim)


def discover_greenhouse_boards(
    client: ModelClient,
    profile: Profile,
    *,
    max_search_requests: int = DEFAULT_SEARCH_REQUESTS,
    max_boards: int = DEFAULT_MAX_BOARDS,
    http_client: httpx.Client | None = None,
    postings_cache: dict[str, list[GreenhouseJob]] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> BoardDiscoveryResult:
    """Discover across the approved brief without a predefined employer list.

    Each search request permits at most one web tool call, followed by at most one
    structured label extraction. Both use the supplied ModelClient's cost ledger.
    Each unique board gets at most one API verification, failures included in the
    inspection cap. No retries, paid calls on import, ranking, or candidate writes.
    A supplied HTTP client stays owned by its caller. Empty API boards are valid.
    An optional invocation-local cache retains successful API responses for batch
    collection without refetching. should_stop prevents starting work after a deadline.
    """
    _limit(max_search_requests, MAX_SEARCH_REQUESTS, "max_search_requests")
    _limit(max_boards, MAX_BOARDS, "max_boards")
    approved = require_confirmed_search_brief(profile)
    brief = public_brief(profile) | {"sectors": [s.model_dump() for s in approved.sectors]}
    result = BoardDiscoveryResult(
        limits={
            "search_requests": max_search_requests,
            "web_tool_calls_per_request": 1,
            "board_inspections": max_boards,
        }
    )
    context = httpx.Client(trust_env=False) if http_client is None else nullcontext(http_client)
    with context as connection:
        for index in range(max_search_requests):
            if should_stop and should_stop():
                result.stop_reason = "time_limit"
                break
            focus = {
                "query": approved.search_queries[index % len(approved.search_queries)],
                "sector": approved.sectors[index % len(approved.sectors)].name
                if approved.sectors
                else None,
                "pass": index + 1,
                "already_seen_boards": list(result.candidates),
            }
            prompt = PROMPTS["discover_greenhouse_boards"].format(
                brief=json.dumps(brief, ensure_ascii=False), focus=json.dumps(focus)
            )
            attempt = {"prompt": prompt, "status": "failed", "sources": [], "queries": []}
            result.searches.append(attempt)
            result.search_requests += 1
            try:
                response = client.search(
                    prompt, operation="greenhouse_board_discovery", max_tool_calls=1
                )
                sources = search_sources(response["raw"])
                attempt.update(
                    status="ok", sources=sources, queries=search_queries(response["raw"])
                )
            except Exception as exc:
                # Provider exceptions can contain credentials/content: retain their
                # type, not arbitrary provider response bodies.
                attempt["error"] = f"Web discovery failed ({type(exc).__name__})."
                if isinstance(exc, BudgetExceeded):
                    result.stop_reason = "budget_limit"
                    break
                continue
            eligible = []
            for source in sources:
                try:
                    token = board_token_from_url(source["url"])
                except ValueError as exc:
                    result.rejected_sources.append(source | {"reason": str(exc)})
                    continue
                record = result.candidates.setdefault(
                    token,
                    {
                        "board_token": token,
                        "status": "deferred",
                        "sources": [],
                        "employer_labels": [],
                        "job_count": None,
                        "error": None,
                    },
                )
                if source not in record["sources"]:
                    record["sources"].append(source)
                eligible.append(source)
            if should_stop and should_stop():
                result.stop_reason = "time_limit"
            elif not eligible:
                attempt["warning"] = "No supported Greenhouse source URLs returned."
            else:
                result.normalization_requests += 1
                try:
                    labels = client.structured(
                        "normalize_greenhouse_employers",
                        PROMPTS["normalize_greenhouse_employers"],
                        {"sources": eligible, "brief": brief},
                        EmployerLabels,
                    )
                    _record_labels(result, labels, eligible)
                except Exception as exc:
                    attempt["normalization_error"] = (
                        f"Employer normalization failed ({type(exc).__name__})."
                    )
                    if isinstance(exc, BudgetExceeded):
                        result.stop_reason = "budget_limit"
            for token, record in result.candidates.items():
                if result.stop_reason in {"time_limit", "budget_limit"}:
                    break
                if should_stop and should_stop():
                    result.stop_reason = "time_limit"
                    break
                if record["status"] != "deferred" or result.boards_inspected >= max_boards:
                    continue
                result.boards_inspected += 1
                try:
                    jobs = fetch_board_jobs(token, client=connection)
                    if postings_cache is not None:
                        postings_cache[token] = jobs
                    record.update(status="verified", job_count=len(jobs))
                except (GreenhouseError, ValueError) as exc:
                    record.update(status="failed", error=str(exc))
            if result.stop_reason in {"budget_limit", "time_limit"}:
                break
            if result.boards_inspected >= max_boards:
                result.stop_reason = "board_inspection_limit"
                break
    for token, record in result.candidates.items():
        labels = record["employer_labels"]
        if record["status"] == "verified" and labels:
            result.boards[token] = labels[0]["employer"]
            if len({_label_key(label["employer"]) for label in labels}) > 1:
                result.warnings.append(
                    f"Board {token}: multiple observed employer labels; using the first source label. "
                    "These labels do not establish corporate ownership or aliases."
                )
        elif record["status"] == "verified":
            result.warnings.append(
                f"Board {token}: API verified, but no source-backed employer label; omitted from mapping."
            )
    return result
