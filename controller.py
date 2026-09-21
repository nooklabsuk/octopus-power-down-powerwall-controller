"""Fail-closed Octopus Power Down Powerwall controller."""

from __future__ import annotations

import json
import logging
import os
import time
from asyncio import run, to_thread
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx
from pymyenergi.client import MyenergiClient
from pymyenergi.connection import Connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("powerdown_controller")
# Digest authentication can make several expected 401/200 exchanges per poll.
# Keep those transport details out of normal controller logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

STATE_PATH = Path("/state/active-session.json")
COMPLETED_PATH = Path("/state/completed-event.json")
def setting(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default or "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


def request(path: str, method: str = "GET", data: dict | None = None) -> object:
    url = setting("HA_URL").rstrip("/") + path
    req = Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {setting('HA_TOKEN')}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=20) as response:  # noqa: S310 - configured private HA URL
            return json.load(response)
    except (HTTPError, URLError) as err:
        raise RuntimeError(f"Home Assistant {method} {path} failed: {err}") from err


def state(entity_id: str) -> dict:
    result = request(f"/api/states/{entity_id}")
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected state response for {entity_id}")
    return result


async def _read_myenergi_grid_power() -> float:
    async with httpx.AsyncClient() as client:
        connection = Connection(
            setting("MYENERGI_USERNAME"),
            setting("MYENERGI_PASSWORD"),
            timeout=10,
            asyncClient=client,
        )
        # Myenergi's director currently returns the ASN header lower-cased by
        # HTTP clients, but pymyenergi 0.2.3 checks only its original casing.
        # Resolve the regional endpoint explicitly before the library refresh.
        response = await client.get(
            f"{connection.director_url}/cgi-jstatus-E",
            auth=httpx.DigestAuth(connection.username, connection.password),
            headers=connection.headers,
            timeout=connection.timeout,
        )
        response.raise_for_status()
        asn = response.headers.get("x_myenergi-asn")
        if not asn:
            raise RuntimeError("Myenergi director response did not include an ASN endpoint")
        connection.base_url = f"https://{asn}"
        connection.do_query_asn = False
        await to_thread(connection.checkAndUpdateToken)
        myenergi = MyenergiClient(connection)
        await myenergi.refresh()
        return float(myenergi.power_grid)


def myenergi_grid_power() -> float:
    """Read current signed grid power directly from Myenergi's cloud API."""
    try:
        return run(_read_myenergi_grid_power())
    except Exception:
        LOGGER.exception("Direct Myenergi grid-power read failed")
        raise


def export_energy_kwh(
    previous_grid_power_w: float, current_grid_power_w: float, elapsed_seconds: float
) -> float:
    """Integrate only the exporting part of a signed grid-power sample."""
    sign = float(setting("MYENERGI_EXPORT_SIGN", "-1"))
    previous_export_w = max(0.0, previous_grid_power_w * sign)
    current_export_w = max(0.0, current_grid_power_w * sign)
    return (previous_export_w + current_export_w) / 2 * elapsed_seconds / 3_600_000


def call_service(domain: str, service: str, data: dict) -> None:
    request(f"/api/services/{domain}/{service}", "POST", data)


def live_tariff() -> dict:
    diagnostics = request(
        f"/api/diagnostics/config_entry/{setting('TESLEMETRY_CONFIG_ENTRY_ID')}"
    )
    try:
        sites = diagnostics["data"]["energysites"]
        if len(sites) != 1:
            raise RuntimeError(f"Expected one Teslemetry energy site, found {len(sites)}")
        info = sites[0]["info"]
    except (KeyError, TypeError) as err:
        raise RuntimeError("Teslemetry diagnostics did not contain energy-site data") from err

    prefix = "tariff_content_v2_"
    sell_prefix = f"{prefix}sell_tariff_"
    tariff = {
        key.removeprefix(prefix): value
        for key, value in info.items()
        if key.startswith(prefix) and not key.startswith(sell_prefix)
    }
    tariff["sell_tariff"] = {
        key.removeprefix(sell_prefix): value
        for key, value in info.items()
        if key.startswith(sell_prefix)
    }
    if not tariff.get("sell_tariff"):
        raise RuntimeError("Live Teslemetry tariff has no sell_tariff")
    return tariff


