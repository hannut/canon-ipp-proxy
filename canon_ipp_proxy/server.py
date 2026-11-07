"""HTTP/IPP proxy: forwards every request to the printer and repairs Get-Printer-Attributes replies.

When the printer does not answer (the MF645C's deep sleep ends only with its sleep button), Get-Printer-Attributes
is answered from the last full reply kept in the state, marked offline-report, and every other operation gets
server-error-service-unavailable, which the CUPS ipp backend retries: jobs wait in the queue instead of failing.
"""
import http.client
import http.server
import json
import logging
import struct
import time
from urllib.parse import urlsplit

from . import enrich, ipp, repair
from .state import State

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5  # long enough for two SYN retransmits; a sleeping printer must not stall CUPS
REPLY_TIMEOUT = {ipp.GET_PRINTER_ATTRIBUTES: 30}  # other operations: DEFAULT_REPLY_TIMEOUT
DEFAULT_REPLY_TIMEOUT = 300
SERVICE_UNAVAILABLE = 0x0502
ASLEEP = "printer asleep or offline: press its sleep button"


def requested_attributes(body):
    try:
        msg = ipp.parse(body)
    except (ValueError, IndexError, struct.error):
        return None
    values = msg.get("requested-attributes", ipp.OPERATION) or []
    return {ipp.text(v) for v in values} or None


def full_request(requested):
    return not requested or bool(requested & {"all", "printer-description"})


def own_uris(msg, authority):
    """Point printer-uri-supported at the proxy (host:port the client used), keeping each URI's path.

    Clients that read it (AirPrint, IPP Everywhere discovery on another machine) then send jobs through the proxy,
    not straight to the printer."""
    uris = msg.get("printer-uri-supported")
    if not uris or not authority:
        return []
    new = [ipp.string("ipp://" + authority + (urlsplit(ipp.text(v)).path or "/ipp/print"), ipp.URI) for v in uris]
    if new == uris:
        return []
    msg.set("printer-uri-supported", new)
    return ["printer-uri-supported -> ipp://" + authority]


def fix_reply(body, requested, snmp=None, offline=None, request_id=None, authority=None):
    """Repaired reply body, or the original when it cannot be parsed.

    offline: text for printer-state-message when the reply comes from the state, not the printer.
    authority: host:port the client reached the proxy at; printer-uri-supported is rewritten to it."""
    try:
        msg = ipp.parse(body)
    except (ValueError, IndexError, struct.error) as e:
        log.warning("reply not parsed, passed through: %s", e)
        return body, []
    notes = repair.repair(msg, requested) + own_uris(msg, authority)
    if snmp is not None and msg.group(ipp.PRINTER) is not None:
        notes += enrich.apply(msg, snmp.info(), repair.wanted(requested))
    if request_id is not None:
        msg.request_id = request_id
    if offline:
        reasons = [v for v in msg.get("printer-state-reasons") or [] if ipp.text(v) != "none"]
        msg.set("printer-state-reasons", reasons + [ipp.string("offline-report")])
        msg.set("printer-state-message", [ipp.string(offline, ipp.TEXT)])
        notes.append("served from state: " + offline)
    return ipp.encode(msg), notes


def error_reply(request, status=SERVICE_UNAVAILABLE, message=ASLEEP):
    """Minimal IPP response with an error status for the request."""
    rid = struct.unpack(">I", request[4:8])[0]
    return ipp.encode(ipp.Message(request[0:2], status, rid, [[ipp.OPERATION, [
        ["attributes-charset", [ipp.string("utf-8", ipp.CHARSET)]],
        ["attributes-natural-language", [ipp.string("en", ipp.LANGUAGE)]],
        ["status-message", [ipp.string(message, ipp.TEXT)]],
    ]]]))


class Proxy(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, listen, printer_uri, snmp=None, state=None):
        u = urlsplit(printer_uri)
        if u.scheme != "ipp":
            raise SystemExit("only ipp:// printer URIs are supported")
        self.printer = (u.hostname, u.port or 631, u.path or "/ipp/print")
        self.snmp = snmp
        self.state = state or (snmp.state if snmp is not None else State())
        super().__init__(listen, Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "canon-ipp-proxy"

    def _body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            body = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";")[0], 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(body)
                body += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _send(self, status, ctype, data):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/status":
            snmp = self.server.snmp.info() if self.server.snmp is not None else {}
            status = dict(snmp, printer=self.server.state.summary())
            self._send(200, "application/json", json.dumps(status, indent=1).encode())
        else:
            self._send(404, "text/plain", b"POST IPP requests; GET /status for printer and SNMP details\n")

    def _forward(self, body, op):
        host, port, path = self.server.printer
        conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)
        try:
            conn.connect()
            conn.sock.settimeout(REPLY_TIMEOUT.get(op, DEFAULT_REPLY_TIMEOUT))
            conn.request("POST", path, body, {"Content-Type": "application/ipp"})
            resp = conn.getresponse()
            return resp.status, resp.getheader("Content-Type", "application/ipp"), resp.read()
        finally:
            conn.close()

    def do_POST(self):
        body = self._body()
        if len(body) < 8:
            self._send(400, "text/plain", b"not an IPP request\n")
            return
        op = struct.unpack(">H", body[2:4])[0]
        state, snmp = self.server.state, self.server.snmp
        requested = requested_attributes(body) if op == ipp.GET_PRINTER_ATTRIBUTES else None
        authority = self.headers.get("Host") or "%s:%d" % self.server.server_address[:2]
        try:
            status, ctype, data = self._forward(body, op)
        except OSError as e:
            state.seen(False)
            if op == ipp.GET_PRINTER_ATTRIBUTES and state.ipp is not None:
                since = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(state.fetched_at("ipp") or time.time()))
                data, notes = fix_reply(state.ipp, requested, snmp, f"{ASLEEP} (attributes from {since})",
                                        struct.unpack(">I", body[4:8])[0], authority)
            else:
                data, notes = error_reply(body), ["service-unavailable: " + ASLEEP]
            log.info("op=0x%04x in=%d printer=%s out=%d | %s", op, len(body), type(e).__name__, len(data),
                     "; ".join(notes))
            self._send(200, "application/ipp", data)
            return
        state.seen(True)
        notes = []
        if op == ipp.GET_PRINTER_ATTRIBUTES and status == 200:
            if full_request(requested):
                state.remember_ipp(data)
            if snmp is not None:
                snmp.refresh()  # the printer is awake now: the only moment SNMP is tried
            data, notes = fix_reply(data, requested, snmp, authority=authority)
        log.info("op=0x%04x in=%d http=%d out=%d%s", op, len(body), status, len(data),
                 " | " + "; ".join(notes) if notes else "")
        self._send(status, ctype, data)

    def log_message(self, *args):
        pass
