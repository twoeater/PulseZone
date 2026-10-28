import asyncio
import time
from collections import deque
from typing import Any, Dict

from .alerts import message
from .config import HTTP_STATUS_SETTINGS, effective
from .probe import Probe, Result


class State:
    def __init__(self, history_size):
        self.history = deque(maxlen=history_size)
        self.latest = None
        self.status = "UNKNOWN"
        self.failures = 0
        self.first_failure = None
        self.duration = 0.0
        self.down = False
        self.next_check = None

    def stop(self, status):
        self.status = status
        self.failures = 0
        self.first_failure = None
        self.duration = 0.0
        self.down = False
        self.next_check = None

    def apply(self, result: Result, started: float, finished: float, retry: int):
        self.latest = result.json()
        self.history.append(self.latest)
        event = None
        if result.ok:
            if self.first_failure is not None:
                self.duration = finished - self.first_failure
            if self.down:
                event = "recovery"
            self.status = "UP"
            self.failures = 0
            self.first_failure = None
            self.down = False
        else:
            self.failures += 1
            if self.first_failure is None:
                self.first_failure = started
            self.duration = finished - self.first_failure
            if self.failures >= retry:
                if not self.down or self.failures % retry == 0:
                    event = "down"
                self.down = True
            self.status = "DOWN" if self.down else "RETRYING"
        return event

    def json(self):
        return {"status": self.status, "failures": self.failures, "duration": self.duration,
                "latest": self.latest, "history": list(self.history), "next_check": self.next_check}


class Monitor:
    def __init__(self, config, alerts, probe=None, on_alert=None):
        self.config = config
        self.alerts = alerts
        self.probe = probe or Probe()
        self.on_alert = on_alert
        self.semaphore = asyncio.Semaphore(config.max_concurrency)
        self.targets: Dict[str, Dict[str, Any]] = {}
        self.states: Dict[str, State] = {}
        self.tasks = {}
        self.retired = set()

    @staticmethod
    def key(zone_id, row_id):
        return str(zone_id) + ":" + str(row_id)

    def reconcile(self, zones):
        incoming = {}
        for zone in zones:
            for row in zone["records"]:
                key = self.key(zone["id"], row["id"])
                settings = effective(self.config, zone, row)
                identity = (settings["host"], row["ip"], settings["protocol"], settings["https_insecure"]) + tuple(
                    settings[name] for name in HTTP_STATUS_SETTINGS)
                signature = identity + tuple(settings[k] for k in ("interval", "timeout", "retry", "retry_delay", "enabled", "present"))
                old = self.targets.get(key)
                target = {"zone": zone, "row": row, "settings": settings,
                          "identity": identity, "signature": signature, "generation": object()}
                if old and old["signature"] == signature:
                    old.update(zone=zone, row=row)
                    old["settings"]["maintenance"] = settings["maintenance"]
                    incoming[key] = old
                    continue
                task = self.tasks.pop(key, None)
                if task:
                    task.cancel()
                    self.retired.add(task)
                    task.add_done_callback(self.retired.discard)
                state = self.states.get(key)
                if state is None or (old and old["identity"] != identity):
                    state = State(self.config.history_size)
                    self.states[key] = state
                incoming[key] = target
                if not settings["present"]:
                    state.stop("MISSING")
                elif not settings["enabled"]:
                    state.stop("OFF")
                else:
                    if state.status in ("OFF", "MISSING"):
                        state.stop("UNKNOWN")
                    self.tasks[key] = asyncio.create_task(self._run(key, target))
        for key in set(self.targets) - set(incoming):
            task = self.tasks.pop(key, None)
            if task:
                task.cancel()
                self.retired.add(task)
                task.add_done_callback(self.retired.discard)
            self.states.pop(key, None)
        self.targets = incoming

    async def _run(self, key, target):
        settings = target["settings"]
        state = self.states[key]
        allowed_classes = tuple(group for group, name in enumerate(HTTP_STATUS_SETTINGS, 2) if settings[name])
        while True:
            async with self.semaphore:
                if self.targets.get(key) is not target:
                    return
                state.next_check = None
                started = time.monotonic()
                try:
                    result = await self.probe.check(settings["host"], target["row"]["ip"],
                                                    settings["protocol"], settings["timeout"],
                                                    settings["https_insecure"], allowed_classes)
                except Exception as exc:
                    result = Result(False, settings["protocol"], None,
                                    (time.monotonic() - started) * 1000, "점검 오류: " + type(exc).__name__)
                if self.targets.get(key) is not target:
                    return
                event = state.apply(result, started, time.monotonic(), settings["retry"])
                if event:
                    text = message(target["zone"], target["row"], state.json(), settings,
                                   recovered=event == "recovery")
                    if self.on_alert:
                        self.on_alert(target, text)
                    else:
                        self.alerts.submit(text)
            delay = settings["interval"] if result.ok else settings["retry_delay"]
            state.next_check = time.time() + delay
            await asyncio.sleep(delay)

    def snapshot(self):
        result = {}
        for key, state in self.states.items():
            data = state.json()
            if state.first_failure is not None:
                data["duration"] = time.monotonic() - state.first_failure
            target = self.targets[key]
            row = target["row"]
            data.update(maintenance=effective(self.config, target["zone"], row)["maintenance"],
                        alerted_at=row.get("alerted_at"))
            result[key] = data
        return result

    async def close(self):
        tasks = list(self.tasks.values()) + list(self.retired)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
