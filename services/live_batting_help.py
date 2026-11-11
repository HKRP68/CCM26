"""Batting instructions and entry points shared by match messages."""
from telegram import InlineKeyboardButton

from services import rich_message as R


COMMANDS = (
    ("/managebatting", "Open all batting controls"),
    ("/retire", "Choose a batter to retire and who comes in"),
    ("/liveorder", "Change who bats next"),
)
TITLE = "⌨️ Live batting commands"
PROMPT = "Type a command yourself in this chat:"
TIMING = "Batting captain only · between overs or before the next delivery."
SCORES = "Retired batters keep their runs and balls and can return. No wicket is added."
SCOPE = "Changes apply to this innings only."
APP_NOTE = "Playing in the Mini App? Use Manage batting there, or type "
APP_END = " first to switch to chat controls."


def manage_batting_button(state):
    """Offer the current human batting captain a live, match-specific entry."""
    if state.get("mode") == "cipl_approach" and state.get("play_mode") == "app":
        return None
    try:
        mid = int(state.get("match_id"))
        actor = int(state.get("bat_user_tg"))
    except (TypeError, ValueError):
        return None
    if mid <= 0 or actor <= 0:
        return None
    from services.live_batting import blocked_reason, waiting_indices
    from services.match_state_store import A_PICK_BAT_APPROACH
    # Prompts render before their action pointer is saved. Check the innings
    # and interruption rules here; the callback checks the latest turn as well.
    if blocked_reason(state, A_PICK_BAT_APPROACH) or not waiting_indices(state):
        return None
    return InlineKeyboardButton("🏏 Manage batting", callback_data=f"lb_n_{mid}")


def with_manage_batting(state, keyboard):
    """Add one full-width entry below the existing match controls."""
    rows = [list(row) for row in (getattr(keyboard, "inline_keyboard", keyboard) or [])]
    button = manage_batting_button(state)
    if button and not any(item.callback_data == button.callback_data
                          for row in rows for item in row):
        rows.append([button])
    return rows or None


def inline_manage_batting(state):
    """The same callback embedded in the rich scorecard's partnership line."""
    button = manage_batting_button(state)
    return R.callback_button(button.text, button.callback_data) if button else None


def start_guide_html(*, app_only=False):
    lines = [f"<b>{TITLE}</b>", f"<i>{PROMPT}</i>"]
    lines.extend(f"<code>{command}</code> — {description}"
                 for command, description in COMMANDS)
    lines.extend(["", TIMING, SCORES, SCOPE])
    if app_only:
        lines.extend(["", APP_NOTE + "<code>/playchat</code>" + APP_END])
    return "\n".join(lines)


def start_guide_blocks(*, app_only=False):
    blocks = [R.heading(TITLE), R.paragraph(R.italic(PROMPT))]
    blocks.extend(R.paragraph([R.code(command), " — ", description])
                  for command, description in COMMANDS)
    blocks.extend([R.paragraph(TIMING), R.paragraph(SCORES), R.footer(SCOPE)])
    if app_only:
        blocks.append(R.paragraph([APP_NOTE, R.code("/playchat"), APP_END]))
    return blocks
