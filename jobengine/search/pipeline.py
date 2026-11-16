from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict
from urllib.parse import urlsplit

from jobengine.grounding import (
    contains_field_quote,
    contains_quote,
    sanitize_job_facts,
    validate_evidence,
    validate_facts,
)
from jobengine.infrastructure.diagnostics import emit, job_context
from jobengine.infrastructure.embeddings import EmbeddingClient
from jobengine.infrastructure.llm import ModelClient
from jobengine.infrastructure.storage import BudgetExceeded
from jobengine.models import (
    DiscoveryItem,
    FitJudgment,
    JobFacts,
    JobPage,
    Profile,
    RankedJob,
    Run,
    SearchRecord,
    SearchStep,
    utc_now,
)
from jobengine.onboarding.brief import require_confirmed_search_brief
from jobengine.onboarding.preferences import is_hard_constraint
from jobengine.prompts import PROMPTS
from jobengine.search.discovery import discover, discover_companies, public_brief
from jobengine.search.employer_identity import validate_employer_identity
from jobengine.search.ranking import rank_job, sort_jobs
from jobengine.search.retrieval import canonical_url, fetch_page

MAX_RESULTS = 10
MAX_RESEARCH_REQUESTS = 12
MAX_COMPANIES = 8
MAX_INSPECTIONS = 20


def job_identity(url):
    parts = urlsplit(canonical_url(url))
    path = parts.path.strip("/").split("/")
    if (
        parts.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"}
        and len(path) >= 3
        and path[1] == "jobs"
    ):
        return "greenhouse:" + path[0] + ":" + path[2]
    if parts.hostname in {"jobs.ashbyhq.com", "jobs.lever.co"} and len(path) >= 2:
        return parts.hostname + ":" + path[0].casefold() + ":" + path[1]
    return canonical_url(url)


def evaluate(
    client: ModelClient, profile: Profile, item: DiscoveryItem, *, page: JobPage | None = None
) -> RankedJob:
    with job_context(item):
        try:
            result = _evaluate(client, profile, item, page=page)
            emit("rank", "completed", result=result.model_dump(mode="json"))
            return result
        except Exception as exc:
            emit(
                "evaluation",
                "failed",
                error_type=type(exc).__name__,
                reason=str(exc),
                retried=False,
                skipped="Remaining evaluation stages",
            )
            raise


