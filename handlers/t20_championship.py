"""Telegram commands scoped to a World T20 Championship."""
import json

from models import User
from services import t20_championship as cup
from services import t20_championship_stats as stats
from utils.message_chunks import chunk_blocks
from utils.tournament_time import format_ist


def command_help(prefix):
    return [
        f"/{prefix} or /{prefix}play [FIXTURE_ID] — reply to your scheduled opponent to play",
        f"/{prefix}info — championship overview",
        f"/{prefix}table — points and series standings",
        f"/{prefix}fixtures [team=ID] — schedule, deadlines and results",
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
    lines = [f"{cup.series_name(fixture.series)} · Match {fixture.number} · #{fixture.id}",
             result or cup.STATUS.get(fixture.status, "Upcoming")]
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


def _fixture_blocks(championship, rows, selected):
    next_by_team = cup.next_fixtures(rows)
    for series in selected:
        header = f"\nSeries #{series.id} · {cup.series_name(series)}\n{series_summary(series)}"
        place = " · ".join(value for value in (series.venue, series.stadium, series.pitch) if value)
        if place:
            header += "\n" + place
        yield header
        for fixture in series.fixtures:
            status = cup.STATUS[cup.fixture_status(championship, fixture, next_by_team)]
            result = result_label(fixture)
            yield (f"Match {fixture.number} · #{fixture.id} · {status}"
                   f"{' · ' + result if result else ''}\n"
                   f"Opens {format_ist(fixture.opens_at)} · Deadline {format_ist(fixture.deadline)}")


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
                f"Semifinals · {cup.PLAYOFFS[championship.playoff_format]} · /{prefix} playoffs"
                if championship.playoff_format != "none" else "",
                f"Champion · {champion.name}" if champion else "",
                f"/{prefix}table · /{prefix}fixtures · /{prefix}teams · /{prefix}help"]
    if view == "table":
        table = cup.standings(championship, rows)
        return ["Pos · Team · P · W · L · T · NR · Bonus · Total Pts · PCT · NRR",
                *(f'{i} · {row["team"].name} · {row["played"]} · {row["won"]} · {row["lost"]} · '
                  f'{row["tied"]} · {row["nr"]} · {row["bonus"]} · {row["points"]} · {float(row["pct"]):.2f}% · '
                  f'{float(row["nrr"]):+.3f}' for i, row in enumerate(table, 1)),
                "\nSeries · Played · Won · Lost · Drawn · Home wins · Away wins",
                *(f'{row["team"].name} · {row["series_played"]} · {row["series_won"]} · '
                  f'{row["series_lost"]} · {row["series_drawn"]} · {row["home_wins"]} · '
                  f'{row["away_wins"]}' for row in table)]
    if view == "teams":
        names = person_names(db, (uid for team in championship.teams
                                 for uid in (team.owner_id, team.co_owner_id)))
        lines = []
        for team in sorted(championship.teams, key=lambda team: team.name.casefold()):
            reserve = " · Reserve team" if not team.is_participating else ""
            lines.append(f"\n#{team.id} · {team.name}{reserve} · Owner {names.get(team.owner_id, team.owner_id)}"
                         + (f" · Co-owner {names.get(team.co_owner_id, team.co_owner_id)}" if team.co_owner_id else ""))
            lines.extend(f"{p.name} · {p.source_player.version or 'Base'}"
                         if p.source_player else p.name for p in team.team.players)
        return lines or ["No teams have been added."]
    if view == "playoffs":
        if championship.playoff_format == "none":
            return ["Top-four semifinals are off.", f"/{prefix}final"]
        postseason = [row for row in rows if cup.is_postseason(row)]
        return [f"Semifinals · {cup.PLAYOFFS[championship.playoff_format]}",
                "Seeds 1 vs 4 and 2 vs 3 · Winners meet in the final.",
                *(list(_fixture_blocks(championship, rows, postseason)) or
                  ["The bracket opens after the league series finish."])]
    if view in ("series", "fixtures", "final"):
        if view == "final":
            final = [row for row in rows if row.is_final]
            if championship.final_format == "none":
                qualification = "The table leader becomes champion."
            elif championship.playoff_format != "none":
                qualification = f"The semifinal winners qualify. See /{prefix} playoffs."
            else:
                qualification = "The top two qualify after the league series finish."
            return [f"Final · {cup.FINALS[championship.final_format]}",
                    f"Champion · {champion.name}" if champion else "",
                    *(list(_fixture_blocks(championship, rows, final)) or [qualification])]
        query, filters = _filters(championship, rows, args, allowed={"team"})
        selected = [row for row in rows if filters.get("team_id") in (None, row.home_id, row.away_id)]
        if view == "series" and query:
            if not query.isdecimal():
                raise ValueError(f"Use /{prefix}series ID.")
            selected = [row for row in selected if row.id == int(query)]
        blocks = list(_fixture_blocks(championship, rows, selected))
        if view == "series" and query and selected:
            best = next((row for row in stats.aggregate(selected) if row["pots"]), None)
            if best:
                blocks.append(f"Player of the Series · {stats.player_label(best)}")
        return blocks or ["No matching series."]
    if view == "scorecard":
        if len(args) != 1 or not args[0].isdecimal():
            raise ValueError(f"Use /{prefix}scorecard FIXTURE_ID. Find the ID in /{prefix}fixtures.")
        match = next((f for row in rows for f in row.fixtures if f.id == int(args[0])), None)
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
        bare = False
    if view == "play" and (not bare or challenge._reply_target_telegram_user(update)):
        await _play(update, context, db, championship, args)
    else:
        try:
            blocks = render(db, championship, "info" if view == "play" else view, args)
        except ValueError as exc:
            blocks = [str(exc)]
        for text in chunk_blocks(blocks, header=championship.name):
            await message.reply_text(text)
    return True


