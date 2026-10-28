import os
import sqlite3

import pytest

from pulsezone.config import Config, effective, validate_patch
from pulsezone.storage import Store


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False), (" TRUE ", True), ("FALSE", False)])
def test_https_insecure_environment(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("HTTPS_INSECURE", value)
    assert Config.from_env(str(tmp_path / "missing.env")).https_insecure is expected


@pytest.mark.parametrize("value", ["", "1", "yes", "no", "invalid"])
def test_https_insecure_invalid_environment(tmp_path, monkeypatch, value):
    monkeypatch.setenv("HTTPS_INSECURE", value)
    with pytest.raises(ValueError, match="HTTPS_INSECURE"):
        Config.from_env(str(tmp_path / "missing.env"))


def test_https_insecure_default_dotenv_and_environment_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("HTTPS_INSECURE", raising=False)
    assert Config.from_env(str(tmp_path / "missing.env")).https_insecure is False
    env = tmp_path / ".env"
    env.write_text("HTTPS_INSECURE=true\n", encoding="utf-8")
    assert Config.from_env(str(env)).https_insecure is True
    monkeypatch.setenv("HTTPS_INSECURE", "false")
    assert Config.from_env(str(env)).https_insecure is False


@pytest.mark.parametrize("value", [0, 1, "true", "false", {}, []])
def test_https_insecure_api_requires_boolean_or_null(value):
    for zone_mode in (False, True):
        with pytest.raises(ValueError, match="https_insecure"):
            validate_patch({"https_insecure": value}, zone_mode)


def test_https_insecure_false_override_and_null_inheritance():
    zone = {"domain": "example.test", "enabled": 1, "present": 1, "https_insecure": None}
    row = {"record": "@", "ip": "127.0.0.1", "source": "file", "present": 1, "https_insecure": None}
    assert effective(Config(), zone, row)["https_insecure"] is False
    cfg = Config(https_insecure=True)
    assert effective(cfg, zone, row)["https_insecure"] is True
    zone["https_insecure"] = False
    assert effective(cfg, zone, row)["https_insecure"] is False
    row["https_insecure"] = True
    assert effective(cfg, zone, row)["https_insecure"] is True
    row["https_insecure"], zone["https_insecure"] = False, True
    assert effective(cfg, zone, row)["https_insecure"] is False
    for value in (True, False, None):
        assert validate_patch({"https_insecure": value})["https_insecure"] is value


def test_existing_database_migration_preserves_settings_and_is_idempotent(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE zones (
                id INTEGER PRIMARY KEY, filename TEXT, relative_path TEXT UNIQUE, table_name TEXT UNIQUE,
                display_name TEXT, domain TEXT, enabled INTEGER, present INTEGER,
                interval REAL, timeout REAL, retry INTEGER, retry_delay REAL,
                description TEXT, tip TEXT, protocol TEXT
            );
            CREATE TABLE "zone:team/db.example.test" (
                id INTEGER PRIMARY KEY, record TEXT, ip TEXT, alias TEXT, enabled INTEGER,
                source TEXT, present INTEGER, interval REAL, timeout REAL, retry INTEGER,
                retry_delay REAL, description TEXT, tip TEXT, protocol TEXT, UNIQUE(record,ip)
            );
            INSERT INTO zones VALUES (1,'db.example.test','team/db.example.test','zone:team/db.example.test',
                'Saved alias','example.test',0,1,12,3,4,5,'Saved description','Saved tip','https');
            INSERT INTO "zone:team/db.example.test" VALUES (7,'@','127.0.0.1','Record alias',1,
                'manual',1,2,1,3,4,'Record description','Record tip','https');
        ''')
        db.row_factory = sqlite3.Row
        original_zone = dict(db.execute("SELECT * FROM zones").fetchone())
        original_record = dict(db.execute('SELECT * FROM "zone:team/db.example.test"').fetchone())
    store = Store(path)
    try:
        assert store.zone(1) == dict(original_zone, https_insecure=None)
        assert store.records(1) == [dict(original_record, https_insecure=None, maintenance=False, alerted_at=None)]
        store.update_zone(1, {"https_insecure": True})
        store.update_record(1, 7, {"https_insecure": False})
        assert store.zone(1)["https_insecure"] is True
        assert store.records(1)[0]["https_insecure"] is False
        with pytest.raises(ValueError):
            store.bulk([{"zone_id": 1, "patch": {"https_insecure": False}},
                        {"zone_id": 1, "record_id": 7, "patch": {"https_insecure": "true"}}])
        assert store.zone(1)["https_insecure"] is True
        store.bulk([{"zone_id": 1, "record_id": 7, "patch": {"https_insecure": None}}])
        assert store.records(1)[0]["https_insecure"] is None
        snapshot = store.snapshot()
        schema_version = store.db.execute("PRAGMA schema_version").fetchone()[0]
    finally:
        store.close()
    store = Store(path)
    try:
        assert store.snapshot() == snapshot
        assert store.db.execute("PRAGMA schema_version").fetchone()[0] == schema_version
        assert store.db.total_changes == 0
    finally:
        store.close()
