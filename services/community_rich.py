"""Rich-text rendering for Community Arena surfaces.

Every builder returns ``(blocks, html)``: ``blocks`` is the Bot API rich layout
(headings, tables, collapsible sections) and ``html`` is the always-built twin
that :mod:`services.rich_message` falls back to when rich delivery is off or a
block build fails. A renderer bug therefore costs formatting, never the
message.
"""

from __future__ import annotations

import html as _html
import logging

from services import rich_message as R

logger = logging.getLogger(__name__)

BAR_LEN = 10


def esc(s) -> str:
    return _html.escape(str(s if s is not None else ""))


def progress_bar(pct: float, length: int = BAR_LEN) -> str:
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(pct / 100.0 * length))
    return "█" * filled + "░" * (length - filled)


def _safe(build, what):
    try:
        return build()
    except Exception:
        logger.exception("community rich %s blocks failed", what)
        return None


# ── Check-in ─────────────────────────────────────────────────────────────

def checkin_card(res: dict, username: str):
    u = esc(username)
    if res.get("already"):
        html = (f"📅 <b>COMMUNITY CHECK-IN</b>\n"
                f"Manager: {u}\n"
                f"✅ You have already checked in today.\n"
                f"🔥 Current streak: {res.get('streak', 0)} days\n"
                f"Come back tomorrow to keep the streak alive!")
        blocks = _safe(lambda: [
            R.heading("📅 Community Check-in", 3),
            R.paragraph([R.bold("Manager: "), username]),
            R.paragraph("✅ Already checked in today."),
            R.paragraph(f"🔥 Current streak: {res.get('streak', 0)} days"),
        ], "checkin_already")
        return blocks, html

    lvl = f"Level {res.get('level')} — {esc(res.get('title'))}"
    extra = ""
    if res.get("leveled_up"):
        extra += f"\n⬆️ <b>LEVEL UP!</b> You are now {lvl}."
    if res.get("milestones"):
        for m in res["milestones"]:
            extra += f"\n🎁 Milestone: {esc(m['detail'])}"
    if res.get("achievements"):
        extra += "\n🏅 Achievement: " + ", ".join(esc(a) for a in res["achievements"])

    html = (f"📅 <b>COMMUNITY CHECK-IN</b>\n"
            f"Manager: {u}\n"
            f"✅ Check-in successful\n"
            f"🔥 Current streak: {res.get('streak', 0)} days\n"
            f"🏅 Longest streak: {res.get('longest', 0)} days\n"
            f"✨ Community XP: +{res.get('credited', 0)}\n"
            f"Total CXP: {int(res.get('lifetime_xp') or 0):,}\n"
            f"Level: {lvl}{extra}")
    blocks = _safe(lambda: [
        R.heading("📅 Community Check-in", 3),
        R.paragraph([R.bold("Manager: "), username, "  ✅ Check-in successful"]),
        R.table([
            [R.cell("🔥 Current streak"), R.cell(R.bold(f"{res.get('streak', 0)} days"), align="right")],
            [R.cell("🏅 Longest streak"), R.cell(f"{res.get('longest', 0)} days", align="right")],
            [R.cell("✨ Community XP"), R.cell(R.bold(f"+{res.get('credited', 0)}"), align="right")],
            [R.cell("Total CXP"), R.cell(f"{int(res.get('lifetime_xp') or 0):,}", align="right")],
            [R.cell("Level"), R.cell(lvl, align="right")],
        ]),
    ] + ([R.paragraph(R.bold(f"⬆️ LEVEL UP! You are now {lvl}."))] if res.get("leveled_up") else []),
        "checkin_ok")
    return blocks, html


# ── CXP summary (/cxp) ─────────────────────────────────────────────────

