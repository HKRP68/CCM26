"""Native Rich Text for World T20 Championship statistics.

Two surfaces, kept deliberately apart exactly as the Challenge League
Tournament keeps ``/tournamentstats`` and ``/statstour`` apart:

* **Category leaderboards** — ``/wt20cstats`` (alias ``/wt20ctournamentstats``).
  Grouped Batting / Bowling / Fielding / Performance boards with category
  buttons. Each board ranks :data:`BOARD_LIMIT` players, shows
  :data:`BOARD_OPEN` open and hides the rest behind a tap, the same split the
  Tournament boards use.

* **One player's full figures** — ``/wt20cstatstour NAME`` (alias
  ``/wt20cplayer NAME``). Independently collapsible native sections for batting,
  bowling, fielding, awards, the additional record sets, a series-by-series
  table and recent form.

Every view renders a Bot API block tree *and* an escaped HTML fallback that
carries the same figures, because :mod:`services.rich_message` falls back to the
HTML whenever the native send is refused. The data all comes from
:func:`services.t20_championship_stats.aggregate`; nothing here queries the
database or decides a rule.
"""
import html

from services import rich_message as R
from services import t20_championship_stats as stats
from services.t20_championship import match_reference, series_name

# How deep each board ranks and how much shows without a tap — the Tournament
# board's own numbers, so the two competitions read the same.
BOARD_OPEN = 10
BOARD_LIMIT = 25

# Rate-board qualifications, matching the Tournament defaults: a strike rate
# needs a real sample of balls, an economy a real sample of overs, and an
# average at least one dismissal (a never-out batter has no defined average).
MIN_BALLS_FOR_SR = 20
MIN_BALLS_FOR_ECON = 12

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


# ── Figures off an aggregate row ─────────────────────────────────────────

def _bat_sr(row):
    return row["bat_runs"] * 100.0 / row["bat_balls"] if row["bat_balls"] else 0.0


def _bat_avg(row):
    return row["bat_runs"] / row["bat_outs"] if row["bat_outs"] else None


def _econ(row):
    return row["bowl_runs"] * 6.0 / row["bowl_balls"] if row["bowl_balls"] else 0.0


def _bowl_avg(row):
    return row["bowl_runs"] / row["bowl_wickets"] if row["bowl_wickets"] else None


def _bowl_sr(row):
    return row["bowl_balls"] / row["bowl_wickets"] if row["bowl_wickets"] else None


def _best(row):
    return (f'{row["best_bowl_wickets"]}/{row["best_bowl_runs"]}'
            if row["best_bowl_runs"] >= 0 else None)


def _hs(row):
    return f'{row["highest_score"]}{"*" if row["highest_score_not_out"] else ""}'


def _boundary_pct(row):
    boundary_runs = row["bat_fours"] * 4 + row["bat_sixes"] * 6
    return boundary_runs * 100.0 / row["bat_runs"] if row["bat_runs"] else None


def _dot_pct(row):
    return row["bowl_dots"] * 100.0 / row["bowl_balls"] if row["bowl_balls"] else None


# ── Leaderboard categories ───────────────────────────────────────────────
# Each category knows how to rank a pool of aggregate rows and what columns to
# show. ``value`` is the ranked figure; ``cells`` are the supporting columns
# (their headers come from the first row that carries them, like the Tournament
# board). ``sort`` returns a key ranked high-to-low unless ``ascending``; every
# key ends in a deterministic ``(name, team_id)`` tie-break through :func:`_rank`.


class _Cat:
    def __init__(self, key, label, group, column, value, *, cells=None,
                 sort=None, eligible=None, ascending=False, matches=True, note=None):
        self.key = key
        self.label = label
        self.group = group
        self.column = column
        self.value = value
        self.cells = cells or (lambda row: [])
        self.sort = sort or (lambda row: 0)
        self.eligible = eligible or (lambda row: True)
        self.ascending = ascending
        self.matches = matches
        self.note = note


def _m(row):
    return [("M", str(row["matches"]))]


