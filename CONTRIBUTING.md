# Contributing

Thanks for contributing to `bgmeter`.

## Non-medical use

This project is technical interoperability software, not medical advice or a
certified medical device. Contributions must not present it as suitable for
diagnosis, treatment decisions, certification, or vendor-supported use.

## Before opening a pull request

- Use Python 3.13 or newer.
- Run `python -m pytest -q`.
- Add focused tests for behavior changes.
- Keep the core package driver-neutral and preserve the public driver API.

## Privacy and compatibility

Do not commit health measurements, personal data, unique device identifiers,
serial numbers, databases, exports, raw BLE captures, APKs, or vendor binaries.
Use synthetic fixtures and redact diagnostics in issues and pull requests.

Do not claim vendor endorsement, certification, or compatibility beyond what
the tests demonstrate. Brand names may be used only where needed to identify
compatibility.

## Scope

Keep pull requests small and explain any user-visible behavior or documentation
change. The project is MIT-licensed. Submit only work you created or have the
right to contribute, including its dependencies and assets, and ensure it can
be distributed under the repository's MIT License. By contributing, you agree
that your contribution is available under that license.
