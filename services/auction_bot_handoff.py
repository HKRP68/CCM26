"""Move the auction Telegram surface to a second bot on the same database.

Set AUCTION_BOT_ENABLED=false on the original service before starting the
dedicated bot. Its other games keep running, while auction commands, callbacks,
focus locks and the scheduler are handed off together. No auction rows change.
"""
import os
import re


def main_auctions_enabled():
    return os.getenv('AUCTION_BOT_ENABLED', 'true').strip().lower() not in ('0', 'false', 'no', 'off')


def auctions_enabled(application):
    return bool(getattr(application, 'bot_data', {}).get('franchise_dedicated')) or main_auctions_enabled()


async def auction_handoff_guard(update, context):
    if auctions_enabled(context.application):
        return
    from services.command_token import command_name, command_token_from_text, is_command_for_this_bot
    from services.franchise_bot_registry import COMMANDS
    from telegram.ext import ApplicationHandlerStop

    query = getattr(update, 'callback_query', None)
    message = getattr(update, 'effective_message', None)
    token = ''
    if query:
        if not (getattr(query, 'data', '') or '').startswith('au_'):
            return
    else:
        text = (getattr(message, 'text', None) or getattr(message, 'caption', None) or '').strip()
        token = command_token_from_text(text)
        if token:
            if not is_command_for_this_bot(token, getattr(context.bot, 'username', None)):
                return
            commands = {'auction', *(name for _, names, _ in COMMANDS for name in names)}
            if command_name(token) not in commands:
                return
        elif text.startswith('.'):
            from handlers.auction import DOT_COMMANDS
            parts = text[1:].split(maxsplit=1)
            if not parts or parts[0].casefold() not in DOT_COMMANDS:
                return
        else:
            return

    username = os.getenv('FRANCHISE_AUCTION_BOT_USERNAME', '').strip().lstrip('@')
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
        username = ''
    destination = f'@{username}' if username else 'the dedicated Franchise Auction bot'
    notice = f'Auctions have moved to {destination}. Open /auction there; existing squads and auction records are preserved.'
    try:
        if query:
            await query.answer(f'Use {destination} for this auction. Open /auction there.', show_alert=True)
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
