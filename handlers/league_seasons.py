"""/seasons — a competition's seasons, champions and players, for everyone.

* ``/seasons`` — the one competition's card (or a picker when there are
  several): every season newest first — where the running one stands, and
  each finished one's champion, runner-up and caps.
* ``/seasons <competition>`` — that competition.
* ``/seasons <player>`` · ``/seasons <competition> | <player>`` — one player
  season by season, with the total.

Buttons switch between the competition, one season (champion, awards, its top
five batters and bowlers) and the all-time boards (runs, wickets, sixes,
highest score, best figures, average, titles). They belong to whoever asked,
the ``/tournamentstats`` way: the opener's id rides in the callback data.

Every card is a Bot API 10.1 rich message beside its HTML twin, through
``services.rich_message`` — the same pairing ``handlers/tournament.py`` uses,
whose stat formatters this reuses so a player's numbers read the same here as
in ``/statstour``. The data is ``services/league_season_service.py``'s.
"""

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from services import league_season_service as LS
from services import rich_message as R

logger = logging.getLogger(__name__)

CB = "ls_"
NO_SERIES = ("🏟 No competitions yet — an admin sets them up on the website "
             "(Tournament Panel → League Seasons).")
CATEGORY_LABELS = dict(LS.ALL_TIME_CATEGORIES)


def _e(value):
    return html.escape(str(value if value is not None else ""))


def _award_text(h, key):
    a = h.get(key) or {}
    who = a.get("player") or a.get("team")
    if not who:
        return None
    value = a.get("value") if a.get("player") else None
    return f"{who}" + (f" {value}" if value else "")


def _season_summary(row):
    """``"🏆 Mumbai · 🥈 Chennai · 🟠 Kohli 612 runs · 🟣 Bumrah 24 wkts"`` or
    the stage of a running season."""
    st, h = row["stage"], row["honours"]
    if st["done"] and (h.get("champion") or {}).get("team"):
        bits = [f"🏆 {h['champion']['team']}"]
        for key, icon in (("runner_up", "🥈"), ("orange_cap", "🟠"),
                          ("purple_cap", "🟣")):
            text = _award_text(h, key)
            if text:
                bits.append(f"{icon} {text}")
        return " · ".join(bits)
    label = f"{st['emoji']} {st['label']}"
    if st.get("detail"):
        label += f" ({st['detail']})"
    if row["season"].squad_mode in (LS.MODE_MEGA, LS.MODE_MINI) and not st["done"]:
        label = f"{row['mode']} · {label}"
    return label


# ── Keyboards ────────────────────────────────────────────────────────

def _btn(label, view, series_id, arg, opener):
    return InlineKeyboardButton(label, callback_data=f"{CB}{view}_{series_id}_{arg}_{opener}")


def _overview_keyboard(series, rows, opener):
    buttons = [[_btn("📊 All-time", "a", series.id, "runs", opener)]]
    line = []
    for row in rows[:12]:
        line.append(_btn(f"S{row['season'].season_no}", "s", series.id,
                         row["season"].id, opener))
        if len(line) == 4:
            buttons.append(line)
            line = []
    if line:
        buttons.append(line)
    return InlineKeyboardMarkup(buttons)


def _back_keyboard(series, opener, extra=None):
    row = [_btn("◀ Seasons", "o", series.id, 0, opener),
           _btn("📊 All-time", "a", series.id, "runs", opener)]
    rows = [row]
    if extra:
        rows.insert(0, extra)
    return InlineKeyboardMarkup(rows)


def _alltime_keyboard(series, active, opener):
    rows, line = [], []
    for key, label in LS.ALL_TIME_CATEGORIES:
        mark = "● " if key == active else ""
        line.append(_btn(f"{mark}{label}", "a", series.id, key, opener))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([_btn("◀ Seasons", "o", series.id, 0, opener)])
    return InlineKeyboardMarkup(rows)


def _picker_keyboard(series_list, opener):
    return InlineKeyboardMarkup([[_btn(f"🏆 {s.name}", "o", s.id, 0, opener)]
                                 for s in series_list[:20]])


