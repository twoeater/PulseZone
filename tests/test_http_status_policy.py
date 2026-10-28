import asyncio
import copy
import os

import pytest
from aiohttp import ClientSession, web

from pulsezone.app import SERVICE, create_app
from pulsezone.config import HTTP_STATUS_SETTINGS, Config, effective, validate_patch
from pulsezone.monitor import Monitor
from pulsezone.probe import Probe, Result
from pulsezone.storage import Store
from test_runtime import server


@pytest.mark.parametrize("setting,default", zip(HTTP_STATUS_SETTINGS, (True, True, False, False)))
def test_status_environment_inheritance_and_validation(tmp_path, monkeypatch, setting, default):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv(setting.upper(), raising=False)
    assert getattr(Config.from_env(str(tmp_path / "missing.env")), setting) is default
    env = tmp_path / ".env"
    env.write_text(setting.upper() + "=true\n", encoding="utf-8")
    assert getattr(Config.from_env(str(env)), setting) is True
    monkeypatch.setenv(setting.upper(), " FALSE ")
    assert getattr(Config.from_env(str(env)), setting) is False
    for invalid in ("", "1", "yes", "invalid"):
        monkeypatch.setenv(setting.upper(), invalid)
        with pytest.raises(ValueError, match=setting.upper()):
            Config.from_env(str(env))
    for zone_mode in (False, True):
        for invalid in (0, 1, "true", "false", [], {}):
            with pytest.raises(ValueError, match=setting):
                validate_patch({setting: invalid}, zone_mode)
        for value in (True, False, None):
            assert validate_patch({setting: value}, zone_mode)[setting] is value
    cfg = Config(**{setting: True})
    zone = {"domain": "example.test", "enabled": 1, "present": 1, setting: None}
    row = {"record": "@", "ip": "127.0.0.1", "source": "file", "present": 1, setting: None}
    assert effective(cfg, zone, row)[setting] is True
    zone[setting] = False
    assert effective(cfg, zone, row)[setting] is False
    row[setting] = True
    assert effective(cfg, zone, row)[setting] is True
    row[setting], zone[setting] = False, True
    assert effective(cfg, zone, row)[setting] is False
    row[setting] = None
    assert effective(cfg, zone, row)[setting] is True


def test_status_class_boundaries_and_concurrent_policies():
    async def run():
        response_status, calls = [200], []

        async def handler(request):
            calls.append(request.path)
            return web.Response(status=response_status[0], headers={"Location": "/must-not-follow"})

        app = web.Application()
        app.router.add_get("/{path:.*}", handler)
        async with server(app) as port:
            probe = Probe(ports={"http": port})
            for status in (200, 204, 299, 300, 302, 399, 400, 404, 499, 500, 503, 599):
                response_status[0] = status
                group = status // 100
                policies = (None, (), (group,), tuple(g for g in range(2, 6) if g != group))
                results = await asyncio.gather(*(
                    probe.check("example.test", "127.0.0.1", "http", 1, allowed_status_classes=policy)
                    for policy in policies))
                assert [result.ok for result in results] == [group in (2, 3), False, True, False]
                for result in results:
                    assert result.status == status and len(result.attempts) == 1
                    assert bool(result.error) is not result.ok
            # Nonstandard status codes outside the four configurable classes still fail.
            response_status[0] = 600
            assert not (await probe.check("example.test", "127.0.0.1", "http", 1,
                                          allowed_status_classes=(2, 3, 4, 5))).ok
            assert len(calls) == 49 and set(calls) == {"/"}
    asyncio.run(run())


def test_status_settings_persist_across_sync_and_reopen(tmp_path):
    path = tmp_path / "settings.sqlite3"
    store = Store(path)
    try:
        files = {"db.example.test": [("@", "127.0.0.1")]}
        store.sync(files, files)
        zone = store.zones()[0]
        record = store.records(zone["id"])[0]
        assert all(zone[key] is None and record[key] is None for key in HTTP_STATUS_SETTINGS)
        zone_patch = dict(zip(HTTP_STATUS_SETTINGS, (False, True, False, True)))
        record_patch = dict(zip(HTTP_STATUS_SETTINGS, (True, False, True, False)))
        store.update_zone(zone["id"], zone_patch)
        store.update_record(zone["id"], record["id"], record_patch)
        changes = store.db.total_changes
        store.sync(files, files)
        assert store.db.total_changes == changes
        store.sync({"db.example.test": []}, files)
        store.sync(files, files)
        snapshot = store.snapshot()
        assert snapshot[0]["records"][0]["source"] == "file"
        assert {key: snapshot[0][key] for key in HTTP_STATUS_SETTINGS} == zone_patch
        assert {key: snapshot[0]["records"][0][key] for key in HTTP_STATUS_SETTINGS} == record_patch
    finally:
        store.close()
    store = Store(path)
    try:
        assert store.snapshot() == snapshot
        assert store.db.total_changes == 0
    finally:
        store.close()


