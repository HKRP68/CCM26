"""Telegram commands scoped to a World T20 Championship."""
import json
from datetime import date, datetime
from functools import partial

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_session
from models import T20Championship, User
from services import rich_message as R
from services.rich_message_html import inline
from services import t20_championship as cup
from services import t20_championship_stats as stats
from services.admin_ids import is_admin
from utils.message_chunks import chunk_blocks
from utils.tournament_time import IST_OFFSET, format_ist

VIEW_TITLES = {
    "help": "Commands", "info": "Overview", "table": "Standings",
    "fixtures": "Match schedule", "teams": "Teams and squads", "series": "Series",
    "playoffs": "Playoffs", "final": "Championship final", "stats": "Player statistics",
    "player": "Player statistics", "awards": "Awards", "scorecard": "Scorecard",
    "today": "Today's fixtures",
}
FIXTURE_VIEWS = {"fixtures": "f", "series": "s", "playoffs": "p", "final": "n", "today": "t"}
PAGE_VIEWS = {code: view for view, code in FIXTURE_VIEWS.items()}


def command_help(prefix):
    return [
        f"/{prefix} or /{prefix}play [FIXTURE_ID] — reply to your scheduled opponent to play",
        f"/{prefix}info — championship overview",
        f"/{prefix}table — points and series standings",
        f"/{prefix}fixtures [team=ID] — schedule, deadlines and results",
        f"/{prefix}teamfix [TEAM] — your fixtures, or look up a team by name, short name or ID",
        f"/wtctoday {prefix} — today's fixtures in this championship",
        f"/{prefix} today [TEAM] — fixtures opening or due today (IST)",
        f"/{prefix} deadline S1M1 YYYY-MM-DD [HH:MM] — extend a match deadline (bot admins)",
        "/matchremind today — preview today's match reminders (bot admins)",
        f"/{prefix}teams — squads and captains",
        f"/{prefix}series [ID] — bilateral series scores and results",
        f"/{prefix} playoffs — semifinal bracket, fixtures and results",
        f"/{prefix}final — final format, fixtures and champion",
        f"/{prefix}stats [team=ID] [series=ID] — player statistics",
        f"/{prefix}player NAME [team=ID] [series=ID] — one player's figures",
        f"/{prefix}awards — player awards",
        f"/{prefix}scorecard FIXTURE_ID — match scorecard",
        f"/{prefix}help — command list",
    ]


def result_label(fixture):
    if fixture.result == "tie":
        return "Tie"
    if fixture.result == "no_result":
        return "No result"
    if fixture.winner_id:
        series = fixture.series
        winner = series.home if series.home_id == fixture.winner_id else series.away
        return f"{winner.name} won" + (" by forfeit" if fixture.result == "forfeit" else "")
    return ""


def series_summary(series):
    home, away = cup.series_score(series)
    label = f"{series.home.name} {home}–{away} {series.away.name}"
    if cup.series_complete(series):
        if cup.needs_decider(series):
            return label + " · Decider needed"
        if all(f.status == "cancelled" for f in series.fixtures):
            return label + " · Cancelled"
        if home != away:
            return label + f" · {(series.home if home > away else series.away).name} won"
        return label + " · Drawn"
    return label