def log_startup() -> None:
    grid_power_w = myenergi_grid_power()
    version = setting("POWERDOWN_CONTROLLER_VERSION", "unknown")
    LOGGER.info("=== OCTOPUS POWER DOWN POWERWALL CONTROLLER %s ===", version)
    LOGGER.info(
        "Ready target_kwh=%.3f timeout_seconds=%s myenergi_poll_seconds=%s initial_grid_power_w=%.1f",
        float(setting("EXPORT_TARGET_KWH", "0.85")),
        setting("MAX_EXPORT_SECONDS", "420"),
        setting("MYENERGI_POLL_SECONDS", "5"),
        grid_power_w,
    )


def wait_for_startup_health() -> None:
    """Block monitoring until direct Myenergi health checks succeed."""
    retry_seconds = int(setting("STARTUP_RETRY_SECONDS", "30"))
    while True:
        try:
            log_startup()
            return
        except Exception:
            LOGGER.exception(
                "Startup health check failed; retrying in %s seconds without monitoring events",
                retry_seconds,
            )
            time.sleep(retry_seconds)


def read_session() -> dict | None:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else None


def write_session(session: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(session, indent=2) + "\n")


def completed_event() -> str | None:
    if not COMPLETED_PATH.exists():
        return None
    return json.loads(COMPLETED_PATH.read_text()).get("event_start")


def calendar() -> tuple[bool, str | None, str | None]:
    event = state(setting("POWER_DOWN_CALENDAR"))
    attributes = event.get("attributes", {})
    return event["state"] == "on", attributes.get("start_time"), attributes.get("end_time")


def restore(reason: str) -> None:
    session = read_session()
    if session is None:
        return
    LOGGER.warning("Restoring saved tariff and %s: %s", session["original_mode"], reason)
    errors: list[str] = []
    try:
        call_service(
            "teslemetry",
            "time_of_use",
            {"device_id": setting("TESLEMETRY_DEVICE_ID"), "tou_settings": session["tariff_snapshot"]},
        )
    except Exception as err:
        LOGGER.exception("Tariff restore request failed")
        errors.append(f"tariff restore: {err}")
    try:
        call_service(
            "select",
            "select_option",
            {"entity_id": setting("OPERATION_MODE_ENTITY"), "option": session["original_mode"]},
        )
    except Exception as err:
        LOGGER.exception("Powerwall mode restore request failed")
        errors.append(f"mode restore: {err}")
    if errors:
        session["restore_error"] = "; ".join(errors)
        session["restore_attempted_at"] = datetime.now(UTC).isoformat()
        write_session(session)
        raise RuntimeError(session["restore_error"])
    for _ in range(6):
        if state(setting("OPERATION_MODE_ENTITY"))["state"] == session["original_mode"]:
            break
        time.sleep(10)
    else:
        raise RuntimeError("Powerwall operation mode did not return to the original mode")
    COMPLETED_PATH.write_text(json.dumps({"event_start": session["event_start"]}) + "\n")
    STATE_PATH.unlink(missing_ok=True)
    LOGGER.info("Restore verified: operation mode is %s", session["original_mode"])


