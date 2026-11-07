"""Printer details from SNMP (Printer MIB, RFC 3805) that the IPP reply leaves out.

Added only when the reply lacks them or SNMP knows more: markers, input trays, full marker-names, printer-supply,
printer-serial-number, page counters (PWG 5100.22; Canon's counter table, else the Printer MIB life count) and
printer-alert (PWG 5100.9). The reading is cached for ttl seconds and served as JSON on GET /status.
"""
import logging
import re
import threading
import time

from . import ipp
from .snmp import Client
from .state import State

log = logging.getLogger(__name__)

SUPPLIES = "1.3.6.1.2.1.43.11.1.1"
COLORANTS = "1.3.6.1.2.1.43.12.1.1"
INPUTS = "1.3.6.1.2.1.43.8.2.1"
LIFECOUNT = "1.3.6.1.2.1.43.10.2.1.4"
ALERTS = "1.3.6.1.2.1.43.18.1.1"
LIFEUNIT = "1.3.6.1.2.1.43.10.2.1.3.1.1"
SERIAL = "1.3.6.1.2.1.43.5.1.1.17.1"
# Canon counter table (value column; index = Canon counter number), as seen on the MF645C.
CANON_COUNTER = "1.3.6.1.4.1.1602.1.11.1.4.1.4"
CANON_COUNTERS = {"total": 101, "single-large": 110, "single-small": 111, "black-large": 112, "black-small": 113,
                  "color-large": 122, "color-small": 123}  # color-* = Full Color + Single Color
IMPRESSIONS = 7  # PrtMarkerCounterUnitTC

# PrtMarkerSuppliesTypeTC (RFC 3805) -> IPP marker-types keyword
SUPPLY_TYPES = {
    3: "toner", 4: "waste-toner", 5: "ink", 6: "ink-cartridge", 7: "ink-ribbon", 8: "waste-ink", 9: "opc",
    10: "developer", 11: "fuser-oil", 12: "solid-wax", 13: "ribbon-wax", 14: "waste-wax", 15: "fuser",
    16: "corona-wire", 17: "fuser-oil-wick", 18: "cleaner-unit", 19: "fuser-cleaning-pad", 20: "transfer-unit",
    21: "toner-cartridge", 22: "fuser-oiler", 23: "water", 24: "waste-water", 27: "binding-supply",
    28: "banding-supply", 29: "stitching-wire", 31: "paper-wrap", 32: "staples", 33: "inserts", 34: "covers",
}
SUPPLY_TYPE_PWG = {  # PWG 5100.13 printer-supply type= values
    3: "toner", 4: "wasteToner", 5: "ink", 6: "inkCartridge", 9: "opc", 10: "developer", 15: "fuser",
    20: "transferUnit", 21: "tonerCartridge",
}
SUPPLY_CLASS = {3: "supplyThatIsConsumed", 4: "receptacleThatIsFilled"}
SUPPLY_UNIT = {19: "percent", 7: "impressions", 8: "sheets", 13: "tenthsOfGrams", 15: "tenthsOfMilliliters"}
COLORS = {"black": "#000000", "cyan": "#00FFFF", "magenta": "#FF00FF", "yellow": "#FFFF00", "red": "#FF0000",
          "green": "#00FF00", "blue": "#0000FF", "photo-black": "#000000", "matte-black": "#000000"}
ALERT_SEVERITY = {1: "other", 3: "critical", 4: "warning", 5: "warningBinaryChangeEvent"}
ALERT_GROUP = {5: "generalPrinter", 6: "cover", 7: "localization", 8: "input", 9: "output", 10: "marker",
               11: "markerSupplies", 12: "markerColorant", 13: "mediaPath", 14: "channel", 15: "interpreter",
               16: "consoleDisplayBuffer", 17: "consoleLights", 18: "alert"}
INPUT_TYPES = {3: "sheetFeedAutoRemovableTray", 4: "sheetFeedAutoNonRemovableTray", 5: "sheetFeedManual",
               6: "continuousRoll", 7: "continuousFanFold"}


def _table(rows, prefix):
    """[(oid, value)] under prefix.column.hr.index -> {index: {column: value}}"""
    table = {}
    for oid, value in rows:
        parts = oid[len(prefix) + 1:].split(".")
        if len(parts) >= 3:
            table.setdefault(int(parts[2]), {})[int(parts[0])] = value
    return table


def _s(v):
    return v.decode("utf-8", "replace").strip("\x00 ") if isinstance(v, bytes) else ("" if v is None else str(v))