# ── Cards: (blocks, html) ────────────────────────────────────────────

def overview_card(session, series):
    rows = LS.overview(session, series)
    titles = LS.titles(session, series)
    head = f"🏆 {series.name} — every season"
    html_lines = [f"<b>{_e(head)}</b>"]
    items = []
    for row in rows:
        line = f"S{row['season'].season_no} · {row['title']} — {_season_summary(row)}"
        html_lines.append(_e(line))
        items.append(line)
    if not rows:
        html_lines.append("<i>No seasons yet.</i>")
    if titles:
        most = ", ".join(f"{team} {n}" for team, n, _w in titles[:3])
        html_lines.append(f"\n🏆 <b>Titles:</b> {_e(most)}")
    html_lines.append("\n<i>Tap a season, or 📊 All-time. "
                      "/seasons &lt;player&gt; for one player.</i>")
    blocks = [R.heading(head, size=2)]
    blocks.append(R.list_block(items) if items
                  else R.paragraph(R.italic("No seasons yet.")))
    if titles:
        blocks.append(R.paragraph([R.bold("🏆 Titles: "),
                                   ", ".join(f"{t} {n}" for t, n, _w in titles[:3])]))
    blocks.append(R.footer(["Tap a season, or 📊 All-time. ",
                            R.code("/seasons <player>"), " for one player."]))
    return blocks, "\n".join(html_lines), rows


def _top_rows(stats, kind):
    from handlers.tournament import _econ, _figure, _sr
    out = []
    for i, s in enumerate(stats, start=1):
        if kind == "bat":
            out.append((i, s.name, s.team_name, s.bat_runs,
                        f"SR {_sr(s):.1f}"))
        else:
            out.append((i, s.name, s.team_name, s.bowl_wickets,
                        f"Econ {_econ(s):.2f}" + (f" · Best {_figure(s)}"
                                                  if _figure(s) else "")))
    return out


def _rank_table(rows, value_head):
    cells = [[R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("PLAYER"), header=True),
              R.cell(R.bold(value_head), header=True, align="right"),
              R.cell(R.bold(""), header=True, align="right")]]
    for i, name, team, value, extra in rows:
        who = name if not team else f"{name} ({team})"
        cells.append([R.cell(str(i), align="center"), R.cell(who),
                      R.cell(R.bold(str(value)), align="right"),
                      R.cell(extra, align="right")])
    return R.table(cells, bordered=True, striped=True, compact=True)


def season_card(session, series, season):
    row = next((r for r in LS.overview(session, series)
                if r["season"].id == season.id), None)
    h = LS.honours(season)
    head = f"🏆 {series.name} — {LS.season_title(season)}"
    lines = [f"<b>{_e(head)}</b>", _e(f"Squads: {LS.mode_label(season)}")]
    blocks = [R.heading(head, size=2),
              R.paragraph(f"Squads: {LS.mode_label(season)}")]
    if row is not None and not row["stage"]["done"]:
        lines.append(_e(_season_summary(row)))
        blocks.append(R.paragraph(_season_summary(row)))
    award_lines = []
    for key, icon, label in (("champion", "🏆", "Champion"),
                             ("runner_up", "🥈", "Runner-up"),
                             ("orange_cap", "🟠", "Orange Cap"),
                             ("purple_cap", "🟣", "Purple Cap"),
                             ("mvp", "⭐", "MVP")):
        text = _award_text(h, key)
        if text:
            award_lines.append(f"{icon} {label}: {text}")
    if award_lines:
        lines.append("\n" + "\n".join(_e(a) for a in award_lines))
        blocks.append(R.list_block(award_lines))
    top = LS.season_top(session, season)
    bat, bowl = _top_rows(top["batters"], "bat"), _top_rows(top["bowlers"], "bowl")
    if bat:
        lines.append("\n<b>🏏 Top batters</b>")
        lines += [_e(f"{i}. {n}" + (f" ({t})" if t else "") + f" — {v} runs · {x}")
                  for i, n, t, v, x in bat]
        blocks.append(R.paragraph(R.bold("🏏 Top batters")))
        blocks.append(_rank_table(bat, "RUNS"))
    if bowl:
        lines.append("\n<b>🎯 Top bowlers</b>")
        lines += [_e(f"{i}. {n}" + (f" ({t})" if t else "") + f" — {v} wkts · {x}")
                  for i, n, t, v, x in bowl]
        blocks.append(R.paragraph(R.bold("🎯 Top bowlers")))
        blocks.append(_rank_table(bowl, "WKTS"))
    if not award_lines and not bat and not bowl:
        lines.append("<i>No results or stats yet.</i>")
        blocks.append(R.paragraph(R.italic("No results or stats yet.")))
    return blocks, "\n".join(lines)