def start_event(event_start: str, event_end: str | None) -> None:
    original_mode = state(setting("OPERATION_MODE_ENTITY"))["state"]
    initial_grid_power_w = myenergi_grid_power()
    initial_sample_at = datetime.now(UTC)
    tariff = live_tariff()
    temporary = json.loads(json.dumps(tariff))
    period = setting("EXPORT_RATE_PERIOD", "PARTIAL_PEAK")
    high_rate = float(setting("TEMPORARY_SELL_RATE", "3"))
    buy_rate = temporary["energy_charges"]["Summer"]["rates"][period]
    if buy_rate < high_rate:
        raise RuntimeError("Temporary sell rate exceeds corresponding buy rate")
    temporary["sell_tariff"]["energy_charges"]["Summer"]["rates"][period] = high_rate

    session = {
        "event_start": event_start,
        "event_end": event_end,
        "started_at": initial_sample_at.isoformat(),
        "exported_kwh": 0.0,
        "last_grid_power_w": initial_grid_power_w,
        "last_power_sample_at": initial_sample_at.isoformat(),
        "original_mode": original_mode,
        "tariff_snapshot": tariff,
        "target_export_kwh": float(setting("EXPORT_TARGET_KWH", "0.85")),
        "timeout_seconds": int(setting("MAX_EXPORT_SECONDS", "420")),
    }
    # Persist restoration information before the first Tesla write.
    write_session(session)
    try:
        call_service(
            "teslemetry",
            "time_of_use",
            {"device_id": setting("TESLEMETRY_DEVICE_ID"), "tou_settings": temporary},
        )
        call_service(
            "select",
            "select_option",
            {"entity_id": setting("OPERATION_MODE_ENTITY"), "option": "autonomous"},
        )
    except Exception:
        LOGGER.exception("Event start failed; attempting immediate restoration")
        restore("unable to start export")
        raise
    LOGGER.warning("Export started: %.3f kWh target", session["target_export_kwh"])


def monitor_event() -> None:
    session = read_session()
    if session is None:
        return
    now = datetime.now(UTC)
    grid_power_w = myenergi_grid_power()
    if session["last_power_sample_at"] is not None:
        elapsed_seconds = now.timestamp() - datetime.fromisoformat(session["last_power_sample_at"]).timestamp()
        session["exported_kwh"] += export_energy_kwh(
            session["last_grid_power_w"], grid_power_w, elapsed_seconds
        )
    session["last_grid_power_w"] = grid_power_w
    session["last_power_sample_at"] = now.isoformat()
    write_session(session)
    export = session["exported_kwh"]
    elapsed = now.timestamp() - datetime.fromisoformat(session["started_at"]).timestamp()
    event_active, _, _ = calendar()
    if export >= session["target_export_kwh"]:
        restore(f"measured export cap reached: {export:.3f} kWh")
    elif elapsed >= session["timeout_seconds"]:
        restore(f"hard timeout reached: {elapsed:.0f} seconds")
    elif not event_active:
        restore("Power Down calendar event ended")
    else:
        LOGGER.info(
            "Export progress exported_kwh=%.3f target_kwh=%.3f grid_power_w=%.1f",
            export,
            session["target_export_kwh"],
            grid_power_w,
        )


def main() -> None:
    wait_for_startup_health()
    # Never resume an incomplete export after process/container restart.
    if read_session() is not None:
        try:
            restore("controller restart")
        except Exception:
            LOGGER.exception("Startup recovery restore failed")
            raise
    idle_poll = int(setting("IDLE_POLL_SECONDS", "60"))
    active_poll = int(setting("MYENERGI_POLL_SECONDS", "5"))
    last_event_start: str | None = None
    while True:
        try:
            if read_session() is not None:
                monitor_event()
                time.sleep(active_poll)
                continue
            event_active, event_start, event_end = calendar()
            if event_active and event_start and event_start not in {last_event_start, completed_event()}:
                # Only mark this event as handled once start_event() actually
                # succeeds. If it raises (e.g. a transient state-write or
                # Tesla API failure), leave last_event_start unset so the next
                # cycle retries the same event instead of silently skipping
                # it for the rest of its active window.
                start_event(event_start, event_end)
                last_event_start = event_start
                time.sleep(active_poll)
                continue
            if not event_active:
                last_event_start = None
                COMPLETED_PATH.unlink(missing_ok=True)
                LOGGER.info("Idle: no active Power Down event")
            elif event_start == last_event_start:
                # This event was already started (and likely already capped
                # and restored). Log this explicitly so the container isn't
                # silently quiet for the remainder of the calendar event.
                LOGGER.info(
                    "Event already handled; waiting for calendar event to end: %s",
                    event_end,
                )
        except Exception:
            LOGGER.exception("Controller cycle failed")
            try:
                restore("controller error")
            except Exception:
                LOGGER.exception("Emergency restore failed")
            time.sleep(idle_poll)
            continue
        time.sleep(idle_poll)


if __name__ == "__main__":
    main()
