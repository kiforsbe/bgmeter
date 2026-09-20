# Knowledge Base: GoChek Blood Glucose Tester

Comprehensive hardware, Bluetooth Low Energy (BLE) GATT profile, brand attribution, protocol documentation, and value interpretation for the **GoChek - Z00MCF** blood glucose tester.

The device identifiers, measurements, and protocol evidence in this document
are owner-authorized public material. `Z00MCF` is this owner's device serial
suffix as observed in its Bluetooth Device Name (`GoChek - Z00MCF`); it is not
a brand or model identifier. These observations describe one device and do not
claim vendor endorsement, certification, or universal compatibility. Brand
names remain the property of their respective owners.

---

## 1. Device Identification & Brand Hierarchy

| Parameter | Value / Description |
| :--- | :--- |
| **Bluetooth Device Name** | `GoChek - Z00MCF` (with owner-authorized serial suffix `Z00MCF`) |
| **MAC Address** | `00:15:93:00:6B:73` |
| **Retail Model** | Wellion NEWTON GDH-FAD BTE |
| **Brand Owner / Distributor** | MED TRUST, Austria |
| **Actual OEM Manufacturer** | MicroTech Medical |
| **Hardware Family** | GoChek / GoChek Connect |
| **Device Category** | Blood Glucose Tester (Bluetooth Smart / BTE) |

---

## 2. Hardware & Chipset Profile

- **Bluetooth System-on-Chip (SoC) Vendor**: **Texas Instruments Inc.**
  - **PnP ID (`0x2A50`)**: `01 0d 00 00 00 10 01`
    - Vendor ID Source: `0x01` (Bluetooth SIG assigned)
    - Vendor ID: `0x000D` (Texas Instruments Inc.)
    - Product ID: `0x0000`
    - Product Version: `0x0110` (v1.1.0)
- **System ID (`0x2A23`)**: `73 6b 00 00 00 93 15 00`
  - Encodes the Bluetooth MAC address (`00:15:93:00:6B:73`) in little-endian order with padding bytes (`00 00`).

---

## 3. GATT Services & Characteristics Architecture

### 3.1 Generic Access Profile (`0x1800`)

| UUID | Attribute Name | Properties | Value (Hex) | Decoded & Interpreted Representation |
| :--- | :--- | :--- | :--- | :--- |
| `00002a00-0000-1000-8000-00805f9b34fb` | Device Name | Read | `47 6f 43 68 65 6b 20 2d 20 5a 30 30 4d 43 46` | Bluetooth Device Name: `'GoChek - Z00MCF'` |
| `00002a01-0000-1000-8000-00805f9b34fb` | Appearance | Read | `00 00` | Device Category: `0x0000` (Generic Category) |
| `00002a02-0000-1000-8000-00805f9b34fb` | Peripheral Privacy Flag | Read, Write | `00` | Disabled |
| `00002a03-0000-1000-8000-00805f9b34fb` | Reconnection Address | Write | — | — |
| `00002a04-0000-1000-8000-00805f9b34fb` | Connection Parameters | Read | `50 00 a0 00 00 00 e8 03` | Min Interval=100.0ms, Max=200.0ms, Latency=0, Supervision Timeout=10000ms |

---

### 3.2 Generic Attribute Profile (`0x1801`)

| UUID | Attribute Name | Properties | Descriptors |
| :--- | :--- | :--- | :--- |
| `00002a05-0000-1000-8000-00805f9b34fb` | Service Changed | Indicate | CCCD (`0x2902`, Handle 15) |

---

### 3.3 Device Information Service (`0x180A`)

