"""Daily reward receipts show granted rewards, roster placement and readiness."""

import html
import math
from datetime import datetime, timezone

from config import MAX_ROSTER, STREAK_MILESTONE
from services import rich_message as R
from services.cooldown_service import format_remaining


def receipt(user, reward, next_claim_at):
    blocks, text = [R.heading("📅 DAILY REWARD CLAIMED", 2)], ["📅 <b>DAILY REWARD CLAIMED</b>"]

    def facts(title, rows):
        blocks.extend([R.heading(title), R.table([[R.cell(label), R.cell(value)]
                                                 for label, value in rows], compact=True)])
        text.extend(["", f"<b>{title}</b>"] + [f"{label}: {html.escape(value)}" for label, value in rows])

    rows = []
    for key, label in (("coins", "🪙 Coins received"), ("gems", "💎 Gems received")):
        total = reward[key]
        bonus = reward.get(f"bonus_{key}", 0)
        value = f"+{total:,}"
        if bonus:
            value += f" ({total - bonus:,} base + {bonus:,} streak bonus)"
        rows.append((label, value))
    facts("🎁 Rewards received", rows)
    facts("💰 Updated balance", [("🪙 Coins", f"{user.total_coins:,}"),
                                 ("💎 Gems", f"{user.total_gems or 0:,}"),
                                 ("Roster", f"{user.roster_count}/{MAX_ROSTER}")])
    streak = reward["streak"]
    if reward["milestone"]:
        note = f"🏆 {STREAK_MILESTONE}/{STREAK_MILESTONE} streak completed! Next bonus in {STREAK_MILESTONE} days."
    else:
        note = f"🔥 Streak: {streak}/{STREAK_MILESTONE} · Next bonus in {STREAK_MILESTONE - streak} days."
    text.extend(["", note])
    blocks.append(R.paragraph(note))
    milestone_player = reward.get("milestone_player")
    if milestone_player:
        note = f"🏆 Milestone bonus card: {milestone_player.name} · {milestone_player.rating} OVR"
        text.append(html.escape(note))
        blocks.append(R.paragraph(note))
    players = [(p, "✅ Added to squad") for p in reward["players_added"]]
    players.extend((p, "⚠️ Squad full · choose Release or Replace below") for p in reward["players_pending"])
    if players:
        text.extend(["", "<b>🏏 Player cards</b>"])
        rows = [[R.cell("Player", header=True), R.cell("OVR", header=True), R.cell("Status", header=True)]]
        for player, status in players:
            version = getattr(player, "version", None)
            name = str(player.name) + (f" · {version}" if version else "")
            text.append(f"{html.escape(name)} · <b>{player.rating} OVR</b> · {status}")
            rows.append([R.cell(name), R.cell(str(player.rating)), R.cell(status)])
        blocks.extend([R.heading("🏏 Player cards"), R.table(rows, compact=True)])
    if next_claim_at.tzinfo is None:
        next_claim_at = next_claim_at.replace(tzinfo=timezone.utc)
    seconds = max(0, math.ceil((next_claim_at - datetime.now(timezone.utc)).total_seconds()))
    readiness = "Ready now" if not seconds else f"Ready in {format_remaining(seconds)}"
    text.extend(["", f"⏰ Next daily claim: {readiness}", "Use /daily when your next claim is ready."])
    blocks.extend([R.paragraph(["⏰ Next daily claim: ", R.date_time(readiness, next_claim_at.timestamp())]),
                   R.footer("Use /daily when your next claim is ready.")])
    return blocks, "\n".join(text)
