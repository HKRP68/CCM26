"""Complete auction-only Telegram registration, shared by the dedicated runtime.

The live buttons remain shared; personal command cards retain owner checks.
No collecting-game handlers or website application are imported here.
"""

from importlib import import_module

# The retired bot recognises these old shortcuts without loading auction
# handlers. The dedicated bot's live mapping is verified against this list.
DOT_ALIASES = frozenset({
    'bid','bd','b','purse','bal','balance','apurse','squad','asquad','mysquad',
    'board','status','aboard','rtm','artm','info','menu','ainfo','rules','arules',
    'lb','leaderboard','mybids','bids','sold','unsold','next','trade','trades',
    'tradeblock','tradehelp',
})

# module, command aliases, callable name. Kept explicit so the supported bot
# surface can be reviewed without importing the main cricket application.
COMMANDS = (
    ('draft', ('dcall', 'dcallteams'), 'dcall_handler'),
    ('draft', ('dpreview','dsearch','dhistory','dclock','dcorrect','dpoolremove','dretention','dretain','dunretain','dimportteams','dapick','daskip','draftpause','dremoveteam'), 'command_handler'),
    ('draft', ('draft', 'dboard', 'dnew', 'dsetup', 'dprices', 'dcustom',
               'dorder', 'drounds', 'daddrounds', 'dstart', 'dpause', 'dresume', 'dfinish',
               'dundo', 'dpick', 'dpool', 'dsquad', 'drules', 'dhelp',
               'dteam', 'dskip', 'dskips', 'dtimer'), 'command_handler'),
    ('draft', ('dadmin','dadminadd','dadminremove','dadmins','draftcommands','dcommands','dcmds',
               'dbind','dpublish','dpurse','downer','dco','dpooladd','drolemin','drolemax','dratingrule','dcancel'), 'command_handler'),
    ('auction', ('bid', 'bd'), 'bid_handler'),
    ('auction', ('artm', 'rtm'), 'artm_handler'),
    ('auction', ('aboard', 'auctionboard'), 'aboard_handler'),
    ('auction', ('apurse', 'apurses'), 'apurse_handler'),
    ('auction', ('ainfo', 'amenu'), 'ainfo_handler'),
    ('auction', ('arules', 'asettings'), 'arules_handler'),
    ('auction', ('asets',), 'asets_handler'),
    ('auction', ('anextset',), 'anextset_handler'),
    ('auction', ('anextplayer', 'anextplayers'), 'anextplayer_handler'),
    ('auction', ('asquad', 'amysquad'), 'asquad_handler'),
    ('auction', ('ateamsummary', 'asummary'), 'ateamsummary_handler'),
    ('auction', ('asoldlist',), 'asoldlist_handler'),
    ('auction', ('aunsoldlist',), 'aunsoldlist_handler'),
    ('auction', ('aleaderboard', 'alb'), 'aleaderboard_handler'),
    ('auction', ('amybids', 'amybidhistory'), 'amybids_handler'),
    ('auction', ('adminhelp', 'ahelp'), 'aadmin_handler'),
    ('auction', ('anew',), 'anew_handler'),
    ('auction', ('abind',), 'abind_handler'),
    ('auction', ('astart',), 'astart_handler'),
    ('auction', ('apause',), 'apause_handler'),
    ('auction', ('aresume', 'aunpause'), 'astart_handler'),
    ('auction', ('anext',), 'anext_handler'),
    ('auction', ('aextend',), 'aextend_handler'),
    ('auction', ('asold',), 'asold_handler'),
    ('auction', ('aunsold',), 'aunsold_handler'),
    ('auction', ('aundobid',), 'aundobid_handler'),
    ('auction', ('awithdraw',), 'awithdraw_handler'),
    ('auction', ('areinstate', 'aunwithdraw'), 'areinstate_handler'),
    ('auction', ('aforce', 'aforcenext'), 'aforce_handler'),
    ('auction', ('arecall', 'aresell'), 'arecall_handler'),
    ('auction', ('aincrement', 'aincrements'), 'aincrement_handler'),
    ('auction', ('atimer',), 'atimer_handler'),
    ('auction', ('acountdown',), 'acountdown_handler'),
    ('auction', ('arestart',), 'arestart_handler'),
    ('auction', ('abidgap',), 'abidgap_handler'),
    ('auction', ('asnipe',), 'asnipe_handler'),
    ('auction', ('afocus', 'afocusmode'), 'afocus_handler'),
    ('auction', ('adirect', 'adirectbids'), 'adirect_handler'),
    ('auction', ('agrant',), 'agrant_handler'),
    ('auction', ('aco',), 'aco_handler'),
    ('auction', ('apublish',), 'apublish_handler'),
    ('auction', ('acancel',), 'acancel_handler'),
    ('auction', ('aretain',), 'aretain_handler'),
    ('auction', ('aretainforce',), 'aretainforce_handler'),
    ('auction', ('retain',), 'retain_handler'),
    ('auction', ('retainlock',), 'retainlock_handler'),
    ('auction', ('aready',), 'aready_handler'),
    ('auction', ('apickclock',), 'apickclock_handler'),
    ('auction', ('ashortlist',), 'ashortlist_handler'),
    ('auction', ('aeligible',), 'aeligible_handler'),
    ('auction', ('aretranges', 'apickranges'), 'aranges_handler'),
    ('auction', ('apickorder',), 'apickorder_handler'),
    ('auction', ('aretlimits',), 'aretlimits_handler'),
    ('auction', ('aretmode',), 'aretmode_handler'),
    ('auction', ('aretslot', 'aretslots'), 'aretslot_handler'),
    ('auction', ('aretrule', 'aretrules'), 'aretrule_handler'),
    ('auction', ('aretdemand',), 'aretdemand_handler'),
    ('auction', ('aprevious', 'alastseason'), 'aprevious_handler'),
    ('auction', ('aprevteam',), 'aprevteam_handler'),
    ('auction', ('asetsexport',), 'asetsexport_handler'),
    ('auction', ('asetsimport',), 'asetsimport_handler'),
    ('auction', ('aoffers',), 'aoffers_handler'),
    ('auction', ('aretcancel',), 'aretcancel_handler'),
    ('auction', ('aunretain', 'arelease'), 'aunretain_handler'),
    ('auction', ('aretlock', 'aretention'), 'aretlock_handler'),
    ('auction', ('aretained', 'allretained'), 'aretained_handler'),
    ('auction', ('aratingrule', 'aratingrules'), 'aratingrule_handler'),
    ('auction', ('artmset', 'artmrules'), 'artmset_handler'),
    ('auction', ('artmcards',), 'artmcards_handler'),
    ('auction', ('artmforce',), 'artmforce_handler'),
    ('auction', ('artmundo',), 'artmundo_handler'),
    ('auction', ('aaccel', 'arelistall'), 'aaccel_handler'),
    ('auction', ('afinish', 'aend', 'askip'), 'afinish_handler'),
    ('auction', ('aclone', 'anextseason'), 'aclone_handler'),
    ('auction', ('apick',), 'apick_handler'),
    ('auction', ('apicks', 'apickboard'), 'apicks_handler'),
    ('auction', ('apickset', 'apickrules'), 'apickset_handler'),
    ('auction', ('apickskip', 'apickpass'), 'apickskip_handler'),
    ('auction', ('apickundo',), 'apickundo_handler'),
    ('auction', ('apool',), 'apool_handler'),
    ('auction', ('aaddpl',), 'aaddpl_handler'),
    ('auction', ('aautosets', 'aautoset'), 'aautosets_handler'),
    ('auction', ('asetorder',), 'asetorder_handler'),
    ('auction', ('aaccelmode',), 'aaccelmode_handler'),
    ('auction', ('acall', 'acallteams'), 'acall_handler'),
    ('auction', ('aremoveteam',), 'aremoveteam_handler'),
    ('auction', ('aadminadd',), 'aadminadd_handler'),
    ('auction', ('aadminremove',), 'aadminremove_handler'),
    ('auction', ('aadmins',), 'aadmins_handler'),
    ('trade', ('atrade',), 'atrade_handler'),
    ('trade', ('atrades', 'atradelog'), 'atrades_handler'),
    ('trade', ('atradehelp', 'tradehelp'), 'atradehelp_handler'),
    ('trade', ('atradecash',), 'atradecash_handler'),
    ('trade', ('atradecancel',), 'atradecancel_handler'),
    ('trade', ('atradeblock', 'atblock'), 'atradeblock_handler'),
    ('trade', ('atraderules',), 'atraderules_handler'),
    ('trade', ('atradewindow',), 'atradewindow_handler'),
    ('trade', ('atradedeadline',), 'atradedeadline_handler'),
    ('trade', ('atradeapprove',), 'atradeapprove_handler'),
    ('trade', ('atradeveto',), 'atradeveto_handler'),
    ('trade', ('atradeundo',), 'atradeundo_handler'),
)