_CATEGORIES = [
    # ── Batting ──
    _Cat("runs", "🏏 Most Runs", "Batting", "RUNS", lambda r: str(r["bat_runs"]),
         cells=lambda r: [("BALLS", str(r["bat_balls"])), ("SR", f"{_bat_sr(r):.2f}"), *_m(r)],
         sort=lambda r: (r["bat_runs"], r["bat_sixes"]), eligible=lambda r: r["bat_innings"] > 0),
    _Cat("hs", "⭐ Highest Score", "Batting", "HS", _hs,
         cells=lambda r: [("BALLS", str(r["highest_score_balls"]) if r["highest_score_balls"] else "—"),
                          ("SR", f'{r["highest_score"] * 100.0 / r["highest_score_balls"]:.2f}'
                           if r["highest_score_balls"] else "—")],
         sort=lambda r: (r["highest_score"], int(r["highest_score_not_out"])),
         eligible=lambda r: r["bat_innings"] > 0, matches=False),
    _Cat("bavg", "⚖️ Batting Average", "Batting", "AVG", lambda r: f"{_bat_avg(r):.2f}",
         cells=lambda r: [("RUNS", str(r["bat_runs"])), ("OUTS", str(r["bat_outs"])), *_m(r)],
         sort=lambda r: _bat_avg(r), eligible=lambda r: _bat_avg(r) is not None),
    _Cat("bsr", "⚡ Batting SR", "Batting", "SR", lambda r: f"{_bat_sr(r):.2f}",
         cells=lambda r: [("RUNS", str(r["bat_runs"])), ("BALLS", str(r["bat_balls"])), *_m(r)],
         sort=lambda r: _bat_sr(r), eligible=lambda r: r["bat_balls"] >= MIN_BALLS_FOR_SR,
         note=f"Strike-rate board needs ≥{MIN_BALLS_FOR_SR} balls faced."),
    _Cat("4s", "4️⃣ Most Fours", "Batting", "4s", lambda r: str(r["bat_fours"]),
         cells=lambda r: [("RUNS", str(r["bat_runs"])), ("SR", f"{_bat_sr(r):.2f}"), *_m(r)],
         sort=lambda r: r["bat_fours"], eligible=lambda r: r["bat_fours"] > 0),
    _Cat("6s", "6️⃣ Most Sixes", "Batting", "6s", lambda r: str(r["bat_sixes"]),
         cells=lambda r: [("RUNS", str(r["bat_runs"])), ("SR", f"{_bat_sr(r):.2f}"), *_m(r)],
         sort=lambda r: r["bat_sixes"], eligible=lambda r: r["bat_sixes"] > 0),
    _Cat("50s", "🧮 Most Fifties", "Batting", "50s", lambda r: str(r["fifties"]),
         cells=lambda r: [("100s", str(r["hundreds"])), ("RUNS", str(r["bat_runs"])), *_m(r)],
         sort=lambda r: (r["fifties"], r["hundreds"]), eligible=lambda r: r["fifties"] > 0),
    _Cat("100s", "💯 Most Hundreds", "Batting", "100s", lambda r: str(r["hundreds"]),
         cells=lambda r: [("50s", str(r["fifties"])), ("RUNS", str(r["bat_runs"])), *_m(r)],
         sort=lambda r: (r["hundreds"], r["fifties"]), eligible=lambda r: r["hundreds"] > 0),
    _Cat("150s", "📈 Most 150+", "Batting", "150+", lambda r: str(r["hundred_fifties"]),
         cells=lambda r: [("100s", str(r["hundreds"])), ("HS", _hs(r)), *_m(r)],
         sort=lambda r: (r["hundred_fifties"], r["highest_score"]),
         eligible=lambda r: r["hundred_fifties"] > 0),
    _Cat("f50", "🚀 Fastest Fifty", "Batting", "BALLS", lambda r: str(r["fastest_fifty"]),
         cells=lambda r: [("RUNS", str(r["bat_runs"])), ("HS", _hs(r))],
         sort=lambda r: r["fastest_fifty"], ascending=True, matches=False,
         eligible=lambda r: r["fastest_fifty"] is not None),
    _Cat("f100", "🚀 Fastest Hundred", "Batting", "BALLS", lambda r: str(r["fastest_hundred"]),
         cells=lambda r: [("RUNS", str(r["bat_runs"])), ("HS", _hs(r))],
         sort=lambda r: r["fastest_hundred"], ascending=True, matches=False,
         eligible=lambda r: r["fastest_hundred"] is not None),
    _Cat("f150", "🚀 Fastest 150", "Batting", "BALLS", lambda r: str(r["fastest_one_fifty"]),
         cells=lambda r: [("RUNS", str(r["bat_runs"])), ("HS", _hs(r))],
         sort=lambda r: r["fastest_one_fifty"], ascending=True, matches=False,
         eligible=lambda r: r["fastest_one_fifty"] is not None),
    _Cat("duck", "🦆 Most Ducks", "Batting", "DUCKS", lambda r: str(r["ducks"]),
         cells=lambda r: [("INNS", str(r["bat_innings"])), *_m(r)],
         sort=lambda r: r["ducks"], eligible=lambda r: r["ducks"] > 0),
    # ── Bowling ──
    _Cat("wkts", "🎯 Most Wickets", "Bowling", "WKTS", lambda r: str(r["bowl_wickets"]),
         cells=lambda r: [("OVERS", stats.overs(r["bowl_balls"])), ("ECON", f"{_econ(r):.2f}"),
                          ("BEST", _best(r) or "—"), *_m(r)],
         sort=lambda r: (r["bowl_wickets"], -r["bowl_runs"]), eligible=lambda r: r["bowl_wickets"] > 0),
    _Cat("bfig", "💥 Best Figures", "Bowling", "BEST", lambda r: _best(r),
         cells=lambda r: [("OVERS", stats.overs(r["bowl_balls"]))],
         sort=lambda r: (r["best_bowl_wickets"], -r["best_bowl_runs"]),
         eligible=lambda r: r["best_bowl_runs"] >= 0, matches=False),
    _Cat("bowa", "🛡️ Bowling Average", "Bowling", "AVG", lambda r: f"{_bowl_avg(r):.2f}",
         cells=lambda r: [("WKTS", str(r["bowl_wickets"])), ("RUNS", str(r["bowl_runs"])), *_m(r)],
         sort=lambda r: _bowl_avg(r), ascending=True, eligible=lambda r: _bowl_avg(r) is not None),
    _Cat("econ", "🪙 Best Economy", "Bowling", "ECON", lambda r: f"{_econ(r):.2f}",
         cells=lambda r: [("WKTS", str(r["bowl_wickets"])), ("OVERS", stats.overs(r["bowl_balls"])), *_m(r)],
         sort=lambda r: _econ(r), ascending=True, eligible=lambda r: r["bowl_balls"] >= MIN_BALLS_FOR_ECON,
         note=f"Economy board needs ≥{MIN_BALLS_FOR_ECON} legal balls bowled."),
    _Cat("bosr", "⚡ Bowling SR", "Bowling", "SR", lambda r: f"{_bowl_sr(r):.2f}",
         cells=lambda r: [("WKTS", str(r["bowl_wickets"])), ("OVERS", stats.overs(r["bowl_balls"])), *_m(r)],
         sort=lambda r: _bowl_sr(r), ascending=True, eligible=lambda r: _bowl_sr(r) is not None),
    _Cat("dots", "⚪ Most Dot Balls", "Bowling", "DOTS", lambda r: str(r["bowl_dots"]),
         cells=lambda r: [("OVERS", stats.overs(r["bowl_balls"])), ("WKTS", str(r["bowl_wickets"])), *_m(r)],
         sort=lambda r: r["bowl_dots"], eligible=lambda r: r["bowl_dots"] > 0),
    _Cat("3fer", "3️⃣ Most 3-Fers", "Bowling", "3-FER", lambda r: str(r["three_fers"]),
         cells=lambda r: [("WKTS", str(r["bowl_wickets"])), *_m(r)],
         sort=lambda r: (r["three_fers"], r["bowl_wickets"]), eligible=lambda r: r["three_fers"] > 0),
    _Cat("4fer", "4️⃣ Most 4-Fers", "Bowling", "4-FER", lambda r: str(r["four_fers"]),
         cells=lambda r: [("WKTS", str(r["bowl_wickets"])), *_m(r)],
         sort=lambda r: (r["four_fers"], r["bowl_wickets"]), eligible=lambda r: r["four_fers"] > 0,
         note="A four-wicket haul is an innings of four or more wickets."),
    _Cat("5fer", "5️⃣ Most 5-Fers", "Bowling", "5-FER", lambda r: str(r["five_fers"]),
         cells=lambda r: [("WKTS", str(r["bowl_wickets"])), *_m(r)],
         sort=lambda r: (r["five_fers"], r["bowl_wickets"]), eligible=lambda r: r["five_fers"] > 0),
    _Cat("hat", "🎩 Most Hat-tricks", "Bowling", "HAT", lambda r: str(r["hattricks"] or 0),
         cells=lambda r: [("WKTS", str(r["bowl_wickets"])), *_m(r)],
         sort=lambda r: (r["hattricks"] or 0, r["bowl_wickets"]),
         eligible=lambda r: bool(r["hattricks"])),
    # ── Fielding ──
    _Cat("cat", "🙌 Most Catches", "Fielding", "CAT", lambda r: str(r["catches"]),
         cells=lambda r: [*_m(r)], sort=lambda r: r["catches"], eligible=lambda r: r["catches"] > 0),
    _Cat("stmp", "🧤 Most Stumpings", "Fielding", "STMP", lambda r: str(r["stumpings"]),
         cells=lambda r: [*_m(r)], sort=lambda r: r["stumpings"], eligible=lambda r: r["stumpings"] > 0),
    _Cat("ro", "🏃 Most Run-outs", "Fielding", "R/O", lambda r: str(r["runouts"]),
         cells=lambda r: [*_m(r)], sort=lambda r: r["runouts"], eligible=lambda r: r["runouts"] > 0),
    _Cat("fdis", "🧯 Most Fielding Dismissals", "Fielding", "DIS", lambda r: str(r["dismissals"]),
         cells=lambda r: [("CAT", str(r["catches"])), ("STMP", str(r["stumpings"])),
                          ("R/O", str(r["runouts"])), *_m(r)],
         sort=lambda r: r["dismissals"], eligible=lambda r: r["dismissals"] > 0),
    _Cat("kdis", "🧤 Most Keeper Dismissals", "Fielding", "WK", lambda r: str(r["keeper_dismissals"]),
         cells=lambda r: [("CAT", str(r["keeper_catches"])), ("STMP", str(r["stumpings"])), *_m(r)],
         sort=lambda r: r["keeper_dismissals"], eligible=lambda r: r["keeper_dismissals"] > 0),
    # ── Performance ──
    _Cat("mvp", "🏅 MVP Rankings", "Performance", "PTS", lambda r: f'{r["mvp"]:g}',
         cells=lambda r: [("RUNS", str(r["bat_runs"])), ("WKTS", str(r["bowl_wickets"])),
                          ("POTM", str(r["potm"])), *_m(r)],
         sort=lambda r: r["mvp"], eligible=lambda r: r["mvp"] > 0),
    _Cat("bbat", "📊 Best Batter", "Performance", "BAT", lambda r: f'{r["batting"]:.1f}',
         cells=lambda r: [("RUNS", str(r["bat_runs"])), *_m(r)],
         sort=lambda r: r["batting"], eligible=lambda r: r["batting"] > 0),
    _Cat("bbowl", "📊 Best Bowler", "Performance", "BOWL", lambda r: f'{r["bowling"]:.1f}',
         cells=lambda r: [("WKTS", str(r["bowl_wickets"])), *_m(r)],
         sort=lambda r: r["bowling"], eligible=lambda r: r["bowling"] > 0 and r["bowl_wickets"] > 0),
    _Cat("allr", "🌟 Best All-rounder", "Performance", "AR", lambda r: f'{min(r["batting"], r["bowling"]):.1f}',
         cells=lambda r: [("RUNS", str(r["bat_runs"])), ("WKTS", str(r["bowl_wickets"])), *_m(r)],
         sort=lambda r: min(r["batting"], r["bowling"]),
         eligible=lambda r: r["bat_runs"] > 0 and r["bowl_wickets"] > 0),
    _Cat("bwk", "🧤 Best Wicketkeeper", "Performance", "WK", lambda r: str(r["keeper_dismissals"]),
         cells=lambda r: [("CAT", str(r["keeper_catches"])), ("STMP", str(r["stumpings"])), *_m(r)],
         sort=lambda r: r["keeper_dismissals"], eligible=lambda r: r["keeper_dismissals"] > 0),
    _Cat("potm", "⭐ Most POTM", "Performance", "POTM", lambda r: str(r["potm"]),
         cells=lambda r: [*_m(r)], sort=lambda r: r["potm"], eligible=lambda r: r["potm"] > 0),
    _Cat("pots", "🌟 Most POTS", "Performance", "POTS", lambda r: str(r["pots"]),
         cells=lambda r: [*_m(r)], sort=lambda r: r["pots"], eligible=lambda r: r["pots"] > 0),
    _Cat("mats", "🎮 Most Matches", "Performance", "M", lambda r: str(r["matches"]),
         cells=lambda r: [("RUNS", str(r["bat_runs"])), ("WKTS", str(r["bowl_wickets"]))],
         sort=lambda r: r["matches"], eligible=lambda r: r["matches"] > 0, matches=False),
]
_BY_KEY = {cat.key: cat for cat in _CATEGORIES}
GROUPS = ["Batting", "Bowling", "Fielding", "Performance"]
GROUP_DEFAULT = {group: next(c.key for c in _CATEGORIES if c.group == group) for group in GROUPS}
DEFAULT_CATEGORY = "runs"


