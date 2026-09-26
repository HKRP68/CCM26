"""The onboarding surfaces as Bot API 10.1 rich messages, with HTML twins.

Four cards:
  * :func:`welcome_card`: /start for someone with no account yet. It gives a
    short pitch and three steps (join the GC, /debut, first match), not
    the 80-command wall /start used to be.
  * :func:`journey_card`: the guided first-session checklist (after /debut,
    on /start while the journey is running, and on "✅ step done").
  * :func:`status_card`: /start for everyone else: purse, squad, what is
    ready to collect right now.
  * :func:`commands_card`: /commands, the full catalogue, one collapsible
    section per category.

Every builder returns ``(blocks, html, keyboard)``. ``blocks`` is None when the
rich build fails, and ``rich_message`` then sends the HTML, which is always
built. So a renderer bug costs the formatting, never the message.
"""

from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from services import rich_message as R

logger = logging.getLogger(__name__)

BRAND = "CricMaster Ultra"


def _safe_blocks(build, what):
    try:
        return build()
    except Exception:
        logger.exception("onboarding %s blocks failed to build", what)
        return None


def _bot_username():
    import os
    return (os.getenv("BOT_USERNAME") or "").lstrip("@").strip()


def _miniapp(label, tab):
    try:
        from services.miniapp_buttons import miniapp_button
        return miniapp_button(label, tab, is_private=True)
    except Exception:
        return None


def _gc_buttons(cfg, *, verify_data):
    from services.gc_gate import join_url
    url = join_url(cfg)
    row = []
    if url:
        row.append(InlineKeyboardButton("🔗 Join Official GC", url=url))
    row.append(InlineKeyboardButton("✅ I've joined", callback_data=verify_data))
    return row


# ── /start: brand-new visitor ───────────────────────────────────────

def welcome_card(first_name=None, *, in_gc=None, cfg=None, extra_html=""):
    """``in_gc`` True/False ticks step one; None leaves it open."""
    from services import gc_gate, onboarding_service
    has_gc = onboarding_service.gc_step_applies(cfg)
    gate_on = gc_gate.is_gc_gate_active(cfg)
    name = (first_name or "").strip()
    hello = f"Welcome, {name}!" if name else "Welcome!"

    steps = []
    if has_gc:
        steps.append((bool(in_gc), "Join the Official GC",
                      "required to play" if gate_on else "match partners, giveaways, help"))
    steps.append((False, "Send /debut", "claim your free starting squad + coins and gems"))
    steps.append((False, "Play your first match",
                  "/vsbot, practice against the bot, no opponent needed"))

    n_goals = len(onboarding_service.active_steps(cfg))
    pitch = ("Collect real cricketers, build your dream XI, play live matches "
             "and climb the monthly season leaderboard.")

    def build():
        blocks = [
            R.heading(f"🏏 {hello}", 1),
            R.paragraph([R.bold(BRAND), " — ", pitch]),
            R.heading("Get playing in 3 steps", 4),
            R.checklist([(done, [R.bold(t), " — ", sub]) for done, t, sub in steps]),
            R.paragraph(["🎁 ", R.bold("New-player journey:"),
                         f" after /debut, {n_goals} quick goals each pay coins & gems, "
                         "with a big bonus for finishing them all."]),
            R.footer("Every command: /commands · Guide: /howto"),
        ]
        return blocks

    lines = [f"🏏 <b>{html.escape(hello)}</b>", "",
             f"<b>{BRAND}</b> — {pitch}", "", "<b>Get playing in 3 steps</b>"]
    for i, (done, t, sub) in enumerate(steps, 1):
        mark = "✅" if done else f"{i}️⃣"
        lines.append(f"{mark} <b>{t}</b> — {html.escape(sub)}")
    lines += ["", f"🎁 <b>New-player journey:</b> after /debut, {n_goals} quick goals each "
              "pay coins &amp; gems, with a big bonus for finishing them all.",
              "", "<i>Every command: /commands · Guide: /howto</i>"]
    html_text = "\n".join(lines) + (extra_html or "")

    rows = []
    if has_gc and not in_gc:
        rows.append(_gc_buttons(cfg, verify_data="gcjoin_check"))
    rows.append([InlineKeyboardButton("🎁 Start — /debut", callback_data="onb_debut")])
    app = _miniapp("📱 Open the Mini App", "roster")
    if app is not None:
        rows.append([app])
    return _safe_blocks(build, "welcome"), html_text, InlineKeyboardMarkup(rows)


