"""Repairs of Get-Printer-Attributes replies that libcups/cupsd reject or under-use.

Each repair is generic: it acts only when the reply shows the defect, and it derives what it adds from the printer's
own values where it can. Every repair returns notes for the log.
"""
import re
import struct

from . import ipp

# Used only when the printer lists no media at all. PWG 5101.1 names; dimensions follow from the names.
DEFAULT_MEDIA = [
    "iso_a4_210x297mm", "iso_a5_148x210mm", "iso_b5_176x250mm", "jis_b5_182x257mm", "na_letter_8.5x11in",
    "na_legal_8.5x14in", "na_executive_7.25x10.5in", "na_number-10_4.125x9.5in", "iso_dl_110x220mm",
    "iso_c5_162x229mm",
]
DEFAULT_MARGIN = 500  # 5 mm in 1/100 mm, when the printer gives no *-margin-supported
_SIZE = re.compile(r"_(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(mm|in)$")


def media_dimensions(name):
    """PWG self-describing media name -> (width, height) in 1/100 mm, or None."""
    m = _SIZE.search(name)
    if not m or name.startswith("custom_"):
        return None
    scale = 100 if m.group(3) == "mm" else 2540
    return round(float(m.group(1)) * scale), round(float(m.group(2)) * scale)


def _mm(hundredths):
    v = hundredths / 100
    return f"{v:g}"


def split_mixed_collections(msg):
    """A 1setOf mixing collections with other syntaxes is invalid; libcups then rejects the whole reply.

    Keep the majority syntax and return the dropped values per attribute (the Canon MF645C puts its custom media-size
    range collection inside the keyword set media-col-supported)."""
    dropped, notes = {}, []
    for tag, attrs in msg.groups:
        for attr in attrs:
            name, values = attr
            cols = [v for v in values if v[0] == ipp.BEGCOL]
            if cols and len(cols) < len(values):
                others = [v for v in values if v[0] != ipp.BEGCOL]
                keep, drop = (others, cols) if len(others) >= len(cols) else (cols, others)
                attr[1] = keep
                dropped[name] = drop
                notes.append(f"{name}: dropped {len(drop)} value(s) of another syntax")
    return dropped, notes


def clean_languages(msg):
    """naturalLanguage/charset values with padding or stray characters, e.g. 'en-  '."""
    notes = []
    for tag, attrs in msg.groups:
        for attr in attrs:
            for j, (vtag, raw) in enumerate(attr[1]):
                if vtag in (ipp.LANGUAGE, ipp.CHARSET):
                    clean = re.sub(rb"[^A-Za-z0-9-]", b"", raw).strip(b"-")
                    if clean != raw:
                        attr[1][j] = (vtag, clean or b"en")
                        notes.append(f"{attr[0]}: {raw!r} -> {clean!r}")
    return notes


def _custom_range(dropped):
    for value in dropped.get("media-col-supported", []):
        x, y = ipp.member(value, "x-dimension"), ipp.member(value, "y-dimension")
        if x and y and x[0][0] == ipp.RANGE and y[0][0] == ipp.RANGE:
            return value
    return None


def complete_media(msg, dropped, wants=lambda name: True):
    """Add media-size-supported, media-supported and media-col-database when absent."""
    notes = []
    names = [ipp.text(v) for v in msg.get("media-supported") or []]
    custom = _custom_range(dropped)

    if not names:
        for v in msg.get("media-ready") or []:
            names.append(ipp.text(v))
        names += [n for n in DEFAULT_MEDIA if n not in names]
    fixed = [(n, media_dimensions(n)) for n in names]
    fixed = [(n, d) for n, d in fixed if d]

    if msg.get("media-size-supported") is None and wants("media-size-supported"):
        values = [ipp.collection(x_dimension=ipp.integer(w), y_dimension=ipp.integer(h)) for _, (w, h) in fixed]
        if custom:
            values.append(custom)
        msg.set("media-size-supported", values)
        notes.append(f"media-size-supported: added {len(values)}")

    if msg.get("media-supported") is None and wants("media-supported"):
        values = [ipp.string(n) for n, _ in fixed]
        if custom:
            (lo_x, hi_x), (lo_y, hi_y) = (_range(ipp.member(custom, k)[0]) for k in ("x-dimension", "y-dimension"))
            values += [ipp.string(f"custom_min_{_mm(lo_x)}x{_mm(lo_y)}mm"),
                       ipp.string(f"custom_max_{_mm(hi_x)}x{_mm(hi_y)}mm")]
        msg.set("media-supported", values)
        notes.append(f"media-supported: added {len(values)}")

    if msg.get("media-col-database") is None and wants("media-col-database"):
        margins = {}
        for side in ("bottom", "left", "right", "top"):
            vals = msg.get(f"media-{side}-margin-supported")
            margins[side] = min(ipp.number(v) for v in vals) if vals else DEFAULT_MARGIN
        values = [ipp.collection(
            media_size=ipp.collection(x_dimension=ipp.integer(w), y_dimension=ipp.integer(h)),
            media_bottom_margin=ipp.integer(margins["bottom"]), media_left_margin=ipp.integer(margins["left"]),
            media_right_margin=ipp.integer(margins["right"]), media_top_margin=ipp.integer(margins["top"]),
        ) for _, (w, h) in fixed]
        msg.set("media-col-database", values)
        notes.append(f"media-col-database: added {len(values)}")
    return notes


def _range(value):
    return struct.unpack(">ii", value[1])


def _urf(msg):
    return {ipp.text(v) for v in msg.get("urf-supported") or []}


def complete_capabilities(msg):
    """Advertise what the printer's own URF capability string says but the IPP keywords omit."""
    notes = []
    urf = _urf(msg)
    sides = {ipp.text(v) for v in msg.get("sides-supported") or []}
    if sides and any(u.startswith("DM") for u in urf) and "two-sided-long-edge" not in sides:
        values = (msg.get("sides-supported") or []) + [ipp.string("two-sided-long-edge"),
                                                       ipp.string("two-sided-short-edge")]
        msg.set("sides-supported", values)
        notes.append("sides-supported: added two-sided (URF DM)")
    modes = {ipp.text(v) for v in msg.get("print-color-mode-supported") or []}
    if modes and modes <= {"auto"} and urf & {"SRGB24", "ADOBERGB24", "ADOBERGB24-48"}:
        msg.set("print-color-mode-supported", (msg.get("print-color-mode-supported") or []) +
                [ipp.string("monochrome"), ipp.string("color")])
        notes.append("print-color-mode-supported: added monochrome, color (URF SRGB24)")
    return notes


def wanted(requested):
    """requested-attributes of the request -> predicate; a narrow request (a status poll) gets nothing added."""
    if not requested:
        requested = {"all"}
    return lambda name: bool(requested & {"all", "printer-description", name})


def repair(msg, requested=None):
    """Apply every repair to a Get-Printer-Attributes reply in place; return notes."""
    dropped, notes = split_mixed_collections(msg)
    notes += clean_languages(msg)
    if msg.group(ipp.PRINTER) is not None:
        wants = wanted(requested)
        notes += complete_media(msg, dropped, wants)
        notes += complete_capabilities(msg)
    return notes
