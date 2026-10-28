import ipaddress
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit

from dotenv import load_dotenv


TIMINGS = ("interval", "timeout", "retry", "retry_delay")
COMMON = set(TIMINGS) | {"enabled", "description", "tip", "protocol", "https_insecure"}


def positive(value: Any, name: str, integer: bool = False) -> Any:
    if isinstance(value, bool):
        raise ValueError(name + ": 양수를 입력하세요")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(name + ": 숫자를 입력하세요")
    if not math.isfinite(number) or number <= 0 or (integer and not number.is_integer()):
        raise ValueError(name + ": 양의 " + ("정수" if integer else "숫자") + "를 입력하세요")
    return int(number) if integer else number


def hostname(value: str, record: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("도메인/레코드는 문자열이어야 합니다")
    value = value.strip().rstrip(".")
    if record and value in ("", "@"):
        return value
    try:
        value = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("유효하지 않은 도메인입니다")
    if len(value) > 253 or not value:
        raise ValueError("유효하지 않은 도메인입니다")
    labels = value.split(".")
    for i, label in enumerate(labels):
        if record and i == 0 and label == "*":
            continue
        if not re.fullmatch(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?", label):
            raise ValueError("유효하지 않은 도메인/레코드입니다: " + value)
    return value


def validate_patch(data: Any, is_zone: bool = False) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("JSON 객체가 필요합니다")
    allowed = COMMON | ({"display_name", "domain"} if is_zone else {"record", "ip", "alias", "maintenance"})
    if set(data) - allowed:
        raise ValueError("수정할 수 없는 필드: " + ", ".join(sorted(set(data) - allowed)))
    clean = dict(data)
    for name, value in data.items():
        if name in TIMINGS:
            clean[name] = None if value is None else positive(value, name, name == "retry")
        elif name == "enabled":
            if value is None and not is_zone:
                continue
            if type(value) is not int or value not in (0, 1):
                raise ValueError("enabled는 0 또는 1이어야 합니다 (레코드는 null 허용)")
        elif name == "protocol":
            if value not in (None, "auto", "http", "https"):
                raise ValueError("protocol은 auto/http/https/null 중 하나입니다")
        elif name == "https_insecure":
            if value is not None and type(value) is not bool:
                raise ValueError("https_insecure는 true/false/null 중 하나입니다")
        elif name == "maintenance":
            if type(value) is not bool:
                raise ValueError("maintenance는 true/false 중 하나입니다")
        elif name in ("domain", "record"):
            clean[name] = hostname(value, name == "record")
        elif name == "ip":
            try:
                clean[name] = str(ipaddress.IPv4Address(value))
            except (ValueError, TypeError):
                raise ValueError("유효한 IPv4 주소를 입력하세요")
        else:
            if value is None:
                value = ""
            if not isinstance(value, str) or len(value) > 4000:
                raise ValueError(name + ": 4000자 이하 문자열을 입력하세요")
            clean[name] = value
    return clean


@dataclass
class Config:
    zone_path: Path = Path("zones")
    database_path: Path = Path("data/pulsezone.sqlite3")
    zone_refresh_time: float = 3600
    interval: float = 10
    timeout: float = 10
    retry: int = 10
    retry_delay: float = 10
    protocol: str = "auto"
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    max_concurrency: int = 100
    history_size: int = 60
    alerts: Dict[str, str] = field(default_factory=dict)
    gotify_token: str = ""
    ntfy_token: str = ""
    https_insecure: bool = False

    @classmethod
    def from_env(cls, env_file: str = ".env") -> "Config":
        load_dotenv(env_file, override=False)
        env = os.environ
        cfg = cls()
        cfg.zone_path = Path(env.get("ZONE_PATH", "./zones")).expanduser().resolve()
        cfg.database_path = Path(env.get("DATABASE_PATH", "./data/pulsezone.sqlite3")).expanduser().resolve()
        for attr, key in (("zone_refresh_time", "ZONE_REFRESH_TIME"), ("interval", "CHECK_INTERVAL"),
                          ("timeout", "TIMEOUT"), ("retry", "RETRY"), ("retry_delay", "RETRY_DELAY"),
                          ("web_port", "WEB_PORT"), ("max_concurrency", "MAX_CONCURRENCY"),
                          ("history_size", "HISTORY_SIZE")):
            integer = attr in ("retry", "web_port", "max_concurrency", "history_size")
            setattr(cfg, attr, positive(env.get(key, getattr(cfg, attr)), key, integer))
        if cfg.web_port > 65535:
            raise ValueError("WEB_PORT는 65535 이하여야 합니다")
        cfg.web_host = env.get("WEB_HOST", cfg.web_host)
        cfg.protocol = env.get("CHECK_PROTOCOL", "auto")
        validate_patch({"protocol": cfg.protocol})
        insecure = env.get("HTTPS_INSECURE", "false").strip().lower()
        if insecure not in ("true", "false"):
            raise ValueError("HTTPS_INSECURE는 true 또는 false여야 합니다")
        cfg.https_insecure = insecure == "true"
        for channel in ("slack", "discord", "ntfy", "gotify", "custom"):
            url = env.get("ALERT_" + channel.upper(), "").strip()
            if url and (urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname):
                raise ValueError("ALERT_" + channel.upper() + "에는 완전한 HTTP(S) URL이 필요합니다")
            if url:
                cfg.alerts[channel] = url
        cfg.gotify_token = env.get("ALERT_GOTIFY_TOKEN", "")
        cfg.ntfy_token = env.get("ALERT_NTFY_TOKEN", "")
        return cfg

    def defaults(self) -> Dict[str, Any]:
        return {key: getattr(self, key) for key in TIMINGS + ("protocol", "https_insecure")}


def effective(cfg: Config, zone: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    result = cfg.defaults()
    for layer in (zone, row):
        for key in result:
            if layer.get(key) is not None:
                result[key] = layer[key]
    result["enabled"] = bool(zone["enabled"] and row.get("enabled") != 0)
    result["present"] = bool(row["source"] == "manual" or (zone["present"] and row["present"]))
    name = row["record"]
    if not name:
        target = row["ip"]
    elif name == "@":
        target = zone["domain"]
    else:
        target = name.replace("*", "pulsezone-probe") + "." + zone["domain"]
    result["host"] = target
    return result
