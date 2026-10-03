import asyncio
import json
import logging
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from linkedin_bot.domain.models import (
    ActionSnapshot,
    Artifact,
    CandidateFact,
    CollectionPage,
    Policy,
    SearchPreferences,
    SourceObservation,
    digest,
    identifier,
    now,
)
from linkedin_bot.domain.policy import block_reasons
from linkedin_bot.runtime.config import Config, InferenceConfig, load_config
from linkedin_bot.runtime.locking import AlreadyRunning, RuntimeLock
from linkedin_bot.runtime.privacy import BoundaryError, RedactedFormatter, safe_file
from linkedin_bot.storage.database import Database, RevisionConflict, SchemaError, apply_migrations
from linkedin_bot.storage.restore import restore_backup
from linkedin_bot.workflows.executor import DraftExecutor


def action(**changes):
    return ActionSnapshot(
        **{
            "kind": "send_reply",
            "target": "synthetic-recipient",
            "logical_key": "reply:thread:inbound",
            "content": "Synthetic draft",
            "profile_revision": 1,
            "preferences_revision": 1,
            "policy_revision": 1,
            **changes,
        }
    )


def observation(text="Internship"):
    return SourceObservation(
        source="greenhouse",
        external_id="123",
        title=text,
        url="https://example.org/jobs/123",
        content_hash=digest(text.encode()),
    )


def test_fact_revisions_and_confirmation(tmp_path):
    async def scenario():
        db = Database(tmp_path / "data")
        try:
            await db.open()
            fact = CandidateFact(kind="skill", value="Python", source_reference="synthetic-cv")
            assert await db.save_fact(fact) == 2
            assert await db.facts(confirmed_only=True) == ()
            confirmed = CandidateFact(
                **{**fact.model_dump(), "revision": 2, "status": "confirmed", "confirmed_at": now()}
            )
            await db.save_fact(confirmed, expected_revision=1)
            assert (await db.facts(confirmed_only=True))[0] == confirmed
            with pytest.raises(RevisionConflict):
                await db.save_fact(confirmed, expected_revision=1)
            await db.close()
            db = Database(tmp_path / "data")
            await db.open()
            assert (await db.facts())[0].revision == 2
        finally:
            await db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "confirmed", "value": None, "confirmed_at": now()},
        {"status": "confirmed", "value": "Python"},
        {"status": "unconfirmed", "confirmed_at": now()},
        {"status": "confirmed", "value": "Python", "confirmed_at": datetime(2026, 1, 1)},
    ],
)
def test_invalid_fact_confirmation(fields):
    with pytest.raises(ValidationError):
        CandidateFact(kind="skill", source_reference="synthetic", **fields)


def test_action_revisions_and_invalidation(tmp_path):
    async def scenario():
        db = Database(tmp_path)
        try:
            await db.open()
            original = action()
            await db.save_action(original)
            await db.call(
                lambda c: c.execute(
                    "INSERT INTO approvals VALUES (?,?,?,?,?,0,NULL)",
                    (
                        identifier(),
                        original.id,
                        1,
                        original.content_digest,
                        (now() + timedelta(days=1)).isoformat(),
                    ),
                )
            )
            revised = action(id=original.id, revision=2, content="Changed draft")
            await db.save_action(revised, 1)
            assert original.content_digest != revised.content_digest
            assert await db.call(
                lambda c: c.execute("SELECT invalidated FROM approvals").fetchone()
            ) == (1,)
            assert await db.call(
                lambda c: c.execute("SELECT count(*) FROM action_revisions").fetchone()
            ) == (2,)
            with pytest.raises(RevisionConflict):
                await db.save_action(revised, 1)
            with pytest.raises(sqlite3.IntegrityError):
                await db.save_action(action())
            await db.save_preferences(SearchPreferences(revision=2, roles=("intern",)), 1)
            assert await db.call(lambda c: c.execute("SELECT state FROM actions").fetchone()) == (
                "blocked",
            )
            with pytest.raises(RevisionConflict):
                await db.save_action(action(logical_key="new-key"))
            reasons = await DraftExecutor(db).evaluate(revised)
            assert "draft_only" in reasons and "zero_outbound_cap" in reasons
        finally:
            await db.close()

    asyncio.run(scenario())


