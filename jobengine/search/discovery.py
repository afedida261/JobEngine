from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from jobengine.infrastructure.diagnostics import emit
from jobengine.infrastructure.llm import ModelClient
from jobengine.models import (
    CompanyCandidate,
    CompanyOutput,
    DiscoveryOutput,
    Profile,
    SearchRecord,
    SearchStep,
    SectorTarget,
    utc_now,
)
from jobengine.onboarding.brief import require_confirmed_search_brief
from jobengine.prompts import PROMPTS
from jobengine.search.retrieval import canonical_url


def search_sources(raw: dict) -> list[dict[str, str]]:
    sources: dict[str, dict[str, str]] = {}
    for output in raw.get("output", []):
        candidates = []
        if output.get("type") == "web_search_call" and output.get("status") == "completed":
            candidates.extend(output.get("action", {}).get("sources", []) or [])
        for content in output.get("content", []):
            candidates.extend(
                a for a in content.get("annotations", []) if a.get("type") == "url_citation"
            )
        for source in candidates:
            url = source.get("url")
            if not url:
                continue
            try:
                canonical = canonical_url(url)
                sources[canonical] = {"url": url, "title": source.get("title", "Source")}
            except ValueError:
                continue
    return list(sources.values())


def observed_source_url_count(raw: dict) -> int:
    """Count URL-bearing source and citation entries before validation or deduplication."""
    count = 0
    for output in raw.get("output", []):
        if output.get("type") == "web_search_call" and output.get("status") == "completed":
            count += sum(
                bool(s.get("url")) for s in (output.get("action", {}).get("sources") or [])
            )
        for content in output.get("content", []):
            count += sum(
                bool(a.get("url"))
                for a in content.get("annotations", [])
                if a.get("type") == "url_citation"
            )
    return count


def public_brief(profile: Profile) -> dict:
    approved = require_confirmed_search_brief(profile)
    location = [c.value for c in profile.constraints if c.field == "Location / remote / relocation"]
    # The visible approved plan drives discovery; raw resumes and contact details
    # are not included. Full eligibility constraints are assessed after retrieval.
    return {
        "target_roles": approved.target_roles,
        "interests": profile.interests,
        "professional_years": profile.professional_years,
        "location_preferences": location,
        "work_country": "United States",
        "search_objective": approved.summary,
        "work_themes": approved.work_themes,
        "relevant_experience": [s.statement for s in approved.strengths],
        "proposed_queries": approved.search_queries,
        "avoid": approved.avoid,
        "stretch_policy": approved.stretch_policy,
        "date_utc": utc_now()[:10],
    }


def search_queries(raw: dict, *, ignored: bool = False) -> list[str]:
    queries = []
    for output in raw.get("output", []):
        if output.get("type") != "web_search_call":
            continue
        if (output.get("status") != "completed") != ignored:
            continue
        action = output.get("action") or {}
        values = action.get("queries") or ([action["query"]] if action.get("query") else [])
        queries.extend(q for q in values if isinstance(q, str))
    return list(dict.fromkeys(queries))


def discover_companies(
    client: ModelClient, profile: Profile, sector: SectorTarget, limit=2
) -> SearchRecord:
    brief = public_brief(profile)
    prompt = PROMPTS["discover_companies"].format(
        limit=limit,
        sector=json.dumps(sector.model_dump()),
        brief=json.dumps(brief, ensure_ascii=False),
    )
    response = client.search(prompt, operation="company_discovery", max_tool_calls=3)
    sources = search_sources(response["raw"])
    warnings, companies = [], []
    if sources:
        extracted = client.structured(
            "normalize_companies",
            PROMPTS["normalize_companies"],
            {"summary": response["text"], "sources": sources, "limit": limit},
            CompanyOutput,
        )
        allowed = {canonical_url(s["url"]) for s in sources}
        seen = set()
        for company in extracted.companies:
            try:
                key = canonical_url(company.official_url)
                if key not in allowed:
                    host = urlsplit(key).netloc.removeprefix("www.")
                    sourced_page = next(
                        (
                            s["url"]
                            for s in sources
                            if urlsplit(canonical_url(s["url"])).netloc.removeprefix("www.") == host
                        ),
                        None,
                    )
                    if sourced_page:
                        company.official_url = sourced_page
                        key = canonical_url(sourced_page)
                        warnings.append(
                            "Used an actual sourced company subpage instead of an unsupported homepage URL."
                        )
                if key not in allowed or not company.name.strip():
                    warnings.append("Discarded a company without a search-backed official URL.")
                    continue
                if company.careers_url and canonical_url(company.careers_url) not in allowed:
                    company.careers_url = None
                    warnings.append("Discarded an unsupported careers URL.")
                if (
                    company.early_career_evidence_url
                    and canonical_url(company.early_career_evidence_url) not in allowed
                ):
                    company.early_career_evidence_url = None
                    warnings.append("Discarded an unsupported early-career hiring clue.")
                if key in seen:
                    continue
                company.sector = sector.name
                companies.append(company)
                seen.add(key)
            except ValueError:
                warnings.append("Discarded an invalid company URL.")
    else:
        warnings.append(f"No source-backed companies established in {sector.name}.")
    companies.sort(key=lambda c: c.early_career_evidence_url is None)
    step = SearchStep(
        stage="companies",
        sector=sector.name,
        queries=search_queries(response["raw"]),
        source_urls=[s["url"] for s in sources],
        candidates=min(len(companies), limit),
        outcome="Company candidates require a separate job search; early-career clues are not verified eligible vacancies.",
    )
    return SearchRecord(
        query=prompt,
        retrieved_at=utc_now(),
        summary=response["text"],
        sources=sources,
        jobs=[],
        warnings=warnings,
        strategy="sector_company_jobs",
        companies=companies[:limit],
        steps=[step],
    )