def _evaluate(
    client: ModelClient, profile: Profile, item: DiscoveryItem, *, page=None
) -> RankedJob:
    emit("retrieve", "reused" if page is not None else "attempted", url=item.url)
    page = page if page is not None else fetch_page(item.url, client.store)
    emit(
        "retrieve", page.status, page=page.model_dump(mode="json"), reason=page.error, retried=False
    )
    facts = None
    coverage_issues = []
    repairs = []
    if page.status == "failed":
        return RankedJob(
            discovery=item,
            page=page,
            facts=None,
            judgment=None,
            category="unverified",
            score=0,
            reasons=[page.error or "Retrieval failed."],
            validation_issues=[],
            keyword_score=0,
        )
    try:
        stage = "extract"
        facts = client.structured(
            "extract_job",
            PROMPTS["extract_job"],
            {"url": page.url, "source_text": page.text},
            JobFacts,
        )
        facts = sanitize_job_facts(facts, page.text)
        try:
            validate_facts(facts, page.text)
        except ValueError as exc:
            emit("validate", "repair_requested", reason=str(exc), retried=True)
            repairs.append("repair_job_facts")
            original_required = {r.id: r for r in facts.requirements if r.importance == "required"}
            facts = client.structured(
                "repair_job_facts",
                PROMPTS["repair_job_facts"],
                {
                    "source_text": page.text,
                    "proposed_facts": facts.model_dump(),
                    "validation_error": str(exc),
                },
                JobFacts,
            )
            facts = sanitize_job_facts(facts, page.text)
            validate_facts(facts, page.text)
            repaired_requirements = {r.id: r for r in facts.requirements}
            for rid, original in original_required.items():
                replacement = repaired_requirements.get(rid)
                if (
                    not replacement
                    or replacement.importance != "required"
                    or (
                        original.minimum_professional_years is not None
                        and (replacement.minimum_professional_years or 0)
                        < original.minimum_professional_years
                    )
                ):
                    coverage_issues.append(
                        f"Essential requirement {rid} changed or disappeared during quotation repair; coverage needs human review."
                    )
        emit("extract", "completed", facts=facts.model_dump(mode="json"))
        if not facts.is_job_posting:
            emit(
                "validate",
                "rejected",
                reason="Source is not an individual job posting.",
                facts=facts.model_dump(mode="json"),
                skipped="assessment",
            )
            return RankedJob(
                discovery=item,
                page=page,
                facts=facts,
                judgment=None,
                category="unverified",
                score=0,
                reasons=["The retrieved source is not an individual job posting."],
                validation_issues=[],
                keyword_score=0,
            )
        identity = validate_employer_identity(item, page, facts.company)
        emit(
            "employer_identity",
            "accepted" if identity.accepted else "rejected",
            reason=identity.reason,
            evidence=list(identity.evidence),
            discovered_employer=item.company,
            extracted_employer=facts.company,
        )
        if not identity.accepted:
            emit(
                "validate",
                "rejected",
                reason=identity.reason,
                facts=facts.model_dump(mode="json"),
                skipped="assessment",
            )
            return RankedJob(
                discovery=item,
                page=page,
                facts=facts,
                judgment=None,
                category="unverified",
                score=0,
                reasons=[identity.reason],
                validation_issues=[],
                keyword_score=0,
            )
        # Graduation/start declarations remain context, including on older saved profiles.
        stage = "assess"
        constraints = [
            c.model_dump() | {"hard": is_hard_constraint(c)} for c in profile.constraints
        ]
        judgment = client.structured(
            "evaluate_fit",
            PROMPTS["evaluate_fit"],
            {
                "evidence": [e.model_dump() for e in profile.evidence],
                "target_roles": profile.target_roles,
                "interests": profile.interests,
                "professional_years": profile.professional_years,
                "constraints": constraints,
                "confirmed_intake": profile.intake.model_dump(mode="json")
                if profile.intake
                else None,
                "job": facts.model_dump(),
            },
            FitJudgment,
        )
        invalid_citations = any(
            c.status != "unknown" and not contains_field_quote(page.text, c.job_quote)
            for c in judgment.constraints
        ) or (
            judgment.interest_alignment.level != "unknown"
            and not contains_quote(page.text, judgment.interest_alignment.job_quote)
        )
        if invalid_citations:
            emit("validate", "repair_requested", reason="Invalid fit citations.", retried=True)
            repairs.append("repair_fit_citations")
            repaired = client.structured(
                "repair_fit_citations",
                PROMPTS["repair_fit_citations"],
                {
                    "judgment": judgment.model_dump(),
                    "terms": [t.model_dump() for t in facts.terms],
                    "source_text": page.text,
                },
                FitJudgment,
            )
            judgment.constraints, judgment.interest_alignment = (
                repaired.constraints,
                repaired.interest_alignment,
            )
        result = rank_job(profile, item, page, facts, judgment)
        emit("assess", "completed", judgment=judgment.model_dump(mode="json"))
        if coverage_issues:
            result.validation_issues.extend(coverage_issues)
            if result.category in {"strong", "weaker"}:
                result.category = "needs_review"
                result.reasons.append(
                    "Requirement coverage changed during repair; verify the original qualifications."
                )
        if result.validation_issues:
            emit("validate", "rejected", reason="; ".join(result.validation_issues))
        return result
    except BudgetExceeded:
        raise
    except (ValueError, RuntimeError) as exc:
        emit(
            stage,
            "failed",
            error_type=type(exc).__name__,
            reason=str(exc),
            retried=bool(repairs),
            repairs=repairs,
            skipped="assessment/ranking" if stage == "extract" else "ranking",
        )
        return RankedJob(
            discovery=item,
            page=page,
            facts=facts,
            judgment=None,
            category="unverified",
            score=0,
            reasons=["Source or model grounding validation failed; no recommendation was made."],
            validation_issues=[str(exc)],
            keyword_score=0,
        )


