"""Auction League (``/auctionleague``) — a solo, RCPL-style career.

One player takes a franchise in a Challenge League (IPL by default), the
other franchises are run by the AI, and the season goes:

  1. **Retention** — up to three of your own players, at 18 / 14 / 10 Cr. The
     AI retains too (when switched on): its stars — players in the top 20% of
     the league by rating — are kept, best first, up to three.
  2. **Auction** — everyone not retained goes under the hammer in automatic
     sets: the marquee names first, then rating bands per role ("Batsmen
     90–87", "Bowlers 90–87", …), then an accelerated round for the unsold.
     You bid against the AI one tap at a time; ``/simset`` and ``/simtolast``
     hand your side to the same AI and play the rest out instantly.
  3. **Season** — a single round robin and the IPL playoffs (Qualifier 1,
     Eliminator, Qualifier 2, Final). Your matches are played ball by ball
     through the ordinary Challenge League engine; every AI-vs-AI match is
     simulated with ``services.sim_match``.

Everything here is a function of a plain JSON-able ``state`` dict plus a
seeded ``random.Random`` — no Telegram and no session — except the small
database section at the bottom, which loads the league and stores the save
(``models.AuctionLeagueSave``). The handler is ``handlers/auction_league.py``.

Money is in **lakh** throughout (100 lakh = 1 crore), like the multiplayer
auction in ``services/auction_service.py``.
"""

import json
import logging
import random

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# The rulebook
# ════════════════════════════════════════════════════════════════════

LAKH_PER_CRORE = 100
PURSE_LAKH = 12_000                      # ₹120 Cr
RETENTION_PRICES = (1_800, 1_400, 1_000)  # slot 1 / 2 / 3: ₹18 / 14 / 10 Cr
MAX_RETAIN = len(RETENTION_PRICES)
SQUAD_MAX = 18

ROLE_BAT = "Batsman"
ROLE_WK = "Wicket Keeper"
ROLE_AR = "All-rounder"
ROLE_BOWL = "Bowler"
ROLES = (ROLE_BAT, ROLE_AR, ROLE_WK, ROLE_BOWL)
ROLE_MIN = {ROLE_BAT: 4, ROLE_BOWL: 4, ROLE_WK: 1, ROLE_AR: 2}
SQUAD_MIN = sum(ROLE_MIN.values())       # 11 — enough for a Playing XI
ROLE_SHORT = {ROLE_BAT: "BAT", ROLE_WK: "WK", ROLE_AR: "AR", ROLE_BOWL: "BOWL"}
ROLE_PLURAL = {ROLE_BAT: "Batsmen", ROLE_WK: "Wicket-keepers",
               ROLE_AR: "All-rounders", ROLE_BOWL: "Bowlers"}

# The IPL's own squad rule: at most eight overseas players. Only applies when
# the league has a home country (otherwise nobody is overseas).
OVERSEAS_SQUAD_CAP = 8

MARQUEE_MIN_RATING = 91
BAND_WIDTH = 4
# Below this the bands stop and each role gets one "emerging" set.
BAND_FLOOR = 71

# AI retention: a player in the top 20% of the league by rating is a star
# worth keeping, and is kept this often.
AI_RETAIN_TOP_FRACTION = 0.20
AI_RETAIN_CHANCE = 0.85

# The IPL's record fee (₹27 Cr). No AI franchise ever values a player above it.
RECORD_PRICE = 2_700
# How hard the AI leans on its purse as the auction runs down. Real
# franchises are patient in the marquee sets and get urgent once the pool
# thins out — that late urgency is what runs purses down to a few crore,
# where an even share per slot leaves the last, uncontested places cheap
# and the money unspent. Stars stay spread out because the early sets are
# priced at about an even share.
SPEND_DRIVE_START = 1.0
SPEND_DRIVE_END = 3.4


def _auction_progress(state):
    """0 at the first lot, 1 when every listed player has been decided."""
    listed = sum(len(s["pids"]) for s in state.get("sets") or []
                 if not s.get("accelerated"))
    if not listed:
        return 0.0
    decided = sum(1 for e in state.get("sold_log") or [] if e["how"] != HOW_RETAINED)
    decided += len(state.get("unsold") or [])
    return max(0.0, min(1.0, decided / listed))


def _spend_drive(state):
    progress = _auction_progress(state)
    return SPEND_DRIVE_START + (SPEND_DRIVE_END - SPEND_DRIVE_START) * progress ** 1.0

BID_LIMIT = 500          # a bidding war this long is a bug, not an auction

PHASE_RETENTION = "retention"
PHASE_AUCTION = "auction"
PHASE_SEASON = "season"
PHASE_COMPLETED = "completed"
PHASE_ABANDONED = "abandoned"

HOW_RETAINED = "retained"
HOW_AUCTION = "auction"
HOW_RTM = "rtm"
HOW_AUTOFILL = "autofill"

OVERS = 20

STAGE_LEAGUE = "league"
STAGE_Q1 = "q1"
STAGE_ELIM = "elim"
STAGE_Q2 = "q2"
STAGE_FINAL = "final"
STAGE_LABEL = {STAGE_LEAGUE: "League", STAGE_Q1: "Qualifier 1",
               STAGE_ELIM: "Eliminator", STAGE_Q2: "Qualifier 2",
               STAGE_FINAL: "Final"}

# Season rewards — the only thing this mode pays. Every match is unranked
# practice (like /ciplbot), so a season cannot be farmed match by match.
REWARD_CHAMPION = (25_000, 15)
REWARD_RUNNER_UP = (10_000, 5)
REWARD_PLAYOFFS = (4_000, 0)
REWARD_COOLDOWN_HOURS = 48

# Each AI franchise draws one of these for the whole auction, so no two
# auctions play out the same. ``mult`` scales what it will pay, ``target`` is
# the squad size it aims for, ``star_bias`` how much it chases big names.
PERSONALITIES = {
    "aggressive": {"label": "Aggressive", "mult": 1.1, "target": 17, "star_bias": 1.25},
    "balanced": {"label": "Balanced", "mult": 1.0, "target": 18, "star_bias": 1.0},
    "thrifty": {"label": "Thrifty", "mult": 0.92, "target": 18, "star_bias": 0.85},
    "moneyball": {"label": "Moneyball", "mult": 0.97, "target": 18, "star_bias": 0.75},
}


class AuctionLeagueError(Exception):
    """A user action that the rules refuse — the message is shown to them."""


# ════════════════════════════════════════════════════════════════════
# Money
# ════════════════════════════════════════════════════════════════════

def money(lakh):
    """``₹1.5 Cr`` / ``₹75 L`` — the auction room's own way of saying it."""
    if lakh is None:
        return "—"
    lakh = int(lakh)
    sign = "-" if lakh < 0 else ""
    lakh = abs(lakh)
    if lakh < LAKH_PER_CRORE:
        return f"{sign}₹{lakh} L"
    whole, part = divmod(lakh, LAKH_PER_CRORE)
    if part == 0:
        return f"{sign}₹{whole} Cr"
    return f"{sign}₹{whole}.{f'{part:02d}'.rstrip('0')} Cr"


def base_price(rating):
    """Base price for a player of this OVR."""
    r = int(rating or 0)
    if r >= 90:
        return 200
    if r >= 87:
        return 150
    if r >= 84:
        return 100
    if r >= 80:
        return 75
    if r >= 75:
        return 50
    return 30


MIN_BASE = 30


def increment(price):
    """The IPL bid ladder: +5L under ₹1 Cr, +10L to ₹2 Cr, +20L to ₹5 Cr, then +25L."""
    p = int(price or 0)
    if p < 100:
        return 5
    if p < 200:
        return 10
    if p < 500:
        return 20
    return 25


# ════════════════════════════════════════════════════════════════════
# Cards
# ════════════════════════════════════════════════════════════════════

def normalise_role(value):
    low = str(value or "").strip().lower().replace("-", " ").replace("_", " ")
    if low in ("wk", "keeper", "wicketkeeper", "wicket keeper",
               "wicket keeper batter", "wicket keeper batsman", "wk batsman",
               "wk batter"):
        return ROLE_WK
    if low in ("all rounder", "allrounder", "all round", "alr", "ar"):
        return ROLE_AR
    if low in ("bowler", "bowl"):
        return ROLE_BOWL
    return ROLE_BAT


def make_card(pid, name, *, category, rating, bat_rating=0, bowl_rating=0,
              bat_hand="Right", bowl_hand="Right", bowl_style="", country="",
              is_overseas=False, source_player_id=None, team=None):
    """A pool card — everything the auction, the XI rules and the engine need."""
    return {
        "id": int(pid),
        "name": str(name or "Player"),
        "category": normalise_role(category),
        "rating": int(rating or 0),
        "bat_rating": int(bat_rating or 0),
        "bowl_rating": int(bowl_rating or 0),
        "bat_hand": bat_hand or "Right",
        "bowl_hand": bowl_hand or "Right",
        "bowl_style": bowl_style or "",
        "country": country or "",
        "is_overseas": bool(is_overseas),
        "source_player_id": source_player_id,
        "team": team,
    }


class LeagueCard:
    """A pool card wearing a ``ChallengePlayer``'s clothes.

    The twin of ``services.cdraft_service.DraftCard``: the Playing XI picker,
    ``services.xi_rules`` and ``services.cipl_match.cp_to_player_dict`` all read
    a squad member out of ``details_json``. ``id`` stays the league's
    ``ChallengePlayer.id`` (what the card was snapshotted from) and the master
    card rides along as ``source_player_id``, exactly as for a league squad.
    """

    __slots__ = ("id", "name", "country", "details_json", "is_overseas",
                 "source_player_id", "sort_order")

    def __init__(self, card):
        self.id = int(card["id"])
        self.name = str(card.get("name") or "Player")
        self.country = card.get("country") or ""
        self.source_player_id = card.get("source_player_id")
        self.sort_order = 0
        self.is_overseas = bool(card.get("is_overseas"))
        self.details_json = json.dumps({
            "name": self.name,
            "category": card.get("category") or ROLE_BAT,
            "rating": int(card.get("rating") or 0),
            "bat_rating": int(card.get("bat_rating") or 0),
            "bowl_rating": int(card.get("bowl_rating") or 0),
            "bat_hand": card.get("bat_hand") or "Right",
            "bowl_hand": card.get("bowl_hand") or "Right",
            "bowl_style": card.get("bowl_style") or "",
            "country": self.country,
            "source_player_id": self.source_player_id,
            "is_overseas": self.is_overseas,
        })

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<LeagueCard {self.id} {self.name}>"


def card(state, pid):
    return state["pool"][str(int(pid))]


def squad_card_dicts(state, team):
    """``team``'s squad as pool cards, best first."""
    pids = [e["pid"] for e in state["teams"][team]["squad"]]
    cards = [card(state, pid) for pid in pids]
    return sorted(cards, key=lambda c: (-c["rating"], c["name"]))


def squad_cards(draft, side):
    """A match draft's squad for ``side`` as ``LeagueCard`` shims.

    What ``handlers.challenge._query_team_players`` hands the XI picker for an
    Auction League fixture in place of a league team's ``ChallengePlayer`` rows.
    """
    cards = ((draft.get("inline_squads") or {}).get(side)) or []
    return [LeagueCard(c) for c in cards]


