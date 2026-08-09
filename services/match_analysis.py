"""The post-match analysis report — one self-contained HTML file per match.

What the chat already gets when a CIPL / LETSPLAY match ends is a result
message, a summary image, and a plain-text scorecard filed away in the storage
channel that the players never see. All three answer "who won". None of them
answers "why", which is the question the Approach game actually poses: two
captains spent forty overs guessing each other, and the record of that duel was
thrown away at the final ball.

This module is that record. It reads the finished match state — which by then
holds the over-by-over run log, the fall of wickets, every partnership, both
momentum tracks, the live chase probability, and both captains' picks for every
over — and renders one HTML file with the charts to make sense of them.

Design constraints, both load-bearing:

* **Self-contained.** Telegram opens a downloaded file from local storage, so a
  CDN stylesheet or a charting library renders a blank page. Everything is inline
  — the CSS, and the charts as hand-built SVG.
* **Pure.** No database, no Telegram, no clock. It takes the state dict and gives
  back a string, which is what makes it testable and what lets the caller render
  it off the event loop.

Public API:
    build_match_analysis_html(state, result, scorecard=None, potm=None) -> str
    analysis_filename(match_id) -> str
"""

import html
import logging

from engine import approach_modifiers, momentum as momentum_engine, pitch_state
from engine import ground_config
from engine.approach_modifiers import REPEAT_FROM as _REPEAT_FROM

logger = logging.getLogger(__name__)

# Phase windows, as fractions of the innings, so the report agrees with
# services.cipl_match.approach_phase for every format it can be handed. The
# death fraction is 0.20 because that is what the engine uses: its short-format
# branch is round(total * 0.20), and its 20-over branch is DEATH_UNITS = 4, which
# is the same number. A report that filed over 16 under Death while the engine
# called it Middle would disagree with the `phase` it prints from the approach
# log two sections further down.
_PP_FRACTION = 0.30
_DEATH_FRACTION = 0.20

# The Hundred's powerplay is five sets, not six — the one place the fraction
# above does not reproduce the engine, which reads it from the format spec.
_HUNDRED_POWERPLAY = 5

# Momentum points → win-probability points, for ranking turning points together.
_MOMENTUM_WEIGHT = 10.0


def analysis_filename(match_id):
    """The report's filename, shared by the chat send and the channel archive."""
    return f"MatchAnalysis{match_id}.html"


# ── small helpers ────────────────────────────────────────────────────

def _e(value):
    """Escape for HTML text. Everything user-supplied goes through this —
    team names and player names are typed by players."""
    return html.escape(str(value if value is not None else ""), quote=True)


