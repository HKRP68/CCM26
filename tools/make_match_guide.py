"""Build the Match & Tournament guide PDF from its Markdown.

    python tools/make_match_guide.py

Reads ``docs/Match-Types-Guide.md`` and writes ``docs/Match-Types-Guide.pdf``,
which is committed so ``/matchguide pdf`` can hand it over without reportlab on
the bot host.

Unlike the auction guides (two *different* documents written in Python), this
PDF is a rendering of the Markdown: the bot also sends that Markdown file and
renders single sections of it in chat, so one source keeps all three in step.
Re-run this after editing the Markdown — ``tests/test_match_guide.py`` fails
when the PDF is older than its source.

Only the Markdown the guide uses is understood: ``#``/``##``/``###`` headings,
paragraphs, ``*``/``-`` bullets, ``1.`` steps, ``>`` notes, pipe tables, fenced
code, and inline ``code``, ``**bold**``, ``*italic*`` and ``[links](#…)``.
Fonts, colours, the code-block style and the page footer are the auction
guides' own (``tools/make_auction_guides.py``), so the PDFs look like a set.
"""

from __future__ import annotations

import os
import re
import sys
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    CondPageBreak, HRFlowable, ListFlowable, ListItem, PageBreak, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)

from tools.make_auction_guides import (  # noqa: E402
    ACCENT, F, RULE, S, T, code, page_furniture,
)

SOURCE = os.path.join(ROOT, "docs", "Match-Types-Guide.md")
OUTPUT = os.path.join(ROOT, "docs", "Match-Types-Guide.pdf")
TITLE = "CMU Cricket - Complete Match & Tournament Guide"
CONTENT_WIDTH = A4[0] - 40 * mm


# Emoji that carry meaning in the guide, said in words — DejaVu has no glyph
# for them, and T() would otherwise drop them silently, leaving "= group only".
EMOJI_WORDS = {
    "✅": "Yes", "❌": "No", "👥": "[group]", "💬": "[DM]", "💎": " gems",
    "🥉": "", "🥈": "", "🥇": "", "🔷": "", "👑": "",
}


def inline(text):
    """One line of Markdown → ReportLab paragraph markup, glyph-safe."""
    for emoji, words in EMOJI_WORDS.items():
        text = text.replace(emoji, words)
    out = []
    for part in re.split(r"(`[^`]+`)", text):
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            inner = escape(T(part[1:-1].replace("\\|", "|"), collapse=False))
            out.append(f'<font face="{F["Mono"]}" size="8.8">{inner}</font>')
            continue
        part = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", part)
        part = escape(T(part, collapse=False))
        part = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", part)
        part = re.sub(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])", r"<i>\1</i>", part)
        out.append(part)
    return re.sub(r" {2,}", " ", "".join(out)).strip() or " "


def para(text, style="body"):
    return Paragraph(inline(text), S[style])


def cells(row):
    row = row.strip().strip("|")
    return [c.strip() for c in re.split(r"(?<!\\)\|", row)]


def widths_for(rows):
    """Column widths proportional to each column's longest cell, within bounds."""
    n = max(len(r) for r in rows)
    longest = [max((len(r[i]) if i < len(r) else 0) for r in rows) for i in range(n)]
    weights = [min(max(w, 10), 60) for w in longest]
    total = sum(weights)
    return [CONTENT_WIDTH * w / total for w in weights]


def md_table(lines):
    rows = [cells(line) for i, line in enumerate(lines) if i != 1]
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    data = [[Paragraph(inline(c), S["th"]) for c in rows[0]]]
    data += [[Paragraph(inline(c), S["td"]) for c in r] for r in rows[1:]]
    t = Table(data, colWidths=widths_for(rows), repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f7f9fb")]),
    ]))
    return [t, Spacer(1, 7)]


