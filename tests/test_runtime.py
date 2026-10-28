import asyncio
import base64
import datetime
import ssl
from contextlib import asynccontextmanager

import pytest
from aiohttp import ClientSession, web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from pulsezone.alerts import Alerts, message
from pulsezone.app import SERVICE, create_app
from pulsezone.config import Config
from pulsezone.monitor import Monitor, State
from pulsezone.probe import Probe, Result


@asynccontextmanager
async def server(app, context=None):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        await runner.cleanup()


def test_http_pin_status_timeout_and_fallback():
    async def run():
        calls = []
        response_status = [200]

        async def handler(request):
            calls.append(request.host)
            if response_status[0] == -1:
                await asyncio.sleep(.1)
            return web.Response(status=max(response_status[0], 200), headers={"Location": "http://never-follow.invalid"})

        app = web.Application()
        app.router.add_get("/", handler)
        async with server(app) as port:
            probe = Probe(ports={"http": port, "https": port})
            for status, ok in ((200, True), (302, True), (399, True), (400, False), (503, False)):
                response_status[0] = status
                result = await probe.check("nonexistent.invalid", "127.0.0.1", "http", 1)
                assert result.ok == ok and result.status == status
                assert calls[-1] == "nonexistent.invalid:" + str(port)
            response_status[0] = 200
            result = await probe.check("nonexistent.invalid", "127.0.0.1", "auto", 1)
            assert result.ok and result.protocol == "http" and len(result.attempts) == 2
            result = await probe.check("nonexistent.invalid", "127.0.0.2", "http", .05)
            assert not result.ok  # No sharing of the previous IP's connection.
            result = await probe.check("127.0.0.1", "127.0.0.1", "http", 1)
            assert result.ok and calls[-1] == "127.0.0.1:" + str(port)
            result = await probe.check("192.0.2.55", "127.0.0.1", "http", 1)
            assert result.ok and calls[-1] == "192.0.2.55:" + str(port)
            response_status[0] = -1
            result = await probe.check("nonexistent.invalid", "127.0.0.1", "http", .02)
            assert not result.ok and "타임아웃" in result.error
    asyncio.run(run())


def test_https_sni_and_certificate_validation(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "monitor.example.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("monitor.example.test")]), False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert_path), str(key_path))
    sni = []
    tls.set_servername_callback(lambda socket, host, context: sni.append(host))
    trusted = ssl.create_default_context(cafile=str(cert_path))

    async def run():
        async def handler(request):
            assert request.host.startswith("monitor.example.test")
            return web.Response(text="ok")
        app = web.Application()
        app.router.add_get("/", handler)
        async with server(app, tls) as port:
            probe = Probe(trusted, {"https": port})
            assert (await probe.check("monitor.example.test", "127.0.0.1", "https", 1)).ok
            assert sni == ["monitor.example.test"]
            wrong = await probe.check("wrong.example.test", "127.0.0.1", "https", 1)
            assert not wrong.ok and "인증서" in wrong.error
    asyncio.run(run())


def test_state_counts_repeat_recovery_and_history():
    state = State(4)
    fail = Result(False, "http", 503, 10, "HTTP 503")
    events = [state.apply(fail, i * 10, i * 10 + 2, 3) for i in range(7)]
    assert events == [None, None, "down", None, None, "down", None]
    assert state.duration == 62 and state.failures == 7 and len(state.history) == 4
    assert state.apply(Result(True, "http", 200, 1, ""), 70, 71, 3) == "recovery"
    assert state.duration == 71 and state.status == "UP" and state.failures == 0
    assert state.apply(Result(True, "http", 200, 1, ""), 80, 81, 3) is None
    state.apply(fail, 90, 91, 1)
    state.stop("OFF")
    assert state.apply(Result(True, "http", 200, 1, ""), 100, 101, 1) is None


@pytest.mark.parametrize("failure", ["untrusted", "hostname", "expired"])
def test_https_insecure_is_per_request_and_keeps_tls_host_and_status_checks(tmp_path, failure):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "monitor.example.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=2))
            .not_valid_after(now + datetime.timedelta(days=-1 if failure == "expired" else 1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("monitor.example.test")]), False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert_path), str(key_path))
    sni = []
    tls.set_servername_callback(lambda socket, host, context: sni.append(host))
    trusted = None if failure == "untrusted" else ssl.create_default_context(cafile=str(cert_path))
    host = "wrong.example.test" if failure == "hostname" else "monitor.example.test"

    async def run():
        calls, response_status = [], [200]

        async def handler(request):
            calls.append((request.secure, request.host))
            if response_status[0] == -1:
                await asyncio.sleep(.1)
            return web.Response(status=max(response_status[0], 200))

        http_app, https_app = web.Application(), web.Application()
        http_app.router.add_get("/", handler)
        https_app.router.add_get("/", handler)
        async with server(http_app) as http_port, server(https_app, tls) as https_port:
            probe = Probe(trusted, {"https": https_port, "http": http_port})
            strict = await probe.check(host, "127.0.0.1", "https", 1)
            assert not strict.ok and "인증서" in strict.error
            fallback = await probe.check(host, "127.0.0.1", "auto", 1)
            assert fallback.ok and fallback.protocol == "http" and len(fallback.attempts) == 2
            insecure = await probe.check(host, "127.0.0.1", "auto", 1, True)
            assert insecure.ok and insecure.protocol == "https" and len(insecure.attempts) == 1
            assert calls[-1] == (True, host + ":" + str(https_port))
            assert sni and all(value == host for value in sni)
            strict, insecure = await asyncio.gather(
                probe.check(host, "127.0.0.1", "https", 1, False),
                probe.check(host, "127.0.0.1", "https", 1, True))
            assert not strict.ok and insecure.ok
            assert (await probe.check(host, "127.0.0.1", "http", 1, True)).ok
            assert calls[-1] == (False, host + ":" + str(http_port))
            wrong_ip = await probe.check(host, "127.0.0.2", "https", .05, True)
            assert not wrong_ip.ok
            response_status[0] = 503
            status_failure = await probe.check(host, "127.0.0.1", "https", 1, True)
            assert not status_failure.ok and status_failure.status == 503
            response_status[0] = -1
            timeout = await probe.check(host, "127.0.0.1", "https", .02, True)
            assert not timeout.ok and "타임아웃" in timeout.error
    asyncio.run(run())