def _num(value, default=0):
    """Read an int out of a match state that may hold ``None`` or a string."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _phase_of(over, overs_total, spec_pp=None):
    """``"Powerplay"`` / ``"Middle"`` / ``"Death"`` for a 1-based over."""
    total = max(1, _num(overs_total, 20))
    pp = spec_pp if spec_pp else max(1, round(total * _PP_FRACTION))
    death = max(1, round(total * _DEATH_FRACTION))
    if over <= pp:
        return "Powerplay"
    if over > total - death:
        return "Death"
    return "Middle"


def _wickets_by_over(fow, bpu=6):
    """``{over_number: wickets_that_fell}`` from a fall-of-wickets list.

    Each entry is ``[runs, wickets, batsman, overs]`` where ``overs`` reads
    "12.3" in a T20 and "75 balls" in The Hundred; both are handled, because a
    report that silently drops every wicket in a Hundred match would look
    finished and be wrong.
    """
    out = {}
    for entry in fow or []:
        try:
            marker = str(entry[3])
        except (IndexError, TypeError):
            continue
        if marker.endswith("balls"):
            balls = _num(marker.split()[0], -1)
            over = (balls // max(1, bpu) + (1 if balls % max(1, bpu) else 0)
                    if balls >= 0 else 0)
        else:
            whole, _, part = marker.partition(".")
            over = _num(whole, -1) + (1 if _num(part) else 0)
        # A wicket cannot fall before a ball is bowled, so anything that does not
        # parse to a real over is junk. Clamping it to over 1 instead would draw
        # a wicket marker on the chart that never happened, which is worse than
        # drawing none.
        if over < 1:
            continue
        out[over] = out.get(over, 0) + 1
    return out


# ── SVG charts ───────────────────────────────────────────────────────
#
# Hand-built rather than pulled from a library, for the reason in the module
# docstring. They share one viewBox convention: 0 0 W H with the plot inset by
# PAD, and every one scales with the container, so the file reads on a phone.

_W, _H, _PAD_L, _PAD_R, _PAD_T, _PAD_B = 720, 300, 56, 16, 20, 40


def _axes(y_labels, x_max, x_ticks):
    """Gridlines, y labels and x labels shared by all the charts."""
    parts = []
    plot_h = _H - _PAD_T - _PAD_B
    for frac, label in y_labels:
        y = _PAD_T + plot_h * (1.0 - frac)
        parts.append(f'<line class="grid" x1="{_PAD_L}" y1="{y:.1f}" '
                     f'x2="{_W - _PAD_R}" y2="{y:.1f}"/>')
        parts.append(f'<text class="ylab" x="{_PAD_L - 6}" y="{y + 4:.1f}">'
                     f'{_e(label)}</text>')
    plot_w = _W - _PAD_L - _PAD_R
    for tick in x_ticks:
        if x_max <= 0:
            continue
        x = _PAD_L + plot_w * (tick / x_max)
        parts.append(f'<text class="xlab" x="{x:.1f}" y="{_H - 8}">{tick}</text>')
    return "".join(parts)


def _x_ticks(n_overs):
    """Which over numbers to label, thinned so they do not collide on a phone."""
    if n_overs <= 0:
        return []
    step = 1 if n_overs <= 10 else (2 if n_overs <= 24 else 5)
    return list(range(step, n_overs + 1, step))


def _manhattan_svg(series):
    """Per-over runs as grouped bars, with wickets marked.

    ``series`` is a list of ``(label, over_runs, wickets_by_over, css_class)``.
    """
    overs = max([len(s[1]) for s in series] + [1])
    peak = max([max(s[1] or [0]) for s in series] + [1])
    plot_w, plot_h = _W - _PAD_L - _PAD_R, _H - _PAD_T - _PAD_B
    slot = plot_w / overs
    n = max(1, len(series))
    bar_w = max(2.0, slot / n - 2)

    bars = []
    for si, (_label, runs, wkts, cls) in enumerate(series):
        for idx, value in enumerate(runs or []):
            h = plot_h * (value / peak) if peak else 0
            x = _PAD_L + idx * slot + si * (slot / n) + 1
            y = _PAD_T + plot_h - h
            bars.append(f'<rect class="bar {cls}" x="{x:.1f}" y="{y:.1f}" '
                        f'width="{bar_w:.1f}" height="{max(0.0, h):.1f}" rx="1.5">'
                        f'<title>Over {idx + 1}: {value} runs</title></rect>')
            for w in range(wkts.get(idx + 1, 0)):
                bars.append(f'<circle class="wkt" cx="{x + bar_w / 2:.1f}" '
                            f'cy="{y - 6 - w * 9:.1f}" r="3.4"/>')
    y_labels = [(f / peak, str(int(f))) for f in
                (0, peak / 2, peak) if peak] or [(0, "0")]
    names = " and ".join(s[0] for s in series if s[0])
    return _svg(_axes(y_labels, overs, _x_ticks(overs)) + "".join(bars),
                label=f"Runs scored in each over by {names}" if names
                else "Runs scored in each over")


def _worm_svg(series, overs_total):
    """Cumulative score, both innings on one pair of axes."""
    peak = max([sum(s[1]) for s in series] + [1])
    plot_w, plot_h = _W - _PAD_L - _PAD_R, _H - _PAD_T - _PAD_B
    overs = max(overs_total, max([len(s[1]) for s in series] + [1]))
    out = []
    for _label, runs, wkts, cls in series:
        pts, total = ["%.1f,%.1f" % (_PAD_L, _PAD_T + plot_h)], 0
        marks = []
        for idx, value in enumerate(runs or []):
            total += value
            x = _PAD_L + plot_w * ((idx + 1) / overs)
            y = _PAD_T + plot_h * (1.0 - total / peak)
            pts.append(f"{x:.1f},{y:.1f}")
            if wkts.get(idx + 1):
                marks.append(f'<circle class="wkt" cx="{x:.1f}" cy="{y:.1f}" r="4">'
                             f'<title>Wicket, over {idx + 1} ({total})</title></circle>')
        out.append(f'<polyline class="worm {cls}" points="{" ".join(pts)}"/>'
                   + "".join(marks))
    y_labels = [(f / peak, str(int(f))) for f in (0, peak / 2, peak)]
    names = " and ".join(s[0] for s in series if s[0])
    return _svg(_axes(y_labels, overs, _x_ticks(overs)) + "".join(out),
                label=f"Cumulative score over by over for {names}" if names
                else "Cumulative score over by over")


def _band_svg(series, lo, hi, overs_total, zero_line=True, label=""):
    """Signed series (momentum, pressure) drawn about a zero line."""
    plot_w, plot_h = _W - _PAD_L - _PAD_R, _H - _PAD_T - _PAD_B
    overs = max(overs_total, max([len(s[1]) for s in series] + [1]))
    span = float(hi - lo) or 1.0

    def _y(v):
        """A signed value's pixel row, clamped into the band."""
        return _PAD_T + plot_h * (1.0 - (max(lo, min(hi, v)) - lo) / span)

    out = []
    if zero_line:
        out.append(f'<line class="zero" x1="{_PAD_L}" y1="{_y(0):.1f}" '
                   f'x2="{_W - _PAD_R}" y2="{_y(0):.1f}"/>')
    for _label, values, cls in series:
        pts = [f"{_PAD_L + plot_w * ((i + 1) / overs):.1f},{_y(v):.1f}"
               for i, v in enumerate(values or [])]
        if pts:
            out.append(f'<polyline class="worm {cls}" points="{" ".join(pts)}"/>')
    y_labels = [((v - lo) / span, f"{v:g}") for v in (hi, (hi + lo) / 2.0, lo)]
    return _svg(_axes(y_labels, overs, _x_ticks(overs)) + "".join(out),
                label=label)


def _svg(body, label=""):
    """Wrap chart body in an SVG.

    ``label`` becomes the first-child ``<title>``, which is what names a
    ``role="img"`` element to a screen reader. The phase and duel tables carry
    their numbers as text, but the worm and the momentum band have no text
    equivalent anywhere in the file — without a title they announce as an
    unnamed image and the section is simply missing for that reader.
    """
    title = f"<title>{_e(label)}</title>" if label else ""
    return (f'<svg class="chart" viewBox="0 0 {_W} {_H}" role="img">'
            f'{title}{body}</svg>')


