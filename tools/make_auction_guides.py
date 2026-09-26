"""Build the two Franchise Auction guides as PDFs.

    python tools/make_auction_guides.py

Writes ``docs/Franchise-Auction-Guide-Users.pdf`` (for franchise owners,
co-owners and anybody watching a lot) and
``docs/Franchise-Auction-Guide-Admins.pdf`` (for whoever runs one), both
committed so a room can be handed a file rather than a link.

The content lives here rather than in a Markdown file passed through a
converter, for one reason: these two guides are *different documents*, not two
renderings of ``docs/franchise-auction.md``. That file explains why the feature
is built the way it is, to whoever changes it next; an owner with thirty
seconds on the clock needs the number to type, and an admin needs the order to
type things in. Keeping them apart is what stops either from being a worse
version of the other.

Numbers that are per-auction (the purse, the clock, the caps) are written as
the shipped defaults and labelled as such, because every one of them is
editable per season and ``/arules`` prints what *this* auction actually uses.
Requires reportlab; the standard 14 fonts carry no rupee sign, so DejaVu Sans
is embedded from the system font path.
"""

from __future__ import annotations

import os
import sys
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "docs")

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
FONTS = {
    "Body": ("DejaVu", "DejaVuSans.ttf"),
    "Bold": ("DejaVu-Bold", "DejaVuSans-Bold.ttf"),
    "Italic": ("DejaVu-Oblique", "DejaVuSans-Oblique.ttf"),
    "Mono": ("DejaVu-Mono", "DejaVuSansMono.ttf"),
    "MonoBold": ("DejaVu-Mono-Bold", "DejaVuSansMono-Bold.ttf"),
}

INK = colors.HexColor("#14181f")
MUTED = colors.HexColor("#5b6472")
ACCENT = colors.HexColor("#0b6b3a")        # the auction's green
ACCENT_SOFT = colors.HexColor("#e8f3ec")
RULE = colors.HexColor("#d5dbe3")
CODE_BG = colors.HexColor("#f4f6f9")
WARN = colors.HexColor("#8a4b00")


HELVETICA = {"Body": "Helvetica", "Bold": "Helvetica-Bold",
             "Italic": "Helvetica-Oblique", "Mono": "Courier",
             "MonoBold": "Courier-Bold"}


def register_fonts():
    """Embed what DejaVu this host has, and report which codepoints it covers.

    Per file rather than all-or-nothing: Debian's ``fonts-dejavu-core`` ships no
    oblique sans, and an all-or-nothing registration would have that one missing
    file cost the guides their rupee sign as well. A role with no file of its own
    falls back to the nearest one that registered, so ``<i>`` renders upright —
    a typeface nobody notices, where ``Rs`` in place of the rupee sign is a
    document that looks wrong.
    """
    chosen, cmap = {}, set()
    for role, (name, filename) in FONTS.items():
        try:
            font = TTFont(name, os.path.join(FONT_DIR, filename))
        except Exception:
            continue
        pdfmetrics.registerFont(font)
        chosen[role] = name
        if role in ("Body", "Bold"):
            cmap |= set(font.face.charToGlyph)
    if "Body" not in chosen:                                  # pragma: no cover
        print(f"! no DejaVu under {FONT_DIR}; falling back to Helvetica, and "
              f"anything outside Latin-1 is transliterated.", file=sys.stderr)
        return HELVETICA, set(range(0x100))
    chosen.setdefault("Bold", chosen["Body"])
    chosen.setdefault("Italic", chosen["Body"])
    chosen.setdefault("Mono", chosen["Body"])
    chosen.setdefault("MonoBold", chosen["Bold"])
    pdfmetrics.registerFontFamily(
        chosen["Body"], normal=chosen["Body"], bold=chosen["Bold"],
        italic=chosen["Italic"], boldItalic=chosen["Bold"])
    return chosen, cmap


F, CMAP = register_fonts()

# What to write instead when the embedded font has no glyph for a character. A
# missing glyph is not blank in a PDF, it is a hollow box, so everything is
# resolved before it is drawn.
SUBSTITUTES = {
    "\u20b9": "Rs ",   # the rupee sign: the one character these guides cannot lose
    "\u2192": "->", "\u2191": "^", "\u2193": "v",
    "\u2014": "-", "\u2013": "-", "\u2022": "*",
    "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
    "\u00d7": "x", "\u2212": "-", "\u2264": "<=", "\u2265": ">=",
}
# Emoji label real buttons in the bot, so the guides name those buttons in words
# too ("Settings", "Console") and an emoji with no glyph is dropped rather than
# substituted: DejaVu carries almost none of them, and no monochrome emoji font
# is guaranteed on a build host. The variation selector goes the same way.
DROP = {0xFE0F, 0x200D}


def T(text, collapse=True):
    """Text with every character this document cannot draw resolved.

    Markup passes through untouched — everything inside a tag is ASCII.

    ``collapse`` tidies the gap a dropped emoji leaves behind, which is what
    prose wants and what a command block must not have: the columns in those
    are the layout, and a line silently narrowed by one space is a line that
    no longer lines up with the one above it.
    """
    out = []
    for ch in str(text):
        point = ord(ch)
        if point in DROP:
            continue
        if point < 0x80 or point in CMAP:
            out.append(ch)
        else:
            out.append(SUBSTITUTES.get(ch, ""))
    done = "".join(out)
    if not collapse:
        return done or " "
    return done.replace("  ", " ").strip() or " "


# ── Styles ──────────────────────────────────────────────────────────

def styles():
    base = getSampleStyleSheet()
    s = {}
    s["title"] = ParagraphStyle(
        "title", parent=base["Title"], fontName=F["Bold"], fontSize=27,
        leading=32, textColor=INK, alignment=TA_LEFT, spaceAfter=4)
    s["subtitle"] = ParagraphStyle(
        "subtitle", fontName=F["Body"], fontSize=12.5, leading=18,
        textColor=MUTED, spaceAfter=18)
    # keepWithNext: a heading alone at the foot of a page reads as the end of
    # the section above it, which is the one thing a heading must never do.
    s["h1"] = ParagraphStyle(
        "h1", fontName=F["Bold"], fontSize=16, leading=20, textColor=ACCENT,
        spaceBefore=17, spaceAfter=7, keepWithNext=1)
    s["h2"] = ParagraphStyle(
        "h2", fontName=F["Bold"], fontSize=11.7, leading=15, textColor=INK,
        spaceBefore=12, spaceAfter=4, keepWithNext=1)
    s["body"] = ParagraphStyle(
        "body", fontName=F["Body"], fontSize=9.9, leading=14.6, textColor=INK,
        spaceAfter=6.5)
    s["lead"] = ParagraphStyle(
        "lead", parent=s["body"], fontSize=11, leading=16.5, textColor=MUTED)
    s["bullet"] = ParagraphStyle(
        "bullet", parent=s["body"], spaceAfter=3.2, leading=14.2)
    s["code"] = ParagraphStyle(
        "code", fontName=F["Mono"], fontSize=8.9, leading=13.4, textColor=INK,
        backColor=CODE_BG, borderPadding=(7, 8, 7, 8), spaceBefore=4,
        spaceAfter=9, leftIndent=1, borderColor=RULE, borderWidth=0.6)
    s["note"] = ParagraphStyle(
        "note", parent=s["body"], fontName=F["Body"], fontSize=9.5,
        leading=14, textColor=WARN, backColor=colors.HexColor("#fff7ec"),
        borderPadding=(7, 8, 7, 8), borderColor=colors.HexColor("#f0d9b5"),
        borderWidth=0.6, spaceBefore=5, spaceAfter=9)
    s["callout"] = ParagraphStyle(
        "callout", parent=s["body"], fontSize=9.8, leading=14.4,
        backColor=ACCENT_SOFT, borderPadding=(7, 8, 7, 8),
        borderColor=colors.HexColor("#bfe0cd"), borderWidth=0.6,
        spaceBefore=5, spaceAfter=9)
    s["th"] = ParagraphStyle(
        "th", fontName=F["Bold"], fontSize=9.1, leading=12.4,
        textColor=colors.white)
    s["td"] = ParagraphStyle(
        "td", fontName=F["Body"], fontSize=9.1, leading=12.8, textColor=INK)
    s["tdmono"] = ParagraphStyle(
        "tdmono", fontName=F["Mono"], fontSize=8.5, leading=12.4,
        textColor=INK)
    s["cover_tag"] = ParagraphStyle(
        "cover_tag", fontName=F["Bold"], fontSize=9.6, leading=13,
        textColor=ACCENT, spaceAfter=10)
    s["footer"] = ParagraphStyle(
        "footer", fontName=F["Body"], fontSize=7.9, leading=10,
        textColor=MUTED, alignment=TA_CENTER)
    return s


