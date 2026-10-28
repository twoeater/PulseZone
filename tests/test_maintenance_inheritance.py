import asyncio
import os
import sqlite3

import pytest
from aiohttp import ClientSession

from pulsezone.app import SERVICE, create_app
from pulsezone.config import Config, effective, validate_patch
from pulsezone.storage import Store, quote
from test_maintenance import at
from test_runtime import server


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False), (" TRUE ", True), ("False", False)])
def test_maintenance_env(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("MAINTENANCE", value)
    assert Config.from_env(str(tmp_path / "missing.env")).maintenance is expected


@pytest.mark.parametrize("value", ["", "0", "1", "yes", "no", "null"])
def test_maintenance_env_rejects_invalid_boolean(tmp_path, monkeypatch, value):
    monkeypatch.setenv("MAINTENANCE", value)
    with pytest.raises(ValueError, match="MAINTENANCE"):
        Config.from_env(str(tmp_path / "missing.env"))


def test_maintenance_env_default_and_dotenv_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.delenv("MAINTENANCE", raising=False)
    assert Config.from_env(str(tmp_path / "missing.env")).defaults()["maintenance"] is False
    path = tmp_path / ".env"
    path.write_text("MAINTENANCE=true\n", encoding="utf-8")
    assert Config.from_env(str(path)).maintenance is True
    monkeypatch.setenv("MAINTENANCE", "false")
    assert Config.from_env(str(path)).maintenance is False


@pytest.mark.parametrize("global_value", [False, True])
@pytest.mark.parametrize("zone_value", [None, False, True])
@pytest.mark.parametrize("record_value", [None, False, True])
def test_maintenance_inherits_and_false_is_an_override(global_value, zone_value, record_value):
    zone = {"domain": "example.test", "enabled": 1, "present": 1, "maintenance": zone_value}
    row = {"record": "@", "ip": "127.0.0.1", "source": "file", "present": 1, "maintenance": record_value}
    expected = next(value for value in (record_value, zone_value, global_value) if value is not None)
    assert effective(Config(maintenance=global_value), zone, row)["maintenance"] is expected
    assert validate_patch({"maintenance": record_value})["maintenance"] is record_value
    assert validate_patch({"maintenance": zone_value}, True)["maintenance"] is zone_value


def test_record_schema_migration_preserves_explicit_values_indexes_and_alerts(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = Store(path)
    store.sync({"db.example.test": [("@", "127.0.0.1"), ("www", "127.0.0.1")]}, ["db.example.test"])
    zone = store.zones()[0]
    rows = store.records(zone["id"])
    for row, value in zip(rows, (True, False)):
        store.update_record(zone["id"], row["id"], {"maintenance": value, "alias": "Saved alias"})
        store.save_alert_state(zone, row, {"alerted_at": "2026-10-28T14:00:00+09:00"})
    before = store.snapshot()
    store.close()
    table = quote(zone["table_name"])
    with sqlite3.connect(path) as db:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (zone["table_name"],)).fetchone()[0]
        sql = sql.replace("CREATE TABLE " + table, 'CREATE TABLE "legacy_records"', 1)
        sql = sql.replace("maintenance INTEGER CHECK", "maintenance INTEGER NOT NULL DEFAULT 0 CHECK")
        db.execute(sql)
        db.execute('INSERT INTO "legacy_records" SELECT * FROM ' + table)
        db.execute("DROP TABLE " + table)
        db.execute('ALTER TABLE "legacy_records" RENAME TO ' + table)
        db.execute("CREATE INDEX saved_alias_idx ON " + table + " (alias)")
        db.execute("CREATE TABLE audit (record_id INTEGER)")
        db.execute("CREATE TRIGGER saved_update AFTER UPDATE ON " + table +
                   " BEGIN INSERT INTO audit VALUES (new.id); END")
    store = Store(path)
    try:
        assert store.snapshot() == before
        field = next(row for row in store.db.execute("PRAGMA table_info(" + table + ")") if row["name"] == "maintenance")
        assert field["notnull"] == 0 and field["dflt_value"] is None
        assert store.db.execute("SELECT count(*) FROM sqlite_master WHERE name='saved_alias_idx'").fetchone()[0] == 1
        store.update_record(zone["id"], rows[0]["id"], {"maintenance": None})
        assert store.db.execute("SELECT record_id FROM audit").fetchone()[0] == rows[0]["id"]
        assert store.records(zone["id"])[0]["maintenance"] is None
        new_id = store.add_record(zone["id"], {"record": "new", "ip": "127.0.0.1"})
        assert next(row for row in store.records(zone["id"]) if row["id"] == new_id)["maintenance"] is None
        with pytest.raises(sqlite3.IntegrityError):
            store.add_record(zone["id"], {"record": "new", "ip": "127.0.0.1"})
        saved = store.snapshot()
        version = store.db.execute("PRAGMA schema_version").fetchone()[0]
    finally:
        store.close()
    store = Store(path)
    try:
        assert store.snapshot() == saved
        assert store.db.total_changes == 0
        assert store.db.execute("PRAGMA schema_version").fetchone()[0] == version
    finally:
        store.close()


def test_inherited_alert_policy_api_and_per_record_auto_expiry(tmp_path):
    async def run():
        root = tmp_path / "zones"
        root.mkdir()
        (root / "db.example.test").write_text("@ IN A 127.0.0.1\nwww IN A 127.0.0.1\n", encoding="utf-8")

        class PendingProbe:
            async def check(self, *args):
                await asyncio.Event().wait()

        cfg = Config(zone_path=root, database_path=tmp_path / "runtime.sqlite3", maintenance=True)
        app = create_app(cfg, PendingProbe())
        svc = app[SERVICE]
        now = [at("2026-10-28T10:00:00")]
        svc.now = lambda: now[0]
        async with server(app) as port, ClientSession() as client:
            base = f"http://127.0.0.1:{port}"
            zone = svc.public_catalog[0]
            z = zone["id"]
            r, other = [row["id"] for row in zone["records"]]
            key = f"{z}:{r}"

            async def catalog():
                async with client.get(base + "/api/zones") as response:
                    return (await response.json())["zones"][0]

            async def set_value(value, row_id=None):
                url = base + f"/api/zones/{z}" + (f"/records/{row_id}" if row_id is not None else "")
                async with client.patch(url, json={"maintenance": value}) as response:
                    assert response.status == 200

            initial_task = svc.monitor.tasks[key]
            assert zone["maintenance"] is None and zone["effective"]["maintenance"] is True
            assert all(row["maintenance"] is None and row["effective"]["maintenance"] is True for row in zone["records"])
            assert svc.monitor.snapshot()[key]["maintenance"] is True
            await set_value(False)
            assert (await catalog())["records"][0]["effective"]["maintenance"] is False
            await set_value(True, r)
            assert (await catalog())["records"][0]["effective"]["maintenance"] is True
            await set_value(None)
            await set_value(False, r)
            assert svc.monitor.snapshot()[key]["maintenance"] is False
            # Restore both levels to inheritance in one mixed bulk request.
            async with client.post(base + "/api/bulk-update", json={"operations": [
                {"zone_id": z, "patch": {"maintenance": None}},
                {"zone_id": z, "record_id": r, "patch": {"maintenance": None}},
            ]}) as response:
                assert response.status == 200
            assert svc.monitor.tasks[key] is initial_task  # Settings do not interrupt a probe or reset its history.
            assert svc.monitor.snapshot()[key]["maintenance"] is True

            # Exercise the production event callback without sending anything externally.
            submitted = []
            def accept(text):
                submitted.append(text)
                return True
            svc.alerts.submit = accept
            target = svc.monitor.targets[key]
            svc.emit_alert(target, "first")
            baseline = svc.store.db.total_changes
            svc.emit_alert(target, "same day")
            assert submitted == ["first"] and svc.store.db.total_changes == baseline
            # The same behaviour also applies to a zone override with the environment off.
            cfg.maintenance = False
            await set_value(True)
            target = svc.monitor.targets[key]
            svc.emit_alert(target, "inherited from zone")
            assert submitted == ["first"]
            # Explicit record false overrides both possible parents.
            await set_value(False, r)
            svc.emit_alert(svc.monitor.targets[key], "explicit off")
            assert submitted == ["first", "explicit off"]
            await set_value(None, r)
            now[0] = at("2026-10-28T13:00:00")
            svc.expire_maintenance()
            zone = await catalog()
            row = zone["records"][0]
            assert zone["maintenance"] is True and row["maintenance"] is False
            assert row["effective"]["maintenance"] is False and svc.monitor.snapshot()[key]["maintenance"] is False
            assert zone["records"][1]["maintenance"] is None and zone["records"][1]["effective"]["maintenance"] is True
            baseline = svc.store.db.total_changes
            cfg.maintenance = True
            await set_value(None)
            assert (await catalog())["records"][0]["effective"]["maintenance"] is False
            svc.expire_maintenance()
            assert svc.store.db.total_changes == baseline + 1  # Only the requested zone change wrote to DB.
            svc.emit_alert(svc.monitor.targets[key], "after expiry")
            assert submitted[-1] == "after expiry"
            # A new/manual record inherits too, rather than silently forcing false.
            async with client.post(base + f"/api/zones/{z}/records", json={"record": "new", "ip": "127.0.0.2"}) as response:
                assert response.status == 201
                added = (await response.json())["id"]
            zone = await catalog()
            added_row = next(row for row in zone["records"] if row["id"] == added)
            assert added_row["maintenance"] is None and added_row["effective"]["maintenance"] is True
            assert svc.monitor.snapshot()[f"{z}:{other}"]["maintenance"] is True
        reopened = Store(cfg.database_path)
        try:
            row = next(row for row in reopened.records(z) if row["id"] == r)
            assert row["maintenance"] is False
        finally:
            reopened.close()
    asyncio.run(run())