def test_alert_labels_and_actual_downtime():
    zone = {"display_name": "서비스", "filename": "db.example.test", "description": "도메인 설명"}
    row = {"record": "@", "alias": None, "ip": "127.0.0.1", "description": "레코드 설명"}
    state = {"failures": 10, "duration": 290.1, "latest": {"error": "타임아웃"}}
    text = message(zone, row, state, {"retry_delay": 10})
    assert text.startswith("서비스(127.0.0.1)") and "기준 100초 / 실제 down 290.1초" in text
    row.update(record="", alias="직접 점검")
    assert message(zone, row, state, {"retry_delay": 10}, True).startswith("직접 점검(127.0.0.1)\n응답 복구")
    row.update(record="www", alias="웹")
    assert message(zone, row, state, {"retry_delay": 10}).startswith("웹.서비스(127.0.0.1)")


def test_all_alert_payloads_and_channel_failure():
    async def run():
        seen = {}
        async def receive(request):
            channel = request.match_info["channel"]
            seen[channel] = (await request.text(), dict(request.headers), request.query_string)
            return web.Response(status=400 if channel == "slack" else 204)
        app = web.Application()
        app.router.add_post("/{channel}", receive)
        async with server(app) as port:
            cfg = Config(alerts={key: "http://127.0.0.1:{}/{}".format(port, key) for key in ("slack", "discord", "ntfy", "gotify", "custom")}, ntfy_token="ntfy-secret")
            cfg.alerts["gotify"] += "?token=gotify-secret"
            alerts = Alerts(cfg)
            await alerts.start()
            try:
                alerts.submit("서버 장애\n테스트")
                await asyncio.wait_for(alerts.queue.join(), 3)
                import json
                assert json.loads(seen["slack"][0])["text"] == "서버 장애\n테스트"
                assert json.loads(seen["discord"][0])["content"] == "서버 장애\n테스트"
                assert seen["ntfy"][0] == "서버 장애\n테스트"
                assert seen["ntfy"][1]["Authorization"] == "Basic " + base64.b64encode(b":ntfy-secret").decode("ascii")
                assert seen["gotify"][1]["X-Gotify-Key"] == "gotify-secret" and not seen["gotify"][2]
                assert json.loads(seen["gotify"][0])["message"] == "서버 장애\n테스트"
                assert json.loads(seen["custom"][0]) == dict.fromkeys(("text", "content", "body", "alert"), "서버 장애\n테스트")
                assert not alerts.delivery["slack"]["ok"] and alerts.delivery["custom"]["ok"]
            finally:
                await alerts.close()
    asyncio.run(run())


