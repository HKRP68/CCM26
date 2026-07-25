"""Cricket Simulator Telegram Bot — main entry point (Phase 1 + 2 + Admin)."""

import asyncio
import os
import logging
import threading
import time
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram import BotCommand, Update as _TGUpdate

from config import BOT_TOKEN
from database import init_db
from logger import setup_logging
from services.reply_context import bind_reply_context, install_reply_defaults
from services.telegram_user_service import sync_update_users
from services.button_access import (
    bind_button_owner_context,
    button_access_guard,
    install_button_access_defaults,
)

# Phase 1 handlers
from handlers.debut import debut_handler
from handlers.claim import (
    claim_handler, retain_callback,
    release_callback, replace_callback, replace_confirm_callback,
)
from handlers.gspin import gspin_handler, gspin_spin_callback
from handlers.daily import daily_handler, daily_claim_callback
from handlers.myroster import myroster_handler, roster_page_callback
from handlers.playerinfo import playerinfo_handler, player_version_callback

# Phase 2 handlers
from handlers.release import (
    releasepl_handler, releasemultiple_handler,
    release_one_callback, release_cancel_callback,
    releasemultiple_confirm_callback,
)
from handlers.trade import (
    trade_handler, trade_user1_player_callback, trade_user2_player_callback,
    trade_confirm_callback, trade_cancel_callback,
    trade_rating_callback, trade_myplayer_callback, trade_theirplayer_callback,
    trade_send_callback, trade_accept_callback, trade_reject_callback, trade_back_callback,
)

# Phase 3 handlers
from handlers.lineup import playingxi_handler, swapplayers_handler, setcaptain_handler, bench_callback, autobuild_handler
from handlers.batting_order import setbo_handler
from handlers.ximage import ximage_handler
from handlers.search import (
    searchpl_handler, searchovr_handler,
    searchpl_page_callback, searchovr_page_callback,
    search_cancel_callback, noop_callback,
)
from handlers.buy import (
    buypl_handler, buypl_confirm_callback, buypl_cancel_callback,
    player_page_callback, player_page_noop_callback,
)
from handlers.team import teamname_handler, purse_handler, stats_handler, statscl_handler
from handlers.tournament import (
    statstour_handler, tournamentstats_handler, tournamentstats_callback,
)
from handlers.leaderboard import leaderboard_handler, leaderboard_callback
from handlers.profile import myprofile_handler, myprofile_callback

# Bowl-out handlers
from handlers.bowlout import (
    pbo_handler, pbo_accept_callback, pbo_decline_callback,
    bowlout_pick_callback,
)
from handlers.catch import bal_handler, catch_handler
from handlers.challenge import (
    challenge_handler, challenge_league_handler, challenge_accept_callback,
    challenge_cancel_callback, challenge_deny_callback, challenge_coin_callback,
    challenge_toss_callback, challenge_pick_callback, challenge_team_callback, challenge_team_cancel_callback, challenge_xi_callback,
    challenge_xi_pick_callback, challenge_xi_confirm_callback, challenge_start_match_callback,
    challenge_xi_clear_callback, challenge_xi_edit_callback, challenge_xi_quickselect, challenge_change_handler,
    challenge_xi_useprev_callback,
    challenge_pitch_callback, challenge_deny_match_callback,
)
from handlers.unscramble import unscramble_handler, join_handler as unscramble_join_handler, exit_handler as unscramble_exit_handler, start_handler as unscramble_start_handler, cancel_handler as unscramble_cancel_handler, answer_callback as unscramble_answer_callback
from handlers.report import report_handler
from handlers.coins2gems import coins2gems_handler, coins2gems_callback
from handlers.grant import grant_handler
from handlers.giveaway import giveaway_join_callback
from handlers.cmushop import cmushop_handler, cmushop_callback
from handlers.undo import cmuundo_handler
from handlers.app import app_handler
from handlers.ipl16 import ipl160_handler

# Match handlers
from handlers.match import (
    playmatch_handler, wpm_handler, cric_join_callback,
    cric_cancel_lobby_callback, cric_coin_callback, cric_decision_callback,
    match_accept_callback, match_deny_callback,
    overs_text_handler, overs_button_callback, overs_custom_callback,
    toss_decision_callback,
    opener1_callback, opener2_callback, select_bowler_callback,
    variation_callback, length_callback, spinner_delivery_callback,
    shot_callback, new_over_bowler_callback, new_batsman_callback,
    endmatch_handler, endmatch_yes_callback, endmatch_no_callback,
    clearmatches_handler, removematch_handler,
    resume_handler, lastmatch_handler, recentmatches_handler, info_handler,
    testwpm_handler,
)
from handlers.h2h import h2h_handler

# Auto-simulated match handler (/sim)
from handlers.sim import sim_handler

# Trait handlers
from handlers.traits import (
    traits_handler, traitshop_handler, traitapply_handler,
    traitupgrade_handler, traitreplace_handler, removetrait_handler,
    traitbuy_callback, traitreroll_callback, traitshop_cancel_callback,
    trapply_inv_callback, trapply_pl_callback,
    trup_pt_callback, trup_inv_callback,
    trrep_pt_callback, trrep_inv_callback, trrem_pt_callback,
    trait_cancel_callback,
)

# Player market handlers
from handlers.playermarket import (
    playermarket_handler,
    playermarket_buy_callback,
    playermarket_cancel_callback,
    playermarket_select_callback,
    playermarket_back_callback,
    playermarket_noop_callback,
)

# Pack handlers
from handlers.packs import (
    buypack_handler,
    pack_view_callback,
    pack_back_callback,
    pack_close_callback,
    pack_buy_callback,
    pack_noop_callback,
    openpack_handler,
    pack_open_inventory_callback,
    pack_open_close_callback,
)

# vsbot handlers
from handlers.vsbot import (
    vsbot_handler,
    vsbot_pick_callback,
    vsbot_cancel_callback,
    vsbot_toss_callback,
    vsbot_op1_callback,
    vsbot_op2_callback,
    vsbot_selbowl_callback,
)
from handlers.wpmbot import (
    wpmbot_handler,
    wpmbot_cancel_callback,
    wpmbot_coin_callback,
    wpmbot_toss_callback,
)
from handlers.wsp import (
    wsp_handler,
    wsp_join_callback,
    wsp_cancel_callback,
    wsp_decision_callback,
)
from handlers.wspbot import (
    wspbot_handler,
    wspbot_pick_callback,
    wspbot_cancel_callback,
    wspbot_toss_callback,
)

# Quest handlers
from handlers.quests import (
    myquest_handler,
    quest_tab_callback,
    quest_claim_callback,
    quest_claimall_callback,
    quest_close_callback,
    quest_filter_callback,
    quest_page_callback,
    quest_noop_callback,
)

# Tour handlers
from handlers.tours import (
    cmtours_handler, mytours_handler,
    cmt_matches_callback, cmt_overs_callback, cmt_cancel_callback,
    tour_accept_callback, tour_decline_callback,
    mytours_play_callback, mytours_info_callback,
    mytours_stats_callback, mytours_back_callback,
)

# Challenge League Tours handlers
from handlers.cl_tour import (
    cltour_handler,
    cltset_league_callback, cltset_hostteam_callback, cltset_guestteam_callback,
    cltset_count_callback, cltset_cancel_callback,
    cltour_accept_callback, cltour_decline_callback, cltour_play_callback,
)

# Achievements handlers
from handlers.achievements import (
    achievements_handler,
    achievements_tab_callback,
    achievements_close_callback,
)

# /howto handler
from handlers.howto import (
    howto_handler,
    howto_tab_callback,
    howto_close_callback,
)

# Bot vs Bot
from handlers.botvsbot import (
    botvsbot_handler,
    bvb_pickA_callback,
    bvb_pickB_callback,
    bvb_cancel_callback,
)

# /botmatch spectator mode
from handlers.botmatch import (
    botmatch_handler,
    botmatch_pick_a_callback,
    botmatch_pick_b_callback,
    botmatch_cancel_callback,
)

# Subscription mystery box + recurring drops
from handlers.cmumysterybox import cmumysterybox_handler, mysterybox_callback
from handlers.premium_drops import cmuweekly_handler, cmuchest_handler
# New social & gambling games
from handlers.lucky7 import lucky7_handler, lucky7_callback
from handlers.powerplay import powerplay_handler
from handlers.score21 import score21_handler, score21_callback
from handlers.wordchase import (
    wordchase_handler, wordchase_end_handler,
    wordchase_end_callback, wordchase_message_handler,
)
from handlers.bluffmaster import (
    bluff_handler, bluff_accept_callback, bluff_decline_callback,
    bluff_action_callback, bluff_answer_message,
)
from handlers.molehunt import (
    mole_handler,
    mh_join_callback, mh_start_callback, mh_theme_callback,
    mh_cancel_callback, mh_vote_callback,
    molehunt_message_handler,
)
from handlers.cartel import (
    cartel_handler,
    ct_join_callback, ct_start_callback, ct_theme_callback,
    ct_cancel_callback, ct_vote_callback,
    cartel_message_handler,
)
from handlers.feedback import feedback_handler
from handlers.notifications import notifications_handler, notifications_toggle_callback
from handlers.forward_broadcast import (
    frwd_grp_handler, frwd_prvt_handler, track_album_message,
)
from handlers.tournament_access import (
    tourallow_handler, tourblock_handler, tourallowlist_handler,
)
from handlers.setcardid import setcardid_handler

# Fantasy League handlers
from handlers.fantasy import (
    fantasy_handler, myfantasy_handler,
    fantasyleaderboard_handler, fantasystats_handler,
    fantasyguide_handler, fantasy_lb_callback, fantasy_webapp_handler,
)

logger = logging.getLogger(__name__)


# Keep this list aligned with the canonical command names registered in main().
# Aliases remain supported by CommandHandler, but publishing only canonical names
# keeps Telegram's slash-command menu complete without filling it with duplicates.
# Every player-facing command registered in main() must appear here, EXCEPT:
#   • admin/owner-gated ones (/grant, /setcardid, /clearmatches, /removematch,
#     /frwd_grp, /frwd_prvt, /tourallow, /tourblock, /tourallowlist) and the
#     /testwpm diagnostic — these must not show up in everyone's slash menu;
#   • /eu and /cu, which the Unscramble lobby message already spells out for the
#     players in it.
# All of those still work as commands — they are simply not advertised.
#
# HARD LIMIT: Telegram's setMyCommands accepts at most 100 commands. Going over
# fails the whole call (leaving the menu stale), so MENU_LIMIT is enforced below
# and this tuple must stay at or under it.
MENU_LIMIT = 100   # Telegram's setMyCommands ceiling

