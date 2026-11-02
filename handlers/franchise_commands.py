"""Searchable command reference for the dedicated bot, including every alias."""
from __future__ import annotations

import html
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

from services.franchise_bot_registry import COMMANDS

PAGE_SIZE = 7
SEARCH_KEY = 'franchise_command_search'
NATIVE_COMMANDS = (
    (('auction', 'start', 'menu'), 'Open the auction workspace and your current roles.'),
    (('auctions',), 'Choose an existing auction from the shared database.'),
    (('newauction',), 'Create an auction with guided setup.'),
    (('bind',), 'Bind an auction to its Telegram group.'),
    (('settings',), 'Manage every auction rule in the private controls.'),
    (('roles',), 'Show your current global and franchise roles.'),
    (('id',), 'Show Telegram user and group IDs; reply to identify another user.'),
    (('help',), 'Read the retention, picks, bidding and publishing walkthrough.'),
    (('sampleplayers',), 'Download the example player CSV.'),
    (('importplayers',), 'Import player CSV in the bound auction group.'),
    (('cancel',), 'Cancel an unfinished guided input.'),
    (('commands', 'cmds'), 'Search all commands and aliases: /commands retention or /commands trade.'),
)

_TRADE_HELP = {
    'atrade': 'Propose a player and cash trade with another franchise.',
    'atrades': 'Show the trade log and open offers.',
    'atradecash': 'Set the cash component of your current offer.',
    'atradecancel': 'Cancel your offer or decline one made to you.',
    'atradeblock': 'List players available for trading and manage your trade block.',
}


def command_catalogue(query=''):
    # Use the same reference text as /adminhelp. The registry, rather than a
    # manually shortened menu, decides which command names and aliases appear.
    from services import auction_rich as AR
    reference={}
    for section, rows in (*AR.ADMIN_SECTIONS, AR.PLAYER_SECTION, AR.BOT_ADMIN_SECTION):
        for usage, description in rows:
            for name in re.findall(r'/([a-z][a-z0-9_]*)', usage):
                reference.setdefault(name,(section,usage,description))
    result=[]
    for module, names, handler in COMMANDS:
        section,usage,description=next((reference[name] for name in names if name in reference),
            ('🤝 Trades' if module=='trade' else '🔨 Auction commands','/'+names[0],
             _TRADE_HELP.get(names[0],'Open /adminhelp for usage, or /atradehelp for trade examples.')))
        result.append(dict(names=names,section=section,usage=usage,description=description))
    for names,description in NATIVE_COMMANDS:
        result.append(dict(names=names,section='🏠 Workspace',usage='/'+names[0],description=description))
    result.sort(key=lambda row:row['names'][0])
    query=str(query).strip().lstrip('/').casefold()[:100]
    if query:
        result=[row for row in result if query in (' '.join(row['names'])+' '+row['section']+' '+row['usage']+' '+row['description']).casefold()]
    return result


def command_button(actor, page=1, label='📋 Command guide'):
    return InlineKeyboardButton(label,callback_data=f'fc:{int(actor)}:{int(page)}')


def render_commands(actor, page=1, query=''):
    rows=command_catalogue(query)
    pages=max(1,(len(rows)+PAGE_SIZE-1)//PAGE_SIZE)
    page=max(1,min(int(page),pages))
    text=['<b>📋 Franchise Auction command guide</b>',
          'Retention → picks → bidding & RTM → trades → publish squads',
          f'Page {page}/{pages} · {len(rows)} command groups',
          'Search: <code>/commands retention</code>, <code>/commands trade</code> or any command name.']
    if query:text+=['Search: <b>'+html.escape(str(query)[:100])+'</b>']
    for row in rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE]:
        aliases=' · '.join('/'+name for name in row['names'])
        text+=['','<b>'+html.escape(aliases)+'</b> · '+html.escape(row['section']),
               '<code>'+html.escape(row['usage'][:180])+'</code>',html.escape(row['description'][:220])]
    if not rows:text+=['','No matching commands. Use /commands to see the full list.']
    text+=['','Managers control rules and publishing. Team roles bid and answer RTM for their franchises.',
           'Native guided money uses whole lakh: 100 = 1 Cr. /bid 1.5 means 1.5 Cr.']
    nav=[]
    if page>1:nav.append(command_button(actor,page-1,'← Previous'))
    if page<pages:nav.append(command_button(actor,page+1,'Next →'))
    buttons=[nav] if nav else []
    buttons.append([InlineKeyboardButton('🏠 Auction workspace',callback_data=f'fa:{int(actor)}:0:v:home')])
    return '\n'.join(text),InlineKeyboardMarkup(buttons)


async def commands_handler(update,context):
    actor=update.effective_user.id
    query=' '.join(context.args or [])[:100]
    key=f'{SEARCH_KEY}:{update.effective_chat.id}'
    context.user_data[key]=query
    text,markup=render_commands(actor,query=query)
    await update.effective_message.reply_text(text,parse_mode='HTML',reply_markup=markup)


async def commands_callback(update,context):
    query=update.callback_query
    try:
        _,actor,page=query.data.split(':')
        if int(actor)!=query.from_user.id:
            await query.answer('Use /commands to open your own command guide.',show_alert=True)
            return
        page=int(page)
    except (ValueError,TypeError):
        await query.answer('Open /commands for a fresh guide.',show_alert=True)
        return
    await query.answer()
    search=context.user_data.get(f'{SEARCH_KEY}:{update.effective_chat.id}','')
    text,markup=render_commands(int(actor),page,search)
    from telegram.error import BadRequest
    try:
        await query.edit_message_text(text,parse_mode='HTML',reply_markup=markup)
    except BadRequest as exc:
        if 'not modified' not in str(exc).lower():raise


def register(application):
    application.add_handler(CommandHandler(['commands','cmds'],commands_handler))
    application.add_handler(CallbackQueryHandler(commands_callback,pattern=r'^fc:'))
