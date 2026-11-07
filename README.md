# canon-ipp-proxy

Status: alpha, tested on one model (see Limits).

Some Canon laser printers (seen on the i-SENSYS / imageCLASS MF640C series with firmware 13.01) send an IPP
Get-Printer-Attributes reply that CUPS 2.4.7 and later reject as a whole, so adding the printer as a driverless (IPP
Everywhere) printer fails with:

```
lpadmin: Unable to create PPD: No IPP attributes.
```

The printer itself is fine: it prints PDF and PWG raster over IPP, and phones print to it with AirPrint. The reply
breaks two rules:

- `media-col-supported` is a set of keywords with one collection value (the custom media-size range) inside it. An
  IPP 1setOf must not mix syntaxes; libcups stops reading the reply there.
- `media-col-database`, `media-supported` and `media-size-supported` are missing.

`canon-ipp-proxy` sits between CUPS and the printer. It forwards every IPP request unchanged and repairs only
Get-Printer-Attributes replies, then CUPS sets the printer up as a normal IPP Everywhere queue. Nothing on the printer
changes. Python 3.10+, standard library only.

## How it works, and what it touches

- Every request, print jobs included, goes to the printer unchanged, and every reply except Get-Printer-Attributes
  comes back unchanged. While the printer does not answer, the proxy replies itself (see sleep below).
- It reads the printer (IPP, and SNMP if enabled) and never writes settings to it.
- Without `--system` it listens on 127.0.0.1 only. With `--system` it listens on the LAN with no authentication,
  like the printer itself.

## What it repairs

| defect in the reply | repair |
| -- | -- |
| a 1setOf that mixes collections with other syntaxes | keeps the majority syntax; a dropped custom-size range moves to `media-size-supported` |
| no `media-supported` / `media-size-supported` | built from `media-ready` plus common ISO and US sizes, and the printer's custom range |
| no `media-col-database` | one entry per size, with the printer's own `media-*-margin-supported` (5 mm if absent) |
| padded `naturalLanguage` / `charset` values (`en-  `) | trimmed |
| two-sided or colour printing in the URF string but not in `sides-supported` / `print-color-mode-supported` | added |

A narrow request (CUPS's status polls) gets nothing added. A reply that can't be parsed is passed through untouched.

Optional SNMP (Printer MIB, v1 by default) adds what the IPP reply lacks: full toner names, supplies, serial number,
page counters and alerts, plus a JSON reading on `GET /status`. It is read in the background after the printer has
answered over IPP, at most every 60 s, and never delays a request.

The MF645C falls into a deep sleep that only its sleep button ends. The proxy keeps the printer's last full attribute
reply and SNMP reading in a state directory (systemd `StateDirectory=`, else `$XDG_STATE_HOME/canon-ipp-proxy/HOST`).
While the printer does not answer, CUPS gets those attributes, marked `offline-report` with "press its sleep button",
and every other operation gets `server-error-service-unavailable`, so jobs wait in the queue and print once the printer
is awake. The files are rewritten only when the printer's stable attributes change, or once an hour.

## Use

```sh
pipx install git+https://github.com/hannut/canon-ipp-proxy   # or: uv tool install git+https://...
canon-ipp-proxy install ipp://PRINTER/ipp/print
```

That checks the printer answers, runs the proxy as a systemd user service (one instance per printer, the first free
port from 8631), starts it at boot, and adds a CUPS queue through it named after the printer (`Canon_MF645C`, shown
as "Canon MF645C"). Adding the queue needs a CUPS admin group (`wheel` on Fedora, `lpadmin` on Debian); `--no-queue`
skips it. Rerunning `install` repairs or changes an instance; `canon-ipp-proxy uninstall NAME` removes exactly what
install created, and `--purge` also deletes the kept printer state. `--dry-run` shows the steps.

To serve every machine on the LAN instead, install a system service on an always-on Linux machine:

```sh
canon-ipp-proxy install ipp://PRINTER/ipp/print --system            # asks for sudo
canon-ipp-proxy install ipp://PRINTER/ipp/print --host user@machine # copies itself there over ssh
```

`--system` is tested in a Fedora 44 container; `--host` is not yet tested on a real machine.

It listens on all addresses, opens the port in firewalld when that runs, runs as a `DynamicUser=` with systemd's
sandbox, and advertises itself over mDNS (Avahi) as a driverless printer, so clients find it with no setup.
`printer-uri-supported` is rewritten to the address each client used, so jobs go through the proxy.

Where things go (nothing under `/usr`, so it works the same on Fedora Atomic / Bluefin as on a classic system):

| what | user service | system service |
| -- | -- | -- |
| program | the `canon-ipp-proxy` on PATH, else a copy in `~/.local/bin` | `/usr/local/bin/canon-ipp-proxy` |
| unit | `$XDG_DATA_HOME/systemd/user/canon-ipp-proxy@.service` | `/etc/systemd/system/canon-ipp-proxy@.service` |
| settings, install manifest | `$XDG_CONFIG_HOME/canon-ipp-proxy/NAME.env`, `NAME.installed.json` | `/etc/canon-ipp-proxy/` |
| state | `$XDG_STATE_HOME/canon-ipp-proxy/NAME` | `/var/lib/canon-ipp-proxy/NAME` |
| mDNS | none | `/etc/avahi/services/canon-ipp-proxy-NAME.service` |

Without installing, the proxy runs in the foreground: `canon-ipp-proxy ipp://PRINTER/ipp/print` (listens on
127.0.0.1:8631). Options: `--listen ADDR:PORT`, `--state-dir DIR`, `--snmp-version 1|2c|off`, `--snmp-community`,
`--snmp-ttl`.

## Limits

- `ipp://` to the printer only (no `ipps://` yet).
- The system service needs systemd's sandbox; an unprivileged container without nesting refuses it (install says so).
- Tested on one printer model (MF645C, firmware 13.01) with CUPS 2.4.19. Reports from other models are welcome.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

The fixtures are a real Get-Printer-Attributes reply and SNMP walk from the tested printer, with its addresses, MAC,
UUID, location and serial number replaced by placeholders.

## Reporting another model

A capture from another model helps most. From a checkout of this repository, on the printer's network:

```sh
python3 tools/capture.py PRINTER_ADDRESS capture/
```

It sends one Get-Printer-Attributes request and walks the SNMP subtrees the proxy reads, keeps the raw captures in
`capture/` (private: they hold the printer's address, serial and location), and writes scrubbed copies
(`MODEL-get-printer-attributes.raw`, `MODEL-snmp-walk.tsv`) with personal values replaced; it stops if any are left.
Attach the scrubbed copies to an issue.

## Licence

Apache License 2.0 (LICENSE), the same as CUPS.

Not affiliated with or endorsed by Canon Inc. Canon, i-SENSYS and imageCLASS are trademarks of Canon Inc.

Developed with AI assistance (Claude).