def scorecard(fixture):
    result = result_label(fixture)
    if fixture.result == "win" and fixture.match:
        if fixture.match.margin_type == "super over":
            result += " in the Super Over"
        elif fixture.match.margin_type in ("runs", "wickets") and fixture.match.margin_value:
            result += f" by {fixture.match.margin_value} {fixture.match.margin_type}"
    lines = [f"{cup.series_name(fixture.series)} · Match {fixture.number} · #{fixture.id} · {cup.match_reference(fixture)}",
             result or cup.STATUS.get(fixture.status, "Upcoming")]
    if fixture.match:
        lines.append(" · ".join(value for value in (fixture.match.stadium, fixture.match.pitch_type)
                                if value))
    if not fixture.scorecard:
        return lines + ["No innings figures are available."]
    packet = json.loads(fixture.scorecard)
    players = packet["players"]
    for innings in packet["innings"]:
        overs, balls = divmod(innings["balls"], 6)
        lines.extend(["", f'{innings["team"]} {innings["runs"]}/{innings["wickets"]} '
                     f'({overs}.{balls} overs)', "Batting · R (B) · 4s · 6s"])
        for p in players:
            if p["team_id"] == innings["team_id"]:
                if p["batted"]:
                    lines.append(f'{p["name"]} {p["bat_runs"]}{"*" if not p["bat_out"] else ""} '
                                 f'({p["bat_balls"]}) · {p["bat_fours"]} · {p["bat_sixes"]}')
                else:
                    lines.append(f'{p["name"]} · did not bat')
        lines.append("Bowling · O–R–W")
        for p in players:
            if p["team_id"] != innings["team_id"] and p["bowled"]:
                o, b = divmod(p["bowl_balls"], 6)
                lines.append(f'{p["name"]} {o}.{b}–{p["bowl_runs"]}–{p["bowl_wickets"]}')
    best = next((p for p in players if p["roster_id"] == packet["potm"]), None)
    if best:
        lines.append(f'Player of the Match · {best["name"]} ({best["team_name"]})')
    return lines


def person_names(db, ids):
    ids = {uid for uid in ids if uid}
    if not ids:
        return {}
    return {user.telegram_id: user.first_name or
            (f"@{user.username}" if user.username else str(user.telegram_id))
            for user in db.query(User).filter(User.telegram_id.in_(ids))}


def _filters(championship, rows, args, *, allowed):
    names = []
    found = {}
    lookup = {"team": {team.id for team in championship.teams}, "series": {row.id for row in rows}}
    for token in args:
        key, sep, value = token.partition("=")
        if sep and key in allowed:
            if not value.isdecimal() or int(value) not in lookup[key]:
                raise ValueError(f"Choose a {key} ID listed by this championship.")
            found[key + "_id"] = int(value)
        else:
            names.append(token)
    return " ".join(names), found


def _fixture_sections(championship, rows, selected, team_id=0, day_ids=None):
    next_by_team = cup.next_fixtures(rows)
    for series in selected:
        title = f"#{series.id} · {cup.series_name(series)}"
        if team_id:
            if series.is_final and series.stadium:
                side = "Neutral"
            else:
                side = "Home" if series.home_id == team_id else "Away"
            title = f"{title} · {side}"
        stadium = cup.series_stadium(series)
        venue = " · ".join(filter(None, (stadium, series.venue))) or "Home stadium needed"
        lines = [series_summary(series), venue]
        for fixture in series.fixtures:
            if day_ids is not None and fixture.id not in day_ids:
                continue
            status = cup.STATUS[cup.fixture_status(championship, fixture, next_by_team)]
            result = result_label(fixture)
            pitch = fixture.match.pitch_type if fixture.match else (
                fixture.random_pitch if championship.pitch_mode == "random" else None)
            details = " · ".join(filter(None, (status, result, pitch)))
            lines.append(f"Match {fixture.number} · #{fixture.id} · {cup.match_reference(fixture)} · {details}\n"
                         f"Opens {format_ist(fixture.opens_at)} · Deadline {format_ist(fixture.deadline)}")
        yield title, lines


def _fixture_scope(championship, rows, view, team_id=0, series_id=0):
    prefix = championship.command
    if view == "playoffs":
        if championship.playoff_format == "none":
            return ["Top-four semifinals are off.", f"/{prefix}final"], [], ""
        notes = [f"Semifinals · {cup.PLAYOFFS[championship.playoff_format]}",
                 "Seeds 1 vs 4 and 2 vs 3 · Winners meet in the final."]
        postseason = [row for row in rows if cup.is_postseason(row)]
        return notes, postseason, "The bracket opens after the league series finish."
    if view == "final":
        final = [row for row in rows if row.is_final]
        if championship.final_format == "none":
            qualification = "The table leader becomes champion."
        elif championship.playoff_format != "none":
            qualification = f"The semifinal winners qualify. See /{prefix} playoffs."
        else:
            qualification = "The top two qualify after the league series finish."
        champion = next((team for team in championship.teams if team.id == championship.champion_id), None)
        notes = [f"Final · {cup.FINALS[championship.final_format]}"]
        if champion:
            notes.append(f"Champion · {champion.name}")
        return notes, final, qualification
    selected = [row for row in rows if (not team_id or team_id in (row.home_id, row.away_id))
                and (not series_id or series_id == row.id)]
    return [], selected, "No matching series."


