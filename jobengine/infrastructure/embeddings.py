"""Batched OpenAI embeddings using the existing cache and transactional cost ledger."""

import hashlib
import math

from openai import DefaultHttpxClient, OpenAI

from jobengine.infrastructure.storage import Store

MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
VERSION = 1  # Representation/cache epoch; bump if extraction or upstream model behavior changes.
INPUT_USD_PER_MILLION = 0.02  # Standard synchronous requests, verified 2026-11-10.
# https://developers.openai.com/api/docs/models/text-embedding-3-small
MAX_INPUT_BYTES = 6000  # UTF-8 bytes conservatively bound tokens below the 8192-token limit.
CACHE_SECONDS = 365 * 86400


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bounded_text(text):
    return text.encode("utf-8")[:MAX_INPUT_BYTES].decode("utf-8", errors="ignore")


def _unit(vector):
    if not isinstance(vector, list) or len(vector) != DIMENSIONS:
        raise ValueError("Invalid embedding dimensions.")
    if any(
        isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x)
        for x in vector
    ):
        raise ValueError("Embedding contains invalid numbers.")
    length = math.hypot(*vector)
    if not length or not math.isfinite(length):
        raise ValueError("Embedding has invalid magnitude.")
    return [x / length for x in vector]


class EmbeddingClient:
    """One concrete embedding client, not an agent or search provider framework.

    Failed/ambiguous requests retain their reservations, like ModelClient. Errors
    expose exception classes, never provider messages or private request text.
    """

    def __init__(self, store: Store, api_key="", *, client=None, batch_size=32):
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= 32
        ):
            raise ValueError("batch_size must be an integer from 1 to 32.")
        self.store, self.api_key, self.client = store, api_key, client
        self.batch_size, self.owned = batch_size, client is None

    def close(self):
        if self.owned and self.client is not None:
            self.client.close()

    def embed(self, texts, *, namespace, source_hashes=None, purpose="user"):
        """Return vectors (None on failure) and per-call billing/cache diagnostics.

        Cache keys contain model, dimensions, representation version, original-source
        hash and embedding-text hash. No applicant identity enters job cache keys.
        At most 32 inputs x 6000 bytes/request also stays below 300,000 total tokens.
        """
        texts = list(texts)
        hashes = list(source_hashes) if source_hashes is not None else [digest(t) for t in texts]
        if len(hashes) != len(texts):
            raise ValueError("source_hashes must match texts.")
        vectors = [None] * len(texts)
        report = {
            "model": MODEL,
            "dimensions": DIMENSIONS,
            "version": VERSION,
            "purpose": purpose,
            "requests": 0,
            "input_tokens": 0,
            "estimated_usd": 0.0,
            "unsettled_reserved_usd": 0.0,
            "cache_hits": 0,
            "failed_inputs": 0,
            "calls": [],
            "errors": [],
        }
        pending = {}
        for index, (text, source_hash) in enumerate(zip(texts, hashes)):
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text.encode("utf-8")) > MAX_INPUT_BYTES
            ):
                report["errors"].append({"indices": [index], "error_type": "InvalidEmbeddingInput"})
                continue
            key = self.store.key(
                {
                    "namespace": namespace,
                    "model": MODEL,
                    "dimensions": DIMENSIONS,
                    "version": VERSION,
                    "source_hash": source_hash,
                    "text_hash": digest(text),
                }
            )
            cached = self.store.cached(key, CACHE_SECONDS)
            if cached is not None:
                try:
                    vectors[index] = _unit(cached["vector"])
                    report["cache_hits"] += 1
                    continue
                except (ValueError, KeyError, TypeError):
                    pass  # Invalid cache entries are never used for similarity.
            if key not in pending:
                pending[key] = {"text": text, "indices": []}
            pending[key]["indices"].append(index)
        entries = list(pending.items())
        for offset in range(0, len(entries), self.batch_size):
            batch = entries[offset : offset + self.batch_size]
            indices = [i for _, value in batch for i in value["indices"]]
            byte_bound = sum(len(value["text"].encode("utf-8")) for _, value in batch)
            reserve = byte_bound * INPUT_USD_PER_MILLION / 1_000_000
            call = {
                "indices": indices,
                "input_hashes": [digest(v["text"]) for _, v in batch],
                "reserved_usd": reserve,
                "ledger_receipt": None,
                "settled": False,
            }
            report["calls"].append(call)
            try:
                if self.client is None:
                    self.client = OpenAI(
                        api_key=self.api_key,
                        timeout=60,
                        max_retries=0,
                        http_client=DefaultHttpxClient(trust_env=False),
                    )
                receipt = self.store.reserve(
                    f"semantic_embedding:{purpose}:{namespace}", MODEL, reserve
                )
                call["ledger_receipt"] = receipt
                report["unsettled_reserved_usd"] += reserve
                report["requests"] += 1
                response = self.client.embeddings.create(
                    model=MODEL,
                    dimensions=DIMENSIONS,
                    encoding_format="float",
                    input=[value["text"] for _, value in batch],
                )
                call["request_id"] = getattr(response, "_request_id", None)
                usage = response.usage.prompt_tokens if response.usage is not None else None
                if isinstance(usage, bool) or not isinstance(usage, int) or usage < 0:
                    raise ValueError("Embedding usage was missing or invalid.")
                amount = usage * INPUT_USD_PER_MILLION / 1_000_000
                self.store.settle(receipt, amount, usage, 0, 0)
                report["input_tokens"] += usage
                report["estimated_usd"] += amount
                report["unsettled_reserved_usd"] -= reserve
                call.update(settled=True, input_tokens=usage, estimated_usd=amount)
                if response.model != MODEL or len(response.data) != len(batch):
                    raise ValueError("Embedding response model or count mismatch.")
                ordered = {}
                for data in response.data:
                    if (
                        type(data.index) is not int
                        or data.index not in range(len(batch))
                        or data.index in ordered
                    ):
                        raise ValueError("Invalid or duplicate embedding response index.")
                    ordered[data.index] = _unit(data.embedding)
                for index, (key, value) in enumerate(batch):
                    self.store.cache(key, {"vector": ordered[index]})
                    for original in value["indices"]:
                        vectors[original] = ordered[index]
            except Exception as exc:
                error = {
                    "indices": indices,
                    "error_type": type(exc).__name__,
                    "http_status": getattr(exc, "status_code", None),
                }
                report["errors"].append(error)
                call["error_type"] = type(exc).__name__
                # No retries or fabricated vectors; other batches and cached results survive.
        report["failed_inputs"] = sum(vector is None for vector in vectors)
        report["unsettled_reserved_usd"] = max(0.0, report["unsettled_reserved_usd"])
        return vectors, report