def read(client):
    """One SNMP reading of the Printer MIB tables -> plain dict (also served as JSON on /status)."""
    supplies = _table(client.walk(SUPPLIES), SUPPLIES)
    colorants = _table(client.walk(COLORANTS), COLORANTS)
    inputs = _table(client.walk(INPUTS), INPUTS)
    alerts = _table(client.walk(ALERTS), ALERTS)
    life = client.walk(LIFECOUNT)
    unit = client.get(LIFEUNIT).get(LIFEUNIT)
    out = {"supplies": [], "inputs": [], "alerts": [], "page_count": life[0][1] if life else None,
           "page_count_unit": unit, "serial": _s(client.get(SERIAL).get(SERIAL)) or None,
           "counters": canon_counters(client)}
    for i in sorted(supplies):
        row = supplies[i]
        colorant = _s(colorants.get(row.get(3, 0), {}).get(4)).lower()
        maximum, level = row.get(8, -2), row.get(9, -2)
        percent = round(level * 100 / maximum) if maximum and maximum > 0 and level >= 0 else level
        out["supplies"].append({
            "description": _s(row.get(6)), "type": row.get(5), "class": row.get(4), "unit": row.get(7),
            "max": maximum, "level": level, "percent": percent, "colorant": colorant,
        })
    for i in sorted(inputs):
        row = inputs[i]
        out["inputs"].append({
            "name": _s(row.get(13)) or f"input-{i}", "type": row.get(2), "max": row.get(9), "level": row.get(10),
            "status": row.get(11), "feed": row.get(6) or row.get(4), "xfeed": row.get(7) or row.get(5),
            "media_name": _s(row.get(12)), "media_type": _s(row.get(21)),
        })
    for i in sorted(alerts):
        row = alerts[i]
        out["alerts"].append({"severity": row.get(2), "group": row.get(4), "code": row.get(7),
                              "description": _s(row.get(8))})
    return out


def canon_counters(client):
    """Canon's page counters -> {"total", "monochrome", "full-color", "highlight-color"}.

    {} when the model lacks the table.

    One GET for the total first, so a model without the table costs one packet; SNMP v1 fails a whole GET when
    one OID is missing, so the rest falls back to one GET per counter."""
    oid = {k: f"{CANON_COUNTER}.{n}" for k, n in CANON_COUNTERS.items()}
    if oid["total"] not in client.get(oid["total"]):
        return {}
    got = client.get(*oid.values())
    if not got:
        for o in oid.values():
            got.update(client.get(o))
    raw = {k: got[o] for k, o in oid.items() if isinstance(got.get(o), int)}
    out = {"total": raw["total"]}
    if "black-large" in raw or "black-small" in raw:
        out["monochrome"] = raw.get("black-large", 0) + raw.get("black-small", 0)
    if "color-large" in raw or "color-small" in raw:
        single = raw.get("single-large", 0) + raw.get("single-small", 0)
        out["full-color"] = raw.get("color-large", 0) + raw.get("color-small", 0) - single
        out["highlight-color"] = single
    return out


def _pwg_value(s):
    return re.sub(r"[;=]", " ", s)


def _set_supply(msg, supplies):
    msg.set("printer-supply", [ipp.string(
        f"type={SUPPLY_TYPE_PWG.get(s['type'], 'other')};maxcapacity={s['max']};level={s['level']};"
        f"class={SUPPLY_CLASS.get(s['class'], 'supplyThatIsConsumed')};"
        f"unit={SUPPLY_UNIT.get(s['unit'], 'other')};colorantname={s['colorant'] or 'unknown'};", ipp.OCTETS)
        for s in supplies])
    msg.set("printer-supply-description", [ipp.string(s["description"], ipp.TEXT) for s in supplies])


