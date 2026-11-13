"""File prices select catalogue cards and retain the draft confirmation rules."""
import asyncio
import csv
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_franchise_draft_handlers import room, ADMIN, OWNER, CHAT, context, update  # noqa: F401


def sheet(room, delimiter=',', **changes):
    lot = room.A.lots(room.db, room.season.id)[0]
    values = dict(player_id=lot.player_id, name=lot.name, version=lot.version or 'Base',
                  rating=lot.rating, category=lot.category, price_lakh=2550)
    values.update(changes)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(values), delimiter=delimiter)
    writer.writeheader();writer.writerow(values)
    return output.getvalue(), lot


@pytest.mark.parametrize('delimiter', [',', '\t'])
def test_custom_file_preview_does_not_write_until_confirmed(room, delimiter):
    from services import franchise_draft_admin as M
    text, lot = sheet(room, delimiter)
    row = M.prepare(room.db, room.season, ADMIN, 'custom', {'text': '\ufeff' + text})
    assert json.loads(row.preview_json)['requested_ids'] == [lot.id]
    assert room.D.price_for(room.db, room.season, lot) == 2300
    M.answer(room.db, room.season, ADMIN, row.token, accept=True)
    assert room.D.price_for(room.db, room.season, lot) == 2550


@pytest.mark.parametrize('changes', [dict(player_id=999999), dict(name='Wrong card'),
    dict(version='Wrong version'), dict(rating=2), dict(category='Bowler'),
    dict(price_lakh='25.5'), dict(price_lakh='-1'), dict(price_lakh='1cr'),
    dict(price_lakh='1000000001')])
def test_invalid_price_file_leaves_all_prices_intact(room, changes):
    text, _ = sheet(room, **changes)
    before = room.D.get_draft(room.db, room.season).custom_prices_json
    with pytest.raises(room.A.AuctionError):
        room.D.set_custom_prices(room.db, room.season, text, by_tg_id=ADMIN)
    assert room.D.get_draft(room.db, room.season).custom_prices_json == before


def test_duplicate_rows_and_signed_cards_are_rejected_and_zero_is_supported(room):
    text, lot = sheet(room, price_lakh=0)
    with pytest.raises(room.A.AuctionError, match='more than once'):
        room.D.set_custom_prices(room.db, room.season, text + text.splitlines()[1], by_tg_id=ADMIN)
    room.D.set_custom_prices(room.db, room.season, text, by_tg_id=ADMIN)
    assert room.D.price_for(room.db, room.season, lot) == 0
    lot.status = room.A.LOT_SOLD
    with pytest.raises(room.A.AuctionError, match='already signed'):
        room.D.custom_price_rows(room.db, room.season, text)


def test_reply_dcustom_downloads_authorized_file_and_returns_confirmation(room):
    text, lot = sheet(room)
    file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=text.encode()))
    document = SimpleNamespace(file_size=len(text), get_file=AsyncMock(return_value=file))
    event = update(actor=ADMIN, text='/dcustom@DraftTestBot')
    event.effective_message.reply_to_message = SimpleNamespace(document=document)
    asyncio.run(room.H.command_handler(event, context(room.season.id)))
    assert '25.5 Cr' in event.effective_message.replies[-1][0]
    assert room.D.price_for(room.db, room.season, lot) == 2300
    assert document.get_file.await_count == 1
    denied = update(actor=OWNER, text='/dcustom')
    denied.effective_message.reply_to_message = event.effective_message.reply_to_message
    asyncio.run(room.H.command_handler(denied, context(room.season.id)))
    assert document.get_file.await_count == 1


def test_reply_file_correction_requires_pause_and_reason(room):
    text, lot = sheet(room)
    room.D.start(room.db, room.season, by_tg_id=ADMIN)
    room.D.pause(room.db, room.season, by_tg_id=ADMIN)
    room.db.commit()
    file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=text.encode()))
    event = update(actor=ADMIN, text='/dcorrect custom | Correct uploaded prices |')
    event.effective_message.reply_to_message = SimpleNamespace(document=SimpleNamespace(
        file_size=len(text), get_file=AsyncMock(return_value=file)))
    asyncio.run(room.H.command_handler(event, context(room.season.id)))
    assert '25.5 Cr' in event.effective_message.replies[-1][0]
    assert room.D.price_for(room.db, room.season, lot) == 2300