def run_search(
    client: ModelClient,
    profile: Profile,
    limit=MAX_RESULTS,
    direction="",
    progress: Callable[[str], None] | None = None,
    *,
    purpose="user",
    max_minutes: float | None = None,
    greenhouse_boards: Mapping[str, str] | None = None,
    max_assessments: int | None = None,
    discover_greenhouse: bool = False,
    board_search_requests: int | None = None,
    max_boards: int | None = None,
) -> Run:
    if discover_greenhouse and greenhouse_boards is not None:
        raise ValueError("Choose dynamic Greenhouse discovery or explicit boards, not both.")
    if board_search_requests is not None or max_boards is not None:
        if not discover_greenhouse:
            raise ValueError("Board-discovery limits require discover_greenhouse.")
        from jobengine.search.greenhouse_discovery import MAX_BOARDS, MAX_SEARCH_REQUESTS

        for value, maximum, name in (
            (board_search_requests, MAX_SEARCH_REQUESTS, "board_search_requests"),
            (max_boards, MAX_BOARDS, "max_boards"),
        ):
            if value is not None and (type(value) is not int or not 1 <= value <= maximum):
                raise ValueError(f"{name} must be an integer from 1 to {maximum}.")
    if max_assessments is not None:
        if greenhouse_boards is None and not discover_greenhouse:
            raise ValueError("max_assessments is supported only for Greenhouse search.")
        if (
            isinstance(max_assessments, bool)
            or not isinstance(max_assessments, int)
            or not 1 <= max_assessments <= MAX_INSPECTIONS
        ):
            raise ValueError(f"max_assessments must be between 1 and {MAX_INSPECTIONS}.")
    if max_minutes is not None and (not math.isfinite(max_minutes) or max_minutes <= 0):
        raise ValueError("max_minutes must be a positive, finite number.")
    deadline = time.monotonic() + max_minutes * 60 if max_minutes is not None else None
    brief = require_confirmed_search_brief(profile)
    if not brief.sectors:
        raise ValueError("Review and approve the ordered sectors in your search brief first.")
    if direction.strip():
        raise ValueError("Change search direction by editing and approving the brief.")
    validate_evidence(profile.evidence, profile.documents)
    if not profile.target_roles or not profile.evidence:
        raise ValueError("At least one target role and one supported evidence item are required.")
    report = progress or (lambda message: None)
    target = max(1, min(int(limit), MAX_RESULTS))
    if greenhouse_boards is not None or discover_greenhouse:
        if greenhouse_boards is not None and not greenhouse_boards:
            raise ValueError("Provide at least one Greenhouse board token and employer name.")
        return _run_greenhouse_search(
            client,
            profile,
            greenhouse_boards,
            target,
            report,
            purpose,
            deadline,
            max_minutes,
            max_assessments if max_assessments is not None else MAX_INSPECTIONS,
            discover_greenhouse=discover_greenhouse,
            board_search_requests=board_search_requests,
            max_boards=max_boards,
        )
    order = {"preferred": 0, "related": 1, "expanded": 2}
    sectors = sorted(brief.sectors, key=lambda s: order[s.relationship])
    search = SearchRecord(
        query=json.dumps(public_brief(profile), ensure_ascii=False),
        retrieved_at=utc_now(),
        summary="Sector → companies → jobs",
        sources=[],
        jobs=[],
        warnings=[],
        strategy="sector_company_jobs",
        sectors=sectors,
    )
    inspected, seen_urls, seen_companies, source_urls = [], set(), set(), set()
    requests = 0

    def qualifying():
        return sum(j.category in {"strong", "weaker"} for j in inspected)

    def time_expired():
        return deadline is not None and time.monotonic() >= deadline

    def exhausted():
        if time_expired():
            return "time_limit"
        if qualifying() >= target:
            return "shortlist_target_reached"
        if len(inspected) >= MAX_INSPECTIONS:
            return "inspection_limit"
        if requests >= MAX_RESEARCH_REQUESTS:
            return "research_request_limit"
        return ""

    def collect(batch):
        search.steps.extend(batch.steps)
        search.warnings.extend(batch.warnings)
        for source in batch.sources:
            key = canonical_url(source["url"])
            if key not in source_urls:
                search.sources.append(source)
                source_urls.add(key)

    try:
        for sector in sectors:
            if exhausted():
                search.stop_reason = exhausted()
                break
            if len(seen_companies) >= MAX_COMPANIES:
                search.stop_reason = "company_limit"
                break
            report(
                f"{sector.name}: discovering companies ({qualifying()}/{target} confirmed shortlist matches so far)…"
            )
            requests += 1
            companies = discover_companies(client, profile, sector, limit=2)
            collect(companies)
            for company in companies.companies:
                if exhausted() or len(seen_companies) >= MAX_COMPANIES:
                    break
                parts = urlsplit(canonical_url(company.official_url))
                company_key = parts.netloc.removeprefix("www.")
                if company_key in {
                    "jobs.ashbyhq.com",
                    "jobs.lever.co",
                    "job-boards.greenhouse.io",
                    "boards.greenhouse.io",
                }:
                    company_key += "/" + parts.path.strip("/").split("/")[0].casefold()
                if company_key in seen_companies:
                    continue
                seen_companies.add(company_key)
                search.companies.append(company)
                report(f"{sector.name} → {company.name}: finding actual jobs…")
                requests += 1
                batch = discover(client, profile, limit=6, company=company, sector=sector)
                collect(batch)
                for discovered in batch.jobs:
                    emit("discover", "accepted", item=discovered.model_dump(mode="json"))
                for item in batch.jobs:
                    if (
                        time_expired()
                        or qualifying() >= target
                        or len(inspected) >= MAX_INSPECTIONS
                    ):
                        break
                    identity = job_identity(item.url)
                    if identity in seen_urls:
                        continue
                    seen_urls.add(identity)
                    search.jobs.append(item)
                    report(
                        f"{sector.name} → {company.name}: retrieving and evaluating {item.title}…"
                    )
                    result = evaluate(client, profile, item)
                    inspected.append(result)
                    search.steps.append(
                        SearchStep(
                            stage="evaluation",
                            sector=sector.name,
                            company=company.name,
                            source_urls=[item.url],
                            candidates=1,
                            outcome=f"{result.category}: " + " ".join(result.reasons),
                        )
                    )
            search.steps.append(
                SearchStep(
                    stage="evaluation",
                    sector=sector.name,
                    candidates=qualifying(),
                    outcome=f"Sector complete: {qualifying()}/{target} shortlist matches. Expand only if below target and within limits.",
                )
            )
        search.stop_reason = (
            search.stop_reason
            or exhausted()
            or ("company_limit" if len(seen_companies) >= MAX_COMPANIES else "sectors_exhausted")
        )
    except BudgetExceeded as exc:
        emit("search", "failed", error_type=type(exc).__name__, reason=str(exc), retried=False)
        search.stop_reason = "budget_limit"
        search.warnings.append(str(exc))
    except Exception as exc:
        emit("search", "failed", error_type=type(exc).__name__, reason=str(exc), retried=False)
        search.stop_reason = "research_error"
        search.warnings.append(
            f"Research stopped after {type(exc).__name__}; completed results are retained. Retry explicitly or inspect the source trace."
        )
    if search.stop_reason == "time_limit":
        message = f"Search time budget of {max_minutes:g} minutes reached; completed results are retained."
        search.warnings.append(message)
        report(message)
        emit("search", "stopped", reason=message, stop_reason="time_limit")
    search.summary = f"Checked {len(search.companies)} companies; inspected {len(inspected)} postings; {qualifying()} confirmed shortlist matches. Stop: {search.stop_reason}."
    visible = [j for j in sort_jobs(inspected) if j.category not in {"excluded", "unverified"}][
        :target
    ]
    run = Run(
        id=uuid.uuid4().hex,
        model=client.model,
        profile=profile,
        search=search,
        jobs=visible,
        inspected_jobs=sort_jobs(inspected),
        purpose=purpose,
    )
    client.store.save_run(run.id, run.model_dump(mode="json"))
    return run


