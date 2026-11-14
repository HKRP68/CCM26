"""Expose native rich text to handlers running on an older Telegram SDK.

Keep the real rich_message intact in api_kwargs. Supply text/entities only
when Telegram did not supply them, so callback/reply handlers can still read
message.text_html, mentions and score lines after restart or via webhooks.
"""

from functools import wraps


class LegacyText:
    def __init__(self):
        self.parts = []
        self.entities = []
        self.length = 0

    def add(self, text):
        self.parts.append(text)
        self.length += len(text.encode("utf-16-le")) // 2

    def inline(self, value):
        if value is None:
            return
        if isinstance(value, list):
            for item in value:
                self.inline(item)
            return
        if not isinstance(value, dict):
            self.add(str(value))
            return
        kind = value.get("type")
        start = self.length
        if kind == "button":
            self.inline(value.get("button", {}).get("text", ""))
            return
        self.inline(value.get("text", ""))
        entity_type = {
            "url": "text_link",
            "email_address": "email",
            "phone_number": "phone_number",
            "bank_card_number": "bank_card",
        }.get(kind, kind)
        supported = {
            "bold",
            "italic",
            "underline",
            "strikethrough",
            "spoiler",
            "code",
            "text_link",
            "text_mention",
            "mention",
            "hashtag",
            "cashtag",
            "bot_command",
            "email",
            "phone_number",
            "bank_card",
            "custom_emoji",
        }
        if entity_type not in supported or self.length == start:
            return
        entity = dict(type=entity_type, offset=start, length=self.length - start)
        for key in ("url", "user", "custom_emoji_id"):
            if key in value:
                entity[key] = value[key]
        self.entities.append(entity)

    def blocks(self, blocks):
        for index, block in enumerate(blocks):
            if index:
                self.add("\n")
            kind = block.get("type")
            start = self.length
            if kind == "table":
                if block.get("caption"):
                    self.inline(block["caption"])
                    self.add("\n")
                for row_index, row in enumerate(block.get("cells", [])):
                    if row_index:
                        self.add("\n")
                    for cell_index, cell in enumerate(row):
                        if cell_index:
                            self.add("\t")
                        self.inline(cell.get("text", ""))
            elif kind == "details":
                self.inline(block.get("summary", ""))
                self.add("\n")
                self.blocks(block.get("blocks", []))
            elif kind == "list":
                for number, item in enumerate(block.get("items", []), 1):
                    if number > 1:
                        self.add("\n")
                    if item.get("has_checkbox"):
                        self.add("☑ " if item.get("is_checked") else "☐ ")
                    elif "value" in item:
                        self.add(f"{item['value']}. ")
                    else:
                        self.add("• ")
                    self.blocks(item.get("blocks", []))
            elif "blocks" in block:
                self.blocks(block["blocks"])
            elif kind == "buttons":
                self.inline(
                    [button.get("text", "") for button in block.get("buttons", [])]
                )
            elif "text" in block:
                self.inline(block["text"])
            elif block.get("caption"):
                self.inline(block["caption"].get("text", ""))
            if block.get("credit"):
                self.add("\n")
                self.inline(block["credit"])
            entity_type = {
                "heading": "bold",
                "pre": "pre",
                "blockquote": "blockquote",
                "expandable_blockquote": "expandable_blockquote",
            }.get(kind)
            if entity_type and self.length > start:
                entity = dict(
                    type=entity_type, offset=start, length=self.length - start
                )
                if kind == "pre" and block.get("language"):
                    entity["language"] = block["language"]
                self.entities.append(entity)


def adapt_message(data):
    if (
        not isinstance(data, dict)
        or "text" in data
        or not isinstance(data.get("rich_message"), dict)
    ):
        return data
    rich = data["rich_message"]
    if not isinstance(rich.get("blocks"), list):
        return data
    text = LegacyText()
    text.blocks(rich["blocks"])
    return {
        **data,
        "text": "".join(text.parts),
        "entities": sorted(
            text.entities, key=lambda item: (item["offset"], -item["length"])
        ),
    }


def install_message_compat():
    from telegram import Message

    original = Message.de_json.__func__
    if getattr(original, "_cmu_rich_compat", False):
        return

    @wraps(original)
    def de_json(cls, data, bot=None):
        return original(cls, adapt_message(data), bot)

    de_json._cmu_rich_compat = True
    Message.de_json = classmethod(de_json)