# ════════════════════════════════════════════════════════════════════
# New career
# ════════════════════════════════════════════════════════════════════

def new_state(league, teams, user_team, *, ai_retain=True, difficulty="normal",
              seed=None):
    """A fresh career, in the retention phase.

    ``league`` — ``{"id", "name", "key", "home_country", "overseas_min",
    "overseas_max", "ball_format"}``. ``teams`` — a list of
    ``{"name", "short", "players": [card, …]}`` in league order.
    """
    if seed is None:
        seed = random.randrange(1 << 30)
    names = [t["name"] for t in teams]
    if user_team not in names:
        raise AuctionLeagueError("That team is not in this league.")
    if len(names) < 4:
        raise AuctionLeagueError("A league needs at least four teams for an "
                                 "auction and playoffs.")
    rng = random.Random(seed)
    pool = {}
    state_teams = {}
    personalities = list(PERSONALITIES)
    for t in teams:
        for c in t["players"]:
            c = dict(c)
            c["team"] = t["name"]
            pool[str(c["id"])] = c
        state_teams[t["name"]] = {
            "name": t["name"],
            "short": (t.get("short") or _initials(t["name"])).upper()[:5],
            "is_user": t["name"] == user_team,
            "purse": PURSE_LAKH,
            "squad": [],
            "retained": [],
            "rtm": 0,
            "personality": ("user" if t["name"] == user_team
                            else rng.choice(personalities)),
        }
    has_home = bool(league.get("home_country"))
    return {
        "v": 1,
        "seed": int(seed),
        "rng_calls": 0,
        "phase": PHASE_RETENTION,
        "league": {
            "id": league.get("id"),
            "name": league.get("name") or "League",
            "key": league.get("key"),
            "home_country": league.get("home_country"),
            "overseas_min": int(league.get("overseas_min") or 0),
            "overseas_max": int(11 if league.get("overseas_max") is None
                                else league.get("overseas_max")),
            "ball_format": league.get("ball_format") or "T20",
        },
        "overseas_cap": OVERSEAS_SQUAD_CAP if has_home else None,
        "user_team": user_team,
        "ai_retain": bool(ai_retain),
        "difficulty": difficulty,
        "team_order": names,
        "teams": state_teams,
        "pool": pool,
        "sets": [],
        "set_idx": 0,
        "lot_idx": 0,
        "lot": None,
        "lot_seq": 0,
        "autopilot": False,
        "accelerated_done": False,
        "unsold": [],
        "sold_log": [],
        "fixtures": [],
        "table": {},
        "stats": {"bat": {}, "bowl": {}},
        "champion": None,
        "runner_up": None,
        "user_conceded": 0,
    }


def _initials(name):
    words = [w for w in str(name or "").replace("-", " ").split() if w]
    if len(words) > 1:
        return "".join(w[0] for w in words[:4])
    return (name or "TEAM")[:4]


def rng_for(state, salt=""):
    """A fresh generator, seeded from the save and a counter.

    The counter moves on every call, so two actions never draw the same
    numbers, and it is stored in the state, so a replayed save draws the same
    numbers it drew the first time.
    """
    state["rng_calls"] = int(state.get("rng_calls") or 0) + 1
    return random.Random(f"{state['seed']}:{state['rng_calls']}:{salt}")


def user_team(state):
    return state["teams"][state["user_team"]]


def team_label(state, name):
    t = state["teams"][name]
    return f"{t['name']} ({t['short']})"


# ════════════════════════════════════════════════════════════════════
# Squad rules
# ════════════════════════════════════════════════════════════════════

def role_counts(state, team):
    counts = {r: 0 for r in ROLES}
    for e in state["teams"][team]["squad"]:
        counts[card(state, e["pid"])["category"]] += 1
    return counts


def overseas_count(state, team):
    return sum(1 for e in state["teams"][team]["squad"]
               if card(state, e["pid"]).get("is_overseas"))


def required_domestic(state):
    """Domestic players a squad must hold so it can always field a legal XI."""
    if not state.get("overseas_cap"):
        return 0
    return max(0, 11 - int(state["league"].get("overseas_max", 11)))


def owed_roles(state, team, extra=None):
    """``{role: still owed}`` — with ``extra`` (a card) counted as signed."""
    counts = role_counts(state, team)
    if extra is not None:
        counts[extra["category"]] += 1
    return {r: max(0, ROLE_MIN[r] - counts[r]) for r in ROLES}


def owed_slots(state, team, extra=None):
    """Signings this squad still needs before it is legal (roles, domestic, 11)."""
    t = state["teams"][team]
    size = len(t["squad"]) + (1 if extra is not None else 0)
    roles = sum(owed_roles(state, team, extra).values())
    domestic = sum(1 for e in t["squad"] if not card(state, e["pid"]).get("is_overseas"))
    if extra is not None and not extra.get("is_overseas"):
        domestic += 1
    dom_owed = max(0, required_domestic(state) - domestic)
    return max(roles, dom_owed, SQUAD_MIN - size)


def squad_is_legal(state, team):
    t = state["teams"][team]
    return (len(t["squad"]) <= SQUAD_MAX and owed_slots(state, team) == 0
            and (not state.get("overseas_cap")
                 or overseas_count(state, team) <= state["overseas_cap"]))


def can_add(state, team, c):
    """Whether ``team`` may sign card ``c`` at all (ignoring money)."""
    t = state["teams"][team]
    size = len(t["squad"])
    if size >= SQUAD_MAX:
        return False
    if any(e["pid"] == c["id"] for e in t["squad"]):
        return False
    cap = state.get("overseas_cap")
    if cap and c.get("is_overseas") and overseas_count(state, team) >= cap:
        return False
    # After this signing, the free slots left must still cover what is owed.
    if SQUAD_MAX - (size + 1) < owed_slots(state, team, extra=c):
        return False
    return _leaves_enough_for_others(state, team, c)


def _domestic_owed(state, team):
    domestic = sum(1 for e in state["teams"][team]["squad"]
                   if not card(state, e["pid"]).get("is_overseas"))
    return max(0, required_domestic(state) - domestic)


def _leaves_enough_for_others(state, team, c):
    """Whether signing ``c`` still leaves every OTHER side able to finish legally.

    A side buying a role it no longer needs (depth) must leave enough players of
    that role in the pool for everyone still short of it — otherwise the last
    all-rounders can all end up as somebody's fifth, and a side that needs one
    finishes with an illegal squad. The same for domestic players when the
    league caps overseas. A role the buyer itself still owes is always fine:
    that signing is one of the players the count already reserves.
    """
    if state.get("phase") != PHASE_AUCTION:
        return True
    role = c["category"]
    checks = []
    if owed_roles(state, team).get(role, 0) == 0:
        checks.append(("role", role))
    if (state.get("overseas_cap") and not c.get("is_overseas")
            and _domestic_owed(state, team) == 0 and required_domestic(state)):
        checks.append(("domestic", None))
    if not checks:
        return True
    taken = taken_pids(state)
    spare = [x for x in state["pool"].values() if x["id"] not in taken]
    others = [n for n in state["team_order"] if n != team]
    for kind, value in checks:
        if kind == "role":
            supply = sum(1 for x in spare if x["category"] == value)
            owed = sum(owed_roles(state, n).get(value, 0) for n in others)
        else:
            supply = sum(1 for x in spare if not x.get("is_overseas"))
            owed = sum(_domestic_owed(state, n) for n in others)
        if supply - 1 < owed:
            return False
    return True


def max_bid(state, team, c):
    """The most ``team`` can pay for ``c`` and still afford to finish its squad."""
    if not can_add(state, team, c):
        return 0
    reserve = MIN_BASE * owed_slots(state, team, extra=c)
    return max(0, int(state["teams"][team]["purse"]) - reserve)


def needs_line(state, team):
    owed = owed_roles(state, team)
    parts = [f"{n} {ROLE_SHORT[r]}" for r, n in owed.items() if n]
    return ", ".join(parts) if parts else "none"


# ════════════════════════════════════════════════════════════════════
# Retention
# ════════════════════════════════════════════════════════════════════

def retention_cost(n):
    return sum(RETENTION_PRICES[:max(0, min(int(n), MAX_RETAIN))])


def original_squad(state, team):
    """Cards that belonged to ``team`` in the league, best first."""
    cards = [c for c in state["pool"].values() if c.get("team") == team]
    return sorted(cards, key=lambda c: (-c["rating"], c["name"]))


def _sign(state, team, pid, price, how):
    t = state["teams"][team]
    t["squad"].append({"pid": int(pid), "price": int(price), "how": how})
    t["purse"] = int(t["purse"]) - int(price)
    entry = {"pid": int(pid), "team": team, "price": int(price), "how": how}
    state["sold_log"].append(entry)
    return entry


def _ai_retention_picks(state, team, rng):
    ratings = sorted((c["rating"] for c in state["pool"].values()), reverse=True)
    if not ratings:
        return []
    cut = ratings[max(0, int(len(ratings) * AI_RETAIN_TOP_FRACTION) - 1)]
    picks = []
    for c in original_squad(state, team):
        if len(picks) >= MAX_RETAIN:
            break
        if c["rating"] < cut:
            break
        if rng.random() < AI_RETAIN_CHANCE:
            picks.append(c["id"])
    return picks


def apply_retentions(state, user_pids):
    """Lock in the user's retentions (and the AI's), open the auction.

    Returns ``{team: [pid, …]}`` of every retention made.
    """
    if state["phase"] != PHASE_RETENTION:
        raise AuctionLeagueError("Retentions are already locked in.")
    user_pids = [int(p) for p in (user_pids or [])]
    if len(user_pids) > MAX_RETAIN:
        raise AuctionLeagueError(f"You can retain at most {MAX_RETAIN} players.")
    own = {c["id"] for c in original_squad(state, state["user_team"])}
    if any(p not in own for p in user_pids) or len(set(user_pids)) != len(user_pids):
        raise AuctionLeagueError("You can only retain your own team's players.")
    rng = rng_for(state, "retain")
    made = {}
    for name in state["team_order"]:
        if name == state["user_team"]:
            pids = user_pids
        elif state.get("ai_retain"):
            pids = _ai_retention_picks(state, name, rng)
        else:
            pids = []
        for i, pid in enumerate(pids):
            _sign(state, name, pid, RETENTION_PRICES[i], HOW_RETAINED)
        state["teams"][name]["retained"] = list(pids)
        # Right To Match: a side that kept two or fewer gets one card for its
        # own former players.
        state["teams"][name]["rtm"] = 1 if len(pids) < MAX_RETAIN else 0
        made[name] = list(pids)
    state["sets"] = build_sets(state, rng)
    state["set_idx"] = 0
    state["lot_idx"] = 0
    state["phase"] = PHASE_AUCTION
    return made


# ════════════════════════════════════════════════════════════════════
# Auction sets
# ════════════════════════════════════════════════════════════════════

def taken_pids(state):
    return {e["pid"] for t in state["teams"].values() for e in t["squad"]}


