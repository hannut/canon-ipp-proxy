"""Capture a printer's Get-Printer-Attributes reply and SNMP walk, and scrub them for sharing.

    python3 tools/capture.py PRINTER_HOST OUT_DIR [--community public]

Writes the raw captures (private: they hold the printer's addresses, serial and location) as
OUT_DIR/reply.raw and OUT_DIR/walk.tsv, then the scrubbed copies OUT_DIR/MODEL-get-printer-attributes.raw and
OUT_DIR/MODEL-snmp-walk.tsv (tools/scrub_capture.py), which are the ones to share. One IPP request and one SNMP walk
of the subtrees the proxy reads; nothing is changed on the printer.
"""
import argparse
import pathlib
import re
import socket
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import scrub_capture

from canon_ipp_proxy import ipp, snmp


def get_printer_attributes(host, port=631, path="/ipp/print"):
    def attr(tag, name, value):
        n, v = name.encode(), value.encode()
        return bytes([tag]) + struct.pack(">H", len(n)) + n + struct.pack(">H", len(v)) + v
    body = (b"\x02\x00" + struct.pack(">HI", ipp.GET_PRINTER_ATTRIBUTES, 1) + bytes([ipp.OPERATION])
            + attr(ipp.CHARSET, "attributes-charset", "utf-8")
            + attr(ipp.LANGUAGE, "attributes-natural-language", "en")
            + attr(ipp.URI, "printer-uri", f"ipp://{host}:{port}{path}")
            + attr(ipp.KEYWORD, "requested-attributes", "all") + bytes([ipp.END]))
    head = (f"POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/ipp\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
    data = b""
    with socket.create_connection((host, port), timeout=30) as s:
        s.sendall(head + body)
        while chunk := s.recv(65536):
            data += chunk
    return data


def walk(host, community):
    client = snmp.Client(host, community)
    rows = []
    for prefix in (p.rstrip(".") for p in scrub_capture.KEEP):
        cur = prefix
        while True:
            error, binds = client._request(snmp.GETNEXT, [cur])
            if error or not binds:
                break
            oid, tag, value = binds[0]
            if tag in snmp.END_TAGS or not oid.startswith(prefix + "."):
                break
            if isinstance(value, bytes):
                printable = all(32 <= c < 127 for c in value)
                value = value.decode() if printable else "hex:" + value.hex()
            rows.append(f"{oid}\t0x{tag:02x}\t{value}\n")
            cur = oid
    return "".join(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("host")
    p.add_argument("out_dir")
    p.add_argument("--community", default="public")
    a = p.parse_args()
    out = pathlib.Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    reply = get_printer_attributes(a.host)
    (out / "reply.raw").write_bytes(reply)
    try:
        rows = walk(a.host, a.community)
    except TimeoutError:
        print("no SNMP answer (v1, community public?); capturing IPP only", file=sys.stderr)
        rows = ""
    (out / "walk.tsv").write_text(rows)
    msg = ipp.parse(reply.split(b"\r\n\r\n", 1)[1])
    model = re.sub(r"[^a-z0-9]+", "-", scrub_capture.model_of(msg).lower()).strip("-") or "printer"
    scrub_capture.main(out / "reply.raw", out / "walk.tsv", out, model)
    print(f"share {out}/{model}-get-printer-attributes.raw and {model}-snmp-walk.tsv")


if __name__ == "__main__":
    main()
