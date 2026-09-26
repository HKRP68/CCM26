"""Record a tournament fixture from a written scorecard.

Some matches are played away from the bot — on a stream, in a hall, on somebody
else's simulator — and the tournament still has to carry them: the points table,
the net run rate, and the batting and bowling leaderboards. Until now the only
way in was the admin panel's manual-result form, which takes the two scorelines
and nothing else, so an imported match left every player leaderboard untouched.

This module takes the whole scorecard as **text** — the thing a scorer already
has — and turns it into the same shape a bot-played match produces, per-player
lines included. :func:`parse_scorecard` is pure (no database, no models) so the
grammar can be tested on its own; :func:`plan_import` resolves what it parsed
against a real fixture and reports every guess it made; :func:`record_import`
writes it.

The bot's own file
------------------
The quickest path needs no typing at all: ``MatchNo<id>.txt``, the scorecard
this bot archives for every match it plays (see
``services.match_webapp_service._build_text_scorecard`` and
``handlers.match._text_innings_block``), is read exactly as it is —
column-aligned tables, ``Total:`` lines, extras, fall of wickets and all::

    RAJASTHAN CAMELBACK CHARGERS INNINGS  —  @someone
    -------------------------------------------------------------------
    Batsman               Status                      R    B   4s   6s      SR
    -------------------------------------------------------------------
    Abhishek Sharma       Caught                     11   13    1    0   84.60
    Amelia Kerr           not out                    34   24    2    1  141.70

    Total: 147/5 (20.0 Overs)

    -------------------------------------------------------------------
    Bowler                        O     M     R     W    Econ
    -------------------------------------------------------------------
    Glenn Maxwell                 4     0    34     1    8.50

A Super Over rides in that file as two further innings between the same two
sides. They are read, reported, and left out of the scoreline and the player
figures — a Super Over is a separate contest, and that is how the bot records
one itself. The ``Result:`` line is what carries its winner.

The hand-written format
-----------------------
Two innings blocks, each headed by the batting side and its score::

    Innings 1: Mumbai Indians 187/5 (20)
    Batting
    Rohit Sharma 62 (41) 6x4 2x6 not out
    Ishan Kishan 45 (30) 4x4 1x6 c Dhoni b Jadeja
    Bowling
    Deepak Chahar 4-0-31-1
    Ravindra Jadeja 4-0-28-2

    Innings 2: Chennai Super Kings 180/8 (20)
    ...

    Result: Mumbai Indians won by 7 runs

The ``Bowling`` list inside an innings is the *fielding* side's bowlers, exactly
as a printed scorecard reads — this module credits them to the other team.

Deliberately forgiving, because this is typed by a person:

* headings are case-insensitive, with or without a colon, and ``Innings 1`` may
  be written ``[Innings 1]``, ``1st Innings``, or put on its own line above the
  scoreline. It may be left out entirely as long as the scoreline shows wickets
  (``187/5``) — without both a label and a wickets column, a header is
  indistinguishable from a batting line;
* a score may be ``187/5``, ``187-5`` or just ``187`` (all out is assumed at 10
  when wickets are missing), and overs may be ``(20)``, ``in 20 overs`` or
  ``20.3`` in the usual cricket notation;
* a batting line is ``Name runs (balls)`` plus optional ``6x4``/``2x6`` and a
  dismissal, **or** a comma/pipe-separated ``Name, runs, balls, fours, sixes,
  out``, **or** the column-aligned ``Name | Status | R B 4s 6s SR`` above;
* a bowling line is ``Name O-M-R-W``, ``Name O M R W``, the comma form, or the
  column-aligned ``Name | O M R W Econ``;
* an innings' score may sit on its header or on a ``Total:`` line inside it.

A batter counts as out unless the line says otherwise (``not out``, ``n.o.`` or
a trailing ``*``) — the safe default, because assuming not-out would quietly
inflate batting averages.
"""

import logging
import re

logger = logging.getLogger(__name__)

# How many innings a recorded fixture has. A written scorecard with one innings
# is a half-finished match, not an import.
INNINGS_PER_MATCH = 2

# Section headings inside an innings block. Matched on the first word, so the
# bot's own column header — "Batsman  Status  R  B  4s  6s  SR" — opens the
# batting section exactly as a bare "Batting" does.
_BAT_HEADINGS = ("batting", "batsmen", "batsman", "batters", "batter", "bat")
_BOWL_HEADINGS = ("bowling", "bowlers", "bowler", "bowl")
# Headings after which every line is skipped until the next section or innings.
# These introduce *lists* — "1-25 (3.2)", a run of names — that carry nothing
# this importer has a column for, and that no player grammar will accept.
_SKIP_HEADINGS = ("fall of wickets", "fow", "did not bat", "dnb", "partnerships")
# Single lines with nothing to record on them.
_IGNORED_PREFIXES = ("extras", "toss", "venue", "pitch", "stadium", "umpire",
                     "match", "player of the match", "potm", "overs",
                     "super over", "target", "run rate", "rr:")

# Words that mark how a batter got out. Everything from the first one onward is
# the dismissal, not part of the name or the figures.
_DISMISSAL_RE = re.compile(
    r"\b(c\s|caught|b\s|bowled|lbw|st\s|stumped|run\s*out|hit\s*wicket|"
    r"retired|obstruct)", re.I)