def cxp_card(d: dict):
    name = esc(d["name"])
    prog = d["progress"]
    bar = progress_bar(prog["pct"])
    nxt = ""
    if prog.get("next_level"):
        nxt = (f"\nProgress to Level {prog['next_level']} — {esc(prog['next_title'])}:\n"
               f"{bar} {prog['pct']:.1f}%\n{prog['remaining']:,} CXP remaining")
    else:
        nxt = "\n🏆 Maximum level reached!"
    html = (f"✨ <b>COMMUNITY XP</b>\n"
            f"Manager: {name}\n"
            f"Level {d['level']} — {esc(d['title'])}\n"
            f"Lifetime XP: {d['lifetime_xp']:,}\n"
            f"Today: {d['today']:,} · Week: {d['week']:,} · Month: {d['month']:,}"
            f"{nxt}")
    blocks = _safe(lambda: [
        R.heading("✨ Community XP", 3),
        R.paragraph([R.bold("Manager: "), d["name"]]),
        R.paragraph([R.bold(f"Level {d['level']} — {d['title']}")]),
        R.table([
            [R.cell("Lifetime XP"), R.cell(R.bold(f"{d['lifetime_xp']:,}"), align="right")],
            [R.cell("Today"), R.cell(f"{d['today']:,}", align="right")],
            [R.cell("This week"), R.cell(f"{d['week']:,}", align="right")],
            [R.cell("This month"), R.cell(f"{d['month']:,}", align="right")],
        ]),
        R.paragraph(nxt.strip()),
    ], "cxp")
    return blocks, html


# ── Full profile (/community) ──────────────────────────────────────────

def profile_card(d: dict):
    name = esc(d["name"])
    prog = d["progress"]
    bar = progress_bar(prog["pct"])
    rank = d.get("rank")
    rank_line = f"Community Rank: #{rank}" if rank else "Community Rank: unranked"
    prog_block = (f"Progress to Level {prog['next_level']}:\n{bar} {prog['pct']:.1f}%\n"
                  f"{prog['remaining']:,} CXP remaining"
                  if prog.get("next_level") else "🏆 Maximum level reached!")

    act = d["activity"]
    games = d["games"]
    ach = d["achievements"]

    activity_lines = (
        f"• Eligible messages: {act['chat_messages']:,}\n"
        f"• Completed PvP matches: {act['pvp_matches']:,}\n"
        f"• Completed AI matches: {act['ai_matches']:,}\n"
        f"• Successful claims: {act['claims']:,}\n"
        f"• Total check-ins: {act['total_checkins']:,}\n"
        f"• Current streak: {act['current_streak']} days\n"
        f"• Longest streak: {act['longest_streak']} days\n"
        f"• Days active: {act['days_active']:,}")
    games_lines = (
        f"• Games played: {games['plays']:,}\n"
        f"• Total wins: {games['wins']:,}\n"
        f"• Win rate: {games['win_pct']:.0f}%\n"
        + "\n".join(f"• {lbl} wins: {cnt:,}" for lbl, cnt in games["by_type"]) +
        (f"\n• Fastest answer: {games['fastest_s']:.2f}s" if games.get("fastest_s") else "") +
        (f"\n• Avg winning time: {games['avg_s']:.2f}s" if games.get("avg_s") else ""))
    ach_line = (", ".join(esc(a) for a in ach["unlocked"]) if ach["unlocked"]
                else "None yet — play games and check in daily!")

    html = (f"🏟️ <b>CMU COMMUNITY PROFILE</b>\n"
            f"Manager: {name}\n"
            f"Level {d['level']} — {esc(d['title'])}\n"
            f"{rank_line}\n"
            f"Lifetime XP: {d['lifetime_xp']:,}\n"
            f"Weekly XP: {d['week']:,}\nMonthly XP: {d['month']:,}\n"
            f"{prog_block}\n\n"
            f"<b>📊 Community Activity</b>\n{activity_lines}\n\n"
            f"<b>🎮 Community Games</b>\n{games_lines}\n\n"
            f"<b>🏅 Achievements</b> ({ach['count']}/{ach['total']})\n{ach_line}")

    def build():
        blocks = [
            R.heading("🏟️ CMU Community Profile", 2),
            R.paragraph([R.bold("Manager: "), d["name"]]),
            R.paragraph([R.bold(f"Level {d['level']} — {d['title']}"), f"   ·   {rank_line}"]),
            R.table([
                [R.cell("Lifetime XP"), R.cell(R.bold(f"{d['lifetime_xp']:,}"), align="right")],
                [R.cell("Weekly XP"), R.cell(f"{d['week']:,}", align="right")],
                [R.cell("Monthly XP"), R.cell(f"{d['month']:,}", align="right")],
            ]),
            R.paragraph(prog_block),
            R.details(R.bold("📊 Community Activity"),
                      [R.paragraph(activity_lines)]),
            R.details(R.bold("🎮 Community Games"),
                      [R.paragraph(games_lines)]),
            R.details(R.bold(f"🏅 Achievements ({ach['count']}/{ach['total']})"),
                      [R.paragraph(ach_line)]),
        ]
        return blocks
    return _safe(build, "profile"), html


