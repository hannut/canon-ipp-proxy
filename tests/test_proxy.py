"""Tests on a Canon MF645C's replies (firmware 13.01), personal values scrubbed by tools/scrub_capture.py."""
import pathlib
import struct
import unittest

from canon_ipp_proxy import enrich, ipp, repair, server

FIX = pathlib.Path(__file__).parent / "fixtures"
RAW = (FIX / "mf645c-get-printer-attributes.raw").read_bytes()
BODY = RAW[RAW.index(b"\r\n\r\n") + 4:]
WALK = FIX / "mf645c-snmp-walk.tsv"  # system group, Printer MIB, Canon counters


def names(msg):
    return [a[0] for a in msg.group(ipp.PRINTER)]


class Codec(unittest.TestCase):
    def test_round_trip_is_exact(self):
        self.assertEqual(ipp.encode(ipp.parse(BODY)), BODY)


class Repair(unittest.TestCase):
    def test_capture_has_the_defects(self):
        msg = ipp.parse(BODY)
        self.assertIn(ipp.BEGCOL, {v[0] for v in msg.get("media-col-supported")})
        for n in ("media-col-database", "media-supported", "media-size-supported"):
            self.assertIsNone(msg.get(n))

    def test_full_request_is_repaired(self):
        msg = ipp.parse(BODY)
        notes = repair.repair(msg, {"all", "media-col-database"})
        self.assertTrue(notes)
        self.assertEqual({v[0] for v in msg.get("media-col-supported")}, {ipp.KEYWORD})
        media = [ipp.text(v) for v in msg.get("media-supported")]
        self.assertEqual(media[0], "iso_a4_210x297mm")  # media-ready first
        self.assertIn("custom_min_76.2x127mm", media)
        self.assertIn("custom_max_216x355.6mm", media)
        db = msg.get("media-col-database")
        self.assertEqual(len(db), len(media) - 2)
        first = db[0]
        self.assertEqual(ipp.number(ipp.member(first, "media-top-margin")[0]), 500)
        ranges = [struct.unpack(">ii", m[1][0][1]) for v in msg.get("media-size-supported")
                  for m in v[1] if m[1][0][0] == ipp.RANGE]
        self.assertEqual(ranges, [(7620, 21600), (12700, 35560)])
        sides = [ipp.text(v) for v in msg.get("sides-supported")]
        self.assertIn("two-sided-long-edge", sides)
        modes = [ipp.text(v) for v in msg.get("print-color-mode-supported")]
        self.assertIn("color", modes)

    def test_repaired_reply_encodes_and_parses(self):
        msg = ipp.parse(BODY)
        repair.repair(msg, None)
        again = ipp.parse(ipp.encode(msg))
        self.assertEqual(names(again), names(msg))

    def test_narrow_request_gets_nothing_added(self):
        msg = ipp.parse(BODY)
        before = set(names(msg))
        repair.repair(msg, {"printer-state", "printer-state-reasons"})
        self.assertEqual(set(names(msg)), before)

    def test_other_attributes_untouched(self):
        before, msg = ipp.parse(BODY), ipp.parse(BODY)
        repair.repair(msg, None)
        changed = {"media-col-supported", "sides-supported", "print-color-mode-supported"}
        for name, values in before.group(ipp.PRINTER):
            if name not in changed:
                self.assertEqual(msg.get(name), values, name)

    def test_bad_language_is_cleaned(self):
        msg = ipp.parse(BODY)
        msg.set("natural-language-configured", [(ipp.LANGUAGE, b"en-  ")])
        repair.clean_languages(msg)
        self.assertEqual(msg.get("natural-language-configured"), [(ipp.LANGUAGE, b"en")])

    def test_media_names(self):
        self.assertEqual(repair.media_dimensions("na_letter_8.5x11in"), (21590, 27940))
        self.assertEqual(repair.media_dimensions("iso_a4_210x297mm"), (21000, 29700))
        self.assertIsNone(repair.media_dimensions("custom_max_216x355.6mm"))


class FakeSnmp:
    """A v1 walk (OID, BER tag, value) answering GET and walk like the printer's agent."""
    def __init__(self, path, drop=()):
        self.rows = {}
        for line in path.read_text().splitlines():
            oid, tag, value = line.split("\t")
            if not oid.startswith(tuple(drop)):
                self.rows[oid] = (int(value) if tag in ("0x02", "0x41", "0x42", "0x43") else
                                  bytes.fromhex(value[4:]) if value.startswith("hex:") else value.encode())
        self.gets = 0

    def walk(self, prefix):
        return [(o, v) for o, v in self.rows.items() if o.startswith(prefix + ".")]

    def get(self, *oids):
        self.gets += 1
        if any(o not in self.rows for o in oids):
            return {}  # v1: noSuchName fails the whole request
        return {o: self.rows[o] for o in oids}


