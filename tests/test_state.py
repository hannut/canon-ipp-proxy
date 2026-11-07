"""Sleep tolerance: the persistent state, the non-blocking SNMP reader and the proxy while the printer is away."""
import http.client
import http.server
import json
import pathlib
import socket
import struct
import tempfile
import threading
import unittest

from canon_ipp_proxy import enrich, ipp, server, state

FIX = pathlib.Path(__file__).parent / "fixtures"
RAW = (FIX / "mf645c-get-printer-attributes.raw").read_bytes()
BODY = RAW[RAW.index(b"\r\n\r\n") + 4:]


def gpa_request(rid=7, requested=("all",)):
    attrs = [["attributes-charset", [ipp.string("utf-8", ipp.CHARSET)]],
             ["attributes-natural-language", [ipp.string("en", ipp.LANGUAGE)]],
             ["printer-uri", [ipp.string("ipp://localhost/ipp/print", ipp.URI)]],
             ["requested-attributes", [ipp.string(r) for r in requested]]]
    return ipp.encode(ipp.Message(b"\x02\x00", ipp.GET_PRINTER_ATTRIBUTES, rid, [[ipp.OPERATION, attrs]]))


def with_uptime(body, seconds):
    msg = ipp.parse(body)
    msg.set("printer-up-time", [ipp.integer(seconds)])
    return ipp.encode(msg)


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.dir.name) / "p"

    def tearDown(self):
        self.dir.cleanup()

    def test_survives_restart(self):
        s = state.State(self.path)
        self.assertTrue(s.remember_ipp(BODY, now=1000))
        self.assertTrue(s.remember_snmp({"supplies": [], "inputs": [], "page_count": 1}, now=1000))
        again = state.State(self.path)
        self.assertEqual(again.ipp, BODY)
        self.assertEqual(again.snmp["page_count"], 1)
        self.assertEqual(again.fetched_at("ipp"), 1000)

    def test_volatile_change_is_not_written_until_old(self):
        s = state.State(self.path)
        s.remember_ipp(with_uptime(BODY, 1), now=1000)
        self.assertFalse(s.remember_ipp(with_uptime(BODY, 2), now=1100))
        self.assertEqual(state.State(self.path).ipp, with_uptime(BODY, 1))
        self.assertTrue(s.remember_ipp(with_uptime(BODY, 3), now=1000 + state.PERSIST_AGE))

    def test_stable_change_is_written(self):
        s = state.State(self.path)
        s.remember_ipp(BODY, now=1000)
        msg = ipp.parse(BODY)
        msg.set("printer-name", [ipp.string("renamed", ipp.NAME)])
        self.assertTrue(s.remember_ipp(ipp.encode(msg), now=1001))

    def test_damaged_files_are_ignored(self):
        self.path.mkdir(parents=True)
        (self.path / "printer-attributes.ipp").write_bytes(b"\x02\x00\x00")
        (self.path / "state.json").write_text("{")
        s = state.State(self.path)
        self.assertIsNone(s.ipp)
        self.assertEqual(s.meta, {})

    def test_garbage_reply_is_not_kept(self):
        s = state.State(self.path)
        self.assertFalse(s.remember_ipp(b"\x02\x00garbage"))
        self.assertIsNone(s.ipp)

    def test_online_transitions_only(self):
        s = state.State(self.path)
        s.seen(False, now=5)
        s.seen(False, now=6)
        self.assertEqual(s.meta["offline_since"], 5)
        s.seen(True, now=7)
        self.assertNotIn("offline_since", s.meta)
        self.assertEqual(json.loads((self.path / "state.json").read_text())["last_seen_at"], 7)


class FlakyClient:
    """Drops the first n walks like a Canon just out of sleep."""

    def __init__(self, fail=0):
        self.fail, self.calls = fail, 0

    def walk(self, prefix):
        self.calls += 1
        if self.fail:
            self.fail -= 1
            raise TimeoutError()
        return [] if prefix != enrich.LIFECOUNT else [(prefix + ".1.1", 2005)]

    def get(self, *oids):
        self.calls += 1
        return {}