def category(key):
    return _BY_KEY.get(key)


def is_category(key):
    return key in _BY_KEY


def group_categories(group):
    """``(key, label)`` for every category in one group, in display order."""
    return [(cat.key, cat.label) for cat in _CATEGORIES if cat.group == group]


# ── Ranking ──────────────────────────────────────────────────────────────

def _rank(cat, rows):
    """The eligible rows for one category, best first, ties broken by name."""
    pool = [row for row in rows if cat.eligible(row)]
    pool.sort(key=lambda row: (row["name"].casefold(), row["team_id"]))
    pool.sort(key=cat.sort, reverse=not cat.ascending)
    return pool[:BOARD_LIMIT]


def _board_caption(rows):
    if len(rows) <= BOARD_OPEN:
        return f"Top {len(rows)}" if rows else "Top 10"
    return f"Top {BOARD_OPEN} of {len(rows)}"


def scope_label(championship, team_id, series_id, rows):
    bits = []
    team = next((t for t in championship.teams if t.id == team_id), None)
    if team:
        bits.append(team.name)
    series = next((s for s in rows if s.id == series_id), None) if series_id else None
    if series:
        bits.append(series_name(series))
    return " · ".join(bits)


# ── Leaderboard rendering ─────────────────────────────────────────────────

def _rank_cells(cat, rows, start=1):
    extra_heads = next(([head for head, _ in cat.cells(row)] for row in rows), [])
    header = [R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("PLAYER"), header=True),
              R.cell(R.bold("TEAM"), header=True),
              R.cell(R.bold(cat.column), header=True, align="right")]
    header += [R.cell(R.bold(head), header=True, align="right") for head in extra_heads]
    cells = [header]
    for position, row in enumerate(rows, start):
        values = dict(cat.cells(row))
        cells.append([R.cell(_MEDALS.get(position, f"{position}."), align="center"),
                      R.cell(row["name"]), R.cell(row["team_name"] or "—"),
                      R.cell(R.bold(str(cat.value(row))), align="right")]
                     + [R.cell(values.get(head, "—"), align="right") for head in extra_heads])
    return cells