# ── Winner / timeout announcements ─────────────────────────────────────

def winner_card(*, game_label, winner_name, answer_name, response_ms,
                participants, award):
    secs = (response_ms or 0) / 1000.0
    credited = award.get("credited", 0)
    total = int(award.get("lifetime_xp") or 0)
    extra = ""
    if award.get("leveled_up"):
        extra += (f"\n⬆️ <b>LEVEL UP!</b> Level {award.get('old_level')} → "
                  f"{award.get('level')} ({esc(award.get('title'))})")
    for m in (award.get("milestones") or []):
        extra += f"\n🎁 {esc(m['detail'])}"
    if award.get("achievements"):
        extra += "\n🏅 " + ", ".join(esc(a) for a in award["achievements"])

    html = ("🏆 <b>WE HAVE A WINNER!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Game: {esc(game_label)}\n"
            f"👑 Winner: {esc(winner_name)}\n"
            f"✅ Answer: {esc(answer_name)}\n"
            f"⚡ Response time: {secs:.2f} seconds\n"
            f"👥 Participants: {participants}\n"
            f"✨ Community XP: +{credited}\n"
            f"Total CXP: {total:,}{extra}\n"
            "━━━━━━━━━━━━━━━━━━")
    blocks = _safe(lambda: [
        R.heading("🏆 We have a winner!", 2),
        R.table([
            [R.cell("Game"), R.cell(game_label, align="right")],
            [R.cell("👑 Winner"), R.cell(R.bold(winner_name), align="right")],
            [R.cell("✅ Answer"), R.cell(answer_name, align="right")],
            [R.cell("⚡ Response time"), R.cell(f"{secs:.2f}s", align="right")],
            [R.cell("👥 Participants"), R.cell(str(participants), align="right")],
            [R.cell("✨ Community XP"), R.cell(R.bold(f"+{credited}"), align="right")],
            [R.cell("Total CXP"), R.cell(f"{total:,}", align="right")],
        ]),
    ] + ([R.paragraph(R.bold(f"⬆️ LEVEL UP! Now {award.get('title')}"))]
         if award.get("leveled_up") else []), "winner")
    return blocks, html


def timeout_card(*, game_label, answer_name):
    html = ("⏳ <b>TIME'S UP!</b>\n"
            "Nobody guessed the correct answer.\n"
            f"Correct answer: {esc(answer_name)}\n"
            "Better luck next time!")
    blocks = _safe(lambda: [
        R.heading("⏳ Time's up!", 3),
        R.paragraph("Nobody guessed the correct answer."),
        R.paragraph([R.bold("Correct answer: "), answer_name]),
        R.paragraph("Better luck next time!"),
    ], "timeout")
    return blocks, html


# ── Leaderboards ─────────────────────────────────────────────────────────

PERIOD_LABEL = {"today": "Today", "week": "This Week",
                "month": "This Month", "all": "All Time"}