_NOT_OUT_RE = re.compile(r"(\bnot\s*out\b|\bnotout\b|\bn\.?o\.?\b)", re.I)

# "187/5 (20)", "187-5 in 19.3 overs", "187 (20 ov)", "187/5".
_SCORE_RE = re.compile(
    r"(?P<runs>\d{1,4})\s*(?:[/-]\s*(?P<wkts>10|[0-9]))?"
    r"(?:\s*(?:\(|\bin\b)\s*(?P<overs>\d{1,3}(?:\.\d)?)\s*(?:overs?|ov\b|ovs\b)?\s*\)?)?"
    r"\s*$", re.I)
# "Total: 147/5 (20.0 Overs)" and "Total: 187/5 (20.0 Overs, RR: 9.35)" — the
# line that carries an innings' score in the bot's own scorecard file, where the
# header above it names only the team. Anything else inside the brackets (a run
# rate, a target) is read past rather than tripped over.
_TOTAL_RE = re.compile(
    r"^totals?\s*[:\-]?\s*(?P<runs>\d{1,4})(?:\s*[/-]\s*(?P<wkts>10|[0-9]))?"
    r"(?:\s*\(\s*(?P<overs>\d{1,3}(?:\.\d)?)\s*(?:overs?|ov\b|ovs\b)?[^)]*\))?",
    re.I)
# "RAJASTHAN CAMELBACK CHARGERS INNINGS  —  @handle" — how every innings in the
# bot's own ``MatchNo<id>.txt`` is headed. The score is not on this line; it
# arrives below, on the "Total:" line.
_INNINGS_SUFFIX_RE = re.compile(
    r"^(?P<team>.+?)\s+innings\b\s*(?:[—\-–:]+\s*(?P<tag>@?[^\s]+))?\s*$", re.I)
# "Innings 1", "[2nd Innings]", "Inn 1" — the label, wherever it sits.
_INNINGS_LABEL_RE = re.compile(
    r"^\W*(?:(?P<pre>[12])(?:st|nd)?\s*)?inn(?:ing)?s?\b\W*(?P<post>[12])?\W*",
    re.I)

# ``62 (41)`` — and ``62* (41)``, where the star is the not-out shorthand.
_COMPACT_BAT_RE = re.compile(
    r"(?P<runs>\d{1,3})\s*\*?\s*\(\s*(?P<balls>\d{1,3})\s*\)")
_FOURS_RE = re.compile(r"(\d{1,2})\s*[x*]\s*4\b|\b(\d{1,2})\s*(?:fours|4s)\b", re.I)
_SIXES_RE = re.compile(r"(\d{1,2})\s*[x*]\s*6\b|\b(\d{1,2})\s*(?:sixes|6s)\b", re.I)
_FIGURES_RE = re.compile(
    r"(?P<overs>\d{1,2}(?:\.\d)?)\s*[-/\s]\s*(?P<maidens>\d{1,2})\s*[-/\s]\s*"
    r"(?P<runs>\d{1,3})\s*[-/\s]\s*(?P<wickets>10|[0-9])\s*$")

# ── The column-aligned forms the bot's own scorecard file uses ────────
#
# Both are "a name, then five numbers", and the last of the five is a rate with
# a decimal point. What separates a batter's row from a bowler's is only which
# table it is under, so these are only ever tried in section order — and they
# are matched against the *raw* line, because the column gaps are the one thing
# that says where the name ends and the status begins.
_TABULAR_BAT_RE = re.compile(
    r"^(?P<prefix>\S.*?)\s{2,}(?P<runs>\d{1,3})\s+(?P<balls>\d{1,3})\s+"
    r"(?P<fours>\d{1,2})\s+(?P<sixes>\d{1,2})\s+(?P<sr>\d{1,4}\.\d+)\s*$")
_TABULAR_BOWL_RE = re.compile(
    r"^(?P<prefix>\S.*?)\s{2,}(?P<overs>\d{1,2}(?:\.\d)?)\s+(?P<maidens>\d{1,2})\s+"
    r"(?P<runs>\d{1,3})\s+(?P<wickets>10|\d)\s+(?P<econ>\d{1,4}\.\d+)\s*$")
# A status column that means the batter never went out to the middle.
_DID_NOT_BAT_RE = re.compile(r"\b(did\s*not\s*bat|dnb|absent)\b", re.I)


class ScorecardError(Exception):
    """A refusal carrying a message written for whoever pasted the scorecard.

    The message is plain text and may quote a line of their file, so callers
    escape it once when they render it.
    """


def _clean(text):
    """Squeeze whitespace and drop the decorations a pasted card carries."""
    text = str(text or "").replace(" ", " ")
    text = text.strip().strip("*").strip()
    return " ".join(text.split())


def _norm(name):
    """The form player and team names are compared in."""
    return re.sub(r"[^a-z0-9 ]+", "", _clean(name).casefold()).strip()


def _int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _split_fields(line):
    """A comma- or pipe-separated line as stripped fields, or None."""
    for sep in ("|", ","):
        if sep in line:
            return [p.strip() for p in line.split(sep)]
    return None


# ──────────────────────────────────────────────────────────────────────
# Line grammars
# ──────────────────────────────────────────────────────────────────────

