"""Who a team is, and what they look like on a scorecard.

Every card the bot draws wants the same three things: each side's crest, each
side's colour, and the Player of the Match's portrait. Those used to be
resolved inside :mod:`services.scorecard_delivery`, which meant any mode that
imported a generator and called it *directly* — ``/cipl``, the Super Over,
``/sim`` — silently got none of them. Half the bot's matches produced cards
with no crest, no colour and no photo, and nothing said so.

So the resolution lives here instead, in one module every caller can reach
without pulling in the delivery machinery. A mode that wants branded cards
calls :func:`summary_visuals` or :func:`innings_visuals` and spreads the result
into its generator call.

**Nothing here raises.** A card with no crest is a smaller loss than no card,
so every lookup swallows its own failure and returns ``None``.

Identity, first hit wins
────────────────────────
1. **user id** — ``users.team_logo_asset_key`` / ``users.team_colour``
2. **ChallengeTeam** — its ``logo_url`` / ``primary_color``. CIPL sides are
   franchises, not user teams, and both columns already existed unused.
3. **team name** — the ``users`` lookup keyed on the name

A user id beats a name wherever one is in scope, because names are not reliable
keys: ``/sim`` passes ``"🤖 Sim XI"`` for a bot side and can pass ``@username``
or ``"Someone's XI"``, none of which match a ``users.team_name``.
"""

import logging
import math
import os
import re

logger = logging.getLogger(__name__)

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# How far apart two team colours have to read before the card stops looking
# like a rendering fault. Measured with the weighted "redmean" distance below,
# whose range runs to about 765.
COLOUR_MIN_DISTANCE = 110
# How hard the second colour may be pushed while separating it. Past this it
# is no longer the colour anybody chose, so the admin default is honest.
COLOUR_MAX_SHIFT_STEPS = 6


# ══════════════════════════════════════════════════════════════════════
# Colour
# ══════════════════════════════════════════════════════════════════════

# Same shape the repo already validates against in card_template_service, but
# matched here directly: that module's ``normalise_color`` silently *falls back*
# on bad input, and a user who typed a bad colour has to be told rather than
# quietly handed black.
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def normalise_hex(value):
    """``#rrggbb`` for anything recognisable, else ``None``.

    Accepts a leading ``#`` or not, and three-digit shorthand, because people
    type colours both ways.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if not text.startswith("#"):
        text = f"#{text}"
    if not _HEX_RE.match(text):
        return None
    if len(text) == 4:                       # #abc → #aabbcc
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text.lower()


def hex_to_rgb(value, default=None):
    text = normalise_hex(value)
    if not text:
        return default
    return tuple(int(text[i:i + 2], 16) for i in (1, 3, 5))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def _distance(a, b):
    """Weighted RGB distance — the "redmean" approximation.

    Close enough to a perceptual metric for "do these two blocks read apart?",
    and it needs no colour-science dependency.
    """
    rmean = (a[0] + b[0]) / 2.0
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return math.sqrt((2 + rmean / 256) * dr * dr
                     + 4 * dg * dg
                     + (2 + (255 - rmean) / 256) * db * db)


def _luminance(rgb):
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def separate_colours(first, second, fallback=None):
    """Push ``second`` away from ``first`` until the two read apart.

    Two teams can pick the same colour, and two identical innings blocks look
    like a bug rather than a coincidence. Innings 1 always keeps what it chose;
    innings 2 is darkened or lightened — whichever direction it has room to
    move — and only drops to ``fallback`` when it cannot be separated at all,
    which in practice means both sides picked something near-black.

    Returns the ``(r, g, b)`` innings 2 should use.
    """
    if first is None or second is None:
        return second
    if _distance(first, second) >= COLOUR_MIN_DISTANCE:
        return second

    # Move away from the *first* colour's luminance, so a dark clash lightens
    # and a light one darkens rather than both collapsing to the same grey.
    lighten = _luminance(first) < 128
    shifted = tuple(second)
    for _ in range(COLOUR_MAX_SHIFT_STEPS):
        if lighten:
            shifted = tuple(min(255, int(c + (255 - c) * 0.28) + 12) for c in shifted)
        else:
            shifted = tuple(max(0, int(c * 0.68) - 6) for c in shifted)
        if _distance(first, shifted) >= COLOUR_MIN_DISTANCE:
            return shifted

    if fallback is not None and _distance(first, fallback) >= COLOUR_MIN_DISTANCE:
        return fallback
    return shifted


# ══════════════════════════════════════════════════════════════════════
# Team identity
# ══════════════════════════════════════════════════════════════════════

def _challenge_team(session, team_name, league_key=None):
    """The ChallengeTeam row for a franchise name, or ``None``."""
    name = re.sub(r"\s+", " ", str(team_name or "")).strip()
    if not name or session is None:
        return None
    try:
        from sqlalchemy import func
        from models import ChallengeTeam
        query = session.query(ChallengeTeam).filter(
            func.lower(func.trim(ChallengeTeam.name)) == name.lower())
        return query.order_by(ChallengeTeam.id.desc()).first()
    except Exception:
        logger.warning("challenge team lookup failed for %r", team_name, exc_info=True)
        return None


def _challenge_logo_bytes(logo_url):
    """Bytes behind a ChallengeTeam.logo_url.

    The admin panel stores a Flask static URL (``/static/challenge_leagues/x.png``),
    so this maps it back to disk and heals from the durable store on a miss —
    the host filesystem is rebuilt on every deploy.
    """
    raw = str(logo_url or "").strip()
    if not raw:
        return None
    relative = raw.split("?", 1)[0].lstrip("/")
    if not relative.startswith("static/"):
        relative = os.path.join("static", "challenge_leagues", os.path.basename(relative))
    path = os.path.join(_ROOT_DIR, relative.replace("/", os.sep))
    try:
        if not os.path.isfile(path):
            from services.asset_store import ensure
            ensure(path)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                return handle.read()
    except Exception:
        logger.warning("challenge team logo unreadable at %s", path, exc_info=True)
    return None


def _user(session, user_id):
    if not user_id or session is None:
        return None
    try:
        from models import User
        return session.query(User).filter(User.id == user_id).first()
    except Exception:
        logger.warning("user lookup failed for %r", user_id, exc_info=True)
        return None


def team_logo_png(session, *, user_id=None, team_name=None, challenge_team=None,
                  league_key=None):
    """A team's crest as PNG bytes, or ``None``."""
    try:
        user = _user(session, user_id)
        if user is not None and user.team_logo_asset_key:
            from services.team_logo_service import logo_bytes_for_key
            found = logo_bytes_for_key(user.team_logo_asset_key)
            if found:
                return found

        row = challenge_team or _challenge_team(session, team_name, league_key)
        if row is not None and getattr(row, "logo_url", None):
            found = _challenge_logo_bytes(row.logo_url)
            if found:
                return found

        if team_name:
            from services.team_logo_service import logo_png_for_team_name
            return logo_png_for_team_name(session, team_name)
    except Exception:
        logger.exception("team logo lookup failed (user=%s name=%r)", user_id, team_name)
    return None