| UUID | Attribute Name | Properties | Raw Hex Output | Decoded Interpretation |
| :--- | :--- | :--- | :--- | :--- |
| `00002a23-0000-1000-8000-00805f9b34fb` | System ID | Read | `73 6b 00 00 00 93 15 00` | System Identifier (Mapped MAC `00:15:93:00:6B:73`) |
| `00002a24-0000-1000-8000-00805f9b34fb` | Model Number String | Read | `4d 6f 64 65 6c 20 4e 75 6d 62 65 72 00` | `'Model Number'` |
| `00002a25-0000-1000-8000-00805f9b34fb` | Serial Number String | Read | `53 65 72 69 61 6c 20 4e 75 6d 62 65 72 00` | `'Serial Number'` |
| `00002a26-0000-1000-8000-00805f9b34fb` | Firmware Revision | Read | `46 69 72 6d 77 61 72 65 20 52 65 76 69 73 69 6f 6e 00` | `'Firmware Revision'` |
| `00002a27-0000-1000-8000-00805f9b34fb` | Hardware Revision | Read | `48 61 72 64 77 61 72 65 20 52 65 76 69 73 69 6f 6e 00` | `'Hardware Revision'` |
| `00002a28-0000-1000-8000-00805f9b34fb` | Software Revision | Read | `53 6f 66 74 77 61 72 65 20 52 65 76 69 73 69 6f 6e 00` | `'Software Revision'` |
| `00002a29-0000-1000-8000-00805f9b34fb` | Manufacturer Name | Read | `4d 61 6e 75 66 61 63 74 75 72 65 72 20 4e 61 6d 65 00` | `'Manufacturer Name'` |
| `00002a2a-0000-1000-8000-00805f9b34fb` | IEEE 11073 Cert Data | Read | `fe 00 65 78 70 65 72 69 6d 65 6e 74 61 6c` | Regulatory Certification: `'experimental'` |
| `00002a50-0000-1000-8000-00805f9b34fb` | PnP ID | Read | `01 0d 00 00 00 10 01` | Vendor Source=SIG, Vendor=Texas Instruments Inc., Product=0x0000, Ver=1.16.0 |

---

### 3.4 Custom Vendor Service (`0000ffe0-0000-1000-8000-00805f9b34fb`)

- **Service UUID**: `0000ffe0-0000-1000-8000-00805f9b34fb`
- **Data Characteristic UUID**: `0000ffe1-0000-1000-8000-00805f9b34fb`
  - **Properties**: `Read`, `Notify`, `Write-Without-Response`, `Write`
  - **Descriptors**:
    - `00002902-0000-1000-8000-00805f9b34fb` (Client Characteristic Configuration Descriptor - CCCD, Handle 38)
    - `00002901-0000-1000-8000-00805f9b34fb` (Characteristic User Description, Handle 39)
  - **Read State**: `0x01` -> Vendor Channel State: Data Channel Ready / Idle

---

## 4. Blood Glucose Value Interpretation & Clinical Standard

Blood glucose values read back from meter payloads are converted between **mg/dL** and **mmol/L** ($1\text{ mmol/L} = 18.018\text{ mg/dL}$) and categorized according to **ADA / ISPAD Clinical Standards**:

| Concentration (mg/dL) | Concentration (mmol/L) | Category | Clinical Interpretation & Assessment |
| :--- | :--- | :--- | :--- |
| **< 70.0** | **< 3.90** | **HYPOGLYCEMIA (LOW)** | **CRITICAL ALERT**: Blood glucose is below target range (< 70 mg/dL). Risk of severe low blood sugar. |
| **70.0 - 99.0** | **3.90 - 5.50** | **NORMAL (FASTING)** | Optimal blood glucose level for fasting state (70-99 mg/dL). |
| **100.0 - 125.0** | **5.55 - 6.94** | **ELEVATED / PRE-PRANDIAL** | Slightly elevated fasting blood glucose level (100-125 mg/dL). |
| **126.0 - 139.0** | **6.99 - 7.71** | **NORMAL POST-PRANDIAL** | Normal blood glucose level within 2 hours after a meal (126-139 mg/dL). |
| **140.0 - 199.0** | **7.77 - 11.04** | **HYPERGLYCEMIA (HIGH)** | Elevated blood sugar level (> 140 mg/dL / 7.8 mmol/L). |
| **>= 200.0** | **>= 11.10** | **SEVERE HYPERGLYCEMIA (VERY HIGH)** | **CRITICAL ALERT**: Blood glucose is severely high (>= 200 mg/dL / 11.1 mmol/L). |

---

## 5. Verified MicroTech transport protocol

