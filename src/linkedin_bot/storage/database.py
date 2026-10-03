"""One connection, owned by one worker; no network operations in transactions."""

import asyncio
import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import TypeVar

from linkedin_bot.domain.models import (
    ActionSnapshot,
    Artifact,
    CandidateFact,
    CandidateProfile,
    CollectionPage,
    Policy,
    SearchPreferences,
    SourceObservation,
    WorkItem,
    digest,
    identifier,
    now,
)
from linkedin_bot.runtime.privacy import BoundaryError, private_directory, safe_file

T = TypeVar("T")


class RevisionConflict(ValueError):
    pass


class SchemaError(ValueError):
    pass


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def apply_migrations(connection: sqlite3.Connection, migrations: list[str]) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
    )
    rows = connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
    applied = list(rows)
    if [row[0] for row in applied] != list(range(1, len(applied) + 1)):
        raise SchemaError("nonsequential_schema")
    if len(applied) > len(migrations):
        raise SchemaError("newer_schema")
    for version, checksum in applied:
        if checksum != digest(migrations[version - 1].encode()):
            raise SchemaError("migration_checksum_changed")
    for version in range(len(applied) + 1, len(migrations) + 1):
        script = migrations[version - 1]
        with transaction(connection):
            # Project migrations are plain SQL, without triggers or embedded semicolons.
            for statement in script.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?,?)", (version, digest(script.encode()))
            )


def migration_scripts() -> list[str]:
    entries = sorted(
        files("linkedin_bot.storage").joinpath("migrations").iterdir(),
        key=lambda entry: entry.name,
    )
    return [entry.read_text() for entry in entries if entry.name.endswith(".sql")]


