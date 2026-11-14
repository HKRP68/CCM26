"""Bot API 10.4 rich HTML for the same safe, structured message blocks."""

from html import escape


def inline(value):
    if isinstance(value, list):
        return "".join(inline(v) for v in value)
    if not isinstance(value, dict):
        return escape(str(value if value is not None else ""))
    kind = value.get("type")
    text = inline(value.get("text", ""))
    tag = {
        "bold": "b",
        "italic": "i",
        "underline": "u",
        "strikethrough": "s",
        "spoiler": "tg-spoiler",
        "code": "code",
        "marked": "mark",
        "subscript": "sub",
        "superscript": "sup",
    }.get(kind)
    if tag:
        return f"<{tag}>{text}</{tag}>"
    if kind == "url":
        return f'<a href="{escape(str(value["url"]),quote=True)}">{text}</a>'
    if kind == "button":
        return button(value["button"])
    if kind == "date_time":
        return f'<tg-time unix="{int(value["unix_time"])}" format="{escape(value["date_time_format"],quote=True)}">{text}</tg-time>'
    if kind == "reference":
        return f'<tg-reference name="{escape(value["name"],quote=True)}">{text}</tg-reference>'
    if kind in ("reference_link", "anchor_link"):
        name = value["reference_name" if kind == "reference_link" else "anchor_name"]
        return f'<a href="#{escape(name,quote=True)}">{text}</a>'
    if kind == "anchor":
        return f'<a name="{escape(value["name"],quote=True)}"></a>'
    raise ValueError(f"Unsupported rich HTML inline type: {kind}")


def button(value):
    style = escape(value.get("style", "primary"), quote=True)
    text = inline(value.get("text", ""))
    if "callback_data" in value:
        return f'<tg-button type="callback_data" style="{style}" data="{escape(value["callback_data"],quote=True)}">{text}</tg-button>'
    if "url" in value:
        return f'<tg-button type="url" style="{style}" url="{escape(value["url"],quote=True)}">{text}</tg-button>'
    raise ValueError("Unsupported rich HTML button action")


def render(blocks):
    parts = []
    for block in blocks:
        kind = block.get("type")
        if kind in (
            "paragraph",
            "heading",
            "footer",
            "pre",
            "pullquote",
            "expandable_blockquote",
        ):
            text = inline(block.get("text", ""))
            if kind == "heading":
                tag = "h" + str(max(1, min(6, int(block.get("size", 3)))))
            else:
                tag = {
                    "paragraph": "p",
                    "footer": "footer",
                    "pre": "pre",
                    "pullquote": "aside",
                    "expandable_blockquote": "blockquote",
                }[kind]
            attrs = " expandable" if kind == "expandable_blockquote" else ""
            if block.get("credit") is not None:
                text += "<cite>" + inline(block["credit"]) + "</cite>"
            if kind == "pre" and block.get("language"):
                text = (
                    '<code class="language-'
                    + escape(str(block["language"]), quote=True)
                    + '">'
                    + text
                    + "</code>"
                )
            parts.append(f"<{tag}{attrs}>{text}</{tag}>")
        elif kind == "divider":
            parts.append("<hr/>")
        elif kind == "details":
            attrs = " open" if block.get("is_open") else ""
            parts.append(
                f'<details{attrs}><summary>{inline(block["summary"])}</summary>{render(block["blocks"])}</details>'
            )
        elif kind == "blockquote":
            credit = (
                "<cite>" + inline(block["credit"]) + "</cite>"
                if block.get("credit") is not None
                else ""
            )
            parts.append(
                "<blockquote>" + render(block["blocks"]) + credit + "</blockquote>"
            )
        elif kind == "table":
            attrs = "".join(
                " " + short
                for key, short in [
                    ("is_bordered", "bordered"),
                    ("is_striped", "striped"),
                    ("is_compact", "compact"),
                ]
                if block.get(key)
            )
            rows = []
            for row in block["cells"]:
                cells = []
                for cell in row:
                    tag = "th" if cell.get("is_header") else "td"
                    cellattrs = "".join(
                        f' {key}="{escape(str(cell[key]),quote=True)}"'
                        for key in ("align", "valign", "colspan", "rowspan")
                        if key in cell
                    )
                    cells.append(
                        f'<{tag}{cellattrs}>{inline(cell.get("text",""))}</{tag}>'
                    )
                rows.append("<tr>" + "".join(cells) + "</tr>")
            caption = (
                "<caption>" + inline(block["caption"]) + "</caption>"
                if block.get("caption") is not None
                else ""
            )
            parts.append(f"<table{attrs}>{caption}" + "".join(rows) + "</table>")
        elif kind == "list":
            ordered = any(
                "value" in item or item.get("type") in ("1", "a", "A", "i", "I")
                for item in block["items"]
            )
            tag = "ol" if ordered else "ul"
            items = []
            for item in block["items"]:
                attrs = "".join(
                    f' {key}="{escape(str(item[key]),quote=True)}"'
                    for key in ("value", "type")
                    if key in item
                )
                tick = (
                    (
                        '<input type="checkbox"'
                        + (" checked" if item.get("is_checked") else "")
                        + "/>"
                    )
                    if item.get("has_checkbox")
                    else ""
                )
                items.append(f"<li{attrs}>" + tick + render(item["blocks"]) + "</li>")
            parts.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
        elif kind == "buttons":
            parts.append(
                '<tg-button-row align="'
                + escape(block.get("align", "center"), quote=True)
                + '">'
                + "".join(button(b) for b in block["buttons"])
                + "</tg-button-row>"
            )
        else:
            raise ValueError(f"Unsupported rich HTML block type: {kind}")
    return "".join(parts)
