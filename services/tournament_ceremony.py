"""Admin-triggered, resumable award presentations, one photo per honour."""
import asyncio
import io
import logging
from datetime import datetime, timedelta
from html import escape

logger = logging.getLogger(__name__)


def stat_for_honour(session, honour):
    from models import TournamentPlayerStats
    if not honour.player_name or honour.award == "winning_captain":
        return None
    return session.query(TournamentPlayerStats).filter_by(
        tournament_id=honour.tournament_id, name=honour.player_name,
        team_name=honour.team_name).first()


def contribution(session, honour):
    from models import TournamentTeam
    row = stat_for_honour(session, honour)
    if row:
        sr = row.bat_runs * 100 / row.bat_balls if row.bat_balls else 0
        return f'{row.matches} matches · {row.bat_runs} runs · SR {sr:.1f} · {row.bowl_wickets} wickets'
    team = session.query(TournamentTeam).filter_by(tournament_id=honour.tournament_id, name=honour.team_name).first()
    if team:
        return f'{team.played or 0} league matches · {team.won or 0} wins · {team.points or 0} points'
    return honour.value or ''


def award_caption(session, tour, honour):
    from services.tournament_awards import AWARD_LABEL, prize_text
    prize = prize_text({'coins': honour.prize_coins, 'gems': honour.prize_gems}) or 'No prize configured'
    lines = [f'🎉 <b>{escape(tour.name)} — Awards Ceremony</b>',
             f"<b>{AWARD_LABEL.get(honour.award, honour.award)}</b>",
             f"<b>{escape(honour.player_name or honour.team_name or 'Not awarded')}</b>"]
    if honour.player_name:
        lines.append(f'Team: {escape(honour.team_name or "—")}')
    if honour.value:
        lines.append(escape(honour.value))
    if honour.player_name or honour.team_name:
        lines.append(escape(contribution(session, honour)))
    lines.append(f'💰 {prize}')
    return '\n'.join(lines)[:1024]