def _fixture_filters(championship, rows, view, args):
    if view not in ("fixtures", "series"):
        return 0, 0
    query, filters = _filters(championship, rows, args, allowed={"team"})
    if view == "series" and query and not query.isdecimal():
        raise ValueError(f"Use /{championship.command}series ID.")
    return filters.get("team_id", 0), int(query) if view == "series" and query else 0


def _player_block(row):
    fig = stats.player_figures(row)
    return (
        f'\n{stats.player_label(row)} · Matches {row["matches"]}\n'
        f'Batting · Inn {row["bat_innings"]} · Runs {row["bat_runs"]} · Avg {fig["average"]} · '
        f'SR {fig["strike_rate"]} · HS {fig["highest"]} · 50s {row["fifties"]} · 100s {row["hundreds"]} · '
        f'4s {row["bat_fours"]} · 6s {row["bat_sixes"]}\n'
        f'Bowling · Wkts {row["bowl_wickets"]} · Econ {fig["economy"]} · Avg {fig["bowling_average"]} · '
        f'SR {fig["bowling_strike_rate"]} · Best {fig["best"]} · 3-fers {row["three_fers"]} · '
        f'5-fers {row["five_fers"]} · Hat-tricks {row["hattricks"]}\n'
        f'Fielding · Catches {row["catches"]} · Dismissals {row["dismissals"]}\n'
        f'Awards · POTM {row["potm"]} · POTS {row["pots"]} · MVP points {row["mvp"]:.1f}'
    )


def _standing_tables(table):
    return [
        ("Championship points", ("Pos", "Team", "P", "W", "L", "T", "NR", "Bonus", "Pts", "PCT", "NRR"),
         [(i, row["team"].name, row["played"], row["won"], row["lost"], row["tied"], row["nr"],
           row["bonus"], row["points"], f'{float(row["pct"]):.2f}%', f'{float(row["nrr"]):+.3f}')
          for i, row in enumerate(table, 1)]),
        ("Series records", ("Team", "Played", "Won", "Lost", "Drawn", "Home wins", "Away wins"),
         [(row["team"].name, row["series_played"], row["series_won"], row["series_lost"],
           row["series_drawn"], row["home_wins"], row["away_wins"]) for row in table]),
    ]


