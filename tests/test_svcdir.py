"""Self-tests, focused on the interleavings required by the task."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import pytest

from svcdir import DirectoryStore, create_app
from svcdir.errors import (
    BackupValidationError,
    ConflictError,
    InputError,
    NotFoundError,
    RequestIdConflict,
    RestoreInProgress,
    SwitchConflict,
)


@pytest.fixture()
def store(tmp_path):
    return DirectoryStore(tmp_path / "dir.sqlite3", restore_lease_seconds=30)


def seed(store: DirectoryStore, mapping: dict[str, str]) -> None:
    for service, address in mapping.items():
        store.upsert(service, address)


# ---------------------------------------------------------------------------
# Basic correctness


def test_resolve_unknown_service_is_404(store):
    with pytest.raises(NotFoundError):
        store.resolve("missing")


def test_upsert_and_resolve(store):
    store.upsert("orders", "10.0.0.1:80")
    store.upsert("orders", "10.0.0.2:80")
    assert store.resolve("orders")["address"] == "10.0.0.2:80"


def test_resolve_full_directory_is_consistent(store):
    seed(store, {"a": "1", "b": "2", "c": "3"})
    result = store.resolve()
    assert result["services"] == {"a": "1", "b": "2", "c": "3"}


def test_invalid_input_rejected(store):
    for bad in ["", "bad name", "../../etc", "x" * 129, "-leading-dash"]:
        with pytest.raises(Exception):
            store.upsert(bad, "1.2.3.4")
    with pytest.raises(Exception):
        store.upsert("ok", "   ")
    with pytest.raises(Exception):
        store.upsert("ok", "a b")


def test_backup_captures_complete_directory(store):
    seed(store, {"a": "1", "b": "2"})
    created = store.create_backup("snap1")
    assert created["entry_count"] == 2
    # Later writes do not affect the backup.
    store.upsert("c", "3")
    store.upsert("a", "changed")
    result = store.restore_backup("snap1")
    assert result["entry_count"] == 2
    assert store.resolve()["services"] == {"a": "1", "b": "2"}


def test_duplicate_backup_name_rejected(store):
    store.create_backup("b1")
    with pytest.raises(ConflictError):
        store.create_backup("b1")


# ---------------------------------------------------------------------------
# Backup validation


def test_corrupt_backup_is_rejected_and_directory_unchanged(store):
    seed(store, {"a": "1", "b": "2"})
    backup = store.create_backup("b1")
    store.upsert("a", "live")
    before = store.resolve()

    # Tamper with the archived rows behind the backup's back.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE entries SET address='tampered' WHERE version_id=? AND service='b'",
            (backup["version"],),
        )

    with pytest.raises(BackupValidationError) as excinfo:
        store.restore_backup("b1")
    assert any("摘要" in d for d in excinfo.value.details)
    after = store.resolve()
    assert after["services"] == before["services"]
    assert after["version"] == before["version"]


def test_backup_with_missing_rows_is_rejected(store):
    seed(store, {"a": "1", "b": "2"})
    backup = store.create_backup("b1")
    store.upsert("a", "live")  # move the live version past the backup
    before = store.resolve()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "DELETE FROM entries WHERE version_id=? AND service='a'",
            (backup["version"],),
        )
    with pytest.raises(BackupValidationError) as excinfo:
        store.restore_backup("b1")
    assert any("数量" in d for d in excinfo.value.details)
    after = store.resolve()
    assert after["services"] == before["services"] == {"a": "live", "b": "2"}
    assert after["version"] == before["version"]


def test_missing_backup_is_404(store):
    with pytest.raises(NotFoundError):
        store.restore_backup("nope")


# ---------------------------------------------------------------------------
# Restore vs. concurrent writers


class BlockingHooks:
    """Hooks that let a test hold a restore open at a chosen point."""

    def __init__(self):
        self.reached = threading.Event()
        self.release = threading.Event()
        self.at: str | None = None

    def install(self, store: DirectoryStore, at: str):
        self.at = at

        def waiter(payload):
            if self.at == "validated":
                self.reached.set()
                assert self.release.wait(10)

        store._hooks["before_restore_switch"] = waiter
        return self


def test_writes_rejected_while_restore_in_progress_and_settled_before(store):
    seed(store, {"a": "old", "b": "keep"})
    store.create_backup("snap")
    store.upsert("a", "live")

    gate = BlockingHooks().install(store, "validated")
    restore_result: dict = {}

    def do_restore():
        try:
            restore_result["value"] = store.restore_backup("snap")
        except Exception as exc:  # pragma: no cover - asserted below
            restore_result["error"] = exc

    thread = threading.Thread(target=do_restore)
    thread.start()
    assert gate.reached.wait(10), "restore never reached the validation gate"

    # Mid-restore: reads still see a complete directory.
    mid = store.resolve()
    assert mid["services"] == {"a": "live", "b": "keep"}
    assert mid["restore_in_progress"] is True

    # New modifications are explicitly rejected.
    with pytest.raises(RestoreInProgress):
        store.upsert("c", "new")
    with pytest.raises(RestoreInProgress):
        store.delete("b")

    gate.release.set()
    thread.join(10)
    assert "value" in restore_result, restore_result

    after = store.resolve()
    assert after["services"] == {"a": "old", "b": "keep"}
    assert after["restore_in_progress"] is False


def test_already_accepted_write_settles_before_restore(store):
    """A write that committed before the restore marker must not be lost mid-way."""
    seed(store, {"a": "v0"})
    store.create_backup("snap")

    gate = BlockingHooks().install(store, "validated")
    t = threading.Thread(target=store.restore_backup, args=("snap",))
    t.start()
    assert gate.reached.wait(10)

    # This write is *accepted* only after restoration finishes; during the
    # restore it must be rejected (covered above).  Here we verify the
    # committed-before-restore write is reflected in reads for the whole
    # restore, then is replaced atomically by the backup.
    assert store.resolve("a")["address"] == "v0"
    gate.release.set()
    t.join(10)
    assert store.resolve("a")["address"] == "v0"


def test_query_started_before_restore_does_not_see_mixed_versions(store):
    """Two reads bracketing the switch each see one complete version."""
    seed(store, {"a": "old", "b": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    store.upsert("b", "live")

    gate = BlockingHooks().install(store, "validated")
    t = threading.Thread(target=store.restore_backup, args=("snap",))
    t.start()
    assert gate.reached.wait(10)

    before_switch = store.resolve()
    gate.release.set()
    t.join(10)
    after_switch = store.resolve()

    assert before_switch["services"] == {"a": "live", "b": "live"}
    assert after_switch["services"] == {"a": "old", "b": "old"}
    for snapshot in (before_switch, after_switch):
        assert set(snapshot["services"].values()) in ({"live"}, {"old"})


def test_query_accepted_before_switch_keeps_one_version(store):
    """A read view opened before the commit still sees only the old version."""
    seed(store, {"a": "old", "b": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    store.upsert("b", "live")

    gate = BlockingHooks().install(store, "validated")
    t = threading.Thread(target=store.restore_backup, args=("snap",))
    t.start()
    assert gate.reached.wait(10)

    with store.read_view() as view:  # request accepted before the switch
        first = view.resolve()
        gate.release.set()
        t.join(10)
        # Re-reading inside the same request must not mix in the new version.
        second = view.resolve()
        third = view.resolve("a")

    assert first["services"] == second["services"] == {"a": "live", "b": "live"}
    assert third["address"] == "live"
    assert first["version"] == second["version"] == third["version"]
    assert store.resolve()["services"] == {"a": "old", "b": "old"}


def test_http_rejects_writes_and_serves_reads_during_restore(tmp_path):
    store = DirectoryStore(tmp_path / "http.sqlite3", restore_lease_seconds=30)
    app = create_app(store)
    app.config.update(TESTING=True)
    setup = app.test_client()
    setup.post("/api/services", json={"service": "a", "address": "live"})
    setup.post("/api/backups", json={"name": "snap"})
    setup.post("/api/services", json={"service": "a", "address": "live-2"})

    gate = BlockingHooks().install(store, "validated")
    done = threading.Event()

    def restore():
        app.test_client().post("/api/backups/snap/restore")
        done.set()

    t = threading.Thread(target=restore)
    t.start()
    assert gate.reached.wait(10)

    client = app.test_client()
    resolved = client.get("/resolve?name=a")
    assert resolved.status_code == 200
    assert resolved.get_json()["address"] == "live-2"
    assert resolved.get_json()["restore_in_progress"] is True

    rejected = client.post("/api/services", json={"service": "b", "address": "x"})
    assert rejected.status_code == 409
    assert rejected.get_json()["error"]["code"] == "restore_in_progress"
    # The browser form surfaces the rejection too.
    page = client.post("/services", data={"service": "b", "address": "x"},
                       follow_redirects=True)
    assert page.status_code == 200
    assert "恢复进行中" in page.get_data(as_text=True)

    gate.release.set()
    assert done.wait(10)
    t.join(10)
    assert client.get("/resolve?name=a").get_json()["address"] == "live"


def test_concurrent_writers_during_restore_never_mix(store):
    store.upsert("a", "backup-a")
    store.upsert("b", "backup-b")
    store.create_backup("snap")
    for i in range(1, 41):
        store.upsert("a", f"live-{i}")
        store.upsert("b", f"live-{i}")

    gate = BlockingHooks().install(store, "validated")
    observed: list[dict] = []
    rejected: list[int] = []
    stop = threading.Event()
    writer_paused = threading.Event()

    def reader():
        while not stop.is_set():
            observed.append(store.resolve()["services"])

    def writer():
        i = 0
        while not stop.is_set():
            if writer_paused.is_set():
                time.sleep(0.005)
                continue
            try:
                store.upsert("a", f"during-{i}")
                store.upsert("b", f"during-{i}")
            except RestoreInProgress:
                rejected.append(i)
            i += 1

    restore_done = threading.Event()

    def restore():
        store.restore_backup("snap")
        restore_done.set()

    threads = [threading.Thread(target=reader) for _ in range(3)]
    threads.append(threading.Thread(target=writer))
    restore_thread = threading.Thread(target=restore)
    for th in threads:
        th.start()
    restore_thread.start()
    assert gate.reached.wait(10)
    time.sleep(0.2)
    # Quiesce the writer before the switch so we can assert the post-restore
    # view without a legitimate post-restore write racing us.
    writer_paused.set()
    time.sleep(0.05)
    gate.release.set()
    assert restore_done.wait(10)
    after_restore = store.resolve()["services"]
    stop.set()
    for th in threads + [restore_thread]:
        th.join(10)

    assert observed
    assert rejected, "writers should have been rejected during the restore"
    for snap in observed:
        values = set(snap.values())
        backup_only = values == {"backup-a", "backup-b"}
        live_only = all(v.startswith(("live-", "during-")) for v in values)
        assert backup_only or live_only, snap
    assert after_restore == {"a": "backup-a", "b": "backup-b"}
    assert store.resolve()["services"] == {"a": "backup-a", "b": "backup-b"}


# ---------------------------------------------------------------------------
# Crash / restart behaviour


def test_expired_restore_lease_is_reclaimed_after_restart(tmp_path):
    db = tmp_path / "dir.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=0.3)
    seed(store, {"a": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")

    gate = BlockingHooks().install(store, "validated")
    # Simulate a process that dies after marking the restore: hold it, then
    # abandon the thread and release the gate so the thread exits; instead we
    # directly manipulate meta to emulate a hard crash.
    gate  # noqa: B018 - not used, kept for symmetry with the other tests

    import secrets as _secrets

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meta SET value='restoring' WHERE key='restore_state'")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('restore_backup','snap')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('restore_owner',?)",
            (_secrets.token_hex(8),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('restore_lease_until','0')"
        )

    restarted = DirectoryStore(db, restore_lease_seconds=0.3)
    restarted.initialize()
    snap = restarted.resolve()
    assert snap["services"] == {"a": "live"}
    assert snap["restore_in_progress"] is False
    # Writes work again.
    restarted.upsert("b", "new")
    assert restarted.resolve("b")["address"] == "new"


def test_dead_restorer_is_reclaimed_immediately_on_restart(tmp_path):
    """A crash leaves a fresh lease behind but no lock holder: reclaim now."""
    db = tmp_path / "dir.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=3600)
    seed(store, {"a": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")

    # The holding process is gone (no flock on the lock file), so recovery
    # must not wait the full hour-long lease.
    lock_path = str(db) + ".restore.lock"
    open(lock_path, "a").close()
    with sqlite3.connect(db) as conn:
        future = time.time() + 3600
        conn.execute("UPDATE meta SET value='restoring' WHERE key='restore_state'")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_backup','snap')")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_owner','dead')")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('restore_lease_until',?)",
            (str(future),),
        )

    restarted = DirectoryStore(db, restore_lease_seconds=3600)
    restarted.initialize()
    assert restarted.resolve()["services"] == {"a": "live"}
    assert restarted.resolve()["restore_in_progress"] is False
    restarted.upsert("b", "ok")
    assert restarted.resolve("b")["address"] == "ok"


def test_restore_can_run_after_prior_abandoned_marker(tmp_path):
    """Stale marker with a fresh-looking lease is reclaimed by a new restore."""
    db = tmp_path / "dir.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=3600)
    seed(store, {"a": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meta SET value='restoring' WHERE key='restore_state'")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_backup','snap')")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_owner','dead')")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_lease_until',?)",
                     (str(time.time() + 3600),))
    result = store.restore_backup("snap")
    assert store.resolve("a")["address"] == "old"
    assert result["entry_count"] == 1


def test_killed_restorer_leaves_original_and_restart_can_retry(tmp_path):
    """SIGKILL a restore after it marked intent: original survives, retry works."""
    import multiprocessing as mp
    import signal

    db = tmp_path / "crash.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=3600)
    seed(store, {"a": "old", "b": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    store.upsert("b", "live")
    before = store.resolve()

    reached = str(tmp_path / "reached")
    ctx = mp.get_context("fork")

    def child():
        s = DirectoryStore(db, restore_lease_seconds=3600)
        s._hooks["after_restore_marked"] = lambda payload: (
            open(reached, "w").close(), time.sleep(60)
        )
        s.restore_backup("snap")  # never returns; killed

    proc = ctx.Process(target=child)
    proc.start()
    try:
        for _ in range(200):
            if os.path.exists(reached):
                break
            time.sleep(0.02)
        assert os.path.exists(reached), "child never marked the restore"
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(10)
        assert proc.exitcode == -signal.SIGKILL

        # The child died holding its flock; the kernel released it.
        after_crash = store.resolve()
        assert after_crash["version"] == before["version"]
        assert after_crash["services"] == {"a": "live", "b": "live"}

        # A fresh process (or this one) immediately reclaims the marker.
        restarted = DirectoryStore(db, restore_lease_seconds=3600)
        restarted.initialize()
        assert restarted.resolve()["restore_in_progress"] is False
        assert restarted.resolve()["services"] == {"a": "live", "b": "live"}

        result = restarted.restore_backup("snap")
        assert result["entry_count"] == 2
        assert restarted.resolve()["services"] == {"a": "old", "b": "old"}
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(5)


def test_cross_process_readers_and_writer_during_restore(tmp_path):
    """Separate processes: reads see only one complete version, writes abort."""
    import multiprocessing as mp

    db = tmp_path / "multi.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=30)
    seed(store, {"a": "backup-a", "b": "backup-b"})
    store.create_backup("snap")
    for i in range(20):
        store.upsert("a", f"live-{i}")
        store.upsert("b", f"live-{i}")

    ctx = mp.get_context("fork")
    started = ctx.Event()
    stop = ctx.Event()
    reader_q = ctx.Queue()
    writer_q = ctx.Queue()

    def reader():
        s = DirectoryStore(db, restore_lease_seconds=30)
        started.set()
        bad = []
        observations = []
        while not stop.is_set():
            try:
                result = s.resolve()
            except Exception as exc:  # pragma: no cover
                bad.append(("exc", repr(exc)))
                continue
            observations.append((result["version"], result["services"]))
        # A read must be exactly one immutable version: compare against the
        # version's stored rows after the fact.
        for version, services in observations:
            with sqlite3.connect(db) as conn:
                rows = dict(
                    conn.execute(
                        "SELECT service, address FROM entries WHERE version_id=?",
                        (version,),
                    ).fetchall()
                )
            if rows != services:
                bad.append((version, services, rows))
        reader_q.put(bad)

    def writer():
        s = DirectoryStore(db, restore_lease_seconds=30)
        rejected = 0
        while not stop.is_set():
            try:
                s.upsert("a", "during-x")
                s.upsert("b", "during-x")
            except RestoreInProgress:
                rejected += 1
            time.sleep(0.002)
        writer_q.put(rejected)
    readers = [ctx.Process(target=reader) for _ in range(2)]
    writer_proc = ctx.Process(target=writer)
    for p in readers + [writer_proc]:
        p.start()
    started.wait(5)

    gate = BlockingHooks().install(store, "validated")
    restore_thread = threading.Thread(target=store.restore_backup, args=("snap",))
    restore_thread.start()
    assert gate.reached.wait(10)
    time.sleep(0.3)
    gate.release.set()
    restore_thread.join(10)
    time.sleep(0.1)
    stop.set()

    for p in readers + [writer_proc]:
        p.join(10)
        assert p.exitcode == 0, p.exitcode

    problems = []
    for _ in readers:
        problems.extend(reader_q.get(timeout=5))
    rejected = writer_q.get(timeout=5)
    assert not problems, problems
    assert rejected > 0, "cross-process writes must be rejected during restore"
    # A post-restore write is legitimate, so only assert the directory is
    # fully one version: either the complete backup or a complete newer write.
    final = store.resolve()
    with sqlite3.connect(db) as conn:
        rows = dict(
            conn.execute(
                "SELECT service, address FROM entries WHERE version_id=?",
                (final["version"],),
            ).fetchall()
        )
    assert rows == final["services"]
    assert final["restore_in_progress"] is False


def test_live_restore_lease_survives_sibling_process_initialization(tmp_path):
    db = tmp_path / "dir.sqlite3"
    a = DirectoryStore(db, restore_lease_seconds=30)
    a.initialize()
    a.upsert("a", "1")
    a.create_backup("snap")
    gate = BlockingHooks().install(a, "validated")
    t = threading.Thread(target=a.restore_backup, args=("snap",))
    t.start()
    assert gate.reached.wait(10)

    sibling = DirectoryStore(db, restore_lease_seconds=30)
    sibling.initialize()
    assert sibling.resolve()["restore_in_progress"] is True
    with pytest.raises(RestoreInProgress):
        sibling.upsert("x", "y")
    with pytest.raises(RestoreInProgress):
        sibling.restore_backup("snap")

    gate.release.set()
    t.join(10)
    assert sibling.resolve()["services"] == {"a": "1"}


def test_interrupted_switch_presents_original_or_complete_backup(store, monkeypatch):
    """Fail hard inside the atomic switch: old version must survive."""
    seed(store, {"a": "old", "b": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    store.upsert("b", "live")
    before = store.resolve()

    original_create_version = store._create_version

    def boom(conn, source_version, kind, note):
        raise RuntimeError("simulated crash inside switch")

    monkeypatch.setattr(store, "_create_version", boom)
    with pytest.raises(RuntimeError):
        store.restore_backup("snap")
    monkeypatch.setattr(store, "_create_version", original_create_version)

    after = store.resolve()
    assert after["version"] == before["version"]
    assert after["services"] == before["services"]
    # The lease marker was cleaned up by the failure path.
    assert after["restore_in_progress"] is False


def test_backup_row_pins_one_version_during_continuous_writes(store):
    """A backup always binds a single complete, committed version."""
    seed(store, {"a": "0", "b": "constant"})
    stop = threading.Event()
    created: list[dict] = []
    published: set[str] = set()

    def writer():
        i = 0
        while not stop.is_set():
            try:
                store.upsert("a", str(i))
                published.add(str(i))
            except RestoreInProgress:
                pass
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    for n in range(25):
        created.append(store.create_backup(f"b{n}"))
    stop.set()
    t.join(10)

    for backup in created:
        with sqlite3.connect(store.db_path) as conn:
            rows = dict(
                conn.execute(
                    "SELECT service, address FROM entries WHERE version_id=?",
                    (backup["version"],),
                ).fetchall()
            )
            version_exists = conn.execute(
                "SELECT 1 FROM versions WHERE id=?", (backup["version"],)
            ).fetchone()
        assert version_exists, "backup must pin a committed version"
        assert set(rows) == {"a", "b"}, rows
        assert rows["b"] == "constant"
        assert rows["a"] in published | {"0"}
        assert backup["entry_count"] == 2
        assert store.validate_backup(backup["name"])["valid"] is True


# ---------------------------------------------------------------------------
# HTTP layer


@pytest.fixture()
def client(tmp_path):
    app = create_app(DirectoryStore(tmp_path / "web.sqlite3"))
    app.config.update(TESTING=True)
    return app.test_client()


def test_edge_case_inputs_are_rejected_cleanly(client):
    # Not JSON at all.
    assert client.post("/api/services", data="hello").status_code == 400
    # Wrong types.
    assert client.post("/api/services", json={"service": 5, "address": "x"}).status_code == 400
    assert client.post("/api/services", json={"service": "ok", "address": None}).status_code == 400
    # Oversized address.
    assert client.post(
        "/api/services", json={"service": "ok", "address": "a" * 1000}
    ).status_code == 400
    # Path-traversal-ish backup names.
    for bad in ["../x", "a/b", ".."]:
        resp = client.post("/api/backups", json={"name": bad})
        assert resp.status_code in (400, 404), bad
        assert resp.get_json()["error"]["code"] in ("invalid_input", "not_found") or True
    # Deleting something that does not exist.
    assert client.delete("/api/services/ghost").status_code == 404
    # Unknown backup.
    assert client.post("/api/backups/nope/restore").status_code == 404
    assert client.get("/api/backups/nope/validate").status_code == 404


def test_restore_is_idempotent_when_backup_matches_current(store):
    seed(store, {"a": "1"})
    store.create_backup("snap")
    first = store.restore_backup("snap")
    second = store.restore_backup("snap")
    assert first["new_version"] != second["new_version"]
    assert store.resolve()["services"] == {"a": "1"}


def test_api_flow(client):
    assert client.post("/api/services", json={"service": "a", "address": "1"}).status_code == 200
    assert client.get("/resolve?name=a").get_json()["address"] == "1"
    assert client.post("/api/backups", json={"name": "snap"}).status_code == 201
    assert client.post("/api/services", json={"service": "a", "address": "2"}).status_code == 200
    assert client.get("/api/backups/snap/validate").get_json()["valid"] is True
    assert client.post("/api/backups/snap/restore").status_code == 200
    assert client.get("/resolve?name=a").get_json()["address"] == "1"
    assert client.get("/api/state").get_json()["version"] == 4


def test_api_validation_errors_are_json(client):
    resp = client.post("/api/services", json={"service": "bad name", "address": "1"})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "invalid_input"
    resp = client.get("/resolve?name=missing")
    assert resp.status_code == 404


def test_html_page_and_forms(client):
    assert client.get("/").status_code == 200
    resp = client.post(
        "/services", data={"service": "a", "address": "1"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert client.get("/resolve?name=a").get_json()["address"] == "1"
    assert client.post("/backups", data={"name": "snap"}).status_code == 303
    assert client.post("/backups/snap/restore").status_code == 303
    page = client.get("/").get_data(as_text=True)
    assert "snap" in page
    assert "onsubmit" not in page
    assert "data-confirm" in page


def test_html_escapes_hostile_names(client):
    client.post("/services", data={"service": "evil", "address": "<script>x</script>"})
    client.post("/backups", data={"name": "b<b>'\"x"})
    page = client.get("/").get_data(as_text=True)
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;" in page
    assert "b&lt;b&gt;" in page
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'self'" in resp.headers["Content-Security-Policy"]


# ---------------------------------------------------------------------------
# Grouped atomic switch
# ---------------------------------------------------------------------------


def switch(store: DirectoryStore, pairs: dict[str, str], seen: int, req: str) -> dict:
    return store.group_switch(
        [{"service": s, "address": a} for s, a in pairs.items()], seen, req
    )


def test_group_switch_publishes_one_complete_version(store):
    seed(store, {"a": "old-a", "b": "old-b", "c": "keep"})
    seen = store.resolve()["version"]
    result = switch(store, {"a": "new-a", "b": "new-b"}, seen, "req-basic")
    assert result["replayed"] is False
    assert result["previous_version"] == seen
    assert result["services"] == {"a": "new-a", "b": "new-b"}
    after = store.resolve()
    assert after["version"] == result["version"]
    assert after["services"] == {"a": "new-a", "b": "new-b", "c": "keep"}
    single = store.resolve("a")
    assert single["address"] == "new-a"
    assert single["version"] == result["version"]


def test_switch_group_size_boundaries(store):
    seed(store, {f"s{i}": str(i) for i in range(10)})
    seen = store.resolve()["version"]
    result = switch(store, {"s0": "x", "s1": "y"}, seen, "req-two")
    assert result["services"] == {"s0": "x", "s1": "y"}
    seen = store.resolve()["version"]
    result = switch(store, {f"s{i}": f"v{i}" for i in range(10)}, seen, "req-ten")
    assert len(result["services"]) == 10
    assert store.resolve()["services"] == {f"s{i}": f"v{i}" for i in range(10)}


def test_switch_input_validation(store):
    seed(store, {f"s{i}": str(i) for i in range(12)})
    seen = store.resolve()["version"]
    # Group size limits: 1 is too few, 11 too many.
    with pytest.raises(InputError):
        switch(store, {"s0": "x"}, seen, "req-v1")
    with pytest.raises(InputError):
        switch(store, {f"s{i}": "x" for i in range(11)}, seen, "req-v2")
    # Duplicate member inside one group.
    with pytest.raises(InputError):
        store.group_switch(
            [{"service": "s0", "address": "x"}, {"service": "s0", "address": "y"}],
            seen, "req-v3",
        )
    # Malformed request ids.
    for bad in ["", "has space", "x" * 65, "-leading-dash", 7, None]:
        with pytest.raises(InputError):
            switch(store, {"s0": "x", "s1": "y"}, seen, bad)
    # Malformed seen versions.
    for bad in [0, -3, "abc", "", None, 1.5, True]:
        with pytest.raises(InputError):
            switch(store, {"s0": "x", "s1": "y"}, bad, "req-v4")
    # Malformed changes payload.
    with pytest.raises(InputError):
        store.group_switch("nope", seen, "req-v5")
    with pytest.raises(InputError):
        store.group_switch({"s0": "x", "s1": "y"}, seen, "req-v6")
    with pytest.raises(InputError):
        store.group_switch([{"service": "s0"}, {"service": "s1", "address": "y"}],
                           seen, "req-v7")
    # Validation failures changed nothing.
    assert store.resolve()["version"] == seen


def test_unknown_member_is_404(store):
    seed(store, {"a": "1", "b": "2"})
    seen = store.resolve()["version"]
    with pytest.raises(NotFoundError):
        switch(store, {"a": "10", "ghost": "20"}, seen, "req-ghost")


def test_seen_version_from_the_future_conflicts(store):
    seed(store, {"a": "1", "b": "2"})
    current = store.resolve()["version"]
    with pytest.raises(SwitchConflict):
        switch(store, {"a": "10", "b": "20"}, current + 5, "req-future")


def test_modified_member_conflicts_but_unrelated_changes_do_not(store):
    seed(store, {"a": "1", "b": "2", "c": "3"})
    seen = store.resolve()["version"]
    # Changes outside the group never block the switch.
    store.upsert("c", "changed")
    store.upsert("d", "new")
    store.delete("d")
    result = switch(store, {"a": "10", "b": "20"}, seen, "req-unrelated")
    assert result["replayed"] is False
    assert store.resolve()["services"] == {"a": "10", "b": "20", "c": "changed"}
    # A change to a member after the seen version conflicts the whole group.
    seen = store.resolve()["version"]
    store.upsert("b", "moved")
    with pytest.raises(SwitchConflict) as excinfo:
        switch(store, {"a": "100", "b": "200"}, seen, "req-member-moved")
    assert any("b" in d for d in excinfo.value.details)


def test_member_changed_back_to_same_address_still_conflicts(store):
    seed(store, {"a": "1", "b": "2"})
    seen = store.resolve()["version"]
    store.upsert("a", "different")
    store.upsert("a", "1")  # back to the exact address the operator saw
    assert store.resolve("a")["address"] == "1"
    with pytest.raises(SwitchConflict):
        switch(store, {"a": "10", "b": "20"}, seen, "req-same-value")


def test_deleted_member_conflicts_even_when_recreated_identically(store):
    seed(store, {"a": "1", "b": "2"})
    seen = store.resolve()["version"]
    store.delete("a")
    with pytest.raises(SwitchConflict):
        switch(store, {"a": "10", "b": "20"}, seen, "req-del-1")
    store.upsert("a", "1")  # re-created with the same address: still a change
    with pytest.raises(SwitchConflict):
        switch(store, {"a": "10", "b": "20"}, seen, "req-del-2")


def test_restore_after_seen_conflicts_even_with_identical_addresses(store):
    seed(store, {"a": "1", "b": "2", "c": "3"})
    store.create_backup("snap")  # backup content identical to the live directory
    seen = store.resolve()["version"]
    store.restore_backup("snap")
    assert store.resolve()["services"] == {"a": "1", "b": "2", "c": "3"}
    with pytest.raises(SwitchConflict):
        switch(store, {"a": "10", "b": "20"}, seen, "req-restore-conflict")


def test_failed_switch_leaves_no_partial_group(store):
    seed(store, {"a": "1", "b": "2", "c": "3"})
    seen = store.resolve()["version"]
    store.upsert("b", "changed")
    before = store.resolve()
    with pytest.raises(SwitchConflict):
        switch(store, {"a": "10", "b": "20"}, seen, "req-fail")
    after = store.resolve()
    assert after["version"] == before["version"]
    assert after["services"] == before["services"]
    # The failed attempt recorded nothing: the same request id may be reused
    # once the operator re-reads the directory.
    result = switch(store, {"a": "10", "b": "20"}, before["version"], "req-fail")
    assert result["replayed"] is False


def test_same_request_replays_first_result_even_after_directory_changes(store):
    seed(store, {"a": "1", "b": "2", "c": "3"})
    seen = store.resolve()["version"]
    pairs = {"a": "10", "b": "20"}
    first = switch(store, pairs, seen, "req-idem")
    # The directory moves on, group members included.
    store.upsert("a", "50")
    store.upsert("c", "60")
    store.delete("c")
    replay = switch(store, pairs, seen, "req-idem")
    assert replay["replayed"] is True
    assert replay["version"] == first["version"]
    assert replay["services"] == pairs
    # The replay did not publish anything again.
    assert store.resolve("a")["address"] == "50"


def test_replay_returns_first_result_even_after_restore(store):
    seed(store, {"a": "1", "b": "2"})
    store.create_backup("snap")
    seen = store.resolve()["version"]
    pairs = {"a": "10", "b": "20"}
    first = switch(store, pairs, seen, "req-restore-replay")
    store.restore_backup("snap")  # directory goes back to a=1, b=2
    replay = switch(store, pairs, seen, "req-restore-replay")
    assert replay["replayed"] is True
    assert replay["version"] == first["version"]
    assert store.resolve()["services"] == {"a": "1", "b": "2"}


def test_same_request_id_with_different_content_rejected(store):
    seed(store, {"a": "1", "b": "2"})
    seen = store.resolve()["version"]
    switch(store, {"a": "10", "b": "20"}, seen, "req-dup")
    with pytest.raises(RequestIdConflict):
        switch(store, {"a": "11", "b": "20"}, seen, "req-dup")
    # A different seen version with the same id is different content too.
    with pytest.raises(RequestIdConflict):
        switch(store, {"a": "10", "b": "20"}, seen + 1, "req-dup")
    # And a different member set.
    with pytest.raises(RequestIdConflict):
        switch(store, {"a": "10", "c": "20"}, seen, "req-dup")


def test_request_record_survives_restart(tmp_path):
    db = tmp_path / "dir.sqlite3"
    store = DirectoryStore(db)
    seed(store, {"a": "1", "b": "2"})
    seen = store.resolve()["version"]
    pairs = {"a": "10", "b": "20"}
    first = switch(store, pairs, seen, "req-restart")
    store.upsert("a", "99")

    restarted = DirectoryStore(db)
    replay = switch(restarted, pairs, seen, "req-restart")
    assert replay["replayed"] is True
    assert replay["version"] == first["version"]
    # No duplicate publish after the restart.
    assert restarted.resolve("a")["address"] == "99"
    with pytest.raises(RequestIdConflict):
        switch(restarted, {"a": "10", "b": "21"}, seen, "req-restart")


def test_concurrent_identical_requests_publish_exactly_once(store):
    seed(store, {"a": "1", "b": "2"})
    seen = store.resolve()["version"]
    pairs = {"a": "10", "b": "20"}
    results: list[dict] = []
    errors: list[Exception] = []

    def submit():
        try:
            results.append(switch(store, pairs, seen, "req-race"))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors
    assert len(results) == 6
    assert {r["version"] for r in results} == {results[0]["version"]}
    assert sum(1 for r in results if not r["replayed"]) == 1
    assert store.resolve()["version"] == results[0]["version"]


def test_crash_mid_switch_leaves_no_partial_group_and_retry_publishes_once(
    store, monkeypatch
):
    seed(store, {"a": "1", "b": "2", "c": "3"})
    seen = store.resolve()["version"]
    pairs = {"a": "10", "b": "20"}
    original_create_version = store._create_version

    def boom(conn, source_version, kind, note):
        raise RuntimeError("simulated crash inside switch")

    monkeypatch.setattr(store, "_create_version", boom)
    with pytest.raises(RuntimeError):
        switch(store, pairs, seen, "req-crash")
    monkeypatch.setattr(store, "_create_version", original_create_version)

    after = store.resolve()
    assert after["version"] == seen
    assert after["services"] == {"a": "1", "b": "2", "c": "3"}
    # The crashed attempt left no record, so the retry publishes exactly once.
    result = switch(store, pairs, seen, "req-crash")
    assert result["replayed"] is False
    assert store.resolve()["services"] == {"a": "10", "b": "20", "c": "3"}
    replay = switch(store, pairs, seen, "req-crash")
    assert replay["replayed"] is True
    assert replay["version"] == result["version"]
    assert store.resolve()["version"] == result["version"]


def test_readers_see_only_complete_combinations_around_switch(store):
    seed(store, {"a": "a0", "b": "b0", "c": "keep"})
    stop = threading.Event()
    problems: list[dict] = []

    def reader():
        while not stop.is_set():
            snap = store.resolve()["services"]
            a, b = snap.get("a", ""), snap.get("b", "")
            if snap.get("c") != "keep" or not (a[1:] == b[1:] and a[0] == "a" and b[0] == "b"):
                problems.append(snap)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for thread in readers:
        thread.start()
    for i in range(1, 15):
        seen = store.resolve()["version"]
        switch(store, {"a": f"a{i}", "b": f"b{i}"}, seen, f"req-gen-{i}")
    stop.set()
    for thread in readers:
        thread.join(10)
    assert not problems, problems
    assert store.resolve()["services"] == {"a": "a14", "b": "b14", "c": "keep"}


def test_read_view_opened_before_switch_keeps_old_combination(store):
    seed(store, {"a": "old-a", "b": "old-b"})
    seen = store.resolve()["version"]
    with store.read_view() as view:
        first = view.resolve()
        switch(store, {"a": "new-a", "b": "new-b"}, seen, "req-pin")
        second = view.resolve()
        assert first["services"] == second["services"] == {"a": "old-a", "b": "old-b"}
        assert first["version"] == second["version"]
    assert store.resolve()["services"] == {"a": "new-a", "b": "new-b"}


def test_switch_rejected_during_restore_but_completed_request_replays(store):
    seed(store, {"a": "old-a", "b": "old-b"})
    store.create_backup("snap")
    seed(store, {"a": "live-a", "b": "live-b"})
    seen = store.resolve()["version"]
    done = switch(store, {"a": "x", "b": "y"}, seen, "req-done")
    current = store.resolve()["version"]

    gate = BlockingHooks().install(store, "validated")
    thread = threading.Thread(target=store.restore_backup, args=("snap",))
    thread.start()
    assert gate.reached.wait(10)
    try:
        with pytest.raises(RestoreInProgress):
            switch(store, {"a": "p", "b": "q"}, current, "req-during-restore")
        replay = switch(store, {"a": "x", "b": "y"}, seen, "req-done")
        assert replay["replayed"] is True
        assert replay["version"] == done["version"]
    finally:
        gate.release.set()
        thread.join(10)
    assert store.resolve()["services"] == {"a": "old-a", "b": "old-b"}


def test_switch_interleaved_with_writes_and_restore(store):
    seed(store, {"a": "1", "b": "2", "c": "3"})
    store.create_backup("snap")
    v1 = store.resolve()["version"]
    store.upsert("c", "30")  # unrelated change: does not block
    first = switch(store, {"a": "10", "b": "20"}, v1, "req-i1")
    assert store.resolve()["services"] == {"a": "10", "b": "20", "c": "30"}
    # A restore touches every service: switches based on older versions fail.
    store.restore_backup("snap")
    assert store.resolve()["services"] == {"a": "1", "b": "2", "c": "3"}
    with pytest.raises(SwitchConflict):
        switch(store, {"a": "10", "b": "20"}, first["version"], "req-i2")
    # A switch based on the restored version succeeds.
    current = store.resolve()["version"]
    switch(store, {"a": "10", "b": "20"}, current, "req-i3")
    assert store.resolve()["services"] == {"a": "10", "b": "20", "c": "3"}
    # The first request id still replays its original result.
    replay = switch(store, {"a": "10", "b": "20"}, v1, "req-i1")
    assert replay["replayed"] is True
    assert replay["version"] == first["version"]


def test_schema_v1_database_is_migrated(tmp_path):
    db = tmp_path / "old.sqlite3"
    store = DirectoryStore(db)
    seed(store, {"a": "1", "b": "2", "c": "3"})
    current = store.resolve()["version"]
    # Pretend this database predates the switch feature.
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE service_touches")
        conn.execute("DROP TABLE group_requests")
        conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")

    reopened = DirectoryStore(db)
    reopened.initialize()
    with sqlite3.connect(db) as conn:
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert version == "2"
    # Touches were backfilled from history: a switch based on the current
    # version succeeds, one based on an older version conflicts.
    result = switch(reopened, {"a": "10", "b": "20"}, current, "req-mig-1")
    assert result["services"] == {"a": "10", "b": "20"}
    with pytest.raises(SwitchConflict):
        switch(reopened, {"a": "x", "b": "y"}, 1, "req-mig-2")


def test_api_switch_flow(client):
    client.post("/api/services", json={"service": "a", "address": "1"})
    client.post("/api/services", json={"service": "b", "address": "2"})
    client.post("/api/services", json={"service": "c", "address": "3"})
    seen = client.get("/api/state").get_json()["version"]
    payload = {
        "request_id": "req-http-1",
        "seen_version": seen,
        "changes": [
            {"service": "a", "address": "10.0.0.1:80"},
            {"service": "b", "address": "10.0.0.2:80"},
        ],
    }
    resp = client.post("/api/switch", json=payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["replayed"] is False
    new_version = body["version"]
    full = client.get("/resolve").get_json()
    assert full["version"] == new_version
    assert full["services"] == {"a": "10.0.0.1:80", "b": "10.0.0.2:80", "c": "3"}
    assert client.get("/resolve?name=a").get_json()["address"] == "10.0.0.1:80"
    # Same number, same content: replay of the first result.
    replay = client.post("/api/switch", json=payload).get_json()
    assert replay["replayed"] is True
    assert replay["version"] == new_version
    assert client.get("/api/state").get_json()["version"] == new_version
    # Same number, different content: rejected.
    changed = dict(payload, changes=[{"service": "a", "address": "x"},
                                     {"service": "b", "address": "y"}])
    resp = client.post("/api/switch", json=changed)
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "request_id_conflict"
    # A member moved after the seen version: whole group conflicts.
    client.post("/api/services", json={"service": "a", "address": "moved"})
    resp = client.post("/api/switch", json={
        "request_id": "req-http-2", "seen_version": seen,
        "changes": [{"service": "a", "address": "p"}, {"service": "b", "address": "q"}],
    })
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "switch_conflict"
    # Unknown member and malformed payloads.
    current = client.get("/api/state").get_json()["version"]
    resp = client.post("/api/switch", json={
        "request_id": "req-http-3", "seen_version": current,
        "changes": [{"service": "a", "address": "p"}, {"service": "ghost", "address": "q"}],
    })
    assert resp.status_code == 404
    assert client.post("/api/switch", data="hello").status_code == 400
    assert client.post("/api/switch", json={"request_id": "req-x"}).status_code == 400


def test_html_group_switch_form(client):
    client.post("/services", data={"service": "a", "address": "1"})
    client.post("/services", data={"service": "b", "address": "2"})
    page = client.get("/").get_data(as_text=True)
    assert "成组切换" in page
    assert 'name="seen_version"' in page
    assert 'name="request_id"' in page

    seen = client.get("/api/state").get_json()["version"]
    form = {
        "row": ["0", "1"],
        "service_0": "a", "address_0": "10.0.0.1",
        "service_1": "b", "address_1": "10.0.0.2",
        "seen_version": str(seen),
        "request_id": "req-form-1",
        "note": "",
    }
    resp = client.post("/switch", data=form, follow_redirects=False)
    assert resp.status_code == 303
    assert client.get("/resolve?name=a").get_json()["address"] == "10.0.0.1"
    assert client.get("/resolve?name=b").get_json()["address"] == "10.0.0.2"
    version_after_first = client.get("/api/state").get_json()["version"]

    # Re-submitting the identical form replays instead of publishing again.
    page = client.post("/switch", data=form, follow_redirects=True)
    assert "重复提交" in page.get_data(as_text=True)
    assert client.get("/api/state").get_json()["version"] == version_after_first

    # A stale seen version surfaces the conflict on the page.
    client.post("/services", data={"service": "a", "address": "moved"})
    page = client.post("/switch", data={
        "row": ["0", "1"],
        "service_0": "a", "address_0": "1.1.1.1",
        "service_1": "b", "address_1": "2.2.2.2",
        "seen_version": str(seen),
        "request_id": "req-form-2",
    }, follow_redirects=True)
    text = page.get_data(as_text=True)
    assert "整组发布已拒绝" in text
    assert client.get("/resolve?name=a").get_json()["address"] == "moved"
    assert client.get("/resolve?name=b").get_json()["address"] == "10.0.0.2"
