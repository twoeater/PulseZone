import pytest

from pulsezone.config import Config, effective, validate_patch
from pulsezone.storage import Store
from pulsezone.zones import ZoneParser, scan_zones


def test_apex_omitted_owner_and_multiple_addresses(tmp_path):
    file = tmp_path / "db.example.test"
    file.write_text("@ IN A 127.0.0.1\nABC IN A 1.2.3.4\n    IN A 1.2.3.5\nABC IN A 1.2.3.4\n", encoding="utf8")
    assert ZoneParser(tmp_path).parse(file) == [("@", "127.0.0.1"), ("abc", "1.2.3.4"), ("abc", "1.2.3.5")]


def test_origin_soa_ttl_absolute_and_non_a(tmp_path):
    file = tmp_path / "db.wrong.test"
    file.write_text("""; SOA has the origin even if filename differs
example.test. IN SOA ns.example.test. hostmaster.example.test. (
  1 1h 15m 1w 1h )
@ IN NS ns.example.test.
@ IN A 127.0.0.1
api.example.test. 1h IN A 127.0.0.2
$ORIGIN sub.example.test.
$TTL 60
www IN A 127.0.0.3
""", encoding="utf8")
    assert ZoneParser(tmp_path).parse(file) == [("@", "127.0.0.1"), ("api", "127.0.0.2"), ("www.sub", "127.0.0.3")]
    file.write_text("$ORIGIN example.test.\n@ IN A 127.0.0.1\n$ORIGIN sub.example.test.\nwww IN A 127.0.0.2\n", encoding="utf8")
    assert ZoneParser(tmp_path).parse(file) == [("@", "127.0.0.1"), ("www.sub", "127.0.0.2")]


def test_include_restores_owner_origin_ttl_and_detects_cycle(tmp_path):
    (tmp_path / "parts").mkdir()
    child = tmp_path / "parts" / "child"
    child.write_text("@ IN A 127.0.0.2\n$ORIGIN inner.example.test.\nx IN A 127.0.0.3\n", encoding="utf8")
    parent = tmp_path / "db.example.test"
    parent.write_text('$ORIGIN example.test.\nwww IN A 127.0.0.1\n$INCLUDE "parts/child" sub.example.test.\n    IN A 127.0.0.4\n', encoding="utf8")
    assert ZoneParser(tmp_path).parse(parent) == [("sub", "127.0.0.2"), ("www", "127.0.0.1"), ("www", "127.0.0.4"), ("x.inner", "127.0.0.3")]
    child.write_text('$INCLUDE "../db.example.test"\n', encoding="utf8")
    with pytest.raises(ValueError, match="순환"):
        ZoneParser(tmp_path).parse(parent)


def test_include_cannot_leave_root(tmp_path):
    root = tmp_path / "zones"
    root.mkdir()
    (tmp_path / "outside").write_text("@ IN A 127.0.0.1\n")
    file = root / "db.example.test"
    file.write_text('$INCLUDE "../outside"\n')
    with pytest.raises(ValueError, match="내부"):
        ZoneParser(root).parse(file)


def test_reverse_and_failed_scan(tmp_path):
    (tmp_path / "db.127").write_text("$ORIGIN 0.0.127.in-addr.arpa.\n1 IN PTR localhost.\n")
    (tmp_path / "bad").write_text("invalid data\n")
    scan = scan_zones(tmp_path)
    assert scan.complete
    assert scan.parsed["db.127"] == []
    assert "bad" in scan.errors and "bad" in scan.seen
    assert not scan_zones(tmp_path / "missing").complete


def test_sync_preserves_settings_noop_writes_and_missing(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    try:
        parsed = {"a/db.example.test": [("@", "127.0.0.1"), ("api", "127.0.0.2")]}
        store.sync(parsed, parsed)
        zone = store.zones()[0]
        assert zone["domain"] == "db.example.test"
        assert zone["table_name"] == "zone:a/db.example.test"
        apex = store.records(zone["id"])[0]
        store.update_record(zone["id"], apex["id"], {"alias": "main", "interval": 3, "enabled": 1})
        before = store.db.total_changes
        store.sync(parsed, parsed)
        assert store.db.total_changes == before
        store.sync({"a/db.example.test": []}, parsed)
        gone = store.records(zone["id"])[0]
        assert gone["present"] == 0 and gone["enabled"] == 1 and gone["alias"] == "main"
        store.sync(parsed, parsed)
        returned = store.records(zone["id"])[0]
        assert returned["present"] == 1 and returned["interval"] == 3
        # A parse error leaves the last successful snapshot intact.
        before = store.db.total_changes
        store.sync({}, parsed)
        assert store.db.total_changes == before
    finally:
        store.close()


def test_manual_edit_reimport_and_duplicate_filenames(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    try:
        parsed = {"a/db.same.test": [("@", "127.0.0.1")], 'b/db.same.test': [("@", "127.0.0.1")]}
        store.sync(parsed, parsed)
        zone = store.zones()[0]
        assert len({z["table_name"] for z in store.zones()}) == 2
        old = store.records(zone["id"])[0]
        store.update_record(zone["id"], old["id"], {"ip": "127.0.0.2", "alias": "edited"})
        store.sync(parsed, parsed)
        rows = store.records(zone["id"])
        assert len(rows) == 2
        assert next(r for r in rows if r["ip"] == "127.0.0.2")["source"] == "manual"
        store.add_record(zone["id"], {"record": "", "ip": "127.0.0.1"})
        assert next(r for r in store.records(zone["id"]) if r["record"] == "")["protocol"] == "http"
        store.sync({}, [])
        z = store.zone(zone["id"])
        assert z["present"] == 0
        rows = store.records(zone["id"])
        assert all(effective(Config(), z, r)["present"] == (r["source"] == "manual") for r in rows)
    finally:
        store.close()


def test_transaction_rollback_and_quoted_filename(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    try:
        name = 'a/db."quote.test'
        store.sync({name: [("@", "127.0.0.1")]}, [name])
        zone = store.zones()[0]
        with pytest.raises(KeyError):
            store.bulk([{"zone_id": zone["id"], "patch": {"enabled": 0}},
                        {"zone_id": zone["id"], "record_id": 999, "patch": {"enabled": 0}}])
        assert store.zone(zone["id"])["enabled"] == 1
    finally:
        store.close()


@pytest.mark.parametrize("patch", [{"ip": "::1"}, {"ip": "999.1.2.3"}, {"retry": 1.5}, {"timeout": 0},
                                   {"interval": float("nan")}, {"enabled": 2}, {"enabled": True},
                                   {"protocol": "ftp"}, {"domain": "https://bad.test"}])
def test_invalid_values(patch):
    with pytest.raises(ValueError):
        validate_patch(patch, "domain" in patch)


def test_env_and_inheritance(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("CHECK_INTERVAL=22\nRETRY=3\nZONE_PATH=zones\n")
    monkeypatch.setenv("CHECK_INTERVAL", "7")
    monkeypatch.delenv("RETRY", raising=False)
    cfg = Config.from_env(str(env))
    assert cfg.interval == 7 and cfg.retry == 3
    zone = {"domain": "example.test", "enabled": 0, "present": 1, "timeout": 2}
    row = {"record": "@", "ip": "127.0.0.1", "enabled": 1, "source": "file", "present": 1}
    value = effective(cfg, zone, row)
    assert not value["enabled"] and value["timeout"] == 2 and value["interval"] == 7 and value["host"] == "example.test"
    row["record"] = "*.sub"
    assert effective(cfg, zone, row)["host"] == "pulsezone-probe.sub.example.test"
