"""Conservative source-backed adjustments to an existing semantic ranking; no I/O."""

import math
import re

from jobengine.grounding import contains_field_quote, normalized
from jobengine.models import Constraint
from jobengine.onboarding.brief import require_confirmed_search_brief
from jobengine.search.geography import place_key, restriction_match

NUMBER_WORDS = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
NUMBER = r"(?:\d+(?:\.\d+)?|" + "|".join(NUMBER_WORDS) + r")"
AMBIGUOUS_EXPERIENCE = r"equivalent|preferred|up to|not (?:required|mandatory|necessary|necessarily)|may be waived|waivable|negotiable|flexible|or (?:a |an )?(?:degree|education|less|fewer|\d)"
YEARS = re.compile(
    r"\b(?:(?P<upper>no more than|at most|maximum(?: of)?)|(?P<lower>requires?|must have|minimum(?: of)?|at least))?\s*"
    r"(?P<first>" + NUMBER + r")(?:(?:\s*[-\u2013\u2014]\s*|\s+to\s+)(?P<last>" + NUMBER + r"))?"
    r"(?P<plus>\+)?\s+years?(?:['\u2019])?",
    re.I,
)


def _number(value):
    return (
        float(NUMBER_WORDS.index(value.lower())) if value.lower() in NUMBER_WORDS else float(value)
    )


def _experience_bounds(text, facts, page, flags):
    bounds = []
    if facts is not None:
        for req in facts.requirements:
            if (
                req.importance != "required"
                or req.kind != "qualification"
                or req.minimum_professional_years is None
            ):
                continue
            if not contains_field_quote(page.text, req.quote):
                flags.append("Untraceable structured experience requirement; neutral.")
                continue
            parsed = list(YEARS.finditer(req.quote))
            years = req.minimum_professional_years
            if (
                math.isfinite(years)
                and years >= 0
                and any(not m["upper"] and _number(m["first"]) == years for m in parsed)
            ):
                if not re.search(AMBIGUOUS_EXPERIENCE, req.quote, re.I):
                    bounds.append((years, None, req.quote))
            else:
                flags.append(
                    "Structured experience minimum lacks matching numeric source evidence; neutral."
                )
    context = None
    for line in text.splitlines():
        heading = line.strip().strip(":").lower()
        if re.fullmatch(
            r"(?:(?:basic|minimum|required|essential|preferred|desired) )?(?:qualifications|requirements|experience)",
            heading,
        ):
            context = "preferred" if re.search(r"preferred|desired", heading) else "required"
            continue
        if re.fullmatch(
            r"responsibilities|duties|benefits|about us|compensation|how to apply|application requirements",
            heading,
        ):
            context = None
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            matches = list(YEARS.finditer(sentence))
            if not matches:
                continue
            # Bare/ranged counts need a qualification section. Outside one, require
            # an explicit experience requirement, never project tenure or company age.
            explicit = bool(
                re.search(
                    r"(?:requires?|must have|minimum|at least).{0,45}years?.{0,35}(?:experience|tenure)",
                    sentence,
                    re.I,
                )
            )
            if context is None and not explicit:
                continue
            if (
                context == "preferred"
                or re.search(AMBIGUOUS_EXPERIENCE, sentence, re.I)
                or len(matches) != 1
            ):
                flags.append("Experience alternatives or ambiguity need review; neutral.")
                continue
            m = matches[0]
            # Qualification context still needs experience language, or an explicit
            # standalone minimum/maximum ('at least 3 years', not 'contract: 3 years').
            if not re.search(r"experience|tenure", sentence, re.I) and not (
                m["lower"] or m["upper"]
            ):
                continue
            if re.search(
                r"contract|program lasts|company founded|project duration", sentence, re.I
            ):
                continue
            first = _number(m["first"])
            if m["last"] and (_number(m["last"]) < first or m["upper"]):
                flags.append("Inconsistent experience range; neutral.")
                continue
            bounds.append((None, first, sentence) if m["upper"] else (first, None, sentence))
    return bounds


