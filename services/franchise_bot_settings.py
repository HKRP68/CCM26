"""Native Telegram settings prompts for the dedicated auction bot.

All monetary values here are integer lakh. The shared auction service remains
responsible for validation, lifecycle locks, and committing each change.
"""
from dataclasses import dataclass
import json
import re


@dataclass(frozen=True)
class Setting:
    label: str
    kind: str
    group: str
    example: str


SETTINGS = {}

def _add(group, rows):
    for key, label, kind, example in rows:
        SETTINGS[key] = Setting(label, kind, group, example)


_add('general', [
    ('name','Auction name','text','Premier Season 1'),
    ('currency_label','Currency label','text','₹'),
    ('home_country','Home country','text','India'),
    ('opening_purse_lakh','Opening purse','int','10000 = ₹100 Cr'),
    ('base_prices','Player base-price bands','prices','0-83=20;84-90=50;91+=100'),
    ('auto_accelerated','Automatic unsold round','bool','on or off'),
])
_add('squad', [
    ('min_squad_size','Minimum squad size','int','15'),
    ('max_squad_size','Maximum squad size','int','20'),
    ('min_overseas','Minimum overseas players','int','0'),
    ('max_overseas','Maximum overseas players','int','8'),
    ('role_minimums','Minimum players by role','map','BAT=4,BOWL=4,ALR=2,WK=1'),
    ('role_maximums','Maximum players by role','map','BAT=8,BOWL=8,ALR=6,WK=3'),
    ('rating_rules','Required lower-rated players','ratings','83=4 means at least 4 players rated 83 or below'),
])
_add('clock', [
    ('bid_seconds','Opening bid clock','int','30 seconds'),
    ('staged_clock','Staged auction clock','clock','45 20 12 8 5 (opening reset warning1 warning2 countdown)'),
    ('countdown_seconds','Final countdown','int','5 seconds'),
    ('bid_gap_seconds','Quiet gap between bids','int','0 seconds'),
    ('increments','Bid increment ladder','increments','200=10,500=20,*=50 (all amounts in lakh)'),
    ('direct_bids','Allow custom bids','bool','on or off'),
    ('focus_mode','Auction-only group mode','bool','on or off'),
    ('snipe_window_seconds','Anti-snipe window','int','5 seconds'),
    ('snipe_extend_seconds','Anti-snipe extension','int','10 seconds'),
    ('max_extensions','Maximum clock extensions','int','5'),
])
_add('retention', [
    ('retention_mode','Retention mode','text','classic, dynamic or ranges'),
    ('min_retentions','Minimum retentions','int','0'),
    ('max_retentions','Maximum retentions','int','4'),
    ('retention_max_spend_lakh','Retention budget','optional_int','5000; clear removes the cap'),
    ('retention_min_rating','Minimum retention rating','optional_int','80; clear removes the restriction'),
    ('retention_max_rating','Maximum retention rating','optional_int','99; clear removes the restriction'),
    ('retention_categories','Allowed retention roles','list','Batsman,Bowler; clear allows all roles'),
    ('retention_prices','Retention price slabs','slabs','1=1800,2=1400,3=1100; clear uses defaults'),
    ('retention_deadline','Retention deadline','optional_text','2030-01-01T18:00:00+03:00; clear removes it'),
    ('retention_ranges','Retention rating ranges','ranges','70-83,0,2,100 (rating range,min,max,price in lakh); one row per line'),
    ('retention_range_max_total','Overall range retention cap','optional_int','4; clear uses the sum of range limits'),
    ('retention_slots','Negotiation slots','json','JSON list of slot rules; clear restores the default slots'),
    ('retention_rules','Negotiation rules','json','JSON object of negotiation rules; clear restores defaults'),
])
_add('draft', [
    ('expansion_picks','Default expansion picks','int','3'),
    ('draft_ranges','Draft rating ranges','ranges','70-83,0,2,100; one row per line; clear removes ranges'),
    ('draft_pick_seconds','Pick clock','int','90; 0 disables it'),
    ('draft_timeout_action','On pick timeout','text','pause, skip or shortlist'),
    ('rtm_enabled','Right To Match','bool','on or off'),
    ('rtm_per_team','Default RTM cards','int','3'),
    ('rtm_window_seconds','RTM response clock','int','30 seconds'),
    ('rtm_extra_lakh','RTM premium','int','0 lakh'),
])
_add('trades', [
    ('trades_open','Trade window','bool','on or off'),
    ('trade_rules','Trade rulebook','json','JSON object of trade rules'),
])
GROUP_LABELS = {'general':'Season & purse','squad':'Squad rules','clock':'Clock & bidding',
                'retention':'Retention','draft':'Draft & RTM','trades':'Trades'}