def test_unconfirmed_facts_cannot_support_actions(tmp_path):
    async def scenario():
        db = Database(tmp_path)
        try:
            await db.open()
            fact = CandidateFact(kind="authorization", value=None, source_reference="synthetic")
            revision = await db.save_fact(fact)
            with pytest.raises(RevisionConflict, match="unconfirmed"):
                await db.save_action(
                    action(profile_revision=revision, fact_revisions=((fact.id, 1),))
                )
        finally:
            await db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind",
    [
        "submit_application",
        "send_reply",
        "send_outreach",
        "send_followup",
        "send_connection_invite",
        "accept_connection",
        "decline_connection",
    ],
)
def test_every_outbound_kind_blocked(kind):
    assert {"draft_only", "zero_outbound_cap", "capability_not_released"} <= set(
        block_reasons(action(kind=kind), Policy())
    )
    with pytest.raises(ValidationError):
        Policy(mode="rules_based", communication_cap=10)


def test_recovery_dispatch_boundaries_and_duplicate_block(tmp_path):
    async def scenario():
        db = Database(tmp_path)
        try:
            await db.open()
            dispatched, before = action(), action(logical_key="before")
            for proposed in (dispatched, before):
                await db.save_action(proposed)
                attempt = identifier()

                def crash_state(c, proposed=proposed, attempt=attempt):
                    c.execute("UPDATE actions SET state='executing' WHERE id=?", (proposed.id,))
                    c.execute(
                        "INSERT INTO attempts VALUES (?,?,?,?,?,NULL,NULL,NULL)",
                        (
                            attempt,
                            proposed.id,
                            1,
                            now().isoformat(),
                            now().isoformat() if proposed == dispatched else None,
                        ),
                    )
                    c.execute(
                        "INSERT INTO reservations VALUES (?,?,?,0)",
                        (attempt, "2026-10-01", "communication"),
                    )

                await db.call(crash_state)
            await db.close()
            db = Database(tmp_path)
            await db.open()
            assert await db.recover() == {
                "unknown_actions": 1,
                "reprepared_actions": 1,
                "recovered_work": 0,
            }
            states = await db.call(
                lambda c: dict(c.execute("SELECT logical_key,state FROM actions"))
            )
            assert states == {"reply:thread:inbound": "unknown", "before": "proposed"}
            with pytest.raises(RevisionConflict, match="effect_locked"):
                await db.save_action(action(id=dispatched.id, revision=2), 1)
            with pytest.raises(sqlite3.IntegrityError):
                await db.save_action(action())
            assert await db.call(
                lambda c: c.execute(
                    "SELECT released FROM reservations r JOIN attempts a ON a.id=r.attempt_id "
                    "WHERE a.action_id=?",
                    (dispatched.id,),
                ).fetchone()
            ) == (0,)
            assert await db.call(
                lambda c: c.execute(
                    "SELECT released FROM reservations r JOIN attempts a ON a.id=r.attempt_id "
                    "WHERE a.action_id=?",
                    (before.id,),
                ).fetchone()
            ) == (1,)
            assert (await db.recover())["unknown_actions"] == 0
        finally:
            await db.close()

    asyncio.run(scenario())


def test_scheduler_coalesces_pause_dedup_and_checkpoint_atomicity(tmp_path):
    async def scenario():
        db = Database(tmp_path)
        try:
            await db.open()
            at = now()
            await db.schedule("board", "{}", at - timedelta(days=10), 3600)
            await db.pause(True)
            assert await db.claim(at) is None
            await db.pause(False)
            claims = await asyncio.gather(db.claim(at), db.claim(at))
            work = next(work for work in claims if work is not None)
            assert sum(work is not None for work in claims) == 1
            assert work.deadline > at
            with pytest.raises(ValueError, match="unavailable"):
                await db.persist_page(work, CollectionPage(items=(), coverage="unavailable"), at)
            assert (await db.status())["observations"] == 0
            page = CollectionPage(
                items=(observation(), observation()), coverage="partial", next_cursor="20"
            )
            await db.persist_page(work, page, at)
            assert (await db.status())["observations"] == 1
            assert await db.claim(at) is None
            await db.close()
            db = Database(tmp_path)
            await db.open()
            next_work = await db.claim(at + timedelta(hours=1))
            assert next_work.checkpoint == "20"
            changed = CollectionPage(items=(observation("Changed"),), coverage="complete")
            with pytest.raises(RevisionConflict):
                await db.persist_page(work, changed, at)
            await db.persist_page(next_work, changed, at + timedelta(hours=1))
            assert (await db.status())["observations"] == 2
        finally:
            await db.close()

    asyncio.run(scenario())