def apply(msg, info, wants):
    """Add to the reply what SNMP knows and the printer left out (module docstring). Returns notes."""
    notes = []
    supplies = info.get("supplies") or []
    known = len(msg.get("marker-names") or [])
    if len(supplies) > known and wants("marker-names"):
        msg.set("marker-names", [ipp.string(s["description"], ipp.NAME) for s in supplies])
        msg.set("marker-colors", [ipp.string(COLORS.get(s["colorant"], "none"), ipp.NAME) for s in supplies])
        msg.set("marker-types", [ipp.string(SUPPLY_TYPES.get(s["type"], "toner")) for s in supplies])
        msg.set("marker-levels", [ipp.integer(s["percent"] if s["percent"] >= -3 else -2) for s in supplies])
        low = msg.get("marker-low-levels")
        low = ipp.number(low[0]) if low else 10
        msg.set("marker-low-levels", [ipp.integer(low) for _ in supplies])
        msg.set("marker-high-levels", [ipp.integer(100) for _ in supplies])
        _set_supply(msg, supplies)
        notes.append(f"markers: {known} -> {len(supplies)} from SNMP")
    elif supplies and len(supplies) == known and wants("marker-names"):
        names = [ipp.text(v) for v in msg.get("marker-names")]
        full = [s["description"] for s in supplies]
        if all(f.startswith(n) for n, f in zip(names, full)) and full != names:
            msg.set("marker-names", [ipp.string(f, ipp.NAME) for f in full])
            notes.append(f"marker-names: {sum(f != n for n, f in zip(names, full))} completed from SNMP")
    if supplies and msg.get("printer-supply") is None and wants("printer-supply"):
        _set_supply(msg, supplies)
        notes.append(f"printer-supply: {len(supplies)} from SNMP")
    serial = info.get("serial")
    if serial and msg.get("printer-serial-number") is None and wants("printer-serial-number"):
        msg.set("printer-serial-number", [ipp.string(serial, ipp.TEXT)])
        notes.append("printer-serial-number from SNMP")
    counters = info.get("counters") or {}
    total = counters.get("total")
    if total is None and info.get("page_count_unit") == IMPRESSIONS:
        total = info.get("page_count")
    if total is not None and msg.get("printer-impressions-completed") is None \
            and wants("printer-impressions-completed"):
        msg.set("printer-impressions-completed", [ipp.integer(total)])
        notes.append("printer-impressions-completed from SNMP")
    members = {k.replace("-", "_"): ipp.integer(v) for k, v in counters.items() if k != "total"}
    if members and msg.get("printer-impressions-completed-col") is None \
            and wants("printer-impressions-completed-col"):
        msg.set("printer-impressions-completed-col", [ipp.collection(**members)])
    inputs = info.get("inputs") or []
    trays = msg.get("printer-input-tray") or []
    if len(inputs) > len(trays) and wants("printer-input-tray"):
        msg.set("printer-input-tray", [ipp.string(
            f"type={INPUT_TYPES.get(t['type'], 'other')};mediafeed={t['feed'] or 0};mediaxfeed={t['xfeed'] or 0};"
            f"maxcapacity={t['max']};level={t['level']};status={t['status']};name={_pwg_value(t['name'])};",
            ipp.OCTETS) for t in inputs])
        notes.append(f"input trays: {len(trays)} -> {len(inputs)} from SNMP")
    alerts = info.get("alerts") or []
    if alerts and msg.get("printer-alert") is None and wants("printer-alert"):
        msg.set("printer-alert", [ipp.string(
            f"code={a['code']};severity={ALERT_SEVERITY.get(a['severity'], 'other')};"
            f"group={ALERT_GROUP.get(a['group'], 'other')};", ipp.OCTETS) for a in alerts])
        msg.set("printer-alert-description", [ipp.string(a["description"] or "-", ipp.TEXT) for a in alerts])
        notes.append(f"printer-alert: {len(alerts)} from SNMP")
    return notes


class Snmp:
    """SNMP reader that never blocks a CUPS request.

    info() returns the last complete reading (from memory or the state directory). refresh() starts one background
    reading when the stored one is older than ttl; the server calls it only right after the printer answered over IPP,
    so a sleeping printer is never probed. A failed reading keeps the old one and waits `backoff` seconds before the
    next attempt; a partial reading is never stored.
    """

    def __init__(self, host, community="public", version="1", ttl=60, state=None, backoff=300):
        self.client = Client(host, community, version)
        self.ttl, self.backoff = ttl, backoff
        self.state = state or State()
        self._lock, self._running, self._retry_at = threading.Lock(), False, 0.0

    def info(self):
        return self.state.snmp or {}

    def refresh(self):
        """Start a background reading if one is due; return the thread or None."""
        now = time.time()
        with self._lock:
            fetched = self.state.fetched_at("snmp") or 0
            if self._running or now < self._retry_at or (self.state.snmp and now - fetched < self.ttl):
                return None
            self._running = True
        t = threading.Thread(target=self._read, daemon=True)
        t.start()
        return t

    def _read(self):
        try:
            info = read(self.client)
        except (OSError, ValueError, IndexError) as e:
            log.warning("SNMP read failed (%s); keeping the last reading, next try in %d s", type(e).__name__,
                        self.backoff)
            with self._lock:
                self._retry_at = time.time() + self.backoff
        else:
            self.state.remember_snmp(info)
        finally:
            with self._lock:
                self._running = False