# Status words for the one case column gaps can't resolve: a
# name long enough to be padded to a single space before its status.
_STATUS_WORDS = ("did not bat", "hit wicket", "retired hurt", "retired out",
                 "not out", "run out", "obstructing the field", "stumped",
                 "caught", "bowled", "retired", "absent", "lbw", "dnb", "out")


def _split_columns(text):
    """A column-aligned row split on its gaps: two or more spaces.

    Falls back to a status-word split when the row has no gap left — a name
    padded to the full column width leaves a single space before the status —
    and to "it is all the name" when that finds nothing either.
    """
    parts = [p.strip() for p in re.split(r"\s{2,}", str(text or "").strip())
             if p.strip()]
    if len(parts) > 1 or not parts:
        return parts or [""]
    only = parts[0]
    low = only.casefold()
    for word in _STATUS_WORDS:
        at = low.rfind(word)
        if at > 0 and at + len(word) == len(low):
            return [only[:at].strip(), only[at:].strip()]
    return [only]


def _parse_tabular_batting(line):
    """One row of a column-aligned batting table, or None.

    The shape is ``Name | Status | R B 4s 6s SR`` — what
    ``services.match_webapp_service._build_text_scorecard`` writes, which is the
    file people already have for every match the bot has played. A "did not bat"
    status comes back as a real row of zeroes rather than being dropped: whether
    that counts as an innings is decided once, in ``_build_lines``, by whether
    the player faced a ball or got out.
    """
    hit = _TABULAR_BAT_RE.match(str(line).rstrip())
    if not hit:
        return None
    columns = _split_columns(hit.group("prefix"))
    name = columns[0]
    if not name:
        return None
    status = " ".join(columns[1:])
    return {"name": name,
            "runs": _int(hit.group("runs")), "balls": _int(hit.group("balls")),
            "fours": _int(hit.group("fours")), "sixes": _int(hit.group("sixes")),
            "out": not (_NOT_OUT_RE.search(status)
                        or _DID_NOT_BAT_RE.search(status))}


def parse_batting_line(line):
    """One batting line → a dict, or None when the line isn't one.

    Returns ``{name, runs, balls, fours, sixes, out}``. Raises
    :class:`ScorecardError` for a line that is clearly meant to be a batting
    line (it has a name and figures) but whose figures can't be read, so a typo
    is reported rather than silently dropping a batter from the card.
    """
    raw = _clean(line)
    if not raw:
        return None

    tabular = _parse_tabular_batting(line)
    if tabular is not None:
        return tabular

    # ``*`` right after the runs is the scorecard shorthand for not out. It is
    # stripped by _clean at the ends of the line, so test the raw text too.
    not_out = bool(_NOT_OUT_RE.search(raw)) or bool(
        re.search(r"\d\s*\*", str(line)))

    fields = _split_fields(raw)
    if fields and len(fields) >= 3 and fields[0]:
        name = _clean(fields[0])
        runs, balls = _int(fields[1], -1), _int(fields[2], -1)
        if runs < 0 or balls < 0:
            raise ScorecardError(
                f"Couldn't read runs and balls from batting line: {raw!r}")
        tail = " ".join(fields[5:]) if len(fields) > 5 else ""
        if _NOT_OUT_RE.search(tail):
            not_out = True
        return {"name": name, "runs": runs, "balls": balls,
                "fours": max(0, _int(fields[3])) if len(fields) > 3 else 0,
                "sixes": max(0, _int(fields[4])) if len(fields) > 4 else 0,
                "out": not not_out}

    # Compact form. ``runs (balls)`` is required: it is what tells a name from a
    # figure when both are just words and numbers on one line.
    hit = _COMPACT_BAT_RE.search(raw)
    if not hit:
        return None
    name = _clean(raw[:hit.start()])
    # "Mumbai Indians 187/5 (20)" ends in "5 (20)", which is the shape of a
    # batting line. A name that trails off mid-scoreline is not a name.
    if re.search(r"\d\s*[/-]\s*$", name):
        return None
    # A dismissal written before the figures ("c Dhoni b Jadeja" never is, but
    # "Rohit Sharma c Dhoni b Jadeja 62 (41)" happens) is not part of the name.
    dismissal = _DISMISSAL_RE.search(name)
    if dismissal and dismissal.start() > 0:
        name = _clean(name[:dismissal.start()])
    if not name:
        raise ScorecardError(f"Batting line has no player name: {raw!r}")
    tail = raw[hit.end():]
    fours_hit, sixes_hit = _FOURS_RE.search(tail), _SIXES_RE.search(tail)

    def _marked(match):
        return _int(next((g for g in match.groups() if g), 0)) if match else None

    fours, sixes = _marked(fours_hit), _marked(sixes_hit)
    if fours is None and sixes is None:
        # No 4s/6s markers — fall back to two bare numbers straight after the
        # figures ("Rohit Sharma 62 (41) 6 2 out").
        bare = re.match(r"\s*(\d{1,2})\s+(\d{1,2})\b", tail)
        if bare:
            fours, sixes = _int(bare.group(1)), _int(bare.group(2))
    return {"name": name, "runs": _int(hit.group("runs")),
            "balls": _int(hit.group("balls")),
            "fours": max(0, fours or 0), "sixes": max(0, sixes or 0),
            "out": not not_out}