def build_sets(state, rng):
    """The auction order: marquee, then rating bands per role, then emerging.

    Inside a set the order is shuffled, as at a real auction.
    """
    taken = taken_pids(state)
    cards = [c for c in state["pool"].values() if c["id"] not in taken]
    sets = []

    marquee = [c for c in cards if c["rating"] >= MARQUEE_MIN_RATING]
    if marquee:
        hi = max(c["rating"] for c in marquee)
        sets.append(_make_set(f"⭐ Marquee ({hi}–{MARQUEE_MIN_RATING})", marquee, rng))
    rest = [c for c in cards if c["rating"] < MARQUEE_MIN_RATING]

    top = MARQUEE_MIN_RATING - 1
    while top >= BAND_FLOOR:
        lo = max(BAND_FLOOR, top - BAND_WIDTH + 1)
        band = [c for c in rest if lo <= c["rating"] <= top]
        for role in ROLES:
            group = [c for c in band if c["category"] == role]
            if group:
                sets.append(_make_set(f"{ROLE_PLURAL[role]} {top}–{lo}", group, rng))
        top = lo - 1
    for role in ROLES:
        group = [c for c in rest if c["rating"] < BAND_FLOOR and c["category"] == role]
        if group:
            sets.append(_make_set(f"🌱 Emerging {ROLE_PLURAL[role]}", group, rng))
    return sets


def _make_set(name, cards, rng):
    pids = [c["id"] for c in cards]
    rng.shuffle(pids)
    return {"name": name, "pids": pids, "accelerated": False}


def current_set(state):
    sets = state.get("sets") or []
    idx = int(state.get("set_idx") or 0)
    return sets[idx] if idx < len(sets) else None


def sets_overview(state):
    """``[(n, name, size, status)]`` for /alsets."""
    out = []
    idx = int(state.get("set_idx") or 0)
    for n, s in enumerate(state.get("sets") or [], 1):
        if n - 1 < idx:
            status = "done"
        elif n - 1 == idx and state["phase"] == PHASE_AUCTION:
            status = "live"
        else:
            status = "queued"
        out.append((n, s["name"], len(s["pids"]), status))
    return out


# ════════════════════════════════════════════════════════════════════
# The AI franchise's brain
# ════════════════════════════════════════════════════════════════════

def _star(c):
    """0..1 — how much of a star this card is."""
    return max(0.0, min(1.0, (int(c["rating"]) - 72) / 24.0))


def _personality(state, team):
    key = state["teams"][team].get("personality")
    return PERSONALITIES.get(key) or PERSONALITIES["balanced"]


