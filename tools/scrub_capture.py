"""Scrub a printer capture for use as a public test fixture.

    python3 tools/scrub_capture.py REPLY.raw WALK.tsv OUT_DIR PREFIX

REPLY.raw is an HTTP response to Get-Printer-Attributes (headers and IPP body), WALK.tsv an SNMP walk as
`oid<TAB>tag<TAB>value` lines (binary values as `hex:...`). Writes PREFIX-get-printer-attributes.raw and
PREFIX-snmp-walk.tsv to OUT_DIR. The walk keeps only what the proxy reads (system group, Printer MIB, Canon
counters). Personal values are found in the capture itself (addresses, MACs, UUID, location, geo, serial, names
that are not the model) and replaced by placeholders of the same kind; the run fails if any original value is
still present afterwards.
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from canon_ipp_proxy import ipp

KEEP = ("1.3.6.1.2.1.1.", "1.3.6.1.2.1.43.", "1.3.6.1.4.1.1602.1.11.")
SYS_CONTACT, SYS_NAME, SYS_LOCATION = "1.3.6.1.2.1.1.4.0", "1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.1.6.0"
SERIAL_OID = "1.3.6.1.2.1.43.5.1.1.17.1"
IF_PHYS = "1.3.6.1.2.1.2.2.1.6."
IPV4 = re.compile(rb"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
HEXGROUPS = re.compile(rb"\b[0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){2,5}\b")
UUID_ZERO = "urn:uuid:00000000-0000-1000-8000-000000000000"


def private(octets):
    a, b = octets[0], octets[1]
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or (a == 169 and b == 254) \
        or (a == 100 and 64 <= b <= 127)


def placeholder(value):
    """Same shape: letters to A, digits to 0."""
    return re.sub(r"\d", "0", re.sub(r"[A-Za-z]", "A", value))


class Scrubber:
    def __init__(self):
        self.secrets = set()   # original values (bytes) that must not survive
        self.literal = {}      # original -> placeholder, applied to every text value
        self.addresses = {}

    def secret(self, old, new):
        if old and old != new:
            self.secrets.add(old.encode() if isinstance(old, str) else old)
            self.literal[old] = new

    def address(self, m):
        octets = [int(x) for x in m.groups()]
        if not private(octets):
            return m.group(0)
        old = m.group(0)
        if old not in self.addresses:
            self.addresses[old] = f"192.0.2.{10 + len(self.addresses)}".encode()
            self.secrets.add(old)
        return self.addresses[old]

    def text(self, raw):
        for old, new in self.literal.items():
            raw = raw.replace(old.encode(), new.encode())
        raw = IPV4.sub(self.address, raw)
        return HEXGROUPS.sub(lambda m: re.sub(rb"[0-9a-fA-F]", b"0", m.group(0)), raw)


def model_of(msg):
    dev = ipp.text(msg.get("printer-device-id")[0]) if msg.get("printer-device-id") else ""
    m = re.search(r"MDL:([^;]+)", dev)
    return m.group(1).split()[0] if m else ""


def learn_ipp(s, msg):
    model = model_of(msg)
    for name, new in (("printer-location", "Office"), ("printer-geo-location", "geo:0.000000,0.000000"),
                      ("printer-organization", "Organization"), ("printer-organizational-unit", "Unit"),
                      ("printer-serial-number", None)):
        for v in msg.get(name) or []:
            old = ipp.text(v)
            s.secret(old, new or placeholder(old))
    for v in msg.get("printer-uuid") or []:
        old = ipp.text(v)
        s.secret(old, UUID_ZERO)
        node = old.rsplit("-", 1)[-1]
        if len(node) == 12:
            mac(s, bytes.fromhex(node))
    for name in ("printer-name", "printer-dns-sd-name", "printer-info"):
        for v in msg.get(name) or []:
            old = ipp.text(v)
            if model and model not in old and not old.startswith("http"):
                s.secret(old, model)
    return model


def mac(s, b):
    if len(b) == 6 and any(b):
        h = b.hex()
        s.secrets.update({h.encode(), h.upper().encode()})
        s.literal[h] = "000000000000"
        s.literal[h.upper()] = "000000000000"


def learn_walk(s, rows, model):
    for oid, _, value in rows:
        if oid.startswith(IF_PHYS) and value.startswith("hex:"):
            mac(s, bytes.fromhex(value[4:]))
        elif oid == SYS_LOCATION:
            s.secret(value, "Office" if value else "")
        elif oid == SERIAL_OID:
            s.secret(value, placeholder(value))
        elif oid == SYS_CONTACT and value not in ("", "Administrator"):
            s.secret(value, "Administrator")
        elif oid == SYS_NAME and model and model not in value:
            s.secret(value, model)


def scrub_value(s, v):
    tag, raw = v
    if tag == ipp.BEGCOL:
        return (tag, [[m, [scrub_value(s, x) for x in vals]] for m, vals in raw])
    if tag in (ipp.TEXT, ipp.NAME, ipp.URI, ipp.KEYWORD, ipp.OCTETS, ipp.TEXT_LANG, ipp.NAME_LANG):
        return (tag, s.text(raw))
    return v


def main(reply_path, walk_path, out_dir, prefix):
    reply = pathlib.Path(reply_path).read_bytes()
    head, body = reply.split(b"\r\n\r\n", 1)
    msg = ipp.parse(body)
    rows = [line.split("\t", 2) for line in pathlib.Path(walk_path).read_text().splitlines() if line]
    s = Scrubber()
    model = learn_ipp(s, msg)
    learn_walk(s, rows, model)

    for group in msg.groups:
        for attr in group[1]:
            attr[1] = [scrub_value(s, v) for v in attr[1]]
    body = ipp.encode(msg)
    head = re.sub(rb"(?im)^content-length:.*$", b"Content-Length: %d" % len(body), head)
    out_reply = head + b"\r\n\r\n" + body

    kept = []
    for oid, tag, value in rows:
        if not oid.startswith(KEEP):
            continue
        if value.startswith("hex:"):
            raw = bytes.fromhex(value[4:])
            value = "hex:" + (s.text(raw).hex() if raw.isascii() else raw.hex())
        else:
            value = s.text(value.encode()).decode()
        kept.append(f"{oid}\t{tag}\t{value}\n")
    out_walk = "".join(kept).encode()

    leaks = sorted(x.decode(errors="replace") for x in s.secrets
                   if x.lower() in out_reply.lower() or x.lower() in out_walk.lower())
    if leaks:
        sys.exit(f"still present after scrubbing: {leaks}")
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{prefix}-get-printer-attributes.raw").write_bytes(out_reply)
    (out / f"{prefix}-snmp-walk.tsv").write_bytes(out_walk)
    print(f"replaced {len(s.secrets)} values; walk {len(rows)} -> {len(kept)} rows")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:])
