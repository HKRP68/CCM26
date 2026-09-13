"""Read an .xlsx workbook with nothing but the standard library.

``admin._build_players_xlsx`` already *writes* spreadsheets this way — a zip of
hand-built XML parts — precisely so the project needs no Excel dependency. This
is the mirror image, so an admin can upload the same file they downloaded.

It is not a general-purpose Excel library and does not try to be. It reads the
first worksheet as a grid of strings: enough for "here is my player list", which
is the only thing the draft importer asks of it. Formulas yield their cached
value, dates come back as the underlying serial number, and styling is ignored.

Usage::

    from services.xlsx_reader import read_rows, XlsxError
    rows = read_rows(uploaded_file.read())   # [["Name", "Rating", ...], ...]
"""

import logging
import re
import zipfile
import io
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

# The one namespace every part of a SpreadsheetML workbook lives in.
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Hard ceilings. A draft pool is hundreds of rows, not hundreds of thousands, so
# a file far outside that shape is a mistake (or a zip bomb) rather than input.
MAX_ROWS = 10000
MAX_COLS = 200


class XlsxError(Exception):
    """The upload is not a workbook we can read. Message is user-facing."""


def _cell_ref_to_col(ref):
    """``"AB12"`` → 27 (zero-based column index). ``None`` when unparseable."""
    match = re.match(r"^([A-Za-z]+)", ref or "")
    if not match:
        return None
    index = 0
    for char in match.group(1).upper():
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _shared_strings(archive):
    """The workbook's shared string table, in order.

    Most text in a real .xlsx is stored once here and referenced by index; a
    file written by ``_build_players_xlsx`` uses inline strings instead and has
    no such part at all, which is why a missing part is not an error.
    """
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    strings = []
    for si in ElementTree.fromstring(raw).findall(f"{_NS}si"):
        # A single string can be split across several runs (<r><t>), e.g. when
        # part of it is bold. Joining every descendant <t> puts it back together.
        strings.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
    return strings


def _first_sheet_path(archive):
    """Path of the first worksheet part, however the writer chose to name it."""
    names = [n for n in archive.namelist()
             if n.startswith("xl/worksheets/") and n.endswith(".xml")]
    if not names:
        raise XlsxError("That file has no worksheets in it.")
    # sheet1 before sheet10 — plain sorting would put sheet10 first.
    def _key(name):
        digits = re.findall(r"(\d+)", name)
        return (int(digits[-1]) if digits else 0, name)
    return sorted(names, key=_key)[0]


def _cell_text(cell, strings):
    """One cell's value as a string. Empty string when the cell is blank."""
    kind = cell.get("t") or "n"
    if kind == "inlineStr":
        node = cell.find(f"{_NS}is")
        return "".join(t.text or "" for t in node.iter(f"{_NS}t")) if node is not None else ""
    value = cell.find(f"{_NS}v")
    if value is None or value.text is None:
        return ""
    text = value.text
    if kind == "s":
        try:
            return strings[int(text)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "1" if text.strip() in ("1", "true", "TRUE") else "0"
    if kind == "n":
        # Numbers arrive as "96" or "96.0"; a rating that renders as "96.0"
        # then fails int() in the importer, so trim a pure-zero fraction.
        if text.endswith(".0"):
            text = text[:-2]
    return text


def read_rows(data, *, max_rows=MAX_ROWS):
    """Parse .xlsx bytes into a list of rows of stripped strings.

    Rows are padded to the width of the widest row so a caller can index by
    column without bounds-checking every cell, and trailing blank rows (which
    Excel leaves behind constantly) are dropped.
    """
    if not data:
        raise XlsxError("The uploaded file was empty.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise XlsxError("That doesn't look like an .xlsx file. Save it as "
                        "Excel Workbook (.xlsx) or CSV and try again.")
    try:
        strings = _shared_strings(archive)
        sheet_xml = archive.read(_first_sheet_path(archive))
    except XlsxError:
        raise
    except Exception as exc:
        logger.warning("xlsx parse failed: %r", exc)
        raise XlsxError("That workbook could not be read. Try saving it again, "
                        "or upload a CSV instead.")

    try:
        root = ElementTree.fromstring(sheet_xml)
    except ElementTree.ParseError:
        raise XlsxError("That workbook's first sheet is corrupt.")

    rows = []
    for row in root.iter(f"{_NS}row"):
        cells = []
        for cell in row.findall(f"{_NS}c"):
            # Excel omits empty cells entirely, so a row's cells are placed by
            # their own reference rather than by the order they appear in.
            col = _cell_ref_to_col(cell.get("r"))
            if col is None:
                col = len(cells)
            if col >= MAX_COLS:
                continue
            while len(cells) <= col:
                cells.append("")
            cells[col] = (_cell_text(cell, strings) or "").strip()
        rows.append(cells)
        if len(rows) >= max_rows:
            break

    while rows and not any(rows[-1]):
        rows.pop()
    width = max((len(r) for r in rows), default=0)
    for row in rows:
        while len(row) < width:
            row.append("")
    return rows
