"""What the proxy knows about the printer, kept on disk so a sleeping or offline printer is still described.

Some Canon printers (the MF645C among them) can fall into a deep sleep that only their sleep button ends. The proxy
keeps the printer's last full Get-Printer-Attributes reply (as the printer sent it, before repairs) and its last good
SNMP reading, answers CUPS from them while the printer does not answer, and refreshes them only from replies the
printer has just given.

Files in the state directory, each written atomically:
- printer-attributes.ipp: the last full Get-Printer-Attributes reply body
- snmp.json: the last complete SNMP reading
- state.json: when each was fetched and stored, digests of their stable parts, and when the printer was last seen

A file is rewritten only when its stable part changed (up-time, state and supply levels are ignored) or the stored copy
is older than PERSIST_AGE, so CUPS's polling does not turn into disk writes.
"""
import hashlib
import json
import logging
import os
import pathlib
import struct
import tempfile
import threading
import time

from . import ipp

log = logging.getLogger(__name__)

PERSIST_AGE = 3600  # levels on disk are at most an hour older than the last reply

VOLATILE_IPP = {
    "printer-up-time", "printer-current-time", "printer-state", "printer-state-reasons", "printer-state-message",
    "printer-state-change-time", "printer-state-change-date-time", "printer-config-change-time",
    "printer-config-change-date-time", "printer-is-accepting-jobs", "queued-job-count", "marker-levels",
    "printer-supply", "printer-alert", "printer-alert-description", "printer-supply-info-uri",
}


def ipp_stable(body):
    """Digest of a Get-Printer-Attributes reply without the values that change on every poll."""
    msg = ipp.parse(body)
    msg.request_id = 0
    for group in msg.groups:
        if group[0] == ipp.PRINTER:
            group[1] = [a for a in group[1] if a[0] not in VOLATILE_IPP]
    return hashlib.sha256(ipp.encode(msg)).hexdigest()


def snmp_stable(info):
    """Digest of an SNMP reading without levels, statuses, alerts and the page counters."""
    stable = {
        "supplies": [{k: v for k, v in s.items() if k not in ("level", "percent")} for s in info.get("supplies", [])],
        "inputs": [{k: v for k, v in t.items() if k not in ("level", "status")} for t in info.get("inputs", [])],
        "serial": info.get("serial"),
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def default_dir(printer_host):
    """systemd's StateDirectory= when set, else $XDG_STATE_HOME/canon-ipp-proxy/HOST."""
    if os.environ.get("STATE_DIRECTORY"):
        return pathlib.Path(os.environ["STATE_DIRECTORY"].split(":")[0])
    base = os.environ.get("XDG_STATE_HOME") or pathlib.Path.home() / ".local" / "state"
    return pathlib.Path(base) / "canon-ipp-proxy" / printer_host


class State:
    """Thread-safe store of the printer's last known attributes and SNMP reading; path None keeps it in memory."""

    def __init__(self, path=None):
        self.path = pathlib.Path(path) if path else None
        self.lock = threading.Lock()
        self.meta, self.ipp, self.snmp = {}, None, None
        if self.path:
            self.path.mkdir(parents=True, exist_ok=True)
            self.meta = self._load("state.json", json.loads) or {}
            self.ipp = self._load("printer-attributes.ipp", bytes)
            self.snmp = self._load("snmp.json", json.loads)
        self.online = None  # unknown until the first request

    def _load(self, name, decode):
        try:
            raw = (self.path / name).read_bytes()
            value = decode(raw)
            if name.endswith(".ipp"):
                ipp.parse(value)  # refuse a damaged copy rather than serve it
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError, IndexError, struct.error) as e:
            log.warning("state file %s ignored: %s", name, e)
            return None

    def _write(self, name, data):
        if not self.path:
            return
        fd, tmp = tempfile.mkstemp(dir=self.path, prefix=f".{name}.")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, self.path / name)
        except OSError as e:
            log.warning("state file %s not written: %s", name, e)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _save_meta(self):
        self._write("state.json", json.dumps(self.meta, indent=1, sort_keys=True).encode())

    def _remember(self, key, digest, now):
        """Update fetch time; return True when the stored copy must be rewritten."""
        entry = self.meta.setdefault(key, {})
        entry["fetched_at"] = now
        if entry.get("stable") == digest and now - entry.get("stored_at", 0) < PERSIST_AGE:
            return False
        if entry.get("stable") not in (None, digest):
            log.info("%s changed on the printer; stored copy updated", key)
        entry.update(stable=digest, stored_at=now)
        return True

    def remember_ipp(self, body, now=None):
        """Keep a full Get-Printer-Attributes reply body; return True when it was written to disk."""
        now = now or time.time()
        try:
            digest = ipp_stable(body)
        except (ValueError, IndexError, struct.error):
            return False
        with self.lock:
            self.ipp = body
            if not self._remember("ipp", digest, now):
                return False
            self._write("printer-attributes.ipp", body)
            self._save_meta()
            return True

    def remember_snmp(self, info, now=None):
        """Keep a complete SNMP reading; return True when it was written to disk."""
        now = now or time.time()
        with self.lock:
            self.snmp = info
            if not self._remember("snmp", snmp_stable(info), now):
                return False
            self._write("snmp.json", json.dumps(info, indent=1).encode())
            self._save_meta()
            return True

    def seen(self, ok, now=None):
        """Record whether the printer just answered; log and persist only when that changes."""
        now = now or time.time()
        with self.lock:
            if ok:
                self.meta["last_seen_at"] = now
            if ok == self.online:
                return
            first, self.online = self.online is None, ok
            if ok:
                self.meta.pop("offline_since", None)
                log.info("printer answers" if first else "printer answers again")
            else:
                self.meta["offline_since"] = now
                log.warning("printer does not answer (asleep or off); serving its last known attributes")
            self._save_meta()

    def fetched_at(self, key):
        return self.meta.get(key, {}).get("fetched_at")

    def summary(self):
        def when(t):
            return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)) if t else None
        with self.lock:
            return {
                "online": self.online, "state_dir": str(self.path) if self.path else None,
                "last_seen": when(self.meta.get("last_seen_at")), "offline_since": when(self.meta.get("offline_since")),
                "attributes_fetched": when(self.fetched_at("ipp")), "snmp_fetched": when(self.fetched_at("snmp")),
                "attributes_cached": self.ipp is not None,
            }
