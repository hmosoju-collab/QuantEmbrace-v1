"""
Kill Switch HTTP API — lightweight aiohttp endpoints for operator control.

Exposes three endpoints:
    GET  /risk/kill-switch/status    — current state (no auth required for monitoring)
    POST /risk/kill-switch/activate  — halt all trading
    POST /risk/kill-switch/deactivate — resume trading (requires explicit confirmation)

This module is wired into the risk engine's aiohttp Application in
``risk_engine/service.py`` via ``create_kill_switch_app()``.

Authentication note: In production, place an ALB + AWS Cognito or VPC-only
routing in front of these endpoints. The endpoints themselves do not perform
auth — that is handled at the network / ALB layer.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

try:
    from aiohttp import web
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from shared.logging.logger import get_logger

if TYPE_CHECKING:
    from risk_engine.killswitch.killswitch import KillSwitch
    from risk_engine.killswitch.auto_triggers import KillSwitchMonitor

logger = get_logger(__name__, service_name="risk_engine")

_REQUIRED_DEACTIVATION_CONFIRM = "I confirm trading should resume"


def create_kill_switch_app(
    kill_switch: "KillSwitch",
    monitor: "KillSwitchMonitor | None" = None,
) -> Any:
    """
    Build and return an aiohttp Application with kill-switch routes.

    Args:
        kill_switch: The KillSwitch instance to control.
        monitor: Optional KillSwitchMonitor (attached to app state for health).

    Returns:
        aiohttp.web.Application ready to be run or sub-mounted.

    Raises:
        ImportError: If aiohttp is not installed.
    """
    if not _AIOHTTP_AVAILABLE:
        raise ImportError(
            "aiohttp is required for the kill switch HTTP API. "
            "Install it with: pip install aiohttp"
        )

    app = web.Application()
    app["kill_switch"] = kill_switch
    app["monitor"] = monitor

    app.router.add_get("/risk/kill-switch/status", handle_status)
    app.router.add_post("/risk/kill-switch/activate", handle_activate)
    app.router.add_post("/risk/kill-switch/deactivate", handle_deactivate)

    return app


async def handle_status(request: Any) -> Any:
    """
    GET /risk/kill-switch/status

    Returns the current kill switch state as JSON. No side effects.

    Response body::

        {
            "active": true,
            "reason": "Daily loss limit exceeded",
            "activated_at": "2025-10-01T09:15:00+00:00",
            "activated_by": "loss_validator"
        }
    """
    ks: KillSwitch = request.app["kill_switch"]
    status = ks.get_status()
    return web.Response(
        status=200,
        content_type="application/json",
        text=json.dumps(status),
    )


async def handle_activate(request: Any) -> Any:
    """
    POST /risk/kill-switch/activate

    Request body (JSON)::

        {
            "reason": "Manual halt — suspected runaway strategy",
            "activated_by": "operator@example.com"  // optional
        }

    Response::

        {"status": "activated", "kill_switch": {...}}
        {"status": "already_active", "kill_switch": {...}}
    """
    ks: KillSwitch = request.app["kill_switch"]

    try:
        body = await request.json()
    except Exception:
        body = {}

    reason = body.get("reason", "Manual activation via API")
    activated_by = body.get("activated_by", "operator")

    already_active = ks.active

    if not already_active:
        await ks.activate(reason=reason, activated_by=activated_by)
        logger.warning(
            "Kill switch activated via HTTP API | reason=%s | by=%s",
            reason,
            activated_by,
        )

    response_status = "already_active" if already_active else "activated"
    return web.Response(
        status=200,
        content_type="application/json",
        text=json.dumps({"status": response_status, "kill_switch": ks.get_status()}),
    )


async def handle_deactivate(request: Any) -> Any:
    """
    POST /risk/kill-switch/deactivate

    Requires an explicit confirmation string in the request body to prevent
    accidental deactivation from misconfigured automation.

    Request body (JSON)::

        {
            "confirmation": "I confirm trading should resume",
            "deactivated_by": "operator@example.com"   // optional
        }

    Response::

        {"status": "deactivated", "kill_switch": {...}}
        {"status": "already_inactive", "kill_switch": {...}}
        {"status": "error", "message": "Missing confirmation"}  // 400
    """
    ks: KillSwitch = request.app["kill_switch"]

    try:
        body = await request.json()
    except Exception:
        body = {}

    confirmation = body.get("confirmation", "")
    deactivated_by = body.get("deactivated_by", "operator")

    if confirmation != _REQUIRED_DEACTIVATION_CONFIRM:
        logger.warning(
            "Kill switch deactivation rejected — missing/wrong confirmation | by=%s",
            deactivated_by,
        )
        return web.Response(
            status=400,
            content_type="application/json",
            text=json.dumps({
                "status": "error",
                "message": (
                    f"Confirmation required. "
                    f"Send {{\"confirmation\": \"{_REQUIRED_DEACTIVATION_CONFIRM}\"}}"
                ),
            }),
        )

    already_inactive = not ks.active

    if not already_inactive:
        await ks.deactivate(deactivated_by=deactivated_by)
        logger.warning(
            "Kill switch deactivated via HTTP API | by=%s",
            deactivated_by,
        )

    response_status = "already_inactive" if already_inactive else "deactivated"
    return web.Response(
        status=200,
        content_type="application/json",
        text=json.dumps({"status": response_status, "kill_switch": ks.get_status()}),
    )
