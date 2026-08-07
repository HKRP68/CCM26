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
    title. A single block longer than the limit still gets its own message
    rather than being dropped — a rejected send is visible, a silently missing
    section is not.

    Returns a list of strings, always at least one.
    """
    blocks = [b for b in blocks if b]
    messages, current, size = [], [], len(header)
    for block in blocks:
        if current and size + len(block) + 1 > limit:
            messages.append(current)
            current, size = [], 0
        current.append(block)
        size += len(block) + 1
    if current:
        messages.append(current)
    if not messages:
        return ["\n".join(p for p in (header, footer) if p)]

    out = []
    for i, group in enumerate(messages):
        parts = ([header] if (i == 0 and header) else []) + list(group)
        if i == len(messages) - 1 and footer:
            parts.append(footer)
        out.append("\n".join(parts))
    return out
