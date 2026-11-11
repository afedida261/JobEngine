"""Local diagnostic artifacts; authoritative inputs retain the application schemas."""

import hashlib
import json
import os
import re
import time
import uuid
from collections import Counter
from pathlib import Path

from jobengine.models import utc_now


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    path.chmod(0o600)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def identifier(value):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", value):
        raise ValueError("Candidate IDs use 1–80 letters, digits, underscores or hyphens.")
    return value


def job_id(item):
    from jobengine.search.pipeline import job_identity

    try:
        key = job_identity(item["url"])
    except ValueError:
        key = item["url"]
    return hashlib.sha256(key.encode()).hexdigest()[:20]


class Artifacts:
    def __init__(self, root, candidate, command, store, output=print, verbose=False):
        self.id = uuid.uuid4().hex
        self.path = Path(root) / "runs" / identifier(candidate) / self.id
        self.path.mkdir(parents=True, mode=0o700)
        self.store, self.verbose = store, verbose
        self.output = lambda value: output(self.redact(str(value)))
        self.started = time.monotonic()
        self.receipts = set()
        self.events, self.failures, self.jobs = [], [], {}
        self.call_id = None
        self.manifest = dict(
            format_version=1,
            run_id=self.id,
            candidate=candidate,
            command=command,
            started_at=utc_now(),
            status="running",
        )
        self.save("manifest.json", self.manifest)

    def redact(self, value):
        if isinstance(value, dict):
            return {
                k: "[REDACTED]"
                if k.lower()
                in {
                    "api_key",
                    "authorization",
                    "headers",
                    "access_token",
                    "refresh_token",
                    "password",
                    "secret",
                    "openai_api_key",
                }
                else self.redact(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            for name, secret in os.environ.items():
                if len(secret) >= 8 and any(
                    s in name.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
                ):
                    value = value.replace(secret, "[REDACTED]")
            return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
        return value

    def save(self, name, value):
        write_json(self.path / name, self.redact(value))

    def __call__(self, stage, outcome, **details):
        details = self.redact(details)
        if stage == "greenhouse":
            if outcome == "discovery":
                usage = [
                    call
                    for call in self.store.usage()["calls"]
                    if call["id"] in self.receipts
                    and call["operation"]
                    in {
                        "greenhouse_board_discovery",
                        "normalize_greenhouse_employers",
                    }
                ]
                self.save(
                    "search/greenhouse-discovery.json",
                    {
                        **details["details"],
                        "usage": usage,
                        "estimated_or_reserved_usd": sum(call["amount"] for call in usage),
                    },
                )
            elif outcome == "boards":
                self.save("search/greenhouse-boards.json", details["summaries"])
            elif outcome == "embedding_usage":
                self.save("search/embedding-usage.json", details["usage"])
            elif outcome == "pool":
                self.save("search/greenhouse-pool.json", details["rows"])
                for row in details["rows"]:
                    jid = job_id(row["item"])
                    if jid in self.jobs:
                        self.jobs[jid]["priority"] = {
                            key: value
                            for key, value in row.items()
                            if key not in {"item", "page", "evaluation"}
                        }
                        self.save(f"search/jobs/{jid}.json", self.jobs[jid])
        if details.get("ledger_receipt"):
            self.receipts.add(details["ledger_receipt"])
        item = details.get("job") or details.get("item")
        jid = job_id(item) if item else None
        if stage == "model" and outcome == "started":
            self.call_id = f"call-{len(self.events) + 1:04d}"
        event = dict(
            sequence=len(self.events) + 1,
            at=utc_now(),
            stage=stage,
            outcome=outcome,
            job_id=jid,
            call_id=self.call_id,
            **details,
        )
        self.events.append(event)
        self.save(f"events/{event['sequence']:04d}-{stage}-{outcome}.json", event)
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        if jid:
            job = self.jobs.setdefault(
                jid,
                dict(
                    job_id=jid,
                    discovery=item,
                    events=[],
                    assessed=False,
                    technical_failure=False,
                    validation_rejected=False,
                ),
            )
            job["events"].append(event["sequence"])
            if details.get("job") or (stage == "discover" and outcome == "accepted"):
                job["discovery"] = item
            if stage == "retrieve" and "page" in details:
                job["page"] = details["page"]
            if "facts" in details:
                job["facts"] = details["facts"]
            if stage == "screen" and outcome == "deferred":
                job["skip_reason"] = details["reason"]
            if stage == "assess" and outcome == "completed":
                job["assessed"] = True
                job["judgment"] = details["judgment"]
            if stage == "rank" and "result" in details:
                job["result"] = details["result"]
                if details["result"].get("facts"):
                    job["facts"] = details["result"]["facts"]
            if outcome in {"failed", "rejected"}:
                grounding_failure = (
                    stage in {"extract", "assess"} and details.get("error_type") == "ValueError"
                )
                job["technical_failure"] |= (
                    outcome == "failed" and stage != "validate" and not grounding_failure
                )
                job["validation_rejected"] |= outcome == "rejected" or grounding_failure
                job["skip_reason"] = details.get("reason")
            self.save(f"search/jobs/{jid}.json", job)
        if outcome in {"failed", "rejected", "repair_requested"}:
            kind = (
                "retrieval"
                if stage in {"retrieve", "http"}
                else (
                    "model_output_validation"
                    if stage == "model"
                    and details.get("error_type") in {"ValidationError", "ValueError"}
                    else "model"
                    if stage == "model"
                    else "validation"
                    if stage in {"validate", "discover"}
                    or details.get("error_type") in {"ValidationError", "ValueError"}
                    else "pipeline"
                )
            )
            self.failures.append(dict(kind=kind, **event))
            self.save("failures.json", self.failures)
        if outcome not in {
            "request",
            "raw_response",
            "wire_response",
            "configured",
            "cache_hit",
            "candidate",
        }:
            label = details.get("operation") or jid or ""
            detail = details.get("reason") or details.get("notice") or details.get("question") or ""
            if "evidence_added" in details:
                detail = f"{details['evidence_added']} evidence items added. {detail}"
            self.output(f"[{stage}] {label} {outcome} {detail}")
        if self.verbose:
            self.output(json.dumps(event, ensure_ascii=False))
        if stage == "model" and outcome in {"validated", "failed"}:
            self.call_id = None

    def finish(self, status):
        for job in self.jobs.values():
            if not job["assessed"]:
                job["assessment_status"] = "not assessed"
                job.setdefault(
                    "skip_reason",
                    "Not processed before search stopped; see stop reason in search/run.json or failures.json.",
                )
            else:
                job["assessment_status"] = "assessed"
            self.save(f"search/jobs/{job['job_id']}.json", job)
        jobs = list(self.jobs.values())
        for name, field in (
            ("discovered-jobs", "discovery"),
            ("retrieval-results", "page"),
            ("extracted-job-facts", "facts"),
            ("fit-assessments", "judgment"),
        ):
            self.save(
                f"search/{name}.json", [dict(job_id=j["job_id"], value=j.get(field)) for j in jobs]
            )
        usage = [c for c in self.store.usage()["calls"] if c["id"] in self.receipts]
        summary = dict(
            jobs_discovered=len(jobs),
            jobs_retrieved=sum(j.get("page", {}).get("status") == "retrieved" for j in jobs),
            jobs_extracted=sum(bool(j.get("facts")) for j in jobs),
            jobs_assessed=sum(j["assessed"] for j in jobs),
            jobs_not_assessed=sum(not j["assessed"] for j in jobs),
            jobs_rejected_by_validation=sum(j["validation_rejected"] for j in jobs),
            jobs_with_technical_failures=sum(j["technical_failure"] for j in jobs),
            duration_seconds=round(time.monotonic() - self.started, 3),
            ledger_estimated_or_reserved_usd=sum(c["amount"] for c in usage),
            input_tokens=sum(c["input_tokens"] for c in usage),
            output_tokens=sum(c["output_tokens"] for c in usage),
            paid_requests=len(usage),
            failure_events_by_kind=dict(Counter(f["kind"] for f in self.failures)),
        )
        self.save("usage.json", usage)
        self.save("failures.json", self.failures)
        self.save("summary.json", summary)
        self.manifest.update(status=status, finished_at=utc_now())
        self.save("manifest.json", self.manifest)
        text = "\n".join(f"{key}: {value}" for key, value in summary.items())
        text += f"\nStatus: {status}\nArtifacts: {self.path.resolve()}\n"
        (self.path / "summary.txt").write_text(text, encoding="utf-8")
        results = "\n".join(
            f"{j['job_id']} | {j['discovery']['url']} | "
            f"{j.get('result', {}).get('category', 'unverified')} | {j['assessment_status']} | "
            + "; ".join(j.get("result", {}).get("reasons", [j.get("skip_reason", "")]))
            for j in jobs
        )
        (self.path / "search/results.txt").write_text(results, encoding="utf-8")
        self.output(text)
