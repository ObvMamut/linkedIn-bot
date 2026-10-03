"""Restore into a fresh directory so existing history is never overwritten."""

import json
import shutil
import sqlite3
from pathlib import Path

from linkedin_bot.domain.models import Artifact, Policy, digest
from linkedin_bot.runtime.privacy import BoundaryError, private_directory, safe_file
from linkedin_bot.storage.database import migration_scripts


def restore_backup(backup: Path, root: Path) -> None:
    if safe_file(root, "state.sqlite3").exists() or safe_file(root, "artifacts").exists():
        raise BoundaryError("restore_requires_fresh_data_directory")
    manifest_path = safe_file(backup, "manifest.json")
    if manifest_path.stat().st_size > 1_000_000:
        raise BoundaryError("backup_manifest_too_large")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("app_version") != "0.1.0" or manifest.get("schema_version") not in range(
        1, len(migration_scripts()) + 1
    ):
        raise BoundaryError("backup_version")
    checksums = manifest.get("checksums")
    if not isinstance(checksums, dict) or "state.sqlite3" not in checksums:
        raise BoundaryError("backup_manifest")
    for relative, checksum in checksums.items():
        if relative != "state.sqlite3" and not relative.startswith("artifacts/"):
            raise BoundaryError("backup_manifest_path")
        path = safe_file(backup, relative)
        if not path.is_file() or digest(path.read_bytes()) != checksum:
            raise BoundaryError("backup_checksum")
    private_directory(root)
    stage = safe_file(root, "restore-stage")
    if stage.exists():
        raise BoundaryError("unfinished_restore")
    private_directory(stage)
    try:
        for relative in checksums:
            destination = safe_file(stage, relative)
            private_directory(destination.parent)
            shutil.copyfile(safe_file(backup, relative), destination)
            destination.chmod(0o600)
        path = stage / "state.sqlite3"
        with sqlite3.connect(path) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise BoundaryError("backup_integrity")
            if list(connection.execute("PRAGMA foreign_key_check")):
                raise BoundaryError("backup_foreign_keys")
            if connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() != (
                manifest["schema_version"],
            ):
                raise BoundaryError("backup_schema")
            expected = {"state.sqlite3"}
            for (snapshot,) in connection.execute("SELECT snapshot FROM artifacts"):
                artifact = Artifact.model_validate_json(snapshot)
                if checksums.get(artifact.relative_path) != artifact.sha256:
                    raise BoundaryError("backup_artifact_manifest")
                expected.add(artifact.relative_path)
            if set(checksums) != expected:
                raise BoundaryError("backup_unreferenced_files")
            connection.execute(
                "UPDATE runtime_state SET paused=1,restore_audit_required=1 WHERE id=1"
            )
            revision = connection.execute("SELECT MAX(revision) FROM policies").fetchone()[0] + 1
            connection.execute(
                "INSERT INTO policies VALUES (?,?)",
                (
                    revision,
                    Policy(
                        revision=revision, paused=True, restore_audit_required=True
                    ).model_dump_json(),
                ),
            )
            connection.execute(
                "UPDATE actions SET state='unknown',reason_code='restored_requires_audit' "
                "WHERE state IN ('ready','executing','unknown')"
            )
            connection.execute("UPDATE approvals SET invalidated=1 WHERE consumed_at IS NULL")
            if connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='linkedin_state'"
            ).fetchone()[0]:
                # The browser profile is not part of a backup, so the session must be re-verified.
                connection.execute("UPDATE linkedin_state SET state='unknown',last_success=NULL")
            if connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='telegram_binding'"
            ).fetchone()[0]:
                # A restored installation must be paired again before it talks to anyone.
                connection.execute(
                    "UPDATE telegram_binding SET user_id=NULL,chat_id=NULL,paired_at=NULL,"
                    "code_hash=NULL,code_expires_at=NULL,code_attempts=0,last_update_id=0"
                )
            connection.execute("UPDATE work_items SET state='pending' WHERE state='running'")
        marker = safe_file(root, "restore-incomplete")
        marker.touch(mode=0o600, exist_ok=False)
        (stage / "state.sqlite3").rename(root / "state.sqlite3")
        if (stage / "artifacts").exists():
            (stage / "artifacts").rename(root / "artifacts")
        marker.unlink()
    finally:
        shutil.rmtree(stage)