@pytest.mark.parametrize("token,expected", [
    ("", None),
    (":", ":"),
    ("monitor:password", "monitor:password"),
    ("monitor:pass:with:colons", "monitor:pass:with:colons"),
    ("monitor:", "monitor:"),
    (":tk_test", ":tk_test"),
    ("tk_test", ":tk_test"),
    ("사용자: 비밀:번호 ", "사용자: 비밀:번호 "),
])
def test_ntfy_auth_modes(token, expected):
    async def run():
        seen = []
        expected_auth = (None if expected is None else
                         "Basic " + base64.b64encode(expected.encode("utf-8")).decode("ascii"))
        body = "서버 장애\nUTF-8 메시지"

        async def receive(request):
            seen.append((request.headers.get("Authorization"), await request.text(),
                         request.content_type, request.charset, request.query_string))
            return web.Response(status=204 if seen[-1][0] == expected_auth else 401)

        app = web.Application()
        app.router.add_post("/topic", receive)
        async with server(app) as port:
            cfg = Config(alerts={"ntfy": "http://127.0.0.1:{}/topic".format(port)}, ntfy_token=token)
            alerts = Alerts(cfg)
            await alerts.start()
            try:
                alerts.submit(body)
                await asyncio.wait_for(alerts.queue.join(), 3)
                assert seen == [(expected_auth, body, "text/plain", "utf-8", "")]
                assert alerts.delivery["ntfy"]["ok"]
            finally:
                await alerts.close()
    asyncio.run(run())


def test_api_settings_manual_reimport_and_readonly_metrics(tmp_path):
    async def run():
        root = tmp_path / "zones"
        root.mkdir()
        (root / "db.example.test").write_text("@ IN A 127.0.0.1\n")
        class FakeProbe:
            async def check(self, *args):
                return Result(True, "http", 200, 1, "")
        app = create_app(Config(zone_path=root, database_path=tmp_path / "db.sqlite3", interval=.01), FakeProbe())
        async with server(app) as port, ClientSession() as session:
            base = "http://127.0.0.1:" + str(port)
            async with session.get(base + "/api/zones") as response:
                zone = (await response.json())["zones"][0]
            z, r = zone["id"], zone["records"][0]["id"]
            changes = app[SERVICE].store.db.total_changes
            for _ in range(100):
                if len(app[SERVICE].monitor.states[f"{z}:{r}"].history) >= 2:
                    break
                await asyncio.sleep(.02)
            async with session.get(base + "/api/status") as response:
                status = await response.json()
            assert len(status["records"][f"{z}:{r}"]["history"]) >= 2
            assert app[SERVICE].store.db.total_changes == changes
            async with session.patch(base + f"/api/zones/{z}", json={"enabled": 0}) as response:
                assert response.status == 200
            assert app[SERVICE].monitor.snapshot()[f"{z}:{r}"]["status"] == "OFF"
            async with session.patch(base + f"/api/zones/{z}/records/{r}", json={"ip": "127.0.0.2"}) as response:
                assert response.status == 200
            async with session.post(base + "/api/refresh", json={}) as response:
                assert response.status == 200
            assert len(app[SERVICE].store.records(z)) == 2
            async with session.post(base + f"/api/zones/{z}/records", json={"record": "", "ip": "127.0.0.1"}) as response:
                assert response.status == 201
            async with session.post(base + f"/api/zones/{z}/records", json={"record": "", "ip": "127.0.0.1"}) as response:
                assert response.status == 409
            async with session.post(base + "/api/bulk-update", json={"operations": [{"zone_id": z, "patch": {"enabled": 1}}, {"zone_id": z, "record_id": 999, "patch": {"enabled": 0}}]}) as response:
                assert response.status == 404
            assert app[SERVICE].store.zone(z)["enabled"] == 0
            async with session.patch(base + f"/api/zones/{z}", data="enabled=1") as response:
                assert response.status == 400
            (root / "db.example.test").write_text("broken zone data\n")
            await app[SERVICE].refresh()
            assert app[SERVICE].scan_errors and app[SERVICE].store.zone(z)["present"]
    asyncio.run(run())