class Enrich(unittest.TestCase):
    def setUp(self):
        self.info = enrich.read(FakeSnmp(WALK))

    def test_reading(self):
        s = self.info["supplies"]
        self.assertEqual([x["colorant"] for x in s], ["black", "cyan", "magenta", "yellow"])
        self.assertEqual([x["percent"] for x in s], [44, 81, 83, 77])
        self.assertEqual([t["name"] for t in self.info["inputs"]], ["Multi-Purpose Tray", "Tray 1"])
        self.assertEqual(self.info["page_count"], 2006)
        self.assertEqual(self.info["serial"], "0AA00000")
        self.assertEqual(self.info["counters"],
                         {"total": 2017, "monochrome": 1471, "full-color": 546, "highlight-color": 0})

    def test_what_ipp_lacks_is_added(self):
        msg = ipp.parse(BODY)  # IPP lists four toners and three trays, but no supplies, serial or counters
        notes = enrich.apply(msg, self.info, repair.wanted(None))
        self.assertEqual(notes, ["marker-names: 2 completed from SNMP", "printer-supply: 4 from SNMP",
                                 "printer-serial-number from SNMP", "printer-impressions-completed from SNMP"])
        self.assertEqual([ipp.text(v) for v in msg.get("marker-names")][2:],
                         ["Canon Cartridge 054 Magenta Toner", "Canon Cartridge 054 Yellow Toner"])
        self.assertEqual(ipp.text(msg.get("printer-supply")[0]),
                         "type=tonerCartridge;maxcapacity=100;level=44;class=supplyThatIsConsumed;unit=percent;"
                         "colorantname=black;")
        self.assertEqual(ipp.text(msg.get("printer-serial-number")[0]), "0AA00000")
        self.assertEqual(ipp.number(msg.get("printer-impressions-completed")[0]), 2017)
        col = msg.get("printer-impressions-completed-col")[0]
        self.assertEqual({m: ipp.number(v[0]) for m, v in col[1]},
                         {"monochrome": 1471, "full-color": 546, "highlight-color": 0})
        self.assertEqual(ipp.encode(ipp.parse(ipp.encode(msg))), ipp.encode(msg))
        for unchanged in ("marker-levels", "printer-input-tray", "printer-make-and-model"):
            self.assertEqual(msg.get(unchanged), ipp.parse(BODY).get(unchanged))

    def test_second_pass_adds_nothing(self):
        msg = ipp.parse(BODY)
        enrich.apply(msg, self.info, repair.wanted(None))
        self.assertEqual(enrich.apply(msg, self.info, repair.wanted(None)), [])

    def test_narrow_request_gets_no_additions(self):
        msg = ipp.parse(BODY)
        self.assertEqual(enrich.apply(msg, self.info, repair.wanted({"printer-state", "marker-levels"})), [])

    def test_model_without_canon_counter_table(self):
        snmp = FakeSnmp(WALK, drop=[enrich.CANON_COUNTER])
        info = enrich.read(snmp)
        self.assertEqual(info["counters"], {})
        msg = ipp.parse(BODY)
        enrich.apply(msg, info, repair.wanted(None))
        self.assertEqual(ipp.number(msg.get("printer-impressions-completed")[0]), 2006)  # life count, impressions
        self.assertIsNone(msg.get("printer-impressions-completed-col"))
        self.assertEqual(snmp.gets, 3)  # life unit, serial, one probe for the counter table

    def test_counters_one_by_one_when_one_is_missing(self):
        snmp = FakeSnmp(WALK, drop=[enrich.CANON_COUNTER + ".110", enrich.CANON_COUNTER + ".111"])
        self.assertEqual(enrich.canon_counters(snmp), {"total": 2017, "monochrome": 1471, "full-color": 546,
                                                       "highlight-color": 0})

    def test_life_count_in_other_units_is_not_impressions(self):
        msg = ipp.parse(BODY)
        enrich.apply(msg, {"page_count": 99, "page_count_unit": 8}, repair.wanted(None))  # sheets
        self.assertIsNone(msg.get("printer-impressions-completed"))

    def test_trays_from_snmp_when_ipp_has_fewer(self):
        msg = ipp.parse(BODY)
        msg.set("printer-input-tray", msg.get("printer-input-tray")[:1])
        enrich.apply(msg, self.info, repair.wanted(None))
        trays = [ipp.text(v) for v in msg.get("printer-input-tray")]
        self.assertIn("name=Multi-Purpose Tray;", trays[0])
        self.assertTrue(trays[1].startswith("type=sheetFeedAutoRemovableTray;mediafeed=297000;mediaxfeed=210000;"))

    def test_alerts(self):
        msg = ipp.parse(BODY)
        info = dict(self.info, alerts=[{"severity": 4, "group": 11, "code": 1104, "description": "Toner low"}])
        self.assertIn("printer-alert: 1 from SNMP", enrich.apply(msg, info, repair.wanted(None)))
        self.assertEqual([ipp.text(v) for v in msg.get("printer-alert")],
                         ["code=1104;severity=warning;group=markerSupplies;"])

    def test_markers_from_snmp_when_ipp_has_fewer(self):
        msg = ipp.parse(BODY)
        msg.set("marker-names", msg.get("marker-names")[:1])
        enrich.apply(msg, self.info, repair.wanted(None))
        self.assertEqual([ipp.text(v) for v in msg.get("marker-colors")],
                         ["#000000", "#00FFFF", "#FF00FF", "#FFFF00"])
        self.assertEqual([ipp.number(v) for v in msg.get("marker-levels")], [44, 81, 83, 77])

    def test_failed_reading_changes_nothing(self):
        msg = ipp.parse(BODY)
        self.assertEqual(enrich.apply(msg, {}, repair.wanted(None)), [])


class Server(unittest.TestCase):
    def test_fix_reply_passes_garbage_through(self):
        self.assertEqual(server.fix_reply(b"\x02\x00garbage", None)[0], b"\x02\x00garbage")


if __name__ == "__main__":
    unittest.main()
