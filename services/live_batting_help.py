"""Match-start instructions, displayed as code rather than command links."""
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