def _run_greenhouse_search(
    client,
    profile,
    boards,
    target,
    report,
    purpose,
    deadline,
    max_minutes,
    max_assessments,
    *,
    discover_greenhouse=False,
    board_search_requests=None,
    max_boards=None,
):
    # Lazy import: the batch collector reuses this module's canonical job_identity.
    from jobengine.search.greenhouse_batch import discover_boards
    from jobengine.search.semantic import prioritize_semantically

    search = SearchRecord(
        query=json.dumps(public_brief(profile), ensure_ascii=False),
        retrieved_at=utc_now(),
        summary="Greenhouse boards → hybrid semantic priority → existing evaluation",
        sources=[],
        jobs=[],
        warnings=[],
        strategy="greenhouse_semantic",
        sectors=profile.search_brief.sectors,
    )
    inspected, ranked, summaries, usage = [], [], [], {}
    attempts = 0
    evaluation_failed = False

    def qualifying():
        return sum(job.category in {"strong", "weaker"} for job in inspected)

    discovery_failed = embedding_failed = False
    board_discovery_failed = False
    discovery_summary = ""
    discovering_boards = False

    def time_expired():
        return deadline is not None and time.monotonic() >= deadline

    def pool_snapshot():
        emit(
            "greenhouse",
            "pool",
            rows=[
                {
                    **row,
                    "item": row["item"].model_dump(mode="json"),
                    "page": row["page"].model_dump(mode="json"),
                }
                for row in ranked
            ],
        )

    try:
        if time_expired():
            search.stop_reason = "time_limit"
        else:
            prefetched = None
            if discover_greenhouse:
                from jobengine.search.greenhouse_discovery import discover_greenhouse_boards

                report(
                    "Discovering and verifying Greenhouse boards from the approved search brief…"
                )
                options = {}
                if board_search_requests is not None:
                    options["max_search_requests"] = board_search_requests
                if max_boards is not None:
                    options["max_boards"] = max_boards
                prefetched = {}
                discovering_boards = True
                details = {"boards": {}, "reason": "Board discovery did not complete."}
                try:
                    discovery = discover_greenhouse_boards(
                        client,
                        profile,
                        postings_cache=prefetched,
                        should_stop=time_expired,
                        **options,
                    )
                    details = asdict(discovery)
                finally:
                    emit("greenhouse", "discovery", details=details)
                discovering_boards = False
                boards = discovery.boards
                failures = [
                    s
                    for s in discovery.searches
                    if s["status"] == "failed" or s.get("normalization_error")
                ]
                failed_boards = [
                    r for r in discovery.candidates.values() if r["status"] == "failed"
                ]
                board_discovery_failed = bool(failures or failed_boards)
                search.warnings.extend(discovery.warnings)
                for failure in failures:
                    message = failure.get("error") or failure["normalization_error"]
                    search.warnings.append(message)
                    emit("board_discovery", "failed", reason=message)
                for record in failed_boards:
                    search.warnings.append(record["error"])
                    emit("board_discovery", "failed", reason=record["error"], board=record)
                for attempt in discovery.searches:
                    if attempt.get("outcome") in {"no_sources", "source_url_extraction_empty"}:
                        emit(
                            "board_discovery",
                            attempt["outcome"],
                            query=attempt["planned_query"],
                            recovery=attempt["recovery"],
                        )
                    search.sources.extend(s for s in attempt["sources"] if s not in search.sources)
                no_sources = sum(s.get("outcome") == "no_sources" for s in discovery.searches)
                extraction_empty = sum(
                    s.get("outcome") == "source_url_extraction_empty" for s in discovery.searches
                )
                discovery_summary = (
                    f"Greenhouse discovery: {no_sources} searches returned no URL entries; "
                    f"{extraction_empty} had URL entries but none passed extraction; "
                    f"{len(discovery.rejected_sources)} extracted sources had unsupported board URLs; "
                    f"{len(failed_boards)} board inspections failed. "
                    f"Verified {len(boards)} usable Greenhouse boards from {discovery.boards_inspected} inspections. "
                )
                if discovery.stop_reason in {"budget_limit", "time_limit"}:
                    search.stop_reason = discovery.stop_reason
                    search.warnings.append(
                        f"Greenhouse board discovery stopped: {discovery.stop_reason}; no assessment was started."
                    )
                elif not boards:
                    search.stop_reason = (
                        "board_discovery_error"
                        if board_discovery_failed
                        else "board_discovery_unidentified"
                        if discovery.candidates
                        else "board_discovery_empty"
                    )
                    message = "No verified, source-labeled Greenhouse boards are available; job relevance and candidate qualifications were not assessed."
                    search.warnings.append(message)
                    report(message)
            if not search.stop_reason:
                report("Collecting published postings from the Greenhouse boards…")
                candidates, summaries = (
                    discover_boards(boards, prefetched_postings=prefetched)
                    if discover_greenhouse
                    else discover_boards(boards)
                )
            else:
                candidates, summaries = [], []
            emit("greenhouse", "boards", summaries=summaries)
            for summary in summaries:
                if summary["status"] in {"failed", "partial"}:
                    discovery_failed = True
                    message = f"Greenhouse board {summary['board_token']}: {summary['error'] or str(summary['failed']) + ' posting conversions failed.'}"
                    search.warnings.append(message)
                    emit("greenhouse", "failed", reason=message, summary=summary)
                search.sources.append(
                    {
                        "url": f"https://boards-api.greenhouse.io/v1/boards/{summary['board_token']}/jobs?content=true",
                        "title": summary["employer"],
                    }
                )
            # Retain source pages before any embedding or evaluation can fail/stop.
            for index, (item, page) in enumerate(candidates):
                search.jobs.append(item)
                ranked.append(
                    dict(
                        item=item,
                        page=page,
                        rank=index + 1,
                        embedding_rank=None,
                        similarity=None,
                        final_score=None,
                        adjustments={},
                        adjustment_evidence=[],
                        uncertainty_flags=[],
                        status="not_selected_for_batch",
                        evaluation_status="not_evaluated",
                        deferral_reasons=["Semantic screening has not completed."],
                    )
                )
                with job_context(item):
                    emit("discover", "accepted", item=item.model_dump(mode="json"))
                    emit(
                        "retrieve",
                        page.status,
                        page=page.model_dump(mode="json"),
                        reason=page.error,
                    )
            pool_snapshot()
            if search.stop_reason:
                pass
            elif time_expired():
                search.stop_reason = "time_limit"
            elif not candidates:
                search.stop_reason = (
                    "board_discovery_error"
                    if board_discovery_failed
                    else "discovery_error"
                    if discovery_failed
                    else "boards_empty"
                )
            else:
                report(
                    f"Prioritizing {len(candidates)} postings; seeking {target} recommendations within {max_assessments} assessments…"
                )
                # Share the exact ledger and SDK configuration; this wrapper does not own the SDK.
                embeddings = EmbeddingClient(client.store, client=client.client)
                selected, scored, usage = prioritize_semantically(
                    candidates,
                    profile,
                    embeddings,
                    top_n=max_assessments,
                    hybrid=True,
                    purpose=purpose,
                )
                ranked = [dict(row, evaluation_status="not_evaluated") for row in scored]
                emit("greenhouse", "embedding_usage", usage=usage)
                for call in usage.get("embedding_calls", []):
                    for receipt in call.get("calls", []):
                        emit("embedding", "usage", **receipt)
                embedding_failed = any(
                    row["similarity"] is None and row["page"].status == "retrieved"
                    for row in ranked
                )
                budget_failed = any(
                    error.get("error_type") == "BudgetExceeded"
                    for call in usage.get("embedding_calls", [])
                    for error in call.get("errors", [])
                )
                if embedding_failed:
                    message = "Embedding screening failed for one or more postings; affected jobs remain unscored, not non-matches. Inspect embedding diagnostics."
                    search.warnings.append(message)
                    emit("embedding", "failed", reason=message, usage=usage)
                pool_snapshot()
                if budget_failed:
                    search.stop_reason = "budget_limit"
                else:
                    by_identity = {job_identity(row["item"].url): row for row in ranked}
                    for item, page in selected:
                        if qualifying() >= target:
                            search.stop_reason = "shortlist_target_reached"
                            break
                        if attempts >= max_assessments:
                            search.stop_reason = "assessment_limit"
                            break
                        if time_expired():
                            search.stop_reason = "time_limit"
                            break
                        row = by_identity[job_identity(item.url)]
                        attempts += 1
                        row["evaluation_status"] = "attempted"
                        report(f"Evaluating {item.company}: {item.title} (using API description)…")
                        try:
                            result = evaluate(client, profile, item, page=page)
                        except BudgetExceeded:
                            row.update(
                                evaluation_status="failed",
                                evaluation_error="BudgetExceeded",
                                disposition="failed",
                                outcome_reasons=["Cost budget reached during evaluation."],
                            )
                            raise
                        except Exception as exc:
                            row.update(
                                evaluation_status="failed",
                                evaluation_error=type(exc).__name__,
                                disposition="failed",
                                outcome_reasons=[f"Evaluation failed: {type(exc).__name__}."],
                            )
                            evaluation_failed = True
                            search.warnings.append(
                                f"Evaluation failed for {item.url}: {type(exc).__name__}; continuing within limits."
                            )
                            emit(
                                "search",
                                "failed",
                                error_type=type(exc).__name__,
                                reason=search.warnings[-1],
                            )
                            pool_snapshot()
                            continue
                        inspected.append(result)
                        row.update(
                            evaluation_status="completed",
                            evaluation=result.model_dump(mode="json"),
                            disposition="recommended"
                            if result.category in {"strong", "weaker"}
                            else "excluded"
                            if result.category == "excluded"
                            else "failed"
                            if result.category == "unverified"
                            else "not_recommended",
                            outcome_reasons=result.reasons + result.validation_issues,
                        )
                        search.steps.append(
                            SearchStep(
                                stage="evaluation",
                                sector=item.sector or "",
                                company=item.company,
                                source_urls=[item.url],
                                candidates=1,
                                outcome=f"{result.category}: " + " ".join(result.reasons),
                            )
                        )
                        pool_snapshot()
                    search.stop_reason = search.stop_reason or (
                        "shortlist_target_reached"
                        if qualifying() >= target
                        else "time_limit"
                        if time_expired()
                        else "assessment_limit"
                        if attempts >= max_assessments
                        else "embedding_error"
                        if embedding_failed
                        else "discovery_error"
                        if discovery_failed
                        else "board_discovery_error"
                        if board_discovery_failed
                        else "evaluation_error"
                        if evaluation_failed
                        else "candidate_pool_exhausted"
                    )
    except BudgetExceeded as exc:
        search.stop_reason = "budget_limit"
        search.warnings.append(str(exc))
        emit("search", "failed", error_type=type(exc).__name__, reason=str(exc), retried=False)
    except Exception as exc:
        search.stop_reason = "board_discovery_error" if discovering_boards else "research_error"
        search.warnings.append(
            f"Greenhouse search stopped after {type(exc).__name__}; the discovered pool and completed results are retained."
        )
        emit("search", "failed", error_type=type(exc).__name__, reason=str(exc), retried=False)
    if search.stop_reason == "time_limit":
        search.warnings.append(
            f"Search time budget of {max_minutes:g} minutes reached; completed results are retained."
        )
    for row in ranked:
        row.setdefault("disposition", "deferred")
        if row["evaluation_status"] != "completed":
            if row["similarity"] is not None or not usage:
                row["deferral_reasons"] = [
                    f"Not evaluated before search stopped: {search.stop_reason}."
                ]
            with job_context(row["item"]):
                emit(
                    "screen",
                    "deferred",
                    reason=" ".join(row["deferral_reasons"]),
                    priority={
                        key: value for key, value in row.items() if key not in {"item", "page"}
                    },
                )
    pool_snapshot()
    search.summary = (
        discovery_summary
        + f"Discovered {len(search.jobs)} unique postings; attempted {attempts}/{max_assessments} assessments; {qualifying()}/{target} confirmed shortlist matches. Stop: {search.stop_reason}."
    )
    run = Run(
        id=uuid.uuid4().hex,
        model=client.model,
        profile=profile,
        search=search,
        jobs=[j for j in sort_jobs(inspected) if j.category in {"strong", "weaker"}][:target],
        inspected_jobs=sort_jobs(inspected),
        purpose=purpose,
    )
    client.store.save_run(run.id, run.model_dump(mode="json"))
    return run
