import argparse
import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

from aiohttp import web

from .alerts import Alerts
from .config import Config, effective
from .maintenance import expired, local_now, suppressed
from .monitor import Monitor
from .storage import Store
from .zones import scan_zones


STATIC = Path(__file__).parent / "static"
SERVICE = web.AppKey("service", object)


class Service:
    def __init__(self, config, probe=None):
        self.config = config
        self.store = Store(config.database_path)
        self.alerts = Alerts(config)
        self.monitor = Monitor(config, self.alerts, probe, on_alert=self.emit_alert)
        self.now = local_now
        self.scan_lock = asyncio.Lock()
        self.scan_errors = {}
        self.last_scan = None
        self.periodic = None
        self.maintenance_task = None
        self.revision = 0
        self.catalog = []
        self.public_catalog = []
        self.public_records = {}

    def changed(self):
        self.catalog = self.store.snapshot()
        self.public_catalog = []
        self.public_records = {}
        for zone in self.catalog:
            public = dict(zone)
            public["effective"] = self.config.defaults()
            public["effective"].update({k: zone[k] for k in self.config.defaults() if zone[k] is not None})
            public["records"] = [dict(r, effective=effective(self.config, zone, r)) for r in zone["records"]]
            self.public_records.update({(zone["id"], r["id"]): r for r in public["records"]})
            self.public_catalog.append(public)
        self.expire_maintenance()
        self.monitor.reconcile(self.catalog)
        self.revision += 1

    def save_alert_state(self, zone, row, patch):
        self.store.save_alert_state(zone, row, patch)
        row.update(patch)
        public = self.public_records[(zone["id"], row["id"])]
        public.update(patch)
        public["effective"] = effective(self.config, zone, row)
        self.revision += 1

    def emit_alert(self, target, text):
        zone, row, now = target["zone"], target["row"], self.now()
        active = effective(self.config, zone, row)["maintenance"]
        if expired(row, now, enabled=active):
            self.save_alert_state(zone, row, {"maintenance": False})
            active = False
        if suppressed(row, now, enabled=active):
            return
        # No await between accepting the event and persisting its timestamp.
        # This records an alert trigger, not a webhook delivery receipt.
        if self.alerts.submit(text):
            self.save_alert_state(zone, row, {"alerted_at": now.isoformat(timespec="microseconds")})

    def expire_maintenance(self):
        now = self.now()
        for zone in self.catalog:
            for row in zone["records"]:
                if expired(row, now, enabled=effective(self.config, zone, row)["maintenance"]):
                    self.save_alert_state(zone, row, {"maintenance": False})

    async def _maintenance(self):
        while True:
            self.expire_maintenance()
            await asyncio.sleep(30)

    async def refresh(self):
        async with self.scan_lock:
            db = self.config.database_path.resolve()
            excluded = {db, Path(str(db) + "-wal"), Path(str(db) + "-shm"), Path(str(db) + "-journal")}
            try:
                scan = await asyncio.to_thread(scan_zones, self.config.zone_path, excluded)
                self.scan_errors = scan.errors
                if scan.complete:
                    self.store.sync(scan.parsed, scan.seen)
                    self.changed()
            except Exception as exc:
                self.scan_errors = {"scan": str(exc)}
            self.last_scan = time.time()

    async def _periodic(self):
        while True:
            await asyncio.sleep(self.config.zone_refresh_time)
            await self.refresh()

    async def start(self):
        await self.alerts.start()
        self.changed()
        await self.refresh()
        self.periodic = asyncio.create_task(self._periodic())
        self.maintenance_task = asyncio.create_task(self._maintenance())

    async def close(self):
        tasks = [task for task in (self.periodic, self.maintenance_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.monitor.close()
        await self.alerts.close()
        self.store.close()


@web.middleware
async def errors(request, handler):
    try:
        # Mutations are JSON-only; cross-site HTML forms cannot edit settings.
        if request.method in ("POST", "PATCH") and request.content_type != "application/json":
            raise ValueError("Content-Type: application/json이 필요합니다")
        response = await handler(request)
    except (ValueError, json.JSONDecodeError) as exc:
        response = web.json_response({"error": str(exc)}, status=400)
    except KeyError as exc:
        response = web.json_response({"error": str(exc.args[0])}, status=404)
    except sqlite3.IntegrityError:
        response = web.json_response({"error": "같은 레코드·IP가 이미 있거나 제약 조건에 맞지 않습니다"}, status=409)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
    return response


async def catalog(request):
    service = request.app[SERVICE]
    return web.json_response({"zones": service.public_catalog, "defaults": service.config.defaults(),
                              "record_defaults": {"maintenance": None}, "revision": service.revision})


async def update_zone(request):
    service = request.app[SERVICE]
    service.store.update_zone(int(request.match_info["zone"]), await request.json())
    service.changed()
    return web.json_response({"ok": True})


async def records(request):
    service = request.app[SERVICE]
    zone_id = int(request.match_info["zone"])
    if request.method == "GET":
        service.store.zone(zone_id)
        zone = next(z for z in service.public_catalog if z["id"] == zone_id)
        return web.json_response({"records": zone["records"]})
    record_id = service.store.add_record(zone_id, await request.json())
    service.changed()
    return web.json_response({"id": record_id}, status=201)


async def update_record(request):
    service = request.app[SERVICE]
    service.store.update_record(int(request.match_info["zone"]), int(request.match_info["record"]), await request.json())
    service.changed()
    return web.json_response({"ok": True})


async def bulk(request):
    service = request.app[SERVICE]
    data = await request.json()
    if not isinstance(data, dict) or set(data) != {"operations"}:
        raise ValueError("operations 목록이 필요합니다")
    service.store.bulk(data["operations"])
    service.changed()
    return web.json_response({"ok": True})


async def status(request):
    service = request.app[SERVICE]
    return web.json_response({"records": service.monitor.snapshot(), "revision": service.revision,
                              "scan_errors": service.scan_errors, "last_scan": service.last_scan,
                              "alerts": service.alerts.delivery, "dropped_alerts": service.alerts.dropped})


async def alert_test(request):
    service = request.app[SERVICE]
    if await request.json() != {}:
        raise ValueError("빈 JSON 객체 {}를 보내세요")
    if not service.config.alerts:
        raise ValueError("설정된 알림 채널이 없습니다. ALERT_* 설정을 확인하세요.")
    counts = dict.fromkeys(("UP", "DOWN", "OFF"), 0)
    for state in service.monitor.states.values():
        if state.status in counts:
            counts[state.status] += 1
    text = "UP {UP} Down {DOWN} OFF {OFF}".format(**counts)
    # A manual summary test must not change per-record alert timestamps or maintenance.
    if not service.alerts.submit(text):
        return web.json_response({"error": "알림 대기열이 가득 찼습니다. 잠시 후 다시 시도하세요."}, status=503)
    return web.json_response({"ok": True, "message": text, "counts": counts,
                              "channels": list(service.config.alerts)}, status=202)


async def refresh(request):
    await request.app[SERVICE].refresh()
    return await status(request)


async def health(request):
    service = request.app[SERVICE]
    return web.json_response({"ok": True, "last_scan": service.last_scan, "scan_errors": len(service.scan_errors)})


async def static(request):
    filename = request.match_info.get("file", "index.html")
    if filename not in ("index.html", "app.js", "style.css", "icon.svg"):
        raise web.HTTPNotFound()
    return web.FileResponse(STATIC / filename)


def create_app(config=None, probe=None):
    config = config or Config.from_env()
    app = web.Application(middlewares=[errors], client_max_size=2 * 1024 * 1024)
    service = Service(config, probe)
    app[SERVICE] = service

    async def lifecycle(app):
        try:
            await service.start()
            yield
        finally:
            await service.close()

    app.cleanup_ctx.append(lifecycle)
    app.add_routes([
        web.get("/api/zones", catalog), web.patch("/api/zones/{zone:\\d+}", update_zone),
        web.get("/api/zones/{zone:\\d+}/records", records), web.post("/api/zones/{zone:\\d+}/records", records),
        web.patch("/api/zones/{zone:\\d+}/records/{record:\\d+}", update_record),
        web.post("/api/bulk-update", bulk), web.get("/api/status", status),
        web.post("/api/alert-test", alert_test),
        web.post("/api/refresh", refresh), web.get("/healthz", health),
        web.get("/", static), web.get("/static/{file}", static),
    ])
    return app


def main():
    parser = argparse.ArgumentParser(description="PulseZone DNS 존 HTTP 모니터")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    try:
        config = Config.from_env(args.env_file)
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.WARNING)
    web.run_app(create_app(config), host=config.web_host, port=config.web_port, access_log=None)