def parse_bowling_line(line):
    """One bowling line → ``{name, overs, maidens, runs, wickets}``, or None."""
    raw = _clean(line)
    if not raw:
        return None

    hit = _TABULAR_BOWL_RE.match(str(line).rstrip())
    if hit:
        name = _split_columns(hit.group("prefix"))[0]
        if name:
            return {"name": name, "overs": hit.group("overs"),
                    "maidens": _int(hit.group("maidens")),
                    "runs": _int(hit.group("runs")),
                    "wickets": min(10, _int(hit.group("wickets")))}

    fields = _split_fields(raw)
    if fields and len(fields) >= 5 and fields[0]:
        overs = fields[1].strip()
        if not re.fullmatch(r"\d{1,2}(?:\.\d)?", overs or ""):
            raise ScorecardError(
                f"Couldn't read the overs from bowling line: {raw!r}")
        return {"name": _clean(fields[0]), "overs": overs,
                "maidens": max(0, _int(fields[2])),
                "runs": max(0, _int(fields[3])),
                "wickets": max(0, min(10, _int(fields[4])))}

    hit = _FIGURES_RE.search(raw)
    if not hit:
        return None
    name = _clean(raw[:hit.start()])
    if not name:
        raise ScorecardError(f"Bowling line has no player name: {raw!r}")
    return {"name": name, "overs": hit.group("overs"),
            "maidens": _int(hit.group("maidens")),
            "runs": _int(hit.group("runs")),
            "wickets": min(10, _int(hit.group("wickets")))}


def _parse_innings_header(line):
    """``"Innings 1: Mumbai Indians 187/5 (20)"`` → a header dict, or None.

    Returns ``(header, labelled)``. ``labelled`` is True when the line carried an
    explicit "Innings N", which is the only thing that makes a header certain:
    without it the line is just a name followed by numbers, and so is every
    batting and bowling line on the card. An unlabelled header therefore has to
    show wickets or overs — a bare total is too easily somebody's score — and the
    caller only reaches for it once the line has failed the player grammars.
    """
    raw = _clean(line).rstrip(".")
    if not raw:
        return None, False

    # "RAJASTHAN CAMELBACK CHARGERS INNINGS  —  @handle": the bot's own file
    # heads an innings with the team alone and puts the score on a "Total:" line
    # underneath. The word INNINGS makes it unmistakable, so it is labelled.
    suffix = _INNINGS_SUFFIX_RE.match(raw)
    if suffix:
        team = _clean(suffix.group("team")).rstrip(":-–— ").strip()
        if team:
            return {"number": None, "team": team, "runs": None, "wickets": None,
                    "overs": "", "batting": [], "bowling": []}, True

    number, labelled = None, False
    label = _INNINGS_LABEL_RE.match(raw)
    if label:
        labelled = True
        number = _int(label.group("pre") or label.group("post"), 0) or None
        raw = _clean(raw[label.end():]).lstrip(":-–— ").strip()
    hit = _SCORE_RE.search(raw)
    if not hit:
        return None, labelled
    # Without a label the only thing separating a header from a batting line is
    # the wickets column: "Bravo 151/4 (20)" is a scoreline, "Bravo 151 (20)"
    # is a batter who faced 20 balls.
    if not labelled and not hit.group("wkts"):
        return None, False
    team = _clean(raw[:hit.start()]).rstrip(":-–— ").strip()
    if not team:
        return None, labelled
    wkts = hit.group("wkts")
    return {
        "number": number,
        "team": team,
        "runs": _int(hit.group("runs")),
        # No wickets column means the side was bowled out, which is how a
        # scoreline written as a bare total always reads.
        "wickets": 10 if wkts is None else _int(wkts),
        "overs": hit.group("overs") or "",
        "batting": [], "bowling": [],
    }, labelled


def _bare_innings_label(line):
    """``"Innings 2"`` on a line of its own → ``2``; anything else → None."""
    raw = _clean(line)
    label = _INNINGS_LABEL_RE.match(raw)
    if not label or _clean(raw[label.end():]):
        return None
    return _int(label.group("pre") or label.group("post"), 0) or None


def _is_heading(line, headings):
    """True when the line opens one of these sections.

    Matched on the first word rather than the whole line, so the bot's column
    header — ``Batsman  Status  R  B  4s  6s  SR`` — opens the batting section
    exactly as a bare ``Batting`` does.
    """
    text = _clean(line).rstrip(":").casefold()
    if not text:
        return False
    # ``h + ":"`` covers the inline form — "Did Not Bat: Henry, King" heads a
    # list just as much as a bare "Did Not Bat" does.
    return any(text == h or text.startswith(h + " ") or text.startswith(h + ":")
               for h in headings)


def _parse_total_line(line):
    """``"Total: 147/5 (20.0 Overs)"`` → ``(runs, wickets, overs)``, or None.

    A missing wickets column reads as all out, the same way a bare total does
    anywhere else on a card.
    """
    hit = _TOTAL_RE.match(_clean(line))
    if not hit:
        return None
    wkts = hit.group("wkts")
    return (_int(hit.group("runs")),
            10 if wkts is None else _int(wkts),
            hit.group("overs") or "")


