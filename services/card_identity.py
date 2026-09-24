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
0. **the event's own team** — a ``TournamentTeam`` or ``ChallengeTeam`` row,
   when the caller is in a tournament or a league and says so. See below.
1. **user id** — ``users.team_logo_asset_key`` / ``users.team_colour``
2. **ChallengeTeam** — its ``logo_url`` / ``primary_color``. CIPL sides are
   franchises, not user teams, and both columns already existed unused.
3. **team name** — the ``users`` lookup keyed on the name

A user id beats a name wherever one is in scope, because names are not reliable
keys: ``/sim`` passes ``"🤖 Sim XI"`` for a bot side and can pass ``@username``
or ``"Someone's XI"``, none of which match a ``users.team_name``.

The event crest wins over the manager's own
───────────────────────────────────────────
In a tournament or a league nobody is playing *their* team — they are playing a
franchise the admins entered, with the franchise's name in the table and the
franchise's crest on the poster. Drawing the manager's personal crest on that
card mislabels the side, and it was what every /cipl card did: ``user_id`` was
tried first and a CIPL side always has one.

So whenever a caller passes **event context** — ``tournament_id``,
``tournament_team_id``, ``challenge_team_id``, ``challenge_team`` or
``league_key`` — the event's crest and colour are resolved first and win. With
no event context the order above is unchanged, so a plain /playmatch still
draws the manager's own crest.

The context is an id wherever one is in scope, because a name is not a key: two
tournaments can both have a "Super Kings" and a Lets Play side is a *user*
playing under a team label. ``tournament_tteam_by_user`` (Lets Play) and
``tournament_team_by_user`` (Challenge League) already carry those ids through
the match state, so the callers have them without a lookup.
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

def _clean(name):
    return re.sub(r"\s+", " ", str(name or "")).strip()


def _league_record(session, league_key):
    """The ChallengeLeague a ``league_key`` names, or ``None``.

    The resolver lives in ``handlers.challenge`` because it is the one that
    knows about short codes, commands and duplicate leagues. Imported lazily and
    behind a guard: a crest is never worth an import cycle at boot, and a key
    that will not resolve just means the name lookup is not narrowed.
    """
    if not league_key or session is None:
        return None
    try:
        from handlers.challenge import _get_challenge_league_record
        return _get_challenge_league_record(session, league_key)
    except Exception:
        logger.debug("league lookup failed for %r", league_key, exc_info=True)
        return None


def _challenge_team(session, team_name, league_key=None):
    """The ChallengeTeam row for a franchise name, or ``None``.

    Narrowed to one league when ``league_key`` resolves: franchise names repeat
    across leagues (every custom IPL clone has a "Super Kings"), and the bare
    name lookup would hand back whichever was entered last.
    """
    name = _clean(team_name)
    if not name or session is None:
        return None
    try:
        from sqlalchemy import func
        from models import ChallengeTeam
        query = session.query(ChallengeTeam).filter(
            func.lower(func.trim(ChallengeTeam.name)) == name.lower())
        league = _league_record(session, league_key)
        if league is not None:
            narrowed = query.filter(ChallengeTeam.league_id == league.id)
            found = narrowed.order_by(ChallengeTeam.id.desc()).first()
            if found is not None:
                return found
        return query.order_by(ChallengeTeam.id.desc()).first()
    except Exception:
        logger.warning("challenge team lookup failed for %r", team_name, exc_info=True)
        return None


def _challenge_team_by_id(session, challenge_team_id):
    if not challenge_team_id or session is None:
        return None
    try:
        from models import ChallengeTeam
        return session.get(ChallengeTeam, int(challenge_team_id))
    except Exception:
        logger.warning("challenge team %r lookup failed", challenge_team_id,
                       exc_info=True)
        return None