These details describe the transport behavior supported by the MicroTech driver
and confirmed with saved device captures.

- A history request sends command `0x05`, message type `0x02`, acknowledgement
  byte `0x01`, and a two-byte **big-endian** record index.
- The inner header is `01 <command> 00 00`, followed by CRC-16/Modbus in
  little-endian order, then the message-type/acknowledgement bytes and payload.
- The transport adds a six-byte outer header and Dallas/Maxim CRC-8. Frames are
  delimited by `2d 2d`; literal `2d` and `2f` body bytes are prefixed by `2f`.
- Record 1, sequence 0 is exactly:
  `2d 2d 00 00 10 00 80 c6 01 05 00 00 45 7f 02 01 00 01 2d 2d`
- Responses may be fragmented. Outer byte 3 is the payload byte offset and bit
  7 of outer byte 4 marks the final fragment. The observed malformed-request
  response used offsets `0`, `10`, and `20`, confirming byte-offset assembly.
- A notification carries at most 20 bytes, but escaping can make a frame longer:
  a record containing a literal `2d` or `2f` byte (for example a measurement at
  47 seconds, `2f`) turns a 20-byte frame into 21 bytes. The meter then sends
  the frame in two notifications, the second holding a single trailing `2d`.
  Frames therefore have to be reassembled across notifications, using the `2d 2d`
  delimiters and the declared length in the transport header.
- FFE1 supports notifications and write-without-response. Frames longer than
  20 bytes are written as 20 bytes, a 100 ms delay, then the remainder and
  another 100 ms delay.

### BGM history record (18 bytes)

The driver decodes this exact big-endian layout:

| Offset | Size | Field |
| :--- | :--- | :--- |
| 0 | 1 | Year minus 2000 |
| 1..5 | 5 | Month, day, hour, minute, second |
| 6 | 1 | Temperature |
| 7 | 1 | Flags (hypo, hyper, ketone, pre/post meal, invalid, control solution) |
| 8 | 2 | Blood glucose value in mg/dL |
| 10 | 2 | Reserved |
| 12 | 2 | Event index (low 15 bits) |
| 14..17 | 4 | Event port, type, level, value |

A displayed `7.4 mmol/L` record is therefore expected near `133 mg/dL`
(`00 85`).

---

## 6. Value-field validation (7.4 mmol/L / 133.3 mg/dL)

The `7.4 mmol/L` measurement validates the decoded value field. It corresponds
to approximately `133 mg/dL` and the integer `133` (`00 85`). Production code
must collect all unique records and must not contain target-value matching or
special handling for this measurement.

---

## 7. Expected records to verify

The following records are known to be stored on the meter and must be checked
against future extraction results:

| Date and time | Displayed value | Expected native value (approximately) | Verified |
| :--- | :--- | :--- | :---: |
| 2026-09-16 13:43 | 5.4 mmol/L | 97 mg/dL (`00 61`) | No |
| 2026-09-16 19:10 | 7.4 mmol/L | 133 mg/dL (`00 85`) | No |
| 2026-09-17 06:25 | 5.8 mmol/L | 105 mg/dL (`00 69`) | No |
| 2026-09-17 17:27 | 5.7 mmol/L | 103 mg/dL (`00 67`) | No |

The timestamp and glucose value must both match before a record is marked as
verified. The mg/dL values are rounded conversion targets; the displayed
mmol/L values above are authoritative.

---

## 8. Implemented reusable library architecture

The implemented architecture is maintained in
[`ARCHITECTURE.md`](ARCHITECTURE.md), with its original approved specification
under `docs/superpowers/specs/` and development instructions in
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md). The generic core, MicroTech
driver, and CLI are separate Python 3.13 source bases and distributions.

### Generic and device-specific boundaries

- BLE/GATT and the Device Information Service are generic standards.
- The observed FFE0/FFE1 channel, frame format, command `0x05`, indexed history
  retrieval, checksums, fragmentation, and 18-byte records are proprietary to
  the MicroTech meter family. They may be reused by GoChek/Wellion rebrands but
  must not be assumed to work with unrelated meters.
- The concrete driver is `MicroTechBgmDriver`. GoChek and Wellion
  product names are compatibility identities, not the base abstraction.