EMPLOYMENT = {
    "full_time": ("hours", r"full[ _-]time"),
    "part_time": ("hours", r"part[ _-]time"),
    "permanent": ("duration", r"permanent"),
    "temporary": ("duration", r"temporary"),
    "contract": ("duration", r"contract"),
    "internship": ("program", r"internships?"),
}


def _label(text, name):
    match = re.search(r"^" + re.escape(name) + r":[ \t]*([^\n]*)$", text, re.I | re.M)
    return match.group(1).strip() if match else None


def _place(value):
    return place_key(value)


def _activity_words(text):
    stop = set(
        "the and for with from this that work role job duties responsibilities experience professional required relevant desired".split()
    )
    result = set()
    for word in re.findall(r"[a-z]+", normalized(text)):
        if word in stop or len(word) < 4:
            continue
        for suffix in ("ing", "ment", "ers", "er", "ed", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
        if word.endswith("e") and len(word) > 4:
            word = word[:-1]
        result.add(word)
    return result


def _signals(profile, page, facts=None):
    adjustments = {"experience": 0.0, "location": 0.0, "employment_type": 0.0, "misleading": 0.0}
    evidence, flags = [], []
    text = page.text.split("\n\nJOB DESCRIPTION\n", 1)[-1]
    bounds = _experience_bounds(text, facts, page, flags)
    years = profile.professional_years
    if bounds and years is not None and math.isfinite(years) and years >= 0:
        signals = []
        for minimum, maximum, quote in bounds:
            gap = minimum - years if minimum is not None else years - maximum
            adjustment = 0.005 if gap <= 0 else -0.015 if gap <= 2 else -0.04 if gap <= 5 else -0.08
            signals.append(adjustment)
            evidence.append(
                {
                    "signal": "experience",
                    "quote": quote,
                    "minimum_years": minimum,
                    "maximum_years": maximum,
                    "applicant_years": years,
                    "adjustment": adjustment,
                }
            )
        adjustments["experience"] = min(signals)
    else:
        flags.append(
            "Missing reliable experience requirement or applicant total; seniority is neutral."
        )

    intake = profile.intake
    locations = intake.locations if intake else []
    if intake is None and any(
        c.field == "Location / remote / relocation" for c in profile.constraints
    ):
        flags.append(
            "Geography is only free-text; reliable structured intake is unavailable, so neutral."
        )
    arrangement = _label(page.text, "Work arrangement")
    known_arrangement = {
        "remote": "remote",
        "hybrid": "hybrid",
        "on site": "on_site",
        "on_site": "on_site",
    }.get(" ".join(re.findall(r"\w+", normalized(arrangement or ""))))
    location = _label(page.text, "Location")
    if facts is not None:
        for term in facts.terms:
            if term.field == "location" and contains_field_quote(page.text, term.quote):
                location = term.value
                break
    # Only equality after punctuation normalization is certain without a geocoder.
    listed = {_place(x) for x in re.split(r";|\|", location or "") if x.strip()}
    allowed = {_place(x) for x in locations}
    matched = bool(allowed & listed)
    eligible = _label(page.text, "Eligible work locations")
    eligible_match = bool(allowed & {_place(x) for x in (eligible or "").split(";")})
    if matched and known_arrangement != "remote":
        adjustments["location"] += 0.01
        evidence.append({"signal": "location", "quote": location, "adjustment": 0.01})
    elif known_arrangement == "remote" and eligible_match:
        adjustments["location"] += 0.01
        evidence.append(
            {
                "signal": "remote_eligibility",
                "quote": f"Eligible work locations: {eligible}",
                "adjustment": 0.01,
            }
        )
    elif locations:
        flags.append("Geographic equivalence or remote eligibility is unverified; neutral.")
    if (
        intake
        and intake.work_arrangements
        and known_arrangement
        and known_arrangement not in intake.work_arrangements
    ):
        adjustments["location"] -= 0.04
        evidence.append(
            {
                "signal": "work_arrangement",
                "quote": f"Work arrangement: {arrangement}",
                "adjustment": -0.04,
            }
        )
    if not known_arrangement:
        flags.append("Work arrangement is not explicitly structured; neutral.")
    excluded = _label(page.text, "Excluded work locations")
    exclusion = bool(allowed & {_place(x) for x in (excluded or "").split(";")})
    relocation = _label(page.text, "Relocation required")
    if exclusion or (
        intake
        and intake.relocation == "no"
        and normalized(relocation or "") == "yes"
        and not matched
    ):
        adjustments["location"] = min(adjustments["location"], -0.04)
        evidence.append(
            {
                "signal": "geographic_restriction",
                "quote": f"Excluded work locations: {excluded}"
                if exclusion
                else "Relocation required: yes",
                "adjustment": -0.04,
            }
        )

    # Explicit residency assertions describe the job/applicant, not company HQ.
    residency = re.compile(
        r"(?:must (?:be based|reside|live|be located)|you (?:will|must) be based|"
        r"this (?:position|role) (?:is|will be)(?: remote)? based|"
        r"this is (?:a|an) [^.!?\n]{0,65}?(?:role|position) based) in (?:the )?([^.!?\n;]+)",
        re.I,
    )
    for match in residency.finditer(text):
        required = re.split(r",\s*(?:or\s+)?|\s+or\s+", match[1])
        status = restriction_match(locations, required)
        if status is False:
            adjustments["location"] = -0.04
            evidence.append(
                {"signal": "residency_restriction", "quote": match.group(), "adjustment": -0.04}
            )
        elif status is None:
            flags.append(
                "Explicit residency restriction has uncertain geographic coverage; neutral."
            )
        else:
            evidence.append(
                {"signal": "residency_compatible", "quote": match.group(), "adjustment": 0.0}
            )

    employment = _label(page.text, "Employment type")
    job_types = {
        kind
        for kind, (_, pattern) in EMPLOYMENT.items()
        if re.search(r"\b" + pattern + r"\b", employment or "", re.I)
    }
    remaining = normalized(employment or "")
    for _, pattern in EMPLOYMENT.values():
        remaining = re.sub(r"\b" + pattern + r"\b", "", remaining)
    if re.sub(r"[\s,;/&+-]", "", remaining):
        job_types = set()  # Negated/conditional type descriptions are not affirmative type facts.
        flags.append("Employment type wording is not an unambiguous structured value; neutral.")
    preferences = []
    preference_sources = list(profile.constraints)
    if intake and intake.other_exclusions.strip():
        preference_sources.append(
            Constraint(
                id="intake_other_exclusions",
                field="Other exclusions",
                value=intake.other_exclusions,
                hard=True,
            )
        )
    for constraint in preference_sources:
        if normalized(constraint.field) not in {"employment type", "other exclusions"}:
            continue
        value = normalized(constraint.value)
        for kind, (group, pattern) in EMPLOYMENT.items():
            if re.fullmatch(r"no " + pattern + r"(?: roles| positions)?[.]?", value):
                preferences.append((kind, group, False, constraint.id))
            elif normalized(constraint.field) == "employment type" and re.fullmatch(
                pattern + r"(?: roles| positions)?(?: only)?", value
            ):
                preferences.append((kind, group, True, constraint.id))
    for kind, group, wanted, identity in preferences:
        explicit_group = {x for x in job_types if EMPLOYMENT[x][0] == group}
        if (not wanted and kind in job_types) or (
            wanted and explicit_group and kind not in explicit_group
        ):
            adjustments["employment_type"] = -0.03
            evidence.append(
                {
                    "signal": "employment_type",
                    "quote": f"Employment type: {employment}",
                    "preference_id": identity,
                    "adjustment": -0.03,
                }
            )
            break
        if wanted and kind in job_types:
            adjustments["employment_type"] = 0.005
            evidence.append(
                {
                    "signal": "employment_type",
                    "quote": f"Employment type: {employment}",
                    "preference_id": identity,
                    "adjustment": 0.005,
                }
            )
    if not preferences or not job_types:
        flags.append("No explicit employment preference/type pairing; neutral.")

    nonjob = re.search(
        r"(?:^|[.!?]\s+)(?:(?:this (?:posting|listing|advertisement|page) is|this is)\s+)?(?:an? advertisement only[,;]?\s+)?(?:not (?:a|an) (?:job|employment) (?:opening|posting|opportunity)|no (?:actual )?(?:vacancy|employment opportunity) (?:is )?(?:available|offered))\b",
        text,
        re.I | re.M,
    )
    if nonjob:
        adjustments["misleading"] = -0.10
        evidence.append(
            {"signal": "explicit_non_job", "quote": nonjob.group(), "adjustment": -0.10}
        )
    else:
        ids = {eid for strength in profile.search_brief.strengths for eid in strength.evidence_ids}
        desired = _activity_words(
            " ".join(
                profile.target_roles
                + [profile.interests]
                + profile.search_brief.work_themes
                + [e.summary for e in profile.evidence if e.id in ids]
            )
        )
        denials = re.finditer(
            r"\bno ([^.!?\n]{1,180}?) (?:duties|responsibilities)\b|\bno ([^.!?\n]{1,180}?) (?:is|are) performed\b|\b(?:this (?:position|role) )?does not ((?:deliver|perform|include) [^.!?\n]{1,180})",
            text,
            re.I,
        )
        for denial in denials:
            following = text[denial.end() : denial.end() + 60]
            if re.search(r"experience|background|knowledge", denial.group(), re.I) or re.match(
                r"\s*(?:(?:is|are) )?(?:required|needed|necessary|experience)\b", following, re.I
            ):
                flags.append("Duty/experience wording is ambiguous; neutral.")
                continue
            overlap = desired & _activity_words(denial.group())
            if len(overlap) >= 2:
                adjustments["misleading"] = -0.08
                evidence.append(
                    {
                        "signal": "explicit_duty_denial",
                        "quote": denial.group(),
                        "matched_terms": sorted(overlap),
                        "adjustment": -0.08,
                    }
                )
                break
            flags.append("Possible duty denial has insufficient functional evidence; neutral.")
    return adjustments, evidence, flags


def apply_priority_adjustments(ranked, profile, *, top_n=10, job_facts=None):
    """Adjust already-scored rows without embedding/network calls or rejecting jobs.

    Optional existing JobFacts are keyed by original posting URL and must have
    source-grounded quotes. Employment preferences use existing approved Constraint
    entries, not assumed defaults. Unrecognized statements remain neutral.
    """
    require_confirmed_search_brief(profile)
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 0:
        raise ValueError("top_n must be a nonnegative integer.")
    rows = []
    for original in ranked:
        row = dict(original)
        adjustments, evidence, flags = _signals(
            profile, row["page"], (job_facts or {}).get(row["item"].url)
        )
        total = sum(adjustments.values())
        applied = max(-0.12, min(0.12, total))
        adjustments["aggregate_cap"] = round(applied - total, 6)
        row.update(
            embedding_rank=original.get("embedding_rank", original["rank"]),
            adjustments=adjustments,
            adjustment_total=round(applied, 6),
            adjustment_evidence=evidence,
            uncertainty_flags=flags,
            final_score=round(row["similarity"] + applied, 6)
            if row["similarity"] is not None
            else None,
        )
        rows.append(row)
    rows.sort(
        key=lambda r: (r["final_score"] is None, -(r["final_score"] or 0.0), r["embedding_rank"])
    )
    selected = []
    for index, row in enumerate(rows):
        row["rank"] = index + 1
        chosen = row["final_score"] is not None and len(selected) < top_n
        row["status"] = "selected_for_evaluation" if chosen else "not_selected_for_batch"
        row["deferral_reasons"] = (
            []
            if chosen
            else [
                "Embedding unavailable."
                if row["similarity"] is None
                else "Outside this batch's top-N priority cutoff; not a qualification rejection."
            ]
        )
        if not chosen:
            row["deferral_reasons"].extend(
                f"{key} reduced priority by {abs(value):.3f}; see source evidence."
                for key, value in row["adjustments"].items()
                if value < 0 and key != "aggregate_cap"
            )
        if chosen:
            selected.append((row["item"], row["page"]))
    return selected, rows