def _legend(items):
    """Series swatches. Present for every chart with more than one series, so
    identity is never carried by colour alone."""
    return ('<div class="legend">' + "".join(
        f'<span class="key"><i class="sw {cls}"></i>{_e(label)}</span>'
        for label, cls in items) + "</div>")


# ── report sections ──────────────────────────────────────────────────

def _live_overs(state):
    """How far the innings in progress has got, as the live match writes it.

    ``end_first_innings`` archives the first innings as ``inn1_overs``; there is
    no matching ``inn2_overs`` on the state, because the second innings is the
    live one and its figure is produced on demand by ``cipl_match.format_overs``.
    Reuse that rather than re-deriving it — it is the function that knows The
    Hundred counts balls, and a report that formatted overs its own way would
    drift from the scorecard beside it.

    Imported locally: this module is otherwise engine-only, and a match state
    that did not come from CIPL still renders (with a blank figure) rather than
    failing to import.
    """
    try:
        from services.cipl_match import format_overs
        return format_overs(state)
    except Exception:
        logger.exception("match analysis: could not format the live overs")
        return ""


def _innings_views(state):
    """``[(number, team, runs, wickets, overs, over_runs, fow, ...)]`` for the
    innings that were actually played, oldest first.

    Reads the archived ``inn1_*`` keys for the first innings and the live keys
    for the second — the same split ``match_webapp_service.build_scorecard``
    uses, so the two never disagree about who batted when.
    """
    bpu = 5 if str(state.get("ball_format")) == "The100" else 6
    views = []
    if state.get("innings") == 2 or state.get("inn1_over_runs"):
        views.append({
            "number": 1,
            "team": state.get("inn1_bat_team") or state.get("inn1_team") or "",
            "runs": _num(state.get("inn1_runs")),
            "wickets": _num(state.get("inn1_wickets")),
            "overs": state.get("inn1_overs", ""),
            "over_runs": list(state.get("inn1_over_runs") or []),
            "fow": list(state.get("inn1_fow") or []),
            "partnerships": list(state.get("inn1_partnership_history") or []),
            "momentum": list(state.get("inn1_momentum_history") or []),
            "pressure": [],
            "approaches": list(state.get("inn1_approach_log") or []),
        })
    views.append({
        "number": 2 if views else 1,
        "team": state.get("bat_team_name", ""),
        "runs": _num(state.get("total_runs")),
        "wickets": _num(state.get("total_wickets")),
        "overs": _live_overs(state),
        "over_runs": list(state.get("over_runs") or []),
        "fow": list(state.get("fow") or []),
        "partnerships": list(state.get("partnership_history") or []),
        "momentum": list(state.get("momentum_history") or []),
        "pressure": list(state.get("pressure_history") or []),
        "approaches": list(state.get("approach_log") or []),
    })
    for v in views:
        v["wkt_by_over"] = _wickets_by_over(v["fow"], bpu)
        v["balls_per_unit"] = bpu
        # The Hundred is scored in sets of five, not overs, and the report says so
        # everywhere the live match does.
        v["unit"] = "Over" if bpu == 6 else "Set"
        v["powerplay"] = None if bpu == 6 else _HUNDRED_POWERPLAY
    return views


def _phase_table(views, overs_total, pitch):
    """Runs, wickets and rate in each phase, against the pitch's par RPO."""
    par = ground_config.get_scoring_dynamics(pitch) or {}
    par_total = par.get("par_high") or par.get("par_low")
    rows = []
    for v in views:
        buckets = {"Powerplay": [0, 0, 0], "Middle": [0, 0, 0], "Death": [0, 0, 0]}
        for idx, runs in enumerate(v["over_runs"]):
            phase = _phase_of(idx + 1, overs_total, v.get("powerplay"))
            buckets[phase][0] += runs
            buckets[phase][1] += v["wkt_by_over"].get(idx + 1, 0)
            buckets[phase][2] += 1
        rows.append((v, buckets))

    cells = []
    for v, buckets in rows:
        for phase in ("Powerplay", "Middle", "Death"):
            runs, wkts, overs = buckets[phase]
            if not overs:
                continue   # a chase that finished early never reached this phase
            cells.append(
                f"<tr><td>{_e(v['team'])}</td><td>{_e(phase)}</td>"
                f"<td class='n'>{runs}</td><td class='n'>{wkts}</td>"
                f"<td class='n'>{overs}</td>"
                f"<td class='n'>{(runs / overs if overs else 0):.2f}</td></tr>")
    note = (f"<p class='note'>Par on a {_e(pitch)} surface is around "
            f"{_e(par_total)}.</p>" if par_total else "")
    return (
        "<table><thead><tr><th>Innings</th><th>Phase</th><th>Runs</th>"
        "<th>Wkts</th><th>Overs</th><th>RPO</th></tr></thead><tbody>"
        + "".join(cells) + "</tbody></table>" + note)


