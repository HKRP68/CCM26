"""Preserve compact formatting in synchronous website/news notifications."""


def post_message(token, payload, *, timeout=12, post=None):
    """Send once, preserving text, entities, controls and privacy parameters.

    Explicit rich payloads opt in; ordinary notifications use sendMessage.
    Never repeat a possibly delivered message after a network or server error.
    """
    if post is None:
        import requests
        post = requests.post
    method = "sendRichMessage" if "rich_message" in payload else "sendMessage"
    return post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=timeout)
