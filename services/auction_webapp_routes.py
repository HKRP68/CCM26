"""Register the auction API with the app's existing Telegram authentication."""

import json
import logging
import threading
import time

from flask import Response, request, stream_with_context
from sqlalchemy.exc import IntegrityError, OperationalError

from services import auction_webapp as W
from services import auction_antispam as SPAM
from services import auction_service as A

logger = logging.getLogger(__name__)


def register(app, authenticate, csrf_exempt, member_rpc):
    member_cache = {}
    cache_lock = threading.Lock()
    from services import auction_updates as U
    stream_slots = U.StreamSlots()

    def member(chat_id, tg_id):
        key = (chat_id, tg_id)
        now = time.monotonic()
        with cache_lock:
            hit = member_cache.get(key)
        if hit and now - hit[1] < 60:
            return hit[0]
        # This is access to an auction, so an unavailable membership check
        # fails closed. The activity echo helper intentionally fails open.
        try:
            result = member_rpc('getChatMember', params={'chat_id': chat_id, 'user_id': tg_id}, timeout=4)
        except Exception:
            logger.warning('Auction membership check unavailable', exc_info=True)
            result = None
        row = (result or {}).get('result') or {}
        allowed = bool((result or {}).get('ok') and
                       (row.get('status') in ('creator', 'administrator', 'member')
                        or (row.get('status') == 'restricted' and row.get('is_member') is True)))
        with cache_lock:
            if len(member_cache) > 5000:
                member_cache.clear()
            member_cache[key] = (allowed, now)
        return allowed

    def updates():
        if request.content_length and request.content_length > 2_000_000:
            return dict(ok=False, message='Auction requests must be under 2 MB.'), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return dict(ok=False, message='Send a JSON object.'), 400
        if request.headers.get('Authorization', '').removeprefix('tma ').startswith('DEV_'):
            return dict(ok=False, error='bad_init_data',
                        message='Shared auctions require a verified Telegram login.'), 401

        last_authorized = 0.0
        current_identity = None
        selected_season = data.get('season_id')
        previous_authority = None

        def authorize():
            nonlocal last_authorized, current_identity, selected_season, previous_authority
            auth, tg_id, error = authenticate(allow_not_debuted=True)
            if error:
                payload, status = error
                raise U.AccessChanged(payload, status)
            db, user, tg_id = auth
            try:
                from services.miniapp_buttons import auction_launch
                launch = auction_launch(request.headers.get('X-Tg-Start-Param', ''))
                origin = launch['chat_id'] if launch else None
                origin = origin or (getattr(user, 'last_miniapp_chat_id', None) if user else None)
                requested = selected_season or (launch['season_id'] if launch else None)
                season, _rows = W.resolve(db, tg_id, requested, check_member=member,
                                         origin_chat_id=origin)
                selected_season = season.id if season else None
                current_identity = tg_id
                state = U.snapshot(db, selected_season, tg_id)
                previous_authority = state.authority
                last_authorized = time.monotonic()
                return state
            except W.AccessError as exc:
                raise U.AccessChanged(dict(ok=False, error='forbidden', message=str(exc)), 403)
            except (A.AuctionError, ValueError, TypeError) as exc:
                raise U.AccessChanged(dict(ok=False, error='invalid_input', message=W.plain(str(exc))), 400)
            finally:
                db.close()

        def read_snapshot():
            if time.monotonic() - last_authorized >= U.REAUTHORIZE_SECONDS:
                return authorize()
            from database import get_session
            db = get_session()
            try:
                state = U.snapshot(db, selected_season, current_identity)
            finally:
                db.close()
            # A manager/owner loses or gains authority on the next probe. Fresh
            # payload requests independently enforce their current permissions.
            return authorize() if state.authority != previous_authority else state

        try:
            initial = authorize()
        except U.AccessChanged as exc:
            return exc.payload, exc.status
        except Exception:
            logger.exception('Auction updates could not start')
            return dict(ok=False, error='unavailable', message='Live updates are temporarily unavailable.'), 503

        try:
            limit = max(0, min(64, int(app.config.get('AUCTION_STREAM_MAX_CLIENTS', 2))))
        except (TypeError, ValueError):
            limit = 2
        if not stream_slots.acquire(limit):
            return Response(json.dumps(dict(ok=False, error='stream_capacity',
                                             message='Use automatic refresh while live updates reconnect.')),
                            status=503, mimetype='application/json', headers={'Retry-After': '10'})
        released = False
        release_lock = threading.Lock()

        def release():
            nonlocal released
            with release_lock:
                if not released:
                    released = True
                    stream_slots.release()

        def generate():
            try:
                yield from U.notifications(read_snapshot, initial)
            except Exception:
                logger.exception('Auction updates interrupted')
                yield U.encode('access', dict(ok=False, status=503, error='unavailable',
                                             message='Live updates interrupted. Refresh before bidding.'))
            finally:
                release()

        response = Response(stream_with_context(generate()), mimetype='text/event-stream',
                            headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
        response.call_on_close(release)
        return response

    app.add_url_rule('/api/webapp/auction/updates', 'webapp_auction_updates',
                     csrf_exempt(updates), methods=['POST'])

    def run(kind):
        if request.content_length and request.content_length > 2_000_000:
            return dict(ok=False, message='Auction requests must be under 2 MB.'), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return dict(ok=False, message='Send a JSON object.'), 400
        identity = request.headers.get('Authorization', '').removeprefix('tma ')
        if identity.startswith('DEV_'):
            return dict(ok=False, error='bad_init_data',
                        message='Shared auctions require a verified Telegram login.'), 401
        auth, tg_id, error = authenticate(allow_not_debuted=True)
        if error:
            return error
        db, user, tg_id = auth
        try:
            from services.miniapp_buttons import auction_launch
            launch = auction_launch(request.headers.get('X-Tg-Start-Param', ''))
            origin = launch['chat_id'] if launch else None
            origin = origin or (getattr(user, 'last_miniapp_chat_id', None) if user else None)
            mode = data.get('mode') if kind == 'read' else None
            requested = data.get('season_id') or (launch['season_id'] if launch and mode is None else None)
            # Creation never needs access to a selected auction.
            if kind == 'action' and data.get('action') == 'admin_create':
                if not W.admin_for(db, tg_id):
                    raise W.AccessError('Only Auction Managers can create an auction.')
                result = W.perform(db, None, tg_id, data)
                db.commit()
                return dict(ok=True, **result)
            season, rows = W.resolve(db, tg_id, requested, check_member=member, origin_chat_id=origin, acquisition_mode=mode)
            if kind == 'read':
                result = W.payload(db, season, tg_id, rows, data)
                # trade_squad materializes last season's unsigned retention
                # rights as queued lots. Persist these before returning their
                # ids, so release/trade buttons refer to real rows. This
                # neither signs players nor moves money.
                if data.get('view') in ('preseason', 'trades'):
                    db.commit()
                return result
            if season is None:
                raise A.AuctionError('Choose an auction first.')
            if kind == 'action':
                result = W.perform(db, season, tg_id, data)
                db.commit()
                return dict(ok=True, **result)
            if not W.admin_for(db, tg_id, season):
                raise W.AccessError('Only Auction Managers can manage auction setup.')
            if kind == 'catalogue':
                from models import Player
                from services import player_query
                query = player_query.master_player_query(db)
                text = str(data.get('query', ''))[:100].replace('%', '').replace('_', '')
                if text:
                    query = query.filter(Player.name.ilike('%' + text + '%'))
                rows = query.order_by(Player.rating.desc(), Player.name).limit(60).all()
                from models import AuctionLot
                lots = {l.player_id: l for l in db.query(AuctionLot).filter(
                    AuctionLot.season_id == season.id, AuctionLot.player_id.in_([p.id for p in rows])).all()}
                return dict(ok=True, players=[dict(id=p.id, name=p.name, rating=p.rating,
                                                    role=p.category, version=p.version, country=p.country,
                                                    status=lots[p.id].status if p.id in lots else 'not_in_pool',
                                                    base_lakh=lots[p.id].base_price_lakh if p.id in lots
                                                    else A.base_price_for(season, p.rating),
                                                    available=p.id not in lots or lots[p.id].status in
                                                    (A.LOT_QUEUED, A.LOT_UNSOLD)) for p in rows])
            if kind == 'export':
                if data.get('format') == 'franchises':
                    content = json.dumps(A.export_franchises(db, season), indent=2, ensure_ascii=False)
                    name = f'auction-{season.id}-franchises.json'
                else:
                    from services.auction_sets_io import export_sets_json
                    content = export_sets_json(db, season).decode('utf-8')
                    name = f'auction-{season.id}-sets.json'
                return dict(ok=True, filename=name, content=content)
        except SPAM.RateLimited as exc:
            db.rollback()
            return dict(ok=False, error='rate_limited', message=str(exc),
                        retry_after=max(1, int(exc.verdict.wait + 0.999))), 429
        except W.AccessError as exc:
            db.rollback()
            return dict(ok=False, error='forbidden', message=str(exc)), 403
        except A.PriceMoved as exc:
            db.rollback()
            return dict(ok=False, error='stale', message=str(exc)), 409
        except A.AuctionError as exc:
            db.rollback()
            return dict(ok=False, error='auction_rule', message=W.plain(str(exc))), 400
        except (ValueError, TypeError, KeyError) as exc:
            db.rollback()
            logger.info('Invalid auction Mini App input: %s', type(exc).__name__)
            return dict(ok=False, error='invalid_input', message='Check the supplied fields and try again.'), 400
        except IntegrityError:
            db.rollback()
            return dict(ok=False, error='conflict', message='Another action changed this auction. Refresh and try again.'), 409
        except OperationalError:
            db.rollback()
            logger.warning('Auction Mini App database busy', exc_info=True)
            return dict(ok=False, error='busy', message='The auction is busy. Refresh before retrying.'), 503
        except Exception:
            db.rollback()
            logger.exception('Auction Mini App request failed')
            return dict(ok=False, error='internal', message='Could not complete this auction request. Refresh and try again.'), 500
        finally:
            db.close()

    for suffix, kind in (('', 'read'), ('/action', 'action'), ('/catalogue', 'catalogue'), ('/export', 'export')):
        def endpoint(kind=kind):
            result = app.make_response(run(kind))
            result.headers['Cache-Control'] = 'no-store'
            return result
        app.add_url_rule('/api/webapp/auction' + suffix, 'webapp_auction_' + kind,
                         csrf_exempt(endpoint), methods=['POST'])