def _board_title(championship, scope):
    return f"🏆 {championship.name} · Championship Stats" + (f" · {scope}" if scope else "")


def leaderboard_blocks(championship, cat, rows, scope=""):
    ranked = _rank(cat, rows)
    blocks = [R.heading(_board_title(championship, scope), size=2),
              R.paragraph([R.bold(cat.label), f" · {_board_caption(ranked)}"])]
    if not ranked:
        blocks.append(R.paragraph(R.italic("No qualifying players yet.")))
        if cat.note:
            blocks.append(R.footer(cat.note))
        return blocks
    blocks.append(R.table(_rank_cells(cat, ranked[:BOARD_OPEN]),
                          bordered=True, striped=True, compact=True))
    rest = ranked[BOARD_OPEN:]
    if rest:
        blocks.append(R.details(R.bold(f"👇 Ranks {BOARD_OPEN + 1}–{len(ranked)}"),
                                [R.table(_rank_cells(cat, rest, start=BOARD_OPEN + 1),
                                         bordered=True, striped=True, compact=True)]))
    footer = [cat.note] if cat.note else []
    footer.append(f"One player's full figures: /{championship.command}statstour NAME")
    blocks.append(R.footer(" · ".join(footer)))
    return blocks


def _rank_line_html(cat, position, row):
    rank = _MEDALS.get(position, f"{position}.")
    team = f" <i>({html.escape(str(row['team_name']))})</i>" if row.get("team_name") else ""
    head = f"{rank} <b>{html.escape(str(row['name']))}</b>{team}"
    detail = f"<b>{html.escape(str(cat.value(row)))}</b> {cat.column}"
    extras = [f"{head}: {value}" for head, value in cat.cells(row) if head != "M"]
    matches = next((value for head, value in cat.cells(row) if head == "M"), None)
    if extras:
        detail += " (" + " · ".join(html.escape(bit) for bit in extras) + ")"
    if matches is not None:
        detail += f" · 📊 {matches} M"
    return f"{head}\n     — {detail}"


