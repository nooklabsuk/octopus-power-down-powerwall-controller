"""Export a complete Tesla tariff baseline from Teslemetry diagnostics."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


def main() -> None:
    output = Path("tariff/teslemetry-normal-tariff.json")
    request = Request(
        f"{required('HA_URL').rstrip('/')}/api/diagnostics/config_entry/{required('TESLEMETRY_CONFIG_ENTRY_ID')}",
        headers={"Authorization": f"Bearer {required('HA_TOKEN')}"},
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - configured Home Assistant URL
            diagnostics = json.load(response)
    except (HTTPError, URLError) as err:
        raise RuntimeError(f"Unable to download Teslemetry diagnostics: {err}") from err

    sites = diagnostics["data"]["energysites"]
    if len(sites) != 1:
        raise RuntimeError(f"Expected exactly one energy site, found {len(sites)}")
    info = sites[0]["info"]
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
    required_keys = {"energy_charges", "seasons", "sell_tariff", "version"}
    missing = required_keys - tariff.keys()
    if missing or not tariff["sell_tariff"]:
        raise RuntimeError(f"Teslemetry diagnostics did not provide a complete tariff: {sorted(missing)}")

    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(tariff, indent=2) + "\n")
    os.chmod(output, 0o600)
    print(f"Saved tariff baseline to {output}")


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        print(f"Tariff export failed: {err}", file=sys.stderr)
        raise SystemExit(1)