def test_read_retry_budget_resets_after_success(tmp_path):
    async def scenario():
        db = Database(tmp_path)
        try:
            await db.open()
            at = now()
            await db.schedule("board", "{}", at, 3600)
            for _ in range(2):
                work = await db.claim(at)
                await db.fail_work(work, "timeout", True, at)
                at += timedelta(minutes=10)
            work = await db.claim(at)
            await db.persist_page(work, CollectionPage(items=(), coverage="complete"), at)
            at += timedelta(hours=2)
            for _ in range(3):
                work = await db.claim(at)
                await db.fail_work(work, "timeout", True, at)
                at += timedelta(minutes=10)
            assert await db.claim(at) is None
            assert (await db.status())["work"][0]["state"] == "blocked"
            await db.schedule("oneshot", "{}", at, 3600, recurring=False)
            work = await db.claim(at)
            await db.persist_page(work, CollectionPage(items=(), coverage="complete"), at)
            assert await db.claim(at + timedelta(days=1)) is None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_interrupted_read_recovery(tmp_path):
    async def scenario():
        db = Database(tmp_path)
        try:
            await db.open()
            at = now()
            await db.schedule("board", "{}", at, 3600)
            await db.claim(at)
            await db.close()
            db = Database(tmp_path)
            await db.open()
            assert (await db.recover())["recovered_work"] == 1
            assert (await db.claim(at)).attempts == 2
        finally:
            await db.close()

    asyncio.run(scenario())


def test_worker_does_not_block_event_loop(tmp_path):
    async def scenario():
        db = Database(tmp_path)
        release = threading.Event()
        started = threading.Event()
        try:
            await db.open()

            def blocking(c):
                started.set()
                release.wait(2)
                return threading.get_ident(), c.execute("SELECT 1").fetchone()[0]

            task = asyncio.create_task(db.call(blocking))
            await asyncio.to_thread(started.wait, 1)
            assert not task.done()
            release.set()
            worker_id, value = await task
            assert worker_id != threading.get_ident() and value == 1
        finally:
            release.set()
            await db.close()

    asyncio.run(scenario())


def test_migration_rollback_and_schema_rejection():
    c = sqlite3.connect(":memory:", isolation_level=None)
    try:
        initial = "CREATE TABLE facts (value TEXT); INSERT INTO facts VALUES ('synthetic');"
        apply_migrations(c, [initial])
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(c, [initial, "CREATE TABLE scratch (id INTEGER); INVALID SQL;"])
        assert c.execute("SELECT value FROM facts").fetchone() == ("synthetic",)
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='scratch'").fetchone() == (
            0,
        )
        assert c.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (1,)
        with pytest.raises(SchemaError, match="newer_schema"):
            apply_migrations(c, [])
        with pytest.raises(SchemaError, match="checksum"):
            apply_migrations(c, ["CREATE TABLE changed (id INTEGER);"])
    finally:
        c.close()


