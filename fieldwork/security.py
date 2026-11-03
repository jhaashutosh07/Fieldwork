"""Security controls for the public demo: client identity behind a proxy, rate limiting, concurrency limits,
request-size limits, security headers and an admin check for write endpoints. See SECURITY.md."""
from __future__ import annotations

import hmac
import os
import threading
import time

from fastapi import HTTPException, Request

# Behind Render's load balancer every connection comes from the proxy, so the real client is the address the proxy
# appended to X-Forwarded-For. Only the right-most entry is trustworthy: anything to its left is client-supplied.
TRUST_PROXY = os.environ.get("FIELDWORK_TRUST_PROXY") == "1"
ADMIN_TOKEN = os.environ.get("FIELDWORK_ADMIN_TOKEN", "")
MAX_BODY_BYTES = 128 * 1024

CSP = ("default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")
# FastAPI's interactive docs load Swagger UI from jsDelivr and use an inline bootstrap script.
DOCS_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Token bucket per (bucket, client). Memory is bounded: idle clients are pruned."""

    def __init__(self, max_clients: int = 20_000):
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}
        self._max = max_clients

    def check(self, request: Request, bucket: str, per_minute: float, burst: int) -> None:
        key = f"{bucket}:{client_ip(request)}"
        now = time.monotonic()
        rate = per_minute / 60.0
        with self._lock:
            tokens, last = self._buckets.get(key, (float(burst), now))
            tokens = min(float(burst), tokens + (now - last) * rate)
            if tokens < 1.0:
                retry = int((1.0 - tokens) / rate) + 1
                raise HTTPException(429, f"too many requests; try again in {retry}s", headers={"Retry-After": str(retry)})
            self._buckets[key] = (tokens - 1.0, now)
            if len(self._buckets) > self._max:
                self._prune(now)

    def _prune(self, now: float) -> None:
        stale = [k for k, (_, last) in self._buckets.items() if now - last > 600]
        for k in stale:
            del self._buckets[k]
        if len(self._buckets) > self._max:            # still full: drop the oldest half
            for k, _ in sorted(self._buckets.items(), key=lambda kv: kv[1][1])[: len(self._buckets) // 2]:
                del self._buckets[k]

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


limiter = RateLimiter()
# At most this many agent runs (try, stream, batch, eval run) execute at once; extra requests get 503, not a queue.
agent_slots = threading.BoundedSemaphore(int(os.environ.get("FIELDWORK_MAX_CONCURRENT", "4")))


class AgentSlot:
    def __enter__(self):
        if not agent_slots.acquire(timeout=2):
            raise HTTPException(503, "the demo is busy; try again in a few seconds", headers={"Retry-After": "5"})
        return self

    def __exit__(self, *exc):
        agent_slots.release()
        return False


def require_admin(request: Request) -> None:
    """Write endpoints on the public demo need the operator's token (constant-time comparison)."""
    supplied = request.headers.get("x-admin-token", "")
    if not ADMIN_TOKEN or not hmac.compare_digest(supplied.encode(), ADMIN_TOKEN.encode()):
        raise HTTPException(403, "read-only demo: promoting failures needs the operator's admin token")


class BodyLimitMiddleware:
    """Reject request bodies over MAX_BODY_BYTES, whether or not the client sent Content-Length."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app, self.max = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > self.max:
                return await _reject(send, 413, b'{"detail":"request body too large"}')
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max:
                    raise _TooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await _reject(send, 413, b'{"detail":"request body too large"}')


class _TooLarge(Exception):
    pass


async def _reject(send, status: int, body: bytes):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def security_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    response.headers["Content-Security-Policy"] = DOCS_CSP if path in ("/docs", "/redoc") else CSP
    for k, v in HEADERS.items():
        response.headers.setdefault(k, v)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme) if TRUST_PROXY else request.url.scheme
    if proto == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response
