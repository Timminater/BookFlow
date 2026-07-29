from __future__ import annotations

from datetime import timedelta

import pytest

from bookflow.core.catalog_storage_lease import CatalogStorageLease, CatalogStorageLeaseError
from bookflow.core.user_db import UserDB


def test_second_process_cannot_acquire_live_book_lease(tmp_path):
    database = tmp_path / "catalog.sqlite"
    UserDB(str(database)).initialize()
    first = CatalogStorageLease(str(database), "book:7", "first")
    second = CatalogStorageLease(str(database), "book:7", "second")

    first.acquire(timedelta(minutes=1))
    with pytest.raises(CatalogStorageLeaseError):
        second.acquire()
    first.release()
    second.acquire()


def test_expired_lease_is_reclaimable_after_crash(tmp_path):
    database = tmp_path / "catalog.sqlite"
    UserDB(str(database)).initialize()
    first = CatalogStorageLease(str(database), "book:7", "crashed")
    second = CatalogStorageLease(str(database), "book:7", "recovered")

    first.acquire(timedelta(microseconds=-1))
    second.acquire()


def test_only_current_owner_can_renew_lease(tmp_path):
    database = tmp_path / "catalog.sqlite"
    UserDB(str(database)).initialize()
    owner = CatalogStorageLease(str(database), "path:abc", "owner")
    intruder = CatalogStorageLease(str(database), "path:abc", "intruder")

    owner.acquire()
    owner.renew()
    with pytest.raises(CatalogStorageLeaseError):
        intruder.renew()
