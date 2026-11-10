"""Run discovery only from a profile and search brief exported after in-app approval."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        required=True,
        type=Path,
        help="Approved candidate-profile.json exported from the app",
    )
    parser.add_argument("--limit", type=int, default=3, choices=range(1, 11))
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv

    from jobengine.infrastructure.llm import DEFAULT_SEARCH_MODEL, ModelClient
    from jobengine.infrastructure.storage import Store
    from jobengine.models import Profile
    from jobengine.onboarding.brief import require_confirmed_search_brief
    from jobengine.search.pipeline import run_search

    profile = Profile.model_validate_json(args.profile.read_text(encoding="utf-8"))
    require_confirmed_search_brief(profile)
    load_dotenv(ROOT / ".env")
    store = Store(ROOT / "data", float(os.getenv("JOBENGINE_BUDGET_USD", "10")))
    client = ModelClient(
        os.getenv("OPENAI_API_KEY", ""),
        store,
        os.getenv("OPENAI_SEARCH_MODEL", DEFAULT_SEARCH_MODEL),
    )
    run = run_search(
        client, profile, limit=args.limit, progress=lambda message: print(message, flush=True)
    )
    print(
        json.dumps(
            {
                "run_id": run.id,
                "sources": len(run.search.sources),
                "jobs": [
                    {
                        "title": j.facts.title if j.facts else j.discovery.title,
                        "url": j.page.url,
                        "category": j.category,
                        "source_characters": len(j.page.text),
                        "validation_issues": j.validation_issues,
                    }
                    for j in run.jobs
                ],
                "warnings": run.search.warnings,
                "estimated_total_usd": store.usage()["total"],
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
