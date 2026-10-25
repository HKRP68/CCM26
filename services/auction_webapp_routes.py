"""Register the auction API with the app's existing Telegram authentication."""

import json
import logging
import threading
import time

from flask import request
from sqlalchemy.exc import IntegrityError, OperationalError

from services import auction_webapp as W
from services import auction_antispam as SPAM
from services import auction_service as A

logger = logging.getLogger(__name__)


def register(app, authenticate, csrf_exempt, member_rpc):
    member_cache = {}
    cache_lock = threading.Lock()

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

    def run(kind):
        if request.content_length and request.content_length > 2_000_000:
            return dict(ok=False, message='Auction requests must be under 2 MB.'), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return dict(ok=False, message='Send a JSON object.'), 400
        auth, tg_id, error = authenticate(allow_not_debuted=True)
        if error:
            return error
        db, user, tg_id = auth
        try:
            from services.miniapp_buttons import auction_launch
            launch = auction_launch(request.headers.get('X-Tg-Start-Param', ''))
            origin = launch['chat_id'] if launch else None
            origin = origin or (getattr(user, 'last_miniapp_chat_id', None) if user else None)
            requested = data.get('season_id') or (launch['season_id'] if launch else None)
            # Creation never needs access to a selected auction.
            if kind == 'action' and data.get('action') == 'admin_create':
                if not W.admin_for(db, tg_id):
                    raise W.AccessError('Only bot admins can create an auction.')
                result = W.perform(db, None, tg_id, data)
                db.commit()
                return dict(ok=True, **result)
            season, rows = W.resolve(db, tg_id, requested, check_member=member, origin_chat_id=origin)
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
            if not W.admin_for(db, tg_id):
                raise W.AccessError('Only bot admins can manage auction setup.')
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
