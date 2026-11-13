"""Explicit, manager-only CSV upload flow for the dedicated auction bot."""
import logging
import time

from telegram.ext import ApplicationHandlerStop, CommandHandler, MessageHandler, filters

from database import get_session
from services import auction_service as A
from services import rich_message as R, franchise_draft_rich as DR
from services.auction_async import AuctionWorkerBusy, BUSY_MESSAGE, run_session
from services.franchise_player_import import MAX_BYTES, import_players, parse_csv

logger = logging.getLogger(__name__)
_PENDING = 'franchise_player_import'
_BUSY = 'franchise_player_import_busy'
_MAX_AGE = 15 * 60


async def _reply(message, text):
    blocks,fallback=DR.Card('📥 Player import').text(str(text)).pair()
    return await R.reply_rich(message,blocks,fallback)


def _scope(db, actor_id, chat_id, expected_season=None):
    season = A.season_for_chat(db, chat_id)
    if season is None:
        raise A.AuctionError('Create and bind an auction in this group before importing players.')
    A.require_season_manager(db, season, actor_id)
    if expected_season is not None and season.id != expected_season:
        raise A.AuctionError('This group now has a different auction. Start /importplayers again.')
    if season.status not in (A.STATUS_SETUP, A.STATUS_PAUSED):
        raise A.AuctionError('Import players during setup or while the auction is paused.')
    return season


def _import_scope(db, actor_id, chat_id):
    from services import franchise_draft as D
    season=_scope(db,actor_id,chat_id)
    return season.id,D.get_draft(db,season) is not None


async def importplayers_handler(update, context):
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or chat.type not in ('group', 'supergroup'):
        await _reply(update.effective_message, 'Use /importplayers in the auction group after creating and binding its season.')
        return
    if context.user_data.get(_BUSY):
        await _reply(update.effective_message, 'Your current player import is processing. Wait for its result before starting another.')
        return
    set_name = ' '.join(context.args or []).strip() or 'Imported'
    if len(set_name) > 40 or any(ord(c) < 32 for c in set_name):
        await _reply(update.effective_message, 'Use a player set name of at most 40 characters.')
        return
    try:
        season_id,is_draft = await run_session(get_session, lambda db: _import_scope(db, user.id, chat.id))
    except AuctionWorkerBusy:
        await _reply(update.effective_message, BUSY_MESSAGE)
        return
    except A.AuctionError as exc:
        await _reply(update.effective_message, str(exc))
        return
    context.user_data[_PENDING] = dict(chat_id=chat.id, season_id=season_id,
                                       set_name=set_name, draft=is_draft, started_at=time.time())
    card=DR.Card('📥 Import draft players' if is_draft else '📥 Import auction players')
    card.text('Send the player CSV here as a document (UTF-8, under 2 MB, up to 2,000 rows).')
    card.facts([('Player set',set_name),('Roles','BAT · BOWL · AR · WK')])
    card.section('CSV columns')
    card.text(['Required headers: ',R.code('name,rating,role,country')])
    card.text(['Optional: ',R.code('version,base_lakh,bat_rating,bowl_rating,overseas')])
    card.text('base_lakh uses integer lakh: 100 = 1 Cr. Blank base prices use the auction rules.')
    card.text('When batting or bowling ratings are omitted, new cards use the overall rating for those game attributes.')
    card.text('Existing catalogue players are reused; conflicting definitions are refused.')
    card.footer('After importing: /dprices sets signing prices · /dcustom overrides a player · No overseas limit' if is_draft
                else 'Overseas is derived from the auction home country. /asets reviews player sets.')
    card.footer('/cancel leaves this import · Your upload must be sent in this group')
    blocks,text=card.pair()
    await R.reply_rich(update.effective_message,blocks,text)


async def player_document_router(update, context):
    pending = context.user_data.get(_PENDING)
    chat, user, message = update.effective_chat, update.effective_user, update.effective_message
    if (not isinstance(pending, dict) or chat is None or user is None or message is None
            or pending.get('chat_id') != chat.id):
        return
    if context.user_data.get(_BUSY):
        await _reply(message, 'This player import is already processing. Wait for its result; do not resend the file.')
        raise ApplicationHandlerStop
    text = (getattr(message, 'text', None) or '').strip()
    if text.split('@', 1)[0].lower() == '/cancel':
        context.user_data.pop(_PENDING, None)
        await _reply(message, 'Player import cancelled.')
        raise ApplicationHandlerStop
    document = getattr(message, 'document', None)
    if document is None:
        return
    if time.time() - pending.get('started_at', 0) > _MAX_AGE:
        context.user_data.pop(_PENDING, None)
        await _reply(message, 'This import expired. Use /importplayers again.')
        raise ApplicationHandlerStop
    filename = (getattr(document, 'file_name', None) or '').lower()
    if not filename.endswith('.csv'):
        await _reply(message, 'Send a .csv document for this import, or use /cancel.')
        raise ApplicationHandlerStop
    size = getattr(document, 'file_size', None)
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_BYTES:
        await _reply(message, 'The player CSV must have a known size under 2 MB. Send it again as a CSV document.')
        raise ApplicationHandlerStop
    context.user_data[_BUSY] = True
    try:
        try:
            # Permission/phase checks happen before downloading and again inside
            # the write transaction; an old pending prompt never grants authority.
            await run_session(get_session, lambda db: _scope(db, user.id, chat.id, pending['season_id']).id)
            file = await document.get_file()
            raw = bytes(await file.download_as_bytearray())
            rows = parse_csv(raw)
            def apply(db):
                season = _scope(db, user.id, chat.id, pending['season_id'])
                return import_players(db, season, rows, actor_tg_id=user.id, set_name=pending['set_name'])
            result = await run_session(get_session, apply, commit=True)
        except AuctionWorkerBusy:
            await _reply(message, BUSY_MESSAGE)
            raise ApplicationHandlerStop
        except A.AuctionError as exc:
            await _reply(message, str(exc))
            raise ApplicationHandlerStop
        except Exception:
            # Do not log token-bearing Telegram file URLs or exception payloads.
            logger.warning('Dedicated bot player CSV import could not complete')
            await _reply(message, 'The import could not complete. Nothing from a failed transaction is saved. Retry the CSV or use /cancel.')
            raise ApplicationHandlerStop
        context.user_data.pop(_PENDING, None)
        card=DR.Card('✅ Player import complete')
        card.facts([('Added to this draft' if pending.get('draft') else 'Added to this auction',str(result['added'])),
                    ('Already in the pool',str(result['skipped'])),('New catalogue cards',str(result['catalogue_created'])),
                    ('Base prices updated',str(result['prices_changed']))])
        card.footer('Review /dpool and configure /dprices before starting.' if pending.get('draft')
                    else 'Use /asets to review the player sets.')
        blocks,text=card.pair()
        await R.reply_rich(message,blocks,text)
        raise ApplicationHandlerStop

    finally:
        context.user_data.pop(_BUSY, None)


def register(app):
    app.add_handler(CommandHandler('importplayers', importplayers_handler))
    # A separate earlier group lets unrelated documents/cancel commands reach
    # the bot's normal wizard and legacy handlers when no import is pending.
    app.add_handler(MessageHandler(filters.Document.ALL | filters.Regex(r'(?i)^/cancel(?:@\w+)?\s*$'),
                                   player_document_router), group=-2)
