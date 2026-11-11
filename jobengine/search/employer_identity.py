"""Conservative, offline employer identity decisions shared by every posting source.

Names from discovery/extraction are claims. Organizational relationships require
posting evidence and provenance; a caller-supplied board display name is not proof.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from jobengine.models import DiscoveryItem, JobPage

_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "company", "co"}


def _words(value):
    return re.findall(r"[a-z0-9]+", value.casefold())


def _name(value):
    words = _words(value)
    if words[:1] == ["the"]:
        words = words[1:]
    while words and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


def _parts(value):
    return [p.strip() for p in re.split(r"\s*[()/\u2013\u2014]\s*|\s+-\s+", value) if p.strip()]


def _pattern(value):
    return r"\b" + r"\W+".join(map(re.escape, _words(value))) + r"\b"


def _present(value, text):
    return bool(_words(value)) and re.search(_pattern(value), text, re.I) is not None


def _employer_context(value, text):
    name = _pattern(value) + r"(?:\s+(?:Company|Inc\.?|LLC|Corporation))?"
    return bool(
        re.search(
            rf"(?:^\s*|(?:Company|Employer|Department):\s*){name}\s*$"
            rf"|{name}(?:[\u2019\x27]s)?\s+(?:is\s+(?:an?\s+)?(?:equal opportunity|seeking|committed to (?:the hiring|hiring))|will provide reasonable accommodations|provides accommodations|participates in E-Verify)"
            rf"|^\s*The {name} (?:defends|advances|serves|operates|supports)\b"
            rf'|"hiringOrganization"\s*:\s*\{{[^{{}}]{{0,500}}"name"\s*:\s*"{name}"',
            text,
            re.I | re.M,
        )
    )


def _domain_names(value):
    return {_name(p).replace(" ", "") for p in _parts(value)} | {
        _name(p).removesuffix(" university").replace(" ", "") for p in _parts(value)
    }


def _provenance(item, page):
    source = urlsplit(item.company_source_url or "")
    target = urlsplit(page.url)
    # Match a complete domain label, never an arbitrary substring or URL query.
    labels = (source.hostname or "").split(".")
    names = _domain_names(item.company) - {""}
    if len(labels) >= 2 and labels[-2] in names:
        return "researched employer domain"
    if page.requested_url == item.url and any(
        label in names for label in (target.hostname or "").split(".")[-2:-1]
    ):
        return "employer posting domain"
    if (
        source.hostname
        and source.hostname == target.hostname
        and source.hostname
        not in {
            "boards.greenhouse.io",
            "job-boards.greenhouse.io",
            "boards-api.greenhouse.io",
            "jobs.lever.co",
            "jobs.smartrecruiters.com",
            "www.paycomonline.net",
        }
    ):
        return "researched employer posting host"
    match = re.fullmatch(r"/v1/boards/([a-zA-Z0-9_-]+)/jobs", source.path)
    post = re.fullmatch(r"/([^/]+)/jobs/(\d+)/?", target.path)
    if (
        source.hostname == "boards-api.greenhouse.io"
        and match
        and post
        and target.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"}
        and match[1] == post[1]
        and re.search(
            rf"board_token={re.escape(post[1])}; posting_id={post[2]}(?:;|$)",
            page.extraction_method,
        )
        and page.extraction_method.startswith("Greenhouse Job Board API;")
        and _name(post[1]).replace(" ", "").removeprefix("the")
        in {n.removeprefix("the") for n in names}
    ):
        return "verified Greenhouse board and posting IDs"
    return None


@dataclass(frozen=True)
class EmployerIdentity:
    accepted: bool
    reason: str
    evidence: tuple[str, ...] = ()


def validate_employer_identity(
    item: DiscoveryItem, page: JobPage, extracted: str
) -> EmployerIdentity:
    """No fuzzy matching, external lookups, or inferred corporate ownership."""
    if _words(item.company) == _words(extracted) and _name(extracted):
        return EmployerIdentity(True, "Employer names agree.")
    # Adapter metadata repeats the caller's name: it is not independent evidence.
    text = re.sub(r"(?m)^Employer \(provided by caller\):[^\n]*\n?", "", page.text)
    provenance = _provenance(item, page)
    if not provenance:
        return EmployerIdentity(
            False, "Employer identity mismatch: no corroborating employer source provenance."
        )
    if (
        _name(item.company)
        and _name(item.company) == _name(extracted)
        and _present(extracted, text)
    ):
        return EmployerIdentity(
            True, f"Source-grounded legal-name variant; {provenance}.", (extracted,)
        )
    left, right = _parts(item.company), _parts(extracted)
    components = list(dict.fromkeys(left + right))
    joint = set()
    for a in components:
        for b in components:
            if a != b and re.search(rf"\bat {_pattern(a)} and {_pattern(b)},\s+you", text, re.I):
                joint.update((a, b))
    if {_name(p) for p in left} & {_name(p) for p in right} and all(
        _present(p, text) and (_employer_context(p, text) or p in joint) for p in components
    ):
        return EmployerIdentity(
            True,
            f"Source-grounded organizational name components; {provenance}.",
            tuple(dict.fromkeys(left + right)),
        )
    # Require an explicit affirmative relationship connecting the two names in one
    # sentence. Mere co-occurrence (clients, partners, competitors) is insufficient.
    for a, b in ((item.company, extracted), (extracted, item.company)):
        relation = (
            rf"{_pattern(a)}\s+(?:is\s+)?(?:an?\s+)?"
            rf"(?:wholly[ -]owned\s+)?(?:subsidiary|division|department|brand|part)\s+of\s+{_pattern(b)}(?=\s*(?:[.,;!?\n]|$))"
            rf"|{_pattern(a)}\s+(?:is\s+)?(?:also known as|formerly known as|doing business as|d/b/a)\s+{_pattern(b)}(?=\s*(?:[.,;!?\n]|$))"
        )
        match = re.search(relation, text, re.I)
        if match:
            return EmployerIdentity(
                True, f"Explicit organizational relationship; {provenance}.", (match[0],)
            )
    for parent, child in ((item.company, extracted), (extracted, item.company)):
        # Legal entities may append "Brands, LLC" to the product-brand name.
        brand = _name(child).removesuffix(" brands")
        match = re.search(
            rf"{_pattern(parent)}[^.!?\n]{{0,200}}\b(?:owns|owner of)\b"
            rf"[^.!?\n]{{0,150}}\b(?:brand portfolios?|brands|subsidiaries)\b"
            rf"[^\n]{{0,350}}?\bincluding\b[^.!?\n]{{0,700}}?{_pattern(brand)}",
            text,
            re.I,
        )
        if (
            match
            and not re.search(r"\b(?:not|never|no longer)\b", match[0], re.I)
            and _employer_context(child, text)
        ):
            return EmployerIdentity(
                True, f"Explicit employer brand portfolio; {provenance}.", (match[0], child)
            )
    return EmployerIdentity(
        False,
        "Employer identity mismatch: names differ and the posting does not verify an organizational relationship.",
    )
