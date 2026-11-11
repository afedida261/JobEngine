"""Convert an already-fetched Greenhouse post to existing evaluation inputs, without I/O."""

import hashlib
from html import unescape

from bs4 import BeautifulSoup

from jobengine.models import DiscoveryItem, JobPage, utc_now
from jobengine.search.greenhouse import GreenhouseJob
from jobengine.search.retrieval import extract_html


def adapt_posting(
    posting: GreenhouseJob,
    *,
    employer: str,
    sector: str | None = None,
    retrieved_at: str | None = None,
) -> tuple[DiscoveryItem, JobPage]:
    """Return a discovery item and its ready-to-use page; never fetch or call a model.

    Employer is supplied explicitly: a board token is not a company display name.
    The timestamp defaults to conversion time; callers can supply the original fetch time.
    Missing/unusable descriptions produce failed pages, following retrieval conventions.
    """
    if not isinstance(employer, str) or not employer.strip():
        raise ValueError("Provide the employer's display name.")
    employer = employer.strip()
    source = f"https://boards-api.greenhouse.io/v1/boards/{posting.board_token}/jobs?content=true"
    item = DiscoveryItem(
        title=posting.title,
        company=employer,
        url=posting.url,
        sector=sector,
        company_source_url=source,
    )
    html = posting.description or ""
    # Greenhouse may entity-escape the entire HTML fragment, sometimes twice.
    # Stop once markup is present so entities inside ordinary HTML remain text.
    for _ in range(2):
        if BeautifulSoup(html, "html.parser").find() is not None:
            break
        decoded = unescape(html)
        if decoded == html:
            break
        html = decoded
    soup = BeautifulSoup(html, "html.parser")
    # This is a job-description fragment, not site navigation: retain all sections,
    # including application instructions outside main/article or inside a footer.
    for tag in soup.find_all(["main", "article", "header", "footer", "nav"]):
        tag.name = "div"
    description, method = extract_html(str(soup))
    provenance = (
        f"Greenhouse Job Board API; board_token={posting.board_token}; posting_id={posting.id}"
    )
    if posting.internal_job_id is not None:
        provenance += f"; internal_job_id={posting.internal_job_id}"
    error = None
    if len(description.strip()) < 250:
        error = "Too little readable description text (at least 250 characters required)."
    text = (
        f"Title: {posting.title}\nEmployer (provided by caller): {employer}\n"
        f"Location: {posting.location}\n\nJOB DESCRIPTION\n{description}"
    )
    if len(text) > 90000:
        error = "Source text exceeds 90,000 characters; no partial description was adapted."
    if error:
        text = ""
    page = JobPage(
        requested_url=posting.url,
        url=posting.url,
        retrieved_at=retrieved_at or utc_now(),
        text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest() if not error else "",
        extraction_method=f"{provenance}; {method}",
        status="failed" if error else "retrieved",
        error=error,
    )
    return item, page
