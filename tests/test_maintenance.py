import asyncio
import datetime as dt

import pytest
from aiohttp import ClientSession, web

from pulsezone.app import SERVICE, Service, create_app
from pulsezone.config import Config, validate_patch
from pulsezone.maintenance import expired, suppressed
from pulsezone.probe import Result
from pulsezone.storage import Store
from test_runtime import server


KST = dt.timezone(dt.timedelta(hours=9))


def at(value):
    return dt.datetime.fromisoformat(value).replace(tzinfo=KST)


@pytest.mark.parametrize("now,last,expire,mute", [
    ("2026-10-28T12:59:59", "2026-10-28T10:00:00+09:00", False, True),
    ("2026-10-28T13:00:00", "2026-10-28T12:59:59.999999+09:00", True, True),
    ("2026-10-28T13:00:00", "2026-10-28T13:00:00+09:00", False, True),
    ("2026-10-28T15:00:00", "2026-10-28T14:00:00+09:00", False, True),
    ("2026-10-29T00:00:00", "2026-10-28T23:59:59+09:00", False, False),
    ("2026-10-29T12:59:59", "2026-10-28T23:59:59+09:00", False, False),
    ("2026-10-29T13:00:00", "2026-10-28T23:59:59+09:00", True, False),
    ("2026-10-30T14:00:00", "2026-10-28T23:59:59+09:00", True, False),
    ("2026-10-29T00:01:00", "2026-10-28T15:00:00+00:00", False, True),
    ("2026-10-29T13:00:00", "2026-10-29T03:59:59+00:00", True, True),
    ("2026-10-29T15:00:00", None, False, False),
])
def test_calendar_rules(now, last, expire, mute):
    row = {"maintenance": True, "alerted_at": last}
    assert expired(row, at(now)) is expire
    assert suppressed(row, at(now)) is mute
    row["maintenance"] = False
    assert not expired(row, at(now)) and not suppressed(row, at(now))


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", {}, []])
def test_maintenance_requires_boolean_and_is_record_only(value):
    with pytest.raises(ValueError):
        validate_patch({"maintenance": value})
    with pytest.raises(ValueError):
        validate_patch({"maintenance": True}, is_zone=True)
    with pytest.raises(ValueError):
        validate_patch({"alerted_at": "2026-10-28T10:00:00+09:00"})


def test_alert_columns_preserved_on_sync_and_reopen(tmp_path):
    path = tmp_path / "records.sqlite3"
    store = Store(path)
    parsed = {"db.example.test": [("@", "127.0.0.1")]}
    store.sync(parsed, parsed)
    zone = store.zones()[0]
    row = store.records(zone["id"])[0]
    assert row["maintenance"] is False and row["alerted_at"] is None
    store.update_record(zone["id"], row["id"], {"maintenance": True})
    stamp = "2026-10-28T14:00:00.000000+09:00"
    store.save_alert_state(zone, row, {"alerted_at": stamp})
    baseline = store.db.total_changes
    store.sync(parsed, parsed)
    assert store.db.total_changes == baseline
    with pytest.raises(ValueError):
        store.bulk([{"zone_id": zone["id"], "record_id": row["id"], "patch": {"maintenance": False}},
                    {"zone_id": zone["id"], "record_id": row["id"], "patch": {"alerted_at": None}}])
    assert store.records(zone["id"])[0]["maintenance"] is True
    store.sync({"db.example.test": []}, parsed)
    store.sync(parsed, parsed)
    saved = store.records(zone["id"])[0]
    assert saved["alerted_at"] == stamp and saved["maintenance"] is True
    store.close()
    store = Store(path)
    try:
        assert store.records(zone["id"])[0] == saved
        assert store.db.total_changes == 0
    finally:
        store.close()


def test_only_accepted_alert_triggers_write_and_startup_expiry_covers_off_records(tmp_path):
    async def run():
        cfg = Config(database_path=tmp_path / "events.sqlite3")
        svc = Service(cfg)
        now = [at("2026-10-28T10:00:00")]
        svc.now = lambda: now[0]
        svc.store.sync({"db.example.test": [("@", "127.0.0.1")]}, ["db.example.test"])
        zone = svc.store.zones()[0]
        svc.store.update_zone(zone["id"], {"enabled": 0})
        svc.changed()
        key = next(iter(svc.monitor.targets))
        target = svc.monitor.targets[key]
        baseline = svc.store.db.total_changes
        svc.emit_alert(target, "no channels")
        assert target["row"]["alerted_at"] is None
        assert svc.store.db.total_changes == baseline
        cfg.alerts["custom"] = "http://unused.invalid/"
        svc.alerts.queue = asyncio.Queue(maxsize=1)
        svc.alerts.queue.put_nowait("full")
        svc.emit_alert(target, "queue full")
        assert svc.alerts.dropped == 1 and svc.store.db.total_changes == baseline
        svc.alerts.queue.get_nowait()
        svc.alerts.queue.task_done()
        svc.emit_alert(target, "accepted")
        assert svc.store.db.total_changes == baseline + 1
        assert target["row"]["alerted_at"] == now[0].isoformat(timespec="microseconds")
        svc.store.update_record(zone["id"], target["row"]["id"], {"maintenance": True})
        svc.changed()
        assert svc.monitor.snapshot()[key]["maintenance"] is True
        await svc.close()

        svc = Service(cfg)
        svc.now = lambda: at("2026-10-30T14:00:00")
        try:
            svc.changed()
            state = svc.monitor.snapshot()[key]
            assert state["status"] == "OFF" and state["maintenance"] is False
            assert state["alerted_at"] == now[0].isoformat(timespec="microseconds")
            assert svc.public_catalog[0]["records"][0]["maintenance"] is False
            baseline = svc.store.db.total_changes
            svc.expire_maintenance()
            svc.expire_maintenance()
            assert svc.store.db.total_changes == baseline
        finally:
            await svc.close()
    asyncio.run(run())