def _scorecard_section(scorecard):
    """Both innings' batting and bowling, from the Mini App's own builder."""
    if not scorecard or not scorecard.get("ok"):
        return "<p class='note'>Scorecard unavailable for this match.</p>"
    out = []
    for inn in scorecard.get("innings") or []:
        out.append(
            f"<h3>{_e(inn.get('bat_team'))} — {_num(inn.get('runs'))}/"
            f"{_num(inn.get('wickets'))} ({_e(inn.get('overs'))})</h3>")
        rows = "".join(
            f"<tr><td>{_e(b.get('name'))}</td>"
            f"<td class='dim'>{_e(b.get('how_out'))}</td>"
            f"<td class='n'>{_num(b.get('runs'))}</td>"
            f"<td class='n'>{_num(b.get('balls'))}</td>"
            f"<td class='n'>{_num(b.get('fours'))}</td>"
            f"<td class='n'>{_num(b.get('sixes'))}</td>"
            f"<td class='n'>{float(b.get('sr') or 0):.1f}</td></tr>"
            for b in inn.get("batting") or [])
        out.append("<div class='scroll'><table><thead><tr><th>Batter</th>"
                   "<th>How out</th><th>R</th><th>B</th><th>4s</th><th>6s</th>"
                   "<th>SR</th></tr></thead><tbody>" + rows
                   + "</tbody></table></div>")
        rows = "".join(
            f"<tr><td>{_e(b.get('name'))}</td>"
            f"<td class='n'>{_e(b.get('overs'))}</td>"
            f"<td class='n'>{_num(b.get('maidens'))}</td>"
            f"<td class='n'>{_num(b.get('runs'))}</td>"
            f"<td class='n'>{_num(b.get('wickets'))}</td>"
            f"<td class='n'>{float(b.get('econ') or 0):.2f}</td></tr>"
            for b in inn.get("bowling") or [])
        out.append("<div class='scroll'><table><thead><tr><th>Bowler</th>"
                   "<th>O</th><th>M</th><th>R</th><th>W</th><th>Econ</th>"
                   "</tr></thead><tbody>" + rows + "</tbody></table></div>")
    return "".join(out)


def _partnership_section(views):
    """Every stand of the match, and the fall of wickets that ended each."""
    out = []
    for v in views:
        rows = "".join(
            f"<tr><td class='n'>{_num(p.get('wicket'))}</td>"
            f"<td>{_e(p.get('batsman1'))} &amp; {_e(p.get('batsman2'))}</td>"
            f"<td class='n'>{_num(p.get('runs'))}</td>"
            f"<td class='n'>{_num(p.get('balls'))}</td>"
            f"<td class='dim'>{'unbroken' if p.get('notout') else ''}</td></tr>"
            for p in v["partnerships"])
        fow = " &nbsp;·&nbsp; ".join(
            f"{_num(f[1])}-{_num(f[0])} ({_e(f[2])}, {_e(f[3])})"
            for f in v["fow"] if len(f) >= 4)
        out.append(f"<h3>{_e(v['team'])}</h3>")
        if rows:
            out.append("<div class='scroll'><table><thead><tr><th>Wkt</th>"
                       "<th>Partnership</th><th>Runs</th><th>Balls</th><th></th>"
                       "</tr></thead><tbody>" + rows + "</tbody></table></div>")
        out.append(f"<p class='note'><b>Fall of wickets:</b> {fow or '—'}</p>")
    return "".join(out)


# How an over's ball marks are drawn. Keys are the marks
# ``services.cipl_match.simulate_over`` writes to its over timeline.
_BALL_CLASSES = {"0": "b-dot", "4": "b-four", "6": "b-six", "W": "b-wkt",
                 "WD": "b-extra", "NB": "b-extra", "LB": "b-extra"}
_BALL_TEXT = {"0": "•"}

# Match-up grid tint bands, in runs per over. Absolute rather than relative to
# the innings, so a cell means the same thing in both innings and in every match
# — a relative scale would paint the best of a bad set of overs as a good one.
_RPO_BANDS = (6.0, 8.0, 10.0, 12.0)

# An over belongs to whoever won it. A wicket or a strangling is the bowling
# side's; twelve-plus is the batting side's, wicket or no wicket.
_BAT_WON_RUNS = 12
_BOWL_WON_RUNS = 4


def _ball_pills(timeline):
    """An over's ball marks as inline pills, or ``""`` when it wasn't recorded.

    ``timeline`` is absent from overs logged before this column existed — a match
    already in flight when it deployed — so an empty cell is a normal outcome,
    not a bug.
    """
    if not timeline:
        return ""
    return "".join(
        f'<i class="ball {_BALL_CLASSES.get(mark, "b-run")}">'
        f'{_e(_BALL_TEXT.get(mark, mark))}</i>'
        for mark in timeline)


def _over_row_class(runs, wickets):
    """Which side won the over, as a row tint. Decoration only — the runs and
    wickets columns say the same thing in text."""
    if runs >= _BAT_WON_RUNS:
        return "tr-bat"
    if wickets or runs <= _BOWL_WON_RUNS:
        return "tr-bowl"
    return ""