- Other protocols, including the Bluetooth SIG Glucose Service, require
  separate `MeterDriver` implementations.

### Approved component split

- An async, transport-neutral high-level API discovers meters, connects to a
  selected meter, and retrieves records.
- The accepted public API uses `MeterManager` for discovery and opens a selected
  meter as an async context manager. One-shot discovery/read helpers provide a
  simpler facade while retaining the same behavior.
- A built-in driver registry matches discovered transport endpoints to meter
  drivers. Candidate matching is side-effect-free; an optional connected probe
  confirms identity. Ambiguous equal-confidence matches are reported rather
  than silently resolved. Drivers own protocol-specific identification and
  parsing but do not control transport lifecycle.
- `BleTransport` owns scanning and GATT byte transfer without interpreting
  glucose data.
- Vendor-neutral models represent devices, glucose records, retrieval results,
  and raw protocol captures.
- Raw record bytes and relevant response fragments stay attached to each
  normalized record for later re-parsing.
- A normalized record has a stable driver-scoped ID, optional native index,
  precise mmol/L and native values, all available context/status flags,
  device/driver identity, namespaced vendor metadata, and raw request/response
  evidence. A read result adds completeness, warnings, collection times, and
  protocol diagnostics. Results contain unique records sorted by measurement
  time regardless of their arrival order.
- The MicroTech record contains a manually set local wall-clock time with no
  timezone or synchronization metadata. The library and CLI initially assume
  that clock is correct. They preserve the verbatim timezone-unspecified meter
  time and also provide local timezone-aware and UTC timestamps. Conversion
  uses a caller-selected timezone, defaulting to the host system timezone, and
  records the timezone name and UTC offset used.
- A separate `bgmeter` CLI supports interactive selection when appropriate and
  requires `--device` for ambiguous non-interactive use.
- The generic core, MicroTech driver, and CLI are independent Python source
  bases, each with its own packaging metadata, tests, and fixtures. They live
  in this repository for now but must remain ready to move into separate
  repositories unchanged.
- The core publicly exports `MeterDriver` and all implementation-facing types.
  Driver packages declare an API version and register a factory through a
  documented Python package entry point. The MicroTech driver uses exactly this
  mechanism and is not special-cased by the core.
- The core also exports a programmatic `DriverRegistry` that can register,
  unregister, enumerate, and look up driver factories without global state.
- The CLI declares normal versioned dependencies, imports only the core's
  public API, and discovers installed compatible drivers through the core
  registry. This repository's default CLI installation includes the MicroTech
  driver to provide GoChek/Wellion support.
- The CLI persistently records enabled installed driver entry points and
  provides `bgmeter drivers list`, `info`, `register`, and `unregister`.
  Registration validates an already-installed package; it does not install or
  download executable code. Later discovery and reads query every registered
  driver unless explicitly restricted.
- Exporters belong to the CLI source base and operate on normalized public
  library results. Initial formats are terminal, CSV, and versioned JSON;
  healthcare-specific formats such as FHIR are deferred.
- Versioned JSON is the canonical lossless export and includes normalized data,
  completion metadata, warnings, diagnostics, and hexadecimal raw evidence.
  Decimal glucose values are strings to avoid floating-point alteration. CSV
  has one record per row with stable scalar columns and compact JSON cells for
  nested fields; terminal output is intentionally not a machine-readable
  contract.
- The accepted CLI commands are `bgmeter devices`, `bgmeter info`, and
  `bgmeter read`. `read` accepts repeated outputs, defaults to terminal output,
  keeps diagnostics on stderr, protects existing files unless forced, and
  returns a nonzero status for explicitly marked partial retrievals.
- Retrieval completeness is explicitly `complete`, `partial`, or `unknown`.
  Valid records survive a later failure in a partial result; duplicate events
  are counted but omitted. Typed errors distinguish discovery, selection,
  connection, timeout, protocol, and export failures.

The root `gocheck.py` is only a deprecated launcher for `bgmeter`; it contains
no protocol implementation. Normal library and CLI operation discovers
supported devices and never assumes the user's single meter is the source.
