"""Shared readable layouts: labels left, numeric values right, no space padding."""

from copy import deepcopy
from html.parser import HTMLParser
import re
from services import rich_message as R


class InlineLines(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.styles = [[]], []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.handle_data("\n")
        elif tag in ("p", "blockquote", "h1", "h2", "h3", "h4", "pre"):
            if self.lines[-1]:
                self.lines.append([])
            if tag == "pre":
                self.styles.append((tag, {}))
        elif tag in (
            "b",
            "strong",
            "i",
            "em",
            "u",
            "s",
            "strike",
            "del",
            "code",
            "a",
            "tg-spoiler",
        ):
            self.styles.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if self.styles and self.styles[-1][0] == tag:
            self.styles.pop()
        if tag in ("p", "blockquote", "h1", "h2", "h3", "h4", "pre") and self.lines[-1]:
            self.lines.append([])

    def handle_data(self, text):
        for index, value in enumerate(text.split("\n")):
            if index:
                self.lines.append([])
            if not value:
                continue
            for tag, attrs in reversed(self.styles):
                value = (
                    R.link(value, attrs.get("href", ""))
                    if tag == "a"
                    else {
                        "type": {
                            "b": "bold",
                            "strong": "bold",
                            "i": "italic",
                            "em": "italic",
                            "u": "underline",
                            "s": "strikethrough",
                            "strike": "strikethrough",
                            "del": "strikethrough",
                            "code": "code",
                            "pre": "code",
                            "tg-spoiler": "spoiler",
                        }[tag],
                        "text": value,
                    }
                )
            self.lines[-1].append(value)


def lines_from_html(text):
    parser = InlineLines()
    parser.feed(str(text or ""))
    parser.close()
    return parser.lines


def plain(value):
    if isinstance(value, list):
        return "".join(plain(item) for item in value)
    if isinstance(value, dict):
        return plain(value.get("text", ""))
    return str("" if value is None else value)


def text_slice(nodes, start, end):
    """Slice display characters while retaining links and nested formatting."""
    result, offset = [], 0
    for node in nodes if isinstance(nodes, list) else [nodes]:
        length = len(plain(node))
        left, right = max(0, start - offset), min(length, end - offset)
        if right > left:
            if isinstance(node, dict):
                piece = deepcopy(node)
                piece["text"] = text_slice(node.get("text", ""), left, right)
            else:
                piece = str(node)[left:right]
            result.append(piece)
        offset += length
    return result


def aligned_blocks(html):
    """Only convert clear runs of facts; preserve complex/native HTML intact."""
    if re.search(
        r"<\s*(?:table|details|pre|h[1-6]|tg-button|tg-time|tg-emoji|span)\b",
        html,
        re.I,
    ):
        return None
    if re.search(r"<\s*blockquote\b", html, re.I):
        matches = list(
            re.finditer(r"<blockquote\b([^>]*)>(.*?)</blockquote>", html, re.I | re.S)
        )
        if not matches or any(
            re.search(r"<\s*blockquote\b", match[2], re.I) for match in matches
        ):
            return None
        result, changed, position = [], False, 0
        for match in matches:
            leading = html[position : match.start()]
            leading_blocks = aligned_blocks(leading)
            changed = changed or bool(leading_blocks)
            result.extend(
                leading_blocks
                or [R.paragraph(line) for line in lines_from_html(leading) if line]
            )
            inner = aligned_blocks(match[2])
            changed = changed or bool(inner)
            body = inner or [
                R.paragraph(line) for line in lines_from_html(match[2]) if line
            ]
            expandable = "expandable" in match[1].lower()
            result.append(
                R.details("📋 Details", body)
                if expandable and inner
                else R.blockquote(body, expandable=expandable)
            )
            position = match.end()
        trailing = html[position:]
        trailing_blocks = aligned_blocks(trailing)
        changed = changed or bool(trailing_blocks)
        result.extend(
            trailing_blocks
            or [R.paragraph(line) for line in lines_from_html(trailing) if line]
        )
        return result if changed else None
    lines = lines_from_html(html)
    blocks, facts, found = [], [], False

    def flush():
        nonlocal found
        if len(facts) >= 2:
            blocks.append(
                R.table(
                    [[R.cell(key), R.cell(value)] for key, value, _ in facts],
                    compact=True,
                    striped=True,
                )
            )
            found = True
        else:
            blocks.extend(R.paragraph(line) for _, _, line in facts)
        facts.clear()

    for line in lines:
        text = plain(line)
        colon = text.find(":")
        key, value = text[:colon].strip(), text[colon + 1 :].strip()
        if (
            0 < colon < 50
            and value
            and any(char.isalpha() for char in key)
            and key.lower() not in ("http", "https")
        ):
            raw_key, raw_value = text[:colon], text[colon + 1 :]
            facts.append(
                (
                    text_slice(
                        line,
                        len(raw_key) - len(raw_key.lstrip()),
                        len(raw_key.rstrip()),
                    ),
                    text_slice(
                        line,
                        colon + 1 + len(raw_value) - len(raw_value.lstrip()),
                        colon + 1 + len(raw_value.rstrip()),
                    ),
                    line,
                )
            )
        else:
            flush()
            if text.strip():
                blocks.append(R.paragraph(line))
    flush()
    return blocks if found else None