def _is_ignorable(line):
    low = _clean(line).casefold()
    if not low:
        return True
    return any(low.startswith(p) for p in _IGNORED_PREFIXES)


def _parse_result_line(line):
    """``"Result: Mumbai Indians won by 7 runs"`` → the text after the label."""
    raw = _clean(line)
    match = re.match(r"^result\s*[:\-]\s*(?P<text>.+)$", raw, re.I)
    return _clean(match.group("text")) if match else None


# ──────────────────────────────────────────────────────────────────────
# The whole card
# ──────────────────────────────────────────────────────────────────────

def parse_scorecard(text):
    """A written scorecard → ``{innings: [...], result_text}``.

    Pure: no database, no models. Raises :class:`ScorecardError` with a message
    naming the offending line when the card can't be read.
    """
    if not str(text or "").strip():
        raise ScorecardError("The scorecard is empty.")

    innings, result_text, section = [], None, None
    pending_number = None

    def _open_innings(header):
        """Start a new innings block, inheriting a number left on its own line."""
        if header["number"] is None and pending_number is not None:
            header["number"] = pending_number
        innings.append(header)

    for lineno, raw_line in enumerate(str(text).splitlines(), 1):
        line = _clean(raw_line)
        if not line or line.strip("-=_#·•").strip() == "":
            continue
        if line.lstrip().startswith("#"):
            continue  # a comment in the file

        found_result = _parse_result_line(line)
        if found_result:
            result_text = found_result
            continue

        # An explicit "Innings N" (or "TEAM INNINGS") always opens a new block,
        # wherever it appears — and is checked before the skip list below, so a
        # side really called "Total Cricket Club" can still head an innings.
        header, labelled = _parse_innings_header(line)
        if labelled and header is not None:
            _open_innings(header)
            pending_number, section = None, None
            continue

        # "Total: 147/5 (20.0 Overs)" is where an innings headed by its team
        # alone gets its score. Read before the skip list, which would otherwise
        # throw the line away as one more line of trimmings.
        if innings:
            total = _parse_total_line(line)
            if total is not None:
                current = innings[-1]
                if current["runs"] is None:
                    current["runs"], current["wickets"], current["overs"] = total
                continue

        # "Total: 187/5 (20)" on a card that already had its score, a run of
        # names under "Did Not Bat", the venue, the toss — nothing to record.
        if _is_ignorable(line):
            continue
        # A card that puts the label on its own line ("Innings 1" / "Mumbai
        # Indians 187/5 (20)") — remember the number for the header below it.
        bare_label = _bare_innings_label(line)
        if bare_label is not None:
            pending_number = bare_label
            continue

        if _is_heading(line, _BAT_HEADINGS):
            section = "batting"
            continue
        if _is_heading(line, _BOWL_HEADINGS):
            section = "bowling"
            continue
        if _is_heading(line, _SKIP_HEADINGS):
            # A list of fall-of-wickets entries or names follows. None of it has
            # a column here, and none of it parses as a player, so everything up
            # to the next section or innings is skipped rather than refused.
            section = "skip"
            continue
        if section == "skip":
            continue

        if not innings:
            # Before the first innings block: an unlabelled header opens it, and
            # anything else is a title, a venue or a date this importer ignores.
            if header is not None:
                _open_innings(header)
                pending_number, section = None, None
            continue

        # Inside a block, the player grammars come first. An unlabelled header
        # and a player line are the same shape — a name followed by numbers — so
        # the only safe reading of "Deepak Chahar 4-0-31-1" is the one that
        # parses, and a new innings is what is left when nothing does.
        current = innings[-1]
        try:
            # The raw line, not the cleaned one: a column-aligned card says where
            # a name ends and its status begins with the width of the gap, and
            # squeezing the whitespace throws that away.
            parsed = (parse_bowling_line(raw_line) if section == "bowling"
                      else parse_batting_line(raw_line))
            if parsed:
                current["bowling" if section == "bowling" else "batting"].append(parsed)
                continue
            # An un-headed card: a bowling line is still recognisable by its
            # figures, so nobody has to add "Bowling" by hand.
            if section != "bowling":
                parsed = parse_bowling_line(raw_line)
                if parsed:
                    current["bowling"].append(parsed)
                    continue
        except ScorecardError as exc:
            raise ScorecardError(f"Line {lineno}: {exc}") from None
        if header is not None:
            _open_innings(header)
            pending_number, section = None, None
            continue
        raise ScorecardError(
            f"Line {lineno} isn't a batting or bowling line: {line!r}\n"
            "Batting: <name> <runs> (<balls>) — Bowling: <name> O-M-R-W")

    if len(innings) < INNINGS_PER_MATCH:
        raise ScorecardError(
            f"Expected {INNINGS_PER_MATCH} innings, found {len(innings)}. Each "
            "innings starts with a line like "
            "'Innings 1: Mumbai Indians 187/5 (20)', or "
            "'MUMBAI INDIANS INNINGS' with a 'Total:' line under it.")

    # Honour explicit "Innings 2 … / Innings 1 …" ordering when both are numbered.
    numbers = [i["number"] for i in innings[:INNINGS_PER_MATCH]]
    if sorted(n for n in numbers if n) == [1, 2]:
        innings[:INNINGS_PER_MATCH] = sorted(
            innings[:INNINGS_PER_MATCH], key=lambda i: i["number"])

    # Anything past the second innings is the Super Over: the bot's own file
    # appends its innings to the same list, headed by the same two sides. The
    # match is the first two — a Super Over is a separate contest, and the bot
    # keeps it out of the scoreline and the batting figures when it records one
    # itself. It is handed back so the caller can say it was left out, because
    # silently dropping half a file is how an import stops being trustworthy.
    extra = innings[INNINGS_PER_MATCH:]
    innings = innings[:INNINGS_PER_MATCH]

    sides = {_norm(innings[0]["team"]), _norm(innings[1]["team"])}
    if len(sides) == 1:
        raise ScorecardError(
            f"Both innings are headed {innings[0]['team']!r} — name the two "
            "sides differently so each innings can be matched to a team.")
    for block in extra:
        if _norm(block["team"]) not in sides:
            raise ScorecardError(
                f"This card has {len(innings) + len(extra)} innings and "
                f"{block['team']!r} is a third side. Two innings make a match; "
                "anything after them has to be the same two teams playing a "
                "Super Over.")

    for position, block in enumerate(innings, 1):
        block["number"] = position
        if block["runs"] is None:
            raise ScorecardError(
                f"No score found for {block['team']!r}. Put it on the innings "
                "header ('Innings 1: Mumbai Indians 187/5 (20)') or on a "
                "'Total: 187/5 (20)' line inside the innings.")
    return {"innings": innings, "extra_innings": extra,
            "result_text": result_text}