def leaderboard_card(lb: dict, *, me_name=None):
    period = PERIOD_LABEL.get(lb["period"], lb["period"])
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for row in lb["rows"]:
        marker = medals.get(row["rank"], f"{row['rank']}.")
        val = _fmt_value(lb, row["value"])
        lines.append(f"{marker} {esc(row['name'])} — {val}")
    body = "\n".join(lines) if lines else "No entries yet."
    me = ""
    shown_ranks = {r["rank"] for r in lb["rows"]}
    if lb.get("me") and lb["me"]["rank"] not in shown_ranks:
        me = f"\n\n➡️ Your rank: #{lb['me']['rank']} — {_fmt_value(lb, lb['me']['value'])}"
    html = (f"🏆 <b>CMU LEADERBOARD</b>\n{esc(lb['label'])} · {period}\n"
            f"━━━━━━━━━━━━━━━━━━\n{body}{me}")
    blocks = _safe(lambda: [
        R.heading(f"🏆 {lb['label']}", 3),
        R.paragraph(period),
        R.list_block([f"{medals.get(r['rank'], str(r['rank']) + '.')} "
                      f"{r['name']} — {_fmt_value(lb, r['value'])}"
                      for r in lb["rows"]]) if lb["rows"]
        else R.paragraph("No entries yet."),
    ] + ([R.footer(f"Your rank: #{lb['me']['rank']} — {_fmt_value(lb, lb['me']['value'])}")]
         if lb.get("me") and lb["me"]["rank"] not in {r["rank"] for r in lb["rows"]}
         else []), "leaderboard")
    return blocks, html


def _fmt_value(lb, value):
    if lb["category"] == "ff_records":
        return f"{(value or 0) / 1000.0:.2f}s"
    if lb["category"] == "streak":
        return f"{value} days"
    if lb["category"] == "cxp":
        return f"{value:,} CXP"
    return f"{value:,}"


# ── Levels / rewards / help ─────────────────────────────────────────────

def levels_card(ladder, current_level):
    lines = []
    for lvl, title, req in ladder:
        mark = "➡️" if lvl == current_level else ("✅" if lvl < current_level else "🔒")
        lines.append(f"{mark} <b>L{lvl}</b> {esc(title)} — {req:,} CXP")
    html = "🏆 <b>COMMUNITY LEVELS</b>\n" + "\n".join(lines)
    blocks = _safe(lambda: [
        R.heading("🏆 Community Levels", 3),
        R.list_block([f"{'➡️' if l == current_level else ('✅' if l < current_level else '🔒')} "
                      f"L{l} {t} — {r:,} CXP" for l, t, r in ladder]),
    ], "levels")
    return blocks, html


def rewards_card(rows):
    lines = []
    for r in rows:
        mark = "✅" if r["claimed"] else ("🎁" if r["reached"] else "🔒")
        lines.append(f"{mark} <b>L{r['level']}</b> {esc(r['title'])} — {esc(r['reward'])}")
    html = ("🎁 <b>COMMUNITY MILESTONE REWARDS</b>\n"
            "Monetary rewards are enabled by admins per level.\n\n"
            + "\n".join(lines))
    blocks = _safe(lambda: [
        R.heading("🎁 Milestone Rewards", 3),
        R.list_block([f"{'✅' if r['claimed'] else ('🎁' if r['reached'] else '🔒')} "
                      f"L{r['level']} {r['title']} — {r['reward']}" for r in rows]),
    ], "rewards")
    return blocks, html


