import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .config import validate_patch


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


SETTINGS_SQL = """
    interval REAL CHECK(interval > 0), timeout REAL CHECK(timeout > 0),
    retry INTEGER CHECK(retry >= 1), retry_delay REAL CHECK(retry_delay > 0),
    description TEXT NOT NULL DEFAULT '', tip TEXT NOT NULL DEFAULT '',
    protocol TEXT CHECK(protocol IN ('auto', 'http', 'https')),
    https_insecure INTEGER CHECK(https_insecure IN (0,1)),
    maintenance INTEGER CHECK(maintenance IN (0,1))
"""

RECORD_SQL = """ (
    id INTEGER PRIMARY KEY, record TEXT NOT NULL, ip TEXT NOT NULL,
    alias TEXT, enabled INTEGER CHECK(enabled IN (0,1)),
    source TEXT NOT NULL CHECK(source IN ('file','manual')),
    present INTEGER NOT NULL DEFAULT 1 CHECK(present IN (0,1)),
    alerted_at TEXT,
""" + SETTINGS_SQL + ", UNIQUE(record,ip))"


class Store:
    """Called only from the event-loop thread. No metric writes occur here."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS zones (
            id INTEGER PRIMARY KEY, filename TEXT NOT NULL,
            relative_path TEXT NOT NULL UNIQUE, table_name TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL, domain TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            present INTEGER NOT NULL DEFAULT 1 CHECK(present IN (0,1)),
        """ + SETTINGS_SQL + ")")
        self.db.commit()
        self._migrate_columns()

    def _migrate_columns(self) -> None:
        tables = ["zones"] + [row[0] for row in self.db.execute("SELECT table_name FROM zones")]
        missing = []
        rebuild = []
        for table in tables:
            columns = {row["name"]: row for row in self.db.execute("PRAGMA table_info(" + quote(table) + ")")}
            additions = {"https_insecure": "INTEGER CHECK(https_insecure IN (0,1))",
                         "maintenance": "INTEGER CHECK(maintenance IN (0,1))"}
            if table != "zones":
                additions["alerted_at"] = "TEXT"
                old = columns.get("maintenance")
                if old is not None and (old["notnull"] or old["dflt_value"] not in (None, "NULL")):
                    rebuild.append(table)
            missing.extend((table, name, sql) for name, sql in additions.items() if name not in columns)
        if missing or rebuild:
            with self.db:
                self.db.execute("BEGIN")
                for table, name, sql in missing:
                    self.db.execute("ALTER TABLE " + quote(table) + " ADD COLUMN " + quote(name) + " " + sql)
                for table in rebuild:
                    self._make_maintenance_nullable(table)

    def _make_maintenance_nullable(self, table: str) -> None:
        # SQLite versions shipped with Python 3.9 cannot ALTER a column's nullability.
        # Copy explicit values unchanged; only new records default to inheritance.
        temporary = "pulsezone:migration:" + uuid.uuid4().hex
        objects = [row[0] for row in self.db.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
            (table,))]
        columns = ",".join(quote(row["name"]) for row in self.db.execute("PRAGMA table_info(" + quote(table) + ")"))
        self.db.execute("CREATE TABLE " + quote(temporary) + RECORD_SQL)
        self.db.execute("INSERT INTO " + quote(temporary) + " (" + columns + ") SELECT " + columns + " FROM " + quote(table))
        self.db.execute("DROP TABLE " + quote(table))
        self.db.execute("ALTER TABLE " + quote(temporary) + " RENAME TO " + quote(table))
        for sql in objects:
            self.db.execute(sql)

    @staticmethod
    def _settings(row) -> Dict[str, Any]:
        result = dict(row)
        for key in ("https_insecure", "maintenance"):
            if result.get(key) is not None:
                result[key] = bool(result[key])
        return result

    def close(self) -> None:
        self.db.close()

    def zones(self) -> List[Dict[str, Any]]:
        return [self._settings(row) for row in self.db.execute("SELECT * FROM zones ORDER BY relative_path")]

    def zone(self, zone_id: int) -> Dict[str, Any]:
        row = self.db.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
        if row is None:
            raise KeyError("존을 찾을 수 없습니다")
        return self._settings(row)

    def records(self, zone_id: int) -> List[Dict[str, Any]]:
        table = quote(self.zone(zone_id)["table_name"])
        return [self._settings(row) for row in self.db.execute("SELECT * FROM " + table + " ORDER BY record, ip")]

    def snapshot(self) -> List[Dict[str, Any]]:
        return [dict(zone, records=self.records(zone["id"])) for zone in self.zones()]

    def _create_zone(self, relative: str) -> Dict[str, Any]:
        filename = relative.rsplit("/", 1)[-1]
        table_name = "zone:" + relative
        cursor = self.db.execute(
            "INSERT INTO zones(filename,relative_path,table_name,display_name,domain) VALUES(?,?,?,?,?)",
            (filename, relative, table_name, filename, filename),
        )
        self.db.execute("CREATE TABLE " + quote(table_name) + RECORD_SQL)
        return self.zone(cursor.lastrowid)

    def sync(self, parsed: Dict[str, Iterable[Tuple[str, str]]], seen: Iterable[str]) -> None:
        """Apply a complete scan; seen includes files that failed to parse."""
        seen_set = set(seen)
        with self.db:
            known = {row["relative_path"]: row for row in self.zones()}
            for relative, pairs in parsed.items():
                zone = known.get(relative) or self._create_zone(relative)
                table = quote(zone["table_name"])
                if not zone["present"]:
                    self.db.execute("UPDATE zones SET present=1 WHERE id=?", (zone["id"],))
                incoming = set(pairs)
                existing = {(r["record"], r["ip"]): r for r in self.records(zone["id"])}
                for pair in sorted(incoming):
                    row = existing.get(pair)
                    if row is None:
                        self.db.execute("INSERT INTO " + table + " (record,ip,source) VALUES(?,?,'file')", pair)
                    elif row["source"] == "file" and not row["present"]:
                        self.db.execute("UPDATE " + table + " SET present=1 WHERE id=?", (row["id"],))
                for pair, row in existing.items():
                    if row["source"] == "file" and row["present"] and pair not in incoming:
                        self.db.execute("UPDATE " + table + " SET present=0 WHERE id=?", (row["id"],))
            for relative, zone in known.items():
                if relative not in seen_set and zone["present"]:
                    self.db.execute("UPDATE zones SET present=0 WHERE id=?", (zone["id"],))

    def _update(self, table: str, old: Dict[str, Any], values: Dict[str, Any]) -> None:
        changed = {k: v for k, v in values.items() if old.get(k) != v}
        if changed:
            assignment = ",".join(quote(k) + "=?" for k in changed)
            self.db.execute("UPDATE " + quote(table) + " SET " + assignment + " WHERE id=?",
                            tuple(changed.values()) + (old["id"],))

    def _update_zone(self, zone_id: int, patch: Dict[str, Any]) -> None:
        zone = self.zone(zone_id)
        self._update("zones", zone, validate_patch(patch, True))

    def _update_record(self, zone_id: int, record_id: int, patch: Dict[str, Any]) -> None:
        table = self.zone(zone_id)["table_name"]
        row = self.db.execute("SELECT * FROM " + quote(table) + " WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise KeyError("레코드를 찾을 수 없습니다")
        old = dict(row)
        values = validate_patch(patch)
        if any(k in values and values[k] != old[k] for k in ("record", "ip")):
            values.update(source="manual", present=1)
            if values.get("record", old["record"]) == "" and values.get("protocol", old["protocol"]) is None:
                values["protocol"] = "http"
        self._update(table, old, values)

    def update_zone(self, zone_id: int, patch: Dict[str, Any]) -> None:
        with self.db:
            self._update_zone(zone_id, patch)

    def update_record(self, zone_id: int, record_id: int, patch: Dict[str, Any]) -> None:
        with self.db:
            self._update_record(zone_id, record_id, patch)

    def save_alert_state(self, zone: Dict[str, Any], row: Dict[str, Any], patch: Dict[str, Any]) -> None:
        """Internal event writes only; alerted_at is deliberately not editable through the API."""
        if set(patch) - {"maintenance", "alerted_at"}:
            raise ValueError("잘못된 알림 상태 필드입니다")
        with self.db:
            self._update(zone["table_name"], row, patch)

    def add_record(self, zone_id: int, data: Dict[str, Any]) -> int:
        table = self.zone(zone_id)["table_name"]
        values = validate_patch(data)
        values.setdefault("record", "")
        if "ip" not in values:
            raise ValueError("IP를 입력하세요")
        if values["record"] == "" and values.get("protocol") is None:
            values["protocol"] = "http"
        values["source"] = "manual"
        with self.db:
            cur = self.db.execute("INSERT INTO " + quote(table) + " (" + ",".join(quote(k) for k in values)
                                  + ") VALUES(" + ",".join("?" for _ in values) + ")", tuple(values.values()))
            return cur.lastrowid

    def bulk(self, operations: Any) -> None:
        if not isinstance(operations, list) or not operations or len(operations) > 10000:
            raise ValueError("operations는 1~10000개 작업 목록이어야 합니다")
        with self.db:
            for op in operations:
                if not isinstance(op, dict) or set(op) - {"zone_id", "record_id", "patch"}:
                    raise ValueError("잘못된 일괄 작업입니다")
                if type(op.get("zone_id")) is not int or not isinstance(op.get("patch"), dict):
                    raise ValueError("zone_id와 patch가 필요합니다")
                if "record_id" in op:
                    if type(op["record_id"]) is not int:
                        raise ValueError("record_id는 정수여야 합니다")
                    self._update_record(op["zone_id"], op["record_id"], op["patch"])
                else:
                    self._update_zone(op["zone_id"], op["patch"])
