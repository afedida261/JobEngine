from __future__ import annotations

import json
import time
from typing import TypeVar

from openai import DefaultHttpxClient, OpenAI
from pydantic import BaseModel

from jobengine.infrastructure.diagnostics import emit, observing
from jobengine.infrastructure.storage import Store
from jobengine.prompts import BASE_INSTRUCTIONS

T = TypeVar("T", bound=BaseModel)


def _observe_response(response):
    """Capture the response before SDK schema parsing, including invalid JSON outputs."""
    if observing():
        response.read()
        try:
            body = response.json()
        except ValueError:
            body = response.text
        emit("model", "wire_response", http_status=response.status_code, response=body)


# Standard short-context USD per million tokens, verified 2026-11-09.
# https://developers.openai.com/api/docs/pricing
# Conservative cache-write allowance is included in estimates. Configurable model,
# but unknown rates fail closed instead of silently evading the $10 ledger.
PRICES = {
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6.1-sol": (2.0, 10.0),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-6.1-terra": (1.0, 5.0),
}
DEFAULT_INTERVIEW_MODEL = "gpt-6.1-terra"
DEFAULT_SEARCH_MODEL = "gpt-6.1-sol"
SEARCH_REASONING_EFFORT = "low"

SEARCH_OPERATIONS = frozenset(
    {
        "onboarding_search_brief",
        "sector_plan",
        "company_discovery",
        "company_job_discovery",
        "web_discovery",
        "normalize_companies",
        "normalize_discovery",
        "greenhouse_board_discovery",
        "normalize_greenhouse_employers",
        "extract_job",
        "repair_job_facts",
        "evaluate_fit",
        "repair_fit_citations",
    }
)


class ModelClient:
    def __init__(
        self,
        api_key: str,
        store: Store,
        model: str = DEFAULT_SEARCH_MODEL,
        *,
        interview_model: str = DEFAULT_INTERVIEW_MODEL,
    ):
        if not api_key.strip():
            raise ValueError("Configure OPENAI_API_KEY in .env or enter it in the sidebar.")
        if model not in PRICES or interview_model not in PRICES:
            raise ValueError("This model has no configured cost rates. Choose a supported model.")
        self.store, self.model = store, model
        self.interview_model = interview_model
        options = {}
        if observing():
            options["http_client"] = DefaultHttpxClient(
                event_hooks={"response": [_observe_response]}
            )
        self.client = OpenAI(api_key=api_key, timeout=120, max_retries=0, **options)

    def _request(self, operation, prompt, schema=None, search=False, max_tool_calls=3):
        started = time.monotonic()
        emit(
            "model",
            "started",
            operation=operation,
            prompt=prompt,
            schema=schema.model_json_schema() if schema else None,
        )
        try:
            result = self._request_impl(operation, prompt, schema, search, max_tool_calls)
            emit(
                "model",
                "validated",
                operation=operation,
                result=result,
                duration_seconds=time.monotonic() - started,
            )
            return result
        except Exception as exc:
            emit(
                "model",
                "failed",
                operation=operation,
                error_type=type(exc).__name__,
                reason=str(exc),
                duration_seconds=time.monotonic() - started,
                retried=False,
                http_status=getattr(getattr(exc, "response", None), "status_code", None),
                response_body=getattr(exc, "body", None),
            )
            raise

    def _request_impl(
        self,
        operation: str,
        prompt: str,
        schema=None,
        search: bool = False,
        max_tool_calls: int = 3,
    ):
        max_output = 6500 if schema else 5000
        max_tools = max(1, min(max_tool_calls, 5)) if search else 0
        is_search = search or operation in SEARCH_OPERATIONS
        model = self.model if is_search else self.interview_model
        effort = SEARCH_REASONING_EFFORT if is_search else "low"
        signature = {
            "version": 3,
            "operation": operation,
            "model": model,
            "reasoning_effort": effort,
            "prompt": prompt,
            "system": BASE_INSTRUCTIONS,
            "schema": schema.model_json_schema() if schema else None,
            "search": search,
            "max_output": max_output,
            "max_tools": max_tools,
        }
        emit("model", "configured", operation=operation, configuration=signature)
        key = self.store.key(signature)
        cached = self.store.cached(key, 3600 if search else 86400)
        if cached is not None:
            emit("model", "cache_hit", operation=operation, configuration=signature)
            return cached
        input_rate, output_rate = PRICES[model]
        # UTF-8 bytes conservatively bound tokens for supplied text. Search has
        # an additional conservative returned-content allowance per tool call.
        input_bound = (
            len(json.dumps(signature, ensure_ascii=False).encode("utf-8"))
            + 4000
            + max_tools * 128000
        )
        reserve = (
            input_bound * input_rate * 1.25 + max_output * output_rate
        ) / 1_000_000 + max_tools * 0.01
        receipt = self.store.reserve(operation, model, reserve)
        kwargs = dict(
            model=model,
            instructions=BASE_INSTRUCTIONS,
            input=prompt,
            max_output_tokens=max_output,
            reasoning={"effort": effort},
            store=False,
            service_tier="default",
        )
        if search:
            kwargs.update(
                tools=[{"type": "web_search", "search_context_size": "medium"}],
                tool_choice="required",
                max_tool_calls=max_tools,
                include=["web_search_call.action.sources"],
            )
        # If a call fails or its billing is ambiguous the reservation remains.
        emit(
            "model",
            "request",
            operation=operation,
            payload=kwargs,
            response_schema=schema.model_json_schema() if schema else None,
            reserved_usd=reserve,
            ledger_receipt=receipt,
        )
        if schema:
            response = self.client.responses.parse(**kwargs, text_format=schema)
        else:
            response = self.client.responses.create(**kwargs)
        if observing():
            emit(
                "model",
                "raw_response",
                operation=operation,
                response=response.model_dump(mode="json", warnings=False),
            )
        # ParsedResponse's generic nested serializer can log entire candidate
        # objects as Pydantic warnings. Only serialize the metadata we need.
        raw = (
            {
                "status": response.status,
                "usage": response.usage.model_dump(mode="json") if response.usage else {},
                "output": [],
            }
            if schema
            else response.model_dump(mode="json")
        )
        usage = raw.get("usage") or {}
        input_tokens, output_tokens = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        searches = sum(
            o.get("type") == "web_search_call" and o.get("action", {}).get("type") == "search"
            for o in raw.get("output", [])
        )
        # Include the cache-write premium even if this call did not incur it.
        amount = (
            input_tokens * input_rate * 1.25 + output_tokens * output_rate
        ) / 1_000_000 + searches * 0.01
        self.store.settle(receipt, amount, input_tokens, output_tokens, searches)
        if raw.get("status") != "completed":
            raise ValueError(
                "The model did not complete its response. Reduce the input or retry explicitly; no result was fabricated."
            )
        if schema:
            if response.output_parsed is None:
                raise ValueError("The model returned no structured result, possibly a refusal.")
            result = {"parsed": response.output_parsed.model_dump(mode="json")}
        else:
            result = {"text": response.output_text, "raw": raw}
        self.store.cache(key, result)
        return result

    def structured(self, operation: str, instructions: str, data: dict, schema: type[T]) -> T:
        prompt = instructions + "\n\nUNTRUSTED INPUT DATA:\n" + json.dumps(data, ensure_ascii=False)
        return schema.model_validate(self._request(operation, prompt, schema=schema)["parsed"])

    def search(self, prompt: str, *, operation="web_discovery", max_tool_calls=3) -> dict:
        return self._request(operation, prompt, search=True, max_tool_calls=max_tool_calls)