def render(db, championship, view, args):
    prefix = championship.command
    if view == "help":
        return command_help(prefix)
    rows = cup.series_list(db, championship)
    champion = next((team for team in championship.teams if team.id == championship.champion_id), None)
    if view == "info":
        status = "Completed" if championship.completed_at else "In progress" if championship.started_at else "Setup"
        active = sum(team.is_participating for team in championship.teams)
        return [f"{status} · {active} teams · {len(rows)} series",
                f"Final · {cup.FINALS[championship.final_format]}",
                f"Pitch selection · {cup.PITCH_MODES[championship.pitch_mode]}",
                f"Semifinals · {cup.PLAYOFFS[championship.playoff_format]} · /{prefix} playoffs"
                if championship.playoff_format != "none" else "",
                f"Champion · {champion.name}" if champion else "",
                f"/{prefix}table · /{prefix}fixtures · /{prefix}teamfix · /{prefix}teams · /{prefix}help"]
    if view == "table":
        return [" · ".join(map(str, row))
                for _, headers, values in _standing_tables(cup.standings(championship, rows))
                for row in (headers, *values)]
    if view == "teams":
        names = person_names(db, (uid for team in championship.teams
                                 for uid in (team.owner_id, team.co_owner_id)))
        lines = []
        for team in sorted(championship.teams, key=lambda team: team.name.casefold()):
            reserve = " · Reserve team" if not team.is_participating else ""
            lines.append(f"\n#{team.id} · {team.name}{reserve} · Owner {names.get(team.owner_id, team.owner_id)}"
                         + (f" · Co-owner {names.get(team.co_owner_id, team.co_owner_id)}" if team.co_owner_id else ""))
            lines.append(f"Home stadium · {team.home_stadium or 'Needs a stadium'}")
            lines.extend(f"{p.name} · {p.source_player.version or 'Base'}"
                         if p.source_player else p.name for p in team.team.players)
        return lines or ["No teams have been added."]
    if view in FIXTURE_VIEWS and view != "today":
        team_id, series_id = _fixture_filters(championship, rows, view, args)
        notes, selected, empty = _fixture_scope(championship, rows, view, team_id, series_id)
        if not selected:
            return [*notes, empty]
        return [*notes, *_series_award(selected, series_id),
                *(line for title, lines in _fixture_sections(championship, rows, selected, team_id)
                  for line in (title, *lines))]
    if view == "scorecard":
        if len(args) != 1:
            raise ValueError(f"Use /{prefix}scorecard FIXTURE_ID. Find the ID in /{prefix}fixtures.")
        match = cup.find_fixture(rows, args[0])
        return scorecard(match) if match else ["Fixture not found."]
    query, filters = _filters(championship, rows, args, allowed={"team", "series"})
    totals = stats.aggregate(rows, **filters)
    if view == "awards":
        awards = stats.awards(championship, rows, totals)
        captain = championship.champion_captain
        if captain:
            awards["Champion Captain"] = person_names(db, [captain]).get(captain, captain)
        return ["Final awards" if championship.completed_at else "Award leaders",
                *(f"{key} · {value or '–'}" for key, value in awards.items())]
    if view == "player":
        if not query:
            raise ValueError(f"Use /{prefix}player NAME.")
        totals = [r for r in totals if query.casefold() in r["name"].casefold()]
    return [_player_block(row) for row in totals] or ["No player figures for this selection yet."]


def _series_award(series, series_id):
    if series_id and series:
        best = next((row for row in stats.aggregate(series) if row["pots"]), None)
        if best:
            return [f"Player of the Series · {stats.player_label(best)}"]
    return []


def _line(line):
    if line.startswith("Opens "):
        return R.italic(line)
    label, separator, body = line.partition(" — " if line.startswith("/") else " · ")
    if separator:
        mark = R.code if line.startswith("/") else R.bold
        return [mark(label), separator, body]
    return line