def help_card(group_label=None, group_url=None):
    where = esc(group_label) if group_label else "the Official Group"
    link = f"\n👉 Join &amp; play here: {esc(group_label)}" if group_label else ""
    html = (
        "🏟️ <b>CMU COMMUNITY ARENA — GUIDE</b>\n\n"
        f"All Community Arena activity happens in <b>{where}</b>.{link}\n\n"
        "Earn <b>Community XP (CXP)</b> by being active in the Official Group:\n"
        "• Chat (1 CXP), daily /checkin (20), complete matches, claim players\n"
        "• Win automatic mini-games (20–40 CXP each)\n\n"
        "<b>Mini-games</b> (first correct answer wins):\n"
        "🕵️ Mystery Card · 🎯 Odd One Out · 🧩 Cricket Wordle\n"
        "⚡ Fastest Fingers · 🔢 Guess the Rating · 🧠 Guess the Cricketer\n"
        "Just reply with your answer — no command needed.\n\n"
        "<b>Commands</b>\n"
        "/community — your profile\n/cxp — XP & progress\n/checkin — daily check-in\n"
        "/gctop — leaderboards\n/gcgames — game stats\n/gclevels — level ladder\n"
        "/gcrewards — milestone rewards\n/gchelp — this guide")
    blocks = _safe(lambda: [
        R.heading("🏟️ CMU Community Arena — Guide", 2),
        R.paragraph([f"All activity happens in ", R.bold(group_label or "the Official Group"), "."]),
        R.paragraph("Earn Community XP (CXP) by being active in the Official Group."),
        R.details(R.bold("How to earn"), [R.list_block([
            "Chat messages — 1 CXP", "Daily /checkin — 20 CXP",
            "Complete matches & claim players", "Win mini-games — 20–40 CXP"])]),
        R.details(R.bold("Mini-games"), [R.list_block([
            "🕵️ Mystery Card", "🎯 Odd One Out", "🧩 Cricket Wordle",
            "⚡ Fastest Fingers", "🔢 Guess the Rating", "🧠 Guess the Cricketer"])]),
        R.footer("/community · /cxp · /checkin · /gctop · /gcgames · /gclevels · /gcrewards"),
    ], "help")
    return blocks, html


# ── Game announcement texts (media is attached by the handler) ──────────

def game_intro_html(game_type, payload):
    q = payload.get("question", {})
    reward = payload.get("reward", 0)
    dur = payload.get("duration", 60)
    if game_type == "mystery":
        return ("🕵️ <b>MYSTERY CARD</b>\n"
                "Identify the cricketer in this blurred player card.\n"
                f"⏳ Time: {dur} seconds\n🏆 Reward: {reward} Community XP\n"
                "The first correct answer wins!")
    if game_type == "fastestfingers":
        return ("⚡ <b>FASTEST FINGERS</b>\n"
                "Reveal the hidden player card.\n"
                "Type the correct player name before everyone else!\n"
                f"⏳ Time: {dur} seconds\n🏆 Reward: {reward} CXP\n"
                "First correct answer wins.")
    if game_type == "oddoneout":
        opts = "\n".join(f"{o['letter']}. {esc(o['name'])}" for o in q.get("options", []))
        return ("🎯 <b>ODD ONE OUT</b>\n" + opts +
                f"\nHint: {esc(q.get('hint'))}\n⏳ {dur} seconds | 🏆 {reward} CXP\n"
                "Reply with the odd one's letter or name.")
    if game_type == "wordle":
        n = q.get("length", 5)
        return ("🧩 <b>CRICKET WORDLE</b>\n"
                f"Guess the {n}-letter cricketer's surname.\n"
                + "⬜" * n + f"\nAttempts: {payload.get('max_attempts', 6)}\n"
                f"⏳ Time: {dur} seconds\n🏆 Reward: {reward} CXP\n"
                "Reply with your guess.")
    if game_type == "rating":
        return ("🔢 <b>GUESS THE RATING</b>\n"
                f"Player: {esc(q.get('name'))}\nCountry: {esc(q.get('country'))}\n"
                f"Category: {esc(q.get('category'))}\nVersion: {esc(q.get('version'))}\n"
                "Overall Rating: ???\n"
                "Guess the exact CCM26 overall rating.\n"
                f"⏳ Time: {dur} seconds\n🏆 Reward: {reward} CXP")
    if game_type == "cricketer":
        clues = "\n".join(f"{c['label']}: {esc(c['value'])}" for c in q.get("clues", []))
        return ("🧠 <b>GUESS THE CRICKETER</b>\n"
                "I am a cricketer with the following attributes:\n"
                f"{clues}\nGuess my name!\n"
                f"⏳ Time: {dur} seconds\n🏆 Reward: {reward} CXP")
    return "🎮 Community game started!"
