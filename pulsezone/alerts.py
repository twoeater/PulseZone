import asyncio
import time
from typing import Dict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from .config import Config


class Alerts:
    def __init__(self, config: Config):
        self.config = config
        self.queue = asyncio.Queue(maxsize=1000)
        self.delivery: Dict[str, dict] = {}
        self.worker = None
        self.session = None
        self.dropped = 0

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=False)
        self.worker = asyncio.create_task(self._run())

    def submit(self, message: str) -> bool:
        if not self.config.alerts:
            return False
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        return True

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.session:
            await self.session.close()

    async def _run(self):
        while True:
            message = await self.queue.get()
            try:
                await asyncio.gather(*(self._send(channel, url, message)
                                       for channel, url in self.config.alerts.items()))
            finally:
                self.queue.task_done()

    async def _send(self, channel: str, url: str, message: str):
        kwargs = {"allow_redirects": False}
        if channel == "slack":
            kwargs["json"] = {"text": message}
        elif channel == "discord":
            kwargs["json"] = {"content": message[:2000], "allowed_mentions": {"parse": []}}
        elif channel == "ntfy":
            kwargs["data"] = message.encode("utf-8")
            kwargs["headers"] = {"Content-Type": "text/plain; charset=utf-8"}
            if self.config.ntfy_token:
                if ":" in self.config.ntfy_token:
                    username, password = self.config.ntfy_token.split(":", 1)
                else:
                    username, password = "", self.config.ntfy_token
                kwargs["auth"] = aiohttp.BasicAuth(username, password, encoding="utf-8")
        elif channel == "gotify":
            parts = urlsplit(url)
            query = parse_qsl(parts.query, keep_blank_values=True)
            token = self.config.gotify_token or dict(query).get("token", "")
            url = urlunsplit(parts._replace(query=urlencode([(k, v) for k, v in query if k != "token"])))
            kwargs["headers"] = {"X-Gotify-Key": token} if token else {}
            kwargs["json"] = {"title": "PulseZone", "message": message, "priority": 5}
        else:
            kwargs["json"] = dict.fromkeys(("text", "content", "body", "alert"), message)
        # Channel failures remain in memory and are visible in the dashboard.
        # A finite queue and request deadline bound resource consumption.
        for attempt in range(3):
            try:
                async with self.session.post(url, **kwargs) as response:
                    status = response.status
                    response.close()
                    if 200 <= status < 300:
                        self.delivery[channel] = {"ok": True, "timestamp": time.time(), "error": ""}
                        return
                    error = "HTTP " + str(status)
                    retry = status == 429 or status >= 500
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                error = type(exc).__name__  # Do not disclose URLs/tokens.
                retry = True
            self.delivery[channel] = {"ok": False, "timestamp": time.time(), "error": error}
            if not retry or attempt == 2:
                return
            await asyncio.sleep(2 ** attempt)


def message(zone, row, state, settings, recovered=False):
    domain = zone["display_name"] or zone["filename"]
    alias = row["alias"] or row["record"]
    if row["record"] == "@":
        title = (row["alias"] + "." if row["alias"] else "") + domain
    elif row["record"] == "":
        title = row["alias"] or row["ip"]
    else:
        title = alias + "." + domain
    title += "(" + row["ip"] + ")"
    if recovered:
        detail = "응답 복구 (실제 down {:.1f}초)".format(state["duration"])
    else:
        failures = state["failures"]
        delay = settings["retry_delay"]
        detail = "응답없음(연속 실패 {}회 × retry_delay {:g}초 = 기준 {:g}초 / 실제 down {:.1f}초)".format(
            failures, delay, failures * delay, state["duration"])
    text = title + "\n" + detail + "\n" + zone["description"] + " / " + row["description"]
    if not recovered:
        text += "\n원인: " + state["latest"]["error"]
    return text
