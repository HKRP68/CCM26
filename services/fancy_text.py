"""Unicode "fancy" text helpers for stylised chat messages.

These produce the small-caps player names, bold-digit stats and circled
serial numbers used by the Playing XI / claim / retain messages. They are
pure string transforms with no dependencies so they can be reused anywhere.
"""

# ── Small caps (a–z → ᴀ–ᴢ) ───────────────────────────────────────────
_SMALL_CAPS = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ғ", "g": "ɢ",
    "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ",
    "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ", "s": "s", "t": "ᴛ", "u": "ᴜ",
    "v": "ᴠ", "w": "ᴡ", "x": "x", "y": "ʏ", "z": "ᴢ",
}

# ── Mathematical bold digits (0–9 → 𝟎–𝟗) ─────────────────────────────
_BOLD_DIGIT_BASE = 0x1D7CE  # MATHEMATICAL BOLD DIGIT ZERO

# ── Mathematical bold letters for headers ────────────────────────────
_BOLD_UPPER_BASE = 0x1D400  # MATHEMATICAL BOLD CAPITAL A
_BOLD_LOWER_BASE = 0x1D41A  # MATHEMATICAL BOLD SMALL A

# ── Circled numbers (1–20 → ①–⑳) ─────────────────────────────────────
_CIRCLED_BASE = 0x2460  # CIRCLED DIGIT ONE


def small_caps(text: str) -> str:
    """Convert ASCII letters to their small-caps equivalents.

    Non-letters (spaces, punctuation, digits, accented chars) pass through
    unchanged so multi-word names stay readable.
    """
    if not text:
        return ""
    return "".join(_SMALL_CAPS.get(ch.lower(), ch) for ch in text)


def bold_digits(value) -> str:
    """Convert the digits in ``value`` to mathematical bold digits."""
    out = []
    for ch in str(value):
        if "0" <= ch <= "9":
            out.append(chr(_BOLD_DIGIT_BASE + (ord(ch) - ord("0"))))
        else:
            out.append(ch)
    return "".join(out)


def bold_serif(text) -> str:
    """Convert ASCII letters/digits to mathematical bold (serif) glyphs."""
    out = []
    for ch in str(text):
        if "A" <= ch <= "Z":
            out.append(chr(_BOLD_UPPER_BASE + (ord(ch) - ord("A"))))
        elif "a" <= ch <= "z":
            out.append(chr(_BOLD_LOWER_BASE + (ord(ch) - ord("a"))))
        elif "0" <= ch <= "9":
            out.append(chr(_BOLD_DIGIT_BASE + (ord(ch) - ord("0"))))
        else:
            out.append(ch)
    return "".join(out)


def circled(n: int) -> str:
    """Return the circled form of ``n`` (1–20 → ①–⑳).

    Falls back to ``"<n>."`` outside that range.
    """
    if 1 <= n <= 20:
        return chr(_CIRCLED_BASE + (n - 1))
    return f"{n}."


def compact_value(n) -> str:
    """Format a coin value compactly: 1_810_000 → '1.81M', 27_000 → '27K'.

    Trailing zeros after the decimal are trimmed (1_500_000 → '1.5M',
    2_000_000 → '2M').
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)

    def _trim(x: float) -> str:
        s = f"{x:.2f}".rstrip("0").rstrip(".")
        return s

    if abs(n) >= 1_000_000:
        return f"{_trim(n / 1_000_000)}M"
    if abs(n) >= 1_000:
        return f"{_trim(n / 1_000)}K"
    return str(n)