class Reader(unittest.TestCase):
    def snmp(self, fail=0):
        s = enrich.Snmp("192.0.2.1", state=state.State(), backoff=300)
        s.client = FlakyClient(fail)
        return s

    def test_failure_keeps_nothing_and_backs_off(self):
        s = self.snmp(fail=1)
        s.refresh().join()
        self.assertEqual(s.info(), {})
        self.assertIsNone(s.refresh())  # within backoff: no traffic

    def test_reading_is_kept_and_not_repeated_within_ttl(self):
        s = self.snmp()
        s.refresh().join()
        self.assertEqual(s.info()["page_count"], 2005)
        calls = s.client.calls
        self.assertIsNone(s.refresh())
        self.assertEqual(s.client.calls, calls)


class FakePrinter(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        req = self.rfile.read(int(self.headers["Content-Length"]))
        msg = ipp.parse(BODY)
        msg.request_id = struct.unpack(">I", req[4:8])[0]
        data = ipp.encode(msg)
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Asleep(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.port = free_port()
        self.state = state.State(self.dir.name)
        self.proxy = server.Proxy(("127.0.0.1", 0), f"ipp://127.0.0.1:{self.port}/ipp/print", state=self.state)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.printer = None

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        if self.printer:
            self.printer.shutdown()
            self.printer.server_close()
        self.dir.cleanup()

    def wake(self):
        self.printer = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), FakePrinter)
        threading.Thread(target=self.printer.serve_forever, daemon=True).start()

    def sleep(self):
        self.printer.shutdown()
        self.printer.server_close()
        self.printer = None

    def post(self, body):
        conn = http.client.HTTPConnection(*self.proxy.server_address, timeout=10)
        conn.request("POST", "/ipp/print", body, {"Content-Type": "application/ipp"})
        resp = conn.getresponse()
        return resp.status, ipp.parse(resp.read())

    def test_no_state_yet_gives_service_unavailable(self):
        status, msg = self.post(gpa_request())
        self.assertEqual((status, msg.code, msg.request_id), (200, server.SERVICE_UNAVAILABLE, 7))

    def test_attributes_from_state_while_asleep_and_jobs_wait(self):
        self.wake()
        status, msg = self.post(gpa_request())
        self.assertEqual(msg.code, 0)
        self.assertIsNotNone(msg.get("media-col-database"))
        self.sleep()
        status, msg = self.post(gpa_request(rid=9, requested=("printer-state", "printer-state-reasons")))
        self.assertEqual((msg.code, msg.request_id), (0, 9))
        self.assertIn("offline-report", [ipp.text(v) for v in msg.get("printer-state-reasons")])
        self.assertIsNone(msg.get("media-col-database"))  # a narrow poll gets nothing added, as when awake
        status, msg = self.post(gpa_request(rid=11))
        self.assertIsNotNone(msg.get("media-col-database"))  # a full request is repaired like a live reply
        self.assertIn("press its sleep button", ipp.text(msg.get("printer-state-message")[0]))
        job = bytearray(gpa_request(rid=10))
        job[2:4] = struct.pack(">H", 0x0002)  # Print-Job
        status, msg = self.post(bytes(job))
        self.assertEqual(msg.code, server.SERVICE_UNAVAILABLE)
        self.assertFalse(self.state.online)
        restarted = state.State(self.dir.name)
        self.assertEqual(state.ipp_stable(restarted.ipp), state.ipp_stable(BODY))

    def test_narrow_poll_does_not_replace_full_attributes(self):
        self.wake()
        self.post(gpa_request(requested=("printer-state",)))
        self.assertIsNone(self.state.ipp)


if __name__ == "__main__":
    unittest.main()
