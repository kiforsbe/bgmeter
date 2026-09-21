# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Added

- `bgmeter read --newest N` reads only the N most recent records.
- `bgmeter read --new-only` stops at the first record already in the local
  measurement database, making a repeated sync nearly free.
- `CompletionStatus.TRUNCATED` distinguishes a deliberately shortened read from
  a degraded one. `ReadOptions` gains `newest_count` and `known_record_ids`,
  both optional hints a driver may honor.

### Changed

- The MicroTech driver now walks history from the newest record backwards. A read
  cut short by an unresponsive meter therefore keeps the newest records rather
  than the oldest, and raw wire evidence is now ordered newest-first.

## [0.2.0] - 2026-09-20

### Added

- Plain-language progress output with `-v` and `-vv`, and `hint:` lines on errors.
- `ProgressEvent`, `ProgressLevel`, `ProgressCallback`, `emit_progress`,
  `ReadOptions.progress`, and a `progress` argument on `MeterManager`.
- Standard-library logging in the core, MicroTech driver, and CLI, controlled by
  `--log-level` (default `error`) and `--log-file`.

### Fixed

- MicroTech reads no longer fail when a reply frame is split across two BLE
  notifications. Escaping a literal `2d` or `2f` byte in a record (for example a
  measurement taken at 47 seconds) pushes a frame from 20 to 21 bytes, which the
  meter sends as 20 bytes plus a lone trailing byte; the driver now reassembles
  frames across notifications.

### Changed

- The MicroTech "no usable records" error message is now plain language, and the
  CLI reports an incomplete read with an explanatory `retrieval is ...` line.

## [0.1.0] - 2026-09-19

### Added

- The `bgmeter-core` package with a driver-neutral meter API.
- The `bgmeter-microtech` compatibility driver for the observed BLE protocol.
- The `bgmeter-cli` command-line interface with terminal, JSON, and CSV output.
- Community, security, support, and GitHub issue-template documentation.