def _value_for(category, s):
    from handlers.tournament import _figure
    from services.tournament_service import batting_average
    if category == "wkts":
        return s.bowl_wickets, f"{s.bowl_runs} runs conceded"
    if category == "sixes":
        return s.bat_sixes, f"{s.bat_runs} runs"
    if category == "hs":
        return s.highest_score, f"{s.bat_runs} runs total"
    if category == "fig":
        return _figure(s) or "—", f"{s.bowl_wickets} wkts total"
    if category == "avg":
        avg = batting_average(s)
        return f"{avg:.2f}" if avg is not None else "—", f"{s.bat_runs} runs"
    return s.bat_runs, f"{s.bat_balls}b"


def alltime_card(session, series, category):
    label = CATEGORY_LABELS.get(category, CATEGORY_LABELS["runs"])
    head = f"📊 {series.name} — all-time {label}"
    lines = [f"<b>{_e(head)}</b>"]
    blocks = [R.heading(head, size=2)]
    if category == "titles":
        titles = LS.titles(session, series)
        if not titles:
            lines.append("<i>No champions yet.</i>")
            blocks.append(R.paragraph(R.italic("No champions yet.")))
        items = []
        for i, (team, n, which) in enumerate(titles, start=1):
            text = f"{i}. {team} — {n} title{'' if n == 1 else 's'} ({', '.join(which)})"
            lines.append(_e(text))
            items.append(text)
        if items:
            blocks.append(R.list_block(items))
        return blocks, "\n".join(lines)
    board = LS.all_time_leaders(session, series, category, limit=15)
    if not board:
        lines.append("<i>No stats yet.</i>")
        blocks.append(R.paragraph(R.italic("No stats yet.")))
        return blocks, "\n".join(lines)
    rows = []
    for i, s in enumerate(board, start=1):
        value, extra = _value_for(category, s)
        extra = f"{extra} · {s.seasons} season{'' if s.seasons == 1 else 's'}"
        rows.append((i, s.name, None, value, extra))
        lines.append(_e(f"{i}. {s.name} — {value} · {extra}"))
    blocks.append(_rank_table(rows, label.split(" ", 1)[-1].upper()[:8]))
    blocks.append(R.footer("Every season of the competition added up."))
    return blocks, "\n".join(lines)


def player_card(series, result):
    from handlers.tournament import _season_line, _stat_tables, _stats_html
    name, total = result["name"], result["total"]
    head = f"👤 {name} — {series.name}"
    seasons = int(total.seasons or 0)
    lines = [f"<b>{_e(head)}</b>",
             f"<b>📚 All {seasons} season{'' if seasons == 1 else 's'}</b>",
             f"<blockquote expandable>{_stats_html(total)}</blockquote>"]
    blocks = [R.heading(head, size=2),
              R.details(R.bold(f"📚 All {seasons} season{'' if seasons == 1 else 's'}"),
                        _stat_tables(total), is_open=True)]
    if result["breakdown"]:
        season_lines = [_season_line(label, s) for label, s in result["breakdown"]]
        lines.append("<b>🗂 Season by season</b>")
        lines.append("<blockquote expandable>" + "\n".join(_e(x) for x in season_lines)
                     + "</blockquote>")
        blocks.append(R.details(R.bold("🗂 Season by season"),
                                [R.list_block([[x] for x in season_lines])],
                                is_open=True))
    return blocks, "\n".join(lines)


