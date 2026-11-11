"""Embedding-only evaluation priorities, independent of lexical screening and search."""

import math
import re

from jobengine.infrastructure.embeddings import EmbeddingClient, bounded_text, digest
from jobengine.onboarding.brief import require_confirmed_search_brief


def applicant_text(profile):
    """Desired work and approved supporting experience, not a concatenated resume."""
    brief = require_confirmed_search_brief(profile)
    ids = {eid for strength in brief.strengths for eid in strength.evidence_ids}
    experience = [e.summary[:650] for e in profile.evidence if e.id in ids][:6]
    return bounded_text(
        "Desired roles: "
        + "; ".join(profile.target_roles)
        + "\nInterests: "
        + profile.interests
        + "\nDesired work themes: "
        + "; ".join(brief.work_themes)
        + "\nRelevant experience: "
        + "; ".join(experience)
    )


def job_text(item, page):
    """Prioritize generic responsibility sections, then requirements and fallback prose.

    Section headings are industry-independent English structure, not role mappings.
    Unknown headings/prose remain fallback text. Full pages are never modified.
    """
    body = page.text.split("\n\nJOB DESCRIPTION\n", 1)[-1]
    sections = {"duties": [], "requirements": [], "general": [], "boilerplate": []}
    current = "general"
    for raw in body.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        heading = line.casefold().strip(":").replace("’", "'")
        if len(heading) < 100:
            if re.fullmatch(
                r"(?:key |primary |your )?(?:responsibilities|duties)|what you(?:'ll| will) do|the role|your impact",
                heading,
            ):
                current = "duties"
                continue
            if re.fullmatch(
                r"(?:required |preferred |minimum )?(?:qualifications|requirements)|what you bring|your background",
                heading,
            ):
                current = "requirements"
                continue
            if re.fullmatch(
                r"about (?:us|the company)|who we are|our mission|benefits|compensation|equal opportunity(?: employer)?",
                heading,
            ):
                current = "boilerplate"
                continue
        if re.match(r"^(?:Title|Employer(?: \(provided by caller\))?|Location):", line):
            continue
        sections[current].append(line)
    meaningful = "\n".join(sections["duties"] + sections["requirements"] + sections["general"])
    return bounded_text(
        f"Job title: {item.title[:500]}\nActual work and requirements:\n{meaningful}"
    )


def cosine_similarity(left, right):
    if len(left) != len(right) or not left:
        raise ValueError("Cosine similarity requires equally sized nonempty vectors.")
    norm = math.hypot(*left) * math.hypot(*right)
    if not norm or not math.isfinite(norm):
        raise ValueError("Invalid cosine vector magnitude.")
    return max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(left, right)) / norm))


def prioritize_semantically(
    candidates,
    profile,
    embeddings: EmbeddingClient,
    *,
    top_n=10,
    purpose="user",
    hybrid=False,
    job_facts=None,
):
    """Return selected pairs, every scored/deferred row, and usage/errors diagnostics.

    Similarity is not qualification, location eligibility, or hiring probability.
    Failed inputs have similarity=None and remain deferred, never rejected. No
    retrieval or downstream model calls. Embedding-only is the default; hybrid=True
    applies source-backed deterministic priorities without additional API requests.
    """
    query = applicant_text(profile)
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 0:
        raise ValueError("top_n must be a nonnegative integer.")
    pool = list(candidates)
    rows = [
        {
            "item": item,
            "page": page,
            "similarity": None,
            "status": "not_selected_for_batch",
            "embedding_status": "unscored",
            "error": None,
        }
        for item, page in pool
    ]
    report = {"purpose": purpose, "applicant_text": query, "embedding_calls": [], "errors": []}
    active = []
    for index, (item, page) in enumerate(pool):
        if page.status != "retrieved" or not page.text.strip():
            rows[index].update(embedding_status="failed", error="Source description unavailable.")
            continue
        text = job_text(item, page)
        rows[index].update(
            embedding_text=text, source_content_hash=digest(page.text), input_hash=digest(text)
        )
        active.append(index)
    if active:
        query_vectors, query_report = embeddings.embed(
            [query], namespace="semantic_applicant", purpose=purpose
        )
        report["embedding_calls"].append(query_report)
        if query_vectors[0] is None:
            report["errors"].append(
                "Applicant embedding unavailable; the complete pool remains deferred."
            )
            for index in active:
                rows[index].update(
                    embedding_status="failed", error="Applicant embedding unavailable."
                )
        else:
            vectors, job_report = embeddings.embed(
                [rows[i]["embedding_text"] for i in active],
                namespace="semantic_job",
                source_hashes=[rows[i]["source_content_hash"] for i in active],
                purpose=purpose,
            )
            report["embedding_calls"].append(job_report)
            for index, vector in zip(active, vectors):
                if vector is None:
                    rows[index].update(
                        embedding_status="failed",
                        error="Job embedding unavailable; inspect batch diagnostics.",
                    )
                else:
                    rows[index].update(
                        similarity=round(cosine_similarity(query_vectors[0], vector), 6),
                        embedding_status="scored",
                    )
    rows.sort(key=lambda row: (row["similarity"] is None, -(row["similarity"] or 0.0)))
    selected = []
    for index, row in enumerate(rows):
        row["rank"] = index + 1
        if row["similarity"] is not None and len(selected) < top_n:
            row["status"] = "selected_for_evaluation"
            selected.append((row["item"], row["page"]))
    for key in (
        "requests",
        "input_tokens",
        "estimated_usd",
        "unsettled_reserved_usd",
        "cache_hits",
        "failed_inputs",
    ):
        report[key] = sum(call[key] for call in report["embedding_calls"])
    report.update(
        pool_size=len(rows),
        selected=len(selected),
        unscored=sum(r["similarity"] is None for r in rows),
    )
    if hybrid:
        from jobengine.search.semantic_priorities import apply_priority_adjustments

        selected, rows = apply_priority_adjustments(rows, profile, top_n=top_n, job_facts=job_facts)
        report["prioritization"] = "hybrid"
    else:
        report["prioritization"] = "embedding_only"
    return selected, rows, report