async def wait_for(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(.005)
    await asyncio.wait_for(poll(), 3)


@pytest.mark.parametrize("group,setting", enumerate(HTTP_STATUS_SETTINGS, 2))
def test_api_policy_reaches_live_checks_and_bulk_is_atomic(tmp_path, group, setting):
    async def run():
        zones = tmp_path / "zones"
        zones.mkdir()
        (zones / "db.example.test").write_text("@ IN A 127.0.0.1\n", encoding="utf-8")
        web_app = web.Application()

        async def response(request):
            return web.Response(status=group * 100)

        web_app.router.add_get("/", response)
        async with server(web_app) as target_port:
            cfg = Config(zone_path=zones, database_path=tmp_path / "api.sqlite3", protocol="http",
                         interval=60, retry=1, retry_delay=60, **dict.fromkeys(HTTP_STATUS_SETTINGS, False))
            app = create_app(cfg, Probe(ports={"http": target_port}))
            service = app[SERVICE]
            alerts = []
            service.alerts.submit = lambda text: alerts.append(text) or True
            async with server(app) as port, ClientSession() as session:
                base = "http://127.0.0.1:" + str(port)

                async def catalog():
                    async with session.get(base + "/api/zones") as result:
                        return (await result.json())["zones"][0]

                async def update(path, patch, method="PATCH", status=200):
                    async with session.request(method, base + path, json=patch) as result:
                        assert result.status == status, await result.text()

                zone = await catalog()
                row = zone["records"][0]
                key = service.monitor.key(zone["id"], row["id"])
                zone_url = "/api/zones/" + str(zone["id"])
                record_url = zone_url + "/records/" + str(row["id"])
                await wait_for(lambda: service.monitor.states[key].status == "DOWN")
                assert len(alerts) == 1
                alerted_at = (await catalog())["records"][0]["alerted_at"]
                await update(zone_url, {setting: True})
                await wait_for(lambda: service.monitor.states[key].status == "UP")
                current = (await catalog())["records"][0]
                assert current[setting] is None and current["effective"][setting] is True
                assert current["alerted_at"] == alerted_at and len(alerts) == 1
                assert service.monitor.states[key].latest["status"] == group * 100
                await update(record_url, {setting: False})
                await wait_for(lambda: service.monitor.states[key].status == "DOWN")
                assert len(alerts) == 2
                await update("/api/bulk-update", {"operations": [
                    {"zone_id": zone["id"], "patch": {setting: False}},
                    {"zone_id": zone["id"], "record_id": row["id"], "patch": {setting: "true"}}
                ]}, "POST", 400)
                assert (await catalog())[setting] is True
                await update("/api/bulk-update", {"operations": [
                    {"zone_id": zone["id"], "patch": {setting: True}},
                    {"zone_id": zone["id"], "record_id": row["id"], "patch": {setting: None}}
                ]}, "POST")
                await wait_for(lambda: service.monitor.states[key].status == "UP")
                current = (await catalog())["records"][0]
                assert current[setting] is None and current["effective"][setting] is True
                assert len(service.monitor.states[key].history) == 1 and len(alerts) == 2
                await update(zone_url + "/records", {"record": "manual", "ip": "127.0.0.1", setting: True}, "POST", 201)
                manual = next(r for r in (await catalog())["records"] if r["record"] == "manual")
                assert manual[setting] is True and manual["source"] == "manual"
                await update(zone_url, {"enabled": 0})
                assert all(s.status == "OFF" for s in service.monitor.states.values())
    asyncio.run(run())


def test_policy_change_discards_inflight_result_and_keeps_new_history():
    async def run():
        first_started, second_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []

        class PendingProbe:
            async def check(self, host, ip, protocol, timeout, https_insecure, allowed_status_classes):
                seen.append(allowed_status_classes)
                if len(seen) == 1:
                    first_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        return Result(False, "http", 503, 1, "Old policy")
                second_started.set()
                await release.wait()
                return Result(True, "http", 503, 1, "")

        class Alerts:
            def submit(self, text):
                pytest.fail("A stale result must not generate an alert")

        zone = {"id": 1, "display_name": "Example", "domain": "example.test", "enabled": 1, "present": 1,
                "records": [{"id": 1, "record": "@", "ip": "127.0.0.1", "source": "file", "present": 1}]}
        monitor = Monitor(Config(interval=60, retry=1), Alerts(), PendingProbe())
        try:
            monitor.reconcile([zone])
            await asyncio.wait_for(first_started.wait(), 3)
            changed = copy.deepcopy(zone)
            changed["http_allow_5xx"] = True
            monitor.reconcile([changed])
            await asyncio.wait_for(second_started.wait(), 3)
            assert monitor.states["1:1"].latest is None
            release.set()
            await wait_for(lambda: monitor.states["1:1"].status == "UP")
            assert seen == [(2, 3), (2, 3, 5)]
            assert len(monitor.states["1:1"].history) == 1
            assert monitor.states["1:1"].failures == 0
        finally:
            await monitor.close()
    asyncio.run(run())
