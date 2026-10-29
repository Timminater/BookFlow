"""Versioned, crash-safe SQLite schema migrations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class MigrationError(RuntimeError):
    """Base error for database migration failures."""


class DatabaseIntegrityError(MigrationError):
    """Raised when preflight finds a corrupt database."""


class UnsupportedSchemaVersionError(MigrationError):
    """Raised when the database is newer than this application."""


class MigrationChecksumError(MigrationError):
    """Raised when an applied migration no longer matches its definition."""


@dataclass(frozen=True)
class Migration:
    """One immutable schema migration."""

    version: int
    name: str
    checksum: str
    apply: Callable[[sqlite3.Connection], None]
    historical_checksums: tuple[str, ...] = ()


_JOURNAL_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version       INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    checksum      TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    completed_at  TEXT,
    status        TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    error_message TEXT
)
"""


class SQLiteMigrator:
    """Apply an ordered migration catalog to one SQLite database."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        db_path: str,
        migrations: Sequence[Migration],
    ) -> None:
        self._conn = conn
        self._db_path = db_path
        self._migrations = tuple(sorted(migrations, key=lambda migration: migration.version))
        self._validate_catalog()

    def migrate(self) -> Path | None:
        """Apply all pending migrations and return the preflight manifest path."""
        journal_exists = self._table_exists("schema_migrations")
        if journal_exists:
            self._validate_journal()

        completed = self._completed_versions() if journal_exists else set()
        pending = [
            migration for migration in self._migrations if migration.version not in completed
        ]
        if not pending:
            return None

        manifest_path = self._run_preflight()
        self._conn.execute(_JOURNAL_SQL)
        self._conn.commit()
        self._validate_journal()

        for migration in pending:
            self._apply_migration(migration)
        return manifest_path

    def _validate_catalog(self) -> None:
        versions = [migration.version for migration in self._migrations]
        if not versions or versions[0] < 1:
            raise ValueError("Migration catalog must start at a positive version")
        if len(versions) != len(set(versions)):
            raise ValueError("Migration versions must be unique")
        if versions != list(range(1, versions[-1] + 1)):
            raise ValueError("Migration versions must be contiguous")
        for migration in self._migrations:
            if not migration.name.strip():
                raise ValueError(f"Migration {migration.version} must have a name")
            if len(migration.checksum) != 64 or any(
                character not in "0123456789abcdef" for character in migration.checksum
            ):
                raise ValueError(f"Migration {migration.version} must have a SHA-256 checksum")

    def _validate_journal(self) -> None:
        catalog = {migration.version: migration for migration in self._migrations}
        rows = self._conn.execute(
            "SELECT version, name, checksum, status FROM schema_migrations ORDER BY version"
        ).fetchall()
        for row in rows:
            version = int(row[0])
            migration = catalog.get(version)
            if migration is None:
                raise UnsupportedSchemaVersionError(
                    f"Database schema version {version} is newer than this application "
                    f"(latest supported: {self._migrations[-1].version})"
                )
            if str(row[2]) not in (migration.checksum, *migration.historical_checksums):
                raise MigrationChecksumError(
                    f"Migration {version} checksum differs from the applied database record"
                )
            if str(row[1]) != migration.name:
                raise MigrationChecksumError(
                    f"Migration {version} name differs from the applied database record"
                )

    def _completed_versions(self) -> set[int]:
        return {
            int(row[0])
            for row in self._conn.execute(
                "SELECT version FROM schema_migrations WHERE status = 'completed'"
            ).fetchall()
        }

    def _run_preflight(self) -> Path | None:
        integrity_rows = [
            str(row[0]) for row in self._conn.execute("PRAGMA integrity_check").fetchall()
        ]
        if integrity_rows != ["ok"]:
            raise DatabaseIntegrityError(
                f"SQLite integrity check failed: {'; '.join(integrity_rows)}"
            )
        foreign_key_violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_violations:
            raise DatabaseIntegrityError(
                f"SQLite foreign key check found {len(foreign_key_violations)} violation(s)"
            )

        if self._db_path == ":memory:":
            return None

        db_path = Path(self._db_path).resolve()
        backup_root = db_path.parent / f"{db_path.name}.migration-backups"
        backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        schema_objects = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
            ).fetchone()[0]
        )

        backup_path: Path | None = None
        backup_sha256: str | None = None
        backup_size: int | None = None
        if schema_objects:
            backup_path = backup_root / f"{timestamp}.sqlite3"
            try:
                backup_conn = sqlite3.connect(backup_path)
            except sqlite3.OperationalError:
                # A legacy Windows chmod could leave this specific directory
                # accessible for reads but unable to create new SQLite files.
                # Preserve it and use a sibling recovery directory rather than
                # weakening or skipping the mandatory preflight backup.
                backup_root = db_path.parent / f"{db_path.name}.migration-backups-recovery"
                backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                backup_path = backup_root / f"{timestamp}.sqlite3"
                backup_conn = sqlite3.connect(backup_path)
            try:
                self._conn.backup(backup_conn)
            finally:
                backup_conn.close()
            backup_path.chmod(0o600)
            backup_size = backup_path.stat().st_size
            digest = hashlib.sha256()
            with backup_path.open("rb") as backup_file:
                for chunk in iter(lambda: backup_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            backup_sha256 = digest.hexdigest()

        manifest_path = backup_root / f"{timestamp}.manifest.json"
        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "database_path": str(db_path),
            "backup_path": str(backup_path) if backup_path is not None else None,
            "backup_sha256": backup_sha256,
            "backup_size": backup_size,
            "integrity_check": integrity_rows,
            "foreign_key_violations": 0,
            "current_versions": sorted(
                self._completed_versions() if self._table_exists("schema_migrations") else []
            ),
            "target_version": self._migrations[-1].version,
        }
        temporary_manifest = manifest_path.with_suffix(".tmp")
        temporary_manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_manifest.chmod(0o600)
        temporary_manifest.replace(manifest_path)
        return manifest_path

    def _apply_migration(self, migration: Migration) -> None:
        started_at = datetime.now(UTC).isoformat()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            existing = self._conn.execute(
                "SELECT status FROM schema_migrations WHERE version = ?",
                (migration.version,),
            ).fetchone()
            if existing is not None and str(existing[0]) == "completed":
                self._conn.rollback()
                return
            self._conn.execute(
                """
                INSERT INTO schema_migrations (
                    version, name, checksum, started_at, completed_at, status, error_message
                )
                VALUES (?, ?, ?, ?, NULL, 'running', NULL)
                ON CONFLICT(version) DO UPDATE SET
                    name = excluded.name,
                    checksum = excluded.checksum,
                    started_at = excluded.started_at,
                    completed_at = NULL,
                    status = 'running',
                    error_message = NULL
                """,
                (migration.version, migration.name, migration.checksum, started_at),
            )
            self._conn.execute("PRAGMA defer_foreign_keys = ON")
            migration.apply(self._conn)
            self._conn.execute(
                """
                UPDATE schema_migrations
                SET status = 'completed', completed_at = ?, error_message = NULL
                WHERE version = ?
                """,
                (datetime.now(UTC).isoformat(), migration.version),
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            self._conn.execute(
                """
                INSERT INTO schema_migrations (
                    version,
                    name,
                    checksum,
                    started_at,
                    completed_at,
                    status,
                    error_message
                )
                VALUES (?, ?, ?, ?, ?, 'failed', ?)
                ON CONFLICT(version) DO UPDATE SET
                    name = excluded.name,
                    checksum = excluded.checksum,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    status = 'failed',
                    error_message = excluded.error_message
                """,
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    started_at,
                    datetime.now(UTC).isoformat(),
                    str(exc)[:2000],
                ),
            )
            self._conn.commit()
            raise

    def _table_exists(self, table_name: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            is not None
        )