def team_colour(session, *, user_id=None, team_name=None, challenge_team=None,
                league_key=None):
    """A team's own ``#rrggbb``, or ``None`` to use the admin default."""
    try:
        user = _user(session, user_id)
        if user is not None and getattr(user, "team_colour", None):
            found = normalise_hex(user.team_colour)
            if found:
                return found

        row = challenge_team or _challenge_team(session, team_name, league_key)
        if row is not None and getattr(row, "primary_color", None):
            found = normalise_hex(row.primary_color)
            if found:
                return found

        if team_name and session is not None:
            from sqlalchemy import func
            from models import User
            owner = (session.query(User)
                     .filter(User.team_colour.isnot(None),
                             func.lower(func.trim(User.team_name))
                             == re.sub(r"\s+", " ", str(team_name)).strip().lower())
                     .order_by(User.id.desc()).first())
            if owner is not None:
                return normalise_hex(owner.team_colour)
    except Exception:
        logger.exception("team colour lookup failed (user=%s name=%r)", user_id, team_name)
    return None


# ══════════════════════════════════════════════════════════════════════
# Player of the Match portrait
# ══════════════════════════════════════════════════════════════════════

def potm_portrait_png(session, *, player_id=None, name=None):
    """The award winner's cut-out portrait as PNG bytes, or ``None``.

    Uses :mod:`services.player_portrait_service`, whose portraits are cut-outs
    with a transparent background *and* which falls back to the admin's global
    player PNG. That fallback is what makes a photo appear in every mode rather
    than only for the few players with art of their own.

    Not :func:`services.player_image_service.get_custom_image_bytes` — that
    returns the full collectible card, borders and all, which is what the strip
    was pasting in before.
    """
    if session is None or (not player_id and not name):
        return None
    try:
        from models import Player
        from services import player_portrait_service

        player = None
        if player_id:
            player = session.query(Player).filter(Player.id == player_id).first()
        if player is None and name:
            from sqlalchemy import func
            player = (session.query(Player)
                      .filter(func.lower(func.trim(Player.name))
                              == str(name).strip().lower())
                      .first())
        if player is None:
            # Still worth the global fallback: the strip looks better with a
            # silhouette than with a hole.
            portrait = player_portrait_service.get_global_player_portrait()
        else:
            portrait = player_portrait_service.get_player_portrait(player)
        if portrait is None:
            return None

        import io
        buf = io.BytesIO()
        portrait.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        logger.exception("POTM portrait lookup failed (player=%s name=%r)",
                         player_id, name)
        return None