def discover(
    client: ModelClient,
    profile: Profile,
    limit: int = 6,
    direction: str = "",
    *,
    company: CompanyCandidate | None = None,
    sector: SectorTarget | None = None,
) -> SearchRecord:
    brief = public_brief(profile)
    if direction.strip():
        raise ValueError("Edit and confirm the search brief to change search direction.")
    if company is None or sector is None:
        raise ValueError("Discover companies in a confirmed sector before searching their jobs.")
    limit = max(1, min(limit, 6))
    prompt = PROMPTS["discover"].format(
        limit=limit,
        employer=json.dumps(
            {
                "name": company.name,
                "official_url": company.official_url,
                "careers_url": company.careers_url,
            }
        ),
        sector=sector.name,
        brief=json.dumps(brief, ensure_ascii=False),
    )
    response = client.search(prompt, operation="company_job_discovery", max_tool_calls=3)
    sources = search_sources(response["raw"])
    warnings = []
    if not sources:
        return SearchRecord(
            query=json.dumps(brief),
            retrieved_at=utc_now(),
            summary=response["text"],
            sources=[],
            jobs=[],
            warnings=["Search returned no inspectable source URLs. No listings were invented."],
            strategy="sector_company_jobs",
            companies=[company],
            steps=[
                SearchStep(
                    stage="jobs",
                    sector=sector.name,
                    company=company.name,
                    queries=search_queries(response["raw"]),
                    outcome="No inspectable source URLs returned.",
                )
            ],
        )
    result = client.structured(
        "normalize_discovery",
        PROMPTS["normalize_discovery"],
        {
            "summary": response["text"],
            "sources": sources,
            "limit": limit,
            "professional_years": profile.professional_years,
        },
        DiscoveryOutput,
    )
    allowed = {canonical_url(s["url"]) for s in sources}
    seen = set()
    jobs = []
    for item in result.jobs:
        emit("discover", "candidate", item=item.model_dump(mode="json"))
        try:
            canonical = canonical_url(item.url)
        except ValueError:
            emit(
                "discover",
                "rejected",
                item=item.model_dump(mode="json"),
                reason="Invalid result URL.",
                skipped="retrieval, extraction, assessment",
            )
            warnings.append("Discarded an invalid result URL.")
            continue
        if canonical not in allowed:
            emit(
                "discover",
                "rejected",
                item=item.model_dump(mode="json"),
                reason="URL absent from search provenance.",
                skipped="retrieval, extraction, assessment",
            )
            warnings.append(f"Discarded a URL not present in search provenance: {canonical}")
        elif (
            profile.professional_years == 0
            and re.search(
                r"\b(?:senior|sr\.?|staff|principal|director|vice president|head of)\b",
                item.title,
                re.I,
            )
            and not re.search(
                r"\b(?:junior|new[\s-]*grad|entry[\s-]*level|early[\s-]*career)\b", item.title, re.I
            )
        ):
            emit(
                "discover",
                "rejected",
                item=item.model_dump(mode="json"),
                reason="Existing zero-experience senior-title filter.",
                skipped="retrieval, extraction, assessment",
            )
            warnings.append(
                f"Skipped an obviously senior search candidate for a zero-experience profile: {item.title} ({item.url})."
            )
        elif canonical not in seen:
            jobs.append(item)
            item.company, item.sector, item.company_source_url = (
                company.name,
                sector.name,
                company.official_url,
            )
            seen.add(canonical)
        else:
            emit("discover", "skipped", item=item.model_dump(mode="json"), reason="Duplicate URL.")
    step = SearchStep(
        stage="jobs",
        sector=sector.name,
        company=company.name,
        queries=search_queries(response["raw"]),
        source_urls=[s["url"] for s in sources],
        candidates=min(len(jobs), limit),
        outcome="Source-backed URLs; original descriptions will be retrieved and checked.",
    )
    return SearchRecord(
        query=json.dumps(brief, ensure_ascii=False),
        retrieved_at=utc_now(),
        summary=response["text"],
        sources=sources,
        jobs=jobs[:limit],
        warnings=warnings,
        strategy="sector_company_jobs",
        companies=[company],
        steps=[step],
    )