class Database:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storage")
        self.connection: sqlite3.Connection | None = None

    async def call(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def invoke() -> T:
            if self.connection is None:
                raise RuntimeError("storage_not_open")
            return operation(self.connection)

        return await asyncio.get_running_loop().run_in_executor(self.pool, invoke)

    async def open(self) -> None:
        def initialize() -> None:
            private_directory(self.root)
            path = safe_file(self.root, "state.sqlite3")
            connection = sqlite3.connect(path, isolation_level=None, timeout=5)
            self.connection = connection
            path.chmod(0o600)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            migrations = migration_scripts()
            present = connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='schema_migrations'"
            ).fetchone()[0]
            current = (
                connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
                if present
                else 0
            )
            if current < len(migrations) and path.stat().st_size:
                backup_path = safe_file(self.root, "pre-migration.sqlite3")
                with sqlite3.connect(backup_path) as backup:
                    connection.backup(backup)
                backup_path.chmod(0o600)
            apply_migrations(connection, migrations)
            connection.execute("INSERT OR IGNORE INTO profile VALUES (1,?,1)", (identifier(),))
            connection.execute(
                "INSERT OR IGNORE INTO policies VALUES (?,?)", (1, Policy().model_dump_json())
            )
            connection.execute(
                "INSERT OR IGNORE INTO preferences VALUES (?,?)",
                (1, SearchPreferences().model_dump_json()),
            )

        await asyncio.get_running_loop().run_in_executor(self.pool, initialize)

    async def close(self) -> None:
        if self.connection is not None:
            await self.call(lambda connection: connection.close())
            self.connection = None
        self.pool.shutdown(wait=True)

    async def profile(self) -> CandidateProfile:
        def read(connection: sqlite3.Connection) -> CandidateProfile:
            profile_id, revision = connection.execute("SELECT id,revision FROM profile").fetchone()
            fact_ids = tuple(row[0] for row in connection.execute("SELECT DISTINCT id FROM facts"))
            baseline = connection.execute(
                "SELECT id FROM artifacts WHERE json_extract(snapshot,'$.kind')='baseline_cv' "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            return CandidateProfile(
                id=profile_id,
                revision=revision,
                fact_ids=fact_ids,
                baseline_artifact_id=baseline[0] if baseline else None,
            )

        return await self.call(read)

    async def policy(self) -> Policy:
        def read(connection: sqlite3.Connection) -> Policy:
            policy = Policy.model_validate_json(
                connection.execute(
                    "SELECT snapshot FROM policies ORDER BY revision DESC LIMIT 1"
                ).fetchone()[0]
            )
            paused, audit = connection.execute(
                "SELECT paused,restore_audit_required FROM runtime_state WHERE id=1"
            ).fetchone()
            return policy.model_copy(
                update={"paused": bool(paused), "restore_audit_required": bool(audit)}
            )

        return await self.call(read)

    async def pause(self, paused: bool) -> None:
        await self.call(
            lambda c: c.execute("UPDATE runtime_state SET paused=? WHERE id=1", (int(paused),))
        )

    async def save_fact(self, fact: CandidateFact, expected_revision: int = 0) -> int:
        def write(connection: sqlite3.Connection) -> int:
            with transaction(connection):
                latest = connection.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM facts WHERE id=?", (fact.id,)
                ).fetchone()[0]
                if latest != expected_revision or fact.revision != latest + 1:
                    raise RevisionConflict("fact_revision_conflict")
                connection.execute(
                    "INSERT INTO facts VALUES (?,?,?,?)",
                    (
                        fact.id,
                        fact.revision,
                        fact.status,
                        fact.model_dump_json(),
                    ),
                )
                connection.execute("UPDATE profile SET revision=revision+1 WHERE slot=1")
                # Conservative invalidation: all undispatched preparations must be rebuilt.
                connection.execute(
                    "UPDATE actions SET state='blocked',reason_code='stale_profile' "
                    "WHERE state IN ('proposed','ready','awaiting_approval')"
                )
                connection.execute("UPDATE approvals SET invalidated=1 WHERE consumed_at IS NULL")
                return int(
                    connection.execute("SELECT revision FROM profile WHERE slot=1").fetchone()[0]
                )

        return await self.call(write)

    async def facts(self, confirmed_only: bool = False) -> tuple[CandidateFact, ...]:
        def read(connection: sqlite3.Connection) -> tuple[CandidateFact, ...]:
            rows = connection.execute(
                "SELECT f.snapshot FROM facts f JOIN "
                "(SELECT id,MAX(revision) revision FROM facts GROUP BY id) latest "
                "ON f.id=latest.id AND f.revision=latest.revision ORDER BY f.id"
            )
            facts = tuple(CandidateFact.model_validate_json(row[0]) for row in rows)
            return tuple(f for f in facts if f.status == "confirmed") if confirmed_only else facts

        return await self.call(read)

    async def preferences(self) -> SearchPreferences:
        row = await self.call(
            lambda c: c.execute(
                "SELECT snapshot FROM preferences ORDER BY revision DESC LIMIT 1"
            ).fetchone()
        )
        return SearchPreferences.model_validate_json(row[0])

    async def save_preferences(
        self, preferences: SearchPreferences, expected_revision: int
    ) -> None:
        def write(connection: sqlite3.Connection) -> None:
            with transaction(connection):
                latest = connection.execute("SELECT MAX(revision) FROM preferences").fetchone()[0]
                if latest != expected_revision or preferences.revision != latest + 1:
                    raise RevisionConflict("preferences_revision_conflict")
                connection.execute(
                    "INSERT INTO preferences VALUES (?,?)",
                    (
                        preferences.revision,
                        preferences.model_dump_json(),
                    ),
                )
                connection.execute(
                    "UPDATE actions SET state='blocked',reason_code='stale_preferences' "
                    "WHERE state IN ('proposed','ready','awaiting_approval')"
                )
                connection.execute("UPDATE approvals SET invalidated=1 WHERE consumed_at IS NULL")

        await self.call(write)

    async def save_action(self, action: ActionSnapshot, expected_revision: int = 0) -> None:
        def write(connection: sqlite3.Connection) -> None:
            with transaction(connection):
                row = connection.execute(
                    "SELECT revision,state,logical_key FROM actions WHERE id=?", (action.id,)
                ).fetchone()
                latest = row[0] if row else 0
                if latest != expected_revision or action.revision != latest + 1:
                    raise RevisionConflict("action_revision_conflict")
                if row and (
                    row[1] in ("executing", "unknown", "succeeded", "cancelled")
                    or row[2] != action.logical_key
                ):
                    raise RevisionConflict("effect_locked")
                profile_revision = connection.execute("SELECT revision FROM profile").fetchone()[0]
                preferences_revision = connection.execute(
                    "SELECT MAX(revision) FROM preferences"
                ).fetchone()[0]
                policy_revision = connection.execute(
                    "SELECT MAX(revision) FROM policies"
                ).fetchone()[0]
                if (
                    action.profile_revision,
                    action.preferences_revision,
                    action.policy_revision,
                ) != (
                    profile_revision,
                    preferences_revision,
                    policy_revision,
                ):
                    raise RevisionConflict("stale_inputs")
                for fact_id, revision in action.fact_revisions:
                    fact = connection.execute(
                        "SELECT revision,status FROM facts WHERE id=? "
                        "ORDER BY revision DESC LIMIT 1",
                        (fact_id,),
                    ).fetchone()
                    if fact != (revision, "confirmed"):
                        raise RevisionConflict("unconfirmed_or_stale_fact")
                for checksum in action.artifact_hashes:
                    artifact = connection.execute(
                        "SELECT relative_path FROM artifacts WHERE sha256=?", (checksum,)
                    ).fetchone()
                    if artifact is None:
                        raise RevisionConflict("missing_artifact")
                    path = safe_file(self.root, artifact[0])
                    if not path.is_file() or digest(path.read_bytes()) != checksum:
                        raise RevisionConflict("changed_artifact")
                if row:
                    connection.execute(
                        "UPDATE actions SET revision=?,digest=?,state='proposed',reason_code=NULL "
                        "WHERE id=?",
                        (action.revision, action.content_digest, action.id),
                    )
                    connection.execute(
                        "UPDATE approvals SET invalidated=1 WHERE action_id=?", (action.id,)
                    )
                else:
                    connection.execute(
                        "INSERT INTO actions VALUES (?,?,?,?,?,NULL)",
                        (
                            action.id,
                            action.revision,
                            action.logical_key,
                            "proposed",
                            action.content_digest,
                        ),
                    )
                connection.execute(
                    "INSERT INTO action_revisions VALUES (?,?,?,?)",
                    (
                        action.id,
                        action.revision,
                        action.model_dump_json(),
                        action.content_digest,
                    ),
                )

        await self.call(write)

    async def block_action(self, action: ActionSnapshot, reason: str) -> None:
        def write(connection: sqlite3.Connection) -> None:
            updated = connection.execute(
                "UPDATE actions SET state='blocked',reason_code=? "
                "WHERE id=? AND revision=? AND digest=? AND state='proposed'",
                (reason, action.id, action.revision, action.content_digest),
            ).rowcount
            if updated != 1:
                raise RevisionConflict("stale_action")

        await self.call(write)

    async def recover(self) -> dict[str, int]:
        def write(connection: sqlite3.Connection) -> dict[str, int]:
            with transaction(connection):
                # A marker committed before a click is deliberately treated as ambiguous.
                unknown = connection.execute(
                    "UPDATE actions SET state='unknown',reason_code='interrupted_dispatch' "
                    "WHERE state='executing' AND EXISTS (SELECT 1 FROM attempts "
                    "WHERE action_id=actions.id AND revision=actions.revision "
                    "AND dispatch_at IS NOT NULL)"
                ).rowcount
                connection.execute(
                    "UPDATE attempts SET outcome='unknown',reason_code='interrupted_dispatch' "
                    "WHERE outcome IS NULL AND dispatch_at IS NOT NULL"
                )
                prepared = connection.execute(
                    "UPDATE actions SET state='proposed',reason_code='interrupted_before_dispatch' "
                    "WHERE state='executing'"
                ).rowcount
                connection.execute(
                    "UPDATE reservations SET released=1 WHERE attempt_id IN "
                    "(SELECT id FROM attempts WHERE dispatch_at IS NULL AND outcome IS NULL)"
                )
                connection.execute(
                    "UPDATE attempts SET outcome='failed',"
                    "reason_code='interrupted_before_dispatch' "
                    "WHERE outcome IS NULL AND dispatch_at IS NULL"
                )
                work = connection.execute(
                    "UPDATE work_items SET state='pending',reason_code='interrupted_read' "
                    "WHERE state='running'"
                ).rowcount
                return {
                    "unknown_actions": unknown,
                    "reprepared_actions": prepared,
                    "recovered_work": work,
                }

        return await self.call(write)

    async def schedule(
        self,
        workflow: str,
        payload: str,
        due_at: datetime,
        interval_seconds: int,
        *,
        recurring: bool = True,
    ) -> None:
        if due_at.utcoffset() is None or interval_seconds <= 0:
            raise ValueError("invalid_schedule")
        due_at = due_at.astimezone(now().tzinfo)
        await self.call(
            lambda c: c.execute(
                "INSERT INTO work_items "
                "(id,workflow,payload,interval_seconds,due_at,deadline,recurring,state) "
                "VALUES (?,?,?,?,?,?,?,'pending') ON CONFLICT(workflow) DO UPDATE SET "
                "payload=excluded.payload,interval_seconds=excluded.interval_seconds,"
                "due_at=excluded.due_at,deadline=excluded.deadline,recurring=excluded.recurring,"
                "retry_count=0,state='pending'",
                (
                    identifier(),
                    workflow,
                    payload,
                    interval_seconds,
                    due_at.isoformat(),
                    (due_at + timedelta(minutes=15)).isoformat(),
                    int(recurring),
                ),
            )
        )

    async def claim(self, at: datetime, workflow: str | None = None) -> WorkItem | None:
        if at.utcoffset() is None:
            raise ValueError("naive_schedule_time")
        at = at.astimezone(now().tzinfo)

        def write(connection: sqlite3.Connection) -> WorkItem | None:
            with transaction(connection):
                if connection.execute("SELECT paused FROM runtime_state").fetchone()[0]:
                    return None
                row = connection.execute(
                    "SELECT id,workflow,due_at,attempts,checkpoint,payload FROM work_items "
                    "WHERE state='pending' AND due_at<=? AND (? IS NULL OR workflow=?) "
                    "ORDER BY due_at,id LIMIT 1",
                    (at.isoformat(), workflow, workflow),
                ).fetchone()
                if row is None:
                    return None
                deadline = at + timedelta(minutes=15)
                connection.execute(
                    "UPDATE work_items SET state='running',attempts=attempts+1,"
                    "retry_count=retry_count+1,deadline=? WHERE id=?",
                    (deadline.isoformat(), row[0]),
                )
                return WorkItem(
                    id=row[0],
                    workflow=row[1],
                    due_at=datetime.fromisoformat(row[2]),
                    deadline=deadline,
                    attempts=row[3] + 1,
                    checkpoint=row[4],
                    payload=row[5],
                )

        return await self.call(write)

    async def persist_page(self, work: WorkItem, page: CollectionPage, at: datetime) -> None:
        def write(connection: sqlite3.Connection) -> None:
            with transaction(connection):
                row = connection.execute(
                    "SELECT state,attempts,interval_seconds,recurring FROM work_items WHERE id=?",
                    (work.id,),
                ).fetchone()
                if row is None or row[:2] != ("running", work.attempts):
                    raise RevisionConflict("stale_work_claim")
                if page.coverage == "unavailable":
                    raise ValueError("unavailable_page_cannot_advance")
                for observation in page.items:
                    timestamp = observation.observed_at.isoformat()
                    connection.execute(
                        "INSERT INTO observations VALUES (?,?,?,?,?,?) ON CONFLICT "
                        "(source,external_id,content_hash) "
                        "DO UPDATE SET last_seen=excluded.last_seen",
                        (
                            observation.source,
                            observation.external_id,
                            observation.content_hash,
                            observation.model_dump_json(),
                            timestamp,
                            timestamp,
                        ),
                    )
                # Schedule from completion, coalescing every missed interval into one read.
                due = at + timedelta(seconds=row[2])
                connection.execute(
                    "UPDATE work_items SET state=?,due_at=?,deadline=?,checkpoint=?,retry_count=0,"
                    "coverage=?,reason_code=NULL WHERE id=?",
                    (
                        "pending" if row[3] else "completed",
                        due.isoformat(),
                        (due + timedelta(minutes=15)).isoformat(),
                        page.next_cursor,
                        page.coverage,
                        work.id,
                    ),
                )

        await self.call(write)

    async def fail_work(self, work: WorkItem, reason: str, transient: bool, at: datetime) -> None:
        def write(connection: sqlite3.Connection) -> None:
            with transaction(connection):
                row = connection.execute(
                    "SELECT state,attempts,retry_count FROM work_items WHERE id=?", (work.id,)
                ).fetchone()
                if row is None or row[:2] != ("running", work.attempts):
                    raise RevisionConflict("stale_work_claim")
                # Retry budget resets after success; attempt IDs remain monotonic.
                retry = transient and row[2] < 3
                due = at + timedelta(seconds=30 * 2 ** min(work.attempts, 3))
                connection.execute(
                    "UPDATE work_items SET state=?,due_at=?,reason_code=?,coverage='unavailable' "
                    "WHERE id=?",
                    ("pending" if retry else "blocked", due.isoformat(), reason, work.id),
                )

        await self.call(write)

    async def opportunities(self, since: datetime | None) -> tuple[SourceObservation, ...]:
        """Latest version of each posting; `since` drops postings not seen recently."""

        def read(connection: sqlite3.Connection) -> tuple[SourceObservation, ...]:
            rows = connection.execute(
                "SELECT snapshot,last_seen FROM observations o WHERE last_seen=("
                "SELECT MAX(last_seen) FROM observations "
                "WHERE source=o.source AND external_id=o.external_id) "
                "GROUP BY source,external_id ORDER BY source,external_id"
            )
            return tuple(
                SourceObservation.model_validate_json(snapshot)
                for snapshot, last_seen in rows
                if since is None or datetime.fromisoformat(last_seen) >= since
            )

        return await self.call(read)

    async def status(self) -> dict[str, object]:
        def read(connection: sqlite3.Connection) -> dict[str, object]:
            return {
                "schema_version": connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0],
                "profile_revision": connection.execute("SELECT revision FROM profile").fetchone()[
                    0
                ],
                "paused": bool(
                    connection.execute("SELECT paused FROM runtime_state").fetchone()[0]
                ),
                "facts": connection.execute("SELECT count(DISTINCT id) FROM facts").fetchone()[0],
                "observations": connection.execute("SELECT count(*) FROM observations").fetchone()[
                    0
                ],
                "artifacts": connection.execute("SELECT count(*) FROM artifacts").fetchone()[0],
                "work": [
                    dict(
                        zip(("workflow", "state", "due_at", "coverage", "reason"), row, strict=True)
                    )
                    for row in connection.execute(
                        "SELECT workflow,state,due_at,coverage,reason_code FROM work_items"
                    )
                ],
                "unknown_actions": connection.execute(
                    "SELECT count(*) FROM actions WHERE state='unknown'"
                ).fetchone()[0],
            }

        return await self.call(read)

    async def save_artifact(self, content: bytes, manifest: Artifact) -> Artifact:
        if digest(content) != manifest.sha256:
            raise BoundaryError("artifact_checksum")

        def publish(connection: sqlite3.Connection) -> Artifact:
            path = safe_file(self.root, manifest.relative_path)
            if manifest.relative_path != f"artifacts/{manifest.id}":
                raise BoundaryError("artifact_path")
            private_directory(path.parent)
            if path.exists():
                raise BoundaryError("immutable_artifact")
            for fact_id, revision in manifest.fact_revisions:
                row = connection.execute(
                    "SELECT revision,status FROM facts WHERE id=? ORDER BY revision DESC LIMIT 1",
                    (fact_id,),
                ).fetchone()
                if row != (revision, "confirmed"):
                    raise RevisionConflict("artifact_unconfirmed_fact")
            current = connection.execute("SELECT revision FROM profile").fetchone()[0]
            if manifest.profile_revision != current:
                raise RevisionConflict("artifact_stale_profile")
            temp = path.with_suffix(".tmp")
            with temp.open("xb") as output:
                temp.chmod(0o600)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            temp.rename(path)
            connection.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?)",
                (
                    manifest.id,
                    manifest.relative_path,
                    manifest.sha256,
                    manifest.model_dump_json(),
                ),
            )
            return manifest

        return await self.call(publish)

    async def inspect_artifacts(self) -> dict[str, int]:
        def inspect(connection: sqlite3.Connection) -> dict[str, int]:
            manifests = list(connection.execute("SELECT relative_path,sha256 FROM artifacts"))
            known = {row[0] for row in manifests}
            corrupt = 0
            for relative, checksum in manifests:
                path = safe_file(self.root, relative)
                if not path.is_file() or digest(path.read_bytes()) != checksum:
                    corrupt += 1
            directory = safe_file(self.root, "artifacts")
            orphans = (
                sum(str(path.relative_to(self.root)) not in known for path in directory.iterdir())
                if directory.exists()
                else 0
            )
            return {"corrupt_artifacts": corrupt, "orphan_artifacts": orphans}

        return await self.call(inspect)

    async def backup(self) -> Path:
        def snapshot(connection: sqlite3.Connection) -> Path:
            parent = safe_file(self.root, "backups")
            private_directory(parent)
            temporary = parent / f".{identifier()}.tmp"
            private_directory(temporary)
            destination = parent / identifier()
            try:
                database_path = temporary / "state.sqlite3"
                with sqlite3.connect(database_path) as copy:
                    connection.backup(copy)
                database_path.chmod(0o600)
                checksums = {"state.sqlite3": digest(database_path.read_bytes())}
                for relative, checksum in connection.execute(
                    "SELECT relative_path,sha256 FROM artifacts"
                ):
                    source = safe_file(self.root, relative)
                    if digest(source.read_bytes()) != checksum:
                        raise BoundaryError("backup_artifact_checksum")
                    target = safe_file(temporary, relative)
                    private_directory(target.parent)
                    shutil.copyfile(source, target)
                    target.chmod(0o600)
                    checksums[relative] = checksum
                manifest = temporary / "manifest.json"
                manifest.write_text(
                    json.dumps(
                        {
                            "app_version": "0.1.0",
                            "schema_version": connection.execute(
                                "SELECT MAX(version) FROM schema_migrations"
                            ).fetchone()[0],
                            "checksums": checksums,
                        },
                        sort_keys=True,
                    )
                )
                manifest.chmod(0o600)
                temporary.rename(destination)
                return destination
            except BaseException:
                shutil.rmtree(temporary)
                raise

        return await self.call(snapshot)