# ──────────────────────────────────────────────────────────────────────
# Resolving what was parsed against a real fixture
# ──────────────────────────────────────────────────────────────────────

def match_team(name, candidates):
    """The candidate team a written name means, or None when it's ambiguous.

    ``candidates`` is a list of ``TournamentTeam``. Matching runs strongest-first
    — exact name, short name or initials, prefix, substring — and stops at the
    first round that produces exactly one hit, mirroring how a player's typed
    team name is resolved elsewhere.
    """
    want = _norm(name)
    if not want:
        return None

    def _initials(text):
        parts = [p for p in _clean(text).split() if p]
        return "".join(p[0] for p in parts).casefold() if len(parts) > 1 else ""

    for predicate in (
            lambda tt: _norm(tt.name) == want,
            lambda tt: _norm(tt.short_name) == want or _initials(tt.name) == want,
            lambda tt: _norm(tt.name).startswith(want) or want.startswith(_norm(tt.name)),
            lambda tt: want in _norm(tt.name) or _norm(tt.name) in want):
        hits = [tt for tt in candidates if predicate(tt)]
        if len(hits) == 1:
            return hits[0]
    return None


def _team_user_id(session, team):
    """The ``User.id`` a team's stats belong to, or None.

    A Lets Play team *is* a user. A Challenge League team belongs to whoever owns
    the franchise, so its owner (then any co-owner) carries the imported figures
    — the same person a bot-played match would have credited. With nobody at all
    behind the team the match still records; only its per-player rows are
    skipped, because ``TournamentPlayerStats.user_id`` cannot be null.
    """
    from models import User
    from services import tournament_service
    tg_ids = []
    if getattr(team, "user_tg_id", None):
        tg_ids.append(int(team.user_tg_id))
    tg_ids += [int(i) for i in tournament_service.team_member_ids(team) or []]
    for tg_id in tg_ids:
        user = session.query(User).filter_by(telegram_id=tg_id).first()
        if user:
            return user.id
    return None


def _roster_index(session, team):
    """``{normalized name: ChallengePlayer}`` for a Challenge League team.

    Imported figures have to land on the *same* identity a bot-played match
    writes, or a player ends up with two leaderboard rows. That identity is the
    ``ChallengePlayer`` id, so names are resolved back to the squad.
    """
    from models import ChallengePlayer
    if not getattr(team, "challenge_team_id", None):
        return {}
    rows = (session.query(ChallengePlayer)
            .filter_by(team_id=team.challenge_team_id).all())
    return {_norm(p.name): p for p in rows if p.name}


def _resolve_player(name, index):
    """``(roster_id, player_id, resolved_name)`` for a written player name.

    Falls back to a surname match when the full name doesn't hit — scorecards
    are written by hand, and "Bumrah" is how people actually type it. An
    unresolved name still gets figures; it is simply keyed by the name itself,
    which keeps it consistent across every card that spells it the same way.
    """
    want = _norm(name)
    player = index.get(want)
    if player is None and want:
        surname = want.split()[-1]
        hits = [p for key, p in index.items()
                if key.split()[-1] == surname or key.startswith(want)]
        if len(hits) == 1:
            player = hits[0]
    if player is None:
        return None, None, _clean(name)
    return player.id, player.source_player_id, player.name


def _blank_line(user_id, name, team_name, roster_id=None, player_id=None):
    return {
        "user_id": user_id, "player_id": player_id, "roster_id": roster_id,
        "name": name, "team_name": team_name,
        "bat_runs": 0, "bat_balls": 0, "bat_fours": 0, "bat_sixes": 0,
        "bat_out": False, "batted": False,
        "bowl_wickets": 0, "bowl_runs": 0, "bowl_balls": 0, "bowled": False,
    }