def _tournament_team(session, *, tournament_id=None, tournament_team_id=None,
                     team_name=None, user_tg_id=None):
    """The ``TournamentTeam`` row a card is being drawn for, or ``None``.

    Keyed on an id wherever the caller has one. The fallbacks matter for the
    modes that do not carry the mapping: a Lets Play side *is* a Telegram user
    (``user_tg_id``), and everything else is matched on the team label the
    tournament itself stores.
    """
    if session is None:
        return None
    try:
        from models import TournamentTeam
        if tournament_team_id:
            row = session.get(TournamentTeam, int(tournament_team_id))
            if row is not None:
                return row
        if not tournament_id:
            return None
        query = (session.query(TournamentTeam)
                 .filter(TournamentTeam.tournament_id == int(tournament_id)))
        if user_tg_id:
            row = query.filter(TournamentTeam.user_tg_id == int(user_tg_id)).first()
            if row is not None:
                return row
        name = _clean(team_name)
        if name:
            from sqlalchemy import func
            return (query.filter(func.lower(func.trim(TournamentTeam.name))
                                 == name.lower())
                    .order_by(TournamentTeam.id.desc()).first())
    except Exception:
        logger.warning("tournament team lookup failed (tournament=%r name=%r)",
                       tournament_id, team_name, exc_info=True)
    return None


def _event_teams(session, *, team_name=None, user_id=None, league_key=None,
                 challenge_team=None, challenge_team_id=None,
                 tournament_id=None, tournament_team_id=None):
    """The event rows a card should be branded from, most specific first.

    Returns a list of ``TournamentTeam``/``ChallengeTeam`` rows — a list because
    a tournament side usually *is* a franchise and inherits nothing but its
    name: a Challenge League tournament copies ``logo_url`` onto its
    ``TournamentTeam`` at entry, but a row entered before the copy existed, or
    one an admin cleared, still has the franchise behind it via
    ``challenge_team_id``. Falling through to it is the difference between the
    league's crest and no crest at all.

    Empty when the caller passed no event context. That is the signal the
    manager's own crest still wins, so a plain /playmatch is untouched.
    """
    if session is None:
        return []
    if not any((tournament_id, tournament_team_id, challenge_team_id,
                challenge_team, league_key)):
        return []

    rows = []
    tteam = None
    if tournament_id or tournament_team_id:
        user = _user(session, user_id)
        tteam = _tournament_team(
            session, tournament_id=tournament_id,
            tournament_team_id=tournament_team_id, team_name=team_name,
            user_tg_id=getattr(user, "telegram_id", None))
    if tteam is not None:
        rows.append(tteam)

    cteam = challenge_team or _challenge_team_by_id(session, challenge_team_id)
    if cteam is None and tteam is not None:
        cteam = _challenge_team_by_id(session, getattr(tteam, "challenge_team_id", None))
    if cteam is None and league_key:
        cteam = _challenge_team(session, team_name, league_key)
    if cteam is not None:
        rows.append(cteam)
    return rows


def _event_logo_bytes(logo_url):
    """Bytes behind a ``logo_url`` an admin uploaded on the website.

    The admin panel stores a Flask static URL (``/static/challenge_leagues/x.png``),
    so this maps it back to disk and heals from the durable store on a miss —
    the host filesystem is rebuilt on every deploy, which used to leave every
    league playing with no crest until someone re-uploaded one by hand.
    ``asset_store.ensure`` refills it from the database, and from the Telegram
    storage channel behind that.
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
        logger.warning("event team logo unreadable at %s", path, exc_info=True)
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


def event_logo_png(session, **context):
    """The crest of the tournament/league side a card is for, or ``None``.

    Split out so a caller can ask the question on its own — the website's
    league pages and the tests both want "what crest does this franchise
    play under" without a user in scope.
    """
    for row in _event_teams(session, **context):
        found = _event_logo_bytes(getattr(row, "logo_url", None))
        if found:
            return found
    return None


def event_colour(session, **context):
    """The tournament/league side's own ``#rrggbb``, or ``None``."""
    for row in _event_teams(session, **context):
        found = normalise_hex(getattr(row, "primary_color", None))
        if found:
            return found
    return None


