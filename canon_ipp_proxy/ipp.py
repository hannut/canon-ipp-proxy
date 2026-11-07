"""IPP message parsing and encoding (RFC 8010), enough to rewrite printer attributes losslessly.

A message is Message(version, code, request_id, groups, data). groups is a list of [group_tag, [Attr]].
An Attr is [name, [value]]; a value is (tag, bytes) or, for a collection, (BEGCOL, [[member_name, [value]]]).
"""
import struct
from dataclasses import dataclass, field

OPERATION, JOB, END, PRINTER, UNSUPPORTED = 0x01, 0x02, 0x03, 0x04, 0x05
INTEGER, BOOLEAN, ENUM = 0x21, 0x22, 0x23
OCTETS, DATETIME, RESOLUTION, RANGE = 0x30, 0x31, 0x32, 0x33
BEGCOL, TEXT_LANG, NAME_LANG, ENDCOL = 0x34, 0x35, 0x36, 0x37
TEXT, NAME, KEYWORD, URI, URISCHEME, CHARSET, LANGUAGE, MIMETYPE, MEMBER = (
    0x41, 0x42, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A)

GET_PRINTER_ATTRIBUTES = 0x000B


@dataclass
class Message:
    version: bytes
    code: int
    request_id: int
    groups: list = field(default_factory=list)
    data: bytes = b""

    def group(self, tag):
        for g in self.groups:
            if g[0] == tag:
                return g[1]
        return None

    def get(self, name, tag=PRINTER):
        for a in self.group(tag) or []:
            if a[0] == name:
                return a[1]
        return None

    def set(self, name, values, tag=PRINTER):
        attrs = self.group(tag)
        if attrs is None:
            attrs = []
            self.groups.append([tag, attrs])
        for a in attrs:
            if a[0] == name:
                a[1] = values
                return
        attrs.append([name, values])


def _tokens(body, i):
    toks = []
    while i < len(body):
        tag = body[i]
        i += 1
        if tag < 0x10:
            toks.append((tag, None, b""))
            if tag == END:
                return toks, i
            continue
        if tag == 0x7F:  # extension: 4-byte tag follows, keep as is
            raise ValueError("extended value tags are not supported")
        (nl,) = struct.unpack(">H", body[i:i + 2])
        name = body[i + 2:i + 2 + nl].decode("utf-8", "replace")
        i += 2 + nl
        (vl,) = struct.unpack(">H", body[i:i + 2])
        toks.append((tag, name, body[i + 2:i + 2 + vl]))
        i += 2 + vl
    raise ValueError("IPP message has no end-of-attributes tag")


def _value(toks, k):
    tag, _, raw = toks[k]
    if tag != BEGCOL:
        return (tag, raw), k + 1
    members, k = [], k + 1
    while toks[k][0] != ENDCOL:
        if toks[k][0] != MEMBER:
            raise ValueError("collection without member name")
        member = [toks[k][2].decode("utf-8", "replace"), []]
        k += 1
        while toks[k][0] not in (MEMBER, ENDCOL):
            v, k = _value(toks, k)
            member[1].append(v)
        members.append(member)
    return (BEGCOL, members), k + 1


def parse(body):
    version = body[0:2]
    code, request_id = struct.unpack(">HI", body[2:8])
    toks, end = _tokens(body, 8)
    msg = Message(version, code, request_id, data=body[end:])
    k, attrs = 0, None
    while k < len(toks):
        tag, name, _ = toks[k]
        if name is None:
            if tag == END:
                break
            attrs = []
            msg.groups.append([tag, attrs])
            k += 1
            continue
        v, k2 = _value(toks, k)
        if name:
            attrs.append([name, [v]])
        else:
            attrs[-1][1].append(v)
        k = k2
    return msg


def _tok(tag, name, raw=b""):
    n = name.encode()
    return bytes([tag]) + struct.pack(">H", len(n)) + n + struct.pack(">H", len(raw)) + raw


def _encode_value(name, value):
    tag, raw = value
    if tag != BEGCOL:
        return _tok(tag, name, raw)
    out = _tok(BEGCOL, name)
    for member, values in raw:
        out += _tok(MEMBER, "", member.encode())
        for v in values:
            out += _encode_value("", v)
    return out + _tok(ENDCOL, "")


def encode(msg):
    out = bytearray(msg.version + struct.pack(">HI", msg.code, msg.request_id))
    for tag, attrs in msg.groups:
        out.append(tag)
        for name, values in attrs:
            for j, v in enumerate(values):
                out += _encode_value(name if j == 0 else "", v)
    out.append(END)
    return bytes(out) + msg.data


# value helpers
def integer(n, tag=INTEGER):
    return (tag, struct.pack(">i", n))


def string(s, tag=KEYWORD):
    return (tag, s.encode() if isinstance(s, str) else s)


def collection(**members):
    return (BEGCOL, [[k.replace("_", "-"), v if isinstance(v, list) else [v]] for k, v in members.items()])


def text(value):
    tag, raw = value
    return raw.decode("utf-8", "replace")


def number(value):
    return struct.unpack(">i", value[1])[0]


def member(value, name):
    for m, vals in value[1]:
        if m == name:
            return vals
    return None