def _over_by_over_table(view):
    """The duel, over by over: which intent met which plan, and what it cost.

    The row tint is decoration — every over's outcome is already in the runs and
    wickets columns in text, so the table reads the same with colour switched off.
    """
    log = view["approaches"]
    if not log:
        return ""
    has_balls = any(o.get("timeline") for o in log)
    rows = []
    for over in log:
        runs, wkts = _num(over.get("runs")), _num(over.get("wickets"))
        balls = (f"<td class='balls'>{_ball_pills(over.get('timeline'))}</td>"
                 if has_balls else "")
        rows.append(
            f"<tr class='{_over_row_class(runs, wkts)}'>"
            f"<td class='n'>{_num(over.get('over'))}</td>"
            f"<td>{_e(approach_modifiers.batting_label(over.get('bat')))}</td>"
            f"<td>{_e(approach_modifiers.bowling_label(over.get('bowl')))}</td>"
            f"<td class='n'>{runs}</td><td class='n'>{wkts}</td>"
            f"{balls}</tr>")
    # Runs and wickets sit BEFORE the ball sequence on purpose: at phone width
    # six columns do not fit, and the two the table exists to show must be the
    # ones on screen. The sequence is what you scroll for.
    head = (f"<tr><th>{_e(view['unit'])}</th><th>Batting</th><th>Bowling</th>"
            "<th>R</th><th>W</th>"
            + ("<th>Balls</th>" if has_balls else "") + "</tr>")
    return ("<div class='scroll'><table class='duel'><thead>" + head
            + "</thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def _rpo_band(rpo):
    """Which tint step an RPO falls in (0 = quietest, 4 = most expensive)."""
    for idx, edge in enumerate(_RPO_BANDS):
        if rpo < edge:
            return idx
    return len(_RPO_BANDS)


def _matchup_grid(view):
    """Approach vs approach: every intent against every plan it actually faced.

    The over-by-over table says what happened; this says what *worked*. Runs per
    over is the headline because it is the only figure comparable across cells
    that saw different numbers of overs, and it is what the tint encodes — the
    raw runs, wickets and over count sit under it, so the cell is readable with
    no colour at all.
    """
    log = view["approaches"]
    if not log:
        return ""
    cells = {}
    for over in log:
        key = (over.get("bat"), over.get("bowl"))
        agg = cells.setdefault(key, {"runs": 0, "wickets": 0, "overs": 0})
        agg["runs"] += _num(over.get("runs"))
        agg["wickets"] += _num(over.get("wickets"))
        agg["overs"] += 1

    # Abbreviated, with the full name on hover: five spelled-out plans put the
    # last two columns off a phone screen before you have read the first three.
    head = "".join(f'<th title="{_e(name)}">{_e(name[:3].upper())}</th>'
                   for _k, _emoji, name in approach_modifiers.BOWLING_APPROACHES)
    rows = []
    for bat_key, _bat_emoji, bat_name in approach_modifiers.BATTING_APPROACHES:
        tds = []
        for bowl_key, _e2, _n2 in approach_modifiers.BOWLING_APPROACHES:
            agg = cells.get((bat_key, bowl_key))
            if not agg:
                tds.append("<td class='cell empty'>–</td>")
                continue
            rpo = agg["runs"] / agg["overs"]
            tds.append(
                f"<td class='cell q{_rpo_band(rpo)}'>"
                f"<b>{rpo:.1f}</b>"
                f"<span>{agg['runs']}r {agg['wickets']}w · {agg['overs']}</span>"
                "</td>")
        rows.append(f"<tr><th class='rowhead'>{_e(bat_name)}</th>"
                    + "".join(tds) + "</tr>")
    legend = " &nbsp;·&nbsp; ".join(
        f"{_e(name[:3].upper())} {_e(name)}"
        for _k, _emoji, name in approach_modifiers.BOWLING_APPROACHES)

    scale = "".join(f"<i class='sq q{i}'></i>" for i in range(len(_RPO_BANDS) + 1))
    return (
        "<div class='scroll'><table class='grid'><thead><tr>"
        "<th class='rowhead'>Batting \\ Bowling</th>" + head
        + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        f"<p class='note'>Columns: {legend}.<br>Runs per over, with runs, "
        f"wickets and overs bowled beneath. "
        f"Shading runs quiet {scale} expensive.</p>")


def _longest_repeats(log):
    """The longest unbroken run of one pick, per side, as ``{side: (pick, n)}``.

    The over-by-over table shows the sequence and the grid shows the returns;
    this names the thing a captain most wants to be told afterwards — how long
    they were readable for.
    """
    out = {}
    for field in ("bat", "bowl"):
        best_key, best, run, prev = None, 0, 0, object()
        for over in log:
            pick = over.get(field)
            run = run + 1 if pick == prev else 1
            prev = pick
            if run > best:
                best_key, best = pick, run
        out[field] = (best_key, best)
    return out


def _approach_section(views):
    """The duel: over by over, then match-up by match-up, then in aggregate.

    Nothing here is hidden in a bot match, and that is deliberate rather than an
    oversight. ``handlers/cipl_play._render_over_summary`` withholds the bot's
    plan *during* the match, so its mix cannot be read off the screen and
    countered over the next few overs. This file is the record of a match that is
    already over; the line being drawn is mid-match against post-match, and only
    the first half of it needs protecting.
    """
    out = []
    for v in views:
        log = v["approaches"]
        if not log:
            continue
        out.append(f"<h3>{_e(v['team'])} batting</h3>")
        out.append(_over_by_over_table(v))
        out.append("<h4>Approach vs approach</h4>")
        out.append(_matchup_grid(v))

        bat_rows, bowl_rows = [], []
        for keys, label_fn, field, rows in (
                (approach_modifiers.BATTING_APPROACHES,
                 approach_modifiers.batting_label, "bat", bat_rows),
                (approach_modifiers.BOWLING_APPROACHES,
                 approach_modifiers.bowling_label, "bowl", bowl_rows)):
            for key, _emoji, _name in keys:
                picked = [o for o in log if o.get(field) == key]
                if not picked:
                    continue
                runs = sum(_num(o.get("runs")) for o in picked)
                wkts = sum(_num(o.get("wickets")) for o in picked)
                rows.append(
                    f"<tr><td>{_e(label_fn(key))}</td>"
                    f"<td class='n'>{len(picked)}</td>"
                    f"<td class='n'>{runs}</td>"
                    f"<td class='n'>{wkts}</td>"
                    f"<td class='n'>{runs / len(picked):.1f}</td></tr>")
        combos = {}
        for o in log:
            if o.get("combo"):
                combos[o["combo"]] = combos.get(o["combo"], 0) + 1
        streaks = _longest_repeats(log)
        out.append("<div class='two'>")
        for title, rows in (("Batting intent", bat_rows),
                            ("Bowling plan faced", bowl_rows)):
            out.append(f"<div><h4>{title}</h4><div class='scroll'><table><thead>"
                       "<tr><th>Approach</th><th>Overs</th><th>Runs</th>"
                       "<th>Wkts</th><th>RPO</th></tr></thead><tbody>"
                       + "".join(rows) + "</tbody></table></div></div>")
        out.append("</div>")
        for field, label, fn in (
                ("bat", "Longest run of one intent",
                 approach_modifiers.batting_label),
                ("bowl", "Longest run of one plan",
                 approach_modifiers.bowling_label)):
            key, run = streaks.get(field) or (None, 0)
            if run:
                read = (" — read by the opposition" if run >= _REPEAT_FROM else "")
                out.append(f"<p class='note'><b>{label}:</b> {_e(fn(key))} "
                           f"&times;{run}{read}</p>")
        if combos:
            out.append("<p class='note'><b>Special combinations:</b> "
                       + " &nbsp;·&nbsp; ".join(
                           f"{_e(name)} ×{count}" for name, count in
                           sorted(combos.items(), key=lambda kv: -kv[1]))
                       + "</p>")
    return "".join(out) or "<p class='note'>No approach data recorded.</p>"


def _turning_points(views, chase_history):
    """The three overs that moved the match most.

    Two different measures have to be ranked against each other: win probability,
    which spans 0-100, and momentum, which spans -2 to +2. Momentum is scaled by
    ``_MOMENTUM_WEIGHT`` to put them on one axis — a total momentum collapse, the
    largest swing the accumulator can produce, is worth about as much as a 40-point
    move in the chase. Deliberately less than a full swing in win probability,
    because momentum describes how an over *felt* and win probability describes
    what it *cost*.

    Momentum is read from the **first innings only**. In the second there is a
    target, so win probability measures the same swings in the unit that matters,
    and counting both would let one over enter the ranking twice. The cost of
    that choice: a match with no ``chase_history`` — one abandoned before the
    chase, or replayed from a state written before the chase was tracked — gets
    turning points from the first innings alone.
    """
    events = []
    prev = None
    for entry in chase_history or []:
        chance = _num(entry.get("chasing"))
        if prev is not None:
            events.append((abs(chance - prev), entry.get("over"), 2,
                           f"win probability {prev}% → {chance}%"))
        prev = chance
    for v in views:
        if v["number"] != 1:
            continue
        series = v["momentum"]
        for idx in range(1, len(series)):
            swing = series[idx] - series[idx - 1]
            events.append((abs(swing) * _MOMENTUM_WEIGHT, idx + 1, 1,
                           f"momentum {series[idx - 1]:+.1f} → {series[idx]:+.1f}"))
    if not events:
        return ""
    events.sort(key=lambda e: -e[0])
    items = "".join(
        f"<li><b>Innings {inn}, over {_e(over)}</b> — {_e(text)}</li>"
        for _weight, over, inn, text in events[:3])
    return f"<ol class='turns'>{items}</ol>"


def _pitch_section(state, pitch):
    """How the surface changed for the chase — the pitch's own note, plus the
    trace of which innings-2 rules were live and from which over."""
    note = pitch_state.evolution_note(pitch)
    trace = state.get("dps_trace") or []
    seen, ordered = set(), []
    for entry in trace:
        for label in entry.get("effects") or []:
            if label not in seen:
                seen.add(label)
                ordered.append((entry.get("over"), label))
    body = [f"<p>{_e(note)}</p>"] if note else []
    if ordered:
        body.append("<ul class='trace'>" + "".join(
            f"<li><span class='dim'>from over {_e(over)}</span> {_e(label)}</li>"
            for over, label in ordered) + "</ul>")
    elif not note:
        return ""
    return "".join(body)


# ── the page ─────────────────────────────────────────────────────────

def build_match_analysis_html(state, result=None, scorecard=None, potm=None):
    """Render the finished match as one self-contained HTML document.

    ``state`` is the live CIPL / LETSPLAY state, read before cleanup. ``result``
    is :func:`services.cipl_match.compute_result`'s dict, ``scorecard`` is
    :func:`services.match_webapp_service.build_scorecard`'s, and ``potm`` is the
    ``(name, stats, team)`` triple the summary card already computes. All three
    are optional: a section whose data is missing is dropped rather than faked,
    so a partial state still produces a usable file.
    """
    state = state or {}
    pitch = state.get("pitch_type") or "Hard"
    overs_total = _num(state.get("overs"), 20)
    match_id = state.get("match_id", "")
    views = _innings_views(state)

    if result and result.get("tie"):
        headline = "Match tied"
    elif result and result.get("winner"):
        # ``margin_text`` is the finished phrase, used where a run/wicket margin
        # would be nonsense — a Super Over decided on countback has a margin of
        # 0, and "beat X by 0 sixes" is what the summary card already learned not
        # to print. Nothing is escaped here; the whole headline is escaped once,
        # below, where it is rendered.
        margin_text = result.get("margin_text")
        headline = (
            f"{result['winner']} beat {result['loser']} — {margin_text}"
            if margin_text else
            f"{result['winner']} beat {result['loser']} by "
            f"{_num(result.get('margin'))} {result.get('margin_type')}")
    else:
        headline = "Result unavailable"

    meta = [("Match", f"#{match_id}"), ("Ground", state.get("stadium")),
            ("Pitch", pitch), ("Format", state.get("ball_format") or "T20"),
            ("Overs", overs_total)]
    if potm and potm[0]:
        meta.append(("Player of the Match",
                     f"{potm[0]} — {potm[1]}" if potm[1] else potm[0]))
    meta_html = "".join(
        f"<div class='m'><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>"
        for k, v in meta if v not in (None, ""))

    scores = "".join(
        f"<div class='score'><span class='team'>{_e(v['team'])}</span>"
        f"<span class='runs'>{v['runs']}/{v['wickets']}</span>"
        f"<span class='ov'>{_e(v['overs'])}</span></div>" for v in views)

    series_bars = [(v["team"], v["over_runs"], v["wkt_by_over"],
                    f"s{v['number']}") for v in views]
    chase_history = state.get("chase_history") or []

    legend = _legend([(v["team"], f"s{v['number']}") for v in views])
    sections = [
        ("Phase by phase", _phase_table(views, overs_total, pitch)),
        ("Runs per over", _manhattan_svg(series_bars) + legend),
        ("The worm", _worm_svg(series_bars, overs_total) + legend),
        ("Scorecards", _scorecard_section(scorecard)),
    ]

    mom_series = [(v["team"], v["momentum"], f"s{v['number']}") for v in views
                  if v["momentum"]]
    if mom_series:
        pressure = [(f"{v['team']} pressure", v["pressure"], "pressure")
                    for v in views if v["pressure"]]
        sections.append((
            "Momentum &amp; pressure",
            _band_svg(mom_series + pressure, -momentum_engine.MOMENTUM_CAP,
                      momentum_engine.MOMENTUM_CAP, overs_total,
                      label="Batting momentum and chasing pressure, over by over")
            + _legend([(lbl, cls) for lbl, _v, cls in mom_series + pressure])
            + "<p class='note'>Momentum runs from &minus;2 (collapsed) to "
              "+2 (unstoppable). Pressure is the chasing side's; it counts "
              "against them.</p>"))

    if chase_history:
        sections.append((
            "Win probability",
            _band_svg([("Chasing side",
                        [_num(c.get("chasing")) for c in chase_history], "s2")],
                      0, 100, overs_total, zero_line=False,
                      label="The chasing side's win probability, over by over")
            + "<p class='note'>The chasing side's live chance, over by over.</p>"))

    turns = _turning_points(views, chase_history)
    if turns:
        sections.append(("Turning points", turns))

    sections.append(("Partnerships &amp; wickets", _partnership_section(views)))
    sections.append(("The approach duel", _approach_section(views)))
    pitch_html = _pitch_section(state, pitch)
    if pitch_html:
        sections.append(("How the pitch changed", pitch_html))

    body = "".join(f"<section><h2>{title}</h2>{content}</section>"
                   for title, content in sections if content)

    title = " v ".join(_e(v["team"]) for v in views) or "Match analysis"
    return _PAGE.format(title=title, headline=_e(headline), meta=meta_html,
                        scores=scores, body=body, css=_CSS)


_CSS = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#14181f; --dim:#5c6673; --line:#dfe3e9;
  --accent:#1f6feb; --s1:#1f6feb; --s2:#e8590c; --pressure:#8b5cf6; --wkt:#d92d20;
  /* Sequential ramp for the match-up grid: one hue, light to dark. Every step
     clears 4.5:1 against --ink, so the number in the cell stays readable and the
     shading is decoration rather than the only way to read the grid. */
  --q0:#eef4fd; --q1:#cde2fb; --q2:#9ec5f4; --q3:#6da7ec; --q4:#3987e5;
  --tint-bat:rgba(31,111,235,.07); --tint-bowl:rgba(217,45,32,.07);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0e1116; --panel:#161b22; --ink:#e6edf3; --dim:#8b949e; --line:#2a313a;
    --accent:#58a6ff; --s1:#58a6ff; --s2:#ff922b; --pressure:#a78bfa; --wkt:#ff6b6b;
    /* The ramp's anchor flips in dark: quietest sits nearest the surface and the
       expensive end brightens. Same hue, same monotone lightness, re-stepped. */
    --q0:#16202e; --q1:#104281; --q2:#184f95; --q3:#1c5cab; --q4:#256abf;
    --tint-bat:rgba(88,166,255,.10); --tint-bowl:rgba(255,107,107,.10);
  }
}
:root[data-theme="dark"]{
  --bg:#0e1116; --panel:#161b22; --ink:#e6edf3; --dim:#8b949e; --line:#2a313a;
  --accent:#58a6ff; --s1:#58a6ff; --s2:#ff922b; --pressure:#a78bfa; --wkt:#ff6b6b;
  --q0:#16202e; --q1:#104281; --q2:#184f95; --q3:#1c5cab; --q4:#256abf;
  --tint-bat:rgba(88,166,255,.10); --tint-bowl:rgba(255,107,107,.10);
}
*{box-sizing:border-box}
body{margin:0;padding:0 16px 56px;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  overflow-x:hidden}
.wrap{max-width:860px;margin:0 auto}
header{padding:28px 0 12px}
h1{margin:0 0 4px;font-size:1.45rem;letter-spacing:-.01em}
.headline{margin:0 0 18px;color:var(--accent);font-weight:600}
.scores{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}
.score{flex:1 1 200px;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:12px 14px}
.score .team{display:block;color:var(--dim);font-size:.82rem;
  text-transform:uppercase;letter-spacing:.06em}
