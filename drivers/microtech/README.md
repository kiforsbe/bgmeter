# bgmeter-microtech

`bgmeter-microtech` is a community-maintained compatibility driver for an
observed MicroTech Medical BLE protocol used by GoChek and Wellion-family blood
glucose meters. It implements the public `bgmeter` driver API and is discovered
through the `bgmeter.drivers` entry-point group as `microtech`.

## Scope and safety

This driver is not affiliated with or endorsed by GoChek, Wellion, MicroTech
Medical, or their owners. Brand names identify observed compatibility only and
remain the property of their respective owners. Compatibility is not a claim of
vendor support, certification, or universal device coverage.

This software is not medical advice or a certified medical device. Do not make
treatment decisions from its output. Meter data and `RawCapture` values may
contain sensitive health or device information; do not post them in public
issues or repositories.

## Supported identity evidence

The driver recognizes BLE advertisements containing the vendor service FFE0 or
names containing `GoChek` or `Wellion`. A connected probe then requires
characteristic FFE1 to belong to service FFE0. Device addresses are deliberately
not used as identity evidence. Known product and manufacturer strings include:

- GoChek and GoChek Connect
- Wellion NEWTON
- MicroTech Medical

Device Information Service fields are returned when the meter exposes them.
Support is based on the captured FFE0/FFE1 protocol, not on a claim that every
product carrying one of these names uses the same protocol.

## Install and use

Install the core and this package, then use only the public core library:

```powershell
python -m pip install -e core
python -m pip install -e "drivers/microtech[test]"
```

```python
import asyncio

from bgmeter import MeterManager, ReadOptions


async def main() -> None:
    manager = MeterManager.default()
    devices = await manager.discover()
    result = await manager.read(
        devices[0],
        ReadOptions(timezone="Europe/Stockholm"),
    )
    for record in result.records:
        print(record.mmol_l, record.measured_at.measured_at_utc)


asyncio.run(main())
```

The driver preserves the meter's timezone-unspecified wall clock, interprets it
using the requested IANA timezone, and also returns UTC. Glucose normalization
uses `Decimal`, while `RawCapture` retains attributable request bytes plus every
captured fragment, reconstructed response, and native record byte. Treat this
raw material as sensitive. A request is left `None` when an offline capture
cannot reliably associate one with a record.

## Progress and logging

The driver reports plain-language progress through `ReadOptions.progress`
("Reading records: 12 of 57", retries, and, at detail level, replies it ignored
and why). It logs technical detail under `bgmeter_microtech.protocol`,
`.collector`, and `.driver`: DEBUG has every request, every notification as hex,
and the accept or reject verdict for each reply; INFO has counts only, never
glucose values or serial numbers; WARNING summarizes exhausted retries and
rejected replies. The package installs only a `NullHandler`. When a read returns
no usable record, the `bgmeter_microtech.protocol` log (`--log-level warning`
or lower on the CLI) shows what the meter sent and why each reply was rejected.

## Protocol and capture boundary

Framing, checksums, command `0x05`, indexed history retrieval, and the native
18-byte record layout are compatibility details contained entirely in this
package. The core package has no meter-specific knowledge. The regression
fixture at `tests/fixtures/2026-09-17-1700.txt` is owner-authorized public test
material; normal tests replay it and require no meter or Bluetooth hardware.

Run the package tests with:

```powershell
python -m pytest drivers/microtech/tests -q
```

## Opt-in hardware smoke test

The hardware test is skipped unless explicitly enabled. Wake a supported meter,
ensure Bluetooth is available, and run:

```powershell
$env:BGMETER_HARDWARE = "1"
python -m pytest drivers/microtech/tests/hardware/test_live_meter.py -q -s
```

The live test performs discovery, connected probing, and history retrieval. It
does not match or embed a fixed device address.