S = styles()


# ── Flowable helpers ────────────────────────────────────────────────

def p(text, style="body"):
    return Paragraph(T(text), S[style])


def h1(text):
    return Paragraph(T(text), S["h1"])


def h2(text):
    return Paragraph(T(text), S["h2"])


def bullets(items, bullet="•"):
    bullet = T(bullet).strip() or "-"
    return ListFlowable(
        [ListItem(Paragraph(T(item), S["bullet"]), leftIndent=13,
                  value=bullet) for item in items],
        bulletType="bullet", start=bullet, leftIndent=11,
        bulletFontName=F["Body"], bulletFontSize=9.5, spaceAfter=7)


def steps(items):
    return ListFlowable(
        [ListItem(Paragraph(T(item), S["bullet"]), leftIndent=15)
         for item in items],
        bulletType="1", leftIndent=13, bulletFontName=F["Bold"],
        bulletFontSize=9.5, spaceAfter=7)


def code(lines):
    # Escaped: a command block is where ``<player>`` and ``<seconds>`` live,
    # and ReportLab's paragraph parser reads those as tags — dropping them
    # silently, so the guide would print every command without its argument.
    # Alignment is the point of these blocks, so spacing a substitution changed
    # the width of is held by non-breaking spaces.
    # KeepTogether: half a command block at the foot of a page is a command
    # somebody will type half of.
    return KeepTogether(Paragraph(
        "<br/>".join(escape(T(line, collapse=False)).replace(" ", "&nbsp;")
                     for line in lines), S["code"]))


def table(rows, widths, header=True, mono_first=True):
    """A bordered table whose first column is monospaced (commands)."""
    data = []
    for index, row in enumerate(rows):
        # Escaped for ``code()``'s reason — the first column of most of these
        # tables is a command, and half of those carry a <placeholder>.
        if header and index == 0:
            data.append([Paragraph(escape(T(cell)), S["th"]) for cell in row])
            continue
        cells = []
        for column, cell in enumerate(row):
            style = "tdmono" if (mono_first and column == 0) else "td"
            cells.append(Paragraph(escape(T(cell)), S[style]))
        data.append(cells)
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0,
              hAlign="LEFT")
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
    ]
    if header:
        commands += [
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f7f9fb")]),
        ]
    t.setStyle(TableStyle(commands))
    return t


def cover(title, subtitle, tag, whos_it_for, contents):
    """The first page: what this is, who it is for, and what is in it."""
    flow = [Spacer(1, 26 * mm),
            p(tag, "cover_tag"),
            p(title, "title"),
            HRFlowable(width="100%", thickness=2, color=ACCENT,
                       spaceBefore=8, spaceAfter=12),
            p(subtitle, "subtitle"),
            p(whos_it_for, "callout"),
            Spacer(1, 6 * mm),
            h2("What is in here"),
            bullets(contents, bullet="–"),
            Spacer(1, 8 * mm),
            p("<i>Every number in this guide is the shipped default. All of "
              "them are set per auction, and <font face=\"%s\">/arules</font> "
              "in the auction's group prints what this one actually uses.</i>"
              % F["Mono"], "body"),
            PageBreak()]
    return flow


def page_furniture(title):
    """A footer with the guide's name and the page number."""
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont(F["Body"], 7.9)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, 12 * mm, title)
        canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, str(canvas.getPageNumber()))
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(20 * mm, 16 * mm, A4[0] - 20 * mm, 16 * mm)
        canvas.restoreState()
    return draw


def build(filename, title, flow):
    path = os.path.join(OUT_DIR, filename)
    doc = SimpleDocTemplate(
        path, pagesize=A4, title=title,
        author="CricMaster Ultra", subject="Franchise Auction",
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=22 * mm)
    doc.build(flow, onFirstPage=page_furniture(title),
              onLaterPages=page_furniture(title))
    print(f"→ {os.path.relpath(path, ROOT)}")
    return path


def C(text):
    """Inline code."""
    return f'<font face="{F["Mono"]}" size="9">{text}</font>'


def B(text):
    return f"<b>{text}</b>"


# ════════════════════════════════════════════════════════════════════
# The user guide — for owners, co-owners and everybody watching
# ════════════════════════════════════════════════════════════════════

