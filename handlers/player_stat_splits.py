"""Telegram rendering shared by /statstour and /seasons."""

from html import escape

from services import player_stat_splits as splits
from services import rich_message as R
from utils.message_chunks import DEFAULT_CHUNK_LIMIT


def _rates(stats):
    def rate(numerator, denominator, scale=1):
        return f"{scale * numerator / denominator:.2f}" if denominator else "—"

    bat = _figure(stats.runs, "run") + " off " + _figure(stats.faced, "ball")
    bat += f" · Avg {rate(stats.runs, stats.outs)} · SR {rate(stats.runs, stats.faced, 100)}"
    bowl = (_figure(stats.wickets, "wkt") + " · "
            + _figure(stats.conceded, "run") + " off " + _figure(stats.legal, "ball"))
    bowl += f" · Econ/6 {rate(stats.conceded, stats.legal, 6)}"
    return bat, bowl


def _figure(n, word, plural="s"):
    return f"{n:,} {word}{plural if n != 1 else ''}"


def _split_card(label, stats):
    if not stats or not stats.matches:
        return ([R.paragraph([R.bold(label), " · No matching stats"])],
                f"<b>{escape(label)}</b> · No matching stats")
    matches = _figure(stats.matches, "match", "es")
    bat, bowl = _rates(stats)
    return (
        [R.paragraph([R.bold(label), f" · {matches}"]),
         R.table([[R.cell(R.bold("🏏 Batting")), R.cell(bat)],
                  [R.cell(R.bold("🎯 Bowling")), R.cell(bowl)]],
                 bordered=True, compact=True)],
        f"<b>{escape(label)}</b> · {matches}\n🏏 {bat}\n🎯 {bowl}",
    )


def _batches(cards, limit):
    """Keep each HTML page and its native blocks in sync when splitting."""
    native, html, length = [], [], 0
    for blocks, text in cards:
        size = len(text) + 1
        if html and length + size > limit:
            yield native, "\n".join(html)
            native, html, length = [], [], 0
        native.extend(blocks)
        html.append(text)
        length += size
    if html:
        yield native, "\n".join(html)


def _section(label, report, filters, season_rows):
    labels = list(splits.group_labels(filters))
    total_label = labels[0][1] if len(labels) == 1 else "Selected filters combined"
    blocks, text = _split_card(total_label, report.total)
    yield [R.heading(label, size=3), *blocks], f"\n<b>{escape(label)}</b>\n{text}"
    if len(labels) > 1:
        compare_one_dimension = sum(len(values) > 1 for values in filters) == 1
        for group, group_label in labels:
            stats = report.groups.get(group)
            if stats or compare_one_dimension:
                yield _split_card(group_label, stats)
    rows = (_split_card(title, stats) for title, stats in season_rows if stats.matches)
    for blocks, text in _batches(rows, limit=2600):
        yield (
            [R.details(R.bold("Season by season"), blocks)],
            f"<b>Season by season</b>\n<blockquote expandable>{text}</blockquote>",
        )
    coverage = []
    if report.unavailable_matches:
        coverage.append(f"{_figure(report.unavailable_matches, 'match', 'es')} had missing filter data")
    if report.unavailable_seasons:
        n = report.unavailable_seasons
        coverage.append(f"{n} season{'s' if n != 1 else ''} lacked match-level records")
    if coverage:
        note = " · ".join(coverage)
        yield [R.paragraph(R.italic(note))], f"<i>{escape(note)}</i>"


def pages(scope, player, filters, reports):
    header = f"📊 <b>{escape(player)}</b> · {escape(scope)}"
    title = [R.heading(f"📊 {player}", size=2), R.paragraph(R.italic(scope))]
    cards = (card for label, report, season_rows in reports
             for card in _section(label, report, filters, season_rows))
    return [(title + blocks, f"{header}\n{text}")
            for blocks, text in _batches(cards, DEFAULT_CHUNK_LIMIT - len(header))]


async def reply_pages(message, pages, *, reply_markup=None):
    for index, (blocks, text) in enumerate(pages, start=1):
        markup = reply_markup if index == len(pages) else None
        await R.reply_rich(message, blocks, text, reply_markup=markup)