def leaderboard_html(championship, cat, rows, scope=""):
    ranked = _rank(cat, rows)
    lines = [f"🏆 <b>{html.escape(championship.name)}</b> — Championship Stats"
             + (f" · {html.escape(scope)}" if scope else ""),
             f"<b>{html.escape(cat.label)}</b> · {_board_caption(ranked)}", ""]
    if not ranked:
        lines.append("<i>No qualifying players yet.</i>")
    else:
        lines += [_rank_line_html(cat, i, row) for i, row in enumerate(ranked[:BOARD_OPEN], 1)]
        rest = ranked[BOARD_OPEN:]
        if rest:
            lines += ["", f"<b>👇 Ranks {BOARD_OPEN + 1}–{len(ranked)}</b> <i>(tap to expand)</i>",
                      "<blockquote expandable>"
                      + "\n".join(_rank_line_html(cat, i, row)
                                  for i, row in enumerate(rest, BOARD_OPEN + 1))
                      + "</blockquote>"]
    if cat.note:
        lines += ["", f"<i>{html.escape(cat.note)}</i>"]
    return "\n".join(lines)


# ── Individual player figures ─────────────────────────────────────────────

def _bat_pairs(row):
    """Batting fields in the exact order and labels the Tournament card uses."""
    fig = stats.player_figures(row)
    return [("Inns", str(row["bat_innings"])), ("Runs", str(row["bat_runs"])),
            ("50s", str(row["fifties"])), ("100s", str(row["hundreds"])),
            ("4s/6s", f'{row["bat_fours"]}/{row["bat_sixes"]}'),
            ("Avg", fig["average"]), ("SR", fig["strike_rate"]),
            ("Ducks", str(row["ducks"])), ("HS", fig["highest"])]


