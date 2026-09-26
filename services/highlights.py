"""Match highlights reel — the handful of moments that decided a match.

While a /cipl or /letsplay match is played, :func:`record_over` is called at
the end of every over and logs the over's key moments into
``state["highlight_log"]``: wickets (weighted by how set the batter was),
fifties and hundreds, bowling hauls, sixes, big overs, maidens, and the swings
in the chase's win probability. Each moment carries a weight.

When the match ends, :func:`build_reel` picks the heaviest few (no more than
two of any one kind, so a six-fest doesn't crowd out the wickets), puts them
back in match order, and names the heaviest one the *moment of the match*.

Pure: no Telegram, no database. The log lives in the match state, which is
already persisted every over.
"""

import html

LOG_KEY = "highlight_log"
MAX_LOG = 80
REEL_SIZE = 5
# Fewer logged moments than this is not a reel (a match replayed from an old
# state, or one abandoned early) — nothing is posted.
MIN_MOMENTS = 3
MAX_PER_KIND = 2

W_WICKET = 30
W_SIX = 12
W_FIFTY = 35
W_HUNDRED = 60
W_THREE_FER = 30
W_FIVE_FER = 50
W_MAIDEN = 15
W_BIG_OVER = 20          # + the over's runs, for an over of BIG_OVER_RUNS or more
BIG_OVER_RUNS = 18
W_SWING_MIN = 15         # a chase swing of at least this many % points is a moment
W_WINNING_HIT = 45

_ICONS = {"wicket": "☝️", "six": "💥", "fifty": "🏏", "hundred": "💯",
          "haul": "🎯", "maiden": "🧱", "big_over": "🚀", "swing": "📈",
          "winning_hit": "🏆"}


def snapshot(state):
    """What :func:`record_over` needs from the state *before* an over."""
    bats = {}
    for rid, st in (state.get("bat_stats") or {}).items():
        bats[str(rid)] = (int(st.get("runs") or 0), bool(st.get("out")))
    bowl = {str(rid): int(st.get("wickets") or 0)
            for rid, st in (state.get("bowl_stats") or {}).items()}
    return {"bats": bats, "bowl": bowl}


def _names(state):
    out = {}
    for p in state.get("batting_order") or state.get("bat_xi") or []:
        rid = p.get("roster_id")
        if rid is not None:
            out[str(rid)] = p.get("name") or "Batter"
    return out


def _log(state, entry):
    log = state.setdefault(LOG_KEY, [])
    log.append(entry)
    if len(log) > MAX_LOG:
        # Keep the heaviest, in match order.
        keep = sorted(range(len(log)), key=lambda i: -log[i]["w"])[:MAX_LOG]
        state[LOG_KEY] = [log[i] for i in sorted(keep)]


def record_over(state, before, *, over_no, bowler_name, over_runs, over_wkts,
                timeline, legal_balls, bpu=6):
    """Log the key moments of the over just bowled. Never raises."""
    try:
        _record_over(state, before, over_no, bowler_name, over_runs, over_wkts,
                     timeline, legal_balls, bpu)
    except Exception:
        pass


