import importlib.util
import json
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.error import URLError


MODULE_PATH = Path(__file__).parents[1] / "controller.py"
SPEC = importlib.util.spec_from_file_location("controller", MODULE_PATH)
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)

def tariff() -> dict:
    return {
        "name": "Test",
        "utility": "Test Utility",
        "energy_charges": {"Summer": {"rates": {"PARTIAL_PEAK": 3}}},
        "seasons": {"Summer": {}},
        "sell_tariff": {
            "energy_charges": {"Summer": {"rates": {"PARTIAL_PEAK": 0.12}}},
            "seasons": {"Summer": {}},
        },
    }


class AsyncClientContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, *args, **kwargs):
        return DirectorResponse()


class DirectorResponse:
    headers = {"x_myenergi-asn": "s18.myenergi.net"}

    def raise_for_status(self):
        return None


class ControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tempdir.name) / "active-session.json"
        self.completed_path = Path(self.tempdir.name) / "completed-event.json"
        self.paths = patch.multiple(
            controller,
            STATE_PATH=self.state_path,
            COMPLETED_PATH=self.completed_path,
        )
        self.paths.start()
        self.environment = patch.dict(
            os.environ,
            {
                "HA_URL": "http://homeassistant.test",
                "HA_TOKEN": "test-token",
                "TESLEMETRY_CONFIG_ENTRY_ID": "test-entry",
                "TESLEMETRY_DEVICE_ID": "test-device",
                "POWER_DOWN_CALENDAR": "calendar.power_down",
                "OPERATION_MODE_ENTITY": "select.operation_mode",
                "MYENERGI_USERNAME": "test-hub",
                "MYENERGI_PASSWORD": "test-key",
                "MYENERGI_EXPORT_SIGN": "-1",
                "EXPORT_TARGET_KWH": "0.85",
                "MAX_EXPORT_SECONDS": "420",
                "EXPORT_RATE_PERIOD": "PARTIAL_PEAK",
                "TEMPORARY_SELL_RATE": "3",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.paths.stop()
        self.tempdir.cleanup()

    @patch.object(controller, "request")
    def test_live_tariff_reconstructs_diagnostics(self, request) -> None:
        request.return_value = {
            "data": {
                "energysites": [
                    {
                        "info": {
                            "tariff_content_v2_version": 1,
                            "tariff_content_v2_energy_charges": {"Summer": {}},
                            "tariff_content_v2_seasons": {"Summer": {}},
                            "tariff_content_v2_sell_tariff_energy_charges": {"Summer": {}},
                            "tariff_content_v2_sell_tariff_seasons": {"Summer": {}},
                        }
                    }
                ]
            }
        }

        snapshot = controller.live_tariff()

        self.assertEqual(snapshot["version"], 1)
        self.assertEqual(snapshot["sell_tariff"]["seasons"], {"Summer": {}})

    @patch.object(controller, "myenergi_grid_power", return_value=15)
    @patch.object(controller, "LOGGER")
    def test_startup_log_reports_safe_operational_details(self, logger, grid_power) -> None:
        with patch.dict(os.environ, {"POWERDOWN_CONTROLLER_VERSION": "v1.2.3"}):
            controller.log_startup()

        message, *values = logger.info.call_args.args
        self.assertIn("target_kwh=%.3f", message)
        self.assertIn("initial_grid_power_w=%.1f", message)
        self.assertEqual(logger.info.call_count, 2)
        self.assertIn("v1.2.3", str(logger.info.call_args_list))
        self.assertNotIn("test-key", str(logger.info.call_args))

    @patch.object(controller, "time")
    @patch.object(controller, "LOGGER")
    @patch.object(controller, "log_startup", side_effect=[RuntimeError("DNS unavailable"), None])
    def test_startup_health_retries_without_exiting(self, log_startup, logger, time) -> None:
        controller.wait_for_startup_health()

        self.assertEqual(log_startup.call_count, 2)
        logger.exception.assert_called_once()
        time.sleep.assert_called_once_with(30)

    @patch.object(controller, "wait_for_startup_health")
    @patch.object(controller, "time")
    @patch.object(controller, "restore")
    @patch.object(controller, "start_event")
    @patch.object(controller, "completed_event", return_value=None)
    @patch.object(controller, "calendar")
    @patch.object(controller, "read_session", return_value=None)
    def test_main_retries_same_event_after_start_event_failure(
        self,
        read_session,
        calendar,
        completed_event,
        start_event,
        restore,
        time,
        wait_for_startup_health,
    ) -> None:
        event_start = "2026-09-21T18:00:00+01:00"
        event_end = "2026-09-21T19:00:00+01:00"
        calendar.return_value = (True, event_start, event_end)
        # First attempt fails (e.g. a transient state-write permission
        # error), second attempt succeeds and must not be retried again.
        start_event.side_effect = [RuntimeError("permission denied"), None]
        # Break out of main()'s infinite loop once the retry has been
        # observed and the successful call's sleep has happened, since
        # main() intentionally catches ordinary Exceptions and keeps going.
        time.sleep.side_effect = [None, None, SystemExit]

        with self.assertRaises(SystemExit):
            controller.main()

        # start_event must be retried once for the *same* event after it
        # raised, instead of being silently skipped for the rest of its
        # active window, and must not be called again once it has succeeded.
        self.assertEqual(
            start_event.call_args_list,
            [
                ((event_start, event_end),),
                ((event_start, event_end),),
            ],
        )
        restore.assert_called_once_with("controller error")

    @patch.object(controller, "request", return_value={"data": {"energysites": [{"info": {}}]}})
    def test_live_tariff_rejects_missing_sell_tariff(self, request) -> None:
        with self.assertRaisesRegex(RuntimeError, "sell_tariff"):
            controller.live_tariff()

    def test_session_and_completed_event_round_trip(self) -> None:
        self.assertIsNone(controller.read_session())
        self.assertIsNone(controller.completed_event())

        controller.write_session({"event_start": "event", "original_mode": "self_consumption"})
        self.completed_path.write_text(json.dumps({"event_start": "completed-event"}))

        self.assertEqual(controller.read_session()["event_start"], "event")
        self.assertEqual(controller.completed_event(), "completed-event")

    @patch.object(controller, "state")
    def test_calendar_reads_active_event_times(self, state) -> None:
        state.return_value = {
            "state": "on",
            "attributes": {"start_time": "start", "end_time": "end"},
        }

        self.assertEqual(controller.calendar(), (True, "start", "end"))

    def test_integrates_signed_export_power(self) -> None:
        self.assertAlmostEqual(controller.export_energy_kwh(-10_000, -10_000, 5), 0.0138889)
        self.assertEqual(controller.export_energy_kwh(1_000, 1_000, 5), 0)

    @patch.object(controller, "_read_myenergi_grid_power", new_callable=AsyncMock, return_value=-1_234)
    def test_reads_current_myenergi_grid_power_directly(self, read_power) -> None:
        self.assertEqual(controller.myenergi_grid_power(), -1_234)
        read_power.assert_awaited_once()

    @patch.object(controller, "_read_myenergi_grid_power", new_callable=AsyncMock, side_effect=RuntimeError("API down"))
    @patch.object(controller, "LOGGER")
    def test_direct_myenergi_failure_logs_root_cause(self, logger, read_power) -> None:
        with self.assertRaisesRegex(RuntimeError, "API down"):
            controller.myenergi_grid_power()

        logger.exception.assert_called_once_with("Direct Myenergi grid-power read failed")

    @patch.object(controller, "to_thread", new_callable=AsyncMock)
    @patch.object(controller, "MyenergiClient")
    @patch.object(controller, "Connection")
    @patch.object(controller.httpx, "AsyncClient", return_value=AsyncClientContext())
    def test_direct_myenergi_read_refreshes_current_power(
        self, async_client, connection, myenergi_client, to_thread
    ) -> None:
        client = myenergi_client.return_value
        client.power_grid = -4_200
        client.refresh = AsyncMock()

        self.assertEqual(__import__("asyncio").run(controller._read_myenergi_grid_power()), -4_200)

        connection.assert_called_once_with(
            "test-hub", "test-key", timeout=10, asyncClient=async_client.return_value
        )
        to_thread.assert_awaited_once_with(connection.return_value.checkAndUpdateToken)
        client.refresh.assert_awaited_once()

    @patch.object(controller, "to_thread", new_callable=AsyncMock)
    @patch.object(controller, "MyenergiClient")
    @patch.object(controller, "Connection")
    @patch.object(controller.httpx, "AsyncClient", return_value=AsyncClientContext())
    def test_direct_myenergi_read_rejects_missing_director_endpoint(
        self, async_client, connection, myenergi_client, to_thread
    ) -> None:
        async_client.return_value.get = AsyncMock(return_value=type("Response", (), {"headers": {}, "raise_for_status": lambda self: None})())

        with self.assertRaisesRegex(RuntimeError, "ASN endpoint"):
            __import__("asyncio").run(controller._read_myenergi_grid_power())

        to_thread.assert_not_awaited()

    @patch.object(controller, "datetime")
    @patch.object(controller, "restore")
    @patch.object(controller, "calendar", return_value=(True, None, None))
    @patch.object(controller, "myenergi_grid_power", return_value=-10_000)
    def test_monitor_integrates_direct_power_samples(
        self, myenergi_grid_power, calendar, restore, datetime
    ) -> None:
        datetime.now.return_value.isoformat.return_value = "2026-01-01T00:00:05+00:00"
        datetime.now.return_value.timestamp.return_value = 1_767_225_605
        datetime.fromisoformat.return_value.timestamp.side_effect = [1_767_225_600, 1_767_225_600]
        controller.write_session(
            {
                "event_start": "event",
                "started_at": "2026-01-01T00:00:00+00:00",
                "exported_kwh": 0,
                "last_grid_power_w": -10_000,
                "last_power_sample_at": "2026-01-01T00:00:00+00:00",
                "target_export_kwh": 0.85,
                "timeout_seconds": 420,
                "original_mode": "self_consumption",
            }
        )

        controller.monitor_event()

        session = controller.read_session()
        self.assertAlmostEqual(session["exported_kwh"], 0.0138889)
        restore.assert_not_called()

    @patch.object(controller, "urlopen", side_effect=URLError("offline"))
    def test_request_wraps_home_assistant_network_failure(self, urlopen) -> None:
        with self.assertRaisesRegex(RuntimeError, "Home Assistant GET"):
            controller.request("/api/states/sensor.export")

    @patch.object(controller, "call_service")
    @patch.object(controller, "myenergi_grid_power", return_value=0)
    @patch.object(controller, "state")
    @patch.object(controller, "live_tariff", return_value=tariff())
    def test_start_event_persists_before_powerwall_writes(
        self, live_tariff, state, myenergi_grid_power, call_service
    ) -> None:
        state.return_value = {"state": "self_consumption"}

        controller.start_event("2026-09-20T18:00:00+01:00", "2026-09-20T19:00:00+01:00")

        session = json.loads(self.state_path.read_text())
        self.assertEqual(session["exported_kwh"], 0)
        self.assertEqual(session["last_grid_power_w"], 0)
        self.assertEqual(session["original_mode"], "self_consumption")
        self.assertEqual(session["tariff_snapshot"], tariff())
        self.assertEqual(call_service.call_count, 2)
        self.assertEqual(call_service.call_args_list[0].args[:2], ("teslemetry", "time_of_use"))
        self.assertEqual(call_service.call_args_list[1].args[:2], ("select", "select_option"))

    @patch.object(controller, "restore")
    @patch.object(controller, "calendar", return_value=(True, None, None))
    @patch.object(controller, "myenergi_grid_power", return_value=-10_500)
    def test_monitor_restores_at_export_cap(self, myenergi_grid_power, calendar, restore) -> None:
        controller.write_session(
            {
                "event_start": "event",
                "started_at": "2000-01-01T00:00:00+00:00",
                "exported_kwh": 0.85,
                "last_grid_power_w": -10_500,
                "last_power_sample_at": "2000-01-01T00:00:00+00:00",
                "target_export_kwh": 0.85,
                "timeout_seconds": 420,
                "original_mode": "self_consumption",
            }
        )

        controller.monitor_event()

        self.assertIn("measured export cap reached", restore.call_args.args[0])

    @patch.object(controller, "restore")
    @patch.object(controller, "calendar", return_value=(False, None, None))
    @patch.object(controller, "myenergi_grid_power", return_value=0)
    def test_monitor_restores_when_calendar_event_ends(self, myenergi_grid_power, calendar, restore) -> None:
        controller.write_session(
            {
                "event_start": "event",
                "started_at": "2999-01-01T00:00:00+00:00",
                "exported_kwh": 0,
                "last_grid_power_w": 0,
                "last_power_sample_at": "2999-01-01T00:00:00+00:00",
                "target_export_kwh": 0.85,
                "timeout_seconds": 420,
                "original_mode": "self_consumption",
            }
        )

        controller.monitor_event()

        self.assertEqual(restore.call_args.args[0], "Power Down calendar event ended")

    @patch.object(controller, "state", return_value={"state": "self_consumption"})
    @patch.object(controller, "call_service")
    def test_restore_reapplies_tariff_and_original_mode(self, call_service, state) -> None:
        snapshot = tariff()
        snapshot["name"] = "Saved at event start"
        controller.write_session(
            {
                "event_start": "event",
                "original_mode": "self_consumption",
                "tariff_snapshot": snapshot,
            }
        )

        controller.restore("test")

        self.assertFalse(self.state_path.exists())
        self.assertEqual(json.loads(self.completed_path.read_text())["event_start"], "event")
        self.assertEqual(call_service.call_count, 2)
        self.assertEqual(call_service.call_args_list[0].args[:2], ("teslemetry", "time_of_use"))
        self.assertEqual(call_service.call_args_list[0].args[2]["tou_settings"], snapshot)
        self.assertEqual(call_service.call_args_list[1].args[:2], ("select", "select_option"))

    @patch.object(controller, "time")
    @patch.object(controller, "state", return_value={"state": "autonomous"})
    @patch.object(controller, "call_service")
    def test_restore_preserves_session_when_mode_never_returns(self, call_service, state, time) -> None:
        controller.write_session(
            {
                "event_start": "event",
                "original_mode": "self_consumption",
                "tariff_snapshot": tariff(),
            }
        )

        with self.assertRaisesRegex(RuntimeError, "did not return"):
            controller.restore("test")

        self.assertTrue(self.state_path.exists())
        self.assertFalse(self.completed_path.exists())

    @patch.object(controller, "state", return_value={"state": "self_consumption"})
    @patch.object(controller, "call_service", side_effect=RuntimeError("service error"))
    @patch.object(controller, "LOGGER")
    def test_restore_logs_failed_requests(self, logger, call_service, state) -> None:
        controller.write_session(
            {"event_start": "event", "original_mode": "self_consumption", "tariff_snapshot": tariff()}
        )

        with self.assertRaisesRegex(RuntimeError, "tariff restore"):
            controller.restore("test")

        self.assertEqual(logger.exception.call_count, 2)

    @patch.object(controller, "restore")
    @patch.object(controller, "calendar", return_value=(True, None, None))
    @patch.object(controller, "myenergi_grid_power", return_value=0)
    def test_monitor_restores_at_timeout(self, myenergi_grid_power, calendar, restore) -> None:
        controller.write_session(
            {
                "event_start": "event",
                "started_at": "2000-01-01T00:00:00+00:00",
                "exported_kwh": 0,
                "last_grid_power_w": 0,
                "last_power_sample_at": "2000-01-01T00:00:00+00:00",
                "target_export_kwh": 0.85,
                "timeout_seconds": 420,
                "original_mode": "self_consumption",
            }
        )

        controller.monitor_event()

        self.assertIn("hard timeout reached", restore.call_args.args[0])

    def test_emergency_restore_calls_controller_restore(self) -> None:
        with patch.dict(sys.modules, {"controller": controller}):
            with patch.object(controller, "restore") as restore:
                runpy.run_path(Path(__file__).parents[1] / "emergency_restore.py", run_name="__main__")

        self.assertEqual(restore.call_args.args[0], "manual emergency restore")

    @patch.object(controller, "restore")
    @patch.object(controller, "call_service", side_effect=RuntimeError("Tesla rejected request"))
    @patch.object(controller, "myenergi_grid_power", return_value=0)
    @patch.object(controller, "state", return_value={"state": "self_consumption"})
    @patch.object(controller, "live_tariff", return_value=tariff())
    def test_start_event_restores_when_temporary_tariff_fails(
        self, live_tariff, state, myenergi_grid_power, call_service, restore
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "Tesla rejected request"):
            controller.start_event("event", "end")

        self.assertEqual(restore.call_args.args[0], "unable to start export")


if __name__ == "__main__":
    unittest.main()