def whole(raw):
    if not re.fullmatch(r'-?\d+', str(raw).strip()):
        raise ValueError('Send a whole number. Money uses lakh: 100 = 1 Cr.')
    return int(str(raw).strip())


def ids(raw):
    if str(raw).strip().lower() in ('clear','none','skip','0','-'):
        return []
    result = list(dict.fromkeys(whole(value) for value in re.split(r'[,;\s]+', str(raw).strip()) if value))
    if not result or len(result)>30 or any(not 0<value<10**16 for value in result):
        raise ValueError('Send up to 30 positive Telegram user IDs, separated by commas; clear removes the list.')
    return result


def parse_value(key, raw):
    field = SETTINGS[key]
    text = str(raw).strip()
    if len(text)>12000:
        raise ValueError('That value is too long.')
    clear = text.lower() in ('clear','none','null','-')
    if clear and field.kind in ('optional_int','optional_text','json','ranges','slabs','list'):
        return None
    if field.kind in ('int','optional_int'):
        return whole(text)
    if field.kind == 'bool':
        if text.lower() not in ('on','off','yes','no','true','false','1','0'):
            raise ValueError('Send on or off.')
        return text.lower() in ('on','yes','true','1')
    if field.kind in ('text','optional_text'):
        if not text:
            raise ValueError('Send a value, or /cancel.')
        return text
    if text.startswith(('[','{')):
        return json.loads(text)
    if field.kind == 'json':
        raise ValueError('Send valid JSON for this advanced rule, or /cancel.')
    if field.kind == 'clock':
        values = [whole(v) for v in re.split(r'[,\s]+',text) if v]
        if len(values)!=5:raise ValueError('Send five numbers: opening reset warning1 warning2 countdown.')
        return values
    if field.kind == 'map':
        return {part.split('=',1)[0].strip().upper():whole(part.split('=',1)[1]) for part in text.split(',')}
    if field.kind == 'ratings':
        return [dict(max_rating=whole(a),min_players=whole(b)) for a,b in (part.split('=',1) for part in text.split(','))]
    if field.kind == 'list':return [v.strip() for v in text.split(',') if v.strip()]
    if field.kind == 'slabs':return [dict(slab=whole(a),price_lakh=whole(b)) for a,b in (part.split('=',1) for part in text.split(','))]
    if field.kind == 'increments':
        return [dict(upto_lakh=0 if a.strip()=='*' else whole(a),step_lakh=whole(b)) for a,b in (part.split('=',1) for part in text.split(','))]
    if field.kind == 'prices':
        rows=[]
        for part in text.split(';'):
            span,price=part.split('=',1)
            if span.strip().endswith('+'):low,high=whole(span.strip()[:-1]),None
            else:low,high=[whole(v) for v in span.split('-',1)]
            rows.append(dict(min_rating=low,max_rating=high,base_lakh=whole(price)))
        return rows
    if field.kind == 'ranges':
        rows=[]
        for line in text.splitlines():
            span,low,high,price=[v.strip() for v in line.split(',')]
            a,b=[whole(v) for v in span.split('-',1)]
            rows.append(dict(min_rating=a,max_rating=b,min_count=whole(low),max_count=whole(high),price_lakh=whole(price)))
        return rows
    raise ValueError('Unsupported setting.')
