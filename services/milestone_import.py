"""Validate and atomically replace milestone message selections."""
import json

from models import EventMedia
from services.event_media_service import EVENT_KEYS, MESSAGE_SETTINGS

MAX_IMPORT_BYTES = 64 * 1024


def parse_import(raw):
    """Map event keys to custom text or True (automatic); omitted keys are OFF."""
    if len(raw.encode('utf-8')) > MAX_IMPORT_BYTES:
        raise ValueError('File must be 64 KB or smaller.')

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate milestone: {key}')
            result[key] = value
        return result

    try:
        data = json.loads(raw.lstrip('\ufeff'), object_pairs_hook=unique_pairs)
    except json.JSONDecodeError as exc:
        raise ValueError(f'Invalid JSON at line {exc.lineno}, column {exc.colno}.') from exc
    if not isinstance(data, dict) or not data:
        raise ValueError('Use a non-empty JSON object of milestone keys and messages.')
    keys = {key for key, _, _ in EVENT_KEYS}
    unknown = set(data) - keys
    if unknown:
        raise ValueError('Unknown milestone(s): ' + ', '.join(sorted(unknown)))
    result = {}
    for key, value in data.items():
        if value is True:
            result[key] = True
        elif value is False or value is None:
            continue
        elif isinstance(value, str):
            if value.strip():
                if len(value) > 900:
                    raise ValueError(f'{key}: message must be 900 characters or fewer.')
                result[key] = value.strip()
        else:
            raise ValueError(f'{key}: use message text, true for automatic, or false for OFF.')
    return result


def apply_import(session, selections):
    """One transaction; preserve media and turn omitted event messages OFF."""
    try:
        rows = session.query(EventMedia).all()
        by_key = {}
        for row in rows:
            by_key.setdefault(row.event_key, []).append(row)
        for key, _, _ in EVENT_KEYS:
            entries = by_key.get(key, [])
            preferences = [r for r in entries if r.source_type == MESSAGE_SETTINGS]
            status = 'on' if key in selections else 'off'
            if not preferences:
                preference = EventMedia(event_key=key, source_type=MESSAGE_SETTINGS,
                                        source=status, enabled=False, weight=1,
                                        label='Milestone message preference')
                session.add(preference)
            for preference in preferences:
                preference.source = status
                preference.enabled = False
            if key not in selections:
                continue
            caption = None if selections[key] is True else selections[key]
            content = [r for r in entries if r.source_type != MESSAGE_SETTINGS]
            for row in content:
                row.caption = caption
            if caption and not any(r.enabled for r in content):
                session.add(EventMedia(event_key=key, source_type='none', source='',
                                       caption=caption, enabled=True, weight=1,
                                       label='Imported message'))
        session.commit()
    except Exception:
        session.rollback()
        raise


def import_template():
    return json.dumps({
        'fifty': '⭐ FIFTY! {player} — {runs} ({balls}) · SR {strike_rate}\n👤 {manager} · Against {opponent}',
        'century': '💯 CENTURY! {player} — {runs} ({balls})\n🏆 {achievement}\n💥 {fours} fours · {sixes} sixes\n👤 {manager} · Against {opponent}',
        'five_fer': True,
    }, ensure_ascii=False, indent=2)
