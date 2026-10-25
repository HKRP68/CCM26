"""Read-only squad reports shared by rich cards and Telegram HTML."""
import re
from html import escape
from services import squad_ratings

CATEGORY_ORDER = ('Batsmen', 'Wicket-keepers', 'All-rounders', 'Bowlers')


def category_name(raw):
    key = re.sub(r'[^a-z]', '', (raw or '').lower())
    aliases = {'bat': 'Batsmen', 'batsman': 'Batsmen', 'batsmen': 'Batsmen',
               'batter': 'Batsmen', 'batters': 'Batsmen',
               'wk': 'Wicket-keepers', 'wicketkeeper': 'Wicket-keepers',
               'wicketkeepers': 'Wicket-keepers', 'ar': 'All-rounders',
               'allrounder': 'All-rounders', 'allrounders': 'All-rounders',
               'bowler': 'Bowlers', 'bowlers': 'Bowlers', 'bowl': 'Bowlers'}
    return aliases.get(key, raw or 'Other')


def grouped(players):
    groups = {}
    for player in players:
        groups.setdefault(category_name(player.category), []).append(player)
    order = list(CATEGORY_ORDER) + sorted(set(groups) - set(CATEGORY_ORDER))
    return [(name, sorted(groups[name], key=lambda p: (-int(p.rating or 0), (p.name or '').casefold(), p.id)))
            for name in order if name in groups]


def rating(player, attribute):
    value = getattr(player, attribute, None)
    return str(value) if value is not None else '—'


def team_data(session, season, team):
    from services import auction_service as A
    from models import AuctionLot, AuctionLedgerEntry, Player
    from sqlalchemy import func
    rows = A.squad(session, team.id)
    counts = A.role_counts(session, team.id)
    minimums, maximums = A.role_minimums(season), A.role_maximums(season)
    rules = [(role, counts.get(role, 0), minimums.get(role, 0), maximums.get(role))
             for role in sorted(set(minimums) | set(maximums))]
    gaps = [(role, minimum - counts.get(role, 0)) for role, minimum in minimums.items()
            if counts.get(role, 0) < minimum]
    rights = []
    if A.retention_open(season) and not team.retention_finalized_at and season.previous_league_id:
        ids = [pid for pid, holder in A.previous_squad_map(session, season).items() if holder.id == team.id]
        signed = {pid for pid, in session.query(AuctionLot.player_id).filter_by(
            season_id=season.id, status=A.LOT_SOLD).all()}
        ids = [pid for pid in ids if pid not in signed]
        if ids:
            rights = session.query(Player).filter(Player.id.in_(ids)).all()
    averages = squad_ratings.averages(rows)
    return dict(team=team, players=rows, groups=grouped(rows), rights=grouped(rights),
                count=len(rows), overseas=sum(bool(p.is_overseas) for p in rows),
                contract_value=sum(int(p.sold_price_lakh or 0) for p in rows),
                used=int(team.purse_total_lakh or 0) - int(team.purse_remaining_lakh or 0),
                left=int(team.purse_remaining_lakh or 0), total=int(team.purse_total_lakh or 0),
                slots=max(0, int(season.max_squad_size or 0) - len(rows)),
                needed=max(0, int(season.min_squad_size or 0) - len(rows)),
                max_bid=max(0, A.max_bid_now(season, team)), rules=rules, gaps=gaps,
                averages=averages, retained=sum(p.acquisition == A.ACQ_RETAINED for p in rows),
                traded=A.traded_ids(session, season.id),
                trade_net=int(session.query(func.coalesce(func.sum(AuctionLedgerEntry.amount_lakh), 0))
                              .filter(AuctionLedgerEntry.franchise_id == team.id,
                                      AuctionLedgerEntry.kind.in_(('trade', 'trade_cash'))).scalar() or 0),
                rating_rule=A.rating_progress_line(session, season, team),
                warnings=A.squad_warnings(session, season, team))


def summary_lines(season, data):
    from services import auction_service as A
    money = lambda value: A.render_money(value, season.currency_label or '₹')
    team = data['team']
    lines = [f"👥 <b>{escape(team.name)}</b> — {data['count']}/{season.max_squad_size} players",
             f"💰 {money(data['left'])} left of {money(data['total'])} · used (net) {money(data['used'])}",
             f"📑 Current contracts: {money(data['contract_value'])} · 🎯 Max bid: {money(data['max_bid'])}",
             f"Squad min {season.min_squad_size}, max {season.max_squad_size} · {data['needed']} needed · {data['slots']} free slots",
             f"✈️ Overseas: {data['overseas']}/{season.max_overseas} · min {season.min_overseas or 0}",
             squad_ratings.summary(data['averages'])]
    if season.max_retentions:
        final = bool(team.retention_finalized_at or A.retention_locked(season))
        state = 'final' if final else ('open' if A.retention_open(season) else 'closed')
        lines.append(f"🔒 Retained: {data['retained']}/{season.max_retentions} · {state}")
    if data['trade_net'] or any(player.id in data['traded'] for player in data['players']):
        lines.append(f"🔁 Trades: {'+' if data['trade_net'] > 0 else ''}{money(data['trade_net'])} net to the purse")
    if team.draft_picks_total:
        lines.append(f"🆕 Draft picks: {team.draft_picks_used}/{team.draft_picks_total}")
    if A.rtm_configured(season):
        lines.append(f"🪪 RTM cards left: {A.rtm_cards_left(team)}")
    if data['rules']:
        lines.append('Category limits: ' + '; '.join(
            f"{escape(role)} {count} · min {minimum}, max {maximum if maximum is not None else '—'}"
            for role, count, minimum, maximum in data['rules']))
    if data['gaps']:
        lines.append('Needs: ' + ', '.join(f'{need} {escape(role)}' for role, need in data['gaps']))
    if data['rating_rule']:
        lines.append('⭐ Rating rule: ' + escape(data['rating_rule']))
    for warning in data['warnings']:
        icon = '🚫' if warning['level'] == 'danger' else '⚠️'
        lines.append(f"{icon} {escape(warning['message'])}")
    return lines


def render_team(season, data):
    from services import auction_service as A
    lines = summary_lines(season, data)
    if not data['players']:
        lines.append('<i>Nobody signed yet.</i>')
    for category, players in data['groups']:
        lines.append(f'\n<b>{escape(category)} ({len(players)})</b>')
        for player in players:
            how = A.acquisition_mark(player) + A.trade_mark(player, data['traded'])
            lines.append(f"{'✈️' if player.is_overseas else '🏠'} {escape(player.name)} "
                         f"[{escape(player.version or 'Base')}] · {escape(player.category or 'Other')} · OVR {rating(player, 'rating')} · "
                         f"BAT {rating(player, 'bat_rating')} · BOWL {rating(player, 'bowl_rating')} · "
                         f"{A.render_money(player.sold_price_lakh, season.currency_label)}{how}")
    if data['rights']:
        lines.append('\n<b>Unretained squad rights — not signed</b>')
        for category, players in data['rights']:
            lines.append(f'<b>{escape(category)} ({len(players)})</b>')
            for player in players:
                lines.append(f"{escape(player.name)} [{escape(player.version or 'Base')}] · {escape(player.category or 'Other')} · "
                             f"OVR {rating(player, 'rating')} · BAT {rating(player, 'bat_rating')} · "
                             f"BOWL {rating(player, 'bowl_rating')}")
        lines.append('<i>Rights are excluded from signed squad counts and contract totals.</i>')
    return '\n'.join(lines)