# ── The journey checklist ───────────────────────────────────────────

def _step_how(step):
    how = step.how
    if step.command:
        how = f"Send {step.command}. {how}"
    elif step.miniapp_tab:
        how = f"Open the Mini App. {how}"
    return how


def journey_card(user, *, cfg=None, just_done=None, reward=None):
    """The checklist. ``just_done`` (a step key) adds a "✅ done" banner."""
    from services import onboarding_service as O
    steps = O.active_steps(cfg)
    done = set(O.done_keys(user))
    nxt = O.next_step(user, cfg)
    n_done, total = O.progress(user, cfg)
    finished = getattr(user, "onboarding_done_at", None) is not None
    finale = O.reward_label(O.FINALE_COINS, O.FINALE_GEMS)

    banner = None
    if just_done and just_done in O.STEP_BY_KEY:
        st = O.STEP_BY_KEY[just_done]
        banner = f"✅ {st.title} (+{O.reward_label(st.coins, st.gems)})"

    def build():
        blocks = []
        if banner:
            blocks.append(R.pullquote(banner))
        if finished:
            blocks += [
                R.heading("🏆 Journey complete!", 2),
                R.paragraph(["You finished every step and earned the ",
                             R.bold(finale), " finale bonus. You're a real "
                             "manager now. Keep your daily streak alive and "
                             "climb the season leaderboard."]),
            ]
        else:
            blocks.append(R.heading(f"🧭 Your journey — {n_done}/{total}", 2))
        items = []
        for s in steps:
            items.append((s.key in done,
                          [R.bold(s.title), f"  +{O.reward_label(s.coins, s.gems)}"]))
        blocks.append(R.checklist(items))
        if nxt and not finished:
            blocks.append(R.heading(f"👉 Next: {nxt.title}", 4))
            blocks.append(R.paragraph(_step_how(nxt)))
            blocks.append(R.footer(f"Finish all {total} for a bonus {finale}."))
        return blocks

    lines = []
    if banner:
        lines += [f"<b>{html.escape(banner)}</b>", ""]
    if finished:
        lines += ["🏆 <b>Journey complete!</b>",
                  f"You earned the <b>{finale}</b> finale bonus. Keep your daily "
                  "streak alive and climb the season leaderboard.", ""]
    else:
        lines += [f"🧭 <b>Your journey — {n_done}/{total}</b>", ""]
    for s in steps:
        mark = "✅" if s.key in done else "⬜"
        lines.append(f"{mark} {s.title}  <i>+{O.reward_label(s.coins, s.gems)}</i>")
    if nxt and not finished:
        lines += ["", f"👉 <b>Next: {nxt.title}</b>", html.escape(_step_how(nxt)),
                  "", f"<i>Finish all {total} for a bonus {finale}.</i>"]
    html_text = "\n".join(lines)

    rows = []
    if nxt and not finished:
        if nxt.key == "gc":
            rows.append(_gc_buttons(cfg, verify_data="onb_gc"))
        elif nxt.miniapp_tab:
            b = _miniapp(f"▶️ {nxt.title}", nxt.miniapp_tab)
            if b is not None:
                rows.append([b])
        if nxt.command:
            rows.append([InlineKeyboardButton(
                f"▶️ {nxt.title}", callback_data=f"onb_do_{nxt.key}")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="onb_view"),
                 InlineKeyboardButton("📋 All commands", callback_data="onb_cmds")])
    return _safe_blocks(build, "journey"), html_text, InlineKeyboardMarkup(rows)


def step_done_card(user, key, cfg=None):
    return journey_card(user, cfg=cfg, just_done=key)