def _potm_player(session, player_id=None, name=None):
    """The ``Player`` row behind an award, by id then by name, or ``None``."""
    if session is None or (not player_id and not name):
        return None
    from models import Player
    if player_id:
        player = session.query(Player).filter(Player.id == player_id).first()
        if player is not None:
            return player
    if name:
        from sqlalchemy import func
        return (session.query(Player)
                .filter(func.lower(func.trim(Player.name))
                        == str(name).strip().lower())
                .first())
    return None


def potm_card_png(session, *, player_id=None, name=None):
    """The Player of the Match's collectible card, as image bytes.

    This is the card people actually collect — ``card_generator.generate_card``
    dispatches admin custom art, then the website template, then the procedural
    tier card, and caches the result by player, so only the first card of a
    given player pays the render.

    It is sent as its own photo rather than composited into the summary card:
    the card is 1536×1024, and the summary's POTM strip is 125px tall, so
    fitting it in there would shrink it to about a tenth of linear scale with
    its own text a few pixels high. Unreadable is worse than absent.

    Returns ``None`` for anything that does not resolve — a missing card must
    never affect the summary card that has already gone out.
    """
    try:
        player = _potm_player(session, player_id, name)
        if player is None:
            return None
        from services.card_generator import generate_card
        return generate_card(player)
    except Exception:
        logger.exception("POTM card render failed (player=%s name=%r)",
                         player_id, name)
        return None


# ══════════════════════════════════════════════════════════════════════
# What the callers use
# ══════════════════════════════════════════════════════════════════════

def _admin_colours():
    try:
        from services.config_service import get_config
        cfg = get_config()
        return (cfg.get("scorecard_color_inn1"), cfg.get("scorecard_color_inn2"),
                cfg.get("scorecard_text_settings"),
                bool(cfg.get("scorecard_dynamic_flourish")))
    except Exception:
        logger.exception("scorecard config lookup failed — using defaults")
        return (None, None, None, False)


def summary_visuals(session, *, inn1_team=None, inn2_team=None,
                    inn1_user_id=None, inn2_user_id=None,
                    potm_player_id=None, potm_name=None, league_key=None,
                    include_style=True):
    """Every branded argument the summary card takes.

    Spread this into ``generate_match_summary(**payload, **visuals)`` and the
    card gets both crests, both colours, the portrait and — unless
    ``include_style`` is off — the admin's text settings and flourish switch.
    """
    default1, default2, text_settings, dynamic = _admin_colours()

    colour1 = team_colour(session, user_id=inn1_user_id, team_name=inn1_team,
                          league_key=league_key) or default1
    colour2 = team_colour(session, user_id=inn2_user_id, team_name=inn2_team,
                          league_key=league_key) or default2

    # Only separate when the two sides actually chose colours that clash;
    # the admin defaults are already distinct by design.
    rgb1, rgb2 = hex_to_rgb(colour1), hex_to_rgb(colour2)
    if rgb1 and rgb2:
        moved = separate_colours(rgb1, rgb2, fallback=hex_to_rgb(default2))
        if moved and tuple(moved) != tuple(rgb2):
            logger.info("team colours %s/%s were too close; innings 2 shifted to %s",
                        colour1, colour2, rgb_to_hex(moved))
            colour2 = rgb_to_hex(moved)

    visuals = {
        "inn1_color": colour1,
        "inn2_color": colour2,
        "inn1_logo_png": team_logo_png(session, user_id=inn1_user_id,
                                       team_name=inn1_team, league_key=league_key),
        "inn2_logo_png": team_logo_png(session, user_id=inn2_user_id,
                                       team_name=inn2_team, league_key=league_key),
        "potm_photo_png": potm_portrait_png(session, player_id=potm_player_id,
                                            name=potm_name),
    }
    if include_style:
        visuals["text_settings"] = text_settings
        visuals["dynamic_flourish"] = dynamic
    return visuals


def innings_visuals(session, *, team_name=None, user_id=None,
                    is_first_innings=True, league_key=None,
                    include_style=True):
    """The branded arguments the batting and bowling cards take."""
    default1, default2, text_settings, _dynamic = _admin_colours()
    accent = team_colour(session, user_id=user_id, team_name=team_name,
                         league_key=league_key)
    visuals = {
        "accent_hex": accent or (default1 if is_first_innings else default2),
        "team_logo_png": team_logo_png(session, user_id=user_id,
                                       team_name=team_name, league_key=league_key),
    }
    if include_style:
        visuals["text_settings"] = text_settings
    return visuals


def open_session():
    """A session for callers that do not already hold one.

    Returned so the caller can close it; every function above accepts ``None``
    and degrades rather than opening one of its own, because these run inside
    handlers that already hold a session and a second pooled connection is the
    exhaustion trap ``services/player_image_service`` documents.
    """
    from database import get_session
    return get_session()
