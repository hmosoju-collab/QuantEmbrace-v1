"""
Health Server — HTTP liveness and readiness endpoints for all services.

Exposes two endpoints on a configurable port (default 8080):

    GET /health   — Liveness probe.
                    Returns 200 if the process is alive and the event loop is
                    running. ASG and ECS use this to detect crashed processes.
                    Never returns an error unless the process is actually dead.

    GET /ready    — Readiness probe.
                    Returns 200 only when the service has completed startup and
                    all dependencies (broker connections, DynamoDB, strategies)
                    are confirmed healthy. Returns 503 otherwise.

                    The ASG marks an instance InService only after /ready passes.
                    This prevents an instance from receiving traffic while it is
                    still warming up, reconnecting to brokers, or rehydrating state.

Why both endpoints:
    /health is cheap and always succeeds — it exists to tell the ASG "the OS and
    process are alive, don't terminate me." /ready is meaningful — it tells the
    ASG "I am fully operational, let traffic/signals flow to me now."

    Using only /health means the ASG may route work to an instance that is still
    in the middle of broker authentication, DynamoDB state rehydration, or WebSocket
    warm-up. This produces silent partial-failures: the instance receives messages
    but cannot process them correctly yet.

Usage::

    server = HealthServer(port=8080)
    server.set_ready(False)                    # default — not ready at start

    # ... perform startup (connect broker, rehydrate state, etc.) ...

    server.add_check("broker", lambda: broker.is_connected())
    server.add_check("dynamodb", lambda: dynamo_client is not None)

    server.set_ready(True)                     # all startup complete

    # Run server alongside the main service loop
    await asyncio.gather(
        server.start(),
        service.run(),
    )

    # On shutdown
    server.set_ready(False)
    await server.stop()

Check callbacks:
    Callable[[], bool] — synchronous, called on every /ready request.
    Keep them fast — they run inside the request handler and block the
    event loop while executing. Use simple attribute lookups, not I/O.
"""

from __future__ import annotations

import asyncio
import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Type alias for a readiness check function
ReadinessCheck = Callable[[], bool]


class HealthServer:
    """
    Lightweight HTTP health and readiness server.

    Runs in a dedicated daemon thread to avoid competing with the asyncio
    event loop. Checks are registered as synchronous callables so they are
    safe to call from the thread context.

    Thread safety:
        ``_ready`` is read/written atomically (CPython GIL). ``_checks`` is
        only written during startup (before traffic flows), so no locking is
        needed in practice. If checks are added dynamically, callers must
        ensure adds complete before concurrent /ready requests arrive.
    """

    def __init__(self, port: int = 8080, service_name: str = "unknown") -> None:
        self._port = port
        self._service_name = service_name
        self._ready: bool = False
        self._checks: dict[str, ReadinessCheck] = {}
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[Thread] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def set_ready(self, ready: bool) -> None:
        """
        Flip the overall readiness flag.

        Call ``set_ready(True)`` after all startup is complete.
        Call ``set_ready(False)`` before beginning shutdown so the load
        balancer/ASG stops routing work to this instance.

        Args:
            ready: True = ready for traffic; False = not ready.
        """
        self._ready = ready
        logger.info(
            "health_server.readiness_changed service=%s ready=%s",
            self._service_name,
            ready,
        )

    def add_check(self, name: str, check: ReadinessCheck) -> None:
        """
        Register a named readiness check function.

        The check must be a fast, synchronous callable returning bool.
        All registered checks must return True for /ready to return 200.

        Args:
            name:  Human-readable check name (appears in /ready response JSON).
            check: Callable returning True if this dependency is healthy.
        """
        self._checks[name] = check
        logger.debug("health_server.check_registered name=%s", name)

    async def start(self) -> None:
        """
        Start the HTTP server in a daemon thread.

        Returns immediately — the server runs in the background.
        Safe to await before or after the main service loop starts.
        """
        handler = self._make_handler()
        self._server = HTTPServer(("0.0.0.0", self._port), handler)
        self._thread = Thread(
            target=self._server.serve_forever,
            name=f"health-{self._service_name}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "health_server.started service=%s port=%d",
            self._service_name,
            self._port,
        )

    async def stop(self) -> None:
        """Shut down the HTTP server gracefully."""
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("health_server.stopped service=%s", self._service_name)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        """Return a request handler class bound to this HealthServer instance."""
        server_ref = self  # capture for closure

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._send_json(200, {"status": "ok", "service": server_ref._service_name})

                elif self.path == "/ready":
                    failed: dict[str, str] = {}

                    if not server_ref._ready:
                        failed["_ready_flag"] = "set_ready(True) not yet called"
                    else:
                        for name, check in server_ref._checks.items():
                            try:
                                if not check():
                                    failed[name] = "check returned False"
                            except Exception as exc:
                                failed[name] = f"check raised: {exc}"

                    if failed:
                        self._send_json(
                            503,
                            {
                                "status": "not_ready",
                                "service": server_ref._service_name,
                                "failed_checks": failed,
                            },
                        )
                    else:
                        self._send_json(
                            200,
                            {
                                "status": "ready",
                                "service": server_ref._service_name,
                                "checks": list(server_ref._checks.keys()),
                            },
                        )
                else:
                    self._send_json(404, {"error": "not found"})

            def _send_json(self, status: int, body: dict) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt: str, *args: object) -> None:
                # Suppress default per-request stdout logging — our logger handles it
                pass

        return _Handler
