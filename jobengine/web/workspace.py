"""Browser-scoped access to compatible snapshots, runs, bookmarks, and work status."""

from __future__ import annotations

import os
import sqlite3
import uuid

from django.conf import settings

from jobengine.infrastructure.storage import StaleOnboarding, Store
from jobengine.models import Run, SavedOnboarding, utc_now


class Workspace:
    def __init__(self, identifier: str):
        self.id = identifier
        self.store = Store(
            os.getenv("JOBENGINE_DATA_DIR", str(settings.DATA_DIR)),
            float(os.getenv("JOBENGINE_BUDGET_USD", "10")),
        )
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS web_workspaces (id TEXT PRIMARY KEY, active TEXT);
                CREATE TABLE IF NOT EXISTS web_candidates (
                    id TEXT PRIMARY KEY, workspace TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_runs (
                    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, candidate TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_bookmarks (
                    workspace TEXT, run TEXT, position INTEGER,
                    PRIMARY KEY (workspace, run, position));
                CREATE TABLE IF NOT EXISTS web_tasks (
                    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, candidate TEXT NOT NULL,
                    operation TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL,
                    destination TEXT NOT NULL, worker TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS web_active_task ON web_tasks(workspace)
                    WHERE status = 'running';
                CREATE TABLE IF NOT EXISTS web_task_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL,
                    message TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_applications (
                    workspace TEXT NOT NULL, url TEXT NOT NULL, run TEXT NOT NULL,
                    position INTEGER NOT NULL, applied_at TEXT NOT NULL,
                    PRIMARY KEY (workspace, url));
            """)
            db.execute("INSERT OR IGNORE INTO web_workspaces VALUES (?, NULL)", (self.id,))

    def current(self) -> SavedOnboarding | None:
        with self.store.connect() as db:
            row = db.execute("SELECT active FROM web_workspaces WHERE id=?", (self.id,)).fetchone()
        return self.load(row[0]) if row and row[0] else None

    def load(self, identifier: str) -> SavedOnboarding | None:
        with self.store.connect() as db:
            own = db.execute(
                "SELECT 1 FROM web_candidates WHERE id=? AND workspace=?", (identifier, self.id)
            ).fetchone()
        return self.store.load_onboarding(identifier) if own else None

    def activate(self, identifier: str | None):
        if identifier and not self.load(identifier):
            raise ValueError("That saved profile is not in this workspace.")
        with self.store.connect() as db:
            self._check_idle(db)
            db.execute("UPDATE web_workspaces SET active=? WHERE id=?", (identifier, self.id))

    def _check_idle(self, db, task_id=None):
        task = db.execute(
            "SELECT id FROM web_tasks WHERE workspace=? AND status='running'", (self.id,)
        ).fetchone()
        if task and task[0] != task_id:
            raise ValueError("Your current step is still running. Please wait for it to finish.")

    def save(self, record: SavedOnboarding, task_id=None) -> SavedOnboarding:
        updated = record.model_copy(deep=True)
        if not updated.session.id.isalnum():
            raise ValueError("Invalid profile identifier.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_idle(db, task_id)
            row = db.execute(
                "SELECT revision FROM onboardings WHERE id=?", (updated.session.id,)
            ).fetchone()
            owner = db.execute(
                "SELECT workspace FROM web_candidates WHERE id=?", (updated.session.id,)
            ).fetchone()
            if row and (not owner or owner[0] != self.id):
                raise ValueError("That profile belongs to another workspace.")
            if record.revision != (row[0] if row else 0):
                raise StaleOnboarding(
                    "Newer progress has been saved. Reload this page to continue."
                )
            updated.revision += 1
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
                "INSERT OR IGNORE INTO web_candidates VALUES (?, ?)", (updated.session.id, self.id)
            )
            db.execute(
                "UPDATE web_workspaces SET active=? WHERE id=?", (updated.session.id, self.id)
            )
        return updated

    def candidates(self):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT o.payload FROM onboardings o JOIN web_candidates c ON c.id=o.id "
                "WHERE c.workspace=? ORDER BY o.updated_at DESC",
                (self.id,),
            ).fetchall()
        return [SavedOnboarding.model_validate_json(row[0]) for row in rows]

    def add_run(self, run: Run, candidate: str):
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO web_runs VALUES (?, ?, ?)", (run.id, self.id, candidate)
            )

    def run(self, identifier: str | None) -> Run | None:
        if not identifier:
            return None
        with self.store.connect() as db:
            own = db.execute(
                "SELECT 1 FROM web_runs WHERE id=? AND workspace=?", (identifier, self.id)
            ).fetchone()
        payload = self.store.load_run(identifier) if own else None
        return Run.model_validate(payload) if payload else None

    def runs(self):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id FROM web_runs WHERE workspace=? ORDER BY rowid DESC", (self.id,)
            ).fetchall()
        return [run for row in rows if (run := self.run(row[0]))]

    def bookmarks(self):
        with self.store.connect() as db:
            return db.execute(
                "SELECT run, position FROM web_bookmarks WHERE workspace=? ORDER BY rowid DESC",
                (self.id,),
            ).fetchall()

    def toggle_bookmark(self, run: str, position: int):
        result = self.run(run)
        if not result or not 0 <= position < len(result.inspected_jobs or result.jobs):
            raise ValueError("That opportunity was not found.")
        with self.store.connect() as db:
            row = (self.id, run, position)
            if db.execute(
                "SELECT 1 FROM web_bookmarks WHERE workspace=? AND run=? AND position=?", row
            ).fetchone():
                db.execute(
                    "DELETE FROM web_bookmarks WHERE workspace=? AND run=? AND position=?", row
                )
            else:
                db.execute("INSERT INTO web_bookmarks VALUES (?, ?, ?)", row)

    def task(self, identifier=None):
        with self.store.connect() as db:
            db.row_factory = sqlite3.Row
            if identifier:
                row = db.execute(
                    "SELECT * FROM web_tasks WHERE id=? AND workspace=?", (identifier, self.id)
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM web_tasks WHERE workspace=? AND status='running'", (self.id,)
                ).fetchone()
        return dict(row) if row else None

    def task_events(self, identifier):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT e.message, e.created FROM web_task_events e "
                "JOIN web_tasks t ON t.id=e.task WHERE t.workspace=? AND t.id=? "
                "ORDER BY e.sequence DESC LIMIT 12",
                (self.id, identifier),
            ).fetchall()
        return [{"message": row[0], "created": row[1]} for row in reversed(rows)]

    def applications(self):
        with self.store.connect() as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM web_applications WHERE workspace=? ORDER BY applied_at DESC",
                    (self.id,),
                )
            ]

    def set_application(self, identifier, position, *, applied):
        run = self.run(identifier)
        jobs = (run.inspected_jobs or run.jobs) if run else []
        if not 0 <= position < len(jobs):
            raise ValueError("That opportunity was not found.")
        url = jobs[position].discovery.url
        with self.store.connect() as db:
            if applied:
                db.execute(
                    "INSERT OR IGNORE INTO web_applications VALUES (?, ?, ?, ?, ?)",
                    (self.id, url, identifier, position, utc_now()),
                )
            else:
                db.execute(
                    "DELETE FROM web_applications WHERE workspace=? AND url=?", (self.id, url)
                )

    def create_task(self, record, operation, destination, worker):
        identifier = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_idle(db)
            row = db.execute(
                "SELECT revision FROM onboardings WHERE id=?", (record.session.id,)
            ).fetchone()
            if not self.load(record.session.id) or not row or row[0] != record.revision:
                raise StaleOnboarding("Your progress changed. Reload before continuing.")
            db.execute(
                "INSERT INTO web_tasks VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (
                    identifier,
                    self.id,
                    record.session.id,
                    operation,
                    "Getting everything ready…",
                    destination,
                    worker,
                    utc_now(),
                ),
            )
            db.execute(
                "INSERT INTO web_task_events (task, message, created) VALUES (?, ?, ?)",
                (identifier, "I'm getting your next step ready.", utc_now()),
            )
        return identifier

    def update_task(self, identifier, **fields):
        allowed = {"status", "message", "destination"}
        if not fields.keys() <= allowed:
            raise ValueError("Invalid task update.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT message FROM web_tasks WHERE id=? AND workspace=?", (identifier, self.id)
            ).fetchone()
            if previous and "message" in fields and fields["message"] != previous[0]:
                db.execute(
                    "INSERT INTO web_task_events (task, message, created) VALUES (?, ?, ?)",
                    (identifier, fields["message"], utc_now()),
                )
            db.execute(
                "UPDATE web_tasks SET "
                + ", ".join(f"{key}=?" for key in fields)
                + ", updated=? WHERE id=? AND workspace=?",
                (*fields.values(), utc_now(), identifier, self.id),
            )

    def legacy_candidates(self):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT payload FROM onboardings WHERE id NOT IN (SELECT id FROM web_candidates) "
                "ORDER BY updated_at DESC"
            ).fetchall()
        return [SavedOnboarding.model_validate_json(row[0]) for row in rows]

    def import_legacy(self, identifier):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_idle(db)
            row = db.execute("SELECT payload FROM onboardings WHERE id=?", (identifier,)).fetchone()
            if (
                not row
                or db.execute("SELECT 1 FROM web_candidates WHERE id=?", (identifier,)).fetchone()
            ):
                raise ValueError("That local profile is no longer available to import.")
            record = SavedOnboarding.model_validate_json(row[0])
            db.execute("INSERT INTO web_candidates VALUES (?, ?)", (identifier, self.id))
            db.execute("UPDATE web_workspaces SET active=? WHERE id=?", (identifier, self.id))
        if record.last_run_id:
            payload = self.store.load_run(record.last_run_id)
            if payload:
                self.add_run(Run.model_validate(payload), identifier)
        return record