async def _play(update, context, db, championship, args):
    from handlers import challenge
    from services import tournament_only_service
    message = update.effective_message
    if not championship.started_at or championship.completed_at:
        await message.reply_text("This championship has no matches open for play.")
        return
    if update.effective_chat.id > 0:
        await message.reply_text("Play in a group by replying to your opponent's message.")
        return
    host, target = await challenge._resolve_challenge_pair(update, db)
    if not host or not target:
        return
    if any(tournament_only_service.is_restricted(user, session=db) for user in (host, target)):
        await message.reply_text("One captain is restricted to their assigned fixtures in another tournament.")
        return
    rows = cup.series_list(db, championship)
    next_by_team = cup.next_fixtures(rows)
    eligible = []
    if len(args) > 1 or (args and not args[0].isdecimal()):
        await message.reply_text(f"Use /{championship.command}play [fixture ID] as a reply to your opponent.")
        return
    for series in rows:
        for fixture in series.fixtures:
            if args and fixture.id != int(args[0]):
                continue
            if cup.fixture_status(championship, fixture, next_by_team) != "available":
                continue
            teams = cup.meeting(fixture, host.telegram_id, target.telegram_id)
            if teams:
                eligible.append((fixture, teams))
    if not eligible:
        await message.reply_text(
            f"No fixture between your assigned teams is available now. Check /{championship.command}fixtures.")
        return
    if len(eligible) > 1:
        ids = ", ".join(str(fixture.id) for fixture, _ in eligible)
        await message.reply_text(
            f"Choose fixture {ids} with /{championship.command}play FIXTURE_ID.")
        return
    fixture, (host_team, target_team) = eligible[0]
    series = fixture.series
    tag = dict(championship_id=championship.id, fixture_id=fixture.id,
               users=[host.id, target.id], teams=[host_team.id, target_team.id],
               challenge_teams=[host_team.challenge_team_id, target_team.challenge_team_id],
               pitch=series.pitch, stadium=series.stadium,
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
        await message.reply_text(error)