def plan_import(session, fixture, parsed):
    """Work out exactly what recording ``parsed`` onto ``fixture`` would do.

    Returns a plan dict the caller can show before anything is written::

        {"fixture", "innings": [{team, runs, wickets, balls, ...}, ...],
         "winner_team_id", "result_text", "lines", "warnings", "swap_sides"}

    Raises :class:`ScorecardError` when the card cannot be recorded at all — the
    fixture is already played, or a team on the card isn't in the fixture.
    """
    from models import Tournament, TournamentTeam
    from services.tournament_service import _overs_to_balls

    if fixture.status == "completed":
        raise ScorecardError(
            "That fixture is already completed — remove its result from the "
            "tournament dashboard first, then import the card again.")
    if fixture.status != "scheduled":
        raise ScorecardError(
            f"That fixture is '{fixture.status}', not scheduled. A match still "
            "being played has to finish (or be cleared) before a written "
            "scorecard can replace it.")
    if not fixture.team1_id or not fixture.team2_id:
        raise ScorecardError("Both teams must be set on the fixture first.")

    teams = (session.query(TournamentTeam)
             .filter(TournamentTeam.id.in_([fixture.team1_id, fixture.team2_id]))
             .all())
    blocks = parsed["innings"]
    resolved = []
    for block in blocks:
        team = match_team(block["team"], teams)
        if team is None:
            names = " / ".join((tt.name or "?") for tt in teams)
            raise ScorecardError(
                f"{block['team']!r} isn't one of this fixture's teams ({names}).")
        resolved.append(team)
    if resolved[0].id == resolved[1].id:
        raise ScorecardError(
            f"Both innings resolved to {resolved[0].name!r} — check the team "
            "names on the innings headers.")

    tour = session.get(Tournament, fixture.tournament_id)
    max_overs = int(tour.overs) if tour and tour.overs else None
    warnings = []
    innings_out = []
    for block, team in zip(blocks, resolved):
        balls = _overs_to_balls(block["overs"])
        if not balls:
            warnings.append(
                f"No overs given for {team.name} — net run rate will treat the "
                "innings as 0 balls. Add '(20)' to the innings header to fix it.")
        elif max_overs and balls > max_overs * 6:
            raise ScorecardError(
                f"{block['overs']} overs for {team.name} is more than this "
                f"tournament's {max_overs}-over format.")
        innings_out.append({
            "team": team, "runs": block["runs"], "wickets": block["wickets"],
            "balls": balls, "overs": block["overs"],
        })

    # ``recompute_standings`` reads team1 as the side that batted first, so a
    # card whose first innings belongs to the fixture's team2 needs the two
    # slots swapped — otherwise every run is credited to the wrong side's net
    # run rate. The pair is unchanged, so nothing else about the fixture moves.
    swap_sides = resolved[0].id == fixture.team2_id

    # A Super Over's innings ride in the same file, and are not part of the
    # match: they never reach the scoreline, the net run rate or the batting
    # figures — exactly as when the bot records a Super Over itself. Say so,
    # rather than quietly reading half the file.
    for block in parsed.get("extra_innings") or []:
        warnings.append(
            f"Super Over innings ({block['team']} {block['runs']}/"
            f"{block['wickets']}) left out of the scoreline and the player "
            "stats — a Super Over is a separate contest. It still decides the "
            "winner through the result line.")

    winner_team_id, tie = _decide_winner(parsed, innings_out, resolved, warnings)
    result_text = parsed.get("result_text") or (
        "Match Tied" if tie else
        f"{next(tt.name for tt in resolved if tt.id == winner_team_id)} won")

    lines = _build_lines(session, blocks, resolved, warnings)
    return {
        "fixture": fixture, "innings": innings_out, "swap_sides": swap_sides,
        "winner_team_id": winner_team_id, "result_text": result_text[:300],
        "lines": lines, "warnings": warnings,
    }


def _decide_winner(parsed, innings_out, resolved, warnings):
    """``(winner_team_id, is_tie)`` from the result line, else from the runs.

    A written "Result:" line is the authority — it is the only thing that can
    say who took a Super Over, or who got a tie broken their way. It is only
    believed when it names one of the two sides; anything else falls back to the
    scores, with a warning so nobody thinks the line was honoured.
    """
    runs = [i["runs"] for i in innings_out]
    stated = parsed.get("result_text") or ""
    if stated:
        low = _norm(stated)
        if re.search(r"\b(tie|tied)\b", stated, re.I):
            return None, True
        named = [tt for tt in resolved if _norm(tt.name) and _norm(tt.name) in low]
        if len(named) == 1:
            if runs[0] != runs[1]:
                scored_winner = resolved[0] if runs[0] > runs[1] else resolved[1]
                if scored_winner.id != named[0].id:
                    warnings.append(
                        f"The result line says {named[0].name} won, but the "
                        f"scores say {scored_winner.name}. Recording the result "
                        "line — edit it if that's wrong.")
            return named[0].id, False
        warnings.append(
            "Couldn't tell from the result line who won, so the scores decide it.")
    if runs[0] == runs[1]:
        return None, True
    return (resolved[0] if runs[0] > runs[1] else resolved[1]).id, False


