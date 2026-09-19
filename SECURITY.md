# Security Policy

## Supported Version

Only the current `main` branch is supported.

## Reporting A Vulnerability

Do not open a public issue for a vulnerability that exposes Home Assistant,
Tesla, Octopus, or Myenergi credentials, local network details, or a way to
perform an unintended export.

Contact the repository owner privately through GitHub instead. Include a minimal
description, affected version/commit, reproduction steps, and the safety impact.
Remove tokens, tariff files, account identifiers, device IDs, and IP addresses
from all material you share.

## Credential Handling

Never include in issues, pull requests, logs, screenshots, or commits:

- Home Assistant long-lived tokens
- Tesla, Fleet API, or Teslemetry tokens
- Tesla tariff baseline JSON files
- Octopus account identifiers
- Home Assistant device/entity IDs tied to a private installation
- Public IP addresses, hostnames, or local network topology