.score .runs{display:block;font-size:1.5rem;font-weight:700}
.score .ov{color:var(--dim);font-size:.85rem}
dl.meta{display:flex;flex-wrap:wrap;gap:8px 22px;margin:0 0 8px;padding:0}
.m dt{color:var(--dim);font-size:.76rem;text-transform:uppercase;
  letter-spacing:.05em}
.m dd{margin:0;font-weight:600}
section{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:16px 16px 18px;margin:16px 0}
h2{margin:0 0 12px;font-size:1.05rem;letter-spacing:-.01em}
h3{margin:16px 0 8px;font-size:.95rem}
h4{margin:10px 0 6px;font-size:.85rem;color:var(--dim);font-weight:600}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:.88rem;min-width:340px}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);
  white-space:nowrap}
th{color:var(--dim);font-weight:600;font-size:.76rem;text-transform:uppercase;
  letter-spacing:.04em}
td.n{text-align:right;font-variant-numeric:tabular-nums}
td.dim,.dim{color:var(--dim)}
.note{color:var(--dim);font-size:.84rem;margin:10px 0 0}
.two{display:flex;flex-wrap:wrap;gap:18px}
.two>div{flex:1 1 260px;min-width:0}
svg.chart{width:100%;height:auto;aspect-ratio:720/300;display:block;
  margin:4px 0 2px}