def test_artifact_backup_restore_and_corruption(tmp_path):
    async def scenario():
        db = Database(tmp_path / "original")
        restored = Database(tmp_path / "restored")
        try:
            await db.open()
            fact = CandidateFact(
                kind="skill",
                value="Python",
                source_reference="synthetic",
                status="confirmed",
                confirmed_at=now(),
            )
            revision = await db.save_fact(fact)
            artifact_id = identifier()
            content = b"Synthetic cover letter grounded in fact f1"
            manifest = Artifact(
                id=artifact_id,
                kind="cover_letter",
                relative_path=f"artifacts/{artifact_id}",
                sha256=digest(content),
                profile_revision=revision,
                fact_revisions=((fact.id, 1),),
                generator_version="synthetic-1",
            )
            await db.save_artifact(content, manifest)
            with pytest.raises(BoundaryError, match="immutable"):
                await db.save_artifact(content, manifest)
            (db.root / "browser").mkdir()
            (db.root / "browser" / "secret").write_text("synthetic-cookie")
            snapshot = await db.backup()
            assert not (snapshot / "browser").exists()
            assert (snapshot / manifest.relative_path).read_bytes() == content
            assert (snapshot / "state.sqlite3").stat().st_mode & 0o777 == 0o600
            restore_backup(snapshot, restored.root)
            await restored.open()
            await restored.recover()
            policy = await restored.policy()
            assert policy.paused and policy.restore_audit_required and policy.mode == "draft_only"
            assert await restored.facts() == (fact,)
            assert await restored.inspect_artifacts() == {
                "corrupt_artifacts": 0,
                "orphan_artifacts": 0,
            }
            with pytest.raises(BoundaryError, match="fresh"):
                restore_backup(snapshot, restored.root)
            (snapshot / manifest.relative_path).write_bytes(b"corrupted")
            with pytest.raises(BoundaryError, match="checksum"):
                restore_backup(snapshot, tmp_path / "bad-restore")
            assert not (tmp_path / "bad-restore" / "state.sqlite3").exists()
            (db.root / manifest.relative_path).write_bytes(b"corrupted")
            assert (await db.inspect_artifacts())["corrupt_artifacts"] == 1
            with pytest.raises(BoundaryError, match="checksum"):
                await db.backup()
        finally:
            await db.close()
            await restored.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.org",
        "http://192.168.1.1:11434",
        "http://localhost:11434",
        "http://127.0.0.1:11434?token=x",
        "http://user:secret@127.0.0.1:11434",
        "http://127.0.0.1:bad",
    ],
)
def test_remote_or_malformed_ollama_endpoint_rejected(endpoint):
    with pytest.raises(ValidationError):
        InferenceConfig(local_endpoint=endpoint)


@pytest.mark.parametrize(
    "settings",
    [
        {"unknown": 1},
        {"timezone": "Not/A_Zone"},
        {"inference": {"max_concurrency": 2}},
        {"execution": {"mode": "rules_based"}},
        {"linkedin": {"inbox_interval_minutes": 0}},
        {"schema_version": 2},
    ],
)
def test_config_errors(settings):
    with pytest.raises(ValidationError):
        Config.model_validate(settings)


def test_documented_config_and_path_boundaries(tmp_path):
    config = load_config(Path("config.example.toml"))
    assert config.timezone == "Europe/Warsaw"
    assert SearchPreferences().locations == ()
    with pytest.raises(BoundaryError):
        safe_file(tmp_path, "../escape")
    (tmp_path / "link").symlink_to(tmp_path / "other")
    with pytest.raises(BoundaryError):
        safe_file(tmp_path, "link/file")
    with pytest.raises(ValueError):
        Config(data_dir=str(tmp_path / "link")).validate_paths()
    with pytest.raises(ValueError):
        Config(
            data_dir=str(tmp_path), linkedin={"browser_profile_dir": "/tmp/shared"}
        ).validate_paths()


def test_runtime_lock_second_process_and_release(tmp_path):
    code = (
        "from pathlib import Path; from linkedin_bot.runtime.locking import RuntimeLock; "
        "RuntimeLock(Path(__import__('sys').argv[1])).__enter__()"
    )
    with RuntimeLock(tmp_path):
        child = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True)
        assert child.returncode != 0 and b"runtime_already_locked" in child.stderr
        with pytest.raises(AlreadyRunning):
            with RuntimeLock(tmp_path):
                pass
    with RuntimeLock(tmp_path):
        assert (tmp_path / "runtime.lock").exists()


def test_logs_exclude_secrets_bodies_urls_and_exception_text():
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        "",
        1,
        "token=%s https://example.org/secret Raw CV",
        ("secret-value",),
        None,
    )
    record.event = "read_failed"
    record.token = "secret-value"
    record.count = 3
    output = RedactedFormatter().format(record)
    assert json.loads(output) == {"level": "ERROR", "event": "read_failed", "count": 3}
    assert "secret" not in output and "CV" not in output and "https" not in output


