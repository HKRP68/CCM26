"""Direct links to the public cricket portal; no history work in Telegram."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from services.match_broadcast import _webapp_host


async def stats_website_handler(update, context):
    host = _webapp_host()
    if not host:
        await update.effective_message.reply_text('The stats website has not been configured yet.')
        return
    await update.effective_message.reply_text(
        '🏏 CricMaster cricket portal\nLive scores, fixtures, player profiles, tournament tables and records.',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('🏏 Open cricket portal', url=f'{host}/stats')]]))