def _squad_median(state, team):
    ratings = sorted(card(state, e["pid"])["rating"] for e in state["teams"][team]["squad"])
    return ratings[len(ratings) // 2] if ratings else 0


def _spendable(state, team, c):
    """Money ``team`` can put into ``c`` and still afford every compulsory signing."""
    reserve = MIN_BASE * owed_slots(state, team, extra=c)
    return max(0, int(state["teams"][team]["purse"]) - reserve)


# Per-role counts the AI wants so it can field its XI shape (see XI_SHAPE).
XI_TARGET = {ROLE_BAT: 4, ROLE_WK: 1, ROLE_AR: 3, ROLE_BOWL: 3}

def ai_value(state, team, c, rng, *, accelerated=False):
    """The most this franchise would pay for ``c``, or 0 for "not interested".

    How a real franchise plans an auction: the money it has left, spread over
    the slots it still wants to fill, weighted by how good the player is. A
    marquee star is worth several slots' money, a squad filler a fraction of
    one. Because that per-slot figure is recomputed for every lot, a side that
    has been outbid on stars carries a fat purse into the later sets and
    spends it there — so purses run down to a few crore by the end, as they do
    at the IPL. Role needs, the franchise's personality and a little jitter
    shape it; the IPL record (``RECORD_PRICE``) caps it.
    """
    ceiling = max_bid(state, team, c)
    base = base_price(c["rating"])
    if ceiling < base:
        return 0
    pers = _personality(state, team)
    t = state["teams"][team]
    size = len(t["squad"])
    target = int(pers["target"])
    if size >= SQUAD_MAX:
        return 0
    star = _star(c)
    owed = owed_roles(state, team)
    role_owed = owed.get(c["category"], 0) > 0
    counts = role_counts(state, team)

    slots = max(1, target - size)
    per_slot = _spendable(state, team, c) / slots
    # Quality weight: ~3.5 slots' money for the very best, ~1 for a good
    # regular, ~0.3 for a filler.
    weight = 0.3 + 3.2 * (star ** 2) * pers["star_bias"]
    if slots <= 2:
        # The last places in the squad: spend what is left on them.
        weight = max(weight, 1.4)
    if size >= target:
        # Squad planned out — only a real upgrade tempts it.
        if star < 0.6 or rng.random() > 0.35:
            return 0
        weight *= 0.5
    shape_want = XI_TARGET[c["category"]]
    if role_owed:
        weight *= 1.2
    elif counts[c["category"]] < shape_want:
        # Not compulsory, but the XI the AI fields (4/1/3/3) needs him.
        weight *= 1.15
    elif counts[c["category"]] >= max(ROLE_MIN[c["category"]], shape_want) + 2 and star < 0.6:
        # A fourth or fifth of a role it already has is depth, not a priority.
        weight *= 0.55
    if pers is PERSONALITIES["moneyball"] and 0.3 <= star < 0.65:
        weight *= 1.25     # undervalued squad players are its whole plan

    value = per_slot * weight * _spend_drive(state) * pers["mult"] * rng.uniform(0.85, 1.15)
    value = min(value, RECORD_PRICE * rng.uniform(0.85, 1.0))
    value = min(int(value), ceiling)
    if (role_owed or size < SQUAD_MIN) and ceiling >= base:
        # A side that still needs bodies never lets one go for nothing.
        value = max(value, base)
    return value if value >= base else 0


# ════════════════════════════════════════════════════════════════════
# The lot on the block
# ════════════════════════════════════════════════════════════════════

def _ai_teams(state):
    """The franchises the AI is bidding for right now (yours, on autopilot)."""
    names = [n for n in state["team_order"] if n != state["user_team"]]
    if state.get("autopilot"):
        names.append(state["user_team"])
    return names


def next_price(lot):
    if lot.get("price") is None:
        return int(lot["base"])
    return int(lot["price"]) + increment(lot["price"])


def _advance_cursor(state):
    """Move to the next queued player; returns its pid or None at the end."""
    while True:
        s = current_set(state)
        if s is None:
            # Out of sets: one accelerated round for the unsold, then stop.
            if state.get("unsold") and not state.get("accelerated_done"):
                pool = sorted(state["unsold"],
                              key=lambda p: -card(state, p)["rating"])
                state["unsold"] = []
                state["accelerated_done"] = True
                state["sets"].append({"name": "⚡ Accelerated round",
                                      "pids": pool, "accelerated": True})
                continue
            return None
        if state["lot_idx"] < len(s["pids"]):
            pid = s["pids"][state["lot_idx"]]
            if pid in taken_pids(state):
                state["lot_idx"] += 1
                continue
            return pid
        state["set_idx"] += 1
        state["lot_idx"] = 0


def open_next_lot(state, rng=None):
    """Put the next player on the block and let the AI open the bidding.

    Returns the lot, or None when the auction has run out of players.
    """
    if state["phase"] != PHASE_AUCTION:
        return None
    if state.get("lot"):
        return state["lot"]
    pid = _advance_cursor(state)
    if pid is None:
        return None
    rng = rng or rng_for(state, f"lot{pid}")
    c = card(state, pid)
    s = current_set(state)
    accelerated = bool(s and s.get("accelerated"))
    state["lot_seq"] = int(state.get("lot_seq") or 0) + 1
    lot = {
        "seq": state["lot_seq"],
        "pid": pid,
        "set_name": s["name"] if s else "",
        "set_no": int(state["set_idx"]) + 1,
        "accelerated": accelerated,
        "base": base_price(c["rating"]),
        "price": None,
        "leader": None,
        "bids": 0,
        "trail": [],
        "user_out": False,
        "values": {},
        "status": "open",
    }
    # Each franchise decides once what this player is worth to it, so its
    # behaviour is consistent across every round of the same lot.
    for name in state["team_order"]:
        if name == state["user_team"] and not state.get("autopilot"):
            continue
        lot["values"][name] = ai_value(state, name, c, rng, accelerated=accelerated)
    state["lot"] = lot
    settle(state, rng)
    # The same dict whether it is still open or was decided at once (it is
    # then ``state['last_lot']``) — callers read ``lot['status']``.
    return lot


def _user_value(state, rng):
    """Autopilot: value the lot for the user's side when it is switched on mid-lot."""
    lot = state["lot"]
    if state["user_team"] not in lot["values"]:
        lot["values"][state["user_team"]] = ai_value(
            state, state["user_team"], card(state, lot["pid"]), rng,
            accelerated=lot.get("accelerated"))


def user_may_bid(state):
    """``(ok, price)`` — whether the user can raise to the next price."""
    lot = state.get("lot")
    if not lot or lot["status"] != "open" or lot.get("user_out"):
        return False, None
    if lot.get("leader") == state["user_team"]:
        return False, None
    price = next_price(lot)
    c = card(state, lot["pid"])
    return price <= max_bid(state, state["user_team"], c), price


def bid_by_bid(state):
    """True when you answer every single AI raise (the default).

    ``state['fast']`` (the ⚡ Fast toggle) lets the AI franchises settle among
    themselves first, so you are only asked once one of them is left standing.
    """
    return not state.get("fast") and not state.get("autopilot")


def settle(state, rng):
    """Run the AI's bidding until the user has to answer or the lot is decided.

    Bid by bid (the default) you get the next move after every AI raise you
    can afford to answer; in ⚡ Fast mode the AI bids among itself first.

    Returns ``"user"`` (your call: Bid or Pass), ``"rtm"`` (your Right To Match
    decision) or ``"done"`` (sold or unsold — see ``state['lot']``).
    """
    lot = state["lot"]
    c = card(state, lot["pid"])
    if state.get("autopilot"):
        _user_value(state, rng)
    ai = _ai_teams(state)
    one_at_a_time = bid_by_bid(state) and not lot.get("user_out")
    # No squad changes while a lot is being bid on, so each side's ceiling is
    # worked out once rather than at every step of the war.
    caps = {n: max_bid(state, n, c) for n in ai if lot["values"].get(n, 0) > 0}
    while lot["bids"] < BID_LIMIT:
        step = next_price(lot)
        contenders = [n for n in caps
                      if n != lot["leader"]
                      and lot["values"].get(n, 0) >= step
                      and step <= caps[n]]
        if not contenders:
            break
        weights = [max(1, lot["values"][n] - step + 1) for n in contenders]
        leader = rng.choices(contenders, weights=weights, k=1)[0]
        lot["leader"] = leader
        lot["price"] = step
        lot["bids"] += 1
        lot["trail"] = (lot["trail"] + [[leader, step]])[-6:]
        if one_at_a_time and user_may_bid(state)[0]:
            return "user"
    if not state.get("autopilot"):
        ok, _price = user_may_bid(state)
        if ok and lot["leader"] != state["user_team"]:
            return "user"
    return _hammer(state, rng)


def jump_amount(price):
    """How far a 🚀 Jump bid moves the price: ₹50 L, or ₹1 Cr from ₹5 Cr up."""
    return 100 if int(price or 0) >= 500 else 50


def user_jump_price(state):
    """The price a 🚀 Jump bid would offer now, or None when it can't be made."""
    ok, price = user_may_bid(state)
    if not ok:
        return None
    lot = state["lot"]
    jump = (lot["price"] or 0) + jump_amount(lot["price"] or lot["base"])
    jump = max(jump, price + 1)
    c = card(state, lot["pid"])
    return jump if jump <= max_bid(state, state["user_team"], c) else None


def user_bid(state, rng=None, to_price=None):
    """You raise — to the next price, or to ``to_price`` (a 🚀 Jump bid) — and
    the AI answers. Returns as ``settle``."""
    lot = state.get("lot")
    if not lot or lot["status"] != "open":
        raise AuctionLeagueError("There is no player on the block.")
    ok, price = user_may_bid(state)
    if not ok:
        raise AuctionLeagueError("You can't bid on this player — check your "
                                 "purse and squad needs (/alsquad).")
    if to_price is not None:
        to_price = int(to_price)
        if to_price < price:
            raise AuctionLeagueError("The price has moved — check the card and bid again.")
        if to_price > max_bid(state, state["user_team"], card(state, lot["pid"])):
            raise AuctionLeagueError("That's more than you can spend on him.")
        price = to_price
    lot["leader"] = state["user_team"]
    lot["price"] = price
    lot["bids"] += 1
    lot["trail"] = (lot["trail"] + [[state["user_team"], price]])[-6:]
    return settle(state, rng or rng_for(state, "bid"))


def user_pass(state, rng=None):
    """You drop out of this lot; the AI finishes it. Returns as ``settle``."""
    lot = state.get("lot")
    if not lot or lot["status"] != "open":
        raise AuctionLeagueError("There is no player on the block.")
    lot["user_out"] = True
    if lot.get("leader") == state["user_team"]:
        # Passing while holding the top bid just stands on it.
        return _hammer(state, rng or rng_for(state, "pass"))
    return settle(state, rng or rng_for(state, "pass"))


def _hammer(state, rng):
    """Decide the lot: sold to the leader, or unsold — then Right To Match."""
    lot = state["lot"]
    c = card(state, lot["pid"])
    winner = lot.get("leader")
    if winner is None:
        lot["status"] = "unsold"
        if not lot.get("accelerated"):
            state["unsold"].append(lot["pid"])
        return _close_lot(state)
    price = int(lot["price"])
    holder = c.get("team")
    if (holder and holder != winner and holder in state["teams"]
            and state["teams"][holder].get("rtm", 0) > 0
            and price <= max_bid(state, holder, c)):
        if holder == state["user_team"] and not state.get("autopilot"):
            lot["status"] = "rtm"
            lot["rtm_team"] = holder
            return "rtm"
        if holder != state["user_team"] or state.get("autopilot"):
            value = lot["values"].get(holder) or ai_value(state, holder, c, rng)
            if value * 1.1 >= price and _star(c) >= 0.45:
                return _sell(state, holder, price, HOW_RTM)
    return _sell(state, winner, price, HOW_AUCTION)


def user_rtm(state, use, rng=None):
    """Answer your Right To Match prompt."""
    lot = state.get("lot")
    if not lot or lot.get("status") != "rtm":
        raise AuctionLeagueError("There is no Right To Match decision waiting.")
    if use:
        c = card(state, lot["pid"])
        if lot["price"] > max_bid(state, state["user_team"], c):
            raise AuctionLeagueError("You can't afford to match that price.")
        return _sell(state, state["user_team"], int(lot["price"]), HOW_RTM)
    lot["status"] = "open"
    return _sell(state, lot["leader"], int(lot["price"]), HOW_AUCTION)


def _sell(state, team, price, how):
    lot = state["lot"]
    if how == HOW_RTM:
        state["teams"][team]["rtm"] -= 1
    _sign(state, team, lot["pid"], price, how)
    lot["status"] = "sold"
    lot["winner"] = team
    lot["price"] = price
    lot["how"] = how
    return _close_lot(state)


def _close_lot(state):
    state["last_lot"] = state["lot"]
    state["lot"] = None
    state["lot_idx"] += 1
    return "done"


def lot_result_line(state, lot):
    c = card(state, lot["pid"])
    who = f"<b>{_esc(c['name'])}</b> ({c['rating']} {ROLE_SHORT[c['category']]})"
    if lot["status"] == "sold":
        tag = " 🔁 RTM" if lot.get("how") == HOW_RTM else ""
        mine = " 🎉" if lot["winner"] == state["user_team"] else ""
        return (f"🔨 {who} → <b>{_esc(state['teams'][lot['winner']]['short'])}</b> "
                f"for {money(lot['price'])}{tag}{mine}")
    return f"❌ {who} — unsold"


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ════════════════════════════════════════════════════════════════════
# Simulating the auction: one set, or to the end
# ════════════════════════════════════════════════════════════════════

def _sim_lots(state, stop, max_lots=None):
    """Run lots on autopilot until ``stop()`` (or ``max_lots`` are decided).

    Returns the resolved lots.
    """
    rng = rng_for(state, "sim")
    resolved = []
    state["autopilot"] = True
    try:
        if state.get("lot"):
            lot = state["lot"]
            if lot["status"] == "rtm":
                # Autopilot takes the AI's view of your own RTM card.
                lot["status"] = "open"
            _user_value(state, rng)
            settle(state, rng)
            resolved.append(state["last_lot"])
        guard = 0
        while guard < 5000:
            guard += 1
            if max_lots is not None and len(resolved) >= max_lots:
                break
            # Step the cursor first (it never opens a lot), so the stop test
            # sees which set the NEXT player belongs to.
            if _advance_cursor(state) is None or stop():
                break
            lot = open_next_lot(state, rng)
            if lot is None:
                break
            # open_next_lot settles at once under autopilot.
            resolved.append(state["last_lot"])
    finally:
        state["autopilot"] = False
    return resolved


def simulate_lot(state):
    """/skipplayer — decide the player on the block (or the next one) at once.

    Your side bids on the same autopilot as the AI, exactly as in /simset.
    Returns the resolved lot, or None when nothing was left to sell.
    """
    if state["phase"] != PHASE_AUCTION:
        raise AuctionLeagueError("The auction isn't running.")
    lots = _sim_lots(state, lambda: False, max_lots=1)
    return lots[0] if lots else None


def simulate_set(state, upto=None):
    """Simulate the rest of the current set — or every set up to number ``upto``.

    Your side bids on the same autopilot as the AI. Returns the resolved lots.
    """
    if state["phase"] != PHASE_AUCTION:
        raise AuctionLeagueError("The auction isn't running.")
    start = int(state["set_idx"])
    last = start if upto is None else int(upto) - 1
    if last < start:
        raise AuctionLeagueError(f"Set {upto} is already done.")
    if last >= len(state["sets"]):
        raise AuctionLeagueError(f"There are only {len(state['sets'])} sets.")
    return _sim_lots(state, lambda: int(state["set_idx"]) > last)


def simulate_to_last(state):
    """Simulate every remaining lot, accelerated round included."""
    if state["phase"] != PHASE_AUCTION:
        raise AuctionLeagueError("The auction isn't running.")
    return _sim_lots(state, lambda: False)


def auction_finished(state):
    return (state["phase"] == PHASE_AUCTION and not state.get("lot")
            and _peek_done(state))


def _peek_done(state):
    # Only skips finished sets and taken players (and may append the
    # accelerated round) — never opens a lot — so it is safe to call to ask.
    return _advance_cursor(state) is None


def autofill(state):
    """Complete every squad still short of the rules from the unsold players.

    Paid at base price, or whatever purse is left if less. Returns the
    signings as ``[(team, pid, price)]``.
    """
    made = []
    taken = taken_pids(state)
    spare = sorted((c for c in state["pool"].values() if c["id"] not in taken),
                   key=lambda c: -c["rating"])
    for name in state["team_order"]:
        guard = 0
        while owed_slots(state, name) > 0 and guard < 40:
            guard += 1
            owed = owed_roles(state, name)
            need_dom = (required_domestic(state) - sum(
                1 for e in state["teams"][name]["squad"]
                if not card(state, e["pid"]).get("is_overseas"))) > 0
            pick = None
            for c in spare:
                if not can_add(state, name, c):
                    continue
                if need_dom and c.get("is_overseas"):
                    continue
                if any(owed.values()) and not owed.get(c["category"]):
                    continue
                pick = c
                break
            if pick is None:
                pick = next((c for c in spare if can_add(state, name, c)), None)
            if pick is None:
                break
            price = min(base_price(pick["rating"]), max(0, state["teams"][name]["purse"]))
            _sign(state, name, pick["id"], price, HOW_AUTOFILL)
            spare.remove(pick)
            made.append((name, pick["id"], price))
    return made


def finish_auction(state):
    """Close the auction: autofill short squads, draw up the season."""
    if state["phase"] != PHASE_AUCTION:
        raise AuctionLeagueError("The auction isn't running.")
    if state.get("lot"):
        raise AuctionLeagueError("A player is still on the block.")
    filled = autofill(state)
    state["phase"] = PHASE_SEASON
    assign_grounds(state)
    state["fixtures"] = build_league_fixtures(state)
    state["table"] = {n: _empty_row() for n in state["team_order"]}
    open_trade_window(state)
    return filled


# ════════════════════════════════════════════════════════════════════
# Report card
# ════════════════════════════════════════════════════════════════════

# The AI's Playing XI: 4 batsmen, a keeper, 3 all-rounders and 3 bowlers —
# six bowling options, as an IPL side would field.
XI_SHAPE = ((ROLE_BAT, 4), (ROLE_WK, 1), (ROLE_AR, 3), (ROLE_BOWL, 3))
XI_BOWLING_OPTIONS = 5     # services.xi_rules.validate_challenge_xi's minimum


def _batting_order(xi):
    """Highest batting rating first — the openers are the best batters."""
    return sorted(xi, key=lambda c: (-(c.get("bat_rating") or 0), -c["rating"], c["name"]))


def ai_playing_xi(cards, overseas_max=11):
    """The AI's XI from ``cards`` (pool card dicts), in batting order.

    Fills the 4 BAT / 1 WK / 3 AR / 3 BOWL shape best-OVR-first within the
    overseas limit. A squad short of a role still fields a legal XI: the gap is
    filled with the best players left, a bowling option first while the side
    has fewer than five, and a keeper is always included when the squad has one.
    """
    pool = sorted(cards, key=lambda c: (-c["rating"], c["name"]))
    if len(pool) <= 11:
        return _batting_order(pool)
    xi = []
    overseas = 0

    def fits(c):
        return not c.get("is_overseas") or overseas < overseas_max

    def take(c):
        nonlocal overseas
        xi.append(c)
        pool.remove(c)
        if c.get("is_overseas"):
            overseas += 1

    for role, count in XI_SHAPE:
        for c in [c for c in pool if c["category"] == role]:
            if sum(1 for x in xi if x["category"] == role) >= count:
                break
            if fits(c):
                take(c)
    if not any(x["category"] == ROLE_WK for x in xi):
        keeper = next((c for c in pool if c["category"] == ROLE_WK), None)
        if keeper is not None:
            take(keeper)
    while len(xi) < 11 and pool:
        bowling = sum(1 for x in xi if x["category"] in (ROLE_AR, ROLE_BOWL))
        want_bowler = bowling < XI_BOWLING_OPTIONS
        pick = next((c for c in pool if fits(c) and (
            not want_bowler or c["category"] in (ROLE_AR, ROLE_BOWL))), None)
        if pick is None:
            pick = next((c for c in pool if fits(c)), None) or pool[0]
        take(pick)
    return _batting_order(xi)


def _xi_overseas_max(state):
    if not state.get("overseas_cap"):
        return 11
    return int(state["league"].get("overseas_max", 11))


def best_xi_cards(state, team):
    """The franchise's Playing XI, as pool cards in batting order."""
    return ai_playing_xi(squad_card_dicts(state, team), _xi_overseas_max(state))


def team_strength(state, team):
    """``{"ovr", "bat", "bowl"}`` of the best XI — averages, one decimal."""
    xi = best_xi_cards(state, team)
    if not xi:
        return {"ovr": 0.0, "bat": 0.0, "bowl": 0.0}
    bats = sorted((c["bat_rating"] or c["rating"] for c in xi), reverse=True)[:7]
    bowls = sorted((c["bowl_rating"] for c in xi), reverse=True)[:5]
    return {
        "ovr": round(sum(c["rating"] for c in xi) / len(xi), 1),
        "bat": round(sum(bats) / max(1, len(bats)), 1),
        "bowl": round(sum(bowls) / max(1, len(bowls)), 1),
    }


GRADES = ("A+", "A", "B+", "B", "C")


def report_card(state):
    """Every franchise ranked by strength, with a grade and its biggest buy."""
    rows = []
    for name in state["team_order"]:
        st = team_strength(state, name)
        t = state["teams"][name]
        buys = [e for e in t["squad"] if e["how"] != HOW_RETAINED]
        top = max(buys, key=lambda e: e["price"], default=None)
        rows.append({"team": name, **st, "purse": t["purse"], "size": len(t["squad"]),
                     "top_buy": top})
    rows.sort(key=lambda r: (-(r["ovr"] * 2 + r["bat"] + r["bowl"]), r["team"]))
    n = len(rows)
    for i, r in enumerate(rows):
        r["grade"] = GRADES[min(len(GRADES) - 1, int(i * len(GRADES) / max(1, n)))]
    return rows


def biggest_buys(state, limit=5):
    entries = [e for e in state["sold_log"] if e["how"] in (HOW_AUCTION, HOW_RTM)]
    entries.sort(key=lambda e: -e["price"])
    return entries[:limit]


# ════════════════════════════════════════════════════════════════════
# Trade window — after the auction, before your first match
# ════════════════════════════════════════════════════════════════════
#
# Like the IPL's pre-season window. Three things happen in it, and all of them
# are one-for-one player swaps (no cash) that leave both squads legal:
#
#   1. The AI sides trade among themselves to even the league out — the
#      strongest XI swaps a player with the weakest in the same role, so both
#      move towards the league's average. These arrive as trade news, at most
#      AI_BALANCE_TRADES of them.
#   2. Now and then an AI side approaches you (at most AI_OFFERS_MAX offers):
#      it needs a role you have spare and offers a player of similar rating.
#   3. You may propose swaps of your own; the AI says yes only to fair ones.
#
# At most TRADE_MAX swaps of yours go through, so trades stay rare.

TRADE_MAX = 2
AI_OFFERS_MAX = 2
AI_OFFER_CHANCE = 0.07
AI_BALANCE_TRADES = 3
BALANCE_TARGET = 2.0        # stop balancing once AI XIs are this close (OVR)


def _swap(state, team_a, pid_a, team_b, pid_b):
    """Move ``pid_a`` to ``team_b`` and ``pid_b`` to ``team_a`` (prices travel)."""
    ta, tb = state["teams"][team_a], state["teams"][team_b]
    ea = next(e for e in ta["squad"] if e["pid"] == int(pid_a))
    eb = next(e for e in tb["squad"] if e["pid"] == int(pid_b))
    ta["squad"].remove(ea)
    tb["squad"].remove(eb)
    ta["squad"].append(dict(eb, how=eb.get("how")))
    tb["squad"].append(dict(ea, how=ea.get("how")))


def trade_legal(state, team_a, pid_a, team_b, pid_b):
    """Whether the swap leaves both squads legal (roles, overseas, domestic)."""
    if team_a == team_b:
        return False
    owns = lambda t, p: any(e["pid"] == int(p) for e in state["teams"][t]["squad"])
    if not (owns(team_a, pid_a) and owns(team_b, pid_b)):
        return False
    _swap(state, team_a, pid_a, team_b, pid_b)
    try:
        return squad_is_legal(state, team_a) and squad_is_legal(state, team_b)
    finally:
        _swap(state, team_a, pid_b, team_b, pid_a)


def _ai_names(state):
    return [n for n in state["team_order"] if n != state["user_team"]]


def xi_strength(state, team):
    """The mean OVR of the XI ``team`` fields (what parity is measured on)."""
    xi = best_xi_cards(state, team)
    return sum(c["rating"] for c in xi) / max(1, len(xi))


def ai_spread(state):
    vals = [xi_strength(state, n) for n in _ai_names(state)]
    return (max(vals) - min(vals)) if vals else 0.0


def balance_squads(state, max_trades=AI_BALANCE_TRADES, target=BALANCE_TARGET):
    """AI-to-AI trades that pull the strongest and weakest XIs together.

    Each round tries same-role swaps between the three strongest and three
    weakest AI sides and makes the one that narrows the league's spread the
    most. Returns the trades made as news entries.
    """
    news = []
    for _ in range(max_trades):
        strength = {n: xi_strength(state, n) for n in _ai_names(state)}
        spread = max(strength.values()) - min(strength.values())
        if spread <= target:
            break
        ranked = sorted(strength, key=strength.get)
        weak, strong = ranked[:3], ranked[-3:]
        best = None
        for s_team in strong:
            for w_team in weak:
                if strength[s_team] - strength[w_team] < 1.0:
                    continue
                for es in state["teams"][s_team]["squad"]:
                    a = card(state, es["pid"])
                    for ew in state["teams"][w_team]["squad"]:
                        b = card(state, ew["pid"])
                        if a["category"] != b["category"] or a["rating"] <= b["rating"]:
                            continue
                        if not trade_legal(state, s_team, a["id"], w_team, b["id"]):
                            continue
                        _swap(state, s_team, a["id"], w_team, b["id"])
                        new = ai_spread(state)
                        _swap(state, s_team, b["id"], w_team, a["id"])
                        score = (new, a["rating"] - b["rating"])
                        if new < spread - 0.25 and (best is None or score < best[0]):
                            best = (score, s_team, a["id"], w_team, b["id"])
        if best is None:
            break
        _, s_team, pa, w_team, pb = best
        _swap(state, s_team, pa, w_team, pb)
        news.append({"a": s_team, "pa": pa, "b": w_team, "pb": pb})
    return news


def _user_surplus_roles(state):
    counts = role_counts(state, state["user_team"])
    return [r for r in ROLES if counts[r] > max(ROLE_MIN[r], XI_TARGET[r])]


def make_ai_offers(state, rng):
    """At most AI_OFFERS_MAX approaches to you — only when a side has a real
    need you can meet. Each offer is fair (within 2 OVR) and legal both ways."""
    me = state["user_team"]
    surplus = _user_surplus_roles(state)
    offers = []
    if not surplus:
        return offers
    my_xi = {c["id"] for c in best_xi_cards(state, me)}
    teams = _ai_names(state)
    rng.shuffle(teams)
    for team in teams:
        if len(offers) >= AI_OFFERS_MAX:
            break
        if rng.random() > AI_OFFER_CHANCE:
            continue
        before = xi_strength(state, team)
        mine = sorted((c for c in squad_card_dicts(state, me)
                       if c["category"] in surplus and c["id"] not in my_xi),
                      key=lambda c: -c["rating"])
        made = None
        for want in mine:
            gives = sorted((c for c in squad_card_dicts(state, team)
                            if abs(c["rating"] - want["rating"]) <= 2),
                           key=lambda c: -c["rating"])
            for give in gives:
                if not trade_legal(state, team, give["id"], me, want["id"]):
                    continue
                _swap(state, team, give["id"], me, want["id"])
                after = xi_strength(state, team)
                _swap(state, team, want["id"], me, give["id"])
                if after > before:
                    made = (give, want)
                    break
            if made:
                break
        if made:
            offers.append({"id": len(offers) + 1, "team": team, "give": made[0]["id"],
                           "want": made[1]["id"], "status": "open"})
    return offers


def open_trade_window(state, rng=None):
    rng = rng or rng_for(state, "trade")
    news = balance_squads(state)
    state["trade"] = {"open": True, "done": 0, "max": TRADE_MAX, "asked": [],
                      "news": news, "offers": make_ai_offers(state, rng)}
    return state["trade"]


def trade_window(state):
    t = state.get("trade") or {}
    return t if t.get("open") else None


def close_trade_window(state):
    if state.get("trade"):
        state["trade"]["open"] = False


def _trade_slots_left(state):
    t = trade_window(state)
    return 0 if t is None else max(0, int(t["max"]) - int(t["done"]))


def answer_offer(state, offer_id, accept):
    """Accept or reject an AI side's approach. Returns the offer."""
    t = trade_window(state)
    if t is None:
        raise AuctionLeagueError("The trade window is closed.")
    offer = next((o for o in t["offers"] if o["id"] == int(offer_id)), None)
    if offer is None or offer["status"] != "open":
        raise AuctionLeagueError("That offer is no longer on the table.")
    if not accept:
        offer["status"] = "rejected"
        return offer
    if _trade_slots_left(state) <= 0:
        raise AuctionLeagueError(f"You've made your {t['max']} trades for this window.")
    if not trade_legal(state, offer["team"], offer["give"], state["user_team"], offer["want"]):
        offer["status"] = "void"
        raise AuctionLeagueError("That trade would leave a squad illegal now.")
    _swap(state, offer["team"], offer["give"], state["user_team"], offer["want"])
    offer["status"] = "accepted"
    t["done"] += 1
    return offer


def propose_trade(state, my_pid, team, their_pid):
    """Your proposal: ``my_pid`` for ``team``'s ``their_pid``.

    Returns ``(accepted, reason)``. The AI says yes only to a fair swap: the
    player it receives is within 1 OVR of the one it gives (or better), and its
    XI does not get weaker. A pair it has turned down can't be asked again.
    """
    t = trade_window(state)
    if t is None:
        raise AuctionLeagueError("The trade window is closed.")
    if _trade_slots_left(state) <= 0:
        raise AuctionLeagueError(f"You've made your {t['max']} trades for this window.")
    me = state["user_team"]
    key = [team, int(my_pid), int(their_pid)]
    if key in t["asked"]:
        raise AuctionLeagueError("They've already turned that one down.")
    if team not in state["teams"] or team == me:
        raise AuctionLeagueError("Pick one of the AI franchises.")
    if not trade_legal(state, me, my_pid, team, their_pid):
        t["asked"].append(key)
        return False, "That swap would leave one of the squads breaking the rules."
    give, get = card(state, my_pid), card(state, their_pid)
    before = xi_strength(state, team)
    _swap(state, me, my_pid, team, their_pid)
    after = xi_strength(state, team)
    if give["rating"] < get["rating"] - 1 or after < before - 0.05:
        _swap(state, me, their_pid, team, my_pid)
        t["asked"].append(key)
        why = ("he's a better player than the one you're offering"
               if give["rating"] < get["rating"] - 1 else "it would weaken their XI")
        return False, f"{team} said no — {why}."
    t["done"] += 1
    return True, f"{team} accepted!"


# ════════════════════════════════════════════════════════════════════
# Season: fixtures, table, playoffs
# ════════════════════════════════════════════════════════════════════

def _empty_row():
    return {"p": 0, "w": 0, "l": 0, "t": 0, "pts": 0,
            "rf": 0, "bf": 0, "ra": 0, "ba": 0}


# The IPL's own grounds, for a league based in India; any other league gets
# the bot's stadiums in its home country (data/stadiums.json).
IPL_GROUNDS = (
    "Wankhede Stadium, Mumbai", "M.A. Chidambaram Stadium, Chennai",
    "M.Chinnaswamy Stadium, Bengaluru", "Eden Gardens, Kolkata",
    "Arun Jaitley Stadium, Delhi", "Narendra Modi Stadium, Ahmedabad",
    "Rajiv Gandhi Intl. Stadium, Hyderabad", "Sawai Mansingh Stadium, Jaipur",
    "Ekana Cricket Stadium, Lucknow", "PCA New Stadium, Mullanpur",
)


def _ground_list(state):
    home = (state["league"].get("home_country") or "").strip().lower()
    if home in ("", "india"):
        return list(IPL_GROUNDS)
    try:
        import json as _json
        import os as _os
        path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)),
                             "data", "stadiums.json")
        with open(path, encoding="utf-8") as fh:
            rows = _json.load(fh).get("stadiums") or []
        local = [r["name"] for r in rows if str(r.get("country", "")).lower() == home]
        return local or [r["name"] for r in rows] or list(IPL_GROUNDS)
    except Exception:
        return list(IPL_GROUNDS)


