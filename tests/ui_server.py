"""Local browser QA harness; requests only loopback addresses. Not production."""
from pathlib import Path

from aiohttp import web

from pulsezone.app import SERVICE, create_app
from pulsezone.config import Config
from pulsezone.probe import Probe


root = Path("output/playwright/qa-zones").resolve()
root.mkdir(parents=True, exist_ok=True)
(root / "db.example.test").write_text("@ IN A 127.0.0.1\nwww IN A 127.0.0.1\n    IN A 127.0.0.2\n", encoding="utf8")
app = create_app(Config(zone_path=root, database_path=root.parent / "qa.sqlite3", interval=2,
                        timeout=.2, retry=2, retry_delay=2, protocol="http"),
                 Probe(ports={"http": 18766, "https": 18766}))


async def local_server(app):
    async def healthy(request):
        return web.Response(text="healthy")
    local = web.Application()
    local.router.add_get("/", healthy)
    runner = web.AppRunner(local, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 18766).start()
    service = app[SERVICE]
    zone = service.store.zones()[0]
    service.store.update_zone(zone["id"], {"display_name": "Example 서비스", "description": "로컬 HTTP 서버 · 브라우저 검증용", "tip": "도메인 설정을 편집합니다"})
    service.changed()
    yield
    await runner.cleanup()


app.cleanup_ctx.append(local_server)
web.run_app(app, host="127.0.0.1", port=18765, access_log=None)
