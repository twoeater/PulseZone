import asyncio
from datetime import datetime, timezone

import pytest
from aiohttp import ClientSession, web

from pulsezone.app import SERVICE, create_app
from pulsezone.config import Config
from pulsezone.probe import Result
from test_runtime import server


class FakeProbe:
    async def check(self, *args):
        return Result(True, "http", 200, 1, "")


def test_alert_test_sends_summary_to_all_channels_without_record_writes(tmp_path):
    async def run():
        received = {}

        async def receive(request):
            channel = request.match_info["channel"]
            body = await request.text() if channel == "ntfy" else await request.json()
            received[channel] = body
            return web.Response(status=204)

        sink = web.Application()
        sink.router.add_post("/{channel}", receive)
        root = tmp_path / "zones"
        root.mkdir()
        (root / "db.example.test").write_text(
            "".join("record{} IN A 127.0.0.1\n".format(i) for i in range(7)), encoding="utf-8")
        async with server(sink) as sink_port:
            channels = ("slack", "discord", "ntfy", "gotify", "custom")
            cfg = Config(zone_path=root, database_path=tmp_path / "settings.sqlite3", interval=60,
                         maintenance=True, alerts={c: "http://127.0.0.1:{}/{}".format(sink_port, c) for c in channels})
            app = create_app(cfg, FakeProbe())
            service = app[SERVICE]
            service.now = lambda: datetime(2026, 10, 28, 14, tzinfo=timezone.utc)
            async with server(app) as port, ClientSession() as client:
                # Freeze checks to make the exact server-wide counts deterministic.
                await service.monitor.close()
                statuses = ("UP", "UP", "DOWN", "OFF", "RETRYING", "MISSING", "UNKNOWN")
                for state, status in zip(service.monitor.states.values(), statuses):
                    state.status = status
                zone = service.catalog[0]
                service.save_alert_state(zone, zone["records"][0], {"alerted_at": service.now().isoformat()})
                before_db = service.store.snapshot()
                before_states = service.monitor.snapshot()
                before_writes = service.store.db.total_changes
                async with client.post("http://127.0.0.1:{}/api/alert-test".format(port), json={}) as response:
                    assert response.status == 202
                    data = await response.json()
                assert data == {"ok": True, "message": "UP 2 Down 1 OFF 1",
                                "counts": {"UP": 2, "DOWN": 1, "OFF": 1}, "channels": list(channels)}
                await asyncio.wait_for(service.alerts.queue.join(), 3)
                message = data["message"]
                assert received == {
                    "slack": {"text": message},
                    "discord": {"content": message, "allowed_mentions": {"parse": []}},
                    "ntfy": message,
                    "gotify": {"title": "PulseZone", "message": message, "priority": 5},
                    "custom": dict.fromkeys(("text", "content", "body", "alert"), message),
                }
                assert all(service.alerts.delivery[c]["ok"] for c in channels)
                assert service.store.snapshot() == before_db
                assert service.store.db.total_changes == before_writes
                assert service.monitor.snapshot() == before_states
    asyncio.run(run())


@pytest.mark.parametrize("queue_full", (False, True))
def test_alert_test_reports_unconfigured_or_full_queue_and_validates_input(tmp_path, queue_full):
    async def run():
        root = tmp_path / "zones"
        root.mkdir()
        cfg = Config(zone_path=root, database_path=tmp_path / "settings.sqlite3",
                     alerts={"custom": "http://127.0.0.1:1/unused"} if queue_full else {})
        app = create_app(cfg, FakeProbe())
        async with server(app) as port, ClientSession() as client:
            service = app[SERVICE]
            # Stop the worker before filling its queue; no network sends occur.
            await service.alerts.close()
            if queue_full:
                for _ in range(service.alerts.queue.maxsize):
                    service.alerts.queue.put_nowait("existing message")
            url = "http://127.0.0.1:{}/api/alert-test".format(port)
            for body in ([], None, {"message": "Do not accept custom messages"}):
                async with client.post(url, json=body) as response:
                    assert response.status == 400
            async with client.post(url, data="{}", headers={"Content-Type": "text/plain"}) as response:
                assert response.status == 400
            async with client.get(url) as response:
                assert response.status == 405
            async with client.post(url, json={}) as response:
                assert response.status == (503 if queue_full else 400)
                assert ("대기열" if queue_full else "알림 채널") in (await response.json())["error"]
            assert service.alerts.queue.qsize() == (service.alerts.queue.maxsize if queue_full else 0)
    asyncio.run(run())
