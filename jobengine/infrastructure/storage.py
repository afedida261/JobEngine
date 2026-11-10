"""Local cache and transactional cost ledger; no external database service."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from jobengine.models import SavedOnboarding, utc_now


class BudgetExceeded(RuntimeError):
    pass


class StaleOnboarding(RuntimeError):
    pass


class Store:
    def __init__(self, root: str | Path = "data", budget: float = 10):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget = min(float(budget), 10.0)
        if self.budget <= 0:
            raise ValueError("Budget must be positive.")
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, created REAL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS costs (id TEXT PRIMARY KEY, operation TEXT, model TEXT, amount REAL, state TEXT, input_tokens INTEGER, output_tokens INTEGER, searches INTEGER, created TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS onboardings (id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)")

    def connect(self):
        return sqlite3.connect(self.root / "jobengine.sqlite", timeout=10)

    @staticmethod
    def key(value) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def cached(self, key: str, ttl_seconds: int = 86400):
        with self.connect() as db:
            row = db.execute("SELECT value, created FROM cache WHERE key=?", (key,)).fetchone()
        if row and datetime.now(timezone.utc).timestamp() - row[1] <= ttl_seconds:
            return json.loads(row[0])
        return None

    def cache(self, key: str, value) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                (
                    key,
                    json.dumps(value, ensure_ascii=False),
                    datetime.now(timezone.utc).timestamp(),
                ),
            )

    def reserve(self, operation: str, model: str, amount: float) -> str:
        identifier = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            total = db.execute("SELECT COALESCE(SUM(amount),0) FROM costs").fetchone()[0]
            if total + amount > self.budget:
                raise BudgetExceeded(
                    f"This request reserves ${amount:.3f}; ${max(0, self.budget - total):.3f} remains in the local budget. Use cached results or a smaller run."
                )
            db.execute(
                "INSERT INTO costs VALUES (?, ?, ?, ?, 'reserved', 0, 0, 0, ?)",
                (identifier, operation, model, amount, datetime.now(timezone.utc).isoformat()),
            )
        return identifier

    def settle(
        self, identifier: str, amount: float, input_tokens: int, output_tokens: int, searches: int
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE costs SET amount=?, state='estimated', input_tokens=?, output_tokens=?, searches=? WHERE id=?",
                (amount, input_tokens, output_tokens, searches, identifier),
            )

    def usage(self) -> dict:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute("SELECT * FROM costs ORDER BY created")]
        return {"budget": self.budget, "total": sum(r["amount"] for r in rows), "calls": rows}

    def save_run(self, identifier: str, payload: dict) -> Path:
        directory = self.root / "runs"
        directory.mkdir(exist_ok=True)
        # IDs are internally generated; never treat a user filename as a path.
        if not identifier.isalnum():
            raise ValueError("Invalid run ID.")
        target = directory / f"{identifier}.json"
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return target

    def load_run(self, identifier: str) -> dict | None:
        if not identifier.isalnum():
            raise ValueError("Invalid run ID.")
        path = self.root / "runs" / f"{identifier}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def save_onboarding(self, record: SavedOnboarding) -> SavedOnboarding:
        """Atomic snapshots; a stale browser cannot overwrite newer accepted steps."""
        if not record.session.id.isalnum():
            raise ValueError("Invalid onboarding ID.")
        updated = record.model_copy(deep=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision FROM onboardings WHERE id=?", (record.session.id,)
            ).fetchone()
            current_revision = row[0] if row else 0
            if record.revision != current_revision:
                raise StaleOnboarding(
                    "Newer onboarding progress was saved in another session. Reload the app to continue from it."
                )
            updated.revision = current_revision + 1
            updated.updated_at = utc_now()
            db.execute(
                "INSERT OR REPLACE INTO onboardings VALUES (?, ?, ?, ?)",
                (
                    updated.session.id,
                    updated.model_dump_json(),
                    updated.revision,
                    updated.updated_at,
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO app_state VALUES ('active_onboarding', ?)",
                (updated.session.id,),
            )
        return updated

    def load_onboarding(self, identifier: str | None = None) -> SavedOnboarding | None:
        with self.connect() as db:
            if identifier is None:
                row = db.execute(
                    "SELECT value FROM app_state WHERE key='active_onboarding'"
                ).fetchone()
                identifier = row[0] if row else None
            if not identifier:
                return None
            row = db.execute("SELECT payload FROM onboardings WHERE id=?", (identifier,)).fetchone()
        return SavedOnboarding.model_validate_json(row[0]) if row else None

    def list_onboardings(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, payload, updated_at FROM onboardings ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {
                "id": identifier,
                "label": json.loads(payload)["session"]["documents"][0]["label"],
                "updated_at": updated_at,
            }
            for identifier, payload, updated_at in rows
        ]

    def clear_active_onboarding(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM app_state WHERE key='active_onboarding'")

    def activate_onboarding(self, identifier: str) -> None:
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM onboardings WHERE id=?", (identifier,)).fetchone():
                raise ValueError("That saved onboarding was not found.")
            db.execute(
                "INSERT OR REPLACE INTO app_state VALUES ('active_onboarding', ?)", (identifier,)
            )

    def list_runs(self) -> list[dict]:
        directory = self.root / "runs"
        if not directory.exists():
            return []
        results = []
        for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.name.startswith("review"):
                continue
            run = json.loads(path.read_text(encoding="utf-8"))
            documents = run.get("profile", {}).get("documents", [])
            if not documents or any(
                word in documents[0]["label"].upper() for word in ("SYNTHETIC", "FICTIONAL")
            ):
                continue
            label = documents[0]["label"] + (
                " · validation run" if run.get("purpose") == "validation" else ""
            )
            results.append({"id": run["id"], "label": label, "created_at": run["created_at"]})
        return results