.grid{stroke:var(--line);stroke-width:1}
.zero{stroke:var(--dim);stroke-width:1;stroke-dasharray:3 3}
.ylab,.xlab{fill:var(--dim);font-size:17px}
.ylab{text-anchor:end}
.xlab{text-anchor:middle}
.bar.s1,.sw.s1{fill:var(--s1)}
.bar.s2,.sw.s2{fill:var(--s2)}
.sw.pressure{background:var(--pressure)}
.sw.s1{background:var(--s1)}
.sw.s2{background:var(--s2)}
.worm{fill:none;stroke-width:2.5;vector-effect:non-scaling-stroke;
  stroke-linejoin:round;stroke-linecap:round}
.worm.s1{stroke:var(--s1)}
.worm.s2{stroke:var(--s2)}
.worm.pressure{stroke:var(--pressure);stroke-dasharray:5 4}
.wkt{fill:var(--wkt)}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:6px}
.key{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:.82rem}
.sw{width:12px;height:3px;border-radius:2px;display:inline-block}
/* Over-by-over duel */
table.duel th,table.duel td{padding:5px 6px;vertical-align:middle}
tr.tr-bat td{background:var(--tint-bat)}
tr.tr-bowl td{background:var(--tint-bowl)}
td.balls{white-space:nowrap}
.ball{display:inline-block;min-width:17px;padding:1px 3px;margin-right:2px;
  border-radius:4px;font-style:normal;font-size:.74rem;text-align:center;
  font-variant-numeric:tabular-nums;background:var(--bg);color:var(--dim)}
