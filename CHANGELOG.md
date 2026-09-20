# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Added

- Plain-language progress output with `-v` and `-vv`, and `hint:` lines on errors.
- `ProgressEvent`, `ProgressLevel`, `ProgressCallback`, `emit_progress`,
  `ReadOptions.progress`, and a `progress` argument on `MeterManager`.
- Standard-library logging in the core, MicroTech driver, and CLI, controlled by
  `--log-level` (default `error`) and `--log-file`.

### Changed

- The MicroTech "no usable records" error message is now plain language, and the
  CLI reports an incomplete read with an explanatory `retrieval is ...` line.

## [0.1.0] - 2026-09-19

### Added

- The `bgmeter-core` package with a driver-neutral meter API.
- The `bgmeter-microtech` compatibility driver for the observed BLE protocol.
- The `bgmeter-cli` command-line interface with terminal, JSON, and CSV output.
- Community, security, support, and GitHub issue-template documentation.