CALLBACKS = (
    ('draft', 'callback_handler', '^fd_'),
    ('auction', 'bid_callback', '^au_bid_'),
    ('auction', 'me_callback', '^au_me_'),
    ('auction', 'rtm_callback', '^au_rtm_'),
    ('auction', 'info_callback', '^au_info_'),
    ('auction', 'sets_callback', '^au_sets_'),
    ('auction', 'close_callback', '^au_x_'),
    ('auction', 'retention_offer_callback', '^au_ret_'),
    ('auction', 'retention_final_callback', '^au_rfinal_'),
    ('auction', 'retention_talk_callback', '^au_rtn_'),
    ('auction', 'retention_pick_callback', '^au_rpk_'),
    ('auction', 'release_callback', '^au_rel_'),
    ('trade', 'trade_callback', '^au_tr_'),
)


async def bind_update_owner(update, _context):
    from services.button_access import bind_button_owner_context
    bind_button_owner_context(update)


def register_auction_handlers(app, *, include_solo_league=False):
    """Register every existing franchise command without unrelated bot features."""
    from telegram import Update
    from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler, filters
    from services.button_access import button_access_guard, install_button_access_defaults

    from services.ban_guard import check_ban

    app.add_handler(TypeHandler(Update, check_ban), group=-90)
    install_button_access_defaults()
    app.add_handler(TypeHandler(Update, button_access_guard), group=-60)
    app.add_handler(TypeHandler(Update, bind_update_owner), group=-50)
    modules = {'auction':import_module('handlers.auction'),
               'trade':import_module('handlers.auction_trade'),
               'draft':import_module('handlers.franchise_draft')}
    for module, commands, name in COMMANDS:
        app.add_handler(CommandHandler(list(commands), getattr(modules[module], name)))
    for module, name, pattern in CALLBACKS:
        app.add_handler(CallbackQueryHandler(getattr(modules[module], name), pattern=pattern))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS & filters.Regex(r'^\.[A-Za-z]'),
                                   modules['auction'].dot_command_handler), group=11)
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & filters.CaptionRegex(
        r'(?i)^/acall(?:teams)?(?:@[a-z0-9_]+)?(?:\s|$)'), modules['auction'].acall_handler))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & filters.CaptionRegex(
        r'(?i)^/dcall(?:teams)?(?:@[a-z0-9_]+)?(?:\s|$)'), modules['draft'].dcall_handler))
    if include_solo_league:
        import_module('handlers.auction_league').register(app)