def user_guide():
    flow = cover(
        "Franchise Auction",
        "The owner's guide: how to bid, what your purse can reach, and every "
        "command the room can use.",
        "CRICMASTER ULTRA · PLAYER GUIDE",
        f"{B('Who this is for')} — franchise owners and co-owners who will "
        f"be bidding, and anybody in the auction group who wants to follow it. "
        f"You need no admin rights for anything in this guide. If you are "
        f"{B('running')} the auction, read the admin guide instead.",
        ["Money: crore, lakh, and why a bare number is crore",
         "Bidding: the one command you will actually use",
         "Reading the board, and the three views worth knowing",
         "What your purse can really reach (the max-bid rule)",
         "Every rule that can refuse your bid, and what it means",
         "Retention and Right To Match, if your auction uses them",
         "Focus mode: why the group answers nothing else while a lot is live",
         "Homework: what to read in a DM the night before",
         "The whole command list, on one page"])

    # ── 1 ─────────────────────────────────────────────────────────────
    flow += [
        h1("1. The shape of an auction"),
        p("An admin builds a player pool and a field of franchises. Each "
          "franchise has an " + B("owner") + " and any number of " + B("co-owners")
          + ", and a " + B("purse") + " to spend. Players then go on the block "
          "one at a time, as " + B("lots") + ", in sets; the room bids; the "
          "highest bid when the clock runs out wins, and the price comes off "
          "that franchise's purse. When the pool is finished the admin "
          "publishes the squads, and the auction becomes a real league you "
          "play in."),
        p("Three things are worth knowing before your first lot:"),
        bullets([
            B("Auction money is its own currency.") + " A purse cannot buy "
            "anything else in the bot, and your coins are worth nothing at an "
            "auction. The two never meet.",
            B("You bid for your franchise, not for yourself.") + " An owner and "
            "a co-owner are equals: either can bid, and both spend the same "
            "purse.",
            B("Bidding only works in the auction's group.") + " That is "
            "deliberate — a bid nobody in the room saw is how a price "
            "gets disputed. Reading works in a DM too.",
        ]),

        h1("2. Money: a bare number is crore"),
        p("Every amount is read as " + B("crore") + " unless you say lakh out "
          "loud. This is how the room talks, so it is how the bot listens:"),
        code(["/bid 15        →  ₹15 Cr",
              "/bid 1.5       →  ₹1.5 Cr  (₹150 lakh)",
              "/bid 75L       →  ₹75 lakh",
              "/bid 75 lakh   →  ₹75 lakh"]),
        p("Every prompt the bot prints shows the next minimum in the same form, "
          "so the commonest move is typing back the number it just showed you."),

        h1("3. Bidding"),
        p(B("Bare ") + C("/bid") + B(" bids the next minimum.") + " That is the "
          "one you will use most, and the one that cannot be fat-fingered with "
          "ten seconds on the clock. " + C("/bd") + " is the short alias."),
        code(["/bid           →  the next legal minimum, whatever it is",
              "/bid 2.4       →  ₹2.4 Cr, if that clears the minimum",
              "/bd 2.4        →  the same thing, fewer letters"]),
        p(B("The buttons are always at the bottom of the chat.") + " Every "
          "new bid message (“💥 Mumbai bids ₹2.2 Cr · ⬆️ Outbids Chennai”), "
          "the player's card and the countdown carry " + B("quick-bid buttons")
          + " — the minimum and one step above it — and the older message "
          "loses its buttons, so you never tap a stale price. The pinned board "
          "has them too. They are shared: each press is checked against the "
          "franchise " + B("you") + " own, and the exact price is baked into "
          "the button."),
        p(B("💼 My Purse") + " and " + B("📊 Status") + " sit under the bid "
          "buttons. They answer with a private popup — your purse, max bid, "
          "squad, overseas count and RTM cards, or who leads the lot and what "
          "beats it — and add nothing to the chat."),
        p(B("Dot shortcuts") + " work in the auction group: " + C(".bid")
          + ", " + C(".bid 2cr") + ", " + C(".purse") + ", " + C(".squad")
          + ", " + C(".board") + ", " + C(".lb") + ", " + C(".mybids") + "."),
        p(B("Many teams can bid at once.") + " There is no pause for the room "
          "after a bid. If you and another team send a bare " + C("/bid")
          + " at the same instant, both land, in order. Spam is stopped per "
          "person: one bid attempt a second, and a burst of taps pauses only "
          "your bidding for 15 seconds."),
        p(B("The clock warns you before the hammer.") + " With the default "
          "clock (60 40 20 10 5) a player opens with 60 seconds. Any bid with "
          "less than 40 seconds left puts the clock back to 40. At 20 seconds "
          "the room gets the " + B("1st warning") + " — “Selling X to Team for "
          "₹…” — at 10 seconds the " + B("2nd warning") + ", and the last 5 "
          "are counted down in one message, then SOLD. Both warnings carry the "
          "bid buttons. " + C("/arules") + " shows this auction's numbers."),
        p(B("A bid that works gets no reply.") + " Forty bids inside one lot "
          "would be forty messages on top of a board you are trying to read. "
          "Your message gets a reaction, and the board carries the new price "
          "within a tick (two seconds)."),
        p(B("A bid that is refused always answers") + ", and always names the "
          "number that would have worked instead. On a thirty-second clock, "
          "“invalid bid” is useless."),
        p("The one thing you cannot do is " + B("bid against yourself") + ": if "
          "you are already the top bidder, a raise is refused. (The single "
          "exception is a Right To Match window, where the top bidder is "
          "invited to raise their own bid once — see section 8.)"),

        h2("One pair of hands per franchise, per lot"),
        p("Owner and co-owners are equals — but not at the same moment. "
          + B("Whoever bids first for a franchise holds that lot") + ", and the "
          "other one is refused by name until the next lot:"),
        p("“Alice is bidding for Mumbai on this lot — only one of you at a "
          "time, or you end up raising each other. The next lot is open to "
          "either of you.”", "callout"),
        p("Two co-owners bidding the same player is not two tactics; it is one "
          "franchise racing itself up its own price, and the loser of that race "
          "is always the franchise. The claim clears the moment the lot does, "
          "so the next player is open to either of you again — and if an admin "
          "undoes the bid that claimed it, so does the claim."),

        h2("Direct bids, if your auction allows them"),
        p("Some rooms run a strict ladder. If your admin has turned "
          + B("direct bids off") + ", a typed amount is refused, and the "
          "message names the number it would have been:"),
        code(["/bid 12   →  Direct bids are off in this auction",
              "          →  Send /bid on its own to bid ₹2.4 Cr"]),
        p("Bare " + C("/bid") + " and the board's buttons still work, so you "
          "can always bid — one step at a time. " + C("/arules") + " says which "
          "way your auction is set."),

        h1("4. The clock"),
        bullets([
            "A lot stays on the block for " + B("30 seconds") + " by default, "
            "and " + B("every bid restarts it") + ".",
            "At 10 seconds left the room hears " + B("going once") + ", at 5 "
            "seconds " + B("going twice") + ".",
            B("Anti-snipe:") + " a bid landing inside the last few seconds "
            "pushes the clock back out, a limited number of times per lot. "
            "Many rooms set it to “any bid in the last 10 seconds gives "
            "everybody 10 more”. " + C("/arules") + " prints the exact "
            "numbers your auction uses.",
            "The deadline is decided by the bot, not by the announcement: a bid "
            "that arrives after time is refused by the command itself, even if "
            "the room has not been told yet.",
        ]),
        p("An admin can add time with " + C("/aextend") + " — a dropped "
          "connection is not a snipe, so that does not spend the anti-snipe "
          "budget."),
    ]

    # ── 5 ─────────────────────────────────────────────────────────────
    flow += [
        h1("5. What your purse can actually reach"),
        p("Your purse is not your maximum bid. The auction holds back enough to "
          "fill the squad slots you still owe, so you cannot spend your way "
          "into an illegal squad:"),
        code(["slots you would still owe  =  min squad  −  (squad + 1)",
              "reserve                    =  slots × cheapest base price",
              "max bid                    =  purse remaining  −  reserve"]),
        p("So a franchise one player short of the squad minimum may spend its "
          "last rupee — there are no slots left to protect. A franchise "
          "five short cannot. " + B("This number is printed for you") + ": on "
          "the board, in " + C("/apurse") + " and in " + C("/asquad") + ". It "
          "is not a suggestion; a bid above it is refused."),
        KeepTogether([
            p(B("Why it is a rule and not advice"), "h2"),
            p("A minimum squad size that was only checked at the end would be a "
              "complaint nobody could act on — the money would already be "
              "gone. Refusing the bid while there is still a slot to fix the "
              "problem with is the only version you can plan around."),
        ]),

        h1("6. Everything that can refuse a bid"),
        p("In the order they are checked, each with its own message naming the "
          "number that would have worked:"),
        table([
            ["Refusal", "What it means"],
            ["Not live", "The auction is paused, nothing is on the block, or "
                         "the clock has run out."],
            ["You lead already", "You are the top bidder. You cannot raise "
                                 "your own bid."],
            ["Too small", "Below the base price, or below the standing bid plus "
                          "the next increment. The message prints the minimum."],
            ["Squad full", "Winning this lot would take you past the maximum "
                           "squad size."],
            ["Purse", "You do not have the money."],
            ["Reachability", "You have the money, but winning at that price "
                             "would leave you unable to fill your remaining "
                             "minimum slots at base price. See section 5."],
            ["Overseas cap", "Too many players from outside the auction's home "
                             "country."],
            ["Role rule", "Either a role ceiling (an eighth bowler in a "
                          "seven-bowler squad) or a role minimum you would no "
                          "longer be able to reach."],
        ], widths=[35 * mm, None], mono_first=False),
        p(B("Bid increments") + " step up as the price climbs — ten lakh on "
          "top of ₹18 Cr is noise, and forty of those is a room falling asleep. "
          + C("/arules") + " prints the whole ladder; a bare " + C("/bid")
          + " always uses the right step for the current price."),

        h1("7. The views worth knowing"),
        p("None of these are in the slash menu (both menus are full at "
          "Telegram's 100-command ceiling), so they are listed here and behind "
          + C("/ainfo") + "'s buttons."),
        p(B("Every card is yours, and closes.") + " Admin answers included — "
          "a card you asked for carries "
          "a ❌ Close button that takes it out of the chat — useful in a group "
          "where the board is the message everyone is trying to read — and only "
          "you can press it. The " + C("/ainfo") + " menu and the Sets card work "
          "the same way: they answer the person who sent the command, so if "
          "somebody else's card ignores your taps, send the command yourself "
          "for a copy of your own. The " + B("pinned board") + " is the "
          "exception on both counts — its quick-bid buttons belong to the whole "
          "room, and it cannot be closed."),
        table([
            ["Command", "What you get"],
            ["/aboard", "A personal copy of the live board, with quick-bid "
                        "buttons. In a DM it comes without the buttons, since a "
                        "bid outside the group is refused anyway."],
            ["/ainfo", "Where the auction stands, plus a button for every view "
                       "below. Start here if you remember nothing else."],
            ["/arules", "Every number this auction runs by: purse, squad and "
                        "overseas caps, role limits, base prices band by band, "
                        "the bid ladder, the clock, retention, RTM, picks."],
            ["/apurse", "Every franchise's purse, max bid and squad count (and "
                        "RTM cards left). /apurse Mumbai for one of them."],
            ["/asquad", "Your own squad as a table, with purse and max bid. "
                        "/asquad Mumbai for somebody else's."],
            ["/asets", "Every set and its state. /anextset for the next set's "
                       "players, /anextplayer for who is up next."],
            ["/asoldlist", "Everyone sold so far, set by set."],
            ["/aunsoldlist", "Everyone unsold, including the accelerated set."],
            ["/aretlock", "Who has kept whom, and whether the retention window "
                          "is still open."],
            ["/apicks", "The expansion pick order and whose turn it is."],
        ], widths=[30 * mm, None]),
    ]

    # ── 8 ─────────────────────────────────────────────────────────────
    flow += [
        h1("8. Retention and Right To Match"),
        p("Not every auction uses these. " + C("/arules") + " shows them only "
          "when yours does."),
        p(B("Retention") + " happens before the first lot opens: a franchise "
          "keeps some of the players it already had, at a price off the top of "
          "its purse. An admin " + B("offers") + " the retention and " + B("your "
          "franchise accepts it") + " — the ✅ Accept button on that card "
          "works only for that franchise's owner and co-owners. Nobody else can "
          "press it, admins included. A retained player is on your squad from "
          "that moment, and counts against every cap."),
        p(B("Right To Match (RTM)") + " lets last season's franchise take a "
          "player back at the price the room set. It runs as three short "
          "windows on one clock:"),
        steps([
            "The " + B("holder") + " (last season's franchise) is asked whether "
            "it wants to exercise the right at all.",
            "If it does, the " + B("top bidder") + " gets one last raise — "
            "and this is the one moment you may raise your own bid — or "
            "may stand.",
            "The " + B("holder") + " then matches that final number, or lets "
            "the player go.",
        ]),
        p("You answer with one verb, whichever question is on the clock:"),
        code(["/artm yes      →  exercise it / match it",
              "/artm no       →  decline / let him go",
              "/artm stand    →  (top bidder) no further raise"]),
        p("Every window times out to the " + B("safe") + " answer — "
          "decline, stand, decline — so a silent owner can never wedge the "
          "auction. The prompt names the owner and carries buttons, so you do "
          "not have to remember the command."),

        h1("9. Focus mode: the group runs one thing at a time"),
        p("While the auction is " + B("live or paused") + ", its group answers "
          "auction commands and " + B("nothing else") + ". Try anything else and "
          "you get one line back:"),
        p("“🔨 " + B("Season 2") + " is on the block. Only auction commands "
          "work in this group while it runs — " + C("/bid") + ", "
          + C("/ainfo") + ", " + C("/aboard") + ", " + C("/apurse") + " and the "
          "rest of the a-commands. Everything else still works in a DM with "
          "me.”", "callout"),
        p("The reason is the board: a thirty-second clock and a pinned message "
          "being edited every two seconds cannot share a room with a "
          + C("/claim") + " card, and a bid that scrolls past unseen is how a "
          "price gets disputed."),
        bullets([
            B("Talking is never blocked.") + " Shout all you like — half of "
            "an auction is the room.",
            B("Only this group.") + " Your DMs, and every other group, are "
            "untouched. The whole rest of the bot is one DM away.",
            B("Non-auction buttons") + " tapped in the group are refused the "
            "same way, as a pop-up alert.",
            B("Nothing is locked before it starts.") + " While the auction is "
            "still being set up, the group behaves normally.",
        ]),
        p("An admin can turn it off for a particular auction with " + C("/afocus "
          "off") + "; " + C("/afocus") + " on its own says which way it is set."),

        h1("10. Homework: what to read the night before"),
        p("Every read-only view answers " + B("before") + " the auction starts, "
          "and answers in a " + B("DM") + " — because working out what your "
          "purse can reach, what the sets hold and what retention cost you is "
          "homework, and doing it in the group means doing it in front of the "
          "people you are about to bid against."),
        code(["/arules        every number the auction will run by",
              "/asets         what the pool holds, set by set",
              "/apurse        every purse, including what retention cost",
              "/asquad        who you already have",
              "/ainfo         how far setup has got"]),
        p("In a DM the bot works out which auction you mean from the franchise "
          "you own. If you have teams in two auctions at once it will say so "
          "and name them, rather than guess — ask in that auction's group "
          "instead, where the question is unambiguous."),
        p(B("What does not work in a DM:") + " " + C("/bid") + " and "
          + C("/artm") + ". Both belong to the room."),
    ]

    # ── 11 ────────────────────────────────────────────────────────────
    flow += [
        h1("11. Every command you can use"),
        table([
            ["Command", "Who", "What it does"],
            ["/bid [amount] · /bd", "owner + co-owners",
             "Bid. Bare /bid bids the next minimum."],
            ["/artm yes|no|stand", "the holder / top bidder",
             "Answer an open Right To Match."],
            ["/aboard", "anyone", "The live board, with quick-bid buttons."],
            ["/ainfo · /amenu", "anyone",
             "Where the auction stands, and a button per view."],
            ["/arules · /asettings", "anyone",
             "Every rule and number this auction uses."],
            ["/apurse [team]", "anyone",
             "Every purse, max bid and squad count."],
            ["/asquad [team]", "anyone", "A squad as a table."],
            ["/asets", "anyone", "Every set and its state."],
            ["/anextset", "anyone", "The next set's players."],
            ["/anextplayer", "anyone", "Who is up next."],
            ["/asoldlist", "anyone", "Everyone sold, set by set."],
            ["/aunsoldlist", "anyone", "Everyone unsold."],
            ["/aleaderboard · /alb", "anyone",
             "Franchises ranked by spend, with each one's top buy."],
            ["/amybids [team]", "anyone",
             "Every player you bid on — won, lost or live."],
            [".bid .purse .squad …", "as the command",
             "Dot shortcuts, in the auction group."],
            ["/aretlock", "anyone", "Who kept whom; the retention window."],
            ["/apicks", "anyone", "The expansion pick order, and whose turn."],
        ], widths=[38 * mm, 30 * mm, None]),
        p("If you forget all of it: " + C("/ainfo") + " puts every one of those "
          "behind a button.", "callout"),

        h1("12. Quick answers"),
        table([
            ["“Why was my bid refused?”",
             "The refusal says which rule, and prints the number that would "
             "have worked. Section 6 lists all of them."],
            ["“The board says my max bid is less than my purse.”",
             "That is the reachability reserve — money held back to fill "
             "the squad slots you still owe. Section 5."],
            ["“My /claim did not work in the group.”",
             "Focus mode: the auction is live. Use a DM. Section 9."],
            ["“Can my co-owner bid for us?”",
             "Yes — but one of you at a time on a lot. Whoever bids first holds "
             "it; the next lot is open to either of you. Section 3."],
            ["“/bid 12 was refused, but /bid worked.”",
             "Direct bids are off in that auction: every raise is one step. "
             "Section 3."],
            ["“Somebody else's card ignores my taps.”",
             "Cards belong to whoever asked for them. Send the command "
             "yourself. Section 7."],
            ["“Can an admin bid for me if my phone dies?”",
             "Yes, from the website console — and it is announced as an "
             "admin's bid, not as yours."],
            ["“I missed a player I needed.”",
             "Unsold players usually come back once, as the ⚡ Accelerated set, "
             "before the auction finishes. /aunsoldlist has them."],
            ["“My squad is short at the end.”",
             "Squads under the minimum are topped up for free from whoever is "
             "still unsold, inside the squad and overseas caps."],
        ], widths=[52 * mm, None], header=False, mono_first=False),
        Spacer(1, 8),
        p("<i>Nothing in this guide needs admin rights. The auction's own "
          "numbers — purse, clock, caps, ladders — are whatever your "
          "admin set, and " + C("/arules") + " always prints the live "
          "ones.</i>"),
    ]
    return flow


