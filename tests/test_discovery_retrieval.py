import json
import socket

import pytest

from jobengine.models import CompanyCandidate, DiscoveryItem, DiscoveryOutput
from jobengine.search.discovery import discover, search_sources
from jobengine.search.retrieval import canonical_url, extract_html, validate_public_url


def test_search_collects_tool_sources_and_inline_citations():
    raw = {
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "type": "search",
                    "sources": [{"url": "https://example.com/a", "title": "A"}],
                },
            },
            {
                "type": "message",
                "content": [
                    {
                        "annotations": [
                            {"type": "url_citation", "url": "https://example.com/b", "title": "B"}
                        ]
                    }
                ],
            },
        ]
    }
    assert len(search_sources(raw)) == 2


def test_discovery_rejects_invented_urls_and_deduplicates(case, search_brief):
    from jobengine.onboarding.brief import confirm_search_brief

    case[0].search_brief = confirm_search_brief(case[0], search_brief)

    class FakeClient:
        def search(self, prompt, **kwargs):
            assert "SYNTHETIC TEST CANDIDATE" not in prompt
            return {
                "text": "Synthetic research",
                "raw": {
                    "output": [
                        {
                            "type": "web_search_call",
                            "action": {
                                "sources": [{"url": "https://example.com/job", "title": "Test"}]
                            },
                        }
                    ]
                },
            }

        def structured(self, *args):
            return DiscoveryOutput(
                jobs=[
                    DiscoveryItem(title="Test", company="Synthetic", url=url)
                    for url in [
                        "https://example.com/job",
                        "https://example.com/job?utm_source=test",
                        "https://example.com/invented",
                    ]
                ]
            )

    result = discover(
        FakeClient(),
        case[0],
        3,
        company=CompanyCandidate(
            name="Synthetic",
            official_url="https://example.com",
            careers_url=None,
            sector_rationale="Synthetic test",
        ),
        sector=search_brief.sectors[0],
    )
    assert len(result.jobs) == 1
    assert "not present in search provenance" in result.warnings[0]


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://user:pass@example.com/a", "https://example.com:1234/a"]
)
def test_unsafe_url_shapes_rejected(url):
    with pytest.raises(ValueError):
        canonical_url(url)


def test_private_network_destinations_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ValueError, match="Private or reserved"):
        validate_public_url("https://example.com/job")


def test_jsonld_description_and_closure_banner_both_retained():
    data = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Synthetic job",
        "description": "<p>Python programming is required.</p>",
    }
    text, method = extract_html(
        '<html><script type="application/ld+json">'
        + json.dumps(data)
        + "</script><body><p>This job is no longer available.</p></body></html>"
    )
    assert "Python programming is required" in text
    assert "no longer available" in text
    assert "JSON-LD" in method
