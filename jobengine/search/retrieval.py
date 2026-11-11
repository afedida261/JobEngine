from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from jobengine.infrastructure.diagnostics import emit
from jobengine.infrastructure.storage import Store
from jobengine.models import JobPage, utc_now


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("Only public HTTP(S) URLs without credentials are supported.")
    if parts.port not in {None, 80, 443}:
        raise ValueError("Nonstandard ports are not supported.")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"gh_src", "source", "ref", "referral"}
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/") or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def validate_public_url(url: str) -> str:
    url = canonical_url(url)
    parts = urlsplit(url)
    host = parts.hostname
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Private network destinations are not supported.")
    try:
        addresses = {
            a[4][0]
            for a in socket.getaddrinfo(
                host,
                parts.port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise ValueError("Could not resolve the source hostname.") from exc
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError("Private or reserved IP addresses are not supported.")
    return url


def _job_nodes(node):
    if isinstance(node, list):
        for child in node:
            yield from _job_nodes(child)
    elif isinstance(node, dict):
        types = node.get("@type", [])
        if "JobPosting" in (types if isinstance(types, list) else [types]):
            yield node
        if "@graph" in node:
            yield from _job_nodes(node["@graph"])


def extract_html(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    job_nodes = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            job_nodes.extend(_job_nodes(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError):
            continue
    if len(job_nodes) == 1:
        job = dict(job_nodes[0])
        description = BeautifulSoup(str(job.pop("description", "")), "html.parser").get_text(
            "\n", strip=True
        )
        text = json.dumps(job, ensure_ascii=False, indent=2) + "\n\nJOB DESCRIPTION\n" + description
        # Retain visible page text as well: closure banners can supersede stale JSON-LD.
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
            tag.decompose()
        visible = soup.get_text("\n", strip=True)
        return text + "\n\nVISIBLE PAGE\n" + visible, "JobPosting JSON-LD + visible HTML"
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text("\n", strip=True)
    return text, "visible HTML"


def fetch_page(url: str, store: Store | None = None, force: bool = False) -> JobPage:
    original = url
    key = Store.key({"page": url, "version": 1})
    if store and not force:
        cached = store.cached(key, 6 * 3600)
        if cached:
            emit("http", "cache_hit", url=url)
            return JobPage.model_validate(cached)
    try:
        current = validate_public_url(url)
        # Validate every redirect and do not inherit proxy credentials from the environment.
        with httpx.Client(
            timeout=httpx.Timeout(20),
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": "JobEngine/0.1 (personal job research)",
                "Accept": "text/html,application/json",
            },
        ) as client:
            for _ in range(6):
                with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        current = validate_public_url(
                            urljoin(current, response.headers.get("location", ""))
                        )
                        continue
                    response.raise_for_status()
                    emit(
                        "http",
                        "retrieved",
                        url=str(response.url),
                        http_status=response.status_code,
                        redirects=_,
                    )
                    content_type = response.headers.get("content-type", "").lower()
                    if not any(
                        t in content_type
                        for t in (
                            "text/html",
                            "application/xhtml+xml",
                            "text/plain",
                            "application/json",
                        )
                    ):
                        raise ValueError("The URL did not return an HTML or text job page.")
                    parts, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 3_000_000:
                            raise ValueError("The source page exceeded the 3 MB retrieval limit.")
                        parts.append(chunk)
                    html = b"".join(parts).decode(response.encoding or "utf-8", errors="replace")
                    text, method = (
                        extract_html(html) if "html" in content_type else (html, "source text")
                    )
                    if len(text.strip()) < 250:
                        raise ValueError(
                            "Too little readable source text; the page may require JavaScript or be blocked."
                        )
                    if len(text) > 90000:
                        raise ValueError(
                            "Source text exceeds 90,000 characters; no partial description was evaluated."
                        )
                    page = JobPage(
                        requested_url=original,
                        url=str(response.url),
                        retrieved_at=utc_now(),
                        text=text,
                        content_hash=hashlib.sha256(text.encode()).hexdigest(),
                        extraction_method=method,
                        status="retrieved",
                    )
                    if store:
                        store.cache(key, page.model_dump())
                    return page
            raise ValueError("Too many source redirects.")
    except (httpx.HTTPError, ValueError, OSError) as exc:
        emit(
            "http",
            "failed",
            url=original,
            error_type=type(exc).__name__,
            reason=str(exc),
            http_status=getattr(getattr(exc, "response", None), "status_code", None),
            retried=False,
        )
        return JobPage(
            requested_url=original,
            url=original,
            retrieved_at=utc_now(),
            text="",
            content_hash="",
            extraction_method="none",
            status="failed",
            error=str(exc),
        )
