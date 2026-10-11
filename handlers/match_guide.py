"""/matchguide — the complete match & tournament guide, as a file or one topic.

``/matchhelp`` teaches the Approach game and ``/tourhelp`` lists tournament
commands; neither answers "what kinds of match are there and how do I start
each one?". ``docs/Match-Types-Guide.md`` does, and this command hands it over:

  /matchguide            → the guide as a Markdown file, with 📕 PDF / 📑 Topics buttons
  /matchguide pdf        → the PDF (built by ``tools/make_match_guide.py``)
  /matchguide md         → the Markdown file
  /matchguide topics     → every topic as a button
  /matchguide <topic>    → one section, rendered in chat — ``letsplay``, ``sim``,
                           ``4`` (a section number) or a command (``lptour``)

**The Markdown file is the only source.** Each ``##`` section carries a
``<!-- topic: key alias … -->`` line, and the in-chat card is rendered from that
same section, so the file, the PDF and the card can never disagree.
``tests/test_match_guide.py`` checks every command the guide names is really
registered in ``bot.py``.
"""

import html
import logging
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CB = "mguide"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE_MD = os.path.join(_ROOT, "docs", "Match-Types-Guide.md")
GUIDE_PDF = os.path.join(_ROOT, "docs", "Match-Types-Guide.pdf")
FILE_NAMES = {"md": "CMU-Match-Guide.md", "pdf": "CMU-Match-Guide.pdf"}

_TOPIC_RE = re.compile(r"<!--\s*topic:\s*(.*?)\s*-->")
_NUM_RE = re.compile(r"^\D*?(\d+)\.\s+")

# Telegram file_ids of the uploaded guide, so a repeat send is a lookup rather
# than a re-upload. Keyed by kind plus the file's mtime: a redeploy with a new
# guide uploads it afresh.
_FILE_IDS = {}


# ════════════════════════════════════════════════════════════════════
# Parsing the guide (pure — no Telegram)
# ════════════════════════════════════════════════════════════════════

def load_guide(path=GUIDE_MD):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def parse_sections(text):
    """``[{'keys', 'title', 'number', 'lines'}]`` for every ``##`` section that
    carries a topic line, in file order."""
    sections, current = [], None
    in_code = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
        if not in_code and line.startswith("## "):
            current = {"title": line[3:].strip(), "keys": [], "lines": []}
            m = _NUM_RE.match(current["title"])
            current["number"] = int(m.group(1)) if m else None
            sections.append(current)
            continue
        if current is None:
            continue
        m = _TOPIC_RE.search(line) if not in_code else None
        if m:
            current["keys"] = m.group(1).lower().split()
            continue
        current["lines"].append(line)
    return [s for s in sections if s["keys"]]


def find_section(sections, query):
    """The section a user's word points at, or None.

    Tries, in order: a topic key or alias, a section number, a key that starts
    with the word, then the first section that names ``/<word>`` as a command.
    """
    q = (query or "").strip().lower().lstrip("/")
    if not q:
        return None
    for s in sections:
        if q in s["keys"]:
            return s
    if q.isdigit():
        for s in sections:
            if s["number"] == int(q):
                return s
    for s in sections:
        if any(k.startswith(q) for k in s["keys"]):
            return s
    needle = re.compile(r"`/" + re.escape(q) + r"(?![a-z0-9_])")
    for s in sections:
        if needle.search("\n".join(s["lines"])):
            return s
    return None


def _inline(text):
    """One line of Markdown → Telegram HTML (code, bold, italic, links)."""
    out = []
    for part in re.split(r"(`[^`]+`)", text):
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append("<code>" + html.escape(part[1:-1], quote=False) + "</code>")
            continue
        part = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", part)
        part = html.escape(part, quote=False)
        part = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", part)
        part = re.sub(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])", r"<i>\1</i>", part)
        out.append(part)
    return "".join(out)


