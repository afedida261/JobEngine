"""Retrieve actual source URLs without model spending. Not a demo-data generator."""

import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="+", help="Original employer posting URLs to inspect")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from jobengine.infrastructure.storage import Store
    from jobengine.search.retrieval import fetch_page

    store = Store(Path(__file__).resolve().parents[1] / "data")
    for url in args.urls:
        page = fetch_page(url, store)
        print(
            json.dumps(
                {
                    "url": page.url,
                    "status": page.status,
                    "characters": len(page.text),
                    "method": page.extraction_method,
                    "retrieved_at": page.retrieved_at,
                    "error": page.error,
                }
            )
        )


if __name__ == "__main__":
    main()