def _build_lines(session, blocks, resolved, warnings):
    """Per-player scorecard lines, in the shape a bot-played match records.

    A player bats in their side's innings and bowls in the other one, so both
    halves fold into a single line per player — the same aggregation
    ``tournament_service._collect_player_lines`` does for a live match, which is
    what lets an imported match and a played one share a leaderboard row.
    """
    from services.tournament_service import _overs_to_balls

    user_ids, indexes = {}, {}
    for team in resolved:
        user_ids[team.id] = _team_user_id(session, team)
        indexes[team.id] = _roster_index(session, team)
        if user_ids[team.id] is None:
            warnings.append(
                f"No bot account is linked to {team.name}, so its players won't "
                "appear in the tournament leaderboards. The result and the "
                "points table are recorded either way.")

    lines = {}

    def _line_for(team, name):
        roster_id, player_id, resolved_name = _resolve_player(
            name, indexes[team.id])
        key = (team.id, roster_id or ("n", _norm(name)))
        line = lines.get(key)
        if line is None:
            line = _blank_line(user_ids[team.id], resolved_name, team.name,
                               roster_id=roster_id, player_id=player_id)
            lines[key] = line
            if roster_id is None and indexes[team.id]:
                warnings.append(
                    f"{name!r} isn't in {team.name}'s squad — recorded under "
                    "that name.")
        return line

    for position, block in enumerate(blocks):
        batting_team = resolved[position]
        bowling_team = resolved[1 - position]
        for entry in block["batting"]:
            line = _line_for(batting_team, entry["name"])
            # An innings is a ball faced or a dismissal — the same test the live
            # match uses. A row printed for somebody who never went out to the
            # middle must not count against their average.
            if entry["balls"] or entry["out"]:
                line["batted"] = True
            line["bat_runs"] += entry["runs"]
            line["bat_balls"] += entry["balls"]
            line["bat_fours"] += entry["fours"]
            line["bat_sixes"] += entry["sixes"]
            line["bat_out"] = line["bat_out"] or bool(entry["out"])
        for entry in block["bowling"]:
            line = _line_for(bowling_team, entry["name"])
            line["bowled"] = True
            line["bowl_wickets"] += entry["wickets"]
            line["bowl_runs"] += entry["runs"]
            line["bowl_balls"] += _overs_to_balls(entry["overs"])

    return [line for line in lines.values() if line["user_id"] is not None]


# ──────────────────────────────────────────────────────────────────────
# Writing it
# ──────────────────────────────────────────────────────────────────────

def record_import(session, plan):
    """Write a plan from :func:`plan_import` onto its fixture. Caller commits.

    Fills the fixture exactly as a bot-played match would — scoreline, winner,
    per-player scorecard — then rebuilds the standings and the player aggregates
    from every recorded match, so the import is fully reversible: removing the
    match from the dashboard rebuilds the tournament without it.
    """
    import json
    from datetime import datetime
    from services import tournament_service

    fixture = plan["fixture"]
    first, second = plan["innings"]
    if plan["swap_sides"]:
        fixture.team1_id, fixture.team2_id = first["team"].id, second["team"].id

    fixture.status = "completed"
    fixture.match_id = None            # not a bot-played match
    fixture.winner_team_id = plan["winner_team_id"]
    fixture.result_text = plan["result_text"]
    fixture.inn1_runs, fixture.inn1_wickets = first["runs"], first["wickets"]
    fixture.inn1_balls = first["balls"]
    fixture.inn2_runs, fixture.inn2_wickets = second["runs"], second["wickets"]
    fixture.inn2_balls = second["balls"]
    fixture.scorecard_json = (json.dumps(plan["lines"], default=str)
                              if plan["lines"] else None)
    fixture.completed_at = datetime.utcnow()

    session.flush()
    tournament_service.recompute_tournament(session, fixture.tournament_id)

    # A knockout fixture still has to move its winner into the next round — the
    # standings rebuild only looks at league stages.
    try:
        from services import knockout_service
        knockout_service.advance_bracket(session, fixture)
    except Exception:
        logger.exception("Knockout advancement failed for tournament %s",
                         fixture.tournament_id)

    if fixture.stage == "final" and fixture.winner_team_id:
        from models import Tournament
        tournament_service._champion_news(
            session, session.get(Tournament, fixture.tournament_id), fixture)

    logger.info("Imported written scorecard onto fixture %s (winner_team=%s, "
                "%s player lines)", fixture.id, plan["winner_team_id"],
                len(plan["lines"]))
    return fixture


# What the bot's own archived scorecard looks like, and the shortest thing
# somebody can type by hand. Both are read; the first is the one people already
# have, because the bot writes it for every match it plays.
BOT_FILE_HINT = ("MatchNo<id>.txt — the scorecard file the bot already archives "
                 "for every match — is read as it is. Just reply to it.")

TEMPLATE = """Innings 1: <Team A> 187/5 (20)
Batting
Rohit Sharma 62 (41) 6x4 2x6 not out
Ishan Kishan 45 (30) 4x4 1x6 c Dhoni b Jadeja
Bowling
Deepak Chahar 4-0-31-1
Ravindra Jadeja 4-0-28-2

Innings 2: <Team B> 180/8 (20)
Batting
Ruturaj Gaikwad 55 (38) 5x4 1x6 b Bumrah
Bowling
Jasprit Bumrah 4-0-24-3

Result: <Team A> won by 7 runs"""
