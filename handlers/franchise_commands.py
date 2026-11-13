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

_DRAFT_HELP = {
    'dpreview':('/dpreview','Draft Admin: run owner, pool, pricing, budget and squad feasibility checks.'),
    'dsearch':('/dsearch Joe Root','Search eligible players by name, version, country, role or database ID; select an exact card by button.'),
    'dhistory':('/dhistory 1','Read the durable signing, skip and administrator audit history.'),
    'dclock':('/dclock 30s | pause','Draft Admin: preview seconds/minutes and explicitly approve pause, skip, auto or admin timeout handling; 0 disables the clock.'),
    'dcorrect':('/dcorrect prices | correction reason | 96:23cr','Draft Admin: preview a reasoned paused-season correction: prices, custom, rounds, undo, pooladd, poolremove, purse, clock or resolve.'),
    'dpoolremove':('/dpoolremove #12 #34','Draft Admin: withdraw eligible pool IDs before launch; a live-season correction needs /dcorrect and a reason.'),
    'dretention':('/dretention 2','Draft Admin: preview the maximum permitted retained contracts per franchise. Default zero.'),
    'dretain':('/dretain Team | p123 | 10cr','Draft Admin: preview keeping an exact previous-season player contract. Charges the purse and occupies a squad slot.'),
    'dunretain':('/dunretain Team | #12','Draft Admin: preview a full retained-contract refund before draft launch.'),
    'draft': ('/draft', 'Show the current draft turn, squads and purses.'),
    'dboard': ('/dboard', 'Show the franchise draft board.'),
    'dnew': ('/dnew Season 2', 'Manager: create a new franchise draft season.'),
    'dsetup': ('/dsetup 15', 'Convert an empty auction to a draft. Requires both Draft Admin and Auction Manager authority; bot admins have both.'),
    'dprices': ('/dprices\n96:23cr\n95:21cr', 'Manager: set the fixed price for each player rating.'),
    'dcustom': ('/dcustom\nJoe Root:25.5cr', 'Manager: override a player’s rating price.'),
    'dorder': ('/dorder Team 1 | Team 2 | Team 3', 'Manager: set round 1 order; random generates it. Later rounds rotate the first team to last.'),
    'drounds': ('/drounds 15', 'Manager: set the round count and maximum squad size.'),
    'daddrounds': ('/daddrounds 2', 'Manager: add more rounds before squads are published.'),
    'dstart': ('/dstart', 'Manager: validate setup and start the draft.'),
    'dpause': ('/dpause', 'Manager: pause the draft and lock picks.'),
    'dresume': ('/dresume', 'Manager: resume a paused draft.'),
    'dundo': ('/dundo Wrong player version selected', 'Manager: while paused, preview undo of the latest turn with a recorded reason and refund.'),
    'dpick': ('/dpick Joe Root', 'Current team owner or co-owner: review and confirm a player signing.'),
    'dpool': ('/dpool 1', 'Browse available draft players and their fixed prices.'),
    'dsquad': ('/dsquad Team name', 'Show a drafted squad, remaining purse and rule progress.'),
    'drules': ('/drules', 'Show draft role and rating rules. Overseas players have no limit.'),
    'dhelp': ('/dhelp', 'Read the complete franchise draft setup and picking guide.'),
    'dteam': ('/dteam Team name | owner ID | co-owner IDs', 'Manager: add a new franchise during draft setup, with its own purse and full pick allowance.'),
    'dskip': ('/dskip', 'Current team owner or co-owner: defer this pick to the catch-up round without spending purse.'),
    'dskips': ('/dskips 1', 'View catch-up picks in the exact order teams skipped or timed out.'),
    'dtimer': ('/dtimer 10', 'Manager: set minutes per turn while in setup or paused; 0 disables the timer. Keeps the approved timeout policy; /dclock chooses it.'),
    'dadmin': ('/dadmin', 'Open the Draft Admin control panel and setup buttons.'),
    'dadminadd': ('/dadminadd 123456 Name', 'Bot Owner/Admin: appoint a Draft Admin. Name is optional.'),
    'dadminremove': ('/dadminremove 123456', 'Bot Owner/Admin: revoke an appointed Draft Admin immediately.'),
    'dadmins': ('/dadmins', 'Draft Admin: list current appointments. Bot Owners/Admins also have draft authority.'),
    'draftcommands': ('/draftcommands', 'Show the full draft command reference together, including setup and examples.'),
    'dcommands': ('/dcommands', 'Alias for /draftcommands; sends the complete command reference.'),
    'dcmds': ('/dcmds', 'Alias for /draftcommands; sends the complete command reference.'),
    'dbind': ('/dbind draftID', 'Draft Admin: bind a draft to this group during setup or while paused.'),
    'dpublish': ('/dpublish', 'Draft Admin: publish completed squads after scheduled and catch-up picks finish.'),
    'dpurse': ('/dpurse or /dpurse Team | 120cr', 'View purses. Draft Admins can set a team’s balance during setup or while paused.'),
    'downer': ('/downer Team | 123456', 'Draft Admin: assign the team owner during setup or while paused.'),
    'dco': ('/dco Team | 123456,234567', 'Draft Admin: set co-owner IDs during setup or while paused; clear removes co-owners.'),
    'dpooladd': ('/dpooladd 80-96 | BAT | Base', 'Draft Admin: preview catalogue players by rating, category and version, or use ids 12,34,56.'),
    'drolemin': ('/drolemin BAT=4,BOWL=4,AR=2,WK=1', 'Draft Admin: set role minimums; clear removes them. Draft must be in setup or paused.'),
    'drolemax': ('/drolemax BAT=8,BOWL=8,AR=6,WK=3', 'Draft Admin: set role maximums; clear removes them. Draft must be in setup or paused.'),
    'dratingrule': ('/dratingrule 83=4', 'Draft Admin: require at least 4 players rated 83 or below; clear removes rating rules.'),
    'dcancel': ('/dcancel Exact draft name', 'Draft Admin: cancel a draft with its exact name. Squads and purse history remain saved.'),
}
_DRAFT_HELP = {name:(usage,description.replace('Manager:', 'Draft Admin:'))
               for name,(usage,description) in _DRAFT_HELP.items()}