def test_cli_foreground_init_doctor_backup_without_network(tmp_path):
    def invoke(*args):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "linkedin_bot.runtime.cli",
                "--data-dir",
                str(tmp_path / "data"),
                *args,
            ],
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    assert invoke("init")["policy"]["application_cap"] == 0
    assert invoke("doctor")["capabilities"]["employer_effects"] == "disabled"
    assert invoke("run", "--once")["stopped"]
    assert Path(invoke("backup")["snapshot"]).is_dir()


def test_executor_persists_blocked_proposal_and_checks_revision(tmp_path):
    async def scenario():
        db = Database(tmp_path)
        try:
            await db.open()
            proposal = action()
            assert "draft_only" in await DraftExecutor(db).propose(proposal)
            assert await db.call(
                lambda c: c.execute(
                    "SELECT state,reason_code FROM actions WHERE id=?",
                    (proposal.id,),
                ).fetchone()
            ) == ("blocked", "draft_only")
            with pytest.raises(RevisionConflict):
                await db.block_action(action(id=proposal.id, revision=2), "draft_only")
            profile = await db.profile()
            assert profile.revision == 1 and profile.fact_ids == ()
        finally:
            await db.close()

    asyncio.run(scenario())


def test_database_rejects_newer_schema_and_keeps_state(tmp_path):
    async def scenario():
        db = Database(tmp_path)
        try:
            await db.open()
            await db.call(lambda c: c.execute("INSERT INTO schema_migrations VALUES (2,'future')"))
            await db.close()
            db = Database(tmp_path)
            with pytest.raises(SchemaError, match="newer_schema"):
                await db.open()
        finally:
            await db.close()

    asyncio.run(scenario())
    with sqlite3.connect(tmp_path / "state.sqlite3") as c:
        assert c.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (2,)


def test_changed_artifact_blocks_action(tmp_path):
    async def scenario():
        db = Database(tmp_path)
        try:
            await db.open()
            artifact_id = identifier()
            content = b"synthetic draft"
            manifest = Artifact(
                id=artifact_id,
                kind="message_draft",
                relative_path=f"artifacts/{artifact_id}",
                sha256=digest(content),
                profile_revision=1,
                generator_version="synthetic",
            )
            await db.save_artifact(content, manifest)
            (db.root / manifest.relative_path).write_bytes(b"changed")
            with pytest.raises(RevisionConflict, match="changed_artifact"):
                await db.save_action(action(artifact_hashes=(manifest.sha256,)))
        finally:
            await db.close()

    asyncio.run(scenario())


def test_restore_ready_actions_require_audit(tmp_path):
    async def scenario():
        db = Database(tmp_path / "original")
        restored = Database(tmp_path / "restored")
        try:
            await db.open()
            proposal = action()
            await db.save_action(proposal)
            await db.call(lambda c: c.execute("UPDATE actions SET state='ready'"))
            snapshot = await db.backup()
            restore_backup(snapshot, restored.root)
            await restored.open()
            await restored.recover()
            assert (await restored.status())["unknown_actions"] == 1
            assert (await restored.policy()).restore_audit_required
            with pytest.raises(RevisionConflict, match="effect_locked"):
                await restored.save_action(action(id=proposal.id, revision=2), 1)
        finally:
            await db.close()
            await restored.close()

    asyncio.run(scenario())


def test_collect_rejects_invalid_limit_before_scheduling_work(tmp_path):
    def invoke(*args):
        return subprocess.run(
            [sys.executable, "-m", "linkedin_bot.runtime.cli", "--data-dir", str(tmp_path / "data"), *args],
            text=True,
            capture_output=True,
        )

    assert invoke("init").returncode == 0
    assert invoke("collect", "greenhouse", "acme", "--limit", "0").returncode != 0

    async def count():
        db = Database(tmp_path / "data")
        try:
            await db.open()
            return await db.call(lambda c: c.execute("SELECT COUNT(*) FROM work_items").fetchone()[0])
        finally:
            await db.close()

    assert asyncio.run(count()) == 0