def award_photo(session, tour, honour, *, use_team_photo=True):
    """Use the team's supplied photo, or compose its crest / player's card."""
    from PIL import Image, ImageDraw, ImageFont
    from models import TournamentTeam
    from services.tournament_awards import AWARD_LABEL, player_for_stats
    team = session.query(TournamentTeam).filter_by(tournament_id=tour.id, name=honour.team_name).first()
    source = None
    if (not honour.player_name or honour.award == "winning_captain") and team:
        from urllib.parse import urlsplit
        from services.card_identity import _event_logo_bytes
        for url in (team.award_photo_url, team.logo_url):
            if not url:
                continue
            parsed = urlsplit(url)
            if parsed.scheme in ('http', 'https') and parsed.netloc:
                if use_team_photo:
                    return url
            elif parsed.path.startswith('/static/challenge_leagues/'):
                # Website uploads are local asset paths, not Telegram file IDs
                # or public URLs. The shared loader restores missing uploads.
                source = _event_logo_bytes(parsed.path)
                if source:
                    break
    if honour.player_name and honour.award != "winning_captain":
        stat = stat_for_honour(session, honour)
        player = player_for_stats(session, tour, stat) if stat else None
        if player:
            from services.card_generator import generate_card
            source = generate_card(player)
    if source is None:
        from services.team_logo_service import logo_png_for_team_name
        source = logo_png_for_team_name(session, honour.team_name)
    # A photo remains available for imported players without a master card.
    canvas = Image.new('RGB', (1000, 1120), '#101827')
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 32)
        title_font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 46)
    except OSError:
        font = title_font = ImageFont.load_default()
    def line(text, y, chosen=font, color='#f4f5fa'):
        # Keep labels inside the frame; the caption retains the complete text.
        while draw.textlength(text, font=chosen) > 920 and len(text) > 6:
            text = text[:-2]
        draw.text((500, y), text, anchor='mt', font=chosen, fill=color)
    line('AWARDS CEREMONY', 35, title_font, '#f6c65a')
    line(tour.name or 'Tournament', 100)
    if source:
        art = Image.open(io.BytesIO(source)).convert('RGBA')
        art.thumbnail((850, 690))
        canvas.paste(art, ((1000-art.width)//2, 170+(690-art.height)//2), art)
    else:
        line(honour.player_name or honour.team_name or 'Not awarded', 430, title_font)
    # Strip emoji from the photo label; the caption keeps the original.
    label = AWARD_LABEL.get(honour.award, honour.award).split(' ', 1)[-1]
    line(label, 885, title_font, '#f6c65a')
    line(honour.player_name or honour.team_name or '', 952)
    if honour.player_name:
        line(honour.team_name or '', 1000)
    line(contribution(session, honour), 1060)
    out = io.BytesIO()
    canvas.save(out, format='PNG')
    return out.getvalue()


def claim(session, tour):
    """Lease the ceremony across processes; successful awards persist individually."""
    from models import Tournament
    from sqlalchemy import or_
    now = datetime.utcnow()
    changed = session.query(Tournament).filter(
        Tournament.id == tour.id, Tournament.awards_announced_at.is_(None),
        or_(Tournament.ceremony_claimed_at.is_(None),
            Tournament.ceremony_claimed_at < now - timedelta(minutes=10))).update(
        {'ceremony_claimed_at': now}, synchronize_session=False)
    session.commit()
    return bool(changed)


async def present(context, tournament_id, chat_id):
    from database import get_session
    from models import Tournament, TournamentHonour
    from services import tournament_awards as TA
    def prepare():
        session = get_session()
        try:
            tour = session.get(Tournament, tournament_id)
            TA.finalize(session, tour)
            session.commit()
            TA.ensure_ceremony_honours(session, tour)
            session.commit()
            honours = TA.ceremony_honours(session, tour.id)
            if not honours:
                raise ValueError('Finish the tournament final before starting the ceremony.')
            if tour.awards_announced_at:
                if all(h.announced_at is not None for h in honours):
                    raise ValueError('This tournament’s awards ceremony has already finished.')
                # A previous version presented fewer award categories. Newly
                # added awards can be presented without reposting the old ones.
                tour.awards_announced_at = None
                session.commit()
            ids = [h.id for h in honours if h.announced_at is None]
            if not claim(session, tour):
                raise ValueError('This tournament’s awards ceremony is already running.')
            return ids
        finally:
            session.close()
    ids = await asyncio.to_thread(prepare)
    def payload(hid, use_team_photo=True):
        session = get_session()
        try:
            tour = session.get(Tournament, tournament_id)
            honour = session.get(TournamentHonour, hid)
            caption = award_caption(session, tour, honour)
            try:
                photo = award_photo(session, tour, honour, use_team_photo=use_team_photo)
            except Exception:
                # Missing/corrupt art or a renderer failure must not block the
                # same award on every resume. The complete caption still works.
                logger.exception('Award image unavailable for honour %s', hid)
                photo = None
            return caption, photo
        finally:
            session.close()
    def mark(hid=None, done=False, caption=None, photo=None, media_ref=None):
        session = get_session()
        try:
            tour = session.get(Tournament, tournament_id)
            ids = []
            if hid:
                session.get(TournamentHonour, hid).announced_at = datetime.utcnow()
                tour.ceremony_claimed_at = datetime.utcnow()
                from services import tournament_channels as TC
                if TC.channels(tour):
                    fallback = photo if isinstance(photo, bytes) else TC.fallback_card(__import__('re').sub(r'<[^>]+>', '', caption or 'Award Ceremony'))
                    ids = [r.id for r in TC.queue(session, tour, f'award:{hid}', 'ceremony', caption or '', fallback,
                        media_ref=media_ref or (photo if isinstance(photo, str) else None), parse_mode='HTML', skip_chat=chat_id)]
            else:
                tour.ceremony_claimed_at = None
            if done:
                tour.awards_announced_at = datetime.utcnow()
            session.commit()
            return ids
        finally:
            session.close()
    from telegram.error import BadRequest
    completed = False
    try:
        for index, hid in enumerate(ids):
            caption, photo = await asyncio.to_thread(payload, hid)
            delivered = False
            sent = None
            if photo is not None:
                try:
                    sent = await context.bot.send_photo(chat_id=chat_id,
                        photo=io.BytesIO(photo) if isinstance(photo, bytes) else photo,
                        caption=caption, parse_mode='HTML')
                    delivered = True
                except BadRequest:
                    logger.warning('Telegram rejected image for honour %s', hid, exc_info=True)
                    if isinstance(photo, str):
                        # An expired or inaccessible external photo falls back
                        # to an uploaded award card, without reusing the URL.
                        _, fallback = await asyncio.to_thread(payload, hid, False)
                        if isinstance(fallback, bytes):
                            try:
                                sent = await context.bot.send_photo(chat_id=chat_id,
                                    photo=io.BytesIO(fallback), caption=caption, parse_mode='HTML')
                                delivered = True
                            except BadRequest:
                                logger.warning('Telegram rejected fallback image for honour %s', hid, exc_info=True)
            if not delivered:
                await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode='HTML')
            media_ref = None
            if sent and getattr(sent, 'photo', None):
                media_ref = getattr(sent.photo[-1], 'file_id', None)
            channel_ids = await asyncio.to_thread(mark, hid, caption=caption, photo=photo, media_ref=media_ref)
            from services.tournament_channels import dispatch
            try:
                if channel_ids:
                    await dispatch(context, ids=channel_ids)
            except Exception:
                logger.exception('Ceremony channel delivery queued for retry')
            if index < len(ids) - 1:
                await asyncio.sleep(2)
        completed = True
    finally:
        # Release on ordinary errors and task cancellation. A failed delivery
        # remains unannounced, so the command can immediately retry it.
        try:
            await asyncio.to_thread(mark, done=completed)
        except Exception:
            if completed:
                raise
            logger.exception('Could not release ceremony lease for tournament %s', tournament_id)
