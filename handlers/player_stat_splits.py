"""Telegram rendering shared by /statstour and /seasons."""

from html import escape

from services import player_stat_splits as splits
from utils.message_chunks import chunk_blocks, expandable_quotes


def _rates(stats):
    def rate(numerator, denominator, scale=1):
        return f"{scale * numerator / denominator:.2f}" if denominator else "—"

    bat = "🏏 " + _figure(stats.runs, "run") + " off " + _figure(stats.faced, "ball")
    bat += f" · Avg {rate(stats.runs, stats.outs)} · SR {rate(stats.runs, stats.faced, 100)}"
    bowl = ("🎯 " + _figure(stats.wickets, "wkt") + " · "
            + _figure(stats.conceded, "run") + " off " + _figure(stats.legal, "ball"))
    bowl += f" · Econ/6 {rate(stats.conceded, stats.legal, 6)}"
    return bat, bowl


def _figure(n, word, plural="s"):
    return f"{n:,} {word}{plural if n != 1 else ''}"


def _split_text(label, stats):
    heading = f"<b>{escape(label)}</b>"
    if not stats or not stats.matches:
        return heading + " · No matching stats"
    return "\n".join([f"{heading} · {_figure(stats.matches, 'match', 'es')}", *_rates(stats)])


def _section(label, report, filters, season_rows):
    labels = list(splits.group_labels(filters))
    if len(labels) == 1:
        blocks = [f"\n<b>{escape(label)}</b>", _split_text(labels[0][1], report.total)]
    else:
        blocks = [f"\n<b>{escape(label)}</b>", _split_text("Selected filters combined", report.total)]
        compare_one_dimension = sum(len(values) > 1 for values in filters) == 1
        for group, group_label in labels:
            stats = report.groups.get(group)
            if stats or compare_one_dimension:
                blocks.append(_split_text(group_label, stats))
    if season_rows:
        rows = [f"<b>{escape(title)}</b> · {_figure(stats.matches, 'match', 'es')}\n"
                + "\n".join(_rates(stats))
                for title, stats in season_rows if stats.matches]
        if rows:
            blocks += ["<b>Season by season</b>", *expandable_quotes(rows, limit=2700)]
    coverage = []
    if report.unavailable_matches:
        coverage.append(f"{_figure(report.unavailable_matches, 'match', 'es')} had missing filter data")
    if report.unavailable_seasons:
        n = report.unavailable_seasons
        coverage.append(f"{n} season{'s' if n != 1 else ''} lacked match-level records")
    if coverage:
        blocks.append(f"<i>{' · '.join(coverage)}</i>")
    return blocks


def pages(scope, player, filters, reports):
    header = f"📊 <b>{escape(player)}</b> · {escape(scope)}"
    blocks = []
    for label, report, season_rows in reports:
        blocks.extend(_section(label, report, filters, season_rows))
    return chunk_blocks(blocks, header=header)
