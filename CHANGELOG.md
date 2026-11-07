# Changelog

All notable changes to this project are documented here. The format follows Keep a Changelog
(https://keepachangelog.com/en/1.1.0/), and the project uses Semantic Versioning.

## [0.1.0] - 2026-11-07

### Added

- IPP proxy that repairs Get-Printer-Attributes replies CUPS 2.4.7+ rejects: mixed-syntax 1setOf values, missing
  `media-supported`, `media-size-supported` and `media-col-database`, padded language and charset values, duplex and
  colour support listed only in the URF string.
- Optional SNMP (Printer MIB) enrichment: full toner names, `printer-supply`, `printer-serial-number`, page counters,
  `printer-alert`, and a `GET /status` JSON reading.
- Sleep tolerance: the last full attribute reply and SNMP reading are kept on disk and served while the printer
  sleeps; other operations get `server-error-service-unavailable`, so jobs wait in the CUPS queue.
- `install` / `uninstall`: systemd user service with a CUPS queue, or a system service for the LAN with mDNS (Avahi)
  advertising and firewalld; `--dry-run`, `--purge`.
- `tools/capture.py` and `tools/scrub_capture.py` to capture another model's replies and remove personal values.