def test_https_insecure_api_changes_inheritance_and_invalidates_inflight_result(tmp_path):
    async def run():
        root = tmp_path / "zones"
        root.mkdir()
        (root / "db.example.test").write_text("@ IN A 127.0.0.1\n", encoding="utf-8")

        class PendingProbe:
            def __init__(self):
                self.calls = []
                self.started = asyncio.Event()
                self.cancelled = False

            async def check(self, host, ip, protocol, timeout, https_insecure=False):
                self.calls.append(https_insecure)
                if len(self.calls) == 1:
                    self.started.set()
                    try:
                        await asyncio.sleep(60)
                    except asyncio.CancelledError:
                        self.cancelled = True
                        return Result(False, "https", None, 1, "late certificate error")
                return Result(True, "https", 200, 1, "")

        probe = PendingProbe()
        app = create_app(Config(zone_path=root, database_path=tmp_path / "db.sqlite3", interval=60), probe)
        async with server(app) as port, ClientSession() as session:
            base = "http://127.0.0.1:" + str(port)
            await asyncio.wait_for(probe.started.wait(), 2)
            async with session.get(base + "/api/zones") as response:
                catalog = await response.json()
            assert catalog["defaults"]["https_insecure"] is False
            zone = catalog["zones"][0]
            z, r = zone["id"], zone["records"][0]["id"]
            async with session.patch(base + f"/api/zones/{z}", json={"https_insecure": True}) as response:
                assert response.status == 200
            for _ in range(100):
                if app[SERVICE].monitor.snapshot()[f"{z}:{r}"]["status"] == "UP":
                    break
                await asyncio.sleep(.01)
            state = app[SERVICE].monitor.snapshot()[f"{z}:{r}"]
            assert probe.cancelled and probe.calls == [False, True]
            assert state["status"] == "UP" and len(state["history"]) == 1 and state["failures"] == 0
            async with session.patch(base + f"/api/zones/{z}/records/{r}", json={"https_insecure": False}) as response:
                assert response.status == 200
            async with session.get(base + "/api/zones") as response:
                zone = (await response.json())["zones"][0]
            assert zone["https_insecure"] is True and zone["effective"]["https_insecure"] is True
            assert zone["records"][0]["https_insecure"] is False
            assert zone["records"][0]["effective"]["https_insecure"] is False
            async with session.post(base + "/api/bulk-update", json={"operations": [
                {"zone_id": z, "record_id": r, "patch": {"https_insecure": None}}
            ]}) as response:
                assert response.status == 200
            async with session.get(base + "/api/zones") as response:
                row = (await response.json())["zones"][0]["records"][0]
            assert row["https_insecure"] is None and row["effective"]["https_insecure"] is True
            async with session.patch(base + f"/api/zones/{z}", json={"https_insecure": "false"}) as response:
                assert response.status == 400
            async with session.post(base + f"/api/zones/{z}/records", json={"ip": "127.0.0.2", "https_insecure": False}) as response:
                assert response.status == 201
    asyncio.run(run())


def test_stop_invalidates_inflight_result_and_concurrency():
    async def run():
        class SlowProbe:
            active = 0
            maximum = 0
            entered = asyncio.Event()
            async def check(self, *args):
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                self.entered.set()
                try:
                    await asyncio.sleep(20)
                except asyncio.CancelledError:
                    return Result(False, "http", None, 1, "late result")
                finally:
                    self.active -= 1
        class Sink:
            messages = []
            def submit(self, msg): self.messages.append(msg)
        probe, sink = SlowProbe(), Sink()
        monitor = Monitor(Config(max_concurrency=1), sink, probe)
        zone = {"id": 1, "enabled": 1, "present": 1, "domain": "example.test", "records": [
            {"id": i, "record": "@", "ip": "127.0.0." + str(i), "source": "file", "present": 1} for i in (1, 2)]}
        monitor.reconcile([zone])
        await probe.entered.wait()
        zone = dict(zone, enabled=0)
        monitor.reconcile([zone])
        await asyncio.sleep(.02)
        assert probe.maximum == 1 and not sink.messages
        assert all(s["status"] == "OFF" and not s["history"] for s in monitor.snapshot().values())
        await monitor.close()
    asyncio.run(run())