.ball.b-run{color:var(--ink)}
.ball.b-four,.ball.b-six{background:var(--accent);color:#fff;font-weight:700}
.ball.b-wkt{background:var(--wkt);color:#fff;font-weight:700}
.ball.b-extra{font-size:.66rem;color:var(--dim)}
/* Approach vs approach grid */
table.grid th{white-space:nowrap}
th.rowhead{text-align:left;color:var(--ink);text-transform:none;
  font-size:.8rem;letter-spacing:0}
td.cell{text-align:center;padding:5px 8px;border-bottom:2px solid var(--panel);
  border-right:2px solid var(--panel)}
td.cell b{display:block;font-size:.9rem;font-variant-numeric:tabular-nums}
td.cell span{display:block;font-size:.68rem;opacity:.75}
td.cell.empty{color:var(--dim);background:none}
td.cell.q0{background:var(--q0)}
td.cell.q1{background:var(--q1)}
td.cell.q2{background:var(--q2)}
td.cell.q3{background:var(--q3)}
td.cell.q4{background:var(--q4)}
.sq{display:inline-block;width:13px;height:9px;margin:0 1px;border-radius:2px;
  vertical-align:-1px}
.sq.q0{background:var(--q0)}
.sq.q1{background:var(--q1)}
.sq.q2{background:var(--q2)}
.sq.q3{background:var(--q3)}
.sq.q4{background:var(--q4)}
ol.turns{margin:0;padding-left:20px}
ol.turns li{margin-bottom:6px}
ul.trace{margin:0;padding-left:18px}
ul.trace li{margin-bottom:4px}
footer{color:var(--dim);font-size:.8rem;text-align:center;padding-top:8px}
"""

_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Match Analysis</title>
<style>{css}</style>
</head><body><div class="wrap">
<header>
  <h1>{title}</h1>
  <p class="headline">{headline}</p>
  <div class="scores">{scores}</div>
  <dl class="meta">{meta}</dl>
</header>
{body}
<footer>Generated by the CIPL / LETSPLAY Unified T20 Engine v3.0</footer>
</div></body></html>
"""