def test_monitor_triggers_maintenance_suppression_midnight_expiry_and_recovery(tmp_path):
    async def run():
        received = []

        async def receive(request):
            received.append(await request.json())
            return web.Response(status=204)

        sink = web.Application()
        sink.router.add_post("/", receive)
        root = tmp_path / "zones"
        root.mkdir()
        (root / "db.example.test").write_text("@ IN A 127.0.0.1\n", encoding="utf-8")

        class ControlledProbe:
            def __init__(self):
                self.results = asyncio.Queue()

            async def check(self, *args):
                return await self.results.get()

        async with server(sink) as alert_port:
            cfg = Config(zone_path=root, database_path=tmp_path / "runtime.sqlite3", interval=.001,
                         retry_delay=.001, retry=2, alerts={"custom": f"http://127.0.0.1:{alert_port}/"})
            probe = ControlledProbe()
            app = create_app(cfg, probe)
            svc = app[SERVICE]
            now = [at("2026-10-28T10:00:00")]
            svc.now = lambda: now[0]
            async with server(app) as port, ClientSession() as client:
                base = f"http://127.0.0.1:{port}"
                zone = svc.public_catalog[0]
                z, r = zone["id"], zone["records"][0]["id"]
                key = f"{z}:{r}"

                async def checked(ok=False):
                    count = len(svc.monitor.states[key].history)
                    probe.results.put_nowait(Result(ok, "http", 200 if ok else 503, 1, "" if ok else "HTTP 503"))
                    async def wait():
                        while len(svc.monitor.states[key].history) == count:
                            await asyncio.sleep(.001)
                    await asyncio.wait_for(wait(), 2)
                    await asyncio.wait_for(svc.alerts.queue.join(), 2)

                async with client.post(base + "/api/bulk-update", json={"operations": [
                    {"zone_id": z, "record_id": r, "patch": {"maintenance": True}}
                ]}) as response:
                    assert response.status == 200
                baseline = svc.store.db.total_changes
                await checked()
                assert not received and svc.store.db.total_changes == baseline
                await checked()
                stamp = now[0].isoformat(timespec="microseconds")
                assert len(received) == 1 and svc.store.records(z)[0]["alerted_at"] == stamp
                assert svc.store.db.total_changes == baseline + 1
                for ok in (False, False, True, False, False):
                    await checked(ok)
                assert len(received) == 1  # Same-day down AND recovery events are suppressed.
                assert svc.store.db.total_changes == baseline + 1
                assert svc.monitor.states[key].failures == 2 and len(svc.monitor.states[key].history) == 7

                now[0] = at("2026-10-29T00:01:00")
                await checked()
                await checked()
                assert len(received) == 2
                stamp = now[0].isoformat(timespec="microseconds")
                now[0] = at("2026-10-29T12:59:59")
                svc.expire_maintenance()
                assert svc.store.records(z)[0]["maintenance"] is True
                now[0] = at("2026-10-29T13:00:00")
                svc.expire_maintenance()
                assert svc.store.records(z)[0]["maintenance"] is False
                assert svc.store.records(z)[0]["alerted_at"] == stamp
                # Repeated expiry passes and status reads do not write to disk.
                baseline = svc.store.db.total_changes
                svc.expire_maintenance()
                async with client.get(base + "/api/status") as response:
                    row = (await response.json())["records"][key]
                    assert row["maintenance"] is False and row["alerted_at"] == stamp
                assert svc.store.db.total_changes == baseline
                await checked(True)
                assert len(received) == 3 and "응답 복구" in received[-1]["text"]
                stamp = now[0].isoformat(timespec="microseconds")
                async with client.get(base + f"/api/zones/{z}/records") as response:
                    row = (await response.json())["records"][0]
                    assert row["alerted_at"] == stamp and row["maintenance"] is False
                # With maintenance off, ordinary repeat alerts continue on the same date.
                for minute in (1, 2):
                    now[0] = at(f"2026-10-29T13:0{minute}:00")
                    await checked()
                    await checked()
                assert len(received) == 5
                async with client.patch(base + f"/api/zones/{z}/records/{r}", json={"maintenance": True}) as response:
                    assert response.status == 200
                await checked(True)
                assert len(received) == 5
                assert svc.store.records(z)[0]["maintenance"] is True  # Last alert is AFTER 13:00.
                for patch in ({"maintenance": "true"}, {"maintenance": None}, {"alerted_at": None}):
                    async with client.patch(base + f"/api/zones/{z}/records/{r}", json=patch) as response:
                        assert response.status == 400
                async with client.patch(base + f"/api/zones/{z}", json={"maintenance": True}) as response:
                    assert response.status == 400
    asyncio.run(run())