def _section_rows(lines, limit):
    for line in lines:
        parts = [part for part in line.splitlines() if part.strip()]
        formatted = list(map(_line, parts))
        html = "\n".join(map(inline, formatted))
        if len(html) <= limit:
            yield formatted, html
            continue
        # Split unusually long text before adding tags so the fallback stays valid.
        width = max(1, limit // 6)
        for part, value in zip(parts, formatted):
            rendered = inline(value)
            if len(rendered) <= limit:
                yield [value], rendered
            else:
                for start in range(0, len(part), width):
                    piece = part[start:start + width]
                    yield [piece], inline(piece)


def _section_cards(title, lines, limit, *, expanded=False):
    """Keep each details block and its legacy HTML quote together."""
    summary = inline(R.bold(title))
    limit -= len(summary) + 70
    groups = [[]]
    size = 0
    for parts, html in _section_rows(lines, limit):
        if groups[-1] and size + len(html) + 1 > limit:
            groups.append([])
            size = 0
        groups[-1].append((parts, html))
        size += len(html) + 1
    for index, group in enumerate(groups):
        label = f"{title} · continued" if index else title
        blocks = [R.paragraph(part) for parts, _ in group for part in parts]
        body = "\n".join(html for _, html in group)
        heading = inline(R.bold(label))
        if expanded:
            fallback = f"{heading}\n{body}"
        else:
            fallback = f"<blockquote expandable>{heading}\n{body}</blockquote>"
        yield R.details(R.bold(label), blocks, is_open=expanded), fallback


def _pack_cards(cards, limit, *, max_cards=None):
    group, size = [], 0
    for card in cards:
        length = len(card[1]) + 1
        if group and (size + length > limit or len(group) == max_cards):
            yield group
            group, size = [], 0
        group.append(card)
        size += length
    if group:
        yield group


def _fixture_button(label, championship_id, view, team_id, series_id, page, day=None):
    data = f"wt20f:{championship_id}:{FIXTURE_VIEWS[view]}:{team_id}:{series_id}:{page}"
    if day:
        data += f":{day.toordinal()}"
    return InlineKeyboardButton(label, callback_data=data)


def _fixture_page(championship, rows, view, team_id, series_id, page, day=None):
    team = next((team for team in championship.teams if team.id == team_id), None)
    subtitle = f"{team.name} fixtures" if team else VIEW_TITLES[view]
    title = f"{championship.name} · {subtitle}"
    notes, selected, empty = _fixture_scope(championship, rows, view, team_id, series_id)
    day_ids = None
    if day:
        matches = cup.fixtures_on_day(selected, day)
        day_ids = {fixture.id for fixture in matches}
        series_ids = {fixture.series_id for fixture in matches}
        selected = [series for series in selected if series.id in series_ids]
        notes = [day.strftime("%d %b %Y") + " · IST"]
        empty = "No fixtures open or are due on this date."
    if not selected:
        summary = [line for line in (*notes, empty) if line]
        return ([R.heading(title), *(R.paragraph(_line(line)) for line in summary)],
                "\n".join([inline(R.bold(title)), *(inline(_line(line)) for line in summary)]), None)

    intro = [_line(line) for line in notes]
    if team and not day:
        pending = cup.next_fixtures(rows)
        next_id = pending.get(team.id)
        fixture = next((f for row in selected for f in row.fixtures if f.id == next_id), None)
        if fixture:
            status = cup.STATUS[cup.fixture_status(championship, fixture, pending)]
            series = fixture.series
            opponent = series.away if series.home_id == team.id else series.home
            name = opponent.team.short_name or opponent.name
            intro.append([R.bold("Next fixture"),
                          f" · vs {name[:45]} · #{fixture.id} · {status}"])
            intro.append(R.italic(f"Opens {format_ist(fixture.opens_at)}"))
    intro.append("Tap a series to see its fixtures.")
    header = "\n".join([inline(R.bold(title)), *(inline(line) for line in intro)])
    award = _series_award(selected, series_id)
    limit = R.CHUNK_LIMIT - len(header) - 100
    selected = sorted(selected, key=lambda row: (cup.series_complete(row), row.position))
    cards = (card for label, lines in _fixture_sections(championship, rows, selected, team_id, day_ids)
             for card in _section_cards(label, [*lines, *award], limit))
    pages = list(_pack_cards(cards, limit, max_cards=3))
    page = min(max(0, page), len(pages) - 1)
    blocks = [R.heading(title), *(R.paragraph(line) for line in intro)]
    blocks.extend(block for block, _ in pages[page])
    fallback = [header, *(html for _, html in pages[page])]
    buttons = []
    if len(pages) > 1:
        label = f"Page {page + 1} of {len(pages)}"
        blocks.append(R.footer(label))
        fallback.append(inline(label))
        for target, button_label in ((page - 1, "‹ Previous"), (page + 1, "Next ›")):
            if 0 <= target < len(pages):
                buttons.append(_fixture_button(button_label, championship.id, view, team_id, series_id, target, day))
    return blocks, "\n".join(fallback), InlineKeyboardMarkup([buttons]) if buttons else None


def _matching_teams(championship, query, viewer_id):
    teams = sorted(championship.teams, key=lambda team: team.name.casefold())
    if not query:
        return [team for team in teams if cup.assigned(team, viewer_id)] if viewer_id is not None else []
    lookup = query.casefold().removeprefix("team=")
    if lookup.isdecimal():
        return [team for team in teams if team.id == int(lookup)]
    exact = [team for team in teams if lookup in (team.name.casefold(), (team.team.short_name or "").casefold())]
    return exact or [team for team in teams if lookup in team.name.casefold()]


async def _reply_team_fixtures(message, championship, rows, args, viewer_id, *, day=None):
    query = " ".join(args)
    teams = _matching_teams(championship, query, viewer_id)
    view = "today" if day else "fixtures"
    command = f"/{championship.command} today" if day else f"/{championship.command}teamfix"
    if len(teams) == 1 or (day and not teams and not query):
        team_id = teams[0].id if teams else 0
        blocks, fallback, markup = _fixture_page(championship, rows, view, team_id, 0, 0, day)
        await R.reply_rich(message, blocks, fallback, reply_markup=markup)
        return
    title = f"{championship.name} · Team fixtures"
    if teams:
        lines = [f"#{team.id} · {team.name}" for team in teams[:8]]
        if len(teams) > 8:
            lines.append(f"{command} TEAM_NAME — look up another team")
        buttons = [[_fixture_button(team.name[:40], championship.id, view, team.id, 0, 0, day)]
                   for team in teams[:8]]
        await _reply(message, title, ["Choose a team.", *lines],
                     reply_markup=InlineKeyboardMarkup(buttons))
    else:
        text = "No team matches that name or ID." if query else "Choose a team to see its fixtures."
        await _reply(message, title, [text, f"{command} TEAM_NAME",
                                     f"/{championship.command}teams — team names and IDs"])


async def fixture_page_callback(update, context):
    query = update.callback_query
    try:
        _, championship, code, team, series, page, *scope = query.data.split(":")
        view = PAGE_VIEWS[code]
        championship_id, team_id, series_id, page = map(int, (championship, team, series, page))
        if len(scope) != (1 if view == "today" else 0):
            raise ValueError("Invalid date scope")
        day = date.fromordinal(int(scope[0])) if scope else None
    except (ValueError, KeyError, AttributeError, OverflowError):
        await query.answer("This fixture page is unavailable.", show_alert=True)
        return
    with get_session() as db:
        championship = db.get(T20Championship, championship_id)
        if not championship or (team_id and all(team.id != team_id for team in championship.teams)):
            await query.answer("This fixture page is unavailable.", show_alert=True)
            return
        rows = cup.series_list(db, championship)
        blocks, fallback, markup = _fixture_page(championship, rows, view, team_id, series_id, page, day)
    await query.answer()
    await R.edit_rich(query, blocks, fallback, reply_markup=markup)


async def _reply(message, title, content, *, reply_markup=None):
    lines = [content] if isinstance(content, str) else content
    for chunk in chunk_blocks(lines):
        blocks = [R.heading(title)]
        fallback = [inline(R.bold(title))]
        after_blank = False
        for line in chunk.splitlines():
            if not line.strip():
                after_blank = True
                fallback.append("")
                continue
            if after_blank:
                block, formatted = R.heading(line, size=5), R.bold(line)
            else:
                formatted = _line(line)
                block = R.paragraph(formatted)
            blocks.append(block)
            fallback.append(inline(formatted))
            after_blank = False
        await R.reply_rich(message, blocks, "\n".join(fallback), reply_markup=reply_markup)


async def _reply_standings(message, championship, table):
    for title, headers, rows in _standing_tables(table):
        for start in range(0, max(1, len(rows)), 10):
            page = rows[start:start + 10]
            cells = [[R.cell(value, header=True) for value in headers]]
            cells.extend([[R.cell(str(value)) for value in row] for row in page])
            header = f"{championship.name} · {title}"
            blocks = [R.heading(header), R.table(cells, compact=True, striped=True)]
            fallback = [inline(R.bold(header)), inline(R.bold(" · ".join(headers)))]
            fallback.extend(inline(" · ".join(map(str, row))) for row in page)
            await R.reply_rich(message, blocks, "\n".join(fallback))


def _split_sections(lines):
    lead, sections = [], []
    section_start = False
    for text in lines:
        for line in text.split("\n"):
            if not line.strip():
                section_start = True
            elif section_start:
                sections.append((line, []))
                section_start = False
            elif sections:
                sections[-1][1].append(line)
            else:
                lead.append(line)
    return lead, sections


async def _reply_view(message, title, view, lines):
    if view == "help":
        items = [_line(line) for line in lines]
        fallback = [inline(R.bold(title)), *("• " + inline(item) for item in items)]
        await R.reply_rich(message, [R.heading(title), R.list_block(items)], "\n".join(fallback))
        return
    if view in ("info", "awards") and len(lines) > 1:
        summary, *rest = lines
        values = [line.split(" · ", 1) for line in rest if line and not line.startswith("/")]
        cells = [[R.cell(R.bold(label)), R.cell(value)] for label, value in values]
        blocks = [R.heading(title), R.pullquote(R.bold(summary))]
        if cells:
            blocks.append(R.table(cells, compact=True, striped=True))
        blocks.extend(R.footer(line) for line in rest if line.startswith("/"))
        fallback = [inline(R.bold(title)), inline(R.bold(summary)),
                    *(inline(_line(line)) for line in rest if line)]
        await R.reply_rich(message, blocks, "\n".join(fallback))
        return
    if view in ("teams", "stats", "player", "scorecard"):
        lead, sections = _split_sections(lines)
        if sections:
            heading = inline(R.bold(title))
            opening = [R.paragraph(_line(line)) for line in lead]
            prefix = "\n".join([heading, *(inline(_line(line)) for line in lead)])
            limit = R.CHUNK_LIMIT - len(prefix) - 25
            cards = (card for label, body in sections
                     for card in _section_cards(label, body, limit, expanded=view == "player"))
            for page in _pack_cards(cards, limit):
                blocks = [R.heading(title), *opening, *(block for block, _ in page)]
                await R.reply_rich(message, blocks, "\n".join([prefix, *(html for _, html in page)]))
            return
    await _reply(message, title, lines)


async def today_handler(update, context):
    message = update.effective_message
    if len(context.args) > 1:
        await _reply(message, "Today's fixtures", "/wtctoday [CHAMPIONSHIP_PREFIX]")
        return
    viewer_id = update.effective_user.id if update.effective_user else None
    prefix = context.args[0] if context.args else ""
    with get_session() as db:
        championships = cup.command_championships(db, prefix, viewer_id=viewer_id)
        if len(championships) == 1:
            championship = championships[0]
            await _reply_team_fixtures(message, championship, cup.series_list(db, championship), [],
                                       viewer_id, day=(datetime.utcnow() + IST_OFFSET).date())
            return
        lines = (["Choose a championship.", *(f"/wtctoday {c.command} — {c.name}" for c in championships)]
                 if championships else ["World T20 Championship not found."])
    await _reply(message, "Today's fixtures", lines)


async def _deadline(update, db, championship, args):
    title = f"{championship.name} · Match deadline"
    viewer = update.effective_user
    if not viewer or not is_admin(viewer.id):
        await _reply(update.effective_message, title, "Only bot admins can extend a match deadline.")
        return
    if len(args) not in (2, 3):
        await _reply(update.effective_message, title,
                     f"/{championship.command} deadline S1M1 YYYY-MM-DD [HH:MM] — IST")
        return
    try:
        fixture, previous = cup.extend_deadline(db, championship.id, args[0], " ".join(args[1:]))
        lines = [f"{cup.match_reference(fixture)} · {cup.series_name(fixture.series)}",
                 f"Previous deadline · {format_ist(previous)}",
                 f"New deadline · {format_ist(fixture.deadline)}"]
        db.commit()
    except (ValueError, OverflowError) as exc:
        db.rollback()
        lines = [str(exc)]
    await _reply(update.effective_message, title, lines)


async def dispatch(update, context, db, command):
    found = cup.find_command(db, command)
    if not found:
        return False
    championship, view = found
    message = update.effective_message
    args = message.text.split()[1:]
    from handlers import challenge
    bare = command == championship.command
    if bare and args and args[0].lower() in cup.SUBCOMMANDS:
        view, args = args[0].lower(), args[1:]
        if view == "teamfixtures":
            view = "teamfix"
        bare = False
    if view == "play" and (not bare or challenge._reply_target_telegram_user(update)):
        await _play(update, context, db, championship, args)
    elif view == "table":
        table = cup.standings(championship, cup.series_list(db, championship))
        await _reply_standings(message, championship, table)
    elif view in ("teamfix", "today"):
        viewer = getattr(update, "effective_user", None)
        day = (datetime.utcnow() + IST_OFFSET).date() if view == "today" else None
        await _reply_team_fixtures(message, championship, cup.series_list(db, championship),
                                   args, viewer.id if viewer else None, day=day)
    elif view == "deadline":
        await _deadline(update, db, championship, args)
    elif view in FIXTURE_VIEWS:
        rows = cup.series_list(db, championship)
        try:
            team_id, series_id = _fixture_filters(championship, rows, view, args)
        except ValueError as exc:
            await _reply(message, f"{championship.name} · {VIEW_TITLES[view]}", str(exc))
        else:
            blocks, fallback, markup = _fixture_page(championship, rows, view, team_id, series_id, 0)
            await R.reply_rich(message, blocks, fallback, reply_markup=markup)
    else:
        view = "info" if view == "play" else view
        try:
            content = render(db, championship, view, args)
        except ValueError as exc:
            content = [str(exc)]
        await _reply_view(message, f"{championship.name} · {VIEW_TITLES[view]}", view, content)
    return True


async def _play(update, context, db, championship, args):
    from handlers import challenge
    from services import tournament_only_service
    message = update.effective_message
    reply = partial(_reply, message, f"{championship.name} · Match setup")
    if not championship.started_at or championship.completed_at:
        await reply("This championship has no matches open for play.")
        return
    if update.effective_chat.id > 0:
        await reply("Play in a group by replying to your opponent's message.")
        return
    host, target = await challenge._resolve_challenge_pair(update, db, reply=reply)
    if not host or not target:
        return
    if any(tournament_only_service.is_restricted(user, session=db) for user in (host, target)):
        await reply("One captain is restricted to their assigned fixtures in another tournament.")
        return
    rows = cup.series_list(db, championship)
    next_by_team = cup.next_fixtures(rows)
    eligible = []
    target_fixture = cup.find_fixture(rows, args[0]) if args else None
    if len(args) > 1 or (args and target_fixture is None):
        await reply(f"Use /{championship.command}play [fixture ID or S1M1] as a reply to your opponent.")
        return
    for series in rows:
        for fixture in series.fixtures:
            if target_fixture is not None and fixture.id != target_fixture.id:
                continue
            if cup.fixture_status(championship, fixture, next_by_team) != "available":
                continue
            teams = cup.meeting(fixture, host.telegram_id, target.telegram_id)
            if teams:
                eligible.append(fixture)
    if not eligible:
        await reply(
            f"No fixture between your assigned teams is available now. Check /{championship.command}fixtures.")
        return
    if len(eligible) > 1:
        ids = ", ".join(str(fixture.id) for fixture in eligible)
        await reply(
            f"Choose fixture {ids} with /{championship.command}play FIXTURE_ID.")
        return
    try:
        fixture, pitch_mode, stadium, pitch = cup.prepare_conditions(db, championship.id, eligible[0].id)
        teams = cup.meeting(fixture, host.telegram_id, target.telegram_id)
        if not teams:
            raise cup.FixtureUnavailable("Team assignments changed. Start the fixture again.")
        if teams[0].id != fixture.series.home_id:
            host, target = target, host
            teams = teams[::-1]
        db.commit()
    except cup.FixtureUnavailable as exc:
        db.rollback()
        await reply(str(exc))
        return
    host_team, target_team = teams
    series = fixture.series
    tag = dict(championship_id=championship.id, fixture_id=fixture.id,
               users=[host.id, target.id], teams=[host_team.id, target_team.id],
               challenge_teams=[host_team.challenge_team_id, target_team.challenge_team_id],
               pitch_mode=pitch_mode, pitch=pitch, stadium=stadium,
               team_codes={team.name: team.team.short_name or team.name for team in (host_team, target_team)})
    title = championship.name
    if cup.is_postseason(series):
        stage = "Final" if series.is_final else f"Semifinal {series.semifinal_no}"
        title = f"{title} · {stage} · Match {fixture.number}"
    ok, error = await challenge.launch_cl_tour_match(
        context, message_obj=message, chat_id=update.effective_chat.id, host=host,
        target=target, league_key=championship.command, league_name=title,
        league_record=championship.league, host_team=host_team.name, target_team=target_team.name,
        session=db, championship=tag)
    if not ok:
        await reply(error)
