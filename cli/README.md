# bgmeter-cli

`bgmeter-cli` is the driver-neutral command line client for `bgmeter-core`.
The default installation includes the separately packaged MicroTech driver,
which is discovered through the public `bgmeter.drivers` entry-point group.

```console
bgmeter devices
bgmeter info --device ble:AA-BB-CC-DD-EE-FF
bgmeter read --device ble:AA-BB-CC-DD-EE-FF --timezone Europe/Stockholm
bgmeter read --device ble:AA-BB-CC-DD-EE-FF \
  --output terminal --output csv=records.csv --output json=records.json
bgmeter drivers list --registered
```

Driver registration only enables or disables an already installed entry point;
it never downloads or uninstalls packages. Configuration is stored as versioned
JSON in the platform user configuration directory for `bgmeter`.

The JSON read result is the canonical lossless format. CSV has a stable row per
record, while terminal output is intended for people and is not a stable data
contract. Data is written to stdout or requested files; errors and selection
prompts are written to stderr.

Exit statuses are `0` for a complete result, `2` for usage or ambiguous
selection, `3` for no supported meter, `4` for connection or transport failure,
`5` for protocol failure or an incomplete retrieval, `6` for export failure,
and `130` for interruption.