BOT_MENU_COMMANDS = (
    ("start", "Show the welcome message and command overview"),
    ("debut", "Create your account and receive a starting squad"),
    ("claim", "Claim your hourly player and coin reward"),
    ("gspin", "Spin the reward wheel"),
    ("daily", "Claim your daily reward"),
    ("cmumysterybox", "Open your subscriber Mystery Box 🎁"),
    ("cmuweekly", "Claim your Platinum weekly card 🏆"),
    ("cmuchest", "Open your Platinum coin chests 🪙"),
    ("myroster", "View your player roster"),
    ("playerinfo", "View details for a player"),
    ("releasepl", "Release one player from your roster"),
    ("releasemultiple", "Release multiple roster players"),
    ("trade", "Trade players with another user"),
    ("playingxi", "View or manage your playing XI"),
    ("ximage", "View your Playing XI as an image"),
    ("autobuild", "Build your best available playing XI"),
    ("swapplayers", "Swap two players in your lineup"),
    ("setbo", "View or change your batting order (e.g. /sbo 2 11)"),
    ("setcaptain", "Set your team captain"),
    ("searchpl", "Search for a player by name"),
    ("searchovr", "Search players by overall rating"),
    ("buypl", "Buy a player"),
    ("teamname", "Set your team name"),
    ("purse", "Check your balance"),
    ("coins2gems", "Convert coins into gems (1000 coins = 1 gem)"),
    ("cmushop", "Browse the CMU shop 🛍️"),
    ("stats", "View player game statistics"),
    ("statscl", "View any Challenge League player's stats"),
    ("statstour", "View a player's active-tournament stats"),
    ("tournamentstats", "Tournament Top-10 stat leaderboards"),
    ("cmuleaderboard", "View the leaderboard"),
    ("myprofile", "View your profile"),
    ("playmatch", "Challenge another user to a match"),
    ("wpm", "Match lobby up to 20 overs — tag/reply to invite a player (Mini App)"),
    ("wsp", "Wicket Survival Panic — survive as long as you can"),
    ("wspbot", "Wicket Survival Panic against the bot"),
    ("sim", "Simulate a full match instantly"),
    ("endmatch", "Request to end your active match"),
    ("resume", "Resume your active match"),
    ("rcl", "Resume a stuck Challenge League (/cipl) match"),
    ("letsplay", "Challenge a user with your own roster (20 overs)"),
    ("change", "Change your XI/batting order during match setup"),
    ("botstatus", "Bot ping, uptime & status"),
    ("lastmatch", "View your last match"),
    ("recentmatches", "View your recent matches"),
    ("h2h", "Head-to-head record vs another player"),
    ("matchinfo", "View active match information"),
    ("report", "Send feedback or report an issue"),
    ("cmuundo", "Undo your latest eligible action"),
    ("app", "Open the Cricket Simulator Mini App"),
    ("ipl160", "Open the 16-0 IPL season simulator (Mini App)"),
    ("ewm", "Enable welcome messages for this chat"),
    ("dwm", "Disable welcome messages for this chat"),
    ("pbo", "Start a player bowl-out"),
    ("catch", "Catch coins using your purse"),
    ("bal", "Check your catch-game balance"),
    ("cm", "Start a two-wicket challenge match"),
    ("challengeipl", "Challenge another user to an IPL match"),
    ("challengebbl", "Challenge another user to a BBL match"),
    ("challengeint", "Challenge another user to an international match"),
    ("unscramble", "Create an Unscramble Player lobby"),
    ("ju", "Join the Unscramble lobby"),
    ("su", "Start the Unscramble game"),
    ("traits", "View your traits and inventory"),
    ("traitshop", "Browse the daily trait shop"),
    ("traitapply", "Apply a trait to a player"),
    ("traitupgrade", "Upgrade a player trait"),
    ("traitreplace", "Replace a player trait"),
    ("removetrait", "Remove a trait from a player (back to inventory)"),
    ("playermarket", "Browse the player market"),
    ("buypack", "Browse and buy card packs"),
    ("openpack", "Open a pack from your inventory"),
    ("cmtours", "Create a tournament"),
    ("mytours", "View your tournaments"),
    ("cltour", "Challenge League Tour — best-of series vs a friend"),
    ("vsbot", "Play a match against a bot"),
    ("wpmbot", "Play a bot in the Mini App"),
    ("myquest", "View and claim quest rewards"),
    ("achievements", "View your achievements"),
    ("howto", "Open the help guide"),
    ("invite", "Invite friends and view referrals"),
    ("redeem", "Redeem a reward code"),
    ("botvsbot", "Configure a bot-versus-bot match"),
    ("botmatch", "Spectate a bot-versus-bot match"),
    # New social & gambling games
    ("lucky7", "Bet on two dice summing below/above/exactly 7"),
    ("powerplay", "Crash game — bet on a multiplier before it crashes"),
    ("score21", "Play Blackjack against the dealer"),
    ("wordchase", "Host a word-guessing game (group)"),
    ("endchase", "End the running Word Chase game"),
    ("bluff", "Challenge someone to a cricket trivia bluff duel"),
    ("mole", "Start a Mole Hunt social deduction game (group)"),
    ("cartel", "Start a Cricket Cartel multi-role deduction game (group)"),
    ("feedback", "Send feedback or a bug report"),
    ("notifications", "Turn reminder notifications on or off"),
    ("fantasy", "Open the weekly fantasy cricket league"),
    ("myfantasy", "View your fantasy squad and points"),
    ("fantasyleaderboard", "Fantasy league leaderboard"),
    ("fantasystats", "Top fantasy scorers this week"),
    ("fantasyguide", "Learn fantasy rules and commands"),
)


async def register_bot_menu(application):
    """Publish every canonical bot command to Telegram's slash-command menu."""
    # Start the event-loop lag sampler here: post_init runs on the same loop
    # that will serve every update, which is exactly what we want to measure.
    try:
        from services import loop_monitor
        loop_monitor.start()
    except Exception:
        logger.exception("Could not start the event-loop lag monitor")

    published = BOT_MENU_COMMANDS
    if len(published) > MENU_LIMIT:
        # Telegram rejects the whole call over the limit, which would leave the
        # menu frozen on an old build. Publish what fits and shout about the
        # rest so the overflow gets pruned deliberately rather than silently.
        dropped = [c for c, _d in published[MENU_LIMIT:]]
        logger.error(
            "BOT_MENU_COMMANDS has %s entries but Telegram allows %s — "
            "not publishing: %s", len(published), MENU_LIMIT, ", ".join(dropped))
        published = published[:MENU_LIMIT]

    await application.bot.set_my_commands([
        BotCommand(command, description)
        for command, description in published
    ])
    logger.info("Registered %s Telegram bot-menu commands", len(published))


