"""Bowl-out service — tiebreaker mechanic.

Each team picks 5 bowlers from their XI. They alternate bowling at unguarded
stumps (1 delivery each). After 5 each, if tied, sudden death continues
one ball each until someone misses while the other hits.

Hit probability is bowler-rating-weighted:
  p_hit = 0.30 + (bowl_rating / 200)
  →  50 OVR ≈ 55%
  →  70 OVR ≈ 65%
  →  85 OVR ≈ 72.5%
  → 100 OVR ≈ 80%

State machine on Bowlout:
  pending_pick → bowling → completed
  current_picker switches between user1 → user2 → user1 → ...
  Once 10 picks total (5 each), status flips to bowling.
"""

import random
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def create_bowlout(session, *, match_id=None, chat_id, user1_id, user2_id,
                    user1_team_name="Team 1", user2_team_name="Team 2",
                    first_picker_user_id=None):
    """Create a new Bowlout. user1 is who bowls first (and picks first)."""
    from models import Bowlout
    bo = Bowlout(
        match_id=match_id, chat_id=chat_id,
        user1_id=user1_id, user2_id=user2_id,
        user1_team_name=user1_team_name, user2_team_name=user2_team_name,
        status="pending_pick",
        current_picker=first_picker_user_id or user1_id,
        current_ball=0,
    )
    session.add(bo)
    session.flush()
    return bo


def count_picks(session, bowlout_id, user_id):
    """Count how many bowlers a user has already picked."""
    from models import BowloutBall
    return (session.query(BowloutBall)
                   .filter(BowloutBall.bowlout_id == bowlout_id,
                           BowloutBall.bowler_user_id == user_id)
                   .count())


def record_pick(session, bowlout_id, user_id, player_id, player_name, bowl_rating):
    """Record a bowler pick. Doesn't roll dice yet — that happens when
    everyone has picked.

    Returns the ball_index assigned (0..9 for 5 each), or -1 if user already
    has 5 picks.
    """
    from models import Bowlout, BowloutBall
    bo = session.query(Bowlout).get(bowlout_id)
    if not bo:
        return -1

    existing = count_picks(session, bowlout_id, user_id)
    if existing >= 5:
        return -1

    # ball_index: user1's nth pick → 2*n, user2's nth pick → 2*n+1
    if user_id == bo.user1_id:
        ball_index = existing * 2
    else:
        ball_index = existing * 2 + 1

    ball = BowloutBall(
        bowlout_id=bowlout_id, ball_index=ball_index,
        bowler_user_id=user_id, bowler_player_id=player_id,
        bowler_name=player_name, bowler_rating=bowl_rating,
        is_hit=False,
    )
    session.add(ball)
    session.flush()
    return ball_index


def advance_picker(session, bowlout_id):
    """Switch to the other user as picker, or mark ready to bowl if all done."""
    from models import Bowlout
    bo = session.query(Bowlout).get(bowlout_id)
    if not bo:
        return None
    u1 = count_picks(session, bowlout_id, bo.user1_id)
    u2 = count_picks(session, bowlout_id, bo.user2_id)
    if u1 >= 5 and u2 >= 5:
        bo.status = "bowling"
        bo.current_picker = None
    else:
        # Alternate: if equal counts, user1 picks next; else fewer picks goes
        bo.current_picker = bo.user1_id if u1 <= u2 else bo.user2_id
    session.flush()
    return bo


def roll_hit(bowl_rating):
    """Roll a single bowl-out delivery. Returns True if hit."""
    p = 0.30 + max(0, min(100, bowl_rating or 70)) / 200.0
    return random.random() < p


def bowl_next(session, bowlout_id):
    """Advance the bowl-out by one delivery. Returns the BowloutBall row
    that was just bowled (with .is_hit set), or None if nothing left to bowl.

    Updates the parent Bowlout's hit counters and ball pointer.
    """
    from models import Bowlout, BowloutBall
    bo = session.query(Bowlout).get(bowlout_id)
    if not bo or bo.status != "bowling":
        return None

    # Find the lowest ball_index not yet bowled
    balls = (session.query(BowloutBall)
                    .filter(BowloutBall.bowlout_id == bowlout_id)
                    .order_by(BowloutBall.ball_index)
                    .all())
    if not balls:
        return None

    # We mark is_hit at bowl time. Use bowled_at to detect already-bowled
    # actually we need a clearer flag — use a sentinel: bowled balls have a
    # "is_resolved" flag implicitly via... we don't have one. Let me track
    # by current_ball pointer on Bowlout.

    if bo.current_ball >= len(balls):
        # All balls already bowled — should be moving to result/sudden death
        return None

    ball = balls[bo.current_ball]
    ball.is_hit = roll_hit(ball.bowler_rating)
    ball.bowled_at = datetime.utcnow()

    if ball.bowler_user_id == bo.user1_id:
        if ball.is_hit:
            bo.user1_hits += 1
    else:
        if ball.is_hit:
            bo.user2_hits += 1

    bo.current_ball += 1
    session.flush()
    return ball


