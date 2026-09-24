"""The live match, as Bot API 10.1 rich messages, beside the HTML twins.

Where the numbers come from is unchanged: ``services/match_engine.py`` owns the
state and the HTML renderings (``build_live_scorecard``, ``bowler_figures``,
``format_timeline`` …) and this module reads the same state to build the block
tree. Every builder here has an HTML renderer next to it, and every sender
passes that HTML as the fallback, so a refused rich send never costs a ball —
the recipe in ``docs/rich-text-messages.md``.

Why the live board in particular. It is the most-read message in the bot and
the worst served by a proportional font: two batsmen, a bowler's figures, a run
rate and a chase requirement are all *columns*, and the HTML version lines them
up with ``str.ljust(18)``, which holds only while both batsmen have names of
about the same length. A native table is aligned by the client instead.

Three things are deliberately not tables:

* the ball's own headline (``FOUR!``, ``WICKET!``) is a ``pullquote`` — it is
  one loud fact, and a pullquote is the block for that;
* the commentary line is a ``blockquote`` — it is somebody talking over the
  cricket, which is what a quote block means;
* the timeline stays ``code``, because it is the one part of the board whose
  whole job is that every ball occupies the same width.
"""

import logging

from services import match_engine as E
from services import rich_message as R

logger = logging.getLogger(__name__)


def _safe(build, what):
    """Run a block builder, returning None rather than raising.

    A match must never stall on a renderer: every caller has the HTML twin
    ready, and None is how this module says "send that instead".
    """
    try:
        return build()
    except Exception:
        logger.exception("match rich %s failed to build", what)
        return None


def _bat_stats(s, player):
    stats = s.get("bat_stats") or {}
    rid = player.get("roster_id")
    return stats.get(str(rid)) or stats.get(rid) or {}


def _bowl_stats(s, player):
    stats = s.get("bowl_stats") or {}
    rid = (player or {}).get("roster_id")
    return stats.get(str(rid)) or stats.get(rid) or {}


# ── The live board (the twin of match_engine.build_live_scorecard) ───

def _innings_rows(s):
    """Both sides' totals: the one batting, and the one that set or awaits."""
    bat_name = s.get("bat_team_name") or "Batting"
    bowl_name = s.get("bowl_team_name") or "Bowling"
    if s.get("innings") == 2:
        return [
            [R.cell("🔴"), R.cell(R.bold(s.get("inn1_team") or "Team 1")),
             R.cell(R.bold(f"{s.get('inn1_runs', 0)}/{s.get('inn1_wickets', 0)}"),
                    align="right"),
             R.cell(f"({s.get('inn1_overs', '0.0')})", align="right")],
            [R.cell("🟢"), R.cell(R.bold(bat_name)),
             R.cell(R.bold(E.format_score(s)), align="right"),
             R.cell(f"({E.format_overs(s)}/{s.get('overs', 0)})", align="right")],
        ]
    projected = E.projected_score(s)
    return [
        [R.cell("🟢"), R.cell(R.bold(bat_name)),
         R.cell(R.bold(E.format_score(s)), align="right"),
         R.cell(f"({E.format_overs(s)}/{s.get('overs', 0)})", align="right")],
        [R.cell("🔴"), R.cell(R.bold(bowl_name)),
         R.cell(R.italic("Yet to bat"), align="right"),
         R.cell(f"Proj {projected}" if projected else "", align="right")],
    ]