def _bowl_pairs(row):
    fig = stats.player_figures(row)
    return [("Inns", str(row["bowl_innings"])), ("Wkts", str(row["bowl_wickets"])),
            ("3-Fers", str(row["three_fers"])), ("5-Fers", str(row["five_fers"])),
            ("Hattricks", str(row["hattricks"] if row["hattricks"] is not None else "—")),
            ("Avg", fig["bowling_average"]), ("Econ", fig["economy"]),
            ("SR", fig["bowling_strike_rate"]), ("BBF", fig["best"]),
            ("Overs", stats.overs(row["bowl_balls"]))]


def _fielding_pairs(row):
    return [("Catches", str(row["catches"])), ("Keeper catches", str(row["keeper_catches"])),
            ("Stumpings", str(row["stumpings"])), ("Run-outs", str(row["runouts"])),
            ("Total dismissals", str(row["dismissals"])),
            ("WK dismissals", str(row["keeper_dismissals"]))]


def _awards_pairs(row):
    return [("POTM", str(row["potm"])), ("POTS", str(row["pots"])),
            ("Batting impact", f'{row["batting"]:.1f}'),
            ("Bowling impact", f'{row["bowling"]:.1f}'),
            ("MVP points", f'{row["mvp"]:.1f}')]


def _add_bat_pairs(row):
    pct = _boundary_pct(row)
    return [("Balls faced", str(row["bat_balls"])), ("Not outs", str(row["not_outs"])),
            ("150+ scores", str(row["hundred_fifties"])),
            ("HS balls", str(row["highest_score_balls"]) if row["highest_score_balls"] else "—"),
            ("Fastest 50", str(row["fastest_fifty"]) if row["fastest_fifty"] is not None else "—"),
            ("Fastest 100", str(row["fastest_hundred"]) if row["fastest_hundred"] is not None else "—"),
            ("Fastest 150", str(row["fastest_one_fifty"]) if row["fastest_one_fifty"] is not None else "—"),
            ("Boundary %", f"{pct:.1f}%" if pct is not None else "—")]