def _cells(row):
    row = row.strip().strip("|")
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def _table_lines(rows):
    header = _cells(rows[0])
    body = [_cells(r) for r in rows[2:]] if len(rows) > 2 else []
    lines = []
    for cells in body:
        if not cells or not cells[0]:
            continue
        first = _inline(cells[0])
        if "<b>" not in first:
            first = f"<b>{first}</b>"
        rest = cells[1:]
        if len(header) <= 2:
            tail = " — ".join(_inline(c) for c in rest if c)
        else:
            tail = " · ".join(
                (f"{_inline(header[i + 1])}: " if i else "") + _inline(c)
                for i, c in enumerate(rest) if c and i + 1 < len(header))
        lines.append(f"• {first}" + (f" — {tail}" if tail else ""))
    return lines


def render_blocks(lines):
    """Markdown lines → a list of HTML blocks for ``chunk_blocks``."""
    blocks, para, table, code = [], [], [], None

    def flush_para():
        if para:
            blocks.append("\n".join(para))
            para.clear()

    def flush_table():
        if table:
            blocks.append("\n".join(_table_lines(table)))
            table.clear()

    for raw in lines:
        line = raw.rstrip()
        if code is not None:
            if line.startswith("```"):
                blocks.append("<pre>" + html.escape("\n".join(code), quote=False) + "</pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.startswith("```"):
            flush_para(); flush_table()
            code = []
            continue
        if line.startswith("|"):
            flush_para()
            table.append(line)
            continue
        flush_table()
        stripped = line.strip()
        if not stripped or stripped == "---" or stripped.startswith("<!--"):
            flush_para()
            continue
        if stripped.startswith("#"):
            flush_para()
            blocks.append("<b>" + _inline(stripped.lstrip("#").strip()) + "</b>")
            continue
        if stripped.startswith(">"):
            para.append("<i>" + _inline(stripped.lstrip(">").strip()) + "</i>")
            continue
        if stripped.startswith(("* ", "- ")):
            para.append("• " + _inline(stripped[2:]))
            continue
        if para and not re.match(r"\d+\.\s", stripped):
            # A hard-wrapped continuation of the line above, not a new line.
            para[-1] += " " + _inline(stripped)
        else:
            para.append(_inline(stripped))
    flush_para(); flush_table()
    if code:
        blocks.append("<pre>" + html.escape("\n".join(code), quote=False) + "</pre>")
    return [b + "\n" for b in blocks if b.strip()]


def render_section(section):
    """One section as ready-to-send HTML messages (each within Telegram's limit)."""
    from utils.message_chunks import chunk_blocks
    header = f"📘 <b>{_inline(section['title'])}</b>\n"
    footer = ("<i>Full guide: /matchguide · PDF: /matchguide pdf · "
              "all topics: /matchguide topics</i>")
    return chunk_blocks(render_blocks(section["lines"]), header=header, footer=footer)


def topic_list_text(sections):
    lines = ["📑 <b>Match Guide — topics</b>",
             "Tap one, or type <code>/matchguide &lt;topic&gt;</code>:", ""]
    for s in sections:
        lines.append(f"<code>{html.escape(s['keys'][0])}</code> — {_inline(s['title'])}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# Telegram
# ════════════════════════════════════════════════════════════════════

def _file_keyboard(kind):
    other = ("📄 Markdown", f"{CB}_md") if kind == "pdf" else ("📕 PDF", f"{CB}_pdf")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(other[0], callback_data=other[1]),
        InlineKeyboardButton("📑 Topics", callback_data=f"{CB}_list"),
    ]])


def _topics_keyboard(sections):
    rows, row = [], []
    for s in sections:
        label = re.sub(r"^\W*\d+\.\s*", "", s["title"])[:28]
        row.append(InlineKeyboardButton(label, callback_data=f"{CB}_t_{s['keys'][0]}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("📄 Markdown", callback_data=f"{CB}_md"),
                 InlineKeyboardButton("📕 PDF", callback_data=f"{CB}_pdf")])
    return InlineKeyboardMarkup(rows)


async def _send_file(message, kind):
    """Reply to ``message`` with the guide as ``kind`` ('md' or 'pdf')."""
    path = GUIDE_PDF if kind == "pdf" else GUIDE_MD
    if kind == "pdf" and not os.path.exists(path):
        await message.reply_text("📕 The PDF isn't built on this server yet — "
                                 "here is the Markdown guide instead.")
        kind, path = "md", GUIDE_MD
    caption = ("📘 <b>CMU Complete Match &amp; Tournament Guide</b>\n"
               "Every match type, every tour and tournament, and exactly how to "
               "start each one.\n"
               "<i>One topic in chat: /matchguide letsplay · /matchguide sim · "
               "/matchguide topics</i>")
    cache_key = (kind, os.path.getmtime(path))
    cached = _FILE_IDS.get(cache_key)
    if cached:
        try:
            await message.reply_document(document=cached, caption=caption,
                                         parse_mode="HTML",
                                         reply_markup=_file_keyboard(kind))
            return
        except Exception:
            logger.info("cached /matchguide file_id refused; re-uploading")
            _FILE_IDS.pop(cache_key, None)
    with open(path, "rb") as fh:
        sent = await message.reply_document(document=fh, filename=FILE_NAMES[kind],
                                            caption=caption, parse_mode="HTML",
                                            reply_markup=_file_keyboard(kind))
    doc = getattr(sent, "document", None)
    if doc is not None and getattr(doc, "file_id", None):
        _FILE_IDS[cache_key] = doc.file_id


async def _send_section(message, section):
    for part in render_section(section):
        await message.reply_text(part, parse_mode="HTML",
                                 disable_web_page_preview=True)


async def _send_topics(message, sections):
    await message.reply_text(topic_list_text(sections), parse_mode="HTML",
                             reply_markup=_topics_keyboard(sections),
                             disable_web_page_preview=True)


async def matchguide_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/matchguide [pdf|md|topics|<topic>]"""
    message = update.effective_message
    if message is None:
        return
    arg = " ".join(context.args or []).strip().lower()
    try:
        if arg in ("", "md", "markdown", "file"):
            await _send_file(message, "md")
            return
        if arg in ("pdf",):
            await _send_file(message, "pdf")
            return
        sections = parse_sections(load_guide())
        if arg in ("topics", "list", "index", "contents"):
            await _send_topics(message, sections)
            return
        section = find_section(sections, arg)
        if section is None:
            await message.reply_text(
                f"❓ No topic called <code>{html.escape(arg)}</code>.\n\n"
                + topic_list_text(sections), parse_mode="HTML",
                reply_markup=_topics_keyboard(sections))
            return
        await _send_section(message, section)
    except Exception:
        logger.exception("/matchguide failed")
        await message.reply_text("⚠️ Couldn't send the guide right now. Try again.")


async def matchguide_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mguide_md · mguide_pdf · mguide_list · mguide_t_<topic>"""
    q = update.callback_query
    data = q.data or ""
    message = q.message
    try:
        if data == f"{CB}_md":
            await q.answer("Sending the Markdown guide…")
            await _send_file(message, "md")
        elif data == f"{CB}_pdf":
            await q.answer("Sending the PDF…")
            await _send_file(message, "pdf")
        elif data == f"{CB}_list":
            await q.answer()
            await _send_topics(message, parse_sections(load_guide()))
        elif data.startswith(f"{CB}_t_"):
            section = find_section(parse_sections(load_guide()), data[len(CB) + 3:])
            if section is None:
                await q.answer("That topic is gone — try /matchguide topics",
                               show_alert=True)
                return
            await q.answer()
            await _send_section(message, section)
        else:
            await q.answer()
    except Exception:
        logger.exception("/matchguide button failed")
        try:
            await q.answer("Couldn't send that — try /matchguide", show_alert=True)
        except Exception:
            pass