def _batsmen_table(s, striker, non_striker):
    """The pair at the crease. ``*`` marks the striker, as the HTML does."""
    header = [R.cell(R.bold("BATSMAN"), header=True),
              R.cell(R.bold("R"), header=True, align="right"),
              R.cell(R.bold("B"), header=True, align="right"),
              R.cell(R.bold("4s"), header=True, align="right"),
              R.cell(R.bold("6s"), header=True, align="right"),
              R.cell(R.bold("SR"), header=True, align="right")]
    rows = [header]
    for player, on_strike in ((striker, True), (non_striker, False)):
        if not player:
            continue
        st = _bat_stats(s, player)
        runs, balls = st.get("runs", 0), st.get("balls", 0)
        name = f"{player.get('name', '?')}{' *' if on_strike else ''}"
        rows.append([
            R.cell(R.bold(name) if on_strike else name),
            R.cell(R.bold(str(runs)), align="right"),
            R.cell(str(balls), align="right"),
            R.cell(str(st.get("fours", 0)), align="right"),
            R.cell(str(st.get("sixes", 0)), align="right"),
            R.cell(f"{runs / balls * 100:.0f}" if balls else "—", align="right"),
        ])
    return R.table(rows, bordered=True, compact=True,
                   caption=R.bold("🏏 At the crease"))


def _bowler_table(s, bowler):
    """The bowler's figures — O, R, W, econ — in their own columns."""
    bw = _bowl_stats(s, bowler)
    done, extra = bw.get("overs_done", 0), bw.get("this_over_balls", 0)
    overs = f"{done}.{extra}" if extra else f"{done}"
    balls = done * 6 + extra
    runs = bw.get("runs", 0)
    econ = f"{runs / (balls / 6.0):.2f}" if balls else "—"
    return R.table([
        [R.cell(R.bold("BOWLER"), header=True),
         R.cell(R.bold("O"), header=True, align="right"),
         R.cell(R.bold("R"), header=True, align="right"),
         R.cell(R.bold("W"), header=True, align="right"),
         R.cell(R.bold("ECON"), header=True, align="right")],
        [R.cell(bowler.get("name", "?")),
         R.cell(overs, align="right"),
         R.cell(str(runs), align="right"),
         R.cell(R.bold(str(bw.get("wickets", 0))), align="right"),
         R.cell(econ, align="right")],
    ], bordered=True, compact=True, caption=R.bold("🎯 Bowling"))


def _rate_rows(s):
    """Current rate, required rate, projection — whichever the innings has."""
    rows = [[R.cell(R.bold("⚡ CRR")), R.cell(str(E.crr(s)), align="right")]]
    required = E.rrr(s)
    if required is not None:
        rows.append([R.cell(R.bold("🎯 RRR")),
                     R.cell(R.bold(str(required)), align="right")])
    projected = E.projected_score(s)
    if projected and s.get("innings") == 1:
        rows.append([R.cell(R.bold("📈 Projected")),
                     R.cell(str(projected), align="right")])
    return rows


def _chase_paragraph(s):
    """"Need N off M" — the only number that matters in a run chase."""
    if s.get("innings") != 2 or not s.get("target"):
        return None
    try:
        chase = E.chase_requirements(s)
    except Exception:
        return None
    if not chase:
        return None
    return R.pullquote(
        ["🎯 Need ", R.bold(str(chase["runs_required"])), " from ",
         R.bold(str(chase["balls_remaining"])), " balls"],
        caption="To win")


def _pitch_paragraph(s):
    """The pitch-wear note, on the same terms the HTML board shows it."""
    try:
        from engine import pitch_registry
        from services.probability_engine import calc_pitch_wear
        wear = calc_pitch_wear(s.get("innings", 1), s.get("current_over", 1),
                               s.get("overs", 20))
        if wear < 30:
            return None
        label = ("Heavily Worn 🟫" if wear >= 60 else
                 "Worn 🟤" if wear >= 45 else "Moderately Worn 🟧")
        return R.paragraph(["📍 ", R.bold(pitch_registry.normalise(
            s.get("pitch_type"))), f"  ·  {label}"])
    except Exception:
        return None