def _add_bowl_pairs(row):
    pct = _dot_pct(row)
    return [("Balls", str(row["bowl_balls"])), ("Runs conceded", str(row["bowl_runs"])),
            ("Dot balls", str(row["bowl_dots"])),
            ("Dot-ball %", f"{pct:.1f}%" if pct is not None else "—"),
            ("4-Fers", str(row["four_fers"])),
            # Maidens and per-ball phase splits are not captured in the stored
            # scorecards, so they are shown as unavailable rather than a false 0.
            ("Maidens", "—")]


def _pairs_table(pairs, caption):
    return R.table([[R.cell(R.bold(label)), R.cell(value, align="right")] for label, value in pairs],
                   bordered=True, compact=True, caption=R.bold(caption))


def _pairs_html(pairs):
    return "\n".join(f"{html.escape(label)}: {html.escape(str(value))}" for label, value in pairs)


def _side_label(series, team_id):
    if series.is_final and series.stadium:
        return "N"
    return "H" if series.home_id == team_id else "A"


def _series_rows_for(series_rows, team_id, identity):
    out = []
    for series, row in stats.series_breakdown(series_rows, team_id, identity):
        opponent = series.away if series.home_id == team_id else series.home
        awards = []
        if row["potm"]:
            awards.append(f'{row["potm"]}×POTM')
        if row["pots"]:
            awards.append(f'{row["pots"]}×POTS')
        out.append((series, dict(
            opponent=opponent.name, ref=f"S{series.id}", side=_side_label(series, team_id),
            matches=row["matches"], runs=row["bat_runs"], hs=_hs(row),
            wkts=row["bowl_wickets"], bbf=_best(row) or "—", awards=" ".join(awards) or "—")))
    return out


def _form_result(fixture):
    if fixture.result == "tie":
        return "Tie"
    if fixture.result == "no_result":
        return "No result"
    return fixture.result or "—"