def _record_over(state, before, over_no, bowler_name, over_runs, over_wkts,
                 timeline, legal_balls, bpu):
    inn = int(state.get("innings") or 1)
    team = state.get("bat_team_name") or ""
    names = _names(state)
    base = {"inn": inn, "over": int(over_no), "team": team}
    bowler = bowler_name or "the bowler"
    bats_before = (before or {}).get("bats") or {}

    for rid, st in (state.get("bat_stats") or {}).items():
        rid = str(rid)
        runs = int(st.get("runs") or 0)
        balls = int(st.get("balls") or 0)
        prev_runs, prev_out = bats_before.get(rid, (0, False))
        name = names.get(rid) or st.get("name") or "Batter"
        if st.get("out") and not prev_out:
            how = st.get("dismissal") or st.get("how_out") or "out"
            weight = W_WICKET + min(40, runs // 2) + (10 if runs >= 50 else 0)
            _log(state, dict(base, kind="wicket", w=weight,
                             text=f"{name} falls for {runs} ({balls}) — {how}"))
        for mark, kind, weight in ((100, "hundred", W_HUNDRED), (50, "fifty", W_FIFTY)):
            if prev_runs < mark <= runs:
                _log(state, dict(base, kind=kind, w=weight,
                                 text=f"{name} brings up {mark} — {runs} off {balls}"))
                break

    bowl_before = (before or {}).get("bowl") or {}
    bowl_names = {str(p.get("roster_id")): p.get("name")
                  for p in state.get("bowl_xi") or [] if p.get("roster_id") is not None}
    for rid, st in (state.get("bowl_stats") or {}).items():
        wk = int(st.get("wickets") or 0)
        prev = bowl_before.get(str(rid), 0)
        for mark, weight in ((5, W_FIVE_FER), (3, W_THREE_FER)):
            if prev < mark <= wk:
                _log(state, dict(base, kind="haul", w=weight,
                                 text=f"{bowl_names.get(str(rid)) or bowler} takes "
                                      f"{wk} wickets ({wk}/{int(st.get('runs') or 0)})"))
                break

    sixes = sum(1 for s in (timeline or []) if str(s) == "6")
    if sixes:
        extra = f" — {sixes} in the over" if sixes > 1 else ""
        _log(state, dict(base, kind="six", w=W_SIX + 6 * (sixes - 1),
                         text=f"SIX{'ES' if sixes > 1 else ''} off {bowler}{extra}"))
    if over_runs >= BIG_OVER_RUNS:
        _log(state, dict(base, kind="big_over", w=W_BIG_OVER + over_runs,
                         text=f"{over_runs} runs off {bowler}'s over"))
    elif over_runs == 0 and legal_balls >= bpu and not over_wkts:
        _log(state, dict(base, kind="maiden", w=W_MAIDEN,
                         text=f"{bowler} bowls a maiden"))

    hist = state.get("chase_history") or []
    if inn == 2 and len(hist) >= 2 and hist[-1].get("over") == over_no:
        a, b = int(hist[-2].get("chasing") or 0), int(hist[-1].get("chasing") or 0)
        if abs(b - a) >= W_SWING_MIN and 0 < b < 100:
            who = team if b > a else (state.get("bowl_team_name") or "the bowlers")
            _log(state, dict(base, kind="swing", w=abs(b - a),
                             text=f"Momentum to {who} — chase odds {a}% → {b}%"))


def mark_winning_hit(state, result):
    """Log the final act of a successful chase (call once at match end)."""
    try:
        if not result or result.get("tie") or result.get("margin_type") != "wickets":
            return
        if not state.get(LOG_KEY):
            return   # no over was logged — nothing to crown
        _log(state, {"inn": 2, "over": int(state.get("current_over") or 0),
                     "team": state.get("bat_team_name") or "", "kind": "winning_hit",
                     "w": W_WINNING_HIT,
                     "text": f"{state.get('bat_team_name') or 'The chasers'} "
                             f"get home with {result.get('margin')} wicket(s) in hand"})
    except Exception:
        pass


def build_reel(state, size=REEL_SIZE):
    """``(moments in match order, moment of the match)`` or ``([], None)``."""
    log = list((state or {}).get(LOG_KEY) or [])
    if len(log) < MIN_MOMENTS:
        return [], None
    ranked = sorted(enumerate(log), key=lambda t: (-t[1]["w"], t[0]))
    chosen, per_kind = [], {}
    for idx, m in ranked:
        k = m.get("kind")
        if per_kind.get(k, 0) >= MAX_PER_KIND:
            continue
        chosen.append((idx, m))
        per_kind[k] = per_kind.get(k, 0) + 1
        if len(chosen) >= size:
            break
    if not chosen:
        return [], None
    top = chosen[0][1]
    chosen.sort(key=lambda t: t[0])
    return [m for _i, m in chosen], top


def render_reel(state, title_teams=None):
    """The HTML highlights message, or "" when the match left no moments."""
    moments, top = build_reel(state)
    if not moments:
        return ""
    unit = "Set" if state.get("ball_format") == "The100" else "Ov"
    lines = ["🎬 <b>MATCH HIGHLIGHTS</b>"]
    if title_teams:
        lines.append(f"<i>{html.escape(title_teams)}</i>")
    lines.append("━━━━━━━━━━━━━━━")
    for m in moments:
        icon = _ICONS.get(m.get("kind"), "•")
        lines.append(f"{icon} <b>Inn {m['inn']} · {unit} {m['over']}</b> — "
                     f"{html.escape(str(m.get('text', '')), quote=False)}")
    lines.append("━━━━━━━━━━━━━━━")
    lines.append(f"⭐ <b>Moment of the match:</b> {html.escape(str(top.get('text', '')), quote=False)}")
    return "\n".join(lines)
