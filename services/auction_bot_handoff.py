"""Move the auction Telegram surface to a second bot on the same database.

The original bot no longer registers auction commands, callbacks or clocks.
This guard redirects historical entry points without changing auction rows.
"""
import os
import re


async def auction_handoff_guard(update, context):
    if getattr(context.application, 'bot_data', {}).get('franchise_dedicated'):
        return
    from services.command_token import command_name, command_token_from_text, is_command_for_this_bot
    from services.franchise_bot_registry import COMMANDS, DOT_ALIASES
    from telegram.ext import ApplicationHandlerStop

    query = getattr(update, 'callback_query', None)
    message = getattr(update, 'effective_message', None)
    token = ''
    draft_command = False
    if query:
        data = getattr(query, 'data', '') or ''
        if not data.startswith(('au_', 'fd_')):
            return
        draft_command = data.startswith('fd_')
    else:
        text = (getattr(message, 'text', None) or getattr(message, 'caption', None) or '').strip()
        token = command_token_from_text(text)
        if token:
            if not is_command_for_this_bot(token, getattr(context.bot, 'username', None)):
                return
            commands = {'auction', *(name for _, names, _ in COMMANDS for name in names)}
            if command_name(token) not in commands:
                return
            draft_command = command_name(token) in {name for module, names, _ in COMMANDS if module == 'draft' for name in names}
        elif text.startswith('.'):
            parts = text[1:].split(maxsplit=1)
            if not parts or parts[0].casefold() not in DOT_ALIASES:
                return
        else:
            return

    username = os.getenv('FRANCHISE_AUCTION_BOT_USERNAME', '').strip().lstrip('@')
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
        username = ''
    destination = f'@{username}' if username else 'the dedicated Franchise Auction & Draft bot'
    mode, command = ('Franchise Draft', '/draft') if draft_command else ('Franchise Auction', '/auction')
    notice = f'{mode} is available in {destination}. Open {command} there; existing squads and season records are preserved.'
    try:
        if query:
            await query.answer(f'Use {destination} for {mode}. Open {command} there.', show_alert=True)
        elif getattr(getattr(update, 'effective_chat', None), 'type', None) == 'private' or '@' in token:
            await message.reply_text(notice)
        # Bare group bids and dot shortcuts are handled by the new bot without
        # an extra response from the old bot for every bid.
    finally:
        raise ApplicationHandlerStop


def register_main_auction_handoff(application):
    from telegram import Update
    from telegram.ext import TypeHandler
    application.add_handler(TypeHandler(Update, auction_handoff_guard), group=-70)