# ── /start: returning player ────────────────────────────────────────

def status_card(user, *, ready=(), streak=None, extra_html=""):
    """``ready`` is a list of ``(label, command)`` things to collect now."""
    name = (getattr(user, "first_name", None) or getattr(user, "username", None)
            or "manager")
    coins = getattr(user, "total_coins", 0) or 0
    gems = getattr(user, "total_gems", 0) or 0
    squad = getattr(user, "roster_count", 0) or 0
    played = getattr(user, "matches_played", 0) or 0
    won = getattr(user, "matches_won", 0) or 0
    team = getattr(user, "team_name", None)

    rows_data = [("🪙 Coins", f"{coins:,}"), ("💎 Gems", f"{gems:,}"),
                 ("👥 Squad", f"{squad} players"),
                 ("🏏 Record", f"{won} W / {played - won} L" if played else "no matches yet")]
    if streak:
        rows_data.append(("🔥 Login streak", f"{streak} day{'s' if streak != 1 else ''}"))

    def build():
        blocks = [R.heading(f"🏏 Welcome back, {name}!", 2)]
        if team:
            blocks.append(R.paragraph(["Team ", R.bold(team)]))
        blocks.append(R.table([[R.cell(k), R.cell(R.bold(v), align="right")]
                               for k, v in rows_data], compact=True, striped=True))
        if ready:
            blocks.append(R.heading("🎁 Ready for you now", 4))
            blocks.append(R.list_block([f"{lbl} — {cmd}" for lbl, cmd in ready]))
        else:
            blocks.append(R.paragraph("Everything's collected for now. Play a "
                                      "match with /vsbot or challenge a friend with /pm."))
        blocks.append(R.footer("All commands: /commands · Guide: /howto"))
        return blocks

    lines = [f"🏏 <b>Welcome back, {html.escape(name)}!</b>"]
    if team:
        lines.append(f"Team <b>{html.escape(team)}</b>")
    lines.append("")
    lines += [f"{k}: <b>{v}</b>" for k, v in rows_data]
    lines.append("")
    if ready:
        lines.append("🎁 <b>Ready for you now</b>")
        lines += [f"• {html.escape(lbl)} — {cmd}" for lbl, cmd in ready]
    else:
        lines.append("Everything's collected for now. Play a match with /vsbot "
                     "or challenge a friend with /pm.")
    lines += ["", "<i>All commands: /commands · Guide: /howto</i>"]
    html_text = "\n".join(lines) + (extra_html or "")

    kb = []
    first = [b for b in (_miniapp("📅 Daily", "daily"), _miniapp("🎴 Card Pick", "spin"))
             if b is not None]
    if first:
        kb.append(first)
    second = [b for b in (_miniapp("👥 My XI", "xi"), _miniapp("🎯 Quests", "quests"))
              if b is not None]
    if second:
        kb.append(second)
    kb.append([InlineKeyboardButton("📋 All commands", callback_data="onb_cmds")])
    return _safe_blocks(build, "status"), html_text, InlineKeyboardMarkup(kb)


# ── /commands ───────────────────────────────────────────────────────

def commands_html():
    from services.command_catalog import COMMAND_CATEGORIES
    parts = ["📋 <b>All commands</b> <i>(short aliases next to each)</i>"]
    for cat, lines in COMMAND_CATEGORIES:
        parts.append(f"\n<b>{cat}</b>")
        parts += [html.escape(l, quote=False) for l in lines]
    return "\n".join(parts)


def commands_card():
    from services.command_catalog import COMMAND_CATEGORIES

    def build():
        blocks = [R.heading("📋 All commands", 2),
                  R.paragraph("Tap a section to open it. Short aliases are "
                              "listed next to each command.")]
        for i, (cat, lines) in enumerate(COMMAND_CATEGORIES):
            blocks.append(R.details(R.bold(cat), [R.list_block(list(lines))],
                                    is_open=(i == 0)))
        blocks.append(R.footer("New here? /start shows what to do next."))
        return blocks

    return _safe_blocks(build, "commands"), commands_html(), None