async def start_handler(update, context):
    # Handle deep-link payloads from group redirects: /start spin or /start daily
    # Also handle referral: /start ref<inviter_telegram_id>
    args = (context.args or []) if hasattr(context, 'args') else []
    payload = (args[0].lower() if args else "")

    # Record a daily "start" activity for User Retention stats. Best-effort and
    # throttled to once per user per day; skipped for users who haven't done
    # /debut yet (no User row). Runs off the event loop so it never delays the
    # welcome reply.
    try:
        tg_user = update.effective_user
        if tg_user:
            from services.activity_service import record_start
            await asyncio.to_thread(record_start, tg_user.id)
    except Exception:
        pass

    # ── Referral payload ──
    if payload.startswith("ref"):
        try:
            inviter_tg_id = int(payload[3:])
        except (ValueError, TypeError):
            inviter_tg_id = None

        invitee_tg_user = update.effective_user
        if inviter_tg_id and invitee_tg_user and inviter_tg_id != invitee_tg_user.id:
            # Try to create or find a User row for the invitee + record referral.
            # We don't auto-create the invitee here — we want a Real User row
            # from /debut. So if invitee has no User yet, we stash the inviter
            # in user_data and the /debut handler will record on completion.
            try:
                session = get_session()
                try:
                    from models import User
                    from services.referral_service import record_referral
                    inviter = session.query(User).filter(
                        User.telegram_id == inviter_tg_id).first()
                    invitee = session.query(User).filter(
                        User.telegram_id == invitee_tg_user.id).first()
                    if inviter and invitee:
                        # Both exist → record now
                        record_referral(session, inviter.id, invitee.id)
                        session.commit()
                    elif inviter:
                        # Invitee hasn't done /debut yet — stash inviter
                        context.user_data["pending_inviter_tg_id"] = inviter_tg_id
                finally:
                    session.close()
            except Exception:
                import logging
                logging.exception("referral start payload handling failed")
            # Continue to show welcome — let them do /debut

    if payload in ("debut", "debut_app"):
        # Deep link from a group welcome button → run the debut flow in DM.
        from handlers.debut import debut_handler
        await debut_handler(update, context)
        return

    # Open-lobby match deep link from a group: cricket_<matchId>_<chatId>
    # (the group "Play Match" button routes here). Open the same Mini App
    # board, passing match_id/chat_id on the query string the frontend reads.
    if payload.startswith("cricket_"):
        webapp_url = os.getenv("WEBAPP_URL", "").strip()
        if webapp_url and webapp_url.startswith("https://"):
            from urllib.parse import urlparse
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
            rest = args[0][len("cricket_"):]  # keep original case for ids
            last = rest.rfind("_")
            if last != -1:
                m_id, c_id = rest[:last], rest[last + 1:]
            else:
                m_id, c_id = rest, "0"
            p = urlparse(webapp_url)
            host = f"{p.scheme}://{p.netloc}"
            url = f"{host}/cricket?match_id={m_id}&chat_id={c_id}"
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎮 Open Match",
                                     web_app=WebAppInfo(url=url))
            ]])
            await update.message.reply_text(
                "🏏 Tap below to open the match board in the Mini App.",
                parse_mode="HTML", reply_markup=kb,
            )
            return

    # Live match deep link: lm_<id> → open the Mini App live-match board
    if payload.startswith("lm_") or payload.startswith("sc_"):
        webapp_url = os.getenv("WEBAPP_URL", "").strip()
        if webapp_url and webapp_url.startswith("https://"):
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
            is_sc = payload.startswith("sc_")
            label = "📋 View Scorecard" if is_sc else "🎮 Open Match"
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    label,
                    web_app=WebAppInfo(url=webapp_url + "#" + payload),
                )
            ]])
            await update.message.reply_text(
                "🏏 Tap below to open in the Mini App.",
                parse_mode="HTML", reply_markup=kb,
            )
            return

    _MINIAPP_SCREENS = {
        "spin": "🎡 Open Spin", "daily": "📅 Open Daily",
        "market": "🌟 Open Market", "xi": "👥 Open XI",
        "roster": "👥 Open Squad", "packs": "🎴 Open Packs",
        "freepack": "📦 Open Free Pack", "quests": "🎯 Open Quests",
        "achievements": "🏆 Open Achievements", "season": "👑 Open Season",
        "clubs": "🛡️ Open Clubs", "leaderboard": "🏆 Open Ranks",
        "invite": "🎟️ Open Invite",
    }
    if payload in _MINIAPP_SCREENS:
        webapp_url = os.getenv("WEBAPP_URL", "").strip()
        if webapp_url and webapp_url.startswith("https://"):
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
            label = _MINIAPP_SCREENS[payload]
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    label,
                    web_app=WebAppInfo(url=webapp_url + "#" + payload),
                )
            ]])
            await update.message.reply_text(
                f"<b>Welcome!</b> Tap below to open the {payload} screen.",
                parse_mode="HTML", reply_markup=kb,
            )
            return

    # CMU 16-0 deep link (group button + result-share "Play" button use
    # ?start=ipl160 so the game opens here in DM as a real Mini App).
    if payload == "ipl160":
        from handlers.ipl16 import send_ipl160_launch
        await send_ipl160_launch(update, context)
        return

    await update.message.reply_text(
        "🏏 <b>Welcome to Cricket Simulator Bot!</b>\n\n"
        "Use /debut (or /d) to create your account and receive your starting squad.\n\n"
        "<b>Commands</b> <i>(short aliases in brackets)</i>:\n"
        "/debut /d - Create account & get your starter XI\n"
        "/claim /c - Claim 1 player + coins (hourly)\n"
        "/daily /dl - Daily reward (24h)\n"
        "/gspin /gs - Spin the wheel (8h)\n"
        "/myroster /mr - View your roster\n"
        "/playingxi /pxi /xi - Playing XI\n"
        "/ximage /xiimg - Playing XI as an image\n"
        "/playerinfo /pi [name] - Player details\n"
        "/stats /st [name] - Player game stats\n"
        "/searchpl /sp [name] - Search player\n"
        "/searchovr /so [rating] - Search by OVR\n"
        "/buypl /buy /b [name] - Buy a player\n"
        "/swapplayers /swap [n1] [n2] - Swap positions\n"
        "/setcaptain /cap [name] - Set captain\n"
        "/teamname /tn [name] - Set team name\n"
        "/purse /p - Check balance\n"
        "/catch [bet] [height] - Risk purse coins in the catching game\n"
        "/cm @user - Two-wicket challenge mode\n"
        "/letsplay /lp @user - Reply or tag to play 20 overs with your own roster\n"
        "/challengeIPL /cipl - Reply to a user to start an IPL challenge\n"
        "/challengeBBL /cbbl - Reply to a user to start a BBL challenge\n"
        "/challengeINT /cint - Reply to a user to start an international challenge\n"
        "/unscramble - Create an Unscramble Player lobby\n"
        "/release /rel [name|pos] - Release for coins\n"
        "/releasemultiple /relm [from] [to] - Range release\n"
        "/trade /tr @user - Trade players\n"
        "/playmatch /pm @user - Play a match\n"
        "/wpm [overs] [@user] - Match lobby up to 20 overs; tag/reply to invite a player (Mini App)\n"
        "/vsbot [overs] - Play a bot opponent in chat\n"
        "/wpmbot [overs] - Play a bot opponent in the Mini App (up to 20 overs)\n"
        "/endmatch /em - End match (fine applies)\n"
        "/clearmatches - Players in the match (or a bot admin) clear stuck matches here (no winner)\n"
        "/removematch @user - Admin: remove a player stuck in a match\n"
        "/resume /r - If buttons disappear mid-match\n"
        "/rcl - Resume a stuck Challenge League (/cipl) match\n"
        "/botstatus - Bot ping, uptime & status\n"
        "/myprofile /me - Your profile\n"
        "/traits /tt - Your traits & inventory\n"
        "/traitshop /tshop - Daily trait shop\n"
        "/traitapply /tapply - Apply trait to player\n"
        "/traitupgrade /tup - Level up a trait\n"
        "/traitreplace /trep - Replace a trait\n"
        "/removetrait /rtrait - Remove a trait (back to inventory)\n"
        "/leaderboard /lb /top - Leaderboard"
        + _get_start_branding(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def _get_start_branding():
    """Fetch branding HTML snippet for the /start welcome message."""
    try:
        from services.referral_service import format_branding_html
        s = get_session()
        try:
            return format_branding_html(s)
        finally:
            s.close()
    except Exception:
        return ""


def start_admin_panel():
    """Run the Flask admin panel in a background thread."""
    try:
        from admin import app as flask_app
        port = int(os.getenv("ADMIN_PORT", os.getenv("PORT", 5000)))
        logger.info(f"Admin panel starting on port {port}...")
        flask_app.run(
            host="0.0.0.0",
            port=port,
            debug=False,
            use_reloader=False,
        )
    except Exception:
        logger.exception("Admin panel crashed")


def _send_bot_dm_blocking(chat_id, text):
    """Synchronous DM helper for use from the Flask admin panel (and other
    sync callers). Sends ``text`` as HTML to ``chat_id`` on a throwaway Bot
    instance + event loop. Returns True on success, False on any failure.
    """
    try:
        import asyncio
        from telegram import Bot
        bot_instance = Bot(token=BOT_TOKEN)

        async def _send():
            await bot_instance.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_send())
            loop.close()
            return True
        except Exception:
            logger.exception("Async send failed")
            return False
    except Exception:
        logger.exception("Bot DM send failed")
        return False


def _send_admin_reply_blocking(chat_id, text):
    """Send an admin reply to a user's /report through the bot (sync)."""
    return _send_bot_dm_blocking(
        chat_id, "📬 <b>Admin reply to your /report:</b>\n\n" + text)


def main():
    setup_logging()
    install_reply_defaults()
    install_button_access_defaults()
    logger.info("=" * 50)
    logger.info("CRICKET BOT STARTING...")
    logger.info("=" * 50)

    # Show env status
    logger.info("  BOT_TOKEN: %s", "set" if BOT_TOKEN else "NOT SET")
    logger.info("  DATABASE_URL: %s", os.getenv('DATABASE_URL', 'sqlite:///cricket_bot.db'))
    logger.info("  ADMIN_PASSWORD: %s", "set" if os.getenv('ADMIN_PASSWORD') else "using default")
    logger.info("  PORT: %s", os.getenv('PORT', os.getenv('ADMIN_PORT', '5000')))

    logger.info("Initialising database...")
    try:
        init_db()
        print("  Database: ✅ initialised")
    except Exception:
        logger.exception("Database init failed")
        print("  Database: ❌ FAILED")
        return

    # Seed players if table is empty
    try:
        from database import get_session
        from models import Player
        session = get_session()
        count = session.query(Player).count()
        session.close()
        logger.info("  Players in DB: %s", count)
        if count == 0:
            logger.info("  Seeding 3,165 players...")
            from seed_players import seed
            seed()
            session = get_session()
            count = session.query(Player).count()
            session.close()
            logger.info("  After seed: %s players", count)
    except Exception:
        logger.exception("Seed failed")
        logger.warning("  Seed FAILED (you can seed from admin panel)")

    # Check data file exists
    data_path = os.path.join(os.path.dirname(__file__), "data", "players.json")
    logger.info("  data/players.json: %s", "found" if os.path.exists(data_path) else "NOT FOUND")

    # Report the database round trip. Most handlers run several queries
    # synchronously per command, so this number is multiplied into every reply
    # the bot sends — it is usually the single largest term in "why is the bot
    # slow", and it is fixed by hosting, not by code.
    try:
        from database import measure_round_trip
        db_rtt = measure_round_trip()
        if db_rtt is None:
            logger.warning("  DB round-trip: could not measure")
        elif db_rtt >= 25:
            logger.warning(
                "  DB round-trip: %.0f ms — SLOW. The app and the database look "
                "like they are in different regions; every query a command runs "
                "pays this twice-over in user-visible latency. Co-locating them "
                "is the single biggest available speed-up.", db_rtt)
        else:
            logger.info("  DB round-trip: %.1f ms", db_rtt)
    except Exception:
        logger.exception("DB round-trip probe failed (non-fatal)")

    # ── Start admin panel FIRST (Render health check needs this) ─────
    admin_thread = threading.Thread(target=start_admin_panel, daemon=True)
    admin_thread.start()
    admin_port = os.getenv("ADMIN_PORT", os.getenv("PORT", 5000))
    logger.info("  Admin panel: starting on port %s", admin_port)

    import time
    time.sleep(2)  # give Flask a moment to bind the port

    # ── Start Telegram bot ───────────────────────────────────────────
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN not set — bot will NOT run. Admin panel is still running.")
        admin_thread.join()
        return

    try:
        logger.info("  Telegram bot: starting...")
        logger.info("Starting bot...")
        # Keep the bot responsive under load.  PTB processes updates
        # sequentially by default, so a single slow DB/image-heavy command can
        # otherwise make every later command wait behind it.
        concurrent_updates = int(os.getenv("BOT_CONCURRENT_UPDATES", "16"))
        connection_pool_size = int(os.getenv("BOT_CONNECTION_POOL_SIZE", "32"))
        pool_timeout = float(os.getenv("BOT_POOL_TIMEOUT", "10"))
        app = (ApplicationBuilder()
               .token(BOT_TOKEN)
               .post_init(register_bot_menu)
               .concurrent_updates(concurrent_updates)
               .connection_pool_size(connection_pool_size)
               .pool_timeout(pool_timeout)
               .build())

        def _is_storage_only_command(update):
            """Compatibility hook: all current commands use normal middleware."""
            return False

        # ── Reply-context middleware (group=-4, runs before outbound replies) ──
        # Refreshing the sender's stored username/name is a *blocking* DB round
        # trip. Running it on every single update (every button tap, every
        # message) serialises the asyncio event loop behind that I/O and is a
        # major source of the "buttons take forever" lag under load. Usernames
        # rarely change, so throttle the sync per user with a small TTL cache:
        # the first interaction syncs, subsequent ones within the window skip the
        # DB entirely. Command handlers that need a *fresh* reply/mention target
        # still resolve it themselves (see resolve_command_target).
        user_sync_ttl = float(os.getenv("USER_SYNC_TTL_SECONDS", "300"))
        user_sync_cache = {}

        async def _bind_reply_context(update, context):
            bind_reply_context(update)
            bind_button_owner_context(update)
            user = getattr(update, "effective_user", None)
            now = time.monotonic()
            if user is not None:
                last = user_sync_cache.get(user.id)
                if last is not None and now - last < user_sync_ttl:
                    return  # synced recently — skip the blocking DB write
            # The user sync is a blocking DB write. Run it in a worker thread so
            # it never freezes the event loop (and every other update queued on
            # it) while waiting on the database round-trip.
            def _do_user_sync():
                session = get_session()
                try:
                    sync_update_users(session, update)
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                finally:
                    session.close()
            try:
                await asyncio.to_thread(_do_user_sync)
                if user is not None:
                    user_sync_cache[user.id] = now
                    # Opportunistic prune so the cache can't grow without
                    # bound on a long-running, high-traffic bot.
                    if len(user_sync_cache) > 10000:
                        cutoff = now - user_sync_ttl
                        for uid in [u for u, ts in user_sync_cache.items()
                                    if ts < cutoff]:
                            user_sync_cache.pop(uid, None)
            except Exception:
                logger.exception("Failed to sync Telegram user details")
        app.add_handler(TypeHandler(_TGUpdate, _bind_reply_context), group=-4)

        # ── Button-owner guard (group=-5, before callbacks) ──
        app.add_handler(TypeHandler(_TGUpdate, button_access_guard), group=-5)

        # ── Chat-tracker middleware (group=-3, runs FIRST) ──
        async def _track_chat(update, context):
            if _is_storage_only_command(update):
                return
            try:
                from services.chat_tracker import record_chat
                record_chat(update)
            except Exception:
                pass
        app.add_handler(TypeHandler(_TGUpdate, _track_chat), group=-3)

        # ── Maintenance middleware (group=-2, runs FIRST) ──
        async def _maintenance_check(update, context):
            if _is_storage_only_command(update):
                return
            try:
                from services.maintenance_service import (
                    should_block_update, get_maintenance_message,
                    should_reply_with_maintenance,
                )
                if not should_block_update(update):
                    return
                # Blocked — reply only to commands. Non-command group chatter
                # is stopped silently to avoid spamming every message during
                # maintenance mode.
                if should_reply_with_maintenance(
                    update, getattr(context.bot, "username", None)
                ):
                    try:
                        text = get_maintenance_message()
                        if update.message:
                            await update.message.reply_text(text, parse_mode="HTML")
                        elif update.callback_query:
                            # Shouldn't reach here since callbacks bypass, but defensive
                            await update.callback_query.answer(
                                "Bot under maintenance", show_alert=True)
                    except Exception:
                        pass
                raise ApplicationHandlerStop
            except ApplicationHandlerStop:
                raise
            except Exception:
                logger.exception("Maintenance check failed (non-fatal)")
        app.add_handler(TypeHandler(_TGUpdate, _maintenance_check), group=-2)

        # ── Ban-guard middleware (group=-1, runs before all handlers) ──
        # Cache ban lookups briefly so every button tap/command does not pay a
        # synchronous database round trip before the real handler can run.
        ban_cache_ttl = float(os.getenv("BAN_CACHE_TTL_SECONDS", "60"))
        ban_cache = {}

        async def _ban_check(update, context):
            if _is_storage_only_command(update):
                return
            user = update.effective_user
            if not user:
                return
            now = time.monotonic()
            cached = ban_cache.get(user.id)
            if cached and now - cached[0] < ban_cache_ttl:
                is_banned, reason = cached[1], cached[2]
            else:
                # Cold cache: the ban lookup is a blocking DB query. Offload it
                # to a worker thread so it doesn't stall the loop before the real
                # handler (or button acknowledgement) can run.
                def _query_ban():
                    from database import get_session as _gs
                    from models import User as _User
                    s = _gs()
                    try:
                        u = (s.query(_User)
                             .filter(_User.telegram_id == user.id)
                             .first())
                        if u and u.is_banned:
                            return True, (u.ban_reason or "").strip()
                        return False, ""
                    finally:
                        s.close()
                try:
                    is_banned, reason = await asyncio.to_thread(_query_ban)
                    ban_cache[user.id] = (now, is_banned, reason)
                except Exception:
                    logger.exception("Ban-check middleware failed (non-fatal)")
                    return

            if is_banned:
                try:
                    txt = ("🚫 <b>You are banned from CricMaster Ultra.</b>\n"
                           + (f"\n<i>Reason: {reason}</i>" if reason else ""))
                    if update.callback_query:
                        await update.callback_query.answer(
                            "You are banned.", show_alert=True)
                    elif update.message:
                        await update.message.reply_text(txt, parse_mode="HTML")
                except Exception:
                    pass
                raise ApplicationHandlerStop
        app.add_handler(TypeHandler(_TGUpdate, _ban_check), group=-1)

        # Record process start time for /botstatus uptime reporting.
        from datetime import datetime as _dt_now
        app.bot_data["bot_start_time"] = _dt_now.utcnow()

        # ── Command handlers ─────────────────────────────────────────
        # ── Core commands + short aliases ────────────────────────────
        from handlers.botstatus import botstatus_handler
        app.add_handler(CommandHandler(["botstatus", "bstatus", "ping"], botstatus_handler))
        app.add_handler(CommandHandler(["start", "s"], start_handler))
        app.add_handler(CommandHandler(["debut", "d"], debut_handler))
        app.add_handler(CommandHandler(["claim", "c"], claim_handler))
        app.add_handler(CommandHandler(["gspin", "gs"], gspin_handler))
        app.add_handler(CommandHandler(["daily", "dl"], daily_handler))
        app.add_handler(CommandHandler(["myroster", "mr", "roster"], myroster_handler))
        app.add_handler(CommandHandler(["playerinfo", "pi", "info"], playerinfo_handler))
        app.add_handler(CallbackQueryHandler(player_version_callback, pattern=r"^plv_"))
        app.add_handler(CommandHandler(["releasepl", "release", "rel"], releasepl_handler))
        app.add_handler(CommandHandler(["releasemultiple", "relm", "rm"], releasemultiple_handler))
        app.add_handler(CommandHandler(["trade", "tr"], trade_handler))
        app.add_handler(CommandHandler(["playingxi", "pxi", "xi"], playingxi_handler))
        app.add_handler(CommandHandler(["ximage", "xiimg", "xipic"], ximage_handler))
        app.add_handler(CommandHandler(["autobuild", "ab", "best11"], autobuild_handler))
        app.add_handler(CommandHandler(["swapplayers", "swappl", "swap"], swapplayers_handler))
        # /setbo, /sbo — set your own batting order (roster slots 1-11). The
        # order saved here is what /letsplay and the Mini App XI screen use.
        app.add_handler(CommandHandler(
            ["setbo", "sbo", "battingorder", "bo"], setbo_handler))
        app.add_handler(CommandHandler(["setcaptain", "captain", "cap"], setcaptain_handler))
        app.add_handler(CommandHandler(["searchpl", "search", "sp"], searchpl_handler))
        app.add_handler(CommandHandler(["searchovr", "so"], searchovr_handler))
        app.add_handler(CallbackQueryHandler(searchpl_page_callback, pattern=r"^spl_"))
        app.add_handler(CallbackQueryHandler(searchovr_page_callback, pattern=r"^sovr_"))
        app.add_handler(CallbackQueryHandler(search_cancel_callback, pattern=r"^searchcancel_"))
        app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
        app.add_handler(CallbackQueryHandler(giveaway_join_callback, pattern=r"^gwjoin_"))
        app.add_handler(CommandHandler(["buypl", "buy", "b"], buypl_handler))
        app.add_handler(CommandHandler(["teamname", "tn"], teamname_handler))
        app.add_handler(CommandHandler(["purse", "p"], purse_handler))
        app.add_handler(CommandHandler(["stats", "st"], stats_handler))
        app.add_handler(CommandHandler("statscl", statscl_handler))
        app.add_handler(CommandHandler("statstour", statstour_handler))
        app.add_handler(CommandHandler("tournamentstats", tournamentstats_handler))
        app.add_handler(CallbackQueryHandler(tournamentstats_callback, pattern=r"^tstat_"))
        app.add_handler(CommandHandler(["cmuleaderboard", "leaderboard", "lb", "top"], leaderboard_handler))
        app.add_handler(CommandHandler(["myprofile", "profile", "me"], myprofile_handler))
        app.add_handler(CommandHandler(["playmatch", "pm", "match"], playmatch_handler))
        app.add_handler(CommandHandler("wpm", wpm_handler))
        app.add_handler(CommandHandler(["sim", "simmatch"], sim_handler))
        app.add_handler(CommandHandler("testwpm", testwpm_handler))
        app.add_handler(CommandHandler(["endmatch", "em"], endmatch_handler))
        app.add_handler(CommandHandler(["clearmatches", "clearmatch"], clearmatches_handler))
        app.add_handler(CommandHandler(["removematch", "rmatch", "kickmatch"], removematch_handler))
        app.add_handler(CommandHandler(["resume", "r"], resume_handler))
        app.add_handler(CommandHandler(["lastmatch", "lm"], lastmatch_handler))
        app.add_handler(CommandHandler(["recentmatches", "recent", "matches"], recentmatches_handler))
        app.add_handler(CommandHandler(["h2h", "headtohead"], h2h_handler))
        app.add_handler(CommandHandler(["matchinfo", "mi"], info_handler))

        # ── User feedback ────────────────────────────────────────────
        app.add_handler(CommandHandler("report", report_handler))

        # ── Coins → Gems conversion ──────────────────────────────────
        app.add_handler(CommandHandler(["coins2gems", "c2g"], coins2gems_handler))
        app.add_handler(CallbackQueryHandler(coins2gems_callback, pattern=r"^c2g:"))

        # ── Owner-only subscription grant (hidden from menu) ─────────
        app.add_handler(CommandHandler("grant", grant_handler))

        # ── CMU Shop image gallery ───────────────────────────────────
        # PTB requires lowercase command names and lowercases incoming ones
        # before matching, so "cmushop" also handles a user typing /CMUshop.
        app.add_handler(CommandHandler("cmushop", cmushop_handler))
        app.add_handler(CallbackQueryHandler(cmushop_callback, pattern=r"^cmushop:"))

        # ── Admin reply-forward broadcasts ───────────────────────────
        app.add_handler(CommandHandler("frwd_grp", frwd_grp_handler))
        app.add_handler(CommandHandler("frwd_prvt", frwd_prvt_handler))
        # Buffer album (media-group) parts admins send to the bot DM so the
        # forward commands can copy every image, not just the replied one.
        # Own group (7) so it's purely additive — PTB runs one handler per
        # group, and this must not starve other private-message handlers.
        from telegram.ext import MessageHandler as _AlbumMsgHandler
        from telegram.ext import filters as _album_filters
        app.add_handler(_AlbumMsgHandler(
            (_album_filters.PHOTO | _album_filters.VIDEO | _album_filters.Document.ALL)
            & _album_filters.ChatType.PRIVATE,
            track_album_message,
        ), group=7)

        # ── Admin: Challenge League Tournament command allowlist ─────
        app.add_handler(CommandHandler("tourallow", tourallow_handler))
        app.add_handler(CommandHandler("tourblock", tourblock_handler))
        app.add_handler(CommandHandler("tourallowlist", tourallowlist_handler))

        # ── Admin: manually seed a player card file_id ───────────────
        app.add_handler(CommandHandler("setcardid", setcardid_handler))

        # ── Undo command — reverses last /buy or /release within 60s ─
        app.add_handler(CommandHandler("cmuundo", cmuundo_handler))
        logger.info("Registered /cmuundo handler (60s undo window)")

        # ── Mini App launcher ────────────────────────────────────────
        app.add_handler(CommandHandler("app", app_handler))
        logger.info("Registered /app handler (Mini App)")

        # ── 16-0 IPL Sim Mini App ────────────────────────────────────
        app.add_handler(CommandHandler(["ipl160", "16o", "iplsim"], ipl160_handler))
        # The literal "/16-0IPL" isn't a valid Telegram command (hyphen +
        # uppercase), so it never arrives as a bot_command — catch the text.
        # Use local aliases: main() rebinds `MessageHandler`/`filters` via later
        # `from telegram.ext import ...` lines, which makes them function-locals
        # for the whole scope, so the module-level names are unbound here.
        from telegram.ext import MessageHandler as _IplMsgHandler
        from telegram.ext import filters as _ipl_filters
        app.add_handler(_IplMsgHandler(
            _ipl_filters.Regex(r"(?i)^/16-0ipl\b"), ipl160_handler), group=5)
        logger.info("Registered /ipl160 handler (16-0 IPL Sim Mini App)")

        # ── Chat membership tracking ────────────────────────────────
        from services.chat_tracker import handle_chat_member_update
        app.add_handler(ChatMemberHandler(
            handle_chat_member_update,
            ChatMemberHandler.MY_CHAT_MEMBER,
        ))

        # ── Welcome new members + /ewm /dwm toggles ─────────────────
        from handlers.welcome import (
            new_member_handler, enable_welcome_handler, disable_welcome_handler,
        )
        from telegram.ext import MessageHandler, filters as _filters
        app.add_handler(MessageHandler(
            _filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler))
        app.add_handler(CommandHandler("ewm", enable_welcome_handler))
        app.add_handler(CommandHandler("dwm", disable_welcome_handler))

        # ── Bowl-out command ─────────────────────────────────────────
        app.add_handler(CommandHandler(["pbo", "bowlout"], pbo_handler))
        app.add_handler(CommandHandler("catch", catch_handler))
        app.add_handler(CommandHandler("bal", bal_handler))
        app.add_handler(CommandHandler("cm", challenge_handler))
        # /change <out> <in> — swap a Playing XI player. Runs in group 0 (before
        # the group-1 league command regex, which safely ignores it). The router
        # serves both the Challenge League (/cm, /cipl) and /letsplay drafts.
        from handlers.letsplay import change_router as _change_router
        app.add_handler(CommandHandler("change", _change_router))
        app.add_handler(MessageHandler(
            _filters.Regex(r"^/[A-Za-z0-9_]+(?:@\w+)?(?:\s|$)"),
            challenge_league_handler,
        ), group=1)
        app.add_handler(CallbackQueryHandler(challenge_accept_callback, pattern=r"^cm_accept_"))
        app.add_handler(CallbackQueryHandler(challenge_deny_callback, pattern=r"^cm_deny_"))
        app.add_handler(CallbackQueryHandler(challenge_cancel_callback, pattern=r"^cm_cancel_"))
        app.add_handler(CallbackQueryHandler(challenge_coin_callback, pattern=r"^cm_coin_"))
        app.add_handler(CallbackQueryHandler(challenge_toss_callback, pattern=r"^cm_toss_"))
        app.add_handler(CallbackQueryHandler(challenge_pick_callback, pattern=r"^cm_pick_"))
        app.add_handler(CallbackQueryHandler(challenge_team_callback, pattern=r"^cl_team_"))
        app.add_handler(CallbackQueryHandler(challenge_pitch_callback, pattern=r"^cl_pitch_"))
        app.add_handler(CallbackQueryHandler(challenge_deny_match_callback, pattern=r"^cl_denymatch_"))
        app.add_handler(CallbackQueryHandler(challenge_team_cancel_callback, pattern=r"^cl_cancel_"))
        app.add_handler(CallbackQueryHandler(challenge_xi_callback, pattern=r"^cl_xi_"))
        app.add_handler(CallbackQueryHandler(challenge_xi_useprev_callback, pattern=r"^cl_useprev_"))
        app.add_handler(CallbackQueryHandler(challenge_xi_pick_callback, pattern=r"^cl_pick_"))
        app.add_handler(CallbackQueryHandler(challenge_xi_confirm_callback, pattern=r"^cl_confirm_"))
        app.add_handler(CallbackQueryHandler(challenge_xi_clear_callback, pattern=r"^cl_clear_"))
        app.add_handler(CallbackQueryHandler(challenge_xi_edit_callback, pattern=r"^cl_edit_"))
        app.add_handler(CallbackQueryHandler(challenge_start_match_callback, pattern=r"^cl_start_"))
        # Typed quick-select: a participant replies with 11 numbers to set their
        # XI. Own dedicated group (6) — group 2 already holds the WordChase and
        # Bluff text handlers, and PTB runs only the first matching handler per
        # group, so sharing a group would starve those games. It is an O(1)
        # no-op for any text that isn't an active XI quick-select.
        app.add_handler(MessageHandler(
            _filters.TEXT & ~_filters.COMMAND,
            challenge_xi_quickselect,
        ), group=6)
        # Challenge League over-by-over "approach" match flow
        from handlers.cipl_play import (
            cipl_coin_callback, cipl_toss_callback, cipl_bowler_callback,
            cipl_bowlapp_callback, cipl_batapp_callback, rcl_handler,
        )
        app.add_handler(CallbackQueryHandler(cipl_coin_callback, pattern=r"^cipl_coin_"))
        app.add_handler(CallbackQueryHandler(cipl_toss_callback, pattern=r"^cipl_toss_"))
        app.add_handler(CallbackQueryHandler(cipl_bowler_callback, pattern=r"^cipl_bowler_"))
        app.add_handler(CallbackQueryHandler(cipl_bowlapp_callback, pattern=r"^cipl_bowlapp_"))
        app.add_handler(CallbackQueryHandler(cipl_batapp_callback, pattern=r"^cipl_batapp_"))
        # /rcl — resume a stuck Challenge League match from where it left off
        app.add_handler(CommandHandler(["rcl", "resumecl"], rcl_handler))
        # Super Over (tied /cipl, /c[league] and /letsplay matches) — interactive
        # user-vs-user, ball by ball; outcomes from the SimCricketX engine.
        from handlers.super_over import (
            so_bat_callback, so_batok_callback, so_bowl_callback,
            so_bowlok_callback, so_deliv_callback, so_len_callback,
            so_shot_callback, so_killer_callback, so_power_callback,
        )
        app.add_handler(CallbackQueryHandler(so_batok_callback, pattern=r"^so_batok_"))
        app.add_handler(CallbackQueryHandler(so_bat_callback, pattern=r"^so_bat_"))
        app.add_handler(CallbackQueryHandler(so_bowlok_callback, pattern=r"^so_bowlok_"))
        app.add_handler(CallbackQueryHandler(so_bowl_callback, pattern=r"^so_bowl_"))
        app.add_handler(CallbackQueryHandler(so_deliv_callback, pattern=r"^so_dv_"))
        app.add_handler(CallbackQueryHandler(so_len_callback, pattern=r"^so_ln_"))
        app.add_handler(CallbackQueryHandler(so_shot_callback, pattern=r"^so_sh_"))
        # One-per-innings captain gambles: 🎯 Killer Ball / 💥 Power Shot.
        app.add_handler(CallbackQueryHandler(so_killer_callback, pattern=r"^so_kb_"))
        app.add_handler(CallbackQueryHandler(so_power_callback, pattern=r"^so_ps_"))
        # /letsplay — friendly head-to-head played with each user's own roster.
        # Reuses the Challenge League over-by-over engine; traits are active.
        from handlers.letsplay import (
            letsplay_handler, letsplay_invite_callback, letsplay_pitch_callback,
            letsplay_starttoss_callback, letsplay_coin_callback,
            letsplay_toss_callback,
        )
        app.add_handler(CommandHandler(["letsplay", "lp"], letsplay_handler))
        app.add_handler(CallbackQueryHandler(letsplay_invite_callback, pattern=r"^lp_(accept|deny)_"))
        app.add_handler(CallbackQueryHandler(letsplay_pitch_callback, pattern=r"^lp_pitch_"))
        app.add_handler(CallbackQueryHandler(letsplay_starttoss_callback, pattern=r"^lp_starttoss_"))
        app.add_handler(CallbackQueryHandler(letsplay_coin_callback, pattern=r"^lp_coin_"))
        app.add_handler(CallbackQueryHandler(letsplay_toss_callback, pattern=r"^lp_toss_"))
        app.add_handler(CommandHandler(["unscramble", "u"], unscramble_handler))
        app.add_handler(CommandHandler("ju", unscramble_join_handler))
        app.add_handler(CommandHandler("eu", unscramble_exit_handler))
        app.add_handler(CommandHandler("su", unscramble_start_handler))
        app.add_handler(CommandHandler("cu", unscramble_cancel_handler))
        app.add_handler(CallbackQueryHandler(unscramble_join_handler, pattern=r"^us_join$"))
        app.add_handler(CallbackQueryHandler(unscramble_answer_callback, pattern=r"^us_ans_"))

        # ── Trait system ─────────────────────────────────────────────
        app.add_handler(CommandHandler(["traits", "tt"], traits_handler))
        app.add_handler(CommandHandler(["traitshop", "tshop"], traitshop_handler))
        app.add_handler(CommandHandler(["traitapply", "tapply"], traitapply_handler))
        app.add_handler(CommandHandler(["traitupgrade", "tup"], traitupgrade_handler))
        app.add_handler(CommandHandler(["traitreplace", "trep"], traitreplace_handler))
        app.add_handler(CommandHandler(["removetrait", "rtrait"], removetrait_handler))

        app.add_handler(CallbackQueryHandler(traitbuy_callback, pattern=r"^trbuy_"))
        app.add_handler(CallbackQueryHandler(traitreroll_callback, pattern=r"^trreroll_"))
        app.add_handler(CallbackQueryHandler(traitshop_cancel_callback, pattern=r"^trshopcancel_"))
        app.add_handler(CallbackQueryHandler(trapply_inv_callback, pattern=r"^trapply_inv_"))
        app.add_handler(CallbackQueryHandler(trapply_pl_callback, pattern=r"^trapply_pl_"))
        app.add_handler(CallbackQueryHandler(trup_pt_callback, pattern=r"^trup_pt_"))
        app.add_handler(CallbackQueryHandler(trup_inv_callback, pattern=r"^trup_inv_"))
        app.add_handler(CallbackQueryHandler(trrep_pt_callback, pattern=r"^trrep_pt_"))
        app.add_handler(CallbackQueryHandler(trrep_inv_callback, pattern=r"^trrep_inv_"))
        app.add_handler(CallbackQueryHandler(trrem_pt_callback, pattern=r"^trrem_pt_"))
        app.add_handler(CallbackQueryHandler(trait_cancel_callback, pattern=r"^trcancel$"))

        # ── Player Market ────────────────────────────────────────────
        app.add_handler(CommandHandler(["playermarket", "pmarket", "market"], playermarket_handler))
        app.add_handler(CallbackQueryHandler(playermarket_select_callback, pattern=r"^pmsel_"))
        app.add_handler(CallbackQueryHandler(playermarket_buy_callback, pattern=r"^pmbuy_"))
        app.add_handler(CallbackQueryHandler(playermarket_back_callback, pattern=r"^pmback_"))
        app.add_handler(CallbackQueryHandler(playermarket_noop_callback, pattern=r"^pmnoop_"))
        app.add_handler(CallbackQueryHandler(playermarket_cancel_callback, pattern=r"^pmcancel_"))

        # ── Packs ──────────────────────────────────────────────────
        app.add_handler(CommandHandler(["buypack", "packs", "shop"], buypack_handler))
        app.add_handler(CallbackQueryHandler(pack_view_callback, pattern=r"^pkv_"))
        app.add_handler(CallbackQueryHandler(pack_back_callback, pattern=r"^pkbk_"))
        app.add_handler(CallbackQueryHandler(pack_close_callback, pattern=r"^pkc_"))
        app.add_handler(CallbackQueryHandler(pack_buy_callback, pattern=r"^pkb_"))
        app.add_handler(CallbackQueryHandler(pack_noop_callback, pattern=r"^pknoop_"))
        app.add_handler(CommandHandler(["openpack", "open"], openpack_handler))
        # opkc_ before opk_ so longer prefix matches first
        app.add_handler(CallbackQueryHandler(pack_open_close_callback, pattern=r"^opkc_"))
        app.add_handler(CallbackQueryHandler(pack_open_inventory_callback, pattern=r"^opk_"))

        # ── Tours ──────────────────────────────────────────────────
        app.add_handler(CommandHandler(["cmtours", "createtour"], cmtours_handler))
        app.add_handler(CommandHandler(["mytours", "tours"], mytours_handler))
        # `cancel_cmt_` must be registered before `ctm_` to avoid regex confusion;
        # all three picker patterns start with distinct prefixes anyway.
        app.add_handler(CallbackQueryHandler(cmt_cancel_callback, pattern=r"^cancel_cmt_"))
        app.add_handler(CallbackQueryHandler(cmt_matches_callback, pattern=r"^ctm_"))
        app.add_handler(CallbackQueryHandler(cmt_overs_callback, pattern=r"^cto_"))
        app.add_handler(CallbackQueryHandler(tour_accept_callback, pattern=r"^tac_"))
        app.add_handler(CallbackQueryHandler(tour_decline_callback, pattern=r"^tdc_"))
        app.add_handler(CallbackQueryHandler(mytours_play_callback, pattern=r"^mtp_"))
        app.add_handler(CallbackQueryHandler(mytours_info_callback, pattern=r"^mti_"))
        app.add_handler(CallbackQueryHandler(mytours_stats_callback, pattern=r"^mts_"))
        app.add_handler(CallbackQueryHandler(mytours_back_callback, pattern=r"^mtb_"))

        # ── Challenge League Tours ─────────────────────────────────────
        # /cltour with a target creates a tour; without one it shows your tour.
        # Prefixes are distinct from the CIPL `cl_*` callbacks (these start `clt`).
        app.add_handler(CommandHandler(["cltour", "cltours"], cltour_handler))
        app.add_handler(CallbackQueryHandler(cltset_league_callback, pattern=r"^cltset_lg_"))
        app.add_handler(CallbackQueryHandler(cltset_hostteam_callback, pattern=r"^cltset_ht_"))
        app.add_handler(CallbackQueryHandler(cltset_guestteam_callback, pattern=r"^cltset_gt_"))
        app.add_handler(CallbackQueryHandler(cltset_count_callback, pattern=r"^cltset_n_"))
        app.add_handler(CallbackQueryHandler(cltset_cancel_callback, pattern=r"^cltset_x_"))
        app.add_handler(CallbackQueryHandler(cltour_accept_callback, pattern=r"^clt_acc_"))
        app.add_handler(CallbackQueryHandler(cltour_decline_callback, pattern=r"^clt_dec_"))
        app.add_handler(CallbackQueryHandler(cltour_play_callback, pattern=r"^cltplay_"))

        # ── vsbot ────────────────────────────────────────────────────
        app.add_handler(CommandHandler(["vsbot", "vsb"], vsbot_handler))
        app.add_handler(CallbackQueryHandler(vsbot_pick_callback, pattern=r"^vsb_pick_"))
        app.add_handler(CallbackQueryHandler(vsbot_cancel_callback, pattern=r"^vsb_cancel_"))
        app.add_handler(CallbackQueryHandler(vsbot_toss_callback, pattern=r"^vsb_toss_"))
        app.add_handler(CallbackQueryHandler(vsbot_op1_callback, pattern=r"^vsb_op1_"))
        app.add_handler(CallbackQueryHandler(vsbot_op2_callback, pattern=r"^vsb_op2_"))
        app.add_handler(CallbackQueryHandler(vsbot_selbowl_callback, pattern=r"^vsb_selbowl_"))

        # ── wpmbot (vs bot, played in the Mini App) ──────────────────
        app.add_handler(CommandHandler(["wpmbot", "wpmb"], wpmbot_handler))
        app.add_handler(CallbackQueryHandler(wpmbot_cancel_callback, pattern=r"^wpmb_cancel_"))
        app.add_handler(CallbackQueryHandler(wpmbot_coin_callback, pattern=r"^wpmb_coin_"))
        app.add_handler(CallbackQueryHandler(wpmbot_toss_callback, pattern=r"^wpmb_toss_"))

        # ── wsp (PvP auto-simulated watch mode) ──────────────────────
        app.add_handler(CommandHandler(["wsp"], wsp_handler))
        app.add_handler(CallbackQueryHandler(wsp_join_callback,     pattern=r"^wsp_join$"))
        app.add_handler(CallbackQueryHandler(wsp_cancel_callback,   pattern=r"^wsp_cancel$"))
        app.add_handler(CallbackQueryHandler(wsp_decision_callback, pattern=r"^wsp_decision:"))

        # ── wspbot (vs bot auto-simulated watch mode) ─────────────────
        app.add_handler(CommandHandler(["wspbot", "wspb"], wspbot_handler))
        app.add_handler(CallbackQueryHandler(wspbot_pick_callback,   pattern=r"^wspb_pick_"))
        app.add_handler(CallbackQueryHandler(wspbot_cancel_callback, pattern=r"^wspb_cancel_"))
        app.add_handler(CallbackQueryHandler(wspbot_toss_callback,   pattern=r"^wspb_toss_"))

        # ── Quests ──────────────────────────────────────────────────
        app.add_handler(CommandHandler(["myquest", "mq", "quests"], myquest_handler))
        app.add_handler(CallbackQueryHandler(quest_tab_callback, pattern=r"^qst_tab_"))
        app.add_handler(CallbackQueryHandler(quest_filter_callback, pattern=r"^qst_flt_"))
        app.add_handler(CallbackQueryHandler(quest_page_callback, pattern=r"^qst_pg_"))
        app.add_handler(CallbackQueryHandler(quest_noop_callback, pattern=r"^qst_noop_"))
        # IMPORTANT: claimall must register BEFORE claim so the longer pattern wins
        app.add_handler(CallbackQueryHandler(quest_claimall_callback, pattern=r"^qst_claimall_"))
        app.add_handler(CallbackQueryHandler(quest_claim_callback, pattern=r"^qst_claim_"))
        app.add_handler(CallbackQueryHandler(quest_close_callback, pattern=r"^qst_close_"))

        # ── Achievements ────────────────────────────────────────────
        app.add_handler(CommandHandler(["achievements", "ach", "badges"], achievements_handler))
        app.add_handler(CallbackQueryHandler(achievements_tab_callback, pattern=r"^ach_tab_"))
        app.add_handler(CallbackQueryHandler(achievements_close_callback, pattern=r"^ach_close_"))

        # ── /howto Tutorial ─────────────────────────────────────────
        app.add_handler(CommandHandler(["howto", "help", "guide"], howto_handler))
        app.add_handler(CallbackQueryHandler(howto_tab_callback, pattern=r"^howto_tab_"))
        app.add_handler(CallbackQueryHandler(howto_close_callback, pattern=r"^howto_close_"))

        # ── /invite Referral system ─────────────────────────────────
        from handlers.invite import invite_handler
        app.add_handler(CommandHandler(["invite", "ref", "refer", "share"], invite_handler))

        # ── /redeem Referral code redemption ──────────────────────
        from handlers.redeem import (
            redeem_handler, text_code_handler, refcode_skip_callback,
        )
        app.add_handler(CommandHandler(["redeem", "code"], redeem_handler))
        app.add_handler(CallbackQueryHandler(refcode_skip_callback,
                                              pattern=r"^refcode_skip_"))
        # Text fallback: in DM only, catches bare codes typed after /debut.
        # text_code_handler checks the `awaiting_referral_code` flag and
        # returns early if not set, so other text messages pass through.
        from telegram.ext import MessageHandler, filters
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            text_code_handler,
        ), group=0)

        # ── Bot vs Bot spectator mode ─────────────────────────────────
        app.add_handler(CommandHandler(["botvsbot", "bvb"], botvsbot_handler))
        app.add_handler(CallbackQueryHandler(bvb_pickA_callback, pattern=r"^bvb_pickA_"))
        app.add_handler(CallbackQueryHandler(bvb_pickB_callback, pattern=r"^bvb_pickB_"))
        app.add_handler(CallbackQueryHandler(bvb_cancel_callback, pattern=r"^bvb_cancel_"))

        # ── /botmatch Spectator Mode ────────────────────────────────
        app.add_handler(CommandHandler(["botmatch", "spectate"], botmatch_handler))
        app.add_handler(CallbackQueryHandler(botmatch_pick_a_callback, pattern=r"^botmatch_a_"))
        app.add_handler(CallbackQueryHandler(botmatch_pick_b_callback, pattern=r"^botmatch_b_"))
        app.add_handler(CallbackQueryHandler(botmatch_cancel_callback, pattern=r"^botmatch_cancel_"))

        # ── Claim flow callbacks ─────────────────────────────────────
        app.add_handler(CallbackQueryHandler(retain_callback, pattern=r"^retain_"))
        app.add_handler(CallbackQueryHandler(release_callback, pattern=r"^release_"))
        app.add_handler(CallbackQueryHandler(replace_callback, pattern=r"^replace_"))
        app.add_handler(CallbackQueryHandler(replace_confirm_callback, pattern=r"^repl_"))

        # ── Daily & GSpin callbacks ──────────────────────────────────
        app.add_handler(CallbackQueryHandler(daily_claim_callback, pattern=r"^dailyclaim_"))
        app.add_handler(CallbackQueryHandler(gspin_spin_callback, pattern=r"^gspin_"))

        # ── Release callbacks ────────────────────────────────────────
        app.add_handler(CallbackQueryHandler(release_one_callback, pattern=r"^rlone_"))
        app.add_handler(CallbackQueryHandler(release_cancel_callback, pattern=r"^rlcancel$"))
        app.add_handler(CallbackQueryHandler(releasemultiple_confirm_callback, pattern=r"^rlm_"))
        # Legacy patterns (ignored gracefully)
        app.add_handler(CallbackQueryHandler(release_cancel_callback, pattern=r"^rlconfirm_"))
        app.add_handler(CallbackQueryHandler(release_cancel_callback, pattern=r"^relmconf_"))
        app.add_handler(CallbackQueryHandler(release_cancel_callback, pattern=r"^rldup_"))
        app.add_handler(CallbackQueryHandler(roster_page_callback, pattern=r"^roster_page_"))
        app.add_handler(CallbackQueryHandler(bench_callback, pattern=r"^viewbench_"))

        # ── Buy callbacks ────────────────────────────────────────────
        app.add_handler(CallbackQueryHandler(buypl_confirm_callback, pattern=r"^buypl_"))
        app.add_handler(CallbackQueryHandler(buypl_cancel_callback, pattern=r"^buycancel"))
        app.add_handler(CallbackQueryHandler(player_page_callback, pattern=r"^plpg_"))
        app.add_handler(CallbackQueryHandler(player_page_noop_callback, pattern=r"^plpgnoop_"))

        # ── Match callbacks ──────────────────────────────────────────
        app.add_handler(CallbackQueryHandler(cric_join_callback, pattern=r"^cric_join$"))
        app.add_handler(CallbackQueryHandler(cric_cancel_lobby_callback, pattern=r"^cric_cancel_lobby$"))
        app.add_handler(CallbackQueryHandler(cric_coin_callback, pattern=r"^cric_coin:"))
        app.add_handler(CallbackQueryHandler(cric_decision_callback, pattern=r"^cric_decision:"))
        app.add_handler(CallbackQueryHandler(match_accept_callback, pattern=r"^matchacc_"))
        app.add_handler(CallbackQueryHandler(match_deny_callback, pattern=r"^matchdeny_"))
        app.add_handler(CallbackQueryHandler(overs_button_callback, pattern=r"^oversset_"))
        app.add_handler(CallbackQueryHandler(overs_custom_callback, pattern=r"^overscustom_"))
        app.add_handler(CallbackQueryHandler(toss_decision_callback, pattern=r"^toss_"))
        app.add_handler(CallbackQueryHandler(opener1_callback, pattern=r"^op1_"))
        app.add_handler(CallbackQueryHandler(opener2_callback, pattern=r"^op2_"))
        app.add_handler(CallbackQueryHandler(select_bowler_callback, pattern=r"^selbowl_"))
        app.add_handler(CallbackQueryHandler(variation_callback, pattern=r"^bvar_"))
        app.add_handler(CallbackQueryHandler(length_callback, pattern=r"^blen_"))
        app.add_handler(CallbackQueryHandler(spinner_delivery_callback, pattern=r"^bspin_"))
        app.add_handler(CallbackQueryHandler(shot_callback, pattern=r"^bshot_"))
        app.add_handler(CallbackQueryHandler(new_over_bowler_callback, pattern=r"^nbowl_"))
        app.add_handler(CallbackQueryHandler(new_batsman_callback, pattern=r"^newbat_"))

        # ── Bowl-out callbacks ───────────────────────────────────────
        app.add_handler(CallbackQueryHandler(pbo_accept_callback, pattern=r"^pboacc_"))
        app.add_handler(CallbackQueryHandler(pbo_decline_callback, pattern=r"^pbodec_"))
        app.add_handler(CallbackQueryHandler(bowlout_pick_callback, pattern=r"^bopick_"))
        app.add_handler(CallbackQueryHandler(endmatch_yes_callback, pattern=r"^endmatch_"))
        app.add_handler(CallbackQueryHandler(endmatch_no_callback, pattern=r"^endmatchno_"))

        # ── Leaderboard & Profile ──────────────────────────────────
        app.add_handler(CallbackQueryHandler(leaderboard_callback, pattern=r"^lb_"))
        app.add_handler(CallbackQueryHandler(myprofile_callback, pattern=r"^mp_"))

        # ── Trade callbacks ──────────────────────────────────────────
        app.add_handler(CallbackQueryHandler(trade_user1_player_callback, pattern=r"^t1p_"))
        app.add_handler(CallbackQueryHandler(trade_user2_player_callback, pattern=r"^t2p_"))
        app.add_handler(CallbackQueryHandler(trade_confirm_callback, pattern=r"^tcfrm_"))
        app.add_handler(CallbackQueryHandler(trade_cancel_callback, pattern=r"^tcancel_"))
        # Legacy trade callback patterns are routed to compatibility wrappers.
        app.add_handler(CallbackQueryHandler(trade_rating_callback, pattern=r"^trate_"))
        app.add_handler(CallbackQueryHandler(trade_myplayer_callback, pattern=r"^tmypl_"))
        app.add_handler(CallbackQueryHandler(trade_theirplayer_callback, pattern=r"^tthpl_"))
        app.add_handler(CallbackQueryHandler(trade_send_callback, pattern=r"^tsend_"))
        app.add_handler(CallbackQueryHandler(trade_accept_callback, pattern=r"^taccept_"))
        app.add_handler(CallbackQueryHandler(trade_reject_callback, pattern=r"^treject_"))
        app.add_handler(CallbackQueryHandler(trade_cancel_callback, pattern=r"^tcancel$"))
        app.add_handler(CallbackQueryHandler(trade_back_callback, pattern=r"^tback_"))

        # ── Social & Gambling Games ──────────────────────────────────
        # Subscription mystery box + recurring drops
        app.add_handler(CommandHandler("cmumysterybox", cmumysterybox_handler))
        app.add_handler(CallbackQueryHandler(mysterybox_callback, pattern=r"^cmb_"))
        app.add_handler(CommandHandler("cmuweekly", cmuweekly_handler))
        app.add_handler(CommandHandler("cmuchest", cmuchest_handler))

        app.add_handler(CommandHandler("lucky7", lucky7_handler))
        app.add_handler(CallbackQueryHandler(lucky7_callback, pattern=r"^l7_"))

        app.add_handler(CommandHandler(["powerplay", "pp"], powerplay_handler))

        app.add_handler(CommandHandler(["score21", "s21"], score21_handler))
        app.add_handler(CallbackQueryHandler(score21_callback, pattern=r"^s21_"))

        app.add_handler(CommandHandler(["wordchase", "wc"], wordchase_handler))
        app.add_handler(CommandHandler(["endchase", "ewc"], wordchase_end_handler))
        app.add_handler(CallbackQueryHandler(wordchase_end_callback, pattern=r"^wc_end_"))

        app.add_handler(CommandHandler("bluff", bluff_handler))
        app.add_handler(CallbackQueryHandler(bluff_accept_callback, pattern=r"^bm_accept_"))
        app.add_handler(CallbackQueryHandler(bluff_decline_callback, pattern=r"^bm_decline_"))
        app.add_handler(CallbackQueryHandler(bluff_action_callback, pattern=r"^bm_answer_|^bm_steal_"))

        app.add_handler(CommandHandler("mole", mole_handler))
        app.add_handler(CallbackQueryHandler(mh_join_callback, pattern=r"^mh_join_"))
        app.add_handler(CallbackQueryHandler(mh_start_callback, pattern=r"^mh_start_"))
        app.add_handler(CallbackQueryHandler(mh_theme_callback, pattern=r"^mh_theme_"))
        app.add_handler(CallbackQueryHandler(mh_cancel_callback, pattern=r"^mh_cancel_"))
        app.add_handler(CallbackQueryHandler(mh_vote_callback, pattern=r"^mh_vote"))

        app.add_handler(CommandHandler("cartel", cartel_handler))
        app.add_handler(CallbackQueryHandler(ct_join_callback, pattern=r"^ct_join_"))
        app.add_handler(CallbackQueryHandler(ct_start_callback, pattern=r"^ct_start_"))
        app.add_handler(CallbackQueryHandler(ct_theme_callback, pattern=r"^ct_theme_"))
        app.add_handler(CallbackQueryHandler(ct_cancel_callback, pattern=r"^ct_cancel_"))
        app.add_handler(CallbackQueryHandler(ct_vote_callback, pattern=r"^ct_vote"))

        app.add_handler(CommandHandler(["feedback", "fb"], feedback_handler))
        app.add_handler(CommandHandler(["notifications", "notify", "notif"], notifications_handler))
        app.add_handler(CallbackQueryHandler(notifications_toggle_callback, pattern=r"^notif_toggle:"))

        # ── Fantasy League ───────────────────────────────────────────
        app.add_handler(CommandHandler(["fantasy", "fl"], fantasy_handler))
        app.add_handler(CommandHandler(["myfantasy", "mfl"], myfantasy_handler))
        app.add_handler(CommandHandler(["fantasyleaderboard", "flb"], fantasyleaderboard_handler))
        app.add_handler(CommandHandler(["fantasystats", "fstats"], fantasystats_handler))
        app.add_handler(CommandHandler(["fantasyguide", "fguide"], fantasyguide_handler))
        app.add_handler(CallbackQueryHandler(fantasy_lb_callback, pattern=r"^fant_lb_"))
        app.add_handler(MessageHandler(
            filters.StatusUpdate.WEB_APP_DATA, fantasy_webapp_handler,
        ), group=5)

        # Social game group message handlers run alongside overs_text_handler
        # (separate groups so each processes independently)
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            wordchase_message_handler,
        ), group=2)
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            molehunt_message_handler,
        ), group=3)
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            cartel_message_handler,
        ), group=4)
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            bluff_answer_message,
        ), group=2)

        # ── Text handler for over selection (must be LAST) ───────────
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, overs_text_handler))

        logger.info("Bot is running. Press Ctrl+C to stop.")
        logger.info("=" * 50)
        logger.info("EVERYTHING RUNNING! Admin: port %s | Bot: polling", admin_port)
        logger.info("=" * 50)

        # Start the match heartbeat (keeps in-progress matches from getting stuck)
        try:
            from services.match_heartbeat import start_heartbeat
            start_heartbeat(app)
        except Exception:
            logger.exception("Failed to start match heartbeat")

        # Start the giveaway scheduler (announces + draws giveaways on time)
        try:
            from services.giveaway_scheduler import start_giveaway_scheduler
            start_giveaway_scheduler(app)
        except Exception:
            logger.exception("Failed to start giveaway scheduler")

        # Schedule periodic tour expiry (every hour)
        try:
            async def _tour_expiry_job(ctx):
                from database import get_session as _gs
                from services.tour_service import expire_old_tours, expire_overdue_invites
                s = _gs()
                try:
                    n1 = expire_overdue_invites(s)
                    n2 = expire_old_tours(s)
                    s.commit()
                    if n1 or n2:
                        logger.info(f"Tour expiry: {n1} invites, {n2} tours")
                except Exception:
                    s.rollback()
                    logger.exception("Tour expiry job failed")
                finally:
                    s.close()
            if app.job_queue:
                app.job_queue.run_repeating(_tour_expiry_job, interval=3600, first=60,
                                             name="tour_expiry")
        except Exception:
            logger.exception("Failed to schedule tour expiry")

        # Schedule periodic stuck-match cleanup (every hour). A match is
        # "stuck" if its status='active' but its MatchState hasn't been
        # touched in 24+ hours (server crashed, user vanished, etc).
        # Pending invites older than 1 hour also get auto-expired.
        try:
            async def _stuck_match_cleanup(ctx):
                from database import get_session as _gs
                from models import Match, MatchState
                from datetime import datetime as _dt, timedelta as _td
                s = _gs()
                try:
                    now = _dt.utcnow()
                    stale_cutoff = now - _td(hours=24)
                    invite_cutoff = now - _td(hours=1)

                    # Pending invites that have been hanging around
                    expired_invites = 0
                    pending = (s.query(Match)
                                .filter(Match.status == "pending",
                                        Match.created_at < invite_cutoff)
                                .all())
                    for m in pending:
                        m.status = "expired"
                        m.completed_at = now
                        expired_invites += 1

                    # Active matches with no MatchState updates in 24h
                    abandoned = 0
                    active = (s.query(Match)
                                .filter(Match.status == "active",
                                        Match.created_at < stale_cutoff)
                                .all())
                    for m in active:
                        # Check MatchState's last_modified
                        ms = (s.query(MatchState)
                                .filter(MatchState.match_id == m.id)
                                .first())
                        if ms is None or ms.last_modified < stale_cutoff:
                            m.status = "abandoned"
                            m.completed_at = now
                            m.margin_type = "abandoned"
                            m.margin_value = 0
                            abandoned += 1

                    if expired_invites or abandoned:
                        s.commit()
                        logger.info(
                            f"Match cleanup: {expired_invites} stale invites "
                            f"expired, {abandoned} stuck matches abandoned")
                except Exception:
                    s.rollback()
                    logger.exception("Stuck-match cleanup job failed")
                finally:
                    s.close()
            if app.job_queue:
                app.job_queue.run_repeating(_stuck_match_cleanup, interval=3600,
                                             first=120, name="stuck_match_cleanup")
        except Exception:
            logger.exception("Failed to schedule stuck-match cleanup")

        # ── Cooldown-ready notifications ──
        # Nudges users when their daily/gspin/claim/free-pack cooldowns are up.
        try:
            async def _cooldown_notify_job(context):
                try:
                    from services.cooldown_notifier import run_cooldown_notifications
                    await run_cooldown_notifications(context.application)
                except Exception:
                    logger.exception("Cooldown notification job failed")
                # Also sweep abandoned Mini-App matches (force-end after timeout)
                try:
                    from database import get_session
                    from services.match_webapp_service import sweep_stale_webapp_matches
                    _s = get_session()
                    try:
                        n = sweep_stale_webapp_matches(_s)
                        if n:
                            logger.info(f"Swept {n} stale Mini-App match(es)")
                    finally:
                        _s.close()
                except Exception:
                    logger.exception("Stale webapp match sweep failed")
            if app.job_queue:
                # Every 5 minutes. First run after 90s (let startup settle).
                app.job_queue.run_repeating(_cooldown_notify_job, interval=300,
                                             first=90, name="cooldown_notifications")
                logger.info("Cooldown notification job scheduled (every 5 min)")
        except Exception:
            logger.exception("Failed to schedule cooldown notifications")

        # ── Monthly season rollover safety net ──
        try:
            async def _season_rollover_job(context):
                try:
                    from database import get_session
                    from services.season_service import ensure_current_season
                    s = get_session()
                    try:
                        ensure_current_season(s)
                        s.commit()
                    finally:
                        s.close()
                except Exception:
                    logger.exception("Season rollover job failed")
            if app.job_queue:
                app.job_queue.run_repeating(_season_rollover_job, interval=3600,
                                             first=150, name="season_rollover")
                logger.info("Season rollover job scheduled (hourly)")
        except Exception:
            logger.exception("Failed to schedule season rollover")

        # ── Fantasy auto-lock ──
        # Locks any open fantasy league whose admin-set lock time (IST) has
        # passed, then broadcasts the lock notice to active groups.
        try:
            async def _fantasy_autolock_job(context):
                try:
                    from database import get_session
                    from services import fantasy_service
                    s = get_session()
                    try:
                        locked = fantasy_service.apply_auto_locks(s)
                        s.commit()
                    finally:
                        s.close()
                    for _name, chat_ids, msg in locked:
                        for cid in chat_ids:
                            try:
                                await context.bot.send_message(
                                    cid, msg, parse_mode="Markdown")
                            except Exception:
                                pass
                except Exception:
                    logger.exception("Fantasy auto-lock job failed")
            if app.job_queue:
                app.job_queue.run_repeating(_fantasy_autolock_job, interval=60,
                                             first=45, name="fantasy_autolock")
                logger.info("Fantasy auto-lock job scheduled (every 60s)")
        except Exception:
            logger.exception("Failed to schedule fantasy auto-lock")

        # Wire up cross-thread bot ref for admin Send-Now button
        try:
            import asyncio as _asyncio
            from admin import set_bot_for_admin
            try:
                loop = _asyncio.get_event_loop()
            except RuntimeError:
                loop = _asyncio.new_event_loop()
            set_bot_for_admin(app.bot, loop)
        except Exception:
            logger.exception("Failed to wire bot for admin notifications")

        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=[
                "message", "callback_query", "chat_member", "my_chat_member",
                "edited_message", "channel_post",
            ],
        )

    except Exception:
        logger.exception("Bot crashed — admin panel still running")
        admin_thread.join()


if __name__ == "__main__":
    main()
