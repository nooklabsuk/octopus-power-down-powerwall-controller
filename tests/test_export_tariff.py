import importlib.util
import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "tools" / "export_tariff.py"
SPEC = importlib.util.spec_from_file_location("export_tariff", MODULE_PATH)
export_tariff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export_tariff)


class Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TariffExportTest(unittest.TestCase):
    def test_exports_flattened_diagnostics_as_full_tariff(self) -> None:
        diagnostics = {
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
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            os.environ,
            {
                "HA_URL": "http://homeassistant.test",
                "HA_TOKEN": "test-token",
                "TESLEMETRY_CONFIG_ENTRY_ID": "entry-id",
            },
            clear=False,
        ), patch.object(
            export_tariff,
            "urlopen",
            return_value=Response(json.dumps(diagnostics).encode()),
        ), patch.object(export_tariff, "Path") as path:
            output = Path(tempdir) / "tariff" / "teslemetry-normal-tariff.json"
            path.return_value = output
            export_tariff.main()

            tariff = json.loads(output.read_text())
            self.assertEqual(tariff["version"], 1)
            self.assertEqual(tariff["sell_tariff"]["seasons"], {"Summer": {}})