def assign_grounds(state):
    """Give every franchise a home ground (kept once given)."""
    rng = rng_for(state, "grounds")
    grounds = _ground_list(state)
    rng.shuffle(grounds)
    for i, name in enumerate(state["team_order"]):
        t = state["teams"][name]
        if not t.get("ground"):
            t["ground"] = grounds[i % len(grounds)]


def _fixture_conditions(state, rng, home):
    """A fixture's venue (the hosts' ground; neutral for a playoff) and a random pitch."""
    if home is None:
        venue = rng.choice(_ground_list(state))
    else:
        venue = state["teams"][home].get("ground") or rng.choice(_ground_list(state))
    return venue, rng.choice(_pitches())


def fixture_pitch(state, fx):
    """The pitch the fixture is played on — set when it was scheduled."""
    if not fx.get("pitch"):
        fx["pitch"] = rng_for(state, f"pitch{fx['no']}").choice(_pitches())
    return fx["pitch"]


def build_league_fixtures(state):
    """A single round robin, rounds in a shuffled order, home sides alternating.

    Every fixture is played at the hosts' ground on a pitch drawn at random.
    """
    from services.league_schedule_service import round_robin_rounds
    rng = rng_for(state, "fixtures")
    rounds = round_robin_rounds(list(state["team_order"]))
    rng.shuffle(rounds)
    fixtures = []
    home_count = {n: 0 for n in state["team_order"]}
    for rnd in rounds:
        for a, b in rnd:
            if home_count[a] > home_count[b]:
                a, b = b, a
            home_count[a] += 1
            venue, pitch = _fixture_conditions(state, rng, a)
            fixtures.append({"no": len(fixtures) + 1, "stage": STAGE_LEAGUE,
                             "home": a, "away": b, "status": "pending",
                             "result": None, "venue": venue, "pitch": pitch})
    return fixtures