def team_logo_png(session, *, user_id=None, team_name=None, challenge_team=None,
                  league_key=None, tournament_id=None, tournament_team_id=None,
                  challenge_team_id=None):
    """A team's crest as PNG bytes, or ``None``.

    An event crest wins over the manager's own — see the module docstring. With
    no event context in the call this is the order it always had.
    """
    try:
        found = event_logo_png(
            session, team_name=team_name, user_id=user_id, league_key=league_key,
            challenge_team=challenge_team, challenge_team_id=challenge_team_id,
            tournament_id=tournament_id, tournament_team_id=tournament_team_id)
        if found:
            return found

        user = _user(session, user_id)
        if user is not None and user.team_logo_asset_key:
            from services.team_logo_service import logo_bytes_for_key
            found = logo_bytes_for_key(user.team_logo_asset_key)
            if found:
                return found

        row = challenge_team or _challenge_team(session, team_name, league_key)
        if row is not None and getattr(row, "logo_url", None):
            found = _event_logo_bytes(row.logo_url)
            if found:
                return found

        if team_name:
            from services.team_logo_service import logo_png_for_team_name
            return logo_png_for_team_name(session, team_name)
    except Exception:
        logger.exception("team logo lookup failed (user=%s name=%r)", user_id, team_name)
    return None


def team_colour(session, *, user_id=None, team_name=None, challenge_team=None,
                league_key=None, tournament_id=None, tournament_team_id=None,
                challenge_team_id=None):
    """A team's own ``#rrggbb``, or ``None`` to use the admin default.

    Same precedence as the crest: the colour and the crest have to name the
    same side, or the card says one thing in its bar and another on its badge.
    """
    try:
        found = event_colour(
            session, team_name=team_name, user_id=user_id, league_key=league_key,
            challenge_team=challenge_team, challenge_team_id=challenge_team_id,
            tournament_id=tournament_id, tournament_team_id=tournament_team_id)
        if found:
            return found

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
                    tournament_id=None, inn1_team_id=None, inn2_team_id=None,
                    inn1_challenge_team_id=None, inn2_challenge_team_id=None,
                    potm_card=False, include_style=True):
    """Every branded argument the summary card takes.

    Spread this into ``generate_match_summary(**payload, **visuals)`` and the
    card gets both crests, both colours, the portrait and — unless
    ``include_style`` is off — the admin's text settings and flourish switch.

    ``inn1_team_id``/``inn2_team_id`` are ``TournamentTeam`` ids and
    ``inn1_challenge_team_id``/``inn2_challenge_team_id`` are ``ChallengeTeam``
    ids: pass whichever the mode has, and the event's crest is what the card
    wears. ``potm_card`` adds the award winner's collectible card to the strip.
    """
    default1, default2, text_settings, dynamic = _admin_colours()

    event1 = {"league_key": league_key, "tournament_id": tournament_id,
              "tournament_team_id": inn1_team_id,
              "challenge_team_id": inn1_challenge_team_id}
    event2 = {"league_key": league_key, "tournament_id": tournament_id,
              "tournament_team_id": inn2_team_id,
              "challenge_team_id": inn2_challenge_team_id}

    colour1 = team_colour(session, user_id=inn1_user_id, team_name=inn1_team,
                          **event1) or default1
    colour2 = team_colour(session, user_id=inn2_user_id, team_name=inn2_team,
                          **event2) or default2

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
                                       team_name=inn1_team, **event1),
        "inn2_logo_png": team_logo_png(session, user_id=inn2_user_id,
                                       team_name=inn2_team, **event2),
        "potm_photo_png": potm_portrait_png(session, player_id=potm_player_id,
                                            name=potm_name),
    }
    if potm_card:
        visuals["potm_card_png"] = potm_card_png(session, player_id=potm_player_id,
                                                 name=potm_name)
    if include_style:
        visuals["text_settings"] = text_settings
        visuals["dynamic_flourish"] = dynamic
    return visuals


def innings_visuals(session, *, team_name=None, user_id=None,
                    is_first_innings=True, league_key=None,
                    tournament_id=None, tournament_team_id=None,
                    challenge_team_id=None, include_style=True):
    """The branded arguments the batting and bowling cards take."""
    default1, default2, text_settings, _dynamic = _admin_colours()
    event = {"league_key": league_key, "tournament_id": tournament_id,
             "tournament_team_id": tournament_team_id,
             "challenge_team_id": challenge_team_id}
    accent = team_colour(session, user_id=user_id, team_name=team_name, **event)
    visuals = {
        "accent_hex": accent or (default1 if is_first_innings else default2),
        "team_logo_png": team_logo_png(session, user_id=user_id,
                                       team_name=team_name, **event),
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
