<p align="center">
  <img src="assets/power-down-controller.svg" width="720" alt="Octopus Power Down Powerwall Controller">
</p>

<h1 align="center">Octopus Power Down Powerwall Controller</h1>

<p align="center">Capped Tesla Powerwall 3 export during active Octopus Energy Power Down events.</p>

[![CI](https://github.com/nooklabsuk/octopus-power-down-powerwall-controller/actions/workflows/ci.yml/badge.svg)](https://github.com/nooklabsuk/octopus-power-down-powerwall-controller/actions/workflows/ci.yml)
[![Security](https://github.com/nooklabsuk/octopus-power-down-powerwall-controller/actions/workflows/security.yml/badge.svg)](https://github.com/nooklabsuk/octopus-power-down-powerwall-controller/actions/workflows/security.yml)
[![Coverage](https://codecov.io/gh/nooklabsuk/octopus-power-down-powerwall-controller/branch/main/graph/badge.svg)](https://codecov.io/gh/nooklabsuk/octopus-power-down-powerwall-controller)
[![Latest release](https://img.shields.io/github/v/release/nooklabsuk/octopus-power-down-powerwall-controller?display_name=tag&sort=semver)](https://github.com/nooklabsuk/octopus-power-down-powerwall-controller/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-required-41BDF5?logo=home-assistant)](https://www.home-assistant.io/)
[![Teslemetry](https://img.shields.io/badge/Teslemetry-required-cc0000)](https://github.com/Teslemetry/hass-teslemetry)

Experimental Home Assistant controller that makes a small, capped Tesla
Powerwall 3 export during an active, opted-in Octopus Energy Power Down event.

It uses the [Teslemetry](https://github.com/Teslemetry/hass-teslemetry) Home
Assistant integration to temporarily apply a Time-Based Control tariff, polls
Myenergi current grid power directly to measure export, then restores the
original tariff and operation mode.

The controller deliberately does **not** use Myenergi Home Assistant sensor
timestamps as its export cap. Cloud-polling integrations can delay or suppress
unchanged entity state updates, which is unsafe when a Powerwall can export at
high power. Instead, the controller uses the Myenergi hub serial number and API
key to request current signed grid power itself and integrates the exporting
portion locally at the configured polling interval.

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

1. Records direct Myenergi grid power and the original Powerwall mode.
2. Persists that session state before changing Powerwall settings.
3. Applies a complete temporary Tesla tariff with a configured higher sell rate.
4. Switches the Powerwall to Tesla Time-Based Control (`autonomous`).
5. Polls direct Myenergi grid power and integrates exported energy locally until
   a capped target is reached.
6. Re-submits the saved tariff and verifies the original Powerwall mode returns.

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
- Myenergi hub serial number and API key. The controller polls current signed
  grid power directly rather than relying on Home Assistant entity timestamps.
- A dedicated Home Assistant long-lived token.

## Quick Start

1. Copy the repository to the Docker host.

2. Create private local configuration:

   ```sh
   cp .env.example .env
   ```

3. Set every value in `.env`. It contains a Home Assistant token and Myenergi
   API key and is ignored by Git.

  Get the Myenergi values from `myaccount.myenergi.com` under **Products**:
  use the hub serial number for `MYENERGI_USERNAME` and generate an API key for
  `MYENERGI_PASSWORD`. Do not reuse a copied Home Assistant integration
  configuration or publish either value.

   The controller resolves Myenergi's regional cloud endpoint from the director
   service on each direct read. It therefore continues to work if Myenergi moves
   the hub between regional API servers.

4. Start the controller:

   ```sh
   docker compose up --build -d
   ```

5. Watch startup and idle logs:

   ```sh
   docker compose logs -f
   ```

The controller emits an idle heartbeat every minute. It logs export progress and
the restoration reason during an active event. Startup logs include the running
container version, export target, timeout, polling interval, tariff name/utility,
and a read-only direct Myenergi grid-power health check. Logs never include API
keys, tokens, serial numbers, account IDs, or full entity IDs.

If Myenergi is temporarily unreachable during startup, the container stays up,
logs the full connection error, and retries after `STARTUP_RETRY_SECONDS`.
It does not monitor or act on Power Down events until a direct Myenergi health
check succeeds.

### Prebuilt Images

Each semantic GitHub release publishes a multi-architecture image for
`linux/amd64` and `linux/arm64` to GitHub Container Registry:

```text
ghcr.io/nooklabsuk/octopus-power-down-powerwall-controller:<version>
```

The included `compose.yaml` builds the checked-out source by default. To use a
reviewed prebuilt release instead, replace its `build: .` line with:

```yaml
image: ghcr.io/nooklabsuk/octopus-power-down-powerwall-controller:${POWERDOWN_CONTROLLER_VERSION}
```

Then set the selected pinned release in your ignored `.env` file:

```dotenv
POWERDOWN_CONTROLLER_VERSION=vX.Y.Z
```

Do not use `latest` for unattended energy control. Pin a tested release version.
The repository does not chase release tags in `compose.yaml`; update this local
variable deliberately after reviewing each release. Copy the exact `vX.Y.Z` tag
from the [GitHub Releases page](https://github.com/nooklabsuk/octopus-power-down-powerwall-controller/releases).

## Configuration

Find Home Assistant entity IDs in **Developer Tools -> States**. For a device
ID, open the Powerwall under **Settings -> Devices & services -> Devices**; the
UUID at the end of the browser URL is its Home Assistant device ID.

| Variable | Required value |
| --- | --- |
| `HA_URL` | Home Assistant URL reachable from the Docker container. |
| `HA_TOKEN` | Dedicated long-lived Home Assistant token. |
| `TESLEMETRY_CONFIG_ENTRY_ID` | Config-entry ID for the Teslemetry integration. Find it in the browser URL after opening **Settings -> Devices & services -> Teslemetry**. Used to snapshot the live tariff before an event. |
| `TESLEMETRY_DEVICE_ID` | Home Assistant device ID for the Teslemetry Powerwall device. |
| `POWER_DOWN_CALENDAR` | Calendar entity that is `on` during an active joined event. |
| `OPERATION_MODE_ENTITY` | Teslemetry Powerwall operation-mode `select` entity. |
| `MYENERGI_USERNAME` | Myenergi hub serial number. |
| `MYENERGI_PASSWORD` | Myenergi API key generated in the Myenergi account portal. It is used for direct current-power polling by this container. |
| `MYENERGI_EXPORT_SIGN` | Signed-power export convention. Normally `-1`: Myenergi grid power is negative while exporting. |
| `MYENERGI_POLL_SECONDS` | Direct Myenergi cloud polling interval. Default: `5`; lower values increase cloud API use. |
| `STARTUP_RETRY_SECONDS` | Retry delay after a failed startup Myenergi health check. Default: `30`. |
| `EXPORT_TARGET_KWH` | Additional event export target. Use `0.85` for a 1 kWh maximum. |
| `MAX_EXPORT_SECONDS` | Hard failsafe duration. Default: 420 seconds. |
| `EXPORT_RATE_PERIOD` | Existing tariff period to temporarily alter, for example `PARTIAL_PEAK`. |
| `TEMPORARY_SELL_RATE` | Temporary sell rate. Tesla may reject it if it exceeds the matching buy rate. |

## Live Tariff Snapshot

No manual tariff file is required. Immediately before each event, the controller
reads the complete live `tariff_content_v2` from Teslemetry diagnostics, persists
that snapshot in its active-session file, and modifies an in-memory copy for the
temporary export tariff. It restores the exact saved snapshot on every stop path.

If Teslemetry cannot provide one complete live tariff with a nested `sell_tariff`,
the controller fails closed and does not change the Powerwall.

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
