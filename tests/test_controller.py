import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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


class ControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tempdir.name) / "active-session.json"
        self.completed_path = Path(self.tempdir.name) / "completed-event.json"
        self.tariff_path = Path(self.tempdir.name) / "tariff.json"
        self.tariff_path.write_text(json.dumps(tariff()))
        self.paths = patch.multiple(
            controller,
            STATE_PATH=self.state_path,
            COMPLETED_PATH=self.completed_path,
            TARIFF_PATH=self.tariff_path,
        )
        self.paths.start()
        self.environment = patch.dict(
            os.environ,
            {
                "HA_URL": "http://homeassistant.test",
                "HA_TOKEN": "test-token",
                "TESLEMETRY_DEVICE_ID": "test-device",
                "POWER_DOWN_CALENDAR": "calendar.power_down",
                "EXPORT_ENERGY_SENSOR": "sensor.export_today",
                "OPERATION_MODE_ENTITY": "select.operation_mode",
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

    def test_baseline_requires_sell_tariff(self) -> None:
        self.tariff_path.write_text(json.dumps({"name": "invalid"}))

        with self.assertRaisesRegex(RuntimeError, "sell_tariff"):
            controller.baseline()

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

    @patch.object(controller, "state", return_value={"state": "not-a-number"})
    def test_numeric_state_rejects_invalid_state(self, state) -> None:
        with self.assertRaisesRegex(RuntimeError, "numeric"):
            controller.numeric_state("sensor.export")

    @patch.object(controller, "call_service")
    @patch.object(controller, "numeric_state", return_value=4.2)
    @patch.object(controller, "state")
    def test_start_event_persists_before_powerwall_writes(
        self, state, numeric_state, call_service
    ) -> None:
        state.return_value = {"state": "self_consumption"}

        controller.start_event("2026-09-20T18:00:00+01:00", "2026-09-20T19:00:00+01:00")

        session = json.loads(self.state_path.read_text())
        self.assertEqual(session["initial_export_kwh"], 4.2)
        self.assertEqual(session["original_mode"], "self_consumption")
        self.assertEqual(call_service.call_count, 2)
        self.assertEqual(call_service.call_args_list[0].args[:2], ("teslemetry", "time_of_use"))
        self.assertEqual(call_service.call_args_list[1].args[:2], ("select", "select_option"))

    @patch.object(controller, "restore")
    @patch.object(controller, "calendar", return_value=(True, None, None))
    @patch.object(controller, "numeric_state", return_value=5.06)
    def test_monitor_restores_at_export_cap(self, numeric_state, calendar, restore) -> None:
        controller.write_session(
            {
                "event_start": "event",
                "started_at": "2026-09-20T18:00:00+00:00",
                "initial_export_kwh": 4.2,
                "target_export_kwh": 0.85,
                "timeout_seconds": 420,
                "original_mode": "self_consumption",
            }
        )

        controller.monitor_event()

        self.assertIn("measured export cap reached", restore.call_args.args[0])

    @patch.object(controller, "restore")
    @patch.object(controller, "calendar", return_value=(False, None, None))
    @patch.object(controller, "numeric_state", return_value=4.3)
    def test_monitor_restores_when_calendar_event_ends(self, numeric_state, calendar, restore) -> None:
        controller.write_session(
            {
                "event_start": "event",
                "started_at": "2999-01-01T00:00:00+00:00",
                "initial_export_kwh": 4.2,
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
        controller.write_session(
            {
                "event_start": "event",
                "original_mode": "self_consumption",
            }
        )

        controller.restore("test")

        self.assertFalse(self.state_path.exists())
        self.assertEqual(json.loads(self.completed_path.read_text())["event_start"], "event")
        self.assertEqual(call_service.call_count, 2)
        self.assertEqual(call_service.call_args_list[0].args[:2], ("teslemetry", "time_of_use"))
        self.assertEqual(call_service.call_args_list[1].args[:2], ("select", "select_option"))

    @patch.object(controller, "restore")
    @patch.object(controller, "call_service", side_effect=RuntimeError("Tesla rejected request"))
    @patch.object(controller, "numeric_state", return_value=4.2)
    @patch.object(controller, "state", return_value={"state": "self_consumption"})
    def test_start_event_restores_when_temporary_tariff_fails(
        self, state, numeric_state, call_service, restore
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "Tesla rejected request"):
            controller.start_event("event", "end")

        self.assertEqual(restore.call_args.args[0], "unable to start export")


if __name__ == "__main__":
    unittest.main()
