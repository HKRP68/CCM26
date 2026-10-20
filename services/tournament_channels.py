"""Committed tournament results and ceremonies, one photo/caption per channel.

An outbox retries each destination independently. A Super Over result is one
media group: match summary first (with caption), Super Over scorecard second.
"""
import asyncio
import io
import json
import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def channels(tour):
    try:
        raw = json.loads(tour.channels_json or '[]')
        return raw if isinstance(raw, list) else []
    except (TypeError, ValueError):
        return []


def set_channels(tour, raw):
    values = re.split(r'[\s,;]+', raw.strip()) if isinstance(raw, str) else raw
    result = []
    for value in values or []:
        value = str(value).strip()
        if not value:
            continue
        if re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{4,31}', value):
            value = value.lower()
        elif re.fullmatch(r'-100\d+', value):
            value = str(int(value))
        else:
            raise ValueError(f'Invalid channel {value}. Use @channelname or a -100… channel ID.')
        if value not in result:
            result.append(value)
    tour.channels_json = json.dumps(result)
    return result


def _short(value, limit):
    text = str(value or '—')
    return text if len(text) <= limit else text[:limit-1] + '…'


def _overs(balls):
    balls = int(balls or 0)
    return f'{balls // 6}.{balls % 6}' if balls % 6 else str(balls // 6)


def match_caption(tour, fixture, state, result, *, super_over=None):
    from handlers.cipl_play import _cipl_potm_select
    from services.match_broadcast import play_match_url
    a = state.get('inn1_bat_team') or state.get('bowl_team_name') or 'Team 1'
    b = state.get('bat_team_name') or 'Team 2'
    winner = result.get('winner')
    potm = _cipl_potm_select(state, winner)
    match_id = state.get('match_id') or fixture.match_id
    match_link = play_match_url(match_id, state.get('chat_id')) if match_id else None
    stage = 'FINAL MATCH 🏆' if fixture.stage == 'final' else f'MATCH {fixture.match_no or fixture.id}'
    def build(limit=70, detail=True):
        name = lambda value: _short(value, limit)
        lines = [f'🏏 {name(tour.name)} — {stage}', f'🔥 {name(a)} vs {name(b)} 🔥', '']
        if super_over:
            lines.append('🤝 MATCH TIED — SUPER OVER THRILLER! ⚡')
        elif result.get('tie'):
            lines.append('🤝 MATCH TIED!')
        lines += [f'🔴 {name(a)}: {fixture.inn1_runs}/{fixture.inn1_wickets} ({_overs(fixture.inn1_balls)} Overs)',
                  f'🔴 {name(b)}: {fixture.inn2_runs}/{fixture.inn2_wickets} ({_overs(fixture.inn2_balls)} Overs)']
        if super_over:
            if detail:
                lines += ['', '💥 Scores level after both innings — a nail-biting Super Over!']
            for index, innings in enumerate(super_over.get('so_innings') or []):
                if index % 2 == 0:
                    number = index // 2 + 1
                    lines += ['', '⚡ SUPER OVER RESULT' if number == 1 else f'⚡ SUPER OVER {number} RESULT']
                lines.append(f"🔴 {name(innings['team'])}: {innings['runs']}/{innings['wickets']} ({innings.get('overs', '—')} Overs)")
        if winner:
            phrase = result.get('margin_text') or f"won by {result.get('margin', 0)} {result.get('margin_type', '')}"
            lines += ['', f'🏆 {name(winner).upper()} {phrase.upper()}! 🔥']
        if potm:
            bat, bowl = potm.get('bat') or {}, potm.get('bowl') or {}
            lines += ['', f"⭐ PLAYER OF THE MATCH: {name(potm['name'])}"]
            if bat.get('balls'):
                star = '' if bat.get('out') else '*'
                lines.append(f"🏏 {bat.get('runs', 0)} ({bat['balls']} balls){star} | 💥 SR: {bat.get('runs', 0)*100/bat['balls']:.1f}")
            if bowl.get('wickets'):
                lines.append(f"🎯 {bowl['wickets']}/{bowl.get('runs', 0)}")
        if fixture.stage == 'final' and winner:
            lines += ['', f'👑 {name(winner).upper()} — CHAMPIONS' + (' IN A THRILLER! 🏆' if super_over else '! 🏆')]
        if match_link:
            lines += ['', '📲 Tap to view full match:', match_link]
        return '\n'.join(lines)
    caption = build()
    for limit in (48, 32, 20):
        if len(caption.encode('utf-16-le')) // 2 <= 1024:
            return caption
        caption = build(limit, detail=False)
    return caption


def fallback_card(text):
    """A reliable image fallback; channels never split awards into text-only posts."""
    from PIL import Image, ImageDraw, ImageFont
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    try:
        font = ImageFont.truetype(str(root / 'assets/fonts/Lato-Italic.ttf'), 32)
    except OSError:
        font = ImageFont.load_default(size=30)
    probe = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    lines = []
    for paragraph in text.splitlines():
        current = ''
        for word in paragraph.split():
            candidate = f'{current} {word}'.strip()
            if current and probe.textlength(candidate, font=font) > 1110:
                lines.append(current); current = word
            else:
                current = candidate
        lines.append(current)
    canvas = Image.new('RGB', (1200, max(500, 100+len(lines)*46)), '#101827')
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((20, 20, 1180, canvas.height-20), outline='#f6c65a', width=3)
    for i, line in enumerate(lines):
        draw.text((45, 50+i*46), line, fill='#f6c65a' if i == 0 else '#f4f5fa', font=font)
    output = io.BytesIO(); canvas.save(output, format='PNG')
    return output.getvalue()


def _caption_image(caption, photo):
    """Very long histories live on the image, preserving Telegram's caption limit."""
    if len(caption.encode('utf-16-le')) // 2 <= 1024:
        return caption, photo
    from PIL import Image
    a = Image.open(io.BytesIO(photo)).convert('RGB')
    b = Image.open(io.BytesIO(fallback_card(caption))).convert('RGB')
    width = max(a.width, b.width)
    combined = Image.new('RGB', (width, a.height+b.height), '#101827')
    combined.paste(a, ((width-a.width)//2, 0)); combined.paste(b, ((width-b.width)//2, a.height))
    combined.thumbnail((1600, 7800))
    out = io.BytesIO(); combined.save(out, format='PNG')
    short = '🏏 Tournament result — full summary on the scorecard.'
    _, marker, link = caption.rpartition('\n\n📲 Tap to view full match:\n')
    if marker:
        short += marker + link
    return short, out.getvalue()


def queue(session, tour, event_key, kind, caption, photo, *, extra_photo=None, media_ref=None, parse_mode=None, skip_chat=None):
    from models import TournamentChannelPost
    from sqlalchemy.exc import IntegrityError
    photo = photo or fallback_card(re.sub(r'<[^>]+>', '', caption))
    caption, photo = _caption_image(caption, photo)
    rows = []
    for channel in channels(tour):
        if str(channel) == str(skip_chat):
            continue
        ident = dict(tournament_id=tour.id, event_key=event_key, chat_id=channel)
        existing = session.query(TournamentChannelPost).filter_by(**ident).first()
        if existing:
            rows.append(existing); continue
        try:
            with session.begin_nested():
                row = TournamentChannelPost(**ident, kind=kind, caption=caption, photo=photo,
                    extra_photo=extra_photo, media_ref=media_ref, parse_mode=parse_mode, status='pending', attempts=0)
                session.add(row); session.flush(); rows.append(row)
        except IntegrityError:
            rows.append(session.query(TournamentChannelPost).filter_by(**ident).one())
    return rows


def super_over_image(so):
    """Include every Super Over round, with its own scores and result label."""
    from handlers.super_over import _build_super_over_card
    from PIL import Image
    innings = so.get('so_innings') or []
    cards = []
    for index in range(0, len(innings)-1, 2):
        pair = innings[index:index+2]
        current = dict(so, so_innings=pair, so_number=index//2+1)
        if index+2 < len(innings):
            current.update(_winner_uid=None, _margin_text='Super Over tied')
        try:
            photo = _build_super_over_card(current)
        except Exception:
            logger.exception('Super Over round image failed')
            photo = None
        if not photo:
            text = '\n'.join(f"{d['team']}: {d['runs']}/{d['wickets']} ({d.get('overs', '—')})" for d in pair)
            photo = fallback_card(f'SUPER OVER {index//2+1}\n'+text)
        cards.append(Image.open(io.BytesIO(photo)).convert('RGB'))
    if not cards:
        return fallback_card('SUPER OVER RESULT')
    width = max(c.width for c in cards)
    image = Image.new('RGB', (width, sum(c.height for c in cards)), '#101827')
    offset = 0
    for card in cards:
        image.paste(card, ((width-card.width)//2, offset)); offset += card.height
    image.thumbnail((1600, 7800))
    out = io.BytesIO(); image.save(out, format='PNG')
    return out.getvalue()


async def publish_match(context, state, result, *, photo=None, super_over=None):
    if not state.get('tournament_id'):
        return
    def prepare():
        from database import get_session
        from models import Tournament, TournamentMatch
        session = get_session()
        try:
            tour = session.get(Tournament, state['tournament_id'])
            if tour is None or not channels(tour):
                return []
            fixture = session.query(TournamentMatch).filter_by(tournament_id=tour.id, match_id=state.get('match_id'), status='completed').first()
            if fixture is None:
                return []
            caption = match_caption(tour, fixture, state, result, super_over=super_over)
            image = photo
            if not image:
                try:
                    from handlers.cipl_play import _build_cipl_summary_image
                    image = _build_cipl_summary_image(dict(state), result)
                except Exception:
                    logger.exception('Channel summary render failed for match %s', state.get('match_id'))
            extra = super_over_image(super_over) if super_over else None
            rows = queue(session, tour, f"match:{state['match_id']}", 'match', caption, image, extra_photo=extra)
            ids = [r.id for r in rows]; session.commit(); return ids
        except Exception:
            session.rollback(); raise
        finally:
            session.close()
    try:
        ids = await asyncio.to_thread(prepare)
        if ids:
            await dispatch(context, ids=ids)
    except Exception:
        logger.exception('Tournament channel publication failed for match %s', state.get('match_id'))


async def dispatch(context, *, ids=None):
    from database import get_session
    from models import Tournament, TournamentHonour, TournamentChannelPost as Post
    from services.tournament_awards import CEREMONY_AWARDS
    from sqlalchemy import or_
    from telegram import InputMediaPhoto
    from telegram.error import BadRequest
    def claim():
        session = get_session()
        try:
            now = datetime.utcnow()
            session.query(Post).filter(Post.status == 'sending', Post.claimed_at < now-timedelta(minutes=5)).update({'status':'pending'}, synchronize_session=False)
            query = session.query(Post).filter(Post.status == 'pending', or_(Post.next_attempt_at.is_(None), Post.next_attempt_at <= now)).order_by(Post.id)
            if ids is not None:
                query = query.filter(Post.id.in_(ids))
            items = []
            for row in query.limit(50):
                tour = session.get(Tournament, row.tournament_id)
                obsolete = False
                if row.kind == 'ceremony' and row.event_key.startswith('award:'):
                    honour = session.get(TournamentHonour, int(row.event_key.split(':', 1)[1]))
                    obsolete = honour is None or honour.award not in CEREMONY_AWARDS
                if not tour or row.chat_id not in channels(tour) or obsolete:
                    row.status = 'cancelled'; row.photo = b''; row.extra_photo = None; row.media_ref = None
                    continue
                if row.kind == 'ceremony' and session.query(Post.id).filter(Post.tournament_id == row.tournament_id,
                    Post.chat_id == row.chat_id, Post.kind == 'ceremony', Post.id < row.id, Post.status.in_(('pending','sending'))).first():
                    continue
                claimed = session.query(Post).filter_by(id=row.id, status='pending').update({'status':'sending', 'claimed_at':now}, synchronize_session=False)
                if claimed:
                    items.append(dict(id=row.id, chat_id=row.chat_id, caption=row.caption, photo=row.photo,
                        extra=row.extra_photo, ref=row.media_ref, parse_mode=row.parse_mode))
            session.commit(); return items
        finally:
            session.close()
    def finish(item, messages=None, error=None):
        session = get_session()
        try:
            row = session.get(Post, item['id'])
            if row:
                row.attempts += 1
                row.last_error = str(error)[:500] if error else None
                if error:
                    row.status = 'pending'
                    row.next_attempt_at = datetime.utcnow() + timedelta(seconds=min(900, 30*2**min(row.attempts, 5)))
                else:
                    row.status = 'sent'; row.sent_at = datetime.utcnow()
                    row.message_ids_json = json.dumps([getattr(m, 'message_id', None) for m in messages or []])
                    # Media is only needed while delivery is pending. Official
                    # match cards remain in the existing scorecard archive.
                    row.photo = b''; row.extra_photo = None; row.media_ref = None
            session.commit()
        finally:
            session.close()
    for item in await asyncio.to_thread(claim):
        chat_id = int(item['chat_id']) if item['chat_id'].startswith('-') else item['chat_id']
        try:
            if item['extra']:
                messages = await context.bot.send_media_group(chat_id=chat_id, media=[
                    InputMediaPhoto(io.BytesIO(item['photo']), caption=item['caption'], parse_mode=item['parse_mode']),
                    InputMediaPhoto(io.BytesIO(item['extra']))])
            else:
                try:
                    message = await context.bot.send_photo(chat_id=chat_id, photo=item['ref'] or io.BytesIO(item['photo']),
                        caption=item['caption'], parse_mode=item['parse_mode'])
                except BadRequest:
                    if not item['ref']:
                        raise
                    message = await context.bot.send_photo(chat_id=chat_id, photo=io.BytesIO(item['photo']),
                        caption=item['caption'], parse_mode=item['parse_mode'])
                messages = [message]
            await asyncio.to_thread(finish, item, messages=messages)
        except Exception as exc:
            await asyncio.to_thread(finish, item, error=exc)
            logger.warning('Channel post %s failed: %s', item['id'], exc)


async def publication_job(context):
    try:
        await dispatch(context)
    except Exception:
        logger.exception('Tournament channel delivery job failed')


def queue_fixture_result(session, fixture):
    """Manual/deadline results share the same destinations after their commit."""
    from models import Tournament, TournamentTeam
    tour = session.get(Tournament, fixture.tournament_id)
    if not tour or not channels(tour):
        return []
    a = session.get(TournamentTeam, fixture.team1_id)
    b = session.get(TournamentTeam, fixture.team2_id)
    winner = session.get(TournamentTeam, fixture.winner_team_id) if fixture.winner_team_id else None
    state = dict(inn1_bat_team=a.name if a else 'Team 1', bat_team_name=b.name if b else 'Team 2')
    chasing = fixture.winner_team_id == fixture.team2_id
    margin = max(0, 10-int(fixture.inn2_wickets or 0)) if chasing else max(0, int(fixture.inn1_runs or 0)-int(fixture.inn2_runs or 0))
    result = dict(tie=not winner, winner=winner.name if winner else None, margin=margin,
                  margin_type='wickets' if chasing else 'runs')
    caption = match_caption(tour, fixture, state, result)
    photo = None
    try:
        from services.scorecard_delivery import render_card, CARD_SUMMARY
        photo = render_card(CARD_SUMMARY, dict(inn1_team=state['inn1_bat_team'], inn2_team=state['bat_team_name'],
            inn1_runs=fixture.inn1_runs, inn1_wickets=fixture.inn1_wickets, inn1_overs=_overs(fixture.inn1_balls),
            inn2_runs=fixture.inn2_runs, inn2_wickets=fixture.inn2_wickets, inn2_overs=_overs(fixture.inn2_balls),
            winner_name=result['winner'] or 'Match Tied', win_margin_text=fixture.result_text or '',
            overs_total=tour.overs, tournament_id=tour.id))
    except Exception:
        logger.exception('Manual match summary render failed for fixture %s', fixture.id)
    return queue(session, tour, f'fixture:{fixture.id}', 'match', caption, photo)