def _add_fixture(state, stage, home, away):
    venue, pitch = _fixture_conditions(state, rng_for(state, f"po{stage}"), None)
    fx = {"no": len(state["fixtures"]) + 1, "stage": stage, "home": home,
          "away": away, "status": "pending", "result": None,
          "venue": venue, "pitch": pitch}
    state["fixtures"].append(fx)
    return fx


def nrr(row):
    if not row["bf"] or not row["ba"]:
        return 0.0
    return round(row["rf"] * 6 / row["bf"] - row["ra"] * 6 / row["ba"], 3)


def standings(state):
    """Team names in table order: points, then net run-rate, then wins."""
    table = state.get("table") or {}
    return sorted(state["team_order"],
                  key=lambda n: (-table[n]["pts"], -nrr(table[n]), -table[n]["w"], n))


def _stage_done(state, stage):
    fx = [f for f in state["fixtures"] if f["stage"] == stage]
    return bool(fx) and all(f["status"] == "done" for f in fx)


def _winner_loser(fx, state):
    res = fx["result"] or {}
    w = res.get("winner")
    if w:
        return w, (fx["away"] if w == fx["home"] else fx["home"])
    # A playoff tie that even the Super Over could not split: the side higher
    # in the league table goes through.
    order = standings(state)
    a, b = fx["home"], fx["away"]
    return (a, b) if order.index(a) < order.index(b) else (b, a)


def _schedule_playoffs(state):
    """Add whichever playoff fixtures have just become known."""
    have = {f["stage"] for f in state["fixtures"]}
    if STAGE_Q1 not in have and _stage_done(state, STAGE_LEAGUE):
        top = standings(state)[:4]
        _add_fixture(state, STAGE_Q1, top[0], top[1])
        _add_fixture(state, STAGE_ELIM, top[2], top[3])
        return
    if STAGE_Q2 not in have and _stage_done(state, STAGE_Q1) and _stage_done(state, STAGE_ELIM):
        q1 = next(f for f in state["fixtures"] if f["stage"] == STAGE_Q1)
        el = next(f for f in state["fixtures"] if f["stage"] == STAGE_ELIM)
        _add_fixture(state, STAGE_Q2, _winner_loser(q1, state)[1], _winner_loser(el, state)[0])
        return
    if STAGE_FINAL not in have and _stage_done(state, STAGE_Q2):
        q1 = next(f for f in state["fixtures"] if f["stage"] == STAGE_Q1)
        q2 = next(f for f in state["fixtures"] if f["stage"] == STAGE_Q2)
        _add_fixture(state, STAGE_FINAL, _winner_loser(q1, state)[0], _winner_loser(q2, state)[0])
        return
    if _stage_done(state, STAGE_FINAL) and state["phase"] != PHASE_COMPLETED:
        fin = next(f for f in state["fixtures"] if f["stage"] == STAGE_FINAL)
        state["champion"], state["runner_up"] = _winner_loser(fin, state)
        state["phase"] = PHASE_COMPLETED


def recent_form(state, team, n=5):
    """The last ``n`` league results, oldest first: ``["W", "L", "T", …]``."""
    out = []
    for fx in state["fixtures"]:
        if fx["stage"] != STAGE_LEAGUE or fx["status"] != "done":
            continue
        if team not in (fx["home"], fx["away"]):
            continue
        w = (fx["result"] or {}).get("winner")
        out.append("T" if not w else ("W" if w == team else "L"))
    return out[-n:]


PLAYOFF_SPOTS = 4


def qualification(state):
    """``{team: "Q" | "E" | None}`` — through to the playoffs, or out.

    During the league: Q once fewer than four other sides can still reach a
    team's points; E once four sides are already beyond its best possible
    total. Net run-rate is ignored, so a mark is only shown when it is certain.
    After the league stage the top four are Q and the rest E.
    """
    table = state.get("table") or {}
    league = [f for f in state["fixtures"] if f["stage"] == STAGE_LEAGUE]
    if league and all(f["status"] == "done" for f in league):
        top = standings(state)[:PLAYOFF_SPOTS]
        return {n: ("Q" if n in top else "E") for n in state["team_order"]}
    left = {n: 0 for n in state["team_order"]}
    for f in league:
        if f["status"] == "pending":
            left[f["home"]] += 1
            left[f["away"]] += 1
    pts = {n: table.get(n, {}).get("pts", 0) for n in state["team_order"]}
    best = {n: pts[n] + 2 * left[n] for n in state["team_order"]}
    marks = {}
    for n in state["team_order"]:
        others = [m for m in state["team_order"] if m != n]
        if sum(1 for m in others if pts[m] > best[n]) >= PLAYOFF_SPOTS:
            marks[n] = "E"
        elif sum(1 for m in others if best[m] >= pts[n]) < PLAYOFF_SPOTS:
            marks[n] = "Q"
        else:
            marks[n] = None
    return marks


def team_of_the_tournament(state):
    """The season's XI by MVP points, in the AI's 4/1/3/3 shape, batting order."""
    scored = []
    for pid in set(state["stats"]["bat"]) | set(state["stats"]["bowl"]):
        c = state["pool"].get(str(pid))
        if c:
            scored.append(dict(c, rating=mvp_points(state, pid)))
    xi = ai_playing_xi(scored, 11)
    return [state["pool"][str(c["id"])] for c in xi]


def season_awards(state):
    """The end-of-season awards: ``{name: (pid, headline)}`` plus the XI."""
    awards = {}
    orange = stat_board(state, "orange", 1)
    if orange:
        pid, r = orange[0]
        awards["orange"] = (pid, f"{r['runs']} runs")
    purple = stat_board(state, "purple", 1)
    if purple:
        pid, r = purple[0]
        awards["purple"] = (pid, f"{r['wkts']} wickets")
    mvp = stat_board(state, "mvp", 1)
    if mvp:
        pid, r = mvp[0]
        awards["mvp"] = (pid, f"{r['points']} points")
    sixes = stat_board(state, "sixes", 1)
    if sixes:
        pid, r = sixes[0]
        awards["sixes"] = (pid, f"{r['sixes']} sixes")
    young = [(p, r) for p, r in stat_board(state, "mvp", 500)
             if state["pool"].get(str(p), {}).get("rating", 99) < EMERGING_MAX_RATING]
    if young:
        pid, r = young[0]
        awards["emerging"] = (pid, f"{r['points']} points")
    awards["xi"] = team_of_the_tournament(state)
    return awards


# The Emerging Player is the best performer among the lesser-known names.
EMERGING_MAX_RATING = 80


def pending_fixtures(state):
    return [f for f in state["fixtures"] if f["status"] == "pending"]