def draft_command_menu():
    return [(name,description[:256]) for name,(_,description) in _DRAFT_HELP.items()]


def draft_commands_html():
    """One complete reference, sent in consecutive rich messages if needed."""
    groups = (
        ('🛡 Appointments & help', ('dadmin','dadminadd','dadminremove','dadmins','draftcommands','dcommands','dcmds','dhelp')),
        ('🏗 Setup & teams', ('dnew','dsetup','dbind','dteam','downer','dco','dpooladd','dpurse')),
        ('💰 Prices, order & squad rules', ('dprices','dcustom','dorder','drounds','daddrounds','drolemin','drolemax','dratingrule')),
        ('⏱ Live draft & recovery', ('dtimer','dstart','dpause','dresume','dpick','dskip','dundo','dcancel','dpublish')),
        ('👀 Views', ('draft','dboard','dpool','dsquad','dskips','drules')),
        ('🛡 Draft 2.0 previews & contracts',('dpreview','dsearch','dhistory','dclock','dcorrect','dpoolremove','dretention','dretain','dunretain')),
    )
    lines=['🏏 <b>All Franchise Draft Commands</b>',
           'Draft Admins manage drafts. Auction Managers manage auctions. Bot Owners/Admins have both permissions.',
           'Only the current team’s owner or co-owner can pick or skip. Each button rechecks current authority.']
    for title,names in groups:
        lines+=['', '<b>'+title+'</b>']
        for name in names:
            usage,description=_DRAFT_HELP[name]
            lines+=['<code>'+html.escape(usage)+'</code>\n'+html.escape(description)]
    lines+=['', '<b>Shared setup tools</b>',
            '<code>/auction</code> — choose Draft → guided setup, next season, teams and purse.',
            '<code>/importplayers</code> — import player CSV in the bound group; <code>/sampleplayers</code> downloads the format.',
            '<code>/settings</code> — guided rule settings; <code>/roles</code> — current roles; <code>/id</code> — Telegram IDs; <code>/cancel</code> — stop a form.',
            '', '<b>Quick start</b>',
            'Appoint a Draft Admin → /dnew → /dteam → /importplayers or /dpooladd → /dprices → /dorder → /dtimer → /dstart.',
            'Prices: <code>96:23cr</code> and <code>Joe Root:25.5cr</code>. Role/rating edits replace their current table; use clear to remove it.',
            'Skipped picks play after scheduled rounds in skip order. /dclock chooses timeout handling explicitly. Pause freezes remaining time. No overseas limit.',
            'Prices, rounds, budgets, pool edits, retention, start and undo open expiring administrator previews. Confirm to apply; Cancel changes nothing.',
            'After launch, normal prices are locked. Pause and use /dcorrect with a reason for an audited correction. /dcustom remove #12 restores the rating price.']
    return '\n'.join(lines)


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
        if module=='draft':
            for name in names:
                usage,description=_DRAFT_HELP[name]
                result.append(dict(names=(name,),section='📋 Franchise Draft',usage=usage,description=description))
            continue
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
    draft_query=str(query).strip().lstrip('/').casefold() in ('draft',*_DRAFT_HELP)
    pages=max(1,(len(rows)+PAGE_SIZE-1)//PAGE_SIZE)
    page=max(1,min(int(page),pages))
    text=['<b>📋 Franchise Draft command guide</b>' if draft_query else '<b>📋 Franchise Auction command guide</b>',
          'Teams → prices → rotating rounds → timed picks → publish squads' if draft_query else 'Retention → picks → bidding & RTM → trades → publish squads',
          f'Page {page}/{pages} · {len(rows)} command groups',
          'Search: <code>/commands retention</code>, <code>/commands trade</code> or any command name.']
    if query:text+=['Search: <b>'+html.escape(str(query)[:100])+'</b>']
    for row in rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE]:
        aliases=' · '.join('/'+name for name in row['names'])
        text+=['','<b>'+html.escape(aliases)+'</b> · '+html.escape(row['section']),
               '<code>'+html.escape(row['usage'][:180])+'</code>',html.escape(row['description'][:220])]
    if not rows:text+=['','No matching commands. Use /commands to see the full list.']
    text+=['','Draft Admins control setup and publishing. Only the current team’s owner/co-owner may pick or skip.' if draft_query
           else 'Managers control rules and publishing. Team roles bid and answer RTM for their franchises.',
           'No overseas limit. /draftcommands sends every draft command together.' if draft_query
           else 'Native guided money uses whole lakh: 100 = 1 Cr. /bid 1.5 means 1.5 Cr.']
    nav=[]
    if page>1:nav.append(command_button(actor,page-1,'← Previous'))
    if page<pages:nav.append(command_button(actor,page+1,'Next →'))
    buttons=[nav] if nav else []
    buttons.append([InlineKeyboardButton('🏠 Draft workspace' if draft_query else '🏠 Auction workspace',callback_data=f'fa:{int(actor)}:0:v:home')])
    return '\n'.join(text),InlineKeyboardMarkup(buttons)


async def commands_handler(update,context):
    actor=update.effective_user.id
    query=' '.join(context.args or [])[:100]
    key=f'{SEARCH_KEY}:{update.effective_chat.id}'
    context.user_data[key]=query
    text,markup=render_commands(actor,query=query)
    from services import rich_message as R
    from services.franchise_draft_rich import blocks_for_html
    await R.reply_rich(update.effective_message,blocks_for_html(text),text,reply_markup=markup)


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
    from services import rich_message as R
    from services.franchise_draft_rich import blocks_for_html
    await R.edit_rich(query,blocks_for_html(text),text,reply_markup=markup)


def register(application):
    application.add_handler(CommandHandler(['commands','cmds'],commands_handler))
    application.add_handler(CallbackQueryHandler(commands_callback,pattern=r'^fc:'))
