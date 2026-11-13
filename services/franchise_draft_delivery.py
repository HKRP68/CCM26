"""Restart-safe draft announcements and the draft's one pinned status board.

Picks commit before this module talks to Telegram. The durable event cursor
advances only after a successful announcement, including the final pick when
the season is already completed.
"""
import html
from html.parser import HTMLParser
import json
import logging

from services import rich_message as R

logger = logging.getLogger(__name__)


def blocks_for_html(text):
    """Render the draft's HTML content as rich text, preserving user mentions.

    Both paths consume the same escaped display text and links, so a price or
    next-team mention cannot drift between rich messages and the HTML fallback.
    Only the formatting emitted by the draft renderers is interpreted.
    """
    class RichParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.root = []
            self.stack = [(None, self.root)]

        def handle_starttag(self, tag, attrs):
            styles = {'b': 'bold', 'strong': 'bold', 'i': 'italic',
                      'em': 'italic', 'u': 'underline', 's': 'strikethrough',
                      'strike': 'strikethrough', 'del': 'strikethrough',
                      'code': 'code', 'pre': 'code'}
            if tag == 'br':
                self.handle_data('\n')
            elif tag in styles or tag == 'a':
                children = []
                if tag == 'a':
                    url = dict(attrs).get('href')
                    if not url:
                        return
                    node = R.link(children, url)
                else:
                    node = {'type': styles[tag], 'text': children}
                self.stack[-1][1].append(node)
                self.stack.append((tag, children))

        def handle_endtag(self, tag):
            if len(self.stack) > 1 and self.stack[-1][0] == tag:
                self.stack.pop()

        def handle_data(self, data):
            if data:
                self.stack[-1][1].append(data)

    parser = RichParser()
    parser.feed(str(text or ''))
    parser.close()
    return [R.paragraph(parser.root)] if parser.root else []


async def drain_events(bot, session, season, *, limit=5):
    """Deliver committed draft events in order; retry failed sends next tick."""
    from services import auction_scheduler as S, auction_service as A

    if not season.chat_id:
        return 0
    spoken = 0
    for event in A.pending_events(session, season, limit=max(1, limit) * 20):
        if str(event.kind or '').startswith('fd_'):
            if spoken >= limit:
                break
            try:
                detail = json.loads(event.detail_json or '{}')
            except (ValueError, TypeError):
                detail = {}
            text = detail.get('html') if isinstance(detail, dict) else None
            text = text or html.escape(event.headline or 'Draft updated.')
            sent = await S._send_rich(bot, season.chat_id,
                                      blocks_for_html(text), text)
            if sent is None:
                break
            spoken += 1
        # Auction setup changes are reflected on the board. They must not be
        # replayed as auction announcements in a draft room.
        season.announced_event_id = event.id
        session.commit()
    return spoken


async def refresh_board(bot, session, season, *, force=False):
    """Edit the saved board, replacing it if deleted or no longer editable."""
    from services import auction_scheduler as S, auction_rich as AR
    from services import franchise_draft as D

    draft = D.get_draft(session, season)
    if draft is None or not season.chat_id:
        return None
    revision = int(draft.revision or 0)
    if (not force and season.board_message_id
            and int(season.board_rendered_bid_count or 0) == revision):
        return season.board_message_id
    text = D.render_board(session, season)
    blocks = blocks_for_html(text)
    previous_id = season.board_message_id
    if previous_id:
        # Rich and HTML edits share the same content. "Not modified" is a
        # successful retry in both paths.
        ok = await AR.edit(bot, season.chat_id, previous_id, blocks,
                           html_text=text)
        if ok:
            season.board_rendered_bid_count = revision
            return previous_id
    sent = await S._send_rich(bot, season.chat_id, blocks, text)
    if sent is None:
        return None
    season.board_message_id = sent.message_id
    season.board_lot_id = None
    season.board_rendered_bid_count = revision
    await S.pin_board(bot, session, season, sent, previous_id=previous_id)
    return season.board_message_id


async def tick(bot, session, season):
    """Drain every lifecycle state and bring the public board up to date."""
    from services import franchise_draft as D
    D.resolve_timeout(session, season)
    session.commit()
    cursor = int(season.announced_event_id or 0)
    await drain_events(bot, session, season)
    await refresh_board(bot, session, season,
                        force=int(season.announced_event_id or 0) != cursor)
    session.commit()


async def sweep(context, session):
    """Include paused/completed drafts, so the last signing is never lost."""
    from models import AuctionSeason, FranchiseDraft
    from services import auction_scheduler as S

    rows = (session.query(AuctionSeason)
            .join(FranchiseDraft, FranchiseDraft.season_id == AuctionSeason.id)
            .filter(AuctionSeason.chat_id.isnot(None)).all())
    for season in rows:
        try:
            async with S._lock():
                await tick(context.bot, session, season)
        except Exception:
            session.rollback()
            logger.exception('franchise draft #%s delivery failed', season.id)