def next_user_fixture(state):
    me = state["user_team"]
    return next((f for f in pending_fixtures(state) if me in (f["home"], f["away"])), None)


def fixture_by_no(state, no):
    return next((f for f in state["fixtures"] if f["no"] == int(no)), None)


def _team_balls(balls, wickets, overs=OVERS):
    """Balls for NRR: an all-out side is charged its full quota (IPL rule)."""
    return overs * 6 if int(wickets) >= 10 else int(balls)


def record_result(state, fx, *, inn1_team, inn1_runs, inn1_wkts, inn1_balls,
                  inn2_team, inn2_runs, inn2_wkts, inn2_balls, winner, text="",
                  conceded=False):
    """Write one finished fixture into the table (league stage) and the bracket."""
    if fx["status"] == "done":
        return False
    fx["status"] = "done"
    fx["result"] = {
        "inn1": [inn1_team, int(inn1_runs), int(inn1_wkts), int(inn1_balls)],
        "inn2": [inn2_team, int(inn2_runs), int(inn2_wkts), int(inn2_balls)],
        "winner": winner,
        "text": text,
        "conceded": bool(conceded),
    }
    if not conceded:
        _add_total(state, inn1_team, inn1_runs, inn1_wkts, inn1_balls, inn2_team, fx["no"])
        _add_total(state, inn2_team, inn2_runs, inn2_wkts, inn2_balls, inn1_team, fx["no"])
    if fx["stage"] == STAGE_LEAGUE:
        table = state["table"]
        for n in (fx["home"], fx["away"]):
            table[n]["p"] += 1
        if winner:
            loser = fx["away"] if winner == fx["home"] else fx["home"]
            table[winner]["w"] += 1
            table[winner]["pts"] += 2
            table[loser]["l"] += 1
        else:
            for n in (fx["home"], fx["away"]):
                table[n]["t"] += 1
                table[n]["pts"] += 1
        if not conceded:
            b1 = _team_balls(inn1_balls, inn1_wkts)
            b2 = _team_balls(inn2_balls, inn2_wkts)
            table[inn1_team]["rf"] += int(inn1_runs)
            table[inn1_team]["bf"] += b1
            table[inn1_team]["ra"] += int(inn2_runs)
            table[inn1_team]["ba"] += b2
            table[inn2_team]["rf"] += int(inn2_runs)
            table[inn2_team]["bf"] += b2
            table[inn2_team]["ra"] += int(inn1_runs)
            table[inn2_team]["ba"] += b1
    _schedule_playoffs(state)
    return True


RECORDS_KEPT = 10


def _records(state):
    return state.setdefault("records", {"innings": [], "figures": [], "totals": []})


def _keep(rows, row, key, limit=RECORDS_KEPT):
    rows.append(row)
    rows.sort(key=key)
    del rows[limit:]


def _add_bat(state, pid, bs, *, opp=None, fx_no=None):
    """Add one batting innings (``bs``: runs, balls, fours, sixes, out)."""
    c = state["pool"].get(str(pid))
    if not c:
        return
    runs, balls = int(bs.get("runs", 0)), int(bs.get("balls", 0))
    out = bool(bs.get("out"))
    row = state["stats"]["bat"].setdefault(str(pid), {})
    for k in ("runs", "balls", "inns", "outs", "hs", "fours", "sixes", "fifties", "hundreds"):
        row.setdefault(k, 0)
    row["runs"] += runs
    row["balls"] += balls
    row["inns"] += 1
    row["outs"] += 1 if out else 0
    row["fours"] += int(bs.get("fours", 0) or 0)
    row["sixes"] += int(bs.get("sixes", 0) or 0)
    if runs >= 100:
        row["hundreds"] += 1
    elif runs >= 50:
        row["fifties"] += 1
    if runs > row["hs"] or (runs == row["hs"] and not out):
        row["hs"] = runs
        row["hs_not_out"] = not out
    if runs >= 30:
        _keep(_records(state)["innings"],
              {"pid": int(pid), "runs": runs, "balls": balls, "not_out": not out,
               "opp": opp, "fx": fx_no},
              key=lambda r: (-r["runs"], r["balls"]))


def _add_bowl(state, pid, bw, *, opp=None, fx_no=None):
    """Add one bowling spell (``bw``: wickets, runs, balls, maidens)."""
    c = state["pool"].get(str(pid))
    balls = int(bw.get("balls", 0) or 0)
    if not c or not balls:
        return
    wkts, runs = int(bw.get("wickets", 0) or 0), int(bw.get("runs", 0) or 0)
    row = state["stats"]["bowl"].setdefault(str(pid), {})
    for k in ("wkts", "runs", "balls", "maidens", "inns", "three_fors"):
        row.setdefault(k, 0)
    row["wkts"] += wkts
    row["runs"] += runs
    row["balls"] += balls
    row["maidens"] += int(bw.get("maidens", 0) or 0)
    row["inns"] += 1
    if wkts >= 3:
        row["three_fors"] += 1
    best = row.get("best") or [0, 999]
    if wkts > best[0] or (wkts == best[0] and runs < best[1]):
        row["best"] = [wkts, runs]
    if wkts >= 2:
        _keep(_records(state)["figures"],
              {"pid": int(pid), "wkts": wkts, "runs": runs, "balls": balls,
               "opp": opp, "fx": fx_no},
              key=lambda r: (-r["wkts"], r["runs"]))


def _add_total(state, team, runs, wkts, balls, opp, fx_no):
    totals = _records(state)["totals"]
    totals.append({"team": team, "runs": int(runs), "wkts": int(wkts),
                   "balls": int(balls), "opp": opp, "fx": fx_no})


MVP_POINTS = {"run": 1, "four": 1, "six": 2, "wicket": 25, "maiden": 8}


def mvp_points(state, pid):
    b = state["stats"]["bat"].get(str(pid)) or {}
    w = state["stats"]["bowl"].get(str(pid)) or {}
    return (b.get("runs", 0) * MVP_POINTS["run"] + b.get("fours", 0) * MVP_POINTS["four"]
            + b.get("sixes", 0) * MVP_POINTS["six"] + w.get("wkts", 0) * MVP_POINTS["wicket"]
            + w.get("maidens", 0) * MVP_POINTS["maiden"])


STAT_BOARDS = ("orange", "purple", "mvp", "sixes", "sr", "econ",
               "innings", "figures", "totals", "mine")
MIN_SR_BALLS = 30
MIN_ECON_BALLS = 36


def stat_board(state, board, limit=10):
    """Leaderboard rows for one of ``STAT_BOARDS`` (plain dicts, best first)."""
    bat, bowl = state["stats"]["bat"], state["stats"]["bowl"]
    if board == "orange":
        rows = sorted(bat.items(), key=lambda kv: (-kv[1]["runs"], kv[1]["balls"]))
    elif board == "purple":
        rows = sorted(bowl.items(), key=lambda kv: (
            -kv[1]["wkts"], kv[1]["runs"] / max(1, kv[1]["balls"])))
    elif board == "sixes":
        rows = sorted(((k, v) for k, v in bat.items() if v.get("sixes")),
                      key=lambda kv: (-kv[1].get("sixes", 0), -kv[1]["runs"]))
    elif board == "sr":
        rows = sorted(((k, v) for k, v in bat.items() if v["balls"] >= MIN_SR_BALLS),
                      key=lambda kv: -kv[1]["runs"] / kv[1]["balls"])
    elif board == "econ":
        rows = sorted(((k, v) for k, v in bowl.items() if v["balls"] >= MIN_ECON_BALLS),
                      key=lambda kv: kv[1]["runs"] / kv[1]["balls"])
    elif board == "mvp":
        pids = set(bat) | set(bowl)
        rows = sorted(((p, {"points": mvp_points(state, p)}) for p in pids),
                      key=lambda kv: -kv[1]["points"])
    elif board == "innings":
        return list(_records(state)["innings"][:limit])
    elif board == "figures":
        return list(_records(state)["figures"][:limit])
    elif board == "totals":
        totals = _records(state)["totals"]
        high = sorted(totals, key=lambda r: (-r["runs"], r["wkts"]))[:5]
        low = sorted((r for r in totals if r["wkts"] >= 10 or r["balls"] >= OVERS * 6),
                     key=lambda r: (r["runs"], -r["wkts"]))[:5]
        return {"high": high, "low": low}
    elif board == "mine":
        me = state["user_team"]
        pids = [e["pid"] for e in state["teams"][me]["squad"]]
        return [(str(p), bat.get(str(p)) or {}, bowl.get(str(p)) or {}) for p in pids]
    else:
        raise AuctionLeagueError("Unknown stats board.")
    return rows[:limit]


def owner_of(state, pid):
    for name, t in state["teams"].items():
        if any(e["pid"] == int(pid) for e in t["squad"]):
            return name
    return None


def cap_tables(state, limit=5):
    """``(orange, purple)`` — top run-scorers and wicket-takers."""
    return stat_board(state, "orange", limit), stat_board(state, "purple", limit)


# ── AI-vs-AI matches ─────────────────────────────────────────────────

def _engine_xi(state, team):
    from services.cipl_match import cp_to_player_dict
    return [cp_to_player_dict(LeagueCard(c)) for c in best_xi_cards(state, team)]


def _pitches():
    try:
        from services.match_constants import PITCH_TYPES
        return list(PITCH_TYPES) or ["Flat"]
    except Exception:
        return ["Flat"]


def simulate_fixture(state, fx, rng=None):
    """Play one fixture instantly on the sim engine and record it."""
    from services.sim_match import simulate_match
    rng = rng or rng_for(state, f"fx{fx['no']}")
    home_xi = _engine_xi(state, fx["home"])
    away_xi = _engine_xi(state, fx["away"])
    rng.choice(_pitches())   # keeps a save's random sequence where it was
    pitch = fixture_pitch(state, fx)
    # sim_match draws from the global generator; seed it so a save replays,
    # and put the process-wide state back afterwards so no other feature in
    # the bot inherits this career's sequence.
    saved = random.getstate()
    random.seed(rng.random())
    toss = rng.choice([fx["home"], fx["away"]])
    try:
        m = simulate_match(home_xi, away_xi, OVERS, pitch, fx["home"], fx["away"],
                           toss_winner=toss, toss_decision=None, scenario=False)
    finally:
        random.setstate(saved)
    i1, i2 = m["innings1"], m["innings2"]
    for inn in (i1, i2):
        bat_team = inn["batting_team"]
        bowl_team = fx["away"] if bat_team == fx["home"] else fx["home"]
        for p in inn["order"]:
            bs = inn["bat_stats"].get(id(p)) or {}
            if bs.get("balls") or bs.get("out"):
                _add_bat(state, p.get("roster_id"), bs, opp=bowl_team, fx_no=fx["no"])
        seen = set()
        for bp in inn["bowl_plan"]:
            if id(bp) in seen:
                continue
            seen.add(id(bp))
            bw = inn["bowl_stats"].get(id(bp)) or {}
            _add_bowl(state, bp.get("roster_id"), bw, opp=bat_team, fx_no=fx["no"])
    res = m["result"]
    record_result(state, fx,
                  inn1_team=i1["batting_team"], inn1_runs=i1["runs"],
                  inn1_wkts=i1["wickets"], inn1_balls=i1["balls"],
                  inn2_team=i2["batting_team"], inn2_runs=i2["runs"],
                  inn2_wkts=i2["wickets"], inn2_balls=i2["balls"],
                  winner=res.get("winner"), text=res.get("text") or "")
    return fx


