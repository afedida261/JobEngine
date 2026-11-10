from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from urllib.parse import urlsplit

from jobengine.grounding import (
    contains_field_quote,
    contains_quote,
    sanitize_job_facts,
    validate_evidence,
    validate_facts,
)
from jobengine.infrastructure.llm import ModelClient
from jobengine.infrastructure.storage import BudgetExceeded
from jobengine.models import (
    DiscoveryItem,
    FitJudgment,
    JobFacts,
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
from jobengine.search.ranking import rank_job, sort_jobs
from jobengine.search.retrieval import canonical_url, fetch_page

MAX_RESULTS = 10
MAX_RESEARCH_REQUESTS = 12
MAX_COMPANIES = 8
MAX_INSPECTIONS = 20


def _company_name(value):
    return "".join(
        w
        for w in re.findall(r"[a-z0-9]+", value.casefold())
        if w
        not in {
            "inc",
            "incorporated",
            "llc",
            "ltd",
            "limited",
            "corp",
            "corporation",
            "technologies",
        }
    )


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


def evaluate(client: ModelClient, profile: Profile, item: DiscoveryItem) -> RankedJob:
    page = fetch_page(item.url, client.store)
    facts = None
    coverage_issues = []
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
        if not facts.is_job_posting:
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
        if item.company_source_url and _company_name(facts.company) != _company_name(item.company):
            return RankedJob(
                discovery=item,
                page=page,
                facts=facts,
                judgment=None,
                category="unverified",
                score=0,
                reasons=["The retrieved posting's employer does not match the researched company."],
                validation_issues=[],
                keyword_score=0,
            )
        # Graduation/start declarations remain context, including on older saved profiles.
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
        if coverage_issues:
            result.validation_issues.extend(coverage_issues)
            if result.category in {"strong", "weaker"}:
                result.category = "needs_review"
                result.reasons.append(
                    "Requirement coverage changed during repair; verify the original qualifications."
                )
        return result
    except BudgetExceeded:
        raise
    except (ValueError, RuntimeError) as exc:
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
) -> Run:
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

    def exhausted():
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
                for item in batch.jobs:
                    if qualifying() >= target or len(inspected) >= MAX_INSPECTIONS:
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
        search.stop_reason = "budget_limit"
        search.warnings.append(str(exc))
    except Exception as exc:
        search.stop_reason = "research_error"
        search.warnings.append(
            f"Research stopped after {type(exc).__name__}; completed results are retained. Retry explicitly or inspect the source trace."
        )
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