# ════════════════════════════════════════════════════════════════════
# The admin guide — for whoever runs one
# ════════════════════════════════════════════════════════════════════

def admin_guide():
    flow = cover(
        "Franchise Auction",
        "The admin's guide: setting one up, running the room, and getting the "
        "squads into a league.",
        "CRICMASTER ULTRA · ADMIN GUIDE",
        f"{B('Who this is for')} — bot admins and {B('auction admins')} "
        f"(appointed with {C('/aadminadd')}, able to run every auction command "
        f"and no other admin command in the bot). Owners and co-owners want the "
        f"player guide; everything in it still applies to you.",
        ["The one-page running order, start to finish",
         "The setup page, fold by fold",
         "Base prices, squad rules and role limits",
         "The franchise field — and it as a JSON file",
         "Building the pool, and ordering the sets",
         "Running the room: the commands and the web console",
         "Focus mode: the group locked to auction commands",
         "The opening purse, direct bids, and one bidder per franchise",
         "Retention, Right To Match and expansion picks",
         "Money: correcting a purse, and the ledger behind it",
         "Finishing: publishing, cloning, cancelling",
         "Troubleshooting, and the mistakes that are recoverable",
         "Every admin command, grouped"])

    flow += [
        h1("1. The running order"),
        p("Everything below expands on this. If you have run a draft, the shape "
          "is familiar: the event is bound to one group, the commands refuse "
          "anywhere else, and the admin gate answers whoever typed it rather "
          "than pretending the command does not exist."),
        code(["In the auction's group:",
              "   /anew Season 2            create it, bound to this group",
              "",
              "On the website  (Admin → Tournament Panel → \U0001f528 "
              "Franchise Auctions):",
              "   ⚙️  Settings        purse, seconds per lot, anti-snipe",
              "   \U0001f9e9  Squad rules      squad size, overseas, role min/max",
              "   \U0001f3f7  Base prices      one price per rating range",
              "   \U0001f3db  Franchises       name, owner id, co-owners, purse",
              "   \U0001f4cb  Auction pool     build the pool, order the sets",
              "",
              "Back in the group, if the auction uses them:",
              "   /artmset 2 45 200         Right To Match rules",
              "   /aretain Mumbai | Virat Kohli | 18",
              "   /aretlock on              close the retention window",
              "   /apickset 3               expansion picks, if any",
              "",
              "Then:",
              "   /atimer 60 40 20 10 5     open 60s · bid under 40s → 40s",
              "                             · warnings 20s & 10s · count 5",
              "   /adirect off              (optional) ladder only, no typed bids",
              "   /acall Auction starts in 10 minutes",
              "   /astart",
              "",
              "At the end:",
              "   /apublish                 squads → a Challenge League",
              "   /aclone Season 3          next season, same rules and field"]),
        p(B("The reference card is always one command away:") + " " + C("/auction")
          + " (or " + C("/adminhelp") + ") prints every admin command, section "
          "by section, in the group or in your DM.", "callout"),

        h1("2. Creating one, and binding it to a group"),
        p(C("/anew Season 2") + " creates an auction " + B("and") + " binds it to "
          "the group you typed it in. " + C("/abind Season 2") + " binds an "
          "existing one, which is how you move an auction created on the "
          "website into its room."),
        bullets([
            "One group holds " + B("one") + " auction: two would make every "
            "command in that room ambiguous.",
            "A group whose last auction is " + B("finished or cancelled") + " is "
            "free — binding a new season to it releases the old one and "
            "says so in the room. A " + B("running") + " auction keeps its group.",
            "An auction with no group cannot start: " + C("/astart") + " says so.",
        ]),
        p(B("Before it opens, every read-only view already answers") + " — "
          "and answers in a DM. Owners do their homework against "
          + C("/arules") + ", " + C("/asets") + " and " + C("/apurse") + " the "
          "night before, which is the point of setting the numbers early."),

        h1("3. The setup page, fold by fold"),
        p("The page is a dozen rule-sets deep and a visit touches one or two of "
          "them, so each is a " + B("closed fold with its live numbers in the "
          "summary") + " — the status, the purse, how many franchises, what "
          "the role rule says — and a strip at the top reads the whole "
          "auction back without opening anything."),
        table([
            ["Fold", "What it holds"],
            ["⚙️ Settings", "Name, the group chat id, seconds per lot, the "
                            "opening purse (which moves every franchise — see "
                            "below), the three anti-snipe numbers, and the "
                            "focus-mode and direct-bid switches."],
            ["🧩 Squad rules", "Squad minimum and maximum, the home country and "
                               "the overseas cap, and a minimum and maximum for "
                               "each of the four roles."],
            ["🏷 Base prices", "A list of rating ranges, one price each, with "
                               "➕ Add range."],
            ["🏛 Franchises", "One fold per franchise: name, city, logo, owner "
                              "and co-owners, and its purse. Delete asks for "
                              "the name back."],
            ["📦 The field, as a file", "Download the franchises as JSON, and "
                                        "upload one back."],
            ["📋 Auction pool", "The set queue, the two set builders and the "
                                "catalogue filter."],
            ["🔨 Console", "The live auction, driven with buttons."],
        ], widths=[38 * mm, None], mono_first=False),

        h1("4. Base prices by rating range"),
        p("A row reads top first, the way the room says it: " + B("96 down to 92 "
          "→ ₹2 Cr") + ". The highest band's top is left blank, meaning "
          "“and up” — every ladder needs one of those, or its best "
          "cards fall through to the floor."),
        bullets([
            B("Overlaps are settled by the dearer band") + " (the first band a "
            "rating fits wins), rather than refused: an overlap is somebody "
            "narrowing a rung.",
            B("Gaps are named on save") + ", because a gap is silent — those "
            "cards would simply be built at the floor and nobody would find out "
            "until the pool was priced.",
            B("The price is stamped onto the lot when the lot is built") + ", not "
            "looked up when it opens. Editing the ladder afterwards cannot move "
            "a price a lot already went on the block at, and cannot move one "
            "mid-auction.",
            "One player can be overridden from the pool table, while the lot is "
            "still queued.",
        ]),
        p(C("/arules") + " prints the same ladder to the room, band by band."),
    ]

    flow += [
        h1("5. Squad rules and the two ends of the role rule"),
        p("Both ends are saved together, because they constrain each other: a "
          "minimum above its own maximum, or minimums adding up past the squad "
          "limit, describes a squad nobody could ever finish. That is refused "
          "on the page rather than half-way through a live lot."),
        code(["min / max squad      how many players a squad may hold",
              "home country         anybody else counts as overseas",
              "max overseas         the cap on those",
              "role min / max       per role: Batsman, Bowler,",
              "                     All-rounder, Wicket Keeper"]),
        p("They are enforced differently, and they have to be. A " + B("maximum")
          + " is a fact about the squad in front of you, so a bid that would "
          "break it is refused on a plain count. A " + B("minimum") + " is a "
          "promise about a squad that does not exist yet, so it is enforced as "
          + B("reachability") + ": refused while there is still a slot to fix "
          "the problem with, never afterwards. Leave a role blank for “no "
          "rule”; a typed " + C("0") + " maximum is a real rule (“no "
          "specialist keepers here”)."),
        p("Owners see the whole thing in " + C("/arules") + ", and the board "
          "prints each franchise's max bid, so nobody meets one of these rules "
          "for the first time by being refused by it."),

        h1("6. The franchise field"),
        p("Name, city, logo, owner and co-owners, and a purse that defaults to "
          "the season's. The owner is a " + B("Telegram id, not an account in "
          "the bot") + ": an admin builds the field from a list, and some of "
          "those people have never messaged the bot. Owner and co-owners are "
          "equals for every check " + C("/bid") + " makes."),
        bullets([
            C("/aco Mumbai | 123456789") + " adds a co-owner from the group.",
            C("/acall [message]") + " tags every owner and co-owner — the "
            "ten-minute warning.",
            C("/aremoveteam Delhi") + " previews; " + C("/aremoveteam Delhi | "
            "confirm") + " does it. Its players go back into the pool and its "
            "opening purse is shared equally between the rest.",
            B("⬇️ Download franchises (JSON)") + " and the upload beside it are "
            "for building a field of ten offline, or copying one between "
            "seasons.",
        ]),
        p(B("A franchise with no owner id cannot bid, and ") + C("/astart")
          + B(" refuses to open an auction that has one") + " — by name, "
          "rather than letting the room find out on the first lot.", "note"),

        h1("7. Building the pool, and the order it runs in"),
        p("One card holds the set queue, the two set builders and the catalogue "
          "filter: filter by name, rating band, country, role, batting hand, "
          "bowling style and card edition, preview, then add the ticked players "
          "or everything the filter matches. Career cards and inactive rows are "
          "excluded before you see them."),
        p(B("Adding is idempotent per player.") + " Re-running the builder with a "
          "corrected filter adds what is new and leaves what is already there "
          "exactly as it is, including anything already sold — a pool "
          "builder you cannot safely run twice is one nobody dares run once."),
        code(["/apool 85-90 | Marquee    add every card in a rating band as a set",
              "/anextset Marquee        make a set (or a band) come next",
              "/asetorder A, B, C       order the whole queue by set",
              "/asets                   every set and where it stands",
              "/awithdraw <player>      pull somebody out of the auction"]),
        p("The numbers " + C("/asets") + " shows are the order the sets run in, "
          "and the website's Sets card does the same with ↑ / ↓. Ordering the "
          "queue never touches the lot on the block."),

        h1("8. Focus mode: the group locked to auction commands"),
        p("While an auction bound to a group is " + B("live or paused") + ", that "
          "group answers auction commands and " + B("nothing else") + ". "
          "Everything else is refused with one line naming where it still works, "
          "which is a DM with the bot. It is " + B("on by default") + "."),
        code(["/afocus            is it on, and what that means",
              "/afocus off        this group behaves normally again",
              "/afocus on         lock it back"]),
        p("The switch also lives in ⚙️ Settings on the setup page, and the fold's "
          "summary says which way it is set. It is carried into a cloned season: "
          "a room that turned the lock off meant that about the room, not about "
          "one season."),
        table([
            ["What is locked", "A command, or a button, in the auction's own "
                               "group, while the auction is live or paused."],
            ["What is never locked", "Talking. DMs. Every other group. An "
                                     "auction still in setup. Bot admins and "
                                     "auction admins — you keep every "
                                     "command you had."],
            ["What owners see", "One line: which auction is on the block, which "
                                "commands still work, and that everything else "
                                "works in a DM."],
            ["Why", "A thirty-second clock and a pinned board being edited every "
                    "two seconds cannot share a room with a /claim card, and a "
                    "bid that scrolls past unseen is how a price gets disputed."],
        ], widths=[38 * mm, None], header=False, mono_first=False),
        p("It is announced when you change it, because it changes what every "
          "command in the group does and a room that finds out by being refused "
          "has been told the hard way.", "callout"),
    ]

    flow += [
        h1("9. Four rules worth knowing before you start"),
        h2("Changing the opening purse changes every franchise"),
        p("It is not just the default for the next side you add. Save a new "
          "opening purse in ⚙️ Settings and " + B("every franchise moves to "
          "it") + ": its total is set, its remaining moves by the same amount, "
          "and the move is written into the ledger as a correction, so the "
          "purse and its ledger still agree afterwards."),
        bullets([
            B("Spending stays spent.") + " A side that has bought ₹30 Cr of "
            "players out of ₹100 Cr lands on ₹90 Cr of a ₹120 Cr purse.",
            B("A cut that would overdraw somebody is refused by name") + ", "
            "before anything is written — sell somebody first, or set that "
            "one franchise's own purse on its row.",
            "The flash message says how many franchises moved.",
            B("Saving the same number does nothing") + ", so a franchise you "
            "deliberately gave a purse of its own on its row keeps it until "
            "the season's purse actually changes.",
        ]),
        h2("Direct bids: the room's choice, not the rule's"),
        code(["/adirect            is it on, and what that means",
              "/adirect off        ladder only: bare /bid and the buttons",
              "/adirect on         a bidder may type their own number"]),
        p("A jump bid (" + C("/bid 12") + " when the minimum is ₹2.4 Cr) is half "
          "of what an auction is, and a strict ladder is the other half — "
          "neither is wrong, so it is a switch rather than a rule. Off, a typed "
          "amount is refused with the number it " + B("would") + " have been, "
          "and bare " + C("/bid") + ", the board's buttons and your own console "
          "bid all keep working. It is on by default, carried into a cloned "
          "season, and printed in " + C("/arules") + " when it is off."),
        h2("One pair of hands per franchise, per lot"),
        p("Whoever bids first for a franchise holds that lot; another owner or "
          "co-owner of the same side is refused, by the holder's name, until "
          "the next lot. Two of them bidding one player is one franchise "
          "raising its own price. The claim resets with the lot, and an undone "
          "bid releases it — and " + B("your console bid is exempt") + ", "
          "because that is you acting for the franchise, announced as such."),

        h2("Every card belongs to whoever asked for it"),
        p("Each card a command posts carries a " + B("❌ Close") + " button, "
          "and only the person who asked for it can press it. That includes "
          + B("your own answers") + ": “A lot now runs for 45s” is one more "
          "message on top of the board once the room has read it, so every "
          "admin command closes the same way — the settings and running "
          "commands, " + C("/anew") + ", " + C("/abind") + ", " + C("/acall")
          + ", the " + C("/adminhelp") + " card and the auction-admin "
          "commands."),
        p("The " + C("/ainfo") + " menu and the 🗂 Sets card go further: they "
          "answer the person who sent the command alone, because both "
          "re-render under whoever presses and a second pair of hands on one "
          "genuinely fights the first."),
        bullets([
            B("The pinned board is the exception on both counts") + ": its "
            "quick-bid and RTM buttons belong to the room, and it has no Close "
            "— a board any passer-by could delete is a board the auction loses "
            "mid-lot. Delete it from Telegram if you must.",
            B("A refusal is not a card.") + " One-line corrections ("
            "“only auction admins…”) carry no button: one under every "
            "error is the clutter this exists to reduce.",
            B("Nor is a pending retention offer.") + " It already carries the "
            "two answers that resolve it, and " + C("/aretcancel") + " "
            "withdraws it — closing the card would hide an offer that is "
            "still open.",
        ]),

        h1("10. Running the room"),
        table([
            ["Command", "What it does"],
            ["/astart", "Open the auction, or resume a paused one. Refuses "
                        "anything that would strand it halfway: no group, no "
                        "franchises, an ownerless franchise, an empty pool, "
                        "somebody under the retention minimum."],
            ["/apause · /aresume", "Stop and restart the clock. A resumed lot "
                                   "gets a full clock, not the fraction that was "
                                   "left — nobody was watching a frozen "
                                   "countdown."],
            ["/anext", "Put the next lot on the block."],
            ["/aextend [seconds]", "Add time. Does not spend an anti-snipe "
                                   "extension — a dropped connection is "
                                   "not a snipe."],
            ["/asold", "Sell at the standing bid."],
            ["/aunsold", "Pass the lot. Refused while a bid stands."],
            ["/aundobid", "Void the standing bid and fall back to the one "
                          "under it."],
            ["/awithdraw <player>", "Pull a player out of the auction."],
            ["/atimer 60 40 20 10 5", "The staged clock: open with 60s; a bid "
                                      "with under 40s left resets to 40s; 1st "
                                      "warning at 20s, 2nd at 10s; the last 5 "
                                      "counted down, then SOLD. …10 off = no "
                                      "count · /atimer 45 = classic · bare "
                                      "reads it back."],
            ["/asnipe <w> <e> <max>", "Anti-snipe. Setting the window equal to "
                                      "the extension gives the rule most rooms "
                                      "want: any bid in the last N seconds "
                                      "gives everybody N more."],
            ["/aaccel [go]", "Re-list everything unsold now."],
            ["/aaccelmode on|off", "The automatic ⚡ Accelerated round."],
        ], widths=[38 * mm, None]),
        p(B("Nothing you type announces itself directly.") + " Every command "
          "writes one event and commits; a sweeper drains that log every two "
          "seconds and says it out loud. That is why the website can drive the "
          "same auction — “Mark Sold” in the browser and "
          + C("/asold") + " in the group are literally the same function call, "
          "and the room's message order is the same either way."),
        h2("The web console"),
        p("🔨 " + B("Console") + " is the same auction driven with buttons: "
          "start, pause, next player, extend, mark sold, mark unsold, undo the "
          "last bid, withdraw — and a " + B("bid-for-a-franchise") + " form "
          "for an owner whose phone has died. That last one is stamped as an "
          "admin's bid and announced as one: a bid that reads as the owner's own "
          "choice when it was not is how a result gets disputed. The countdown "
          "ticks client-side and the panel refreshes itself every two seconds."),

        h1("11. Retention, RTM and expansion picks"),
        h2("Retention — before the first lot"),
        code(["/aretain Mumbai | Virat Kohli | 18   offer it (they must accept)",
              "/aretainforce Mumbai | Virat Kohli   retain at once, no Accept",
              "/aoffers                             offers still waiting",
              "/aretcancel <player>                 withdraw a waiting offer",
              "/aunretain <player>                  release one into the pool",
              "/aretlock on                         close the window"]),
        p(C("/aretain") + " " + B("offers") + " a retention: the franchise's "
          "owner or a co-owner presses ✅ Accept, and only then is the player "
          "kept. Nobody else can press it — not another owner, not you. "
          + C("/aretainforce") + " is the instant path, for an owner who agreed "
          "in person. A retained player is an ordinary sold lot, so it counts "
          "against every cap and reaches the published league with no special "
          "handling. The spend comes off the top of the purse; the ladder "
          "pre-fills the price and the caps are what refuse. Opening the "
          "auction closes the retention window for you."),
        h2("Right To Match"),
        code(["/artmset 2 45 200     2 cards each, 45s to answer, +₹2 Cr premium",
              "/artmset off          turn it off",
              "/artmcards Mumbai 3   one franchise's own card count",
              "/artmforce yes|no|stand   answer an open window for a franchise",
              "/artmundo <player>    undo a match — money and card both back"]),
        p("Three windows on one clock: the holder is asked first, the top bidder "
          "then gets one last raise, and the match is against that final number. "
          "Each window times out to the safe answer — decline, stand, "
          "decline — so a silent owner can never wedge the auction, and "
          + C("/artmforce") + " is for when you need to answer for somebody who "
          "is not there."),
        h2("Expansion picks"),
        code(["/apickset 3                       deal 3 picks to each new side",
              "/apick Lucknow | Rahul | 12       a new side signs a player",
              "/apickskip                        burn the turn on the clock",
              "/apickundo                        roll the last pick back",
              "/apicks                           the order, and whose turn"]),
        p("A side joining a league that already exists has no previous squad, so "
          "it retains nobody; picks are how it signs a few players before the "
          "auction opens. A skipped turn is a spent turn, or the order never "
          "moves on. " + C("/apicks") + " is the room's readout, not yours "
          "alone — it is the new side's turn coming up."),
    ]

    flow += [
        h1("12. Money, and the ledger behind it"),
        code(["/apurse                     every purse, max bid, squad count",
              "/agrant Mumbai | 5          add ₹5 Cr",
              "/agrant Mumbai | -2         take ₹2 Cr back"]),
        bullets([
            B("A bid moves no money.") + " The purse is debited exactly once, "
            "when a lot sells. Losing bids are kept as history — that is "
            "where a price's story lives.",
            B("Every movement is a ledger row") + ", and the purse column is a "
            "cache over it. The setup page shows any drift and a " + B("🔍 "
            "Reconcile purses") + " button; a repair is written as a "
            "correction row rather than by overwriting the column, so the "
            "evidence survives.",
            C("/agrant") + " is the honest way to fix a mistake: it is recorded "
            "as an admin correction, with your id on it.",
        ]),

        h1("13. Finishing"),
        code(["/apublish            squads → one Challenge League",
              "/aclone Season 3     next season: every rule, every franchise",
              "/acancel             cancel the auction"]),
        bullets([
            C("/apublish") + " writes one team per franchise and one squad row "
            "per bought player. " + B("Publishing again re-syncs the same "
            "league") + " rather than creating a second one, so a correction is "
            "simply republished.",
            "Everything downstream — the matches, the XI picker, the points "
            "table, the playoff bracket — is code that already existed and "
            "neither knows nor cares that an auction produced the squads.",
            C("/aclone") + " carries every rule and every franchise with its "
            "owners. It does " + B("not") + " carry the pool (retain first, then "
            "build) or the bound group (bind the new season when the old one is "
            "done).",
            B("Short squads are topped up for free") + " at the end, from "
            "whoever is still unsold, inside the squad and overseas caps — "
            "but only after the unsold players have had their accelerated round.",
        ]),

        h1("14. Troubleshooting"),
        table([
            ["“/astart says the pool is empty.”",
             "Nothing is queued. Build the pool on the setup page, or /apool a "
             "rating band."],
            ["“It names franchises with no owner.”",
             "A franchise nobody can bid for would sit out the whole auction "
             "and end with no squad. Add owner Telegram ids, then start."],
            ["“This group is already running another auction.”",
             "One group, one auction. Finish or cancel the old one — a "
             "finished one is released automatically when you bind the next."],
            ["“Somebody's /claim is being refused in the group.”",
             "Focus mode, working as intended. /afocus reads it back; /afocus "
             "off turns it off for this auction."],
            ["“I sold a lot to the wrong franchise.”",
             "Undo the sale from the console, or /aundobid before it sells. The "
             "money and the squad slot both come back."],
            ["“An owner is offline and an RTM window is open.”",
             "/artmforce yes|no|stand answers for them; it is recorded as an "
             "admin's answer. Left alone, the window times out to the safe "
             "answer anyway."],
            ["“An owner's phone has died mid-lot.”",
             "Bid for them from the console's bid-for-a-franchise form. It is "
             "announced as an admin's bid, deliberately."],
            ["“I changed the opening purse and nothing happened.”",
             "It moves every franchise now, and the flash says how many. If "
             "one refused, the message names the side that has already spent "
             "more than the new purse."],
            ["“Two co-owners keep outbidding each other.”",
             "They cannot any more: the first to bid for a side holds that "
             "lot. Section 9."],
            ["“The room is jump-bidding wildly.”",
             "/adirect off — every raise becomes one step. Bare /bid and the "
             "board's buttons still work."],
            ["“A purse looks wrong.”",
             "🔍 Reconcile purses on the setup page re-sums the ledger and "
             "reports the drift; /agrant is the recorded way to correct one."],
            ["“The base price on a lot is wrong.”",
             "Override that one player from the pool table while the lot is "
             "still queued. Editing the ladder cannot move a price a lot "
             "already carries."],
        ], widths=[52 * mm, None], header=False, mono_first=False),

        h1("15. Auction admins"),
        p("A bot admin can appoint somebody who may run " + B("every auction "
          "command and no other admin command in the bot") + " — the person "
          "who runs your league's auction does not need the keys to everything "
          "else."),
        code(["/aadminadd <id | @user | reply>      let somebody run auctions",
              "/aadminremove <id | @user | reply>   take it away again",
              "/aadmins                             everyone who may run one"]),
        p("Those three are bot-admin only. Auction admins are also exempt from "
          "focus mode, like you."),

        h1("16. The admin command list"),
        table([
            ["Group", "Commands"],
            ["Setting up", "/anew · /abind · /atimer · /asnipe · /afocus · "
                           "/adirect · /aco · /aaccelmode"],
            ["Pool & sets", "/apool · /anextset · /asetorder · /asets · "
                            "/awithdraw"],
            ["Running it", "/astart · /apause · /aresume · /anext · /aextend · "
                           "/asold · /aunsold · /aundobid · /aaccel"],
            ["Money", "/agrant"],
            ["Retention", "/aretain · /aretainforce · /aoffers · /aretcancel · "
                          "/aunretain · /aretlock on"],
            ["Right To Match", "/artmset · /artmcards · /artmforce · /artmundo"],
            ["Expansion picks", "/apick · /apickset · /apickskip · /apickundo"],
            ["Teams", "/acall · /aremoveteam"],
            ["The end", "/apublish · /aclone · /acancel"],
            ["Bot admins only", "/aadminadd · /aadminremove · /aadmins"],
            ["The card itself", "/auction · /adminhelp"],
        ], widths=[35 * mm, None], mono_first=False),
        Spacer(1, 8),
        p("<i>" + C("/auction") + " prints this list live, with a line on each "
          "command, and it is the version that cannot fall out of date.</i>"),
    ]
    return flow


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    build("Franchise-Auction-Guide-Users.pdf",
          "Franchise Auction — Player & Owner Guide", user_guide())
    build("Franchise-Auction-Guide-Admins.pdf",
          "Franchise Auction — Admin Guide", admin_guide())


if __name__ == "__main__":
    main()