def _chemistry_blocks(s, striker, non_striker):
    """Team chemistry and the partnership bond, when both are scorable."""
    out = []
    try:
        from services import chemistry
        bat = chemistry.live_badge(s.get("bat_xi") or [])
        bowl = chemistry.live_badge(s.get("bowl_xi") or [])
    except Exception:
        bat = bowl = None
    # All or nothing, as build_chemistry_line has it: one side's number alone
    # invites a comparison that cannot be made.
    if bat and bowl:
        out.append(R.table([
            [R.cell("🏏"), R.cell(s.get("bat_team_name") or "Batting"),
             R.cell(bat, align="right")],
            [R.cell("🎯"), R.cell(s.get("bowl_team_name") or "Bowling"),
             R.cell(bowl, align="right")],
        ], bordered=True, compact=True, caption=R.bold("🧪 Team chemistry")))
    partnership = ["🤝 ", R.bold("Partnership"),
                   f"  ➤  {s.get('partnership_runs', 0)} "
                   f"({s.get('partnership_balls', 0)})"]
    try:
        from services import chemistry
        bond = chemistry.bond_label(
            chemistry.partnership_bond(striker, non_striker))
    except Exception:
        bond = None
    if bond:
        partnership.append(f"  ·  {bond}")
    out.append(R.paragraph(partnership))
    return out


def live_scorecard_blocks(s):
    """The live board — the block twin of ``match_engine.build_live_scorecard``.

    Same sections in the same order (both totals, the crease, chemistry and the
    partnership, the rates, the bowler, the timeline), each one rendered as the
    block that fits it rather than as a padded line.
    """
    def build():
        striker = E.get_striker(s)
        non_striker = E.get_non_striker(s)
        bowler = E.get_bowler(s)

        blocks = [R.heading("🏏 LIVE MATCH UPDATE", size=3),
                  R.table(_innings_rows(s), bordered=True, compact=True)]
        pitch = _pitch_paragraph(s)
        if pitch is not None:
            blocks.append(pitch)
        chase = _chase_paragraph(s)
        if chase is not None:
            blocks.append(chase)
        blocks.append(_batsmen_table(s, striker, non_striker))
        blocks.extend(_chemistry_blocks(s, striker, non_striker))
        blocks.append(R.table(_rate_rows(s), bordered=True, compact=True,
                              caption=R.bold("📊 Run rate")))
        if bowler:
            blocks.append(_bowler_table(s, bowler))
        timeline = E.format_timeline(s)
        if timeline:
            blocks.append(R.paragraph([R.bold("⏱ Timeline"), "  ➤  ", timeline]))
        return blocks
    return _safe(build, "live scorecard")


# ── The ball that was just bowled ────────────────────────────────────

def ball_result_blocks(s, *, bowler_name, delivery, striker_name, shot,
                       headline, commentary=None, trait_lines=()):
    """One delivery's result: what happened, then the board it happened on.

    ``headline`` is the plain-text form of the HTML renderer's ``rtxt`` — "SIX!",
    "WICKET! …" — and becomes a pullquote, because a boundary is one loud fact
    and everything else on the message is context for it.
    """
    def build():
        blocks = [R.table([
            [R.cell("🎳"), R.cell(R.bold(bowler_name or "?")),
             R.cell(delivery or "?", align="right")],
            [R.cell("🏏"), R.cell(R.bold(striker_name or "?")),
             R.cell(shot or "?", align="right")],
        ], compact=True)]
        blocks.append(R.pullquote(R.bold(headline or "")))
        if commentary:
            blocks.append(R.blockquote(
                [R.paragraph(["💬 ", R.italic(commentary)])]))
        for line in trait_lines:
            if line:
                blocks.append(R.paragraph(line))
        board = live_scorecard_blocks(s)
        if board is None:
            return None
        blocks.append(R.divider())
        blocks.extend(board)
        return blocks
    return _safe(build, "ball result")


# ── The two prompts a player answers ─────────────────────────────────

def _prompt_head(s, *, emoji):
    """The over, the ball, and the three numbers both prompts open with."""
    return [R.heading(f"{emoji} OVER {s.get('current_over')} • "
                      f"BALL {s.get('current_ball', 0) + 1}", size=3),
            R.table([
                [R.cell(R.bold("📊 Score")),
                 R.cell(R.bold(E.format_score(s)), align="right")],
                [R.cell(R.bold("⏱ Overs")),
                 R.cell(f"{E.format_overs(s)}/{s.get('overs', 0)}",
                        align="right")],
                [R.cell(R.bold("⚡ CRR")), R.cell(str(E.crr(s)), align="right")],
            ], bordered=True, compact=True)]


