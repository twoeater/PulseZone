import asyncio
import socket
import ssl
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp


@dataclass
class Result:
    ok: bool
    protocol: str
    status: Optional[int]
    elapsed_ms: float
    error: str
    timestamp: float = field(default_factory=time.time)
    attempts: List[Dict[str, Any]] = field(default_factory=list)

    def json(self):
        return asdict(self)


class Probe:
    def __init__(self, ssl_context: Optional[ssl.SSLContext] = None, ports=None):
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.ports = ports or {"http": 80, "https": 443}

    async def check(self, host: str, ip: str, protocol: str, timeout: float,
                    https_insecure: bool = False) -> Result:
        protocols = ("https", "http") if protocol == "auto" else (protocol,)
        attempts = []
        start = time.monotonic()
        # Connecting to the literal row IP also pins numeric HTTP hostnames.
        # Host and server_hostname preserve virtual hosting and TLS verification.
        connector = aiohttp.TCPConnector(force_close=True,
                                         ssl=False if https_insecure else self.ssl_context,
                                         family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector, cookie_jar=aiohttp.DummyCookieJar(),
                                         trust_env=False, auto_decompress=False) as session:
            for scheme in protocols:
                status = None
                error = ""
                began = time.monotonic()
                try:
                    port = self.ports[scheme]
                    wire_host = host.encode("idna").decode("ascii")
                    header = wire_host if port == {"http": 80, "https": 443}[scheme] else "{}:{}".format(wire_host, port)
                    url = "{}://{}:{}/".format(scheme, ip, port)
                    async with session.get(url, headers={"Host": header},
                                           server_hostname=wire_host if scheme == "https" else None,
                                           allow_redirects=False,
                                           timeout=aiohttp.ClientTimeout(total=timeout, ceil_threshold=float("inf"))) as response:
                        status = response.status
                        if not 200 <= status <= 399:
                            error = "HTTP " + str(status)
                        response.close()
                except asyncio.TimeoutError:
                    error = "타임아웃 ({}초)".format(timeout)
                except aiohttp.ClientConnectorCertificateError:
                    error = "TLS 인증서 검증 실패"
                except (aiohttp.ClientError, OSError, ValueError) as exc:
                    error = "{}: {}".format(type(exc).__name__, str(exc)[:240])
                attempts.append({"protocol": scheme, "status": status, "error": error,
                                 "elapsed_ms": round((time.monotonic() - began) * 1000, 2)})
                if not error:
                    return Result(True, scheme, status, round((time.monotonic() - start) * 1000, 2), "", attempts=attempts)
        return Result(False, protocols[-1], attempts[-1]["status"],
                      round((time.monotonic() - start) * 1000, 2),
                      "; ".join(a["protocol"] + ": " + a["error"] for a in attempts), attempts=attempts)