# ── Commands ─────────────────────────────────────────────────────────

async def _send(update, blocks, text, markup=None):
    await R.reply_rich(update.effective_message, blocks, text, reply_markup=markup)


async def seasons_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/seasons [competition] [| player]"""
    message = update.effective_message
    if message is None:
        return
    user = update.effective_user
    opener = user.id if user else 0
    raw = " ".join(context.args or []).strip()
    session = get_session()
    try:
        active = LS.series_list(session, active_only=True)
        if not active:
            await message.reply_text(NO_SERIES)
            return
        left, bar, right = raw.partition("|")
        series, query = None, ""
        if bar:
            series = LS.find_series(session, left)
            query = right.strip()
            if series is None:
                await message.reply_text(
                    f"❌ No competition called “{left.strip()}”. Try /seasons.")
                return
        elif raw:
            series = LS.find_series(session, raw)
            if series is None:
                query = raw
        if series is None and query:
            hits = [(s, LS.player_history(session, s, query)) for s in active]
            hits = [(s, r) for s, r in hits if r]
            if not hits:
                await message.reply_text(f"❌ No season stats for “{query}”.")
                return
            series = hits[0][0]
        if series is None:
            if len(active) == 1:
                series = active[0]
            else:
                await message.reply_text("🏆 Which competition?",
                                         reply_markup=_picker_keyboard(active, opener))
                return
        if LS.sync_series(session, series):
            session.commit()
        if query:
            result = LS.player_history(session, series, query)
            if not result:
                await message.reply_text(
                    f"❌ No season stats for “{query}” in {series.name}.")
                return
            if "choices" in result:
                await message.reply_text(
                    "🔎 Which one? " + ", ".join(result["choices"])
                    + f"\nUse the full name: /seasons {series.name} | <player>")
                return
            blocks, text = player_card(series, result)
            await _send(update, blocks, text,
                        _back_keyboard(series, opener))
            return
        blocks, text, rows = overview_card(session, series)
        await _send(update, blocks, text, _overview_keyboard(series, rows, opener))
    except Exception:
        session.rollback()
        logger.exception("/seasons failed")
        await message.reply_text("⚠️ Could not load the seasons. Try again.")
    finally:
        session.close()


async def seasons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``ls_<view>_<series>_<arg>_<opener>`` — only the opener may switch."""
    query = update.callback_query
    if query is None:
        return
    try:
        view, series_id, arg, opener = (query.data or "")[len(CB):].split("_", 3)
        series_id, opener = int(series_id), int(opener)
    except (ValueError, AttributeError):
        await query.answer("That button is from an older card.", show_alert=True)
        return
    if query.from_user is None or query.from_user.id != opener:
        await query.answer("Only the person who opened this can use these "
                           "buttons — send /seasons for your own.", show_alert=True)
        return
    session = get_session()
    try:
        series = LS.get_series(session, series_id)
        if series is None:
            await query.answer("That competition no longer exists.", show_alert=True)
            return
        if view == "s":
            season = LS.get_season(session, series, arg)
            if season is None:
                await query.answer("That season is gone.", show_alert=True)
                return
            blocks, text = season_card(session, series, season)
            markup = _back_keyboard(series, opener)
        elif view == "a":
            category = arg if arg in CATEGORY_LABELS else "runs"
            blocks, text = alltime_card(session, series, category)
            markup = _alltime_keyboard(series, category, opener)
        else:
            blocks, text, rows = overview_card(session, series)
            markup = _overview_keyboard(series, rows, opener)
        await query.answer()
        await R.edit_rich(query, blocks, text, reply_markup=markup)
    except Exception:
        session.rollback()
        logger.exception("/seasons callback failed")
    finally:
        session.close()
