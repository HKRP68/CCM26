"""A clear first-innings result and chase equation, shared by match modes."""

from html import escape

from services import rich_message as R
from services.rich_layout import lines_from_html, plain


def build(*, first_team, chasing_team, runs, wickets, progress, balls,
          next_action, hundred=False, scorecard=""):
    target = runs + 1
    score = f"{runs}/{wickets}"
    available = f"{balls} balls" if hundred else f"{balls // 6} overs · {balls} balls"
    rate = (f"{target / balls:.2f} runs/ball" if hundred
            else f"{target * 6 / balls:.2f} runs/over")
    equation = f"{chasing_team} need {target} to win"
    blocks = [
        R.heading("⏸ INNINGS BREAK", 2),
        R.paragraph("First innings complete"),
        R.table([[R.cell(first_team), R.cell(R.bold(score)), R.cell(progress)]], compact=True),
        R.pullquote(f"🎯 {equation}"),
        R.table([[R.cell("Balls available"), R.cell(available)],
                 [R.cell("Required rate"), R.cell(rate)]], compact=True),
        R.paragraph(next_action),
    ]
    text = (f"⏸ <b>INNINGS BREAK</b>\n\n"
            f"🏏 <b>{escape(str(first_team))} {score}</b> · {escape(progress)}\n"
            f"<i>First innings complete</i>\n\n"
            f"🎯 <b>{escape(str(chasing_team))} need {target} to win</b>\n"
            f"🏁 Chase: {available}\n"
            f"📈 Required rate: <b>{rate}</b>\n\n{escape(next_action)}")
    if scorecard:
        blocks.append(R.details("📋 First-innings scorecard", [R.paragraph(line)
            for line in lines_from_html(scorecard) if plain(line).strip()]))
        text += f"\n\n<blockquote expandable>{scorecard}</blockquote>"
    return blocks, text