def player_cards(championship, series_rows, matched):
    """``(blocks, html)`` for one or more matched players' full figures."""
    blocks = [R.heading(f"🏆 {championship.name} · Player Statistics", size=2),
              R.paragraph(R.italic("Tap a section to expand."))]
    parts = [f"🏆 <b>{html.escape(championship.name)}</b> — Player Statistics",
             "<i>Tap a section to expand.</i>"]
    legacy = False
    for row in matched:
        team_id, identity = stats.player_key(row)
        name = row["name"]
        heading = [R.bold(name)]
        if row["team_name"]:
            heading.append(f"  ·  {row['team_name']}")
        blocks.append(R.paragraph(["👤 ", *heading]))
        blocks.append(R.paragraph([R.bold(f"📊 This Season Stats — {name}"),
                                   f"  ·  🎮 Matches: {row['matches']}"]))
        parts.append(f"\n👤 <b>{html.escape(name)}</b>"
                     + (f" · {html.escape(row['team_name'])}" if row["team_name"] else ""))
        parts.append(f"<b>📊 This Season Stats — {html.escape(name)}</b> · 🎮 Matches: {row['matches']}")

        sections = [("🏏 Batting", _bat_pairs(row), True),
                    ("🎯 Bowling", _bowl_pairs(row), True),
                    ("🧤 Fielding", _fielding_pairs(row), False),
                    ("🏅 Awards & Performance", _awards_pairs(row), False),
                    ("➕ Additional Batting Records", _add_bat_pairs(row), False),
                    ("➕ Additional Bowling Records", _add_bowl_pairs(row), False)]
        for title, pairs, is_open in sections:
            blocks.append(R.details(R.bold(title), [_pairs_table(pairs, title)], is_open=is_open))
            parts.append(f"<b>{html.escape(title)}</b>\n<blockquote expandable>"
                         f"{_pairs_html(pairs)}</blockquote>")

        breakdown = _series_rows_for(series_rows, team_id, identity)
        if breakdown:
            cells = [[R.cell(R.bold(h), header=True) for h in
                      ("Opp", "Ref", "H/A", "M", "Runs", "HS", "Wkts", "BBF", "Awards")]]
            for _, info in breakdown:
                cells.append([R.cell(info["opponent"]), R.cell(info["ref"]), R.cell(info["side"]),
                              R.cell(str(info["matches"]), align="right"),
                              R.cell(str(info["runs"]), align="right"), R.cell(info["hs"], align="right"),
                              R.cell(str(info["wkts"]), align="right"), R.cell(info["bbf"], align="right"),
                              R.cell(info["awards"])])
            blocks.append(R.details(R.bold("🗂 Series by Series"),
                                    [R.table(cells, bordered=True, compact=True, striped=True)]))
            html_rows = ["Opp · Ref · H/A · M · Runs · HS · Wkts · BBF · Awards"]
            html_rows += [" · ".join(html.escape(str(v)) for v in
                          (i["opponent"], i["ref"], i["side"], i["matches"], i["runs"],
                           i["hs"], i["wkts"], i["bbf"], i["awards"])) for _, i in breakdown]
            parts.append("<b>🗂 Series by Series</b>\n<blockquote expandable>"
                         + "\n".join(html_rows) + "</blockquote>")

        form = stats.recent_form(series_rows, team_id, identity)
        if form:
            cells = [[R.cell(R.bold(h), header=True) for h in
                      ("Fixture", "Opponent", "Runs (Balls)", "Wkts/Runs", "Result")]]
            html_rows = ["Fixture · Opponent · Runs (Balls) · Wkts/Runs · Result"]
            for entry in form:
                fixture = entry["fixture"]
                series = entry["series"]
                opponent = series.away if series.home_id == team_id else series.home
                runs = ("DNB" if not entry["batted"]
                        else f'{entry["bat_runs"]}{"" if entry["bat_out"] else "*"} ({entry["bat_balls"]})')
                bowl = ("Did not bowl" if not entry["bowled"]
                        else f'{entry["bowl_wickets"]}/{entry["bowl_runs"]}')
                result = _form_result(fixture)
                cells.append([R.cell(match_reference(fixture)), R.cell(opponent.name),
                              R.cell(runs, align="right"), R.cell(bowl, align="right"), R.cell(result)])
                html_rows.append(" · ".join(html.escape(str(v)) for v in
                                 (match_reference(fixture), opponent.name, runs, bowl, result)))
            blocks.append(R.details(R.bold("📈 Recent Form"),
                                    [R.table(cells, bordered=True, compact=True, striped=True)]))
            parts.append("<b>📈 Recent Form</b>\n<blockquote expandable>"
                         + "\n".join(html_rows) + "</blockquote>")

        from services.tournament_service import DETAILED_STAT_FIELDS
        if any(row.get(f) is None for f in DETAILED_STAT_FIELDS):
            legacy = True

    note = (f"Career totals span this World T20 Championship season only. "
            f"Leaderboards: /{championship.command}stats")
    blocks.append(R.footer(note))
    parts.append(f"\n<i>{html.escape(note)}</i>")
    if legacy:
        tail = "— = not recorded in older scorecards."
        blocks.append(R.footer(tail))
        parts.append(f"<i>{tail}</i>")
    return blocks, "\n\n".join(parts)
