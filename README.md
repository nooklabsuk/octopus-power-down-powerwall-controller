# Octopus Power Down Powerwall Controller

[![CI](https://github.com/nooklabsuk/octopus-power-down-powerwall-controller/actions/workflows/ci.yml/badge.svg)](https://github.com/nooklabsuk/octopus-power-down-powerwall-controller/actions/workflows/ci.yml)
[![Security](https://github.com/nooklabsuk/octopus-power-down-powerwall-controller/actions/workflows/security.yml/badge.svg)](https://github.com/nooklabsuk/octopus-power-down-powerwall-controller/actions/workflows/security.yml)
[![Latest release](https://img.shields.io/github/v/release/nooklabsuk/octopus-power-down-powerwall-controller?display_name=tag&sort=semver)](https://github.com/nooklabsuk/octopus-power-down-powerwall-controller/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-required-41BDF5?logo=home-assistant)](https://www.home-assistant.io/)
[![Teslemetry](https://img.shields.io/badge/Teslemetry-required-cc0000)](https://github.com/Teslemetry/hass-teslemetry)

Experimental Home Assistant controller that makes a small, capped Tesla
Powerwall 3 export during an active, opted-in Octopus Energy Power Down event.

It uses the [Teslemetry](https://github.com/Teslemetry/hass-teslemetry) Home
Assistant integration to temporarily apply a Time-Based Control tariff, switches
the Powerwall to `autonomous`, measures grid export, then restores the original
tariff and operation mode.

> [!WARNING]
> This project controls battery behaviour. It can cause unexpected Powerwall
> discharge, affect backup capacity, or fail to restore a tariff or operating
> mode. It is experimental, unaffiliated with Tesla, Octopus, Teslemetry, or
> Myenergi, and is provided without warranty. Use it entirely at your own risk.
> Test under observation before any unattended use.

## Contents

- [What It Does](#what-it-does)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [How It Stops](#how-it-stops)
- [Emergency Restore](#emergency-restore)
- [Tested Scope](#tested-scope)
- [Security](#security)

## What It Does

When the configured Octopus Power Down calendar becomes `on`, the controller:

1. Records the current cumulative grid-export meter and original Powerwall mode.
2. Persists that session state before changing Powerwall settings.
3. Applies a complete temporary Tesla tariff with a configured higher sell rate.
4. Switches the Powerwall to Tesla Time-Based Control (`autonomous`).
5. Polls the grid-export energy meter until a capped export target is reached.
6. Restores the exact saved tariff and original Powerwall mode.

The default target is **0.85 kWh**. It deliberately leaves margin below a 1 kWh
maximum for meter sampling and Tesla API latency. The timeout is only a failsafe,
not the primary energy limit.

The controller waits for the calendar state to be `on`. It does not export simply
because an upcoming Power Down event is visible on a calendar.

## Requirements

- Tesla Powerwall 3.
- Home Assistant with Docker Compose.
- **Teslemetry Home Assistant integration** with Powerwall energy-site support.
  The controller calls `teslemetry.time_of_use` to submit the temporary tariff.
- Teslemetry Powerwall operation-mode entity with an `autonomous` option.
- Octopus Energy Home Assistant integration with a Power Down calendar that is
  `on` only for an active event you joined.
- A current cumulative **grid-export energy** meter in kWh. It should have
  `state_class: total_increasing`; Myenergi `grid_export_today` is one example.
- A complete, known-good Tesla `tariff_content_v2` baseline containing a nested
  `sell_tariff`.
- A dedicated Home Assistant long-lived token.

## Quick Start

1. Copy the repository to the Docker host.

2. Create private local configuration:

   ```sh
   cp .env.example .env
   ```

3. Set every value in `.env`. It contains a Home Assistant token and is ignored
   by Git.

4. Save the complete current Tesla tariff baseline here:

   ```text
   tariff/teslemetry-normal-tariff.json
   ```

5. Restrict local secrets and tariff data:

   ```sh
   chmod 600 .env tariff/teslemetry-normal-tariff.json
   ```

6. Start the controller:

   ```sh
   docker compose up --build -d
   ```

7. Watch startup and idle logs:

   ```sh
   docker compose logs -f
   ```

The controller emits an idle heartbeat every minute. It logs export progress and
the restoration reason during an active event.

### Prebuilt Images

Each GitHub release publishes a multi-architecture image for `linux/amd64` and
`linux/arm64` to GitHub Container Registry:

```text
ghcr.io/nooklabsuk/octopus-power-down-powerwall-controller:<version>
```

For example, replace `build: .` in `compose.yaml` with a pinned release image:

```yaml
image: ghcr.io/nooklabsuk/octopus-power-down-powerwall-controller:0.1.0
```

Do not use `latest` for unattended energy control. Pin a tested release version.

## Configuration

Find Home Assistant entity IDs in **Developer Tools -> States**. For a device
ID, open the Powerwall under **Settings -> Devices & services -> Devices**; the
UUID at the end of the browser URL is its Home Assistant device ID.

| Variable | Required value |
| --- | --- |
| `HA_URL` | Home Assistant URL reachable from the Docker container. |
| `HA_TOKEN` | Dedicated long-lived Home Assistant token. |
| `TESLEMETRY_DEVICE_ID` | Home Assistant device ID for the Teslemetry Powerwall device. |
| `POWER_DOWN_CALENDAR` | Calendar entity that is `on` during an active joined event. |
| `EXPORT_ENERGY_SENSOR` | Live cumulative grid-export sensor in kWh, not an instantaneous W/kW sensor. |
| `OPERATION_MODE_ENTITY` | Teslemetry Powerwall operation-mode `select` entity. |
| `EXPORT_TARGET_KWH` | Additional event export target. Use `0.85` for a 1 kWh maximum. |
| `MAX_EXPORT_SECONDS` | Hard failsafe duration. Default: 420 seconds. |
| `EXPORT_RATE_PERIOD` | Existing tariff period to temporarily alter, for example `PARTIAL_PEAK`. |
| `TEMPORARY_SELL_RATE` | Temporary sell rate. Tesla may reject it if it exceeds the matching buy rate. |

## Tariff Baseline

The controller never builds a tariff from scratch. It modifies an in-memory copy
of your complete saved baseline, then submits that baseline again to restore the
normal configuration.

Export the tariff from Teslemetry diagnostics or another trusted source. Verify
the JSON includes `energy_charges`, `seasons`, and a nested `sell_tariff`. Tesla
expects a complete valid schedule, not a partial tariff fragment.

Do not commit the tariff file. It may reveal utility and account configuration.

## How It Stops

The controller restores the normal tariff and original operation mode when:

- The configured additional export target is reached.
- The hard timeout is reached.
- The Power Down calendar event ends.
- A Home Assistant, Tesla, or controller error occurs.
- The container restarts while a session was active.
- You run the emergency restore command.

After a completed export, the controller records that event start time and will
not export a second time if the container restarts before the calendar turns off.

## Emergency Restore

To stop an active export manually:

```sh
docker compose run --rm --entrypoint python powerdown-controller /app/emergency_restore.py
```

This restores the saved tariff and original Powerwall mode from the persisted
active-session record. Keep Docker access available whenever the controller runs.

## Before Unattended Use

1. Manually test the tariff write, export, and restoration while watching Home
   Assistant and the Tesla app.
2. Confirm the export meter updates frequently enough for the selected target.
3. Confirm the calendar does not become `on` for events you did not opt into.
4. Confirm the emergency restore works.
5. Validate your actual Octopus result before relying on battery export for
   rewards.

## Tested Scope

Manually observed with Tesla Powerwall 3, Teslemetry Time-Based Control,
restoration to `self_consumption`, Myenergi cumulative export metering, and the
Octopus Energy Home Assistant Power Down calendar.

Other Powerwall models, Tesla integrations, tariff schemas, and export meters
are untested. Treat any different setup as a new manual test.

## Security

Never commit or share `.env`, `tariff/`, `state/`, Home Assistant tokens, Tesla
credentials, tariff baselines, account identifiers, device IDs, or private
network information. See [SECURITY.md](SECURITY.md) for reporting guidance.

## License

MIT licensed. See [LICENSE](LICENSE). Contributions are welcome under the rules
in [CONTRIBUTING.md](CONTRIBUTING.md).

This project was created with assistance from GPT-5.6 Terra. AI-assisted code
can be incorrect or incomplete and is not professional, electrical, financial,
or safety advice.