def sim_until_user(state):
    """Simulate every pending AI fixture that comes before your next one.

    When you have no fixture left (knocked out, or the league is over for
    you), plays the rest of the season out. Returns the fixtures played.
    """
    played = []
    guard = 0
    while guard < 500 and state["phase"] == PHASE_SEASON:
        guard += 1
        pend = pending_fixtures(state)
        if not pend:
            break
        fx = pend[0]
        if state["user_team"] in (fx["home"], fx["away"]):
            break
        simulate_fixture(state, fx)
        played.append(fx)
    return played


def concede_fixture(state, fx):
    """You forfeit your fixture: a loss, no NRR either way."""
    me = state["user_team"]
    opp = fx["away"] if fx["home"] == me else fx["home"]
    state["user_conceded"] = int(state.get("user_conceded") or 0) + 1
    record_result(state, fx, inn1_team=opp, inn1_runs=0, inn1_wkts=0, inn1_balls=0,
                  inn2_team=me, inn2_runs=0, inn2_wkts=0, inn2_balls=0,
                  winner=opp, text=f"{opp} won — conceded", conceded=True)


def record_user_match(state, fixture_no, match_state, winner_team, match_id=None):
    """Write your ball-by-ball match into the season.

    ``match_state`` is the finished live state from ``handlers.cipl_play``.
    ``winner_team`` is the winning team's name, or None for a tie. Idempotent
    on ``match_id`` — the same match is never recorded twice.
    """
    fx = fixture_by_no(state, fixture_no)
    if fx is None or fx["status"] == "done":
        return False
    if match_id is not None and fx.get("match_id") not in (None, match_id):
        return False
    fx["match_id"] = match_id
    s = match_state
    inn1_team = s.get("inn1_bat_team") or s.get("inn1_team")
    inn2_team = s.get("bat_team_name")
    if inn1_team not in (fx["home"], fx["away"]) or inn2_team not in (fx["home"], fx["away"]):
        # Fall back to the two sides in fixture order — the scores still count.
        inn1_team = inn1_team if inn1_team in (fx["home"], fx["away"]) else fx["home"]
        inn2_team = fx["away"] if inn1_team == fx["home"] else fx["home"]
    from services import cipl_match
    try:
        inn2_balls = int(cipl_match.balls_bowled(s))
    except Exception:
        inn2_balls = int(s.get("balls") or 0)
    for key, opp in (("inn1_bat_stats", inn2_team), ("bat_stats", inn1_team)):
        for rid, bs in (s.get(key) or {}).items():
            if bs.get("balls") or bs.get("out"):
                _add_bat(state, rid, bs, opp=opp, fx_no=fx["no"])
    for key, opp in (("inn1_bowl_stats", inn1_team), ("bowl_stats", inn2_team)):
        for rid, bw in (s.get(key) or {}).items():
            _add_bowl(state, rid, bw, opp=opp, fx_no=fx["no"])
    if winner_team not in (fx["home"], fx["away"]):
        winner_team = None
    text = (f"{winner_team} won" if winner_team else "Match tied")
    record_result(state, fx,
                  inn1_team=inn1_team, inn1_runs=s.get("inn1_runs") or 0,
                  inn1_wkts=s.get("inn1_wickets") or 0,
                  inn1_balls=s.get("inn1_balls") or 0,
                  inn2_team=inn2_team, inn2_runs=s.get("total_runs") or 0,
                  inn2_wkts=s.get("total_wickets") or 0, inn2_balls=inn2_balls,
                  winner=winner_team, text=text)
    return True


def season_finish(state):
    """Where you finished: ``"champion" | "runner_up" | "playoffs" | "league"``."""
    me = state["user_team"]
    if state.get("champion") == me:
        return "champion"
    if state.get("runner_up") == me:
        return "runner_up"
    if any(me in (f["home"], f["away"]) for f in state["fixtures"]
           if f["stage"] != STAGE_LEAGUE):
        return "playoffs"
    return "league"


def season_reward(state):
    """``(coins, gems)`` this finished season earns — nothing if you conceded."""
    if state["phase"] != PHASE_COMPLETED or int(state.get("user_conceded") or 0):
        return 0, 0
    return {"champion": REWARD_CHAMPION, "runner_up": REWARD_RUNNER_UP,
            "playoffs": REWARD_PLAYOFFS}.get(season_finish(state), (0, 0))


# ════════════════════════════════════════════════════════════════════
# Database: the league snapshot and the save row
# ════════════════════════════════════════════════════════════════════

def _details(cp):
    raw = getattr(cp, "details_json", None) or ""
    try:
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def card_from_challenge_player(cp, team_name):
    d = _details(cp)

    def _g(*keys, default=None):
        for k in keys:
            if d.get(k) not in (None, ""):
                return d.get(k)
        return default

    from services.xi_rules import challenge_is_overseas
    return make_card(
        cp.id, getattr(cp, "name", None) or _g("name", default="Player"),
        category=_g("category", "role", default=ROLE_BAT),
        rating=int(_g("rating", default=50) or 50),
        bat_rating=int(_g("bat_rating", default=50) or 50),
        bowl_rating=int(_g("bowl_rating", default=40) or 40),
        bat_hand=_g("bat_hand", default="Right"),
        bowl_hand=_g("bowl_hand", default="Right"),
        bowl_style=_g("bowl_style", default=""),
        country=_g("country", default=""),
        is_overseas=challenge_is_overseas(cp),
        source_player_id=getattr(cp, "source_player_id", None) or _g("source_player_id"),
        team=team_name)


def league_dict(league, key):
    return {
        "id": league.id,
        "name": league.name,
        "key": key,
        "home_country": getattr(league, "home_country", None),
        "overseas_min": getattr(league, "min_overseas", 0) or 0,
        "overseas_max": (11 if getattr(league, "max_overseas", None) is None
                         else league.max_overseas),
        "ball_format": getattr(league, "match_format", "T20") or "T20",
    }


def league_teams(session, league):
    """The league's active teams as ``new_state`` wants them."""
    from models import ChallengePlayer, ChallengeTeam
    teams = []
    rows = (session.query(ChallengeTeam)
            .filter(ChallengeTeam.league_id == league.id,
                    ChallengeTeam.is_active.is_(True))
            .order_by(ChallengeTeam.sort_order, ChallengeTeam.name).all())
    for t in rows:
        players = (session.query(ChallengePlayer)
                   .filter(ChallengePlayer.team_id == t.id)
                   .order_by(ChallengePlayer.sort_order, ChallengePlayer.name).all())
        teams.append({"name": t.name, "short": t.short_name,
                      "players": [card_from_challenge_player(p, t.name) for p in players]})
    return teams


ACTIVE_STATUSES = (PHASE_RETENTION, PHASE_AUCTION, PHASE_SEASON)


def active_save(session, tg_id):
    from models import AuctionLeagueSave
    return (session.query(AuctionLeagueSave)
            .filter(AuctionLeagueSave.user_tg_id == int(tg_id),
                    AuctionLeagueSave.status.in_(ACTIVE_STATUSES))
            .order_by(AuctionLeagueSave.id.desc()).first())


def latest_save(session, tg_id):
    """The active save, else the most recently finished one (for its results)."""
    from models import AuctionLeagueSave
    return active_save(session, tg_id) or (
        session.query(AuctionLeagueSave)
        .filter(AuctionLeagueSave.user_tg_id == int(tg_id),
                AuctionLeagueSave.status == PHASE_COMPLETED)
        .order_by(AuctionLeagueSave.id.desc()).first())


def load(row):
    return json.loads(row.state_json)


def store(row, state):
    row.state_json = json.dumps(state, separators=(",", ":"))
    row.status = state["phase"]
    row.version = int(row.version or 0) + 1


def create_save(session, tg_id, state):
    from models import AuctionLeagueSave
    if active_save(session, tg_id) is not None:
        raise AuctionLeagueError("You already have an Auction League career — "
                                 "/auctionleague to continue it, or /alquit first.")
    row = AuctionLeagueSave(user_tg_id=int(tg_id), league_id=state["league"]["id"],
                            league_name=state["league"]["name"],
                            user_team_name=state["user_team"], status=state["phase"],
                            state_json="{}", version=0)
    store(row, state)
    session.add(row)
    session.flush()
    return row


def record_user_result(session, match_state, winner_user_id=None):
    """Called by the match engine when an Auction League fixture finishes.

    ``match_state['auction_league']`` carries the save and fixture. Returns the
    save's state after recording, or None when nothing was recorded.
    """
    from models import AuctionLeagueSave
    tag = (match_state or {}).get("auction_league") or {}
    save_id = tag.get("save_id")
    if not save_id:
        return None
    row = session.get(AuctionLeagueSave, int(save_id))
    if row is None or row.status != PHASE_SEASON:
        return None
    state = load(row)
    if winner_user_id is None:
        winner = None
    elif int(winner_user_id) == int(tag.get("user_id") or 0):
        winner = state["user_team"]
    else:
        winner = tag.get("opp_team")
    if not record_user_match(state, tag.get("fixture_no"), match_state, winner,
                             match_id=match_state.get("match_id")):
        return None
    store(row, state)
    return state


def pay_season_reward(session, row, state, user):
    """Pay the season's reward once, honouring the per-user cooldown.

    Returns ``(coins, gems, note)``; ``note`` explains a zero.
    """
    from datetime import datetime, timedelta
    from models import AuctionLeagueSave
    if row.reward_paid:
        return 0, 0, "already paid"
    coins, gems = season_reward(state)
    if not coins and not gems:
        row.reward_paid = True
        if int(state.get("user_conceded") or 0):
            return 0, 0, "a conceded match forfeits the season reward"
        return 0, 0, ""
    now = datetime.utcnow()
    since = now - timedelta(hours=REWARD_COOLDOWN_HOURS)
    # Only a season that actually paid out starts the cooldown — and it runs
    # from the moment it paid, not from the save's last update.
    recent = (session.query(AuctionLeagueSave.id)
              .filter(AuctionLeagueSave.user_tg_id == row.user_tg_id,
                      AuctionLeagueSave.id != row.id,
                      AuctionLeagueSave.reward_paid_at.isnot(None),
                      AuctionLeagueSave.reward_paid_at >= since)
              .first())
    row.reward_paid = True
    if recent is not None:
        return 0, 0, (f"one paid season every {REWARD_COOLDOWN_HOURS} hours")
    row.reward_paid_at = now
    user.total_coins = (user.total_coins or 0) + coins
    user.total_gems = (user.total_gems or 0) + gems
    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "auction_league_reward",
                     f"Auction League {season_finish(state)}: +{coins} coins, +{gems} gems",
                     coins_change=coins, gems_change=gems)
    except Exception:
        logger.exception("auction league: could not log the reward")
    return coins, gems, ""
