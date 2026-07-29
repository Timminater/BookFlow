"""Small cross-process lease used around catalog publication and cleanup."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

from bookflow.core.book_groups import canonical_book_id, equivalent_book_ids


class CatalogStorageLeaseError(RuntimeError):
    """A catalog file operation could not acquire its keyed lease."""


class CatalogStorageLease:
    def __init__(self, database_path: str, key: str, owner: str) -> None:
        self.database_path, self.key, self.owner = database_path, key, owner

    def acquire(self, duration: timedelta = timedelta(minutes=2)) -> None:
        now = datetime.now(UTC)
        expiry = now + duration
        with sqlite3.connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner, expires_at FROM catalog_storage_leases WHERE lease_key=?",
                (self.key,),
            ).fetchone()
            if row is not None and row[0] != self.owner and str(row[1]) > now.isoformat():
                raise CatalogStorageLeaseError("Catalog storage is busy")
            conn.execute(
                """INSERT INTO catalog_storage_leases(lease_key, owner, expires_at)
                   VALUES(?,?,?) ON CONFLICT(lease_key) DO UPDATE SET owner=excluded.owner,
                   expires_at=excluded.expires_at""",
                (self.key, self.owner, expiry.isoformat()),
            )

    def release(self) -> None:
        with sqlite3.connect(self.database_path) as conn:
            conn.execute(
                "DELETE FROM catalog_storage_leases WHERE lease_key=? AND owner=?",
                (self.key, self.owner),
            )

    def renew(self, duration: timedelta = timedelta(minutes=2)) -> None:
        """Fence a long operation by extending only this owner's live lease."""
        expiry = datetime.now(UTC) + duration
        with sqlite3.connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                "UPDATE catalog_storage_leases SET expires_at=? WHERE lease_key=? AND owner=?",
                (expiry.isoformat(), self.key, self.owner),
            ).rowcount
            if updated != 1:
                raise CatalogStorageLeaseError("Catalog storage lease was lost")


def catalog_book_lease_key(database_path: str, book_id: int) -> str:
    """Resolve aliases to one durable group lock key before any file side effect."""
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        ids = equivalent_book_ids(conn, book_id)
        canonical = canonical_book_id(conn, ids) if ids else book_id
    return f"book:{canonical}"


def catalog_path_lease_key(storage_path: str, sha256: str) -> str:
    """Stable storage-object key; never expose a raw local path in the lease table."""
    fingerprint = hashlib.sha256(f"{storage_path}\0{sha256}".encode()).hexdigest()
    return f"file:{fingerprint}"