def is_match_decided_after(session, bowlout_id):
    """After the most-recent ball is bowled, can we declare a winner?

    During the first 10 balls (5 from each): only after all 10 are done.
    Sudden death: after each *pair* of deliveries (one from each side),
    if hits differ, winner declared.

    Returns winner user_id, or None if undecided / needs more balls.
    """
    from models import Bowlout, BowloutBall
    bo = session.query(Bowlout).get(bowlout_id)
    if not bo:
        return None

    total_balls_bowled = bo.current_ball
    if total_balls_bowled < 10:
        return None  # not done with the main 5-each yet

    if total_balls_bowled == 10:
        if bo.user1_hits != bo.user2_hits:
            return bo.user1_id if bo.user1_hits > bo.user2_hits else bo.user2_id
        return None  # tied → sudden death required

    # Sudden death — pairs at indices 10/11, 12/13, ...
    # Decided after each even-indexed final ball if hits differ
    if total_balls_bowled % 2 == 0:
        if bo.user1_hits != bo.user2_hits:
            return bo.user1_id if bo.user1_hits > bo.user2_hits else bo.user2_id
    return None


def queue_sudden_death(session, bowlout_id, user1_pick, user2_pick):
    """Add one sudden-death pair (user1 + user2 each pick a bowler).

    user{1,2}_pick: dict with player_id, name, bowl_rating, user_id.
    Returns the two new ball_index values.
    """
    from models import Bowlout, BowloutBall
    bo = session.query(Bowlout).get(bowlout_id)
    if not bo:
        return None

    # next ball_index = bo.current_ball (since we increment after bowl)
    next_idx_u1 = bo.current_ball
    next_idx_u2 = bo.current_ball + 1

    session.add(BowloutBall(
        bowlout_id=bowlout_id, ball_index=next_idx_u1,
        bowler_user_id=bo.user1_id,
        bowler_player_id=user1_pick.get("player_id"),
        bowler_name=user1_pick["name"],
        bowler_rating=user1_pick.get("bowl_rating", 70),
    ))
    session.add(BowloutBall(
        bowlout_id=bowlout_id, ball_index=next_idx_u2,
        bowler_user_id=bo.user2_id,
        bowler_player_id=user2_pick.get("player_id"),
        bowler_name=user2_pick["name"],
        bowler_rating=user2_pick.get("bowl_rating", 70),
    ))
    session.flush()
    return next_idx_u1, next_idx_u2


def render_scoreboard(session, bowlout_id, max_pairs=5):
    """Render the bowl-out scoreboard text as in the user's spec:
      Team1 4 | 2 Team2

      🟩 Bowler1A | 🟥 Bowler2A
      🟩 Bowler1B | 🟩 Bowler2B
      ...

    Each row shows one pair of deliveries. Rows for pairs not yet bowled
    show the bowler names with ⚪ for "not yet bowled".
    """
    from models import Bowlout, BowloutBall
    bo = session.query(Bowlout).get(bowlout_id)
    if not bo:
        return ""

    balls = (session.query(BowloutBall)
                    .filter(BowloutBall.bowlout_id == bowlout_id)
                    .order_by(BowloutBall.ball_index).all())

    # Group by pair
    pairs = []
    by_index = {b.ball_index: b for b in balls}
    n_pairs = max(max_pairs, (len(balls) + 1) // 2)
    for pair_n in range(n_pairs):
        u1_ball = by_index.get(pair_n * 2)
        u2_ball = by_index.get(pair_n * 2 + 1)
        pairs.append((u1_ball, u2_ball))

    def render_side(ball, bowled_idx):
        if ball is None:
            return "⚫ (not picked)"
        # Has it actually been bowled yet?
        is_bowled = bowled_idx > ball.ball_index
        if not is_bowled:
            return f"⚪ {ball.bowler_name}"
        emoji = "🟩" if ball.is_hit else "🟥"
        return f"{emoji} {ball.bowler_name}"

    lines = []
    for u1_b, u2_b in pairs:
        left = render_side(u1_b, bo.current_ball)
        right = render_side(u2_b, bo.current_ball)
        lines.append(f"{left}  |  {right}")

    header = (f"<b>{bo.user1_team_name}</b> {bo.user1_hits}  —  "
              f"{bo.user2_hits} <b>{bo.user2_team_name}</b>")
    return header + "\n\n" + "\n".join(lines)
