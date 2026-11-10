"""Syntactic evidence checks, not semantic truth verification."""

from __future__ import annotations

import re
import unicodedata

from jobengine.models import Document, Evidence, JobFacts


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def contains_quote(text: str, quote: str | None) -> bool:
    return bool(quote and len(normalized(quote)) >= 8 and normalized(quote) in normalized(text))


def contains_field_quote(text: str, quote: str | None, minimum=3) -> bool:
    if not quote or len(normalized(quote)) < minimum:
        return False
    return bool(re.search(r"(?<!\w)" + re.escape(normalized(quote)) + r"(?!\w)", normalized(text)))


def availability_is_grounded(facts: JobFacts, text: str) -> bool:
    quote = facts.availability_quote
    if not contains_field_quote(text, quote):
        return False
    pattern = (
        r"\bapply\b|submit|accepting applications"
        if facts.availability == "open"
        else r"closed|no longer|filled|expired|not available"
    )
    return bool(re.search(pattern, normalized(quote)))


def sanitize_job_facts(facts: JobFacts, text: str) -> JobFacts:
    result = facts.model_copy(deep=True)
    if result.availability != "unknown" and not availability_is_grounded(result, text):
        result.availability, result.availability_quote = "unknown", None
        result.extraction_notes.append(
            "Application availability was not grounded; the description remains usable."
        )
    valid_terms = []
    for term in result.terms:
        if contains_field_quote(text, term.quote, 2 if term.field == "work_country" else 3):
            valid_terms.append(term)
        else:
            result.extraction_notes.append(
                f"Untraceable {term.field} field omitted; verify it against the original posting."
            )
    result.terms = valid_terms
    return result


def validate_evidence(evidence: list[Evidence], documents: list[Document]) -> None:
    sources = {d.id: d.text for d in documents}
    seen: set[str] = set()
    if len(sources) != len(documents):
        raise ValueError("Duplicate document IDs.")
    for item in evidence:
        if item.id in seen or not item.id:
            raise ValueError("Candidate evidence IDs must be unique and nonempty.")
        seen.add(item.id)
        if not contains_quote(sources.get(item.source_id, ""), item.quote):
            raise ValueError(
                f"Evidence {item.id} has no valid quotation in its source. Correct the profile."
            )


def validate_facts(facts: JobFacts, text: str) -> None:
    seen: set[str] = set()
    for req in facts.requirements:
        if not req.id or req.id in seen:
            raise ValueError("Employer requirement IDs are not unique.")
        seen.add(req.id)
        if not contains_quote(text, req.quote):
            raise ValueError(f"Requirement {req.id} contains an unsupported employer quotation.")
        if req.minimum_professional_years is not None and req.minimum_professional_years < 0:
            raise ValueError("Invalid professional experience requirement.")
    if facts.availability != "unknown" and not availability_is_grounded(facts, text):
        raise ValueError("Posting availability was asserted without a source quotation.")


def keyword_overlap(evidence: list[Evidence], job_text: str) -> float:
    """Deliberately simple comparison baseline, not used in AI ranking."""
    stop = {
        "with",
        "that",
        "this",
        "from",
        "have",
        "using",
        "project",
        "experience",
        "built",
        "work",
    }
    terms = (
        set(re.findall(r"[a-z][a-z0-9+#.]{2,}", " ".join(e.quote for e in evidence).lower())) - stop
    )
    job_terms = set(re.findall(r"[a-z][a-z0-9+#.]{2,}", job_text.lower()))
    return round(100 * len(terms & job_terms) / len(terms), 1) if terms else 0.0
