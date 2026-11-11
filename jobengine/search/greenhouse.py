"""Read-only Greenhouse board listings, independent of search and model calls."""

import re
from contextlib import nullcontext

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jobengine.search.retrieval import canonical_url


class GreenhouseError(RuntimeError):
    """A board request failed or returned malformed data; never an empty-board signal."""


class GreenhouseJob(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    board_token: str
    id: int = Field(gt=0)
    internal_job_id: int | None = None
    title: str = Field(min_length=1)
    location: str
    url: str
    description: str | None = None

    @field_validator("url")
    @classmethod
    def posting_url(cls, value):
        canonical_url(value)  # Validate the shape without changing the original source URL.
        return value


def fetch_board_jobs(
    board_token: str, *, client: httpx.Client | None = None
) -> list[GreenhouseJob]:
    """Return every published post, or [] for an empty board; raise GreenhouseError on failure.

    Descriptions retain the API's original HTML/entities. Missing descriptions remain None.
    Prospect/general-application posts are retained (internal_job_id is None). No filtering,
    retries, model calls, candidate state, or search-pipeline integration occurs here.
    A supplied client remains owned by its caller.
    """
    if not isinstance(board_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", board_token):
        raise ValueError("board_token must contain only letters, digits, underscores, or hyphens.")
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    context = httpx.Client(trust_env=False) if client is None else nullcontext(client)
    try:
        with context as connection:
            response = connection.get(
                url,
                params={"content": "true"},
                timeout=20,
                follow_redirects=False,
                headers={
                    "User-Agent": "JobEngine/0.1 (personal job research)",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise GreenhouseError(f"Greenhouse board {board_token!r}: request timed out.") from exc
    except httpx.HTTPStatusError as exc:
        raise GreenhouseError(
            f"Greenhouse board {board_token!r}: HTTP {exc.response.status_code}."
        ) from exc
    except httpx.RequestError as exc:
        raise GreenhouseError(
            f"Greenhouse board {board_token!r}: request failed ({type(exc).__name__})."
        ) from exc
    try:
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Expected a JSON object with a jobs array.")
        jobs = []
        for index, posting in enumerate(payload["jobs"]):
            if not isinstance(posting, dict):
                raise ValueError(f"jobs[{index}]: expected a job object.")
            location = posting.get("location")
            if not isinstance(location, dict):
                raise ValueError(f"jobs[{index}]: expected a location object.")
            try:
                jobs.append(
                    GreenhouseJob.model_validate(
                        {
                            "board_token": board_token,
                            "id": posting.get("id"),
                            "internal_job_id": posting.get("internal_job_id"),
                            "title": posting.get("title"),
                            "location": location.get("name"),
                            "url": posting.get("absolute_url"),
                            "description": posting.get("content"),
                        }
                    )
                )
            except ValidationError as exc:
                fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
                raise ValueError(f"jobs[{index}]: invalid or missing fields: {fields}.") from exc
        return jobs
    except ValueError as exc:
        raise GreenhouseError(
            f"Greenhouse board {board_token!r}: malformed response ({exc})."
        ) from exc
