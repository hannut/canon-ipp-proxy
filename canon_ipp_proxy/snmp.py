"""Minimal SNMP v1/v2c client (GET and GETNEXT walk) over UDP, Python stdlib only."""
import itertools
import socket

_ids = itertools.count(1)

INTEGER, OCTETS, NULL, OID, SEQ = 0x02, 0x04, 0x05, 0x06, 0x30
GET, GETNEXT, RESPONSE = 0xA0, 0xA1, 0xA2
END_TAGS = {0x80, 0x81, 0x82}  # noSuchObject, noSuchInstance, endOfMibView (v2c)


def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, body):
    return bytes([tag]) + _len(len(body)) + body


def _int(v):
    return _tlv(INTEGER, v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big", signed=True))


def _oid(text):
    parts = [int(x) for x in text.strip(".").split(".")]
    body = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        enc = [p & 0x7F]
        p >>= 7
        while p:
            enc.insert(0, (p & 0x7F) | 0x80)
            p >>= 7
        body += bytes(enc)
    return _tlv(OID, body)


def _read(data, i):
    tag = data[i]
    n = data[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(data[i:i + k], "big")
        i += k
    return tag, data[i:i + n], i + n


def _children(body):
    out, i = [], 0
    while i < len(body):
        tag, val, i = _read(body, i)
        out.append((tag, val))
    return out


def _decode_oid(b):
    parts = [b[0] // 40, b[0] % 40]
    v = 0
    for x in b[1:]:
        v = (v << 7) | (x & 0x7F)
        if not x & 0x80:
            parts.append(v)
            v = 0
    return ".".join(map(str, parts))


def _value(tag, b):
    if tag in (INTEGER, 0x41, 0x42, 0x43, 0x46):  # Integer, Counter32, Gauge32, TimeTicks, Counter64
        return int.from_bytes(b, "big", signed=(tag == INTEGER)) if b else 0
    if tag == OCTETS:
        return b
    if tag == OID:
        return _decode_oid(b)
    if tag == NULL or tag in END_TAGS:
        return None
    return b


class Client:
    def __init__(self, host, community="public", version="1", timeout=2.0, port=161, retries=1):
        self.addr = (host, port)
        self.community = community.encode()
        self.version = 0 if version == "1" else 1
        self.timeout = timeout
        self.retries = retries  # the printer's SNMP stack drops requests now and then

    def _request(self, pdu_type, oids):
        rid = next(_ids)
        binds = b"".join(_tlv(SEQ, _oid(o) + _tlv(NULL, b"")) for o in oids)
        pdu = _tlv(pdu_type, _int(rid) + _int(0) + _int(0) + _tlv(SEQ, binds))
        msg = _tlv(SEQ, _int(self.version) + _tlv(OCTETS, self.community) + pdu)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(self.timeout)
            for attempt in range(self.retries + 1):
                s.sendto(msg, self.addr)
                try:
                    data = s.recv(65535)
                    break
                except TimeoutError:
                    if attempt == self.retries:
                        raise
        _, body, _ = _read(data, 0)
        parts = _children(body)
        tag, pdu_body = parts[2]
        fields = _children(pdu_body)
        error = _value(*fields[1])
        if error:
            return error, []
        binds = []
        for _, vb in _children(fields[3][1]):
            (otag, oval), (vtag, vval) = _children(vb)
            binds.append((_decode_oid(oval), vtag, _value(vtag, vval)))
        return 0, binds

    def get(self, *oids):
        """Return {oid: value} for the OIDs that exist."""
        error, binds = self._request(GET, list(oids))
        return {o: v for o, t, v in binds if t not in END_TAGS} if not error else {}

    def walk(self, prefix, limit=1000):
        """Return [(oid, value)] under prefix, by GETNEXT."""
        out, cur = [], prefix
        for _ in range(limit):
            error, binds = self._request(GETNEXT, [cur])
            if error or not binds:
                break
            oid, tag, value = binds[0]
            if tag in END_TAGS or not oid.startswith(prefix + "."):
                break
            out.append((oid, value))
            cur = oid
        return out
