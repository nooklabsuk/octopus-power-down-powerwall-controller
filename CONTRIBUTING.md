# Contributing

Contributions are welcome, particularly support for safer validation, additional
export meters, and broader tariff structures.

Before opening a pull request:

1. Do not include credentials, tariff baselines, account IDs, device IDs, or
   private network details.
2. Keep control changes fail-closed: an API error, restart, missing meter, or
   invalid response must restore or avoid changing the Powerwall.
3. Explain the tested hardware, firmware, Home Assistant version, Teslemetry
   version, and export-meter integration.
4. Include tests where practical, or document why a behaviour needs a manual
   observed test.
5. Do not change a tariff payload using a partial update; Tesla expects a
   complete valid tariff object.

By contributing, you agree that your contribution is licensed under the MIT
License in this repository.
