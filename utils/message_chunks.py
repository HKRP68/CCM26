"""Splitting a long message into sends Telegram will actually accept.

Telegram rejects a message over 4,096 characters outright — it does not
truncate — so a listing that grows past that limit stops working entirely, and
it does so on the day the content grows rather than the day the code changed.

The rule that makes this safe is splitting between whole pre-rendered blocks
rather than at a character count: a blind slice can land inside an HTML tag or
between a ``<blockquote>`` and its closing tag, and Telegram rejects that too,
so the "fix" for a long message would trade one failure for another.

No Telegram and no database here, so the packing is directly testable.
"""

# A little under Telegram's 4,096 so a header, a footer and a stray wide emoji
# can't tip a packed message over the line.
DEFAULT_CHUNK_LIMIT = 3800


def chunk_blocks(blocks, header="", footer="", limit=DEFAULT_CHUNK_LIMIT):
    """Group pre-rendered ``blocks`` into messages that fit the limit.

    ``header`` goes on the first message and ``footer`` on the last, so a
    multi-message listing reads as one thing rather than repeating its own
    title.

    **The guarantee:** every returned message is within ``limit``, unless a
    single indivisible piece — one block, or the header or footer on its own —
    is itself longer than that. Those are passed through rather than dropped: a
    rejected send is visible, a silently missing section is not.

    Two things make the guarantee hold that a naive packer gets wrong. The
    footer is reserved in the budget up front, because it is appended after
    packing — filling to ``limit`` and then adding it is how a "safe" 3,800
    produces a 4,100-character send. And when the last group still cannot take
    the footer (its one block was already oversized), the footer goes out as its
    own message instead of overflowing the one before it.

    Returns a list of strings, always at least one.
    """
    blocks = [b for b in blocks if b]
    footer_cost = len(footer) + 1 if footer else 0
    budget = max(1, limit - footer_cost)

    messages, current, size = [], [], len(header)
    for block in blocks:
        if current and size + len(block) + 1 > budget:
            messages.append(current)
            current, size = [], 0
        current.append(block)
        size += len(block) + 1
    if current:
        messages.append(current)

    if not messages:
        # Nothing to page: the header and footer are the whole message.
        return ["\n".join(p for p in (header, footer) if p)] if (header or footer) else [""]

    out = []
    for i, group in enumerate(messages):
        parts = ([header] if (i == 0 and header) else []) + list(group)
        out.append("\n".join(parts))

    if footer:
        if len(out[-1]) + footer_cost <= limit:
            out[-1] = f"{out[-1]}\n{footer}"
        else:
            # The last message is already at or over the limit on its own — an
            # oversized block. Appending the footer would push a second message
            # over too, so it gets its own.
            out.append(footer)
    return out


# ── Tap-to-expand sections ───────────────────────────────────────────
#
# ``<blockquote expandable>`` is Telegram's own collapsible: the client shows
# the first few lines and a "expand" affordance, and nothing is lost. Every long
# section in this bot — the ranks past tenth on a stat board, the fixtures past
# the twentieth, a bench list — rides in one rather than being truncated with
# "…and 22 more.", which is not an answer to the person whose row was number 23.
#
# The catch is that a blockquote is *indivisible* to the packer above. A quote
# longer than one message cannot be split: every cut lands between the tag and
# its closing tag, which Telegram rejects outright as unparseable entities — so
# the section that grew too long stops being sent at all. Wrapping a long body
# as SEVERAL quotes, each inside the limit, is what keeps it sendable however
# far the list grows.

_QUOTE_OPEN = "<blockquote expandable>"
_QUOTE_CLOSE = "</blockquote>"


def expandable_quotes(lines, limit=DEFAULT_CHUNK_LIMIT):
    """``lines`` as expandable blockquotes, each within ``limit`` characters.

    Returns a list of ready-to-send strings — usually one, and more only when
    the body is longer than a single message can carry. Empty in, empty out, so
    a caller can splice the result into its output unconditionally.

    Each returned quote is its own block for :func:`chunk_blocks`, which splits
    between blocks and therefore never inside one of them.
    """
    lines = [str(line) for line in lines if line]
    if not lines:
        return []
    budget = max(1, limit - len(_QUOTE_OPEN) - len(_QUOTE_CLOSE))
    return [_QUOTE_OPEN + body + _QUOTE_CLOSE
            for body in chunk_blocks(lines, limit=budget)]