def delivery_prompt_blocks(s, *, bowler, striker, phase, mention_text,
                           mention_tg_id, choose_label, ai_note=None):
    """"Choose your delivery" — the bowler's turn.

    The mention is a real ``tg://user`` node rather than an HTML ``<a>``, so the
    person being asked to bowl is pinged by the block tree too.
    """
    def build():
        blocks = []
        if ai_note:
            blocks.append(R.paragraph(R.italic(ai_note)))
        blocks += _prompt_head(s, emoji="🎳")
        blocks.append(R.table([
            [R.cell("🎳"), R.cell(R.bold(bowler.get("name", "?"))),
             R.cell(f"{bowler.get('bowl_rating', '—')} BWL", align="right")],
            [R.cell("🏏"), R.cell(striker.get("name", "?")),
             R.cell(f"{striker.get('bat_rating', '—')} BAT", align="right")],
            [R.cell("📍"), R.cell(phase or "—"), R.cell("")],
        ], bordered=True, compact=True))
        blocks.append(R.paragraph([
            R.mention(R.bold(mention_text), mention_tg_id), ", ",
            R.bold(choose_label)]))
        return blocks
    return _safe(build, "delivery prompt")


def shot_prompt_blocks(s, *, bowler, striker, delivery, mention_text,
                       mention_tg_id, ai_note=None):
    """"Play your shot" — the batsman's turn, with what is coming at them."""
    def build():
        st = _bat_stats(s, striker)
        blocks = []
        if ai_note:
            blocks.append(R.paragraph(R.italic(ai_note)))
        blocks += _prompt_head(s, emoji="🏏")
        blocks.append(R.table([
            [R.cell("🎳"), R.cell(bowler.get("name", "?")),
             R.cell(R.bold(delivery or "?"), align="right")],
            [R.cell("🏏"), R.cell(R.bold(striker.get("name", "?"))),
             R.cell(f"{st.get('runs', 0)}({st.get('balls', 0)})",
                    align="right")],
        ], bordered=True, compact=True))
        blocks.append(R.paragraph([
            R.mention(R.bold(mention_text), mention_tg_id), ", ",
            R.bold("play your shot:")]))
        return blocks
    return _safe(build, "shot prompt")


# ── The XI, announced ────────────────────────────────────────────────

def playing_xi_blocks(team_name, xi, *, bench=(), subtitle=None, footer=None,
                      name_of=str, detail_of=None):
    """A confirmed Playing XI as a numbered table, bench behind a ``details``.

    Shared by every mode that announces an XI before a match, so the batting
    order a captain reads is numbered the same way whichever mode they are in —
    and the number they type at ``/change`` means the same thing.

    ``name_of`` and ``detail_of`` turn whatever the caller holds (an ORM player,
    an engine dict) into a name and a right-hand detail column; keeping them as
    callbacks is what lets one builder serve all of them.
    """
    def build():
        header = [R.cell(R.bold("#"), header=True, align="center"),
                  R.cell(R.bold("PLAYER"), header=True),
                  R.cell(R.bold("ROLE"), header=True, align="right")]

        def rows(players, start):
            out = [header]
            for position, player in enumerate(players, start):
                out.append([
                    R.cell(str(position), align="center"),
                    R.cell(name_of(player)),
                    R.cell(detail_of(player) if detail_of else "",
                           align="right"),
                ])
            return out

        blocks = [R.heading(f"✅ {team_name} — Playing XI", size=3)]
        if subtitle:
            blocks.append(R.paragraph(subtitle))
        blocks.append(R.table(rows(xi, 1), bordered=True, striped=True,
                              compact=True, caption=R.bold("🏏 Playing XI")))
        if bench:
            blocks.append(R.details(
                R.bold(f"🪑 Bench ({len(bench)})"),
                [R.table(rows(bench, len(xi) + 1), bordered=True,
                         compact=True)]))
        if footer:
            blocks.append(R.footer(footer))
        return blocks
    return _safe(build, "playing XI")
