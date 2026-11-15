"""Match tools inside one collapsed section; selection buttons stay outside."""

from html import escape

from services import rich_message as R
from services.rich_layout import lines_from_html, plain

TITLE = "⚙️ Manage match"
_HTML_TITLE = f"<b>{TITLE}</b>"


def attach(state, text, blocks, rows):
    """Reuse the current authorized controls, with commands for regular HTML."""
    tools, legacy, seen = [], [], set()
    for row in rows or []:
        for button in row:
            callback = button.callback_data
            url = button.url
            if button.web_app:
                # A t.me launch link keeps Telegram's Mini App authentication
                # and the exact match/chat route when the button moves in-text.
                from services.match_broadcast import play_match_url
                url = play_match_url(state.get("match_id"), state.get("chat_id")) or button.web_app.url
            key = callback or url
            if not key or key in seen:
                continue
            seen.add(key)
            if url:
                tools.append(R.paragraph(R.link(button.text, url)))
                legacy.append(f'<a href="{escape(url, quote=True)}">{escape(button.text)}</a>')
            elif callback:
                tools.append(R.paragraph(R.callback_button(button.text, callback)))
                command = ("/impact" if callback.startswith(("cipl_imp_", "impact_")) else
                           "/managebatting" if callback.startswith("lb_n_") else None)
                legacy.append(f"{escape(button.text)} — {command}" if command else escape(button.text))
    marker = f"\n\n<blockquote expandable>{_HTML_TITLE}\n"
    text = text.split(marker, 1)[0]
    base = ([block for block in blocks if not (block.get("type") == "details" and block.get("summary") == TITLE)]
            if blocks is not None else None)
    if not tools:
        return base, text
    if base is None:
        base = [R.paragraph(line) for line in lines_from_html(text) if plain(line).strip()]
    # Retried sends/edits may already carry the same section. Refresh it rather
    # than accumulating duplicate controls or retaining a stale captain's tools.
    base.append(R.details(TITLE, tools))
    text += marker + "\n".join(legacy) + "</blockquote>"
    return base, text