def md_list(items, numbered):
    flow = [ListItem(Paragraph(inline(item), S["bullet"]),
                     leftIndent=15 if numbered else 13) for item in items]
    if numbered:
        return [ListFlowable(flow, bulletType="1", leftIndent=13,
                             bulletFontName=F["Bold"], bulletFontSize=9.5,
                             spaceAfter=7)]
    return [ListFlowable(flow, bulletType="bullet", start="•", leftIndent=11,
                         bulletFontName=F["Body"], bulletFontSize=9.5,
                         spaceAfter=7)]


def render(markdown):
    """The whole document as flowables: a cover, then every section."""
    flow, title_done = [], False
    para_buf, list_buf, list_numbered, table_buf, code_buf = [], [], False, [], None
    quote_buf = []

    def flush():
        nonlocal list_buf
        if quote_buf:
            flow.append(para(" ".join(quote_buf), "callout"))
            quote_buf.clear()
        if para_buf:
            flow.append(para(" ".join(para_buf)))
            para_buf.clear()
        if list_buf:
            flow.extend(md_list(list_buf, list_numbered))
            list_buf = []
        if table_buf:
            flow.extend(md_table(table_buf))
            table_buf.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if code_buf is not None:
            if stripped.startswith("```"):
                flow.append(code(code_buf))
                code_buf = None
            else:
                code_buf.append(line)
            continue
        if stripped.startswith("```"):
            flush()
            code_buf = []
            continue
        if stripped.startswith("|"):
            if para_buf or list_buf:
                flush()
            table_buf.append(stripped)
            continue
        if table_buf:
            flush()
        if not stripped or stripped.startswith("<!--"):
            flush()
            continue
        if stripped == "---":
            flush()
            continue
        if stripped.startswith("# "):
            flush()
            flow += [Spacer(1, 30 * mm),
                     para("CMU CRICKET SIMULATOR · PLAYER GUIDE", "cover_tag"),
                     para(stripped[2:], "title"),
                     HRFlowable(width="100%", thickness=2, color=ACCENT,
                                spaceBefore=8, spaceAfter=12)]
            title_done = True
            continue
        if stripped.startswith("## "):
            flush()
            heading = stripped[3:]
            if title_done and "Contents" in heading:
                flow.append(Spacer(1, 6 * mm))
            elif "1. Getting started" in heading:
                flow.append(PageBreak())
            else:
                flow.append(CondPageBreak(55 * mm))
            flow.append(para(heading, "h1"))
            continue
        if stripped.startswith("### "):
            flush()
            flow.append(para(stripped[4:], "h2"))
            continue
        if stripped.startswith(">"):
            if not quote_buf:
                flush()
            quote_buf.append(stripped.lstrip(">").strip())
            continue
        if quote_buf:
            flush()
        bullet = re.match(r"^[*-]\s+(.*)", stripped)
        number = re.match(r"^\d+\.\s+(.*)", stripped)
        if bullet or number:
            if para_buf:
                flow.append(para(" ".join(para_buf)))
                para_buf.clear()
            numbered = bool(number)
            if list_buf and numbered != list_numbered:
                flush()
            list_numbered = numbered
            list_buf.append((bullet or number).group(1))
            continue
        if list_buf and raw[:1] == " ":
            list_buf[-1] += " " + stripped          # a wrapped list item
            continue
        if list_buf:
            flush()
        para_buf.append(stripped)
    flush()
    return flow


def main():
    with open(SOURCE, encoding="utf-8") as fh:
        markdown = fh.read()
    doc = SimpleDocTemplate(
        OUTPUT, pagesize=A4, title=TITLE, author="CricMaster Ultra",
        subject="Match types, tours and tournaments",
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=22 * mm)
    furniture = page_furniture(TITLE)
    doc.build(render(markdown), onFirstPage=furniture, onLaterPages=furniture)
    print(f"→ {os.path.relpath(OUTPUT, ROOT)}")


if __name__ == "__main__":
    main()
