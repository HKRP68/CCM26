"""SQLAlchemy ORM models."""

from datetime import datetime, timedelta
from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, Boolean, DateTime, ForeignKey, Index,
    LargeBinary, Text, UniqueConstraint, text
)
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(100), nullable=True)
    first_name = Column(String(100), nullable=True)
    team_name = Column(String(50), nullable=True)
    total_coins = Column(Integer, default=0)
    total_gems = Column(Integer, default=0)
    roster_count = Column(Integer, default=0)
    captain_roster_id = Column(Integer, nullable=True)
    # Short unique code used by friends to redeem a referral via /debut prompt
    # or /redeem <code>. Auto-generated on first /invite or first request that
    # needs it. 6 chars uppercase alphanumeric, ~1 billion possibilities.
    referral_code = Column(String(12), nullable=True, unique=True, index=True)
    matches_played = Column(Integer, default=0)
    matches_won = Column(Integer, default=0)
    matches_lost = Column(Integer, default=0)
    win_streak = Column(Integer, default=0)
    # ── Monthly season ──
    # season_points accumulate during the current season and reset at rollover.
    # season_key is the season the points belong to ('YYYY-MM'); if it differs
    # from the live season, the points are treated as 0 (stale, pre-reset).
    season_points = Column(Integer, default=0)
    season_key = Column(String(7), nullable=True)  # 'YYYY-MM'
    season_wins = Column(Integer, default=0)
    # Club membership (one club per user). NULL = not in a club.
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True, index=True)
    club_joined_at = Column(DateTime, nullable=True)
    last_club_leave = Column(DateTime, nullable=True)  # for join cooldown
    best_streak = Column(Integer, default=0)
    active_days = Column(Integer, default=0)  # days with at least 1 match
    last_match_date = Column(DateTime, nullable=True)
    # Set the first time the user arranges their own batting order (/setbo,
    # /sbo or the Mini App XI reorder). While NULL, modes that auto-build a
    # line-up (e.g. /letsplay) sort the XI by batting rating high → low; once
    # set, the roster's own order_position 1-11 IS the batting order and is
    # used verbatim. /setbo auto clears it back to NULL.
    batting_order_set_at = Column(DateTime, nullable=True)
    # Quick Match counters — kept SEPARATE from matches_played/won/lost so
    # Quick Match (casual practice mode) doesn't pollute the global
    # leaderboard, player stats, or career win/loss records.
    quick_matches_played = Column(Integer, default=0)
    quick_matches_won = Column(Integer, default=0)
    quick_matches_lost = Column(Integer, default=0)
    # Daily limit tracking. Reset to 0 when quick_matches_today_date != today.
    quick_matches_today = Column(Integer, default=0)
    quick_matches_today_date = Column(String(10), nullable=True)  # 'YYYY-MM-DD'
    quest_points = Column(Integer, default=0)  # earned from completing quests
    # AI-match quest allowance. Matches against the bot (/wpmbot, /vsbot,
    # /lpbot, /ciplbot) DO complete quests, but only the first N each UTC day —
    # see services.quest_service.consume_bot_quest_allowance, which resets the
    # counter lazily when bot_quest_matches_date is not today.
    bot_quest_matches_today = Column(Integer, default=0)
    bot_quest_matches_date = Column(String(10), nullable=True)  # 'YYYY-MM-DD'
    # Pack pity timer — increments on low rolls, resets on a max-rating roll.
    # When ≥ PITY_THRESHOLD, the next pack guarantees max-rating from the band.
    pack_pity_counter = Column(Integer, default=0)
    # Last group/supergroup chat the user opened the Mini App from (negative id).
    # Captured from the launch deep link's start_param so Mini App activities
    # (opening packs, GSpin, buying players, daily reward) can echo back into
    # that group even when a later launch carries no origin param.
    last_miniapp_chat_id = Column(BigInteger, nullable=True)
    # Per-user notification opt-out. When False, the bot stops sending
    # cooldown-ready DM nudges, scheduled FOMO push messages, and echoing
    # this user's Mini App actions into groups. Toggled via /notifications.
    notifications_enabled = Column(Boolean, default=True, nullable=False)
    # Set when Telegram answers 403 (the user blocked the bot). Every
    # outbound DM job skips these until the user talks to the bot again.
    dm_blocked = Column(Boolean, default=False, nullable=False)
    # Last time the user sent the bot anything at all (command, button, text).
    # Written by a throttled middleware in bot.py (at most once an hour per
    # user) — the comeback job reads it to decide who has gone quiet.
    last_seen_at = Column(DateTime, nullable=True)
    # ── Guided first session (services/onboarding_service.py) ──
    # onboarding_started_at is stamped by /debut. NULL means the account
    # predates the journey, so it is never shown the checklist.
    # onboarding_steps is the comma-separated list of step keys already
    # completed (and paid); onboarding_done_at is stamped when all are done.
    onboarding_started_at = Column(DateTime, nullable=True)
    onboarding_steps = Column(String(300), nullable=True)
    onboarding_done_at = Column(DateTime, nullable=True)
    # ── Comeback nudges (services/comeback_service.py) ──
    # comeback_tier is the highest inactivity tier already DMed during the
    # current absence (0 = none); it resets once the user is seen again.
    # comeback_claim_tier is the tier whose reward is waiting to be claimed.
    comeback_tier = Column(Integer, default=0, nullable=False)
    comeback_sent_at = Column(DateTime, nullable=True)
    comeback_claim_tier = Column(Integer, nullable=True)
    # ── Paid subscription (manually granted by an admin from the website) ──
    # 'none' = free user. 'bronze'/'silver'/'platinum'/'diamond' = the paid
    # tiers declared in config.SUBSCRIPTION_TIERS (cheapest → richest). Access is
    # time-boxed: a tier is only "active" while subscription_expires_at is in the
    # future — see services/subscription_service.get_tier(), which treats an
    # expired tier as 'none' everywhere without needing a background job.
    subscription_tier = Column(String(20), default="none", nullable=False)
    subscription_expires_at = Column(DateTime, nullable=True)
    subscription_activated_at = Column(DateTime, nullable=True)
    # ── Career Player weekly-quest streak (see services/career_service.py) ──
    # Consecutive ISO weeks in which the user completed EVERY career weekly quest
    # assigned to them. Evaluated lazily once per week at quest rollover;
    # career_weekly_last_period is the last week already judged, which is what
    # makes that evaluation idempotent.
    career_weekly_streak = Column(Integer, default=0, nullable=False)
    career_weekly_best_streak = Column(Integer, default=0, nullable=False)
    career_weekly_last_period = Column(String(10), nullable=True)  # 'YYYY-Wnn'
    # The team's approved crest, drawn on the scorecards. Only an *approved*
    # logo lands here: a submission waiting on review lives in
    # team_logo_requests and changes nothing until an admin says yes.
    # asset_key addresses the normalised PNG in stored_assets (the host
    # filesystem is wiped on deploy); file_id is Telegram's own id for the same
    # image, so a DM or /myteam can re-send it without a render.
    team_logo_asset_key = Column(String(300), nullable=True)
    team_logo_file_id = Column(String(200), nullable=True)
    team_logo_updated_at = Column(DateTime, nullable=True)
    # The team's own colour on the scorecards — its header bar, crest panel and
    # not-out scores. '#rrggbb'; NULL means the admin's per-innings default.
    # Unlike the crest this needs no review: a hex code carries nothing to
    # moderate, and the card picks readable text for whatever is chosen.
    team_colour = Column(String(9), nullable=True)
    # Ban / disable — banned users are refused by the bot's middleware
    is_banned = Column(Boolean, default=False, nullable=False)
    ban_reason = Column(String(500), nullable=True)
    banned_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    roster = relationship("UserRoster", back_populates="user", cascade="all, delete-orphan")
    stats = relationship("UserStats", back_populates="user", uselist=False, cascade="all, delete-orphan")


class Player(Base):
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # name no longer unique — multiple versions can share a name
    name = Column(String(150), nullable=False, index=True)
    version = Column(String(50), default="Base")  # 'Base', 'Gold', 'World Cup 2023', etc.
    # NULL for base cards. Set to base player's id for variant versions.
    parent_player_id = Column(Integer, ForeignKey("players.id"), nullable=True, index=True)
    rating = Column(Integer, nullable=False)
    category = Column(String(30), nullable=False)
    country = Column(String(60), nullable=False)
    bat_hand = Column(String(10), nullable=False)
    bowl_hand = Column(String(10), nullable=False)
    bowl_style = Column(String(30), nullable=False)
    bat_rating = Column(Integer, default=0)
    bowl_rating = Column(Integer, default=0)
    # Career stats kept in schema but seeded to 0 — real stats are in PlayerGameStats
    bat_avg = Column(Float, default=0.0)
    strike_rate = Column(Float, default=0.0)
    runs = Column(Integer, default=0)
    centuries = Column(Integer, default=0)
    bowl_avg = Column(Float, default=0.0)
    economy = Column(Float, default=0.0)
    wickets = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    # When True, blocks /buypl direct-name purchase. Player remains
    # available via player market, packs, trades, debut grants, etc.
    restricted_from_buypl = Column(Boolean, default=False, nullable=False)
    image_url = Column(String(500), nullable=True)
    # Admin /setcardid manual pin — a photo file_id the admin explicitly pinned
    # for this player. Honoured directly by card_sender (never auto-written).
    card_file_id = Column(String(200), nullable=True)
    # Telegram file_id of the last auto-generated/template card. Persists the
    # in-process generated-card cache across restarts so the card commands skip
    # the Pillow re-render + photo re-upload. Distinct from card_file_id above;
    # cleared whenever the player or the card template changes.
    gen_card_file_id = Column(String(200), nullable=True)
    # ── Career Player ──────────────────────────────────────────────────
    # A career card belongs to exactly one user (/cmucareer). It is a normal
    # players row so it plays, scores and renders like any other card, but it
    # must never be dealt from a shared pool, sold or traded — see
    # services/career_service.py and services/player_service.not_career().
    is_career = Column(Boolean, default=False, nullable=False, index=True)
    career_owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    career_face = Column(String(30), nullable=True)  # card template variant, e.g. 'career_1'
    # Honoured dynamically by services/rating_matcher_service.is_player_non_tradable,
    # which already probes for this attribute — setting it excludes the card from
    # every rating-matched trade path.
    non_tradable = Column(Boolean, default=False, nullable=False)
    # The ten career attributes. All start at CAREER_START (78) so a fresh career
    # player is exactly 78 Bat / 78 Bowl / 78 OVR; each is upgraded with gems and
    # the three ratings above are recomputed from them.
    attr_technique = Column(Integer, default=78, nullable=False)
    attr_power = Column(Integer, default=78, nullable=False)
    attr_timing = Column(Integer, default=78, nullable=False)
    attr_footwork = Column(Integer, default=78, nullable=False)
    attr_composure = Column(Integer, default=78, nullable=False)
    attr_pace = Column(Integer, default=78, nullable=False)
    attr_accuracy = Column(Integer, default=78, nullable=False)
    attr_swing = Column(Integer, default=78, nullable=False)
    attr_stamina = Column(Integer, default=78, nullable=False)
    attr_variation = Column(Integer, default=78, nullable=False)
    # ── Name / country changes (see services/career_change_service.py) ──
    # How many identity changes this card has consumed. The first is free and
    # every one after it costs gems on a rising ladder, so this counter is what
    # the price is read off. career_free_changes are admin-granted extras that
    # are spent before the ladder and never advance it.
    career_changes_used = Column(Integer, default=0, nullable=False)
    career_free_changes = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_players_rating", "rating"),
        Index("ix_players_parent", "parent_player_id"),
        # One career card per user, enforced by the database. The service layer
        # checks first, but two concurrent /cmucareer taps can both pass that
        # check and both commit; this partial index is what actually stops the
        # second one. Partial so the millions of catalogue rows — all
        # is_career=False with a NULL owner — are not indexed at all.
        Index("uq_players_career_owner", "career_owner_user_id", unique=True,
              sqlite_where=text("is_career = 1"),
              postgresql_where=text("is_career")),
    )


class UserRoster(Base):
    __tablename__ = "user_roster"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    order_position = Column(Integer, default=99)
    acquired_date = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="roster")
    player = relationship("Player")

    __table_args__ = (Index("ix_user_roster_user", "user_id"),)


class UserStats(Base):
    __tablename__ = "user_stats"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    last_claim = Column(DateTime, nullable=True)
    last_daily = Column(DateTime, nullable=True)
    last_gspin = Column(DateTime, nullable=True)
    # /ximage Playing XI image render (1h cooldown)
    last_ximage = Column(DateTime, nullable=True)
    # /sim auto-simulated match (4h cooldown). A challenge sim spends this for
    # BOTH players, so it also gates who can be challenged.
    last_sim = Column(DateTime, nullable=True)
    # Free Pack (Mini App, ad-gated, 1h cooldown)
    last_free_pack = Column(DateTime, nullable=True)
    # ── Subscription recurring-drop cooldowns ──
    # /cmumysterybox (tiered: Silver every 8 days, Platinum every 4),
    # /cmuweekly (Platinum weekly card), /cmuchest (Platinum coin chests).
    last_mysterybox = Column(DateTime, nullable=True)
    last_weekly = Column(DateTime, nullable=True)
    # /cmuchest quota cycle: last_coinchest is the current cycle's start; up to
    # coin_chests.count chests may be opened (one per /cmuchest) per cycle.
    last_coinchest = Column(DateTime, nullable=True)
    coinchest_used = Column(Integer, default=0, nullable=False)
    # Cooldown-ready notification flags. Set True once we've notified the user
    # their cooldown is up, so the background job doesn't spam them every tick.
    # Reset to False when the user performs the action (consuming the cooldown).
    notified_daily_ready = Column(Boolean, default=False, nullable=False)
    notified_gspin_ready = Column(Boolean, default=False, nullable=False)
    notified_claim_ready = Column(Boolean, default=False, nullable=False)
    notified_free_pack_ready = Column(Boolean, default=False, nullable=False)
    streak_count = Column(Integer, default=0)
    total_streaks_completed = Column(Integer, default=0)
    last_streak_reset = Column(DateTime, nullable=True)
    # ── Login streak ladder (separate from the /daily milestone streak) ──
    # Increments once per calendar day (UTC) the user opens the app.
    # Drives a 7-day reward calendar that loops. login_streak_day is the
    # 1-based position in the current ladder cycle (1..7).
    login_streak = Column(Integer, default=0)            # consecutive days
    login_best_streak = Column(Integer, default=0)
    last_login_date = Column(String(10), nullable=True)  # 'YYYY-MM-DD'
    login_reward_claimed_date = Column(String(10), nullable=True)  # last claim day
    # ── Multi-use spin/daily quota (24h rolling cycle) ──
    # When the user first spins/claims-daily, we record the cycle start.
    # During each cycle: 1 free use + N ad-gated uses. Cycle resets after 24h.
    spin_cycle_started_at = Column(DateTime, nullable=True)
    spin_free_used = Column(Boolean, default=False, nullable=False)
    spin_ad_count = Column(Integer, default=0, nullable=False)
    daily_cycle_started_at = Column(DateTime, nullable=True)
    daily_free_used = Column(Boolean, default=False, nullable=False)
    daily_ad_count = Column(Integer, default=0, nullable=False)
    # Ad-gated slots taken this cycle WITHOUT an ad, because the network had
    # none to serve (see services.adsgram_service "No-fill passes"). Counted
    # separately from spin_ad_count so the grace can be capped on its own.
    spin_nofill_used = Column(Integer, default=0, nullable=False)
    daily_nofill_used = Column(Integer, default=0, nullable=False)
    # When the last AD-GATED spin/daily was taken. The next one is locked until
    # GameConfig.ad_reward_gap_minutes have passed — rewarded ads are spaced
    # out rather than watchable back to back. Independent of the quota cycle,
    # and never applied to the free use.
    spin_last_ad_at = Column(DateTime, nullable=True)
    daily_last_ad_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="stats")


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    initiator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    initiator_player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    receiver_player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    initiator_roster_id = Column(Integer, ForeignKey("user_roster.id"), nullable=True)
    receiver_roster_id = Column(Integer, ForeignKey("user_roster.id"), nullable=True)
    status = Column(String(20), default="pending", nullable=False)
    trade_fee = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    initiator = relationship("User", foreign_keys=[initiator_id])
    receiver = relationship("User", foreign_keys=[receiver_id])
    initiator_player = relationship("Player", foreign_keys=[initiator_player_id])
    receiver_player = relationship("Player", foreign_keys=[receiver_player_id])

    __table_args__ = (
        Index("ix_trades_status", "status"),
        Index("ix_trades_initiator", "initiator_id"),
        Index("ix_trades_receiver", "receiver_id"),
    )


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    action = Column(String(50), nullable=False)
    detail = Column(String(500), nullable=True)
    coins_change = Column(Integer, default=0)
    gems_change = Column(Integer, default=0)
    player_name = Column(String(150), nullable=True)
    player_rating = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")

    __table_args__ = (
        Index("ix_activity_user", "user_id"),
        Index("ix_activity_action", "action"),
        Index("ix_activity_time", "created_at"),
    )


class PlayerGameStats(Base):
    """Per-player-per-owner game stats. Created when a player plays for a team."""
    __tablename__ = "player_game_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)

    # Awards
    potm = Column(Integer, default=0)  # Player of the Match count

    # Batting
    bat_inns = Column(Integer, default=0)
    runs = Column(Integer, default=0)
    fifties = Column(Integer, default=0)
    hundreds = Column(Integer, default=0)
    fours = Column(Integer, default=0)
    sixes = Column(Integer, default=0)
    balls_faced = Column(Integer, default=0)
    times_out = Column(Integer, default=0)
    ducks = Column(Integer, default=0)
    highest_score = Column(Integer, default=0)
    highest_score_not_out = Column(Boolean, default=False)

    # Bowling
    bowl_inns = Column(Integer, default=0)
    wickets_taken = Column(Integer, default=0)
    runs_conceded = Column(Integer, default=0)
    overs_bowled = Column(Float, default=0.0)
    balls_bowled = Column(Integer, default=0)
    three_fers = Column(Integer, default=0)
    five_fers = Column(Integer, default=0)
    hattricks = Column(Integer, default=0)
    best_bowl_wickets = Column(Integer, default=0)
    best_bowl_runs = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    player = relationship("Player")

    __table_args__ = (
        Index("ix_pgs_user_player", "user_id", "player_id", unique=True),
        # /gstats filters on player_id alone (every owner's rows for one card).
        # The composite above leads with user_id, which most engines cannot use
        # for that predicate, so a widely owned card would scan the table.
        Index("ix_pgs_player", "player_id"),
    )

    @property
    def bat_avg(self):
        return round(self.runs / self.times_out, 2) if self.times_out else 0.0

    @property
    def bat_sr(self):
        return round((self.runs / self.balls_faced) * 100, 2) if self.balls_faced else 0.0

    @property
    def bowl_avg(self):
        return round(self.runs_conceded / self.wickets_taken, 2) if self.wickets_taken else 0.0

    @property
    def bowl_economy(self):
        return round(self.runs_conceded / (self.overs_bowled or 1), 2) if self.overs_bowled else 0.0

    @property
    def bowl_sr(self):
        return round(self.balls_bowled / self.wickets_taken, 2) if self.wickets_taken else 0.0

    @property
    def hs_str(self):
        if self.highest_score == 0 and self.bat_inns == 0:
            return "-"
        no = "*" if self.highest_score_not_out else ""
        return f"{self.highest_score}{no}"

    @property
    def bbf_str(self):
        if self.best_bowl_wickets == 0 and self.bowl_inns == 0:
            return "-"
        return f"{self.best_bowl_wickets}/{self.best_bowl_runs}"

class Match(Base):
    """Tracks a match between two users."""
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user1_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user2_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String(30), default="pending")
    # Which game mode created this row — /cric, /vsbot, CIPL, … See
    # ``services.match_outcome.MATCH_TYPE_LABELS``. NULL on rows written before
    # the column existed; the admin views infer a coarse label for those.
    match_type = Column(String(30), nullable=True)
    overs = Column(Integer, default=20)
    toss_winner_id = Column(Integer, nullable=True)
    toss_decision = Column(String(10), nullable=True)
    batting_first_id = Column(Integer, nullable=True)
    bowling_first_id = Column(Integer, nullable=True)
    stadium = Column(String(100), nullable=True)
    pitch_type = Column(String(30), nullable=True)
    weather = Column(String(30), nullable=True)
    temperature = Column(Integer, nullable=True)
    umpire1 = Column(String(60), nullable=True)
    umpire2 = Column(String(60), nullable=True)
    chat_id = Column(BigInteger, nullable=True)
    # Result fields
    winner_id = Column(Integer, nullable=True)
    loser_id = Column(Integer, nullable=True)
    margin_type = Column(String(20), nullable=True)  # "runs" or "wickets"
    margin_value = Column(Integer, nullable=True)
    result_message_id = Column(BigInteger, nullable=True)  # telegram msg id for /jump
    inn1_runs = Column(Integer, nullable=True)
    inn1_wickets = Column(Integer, nullable=True)
    inn2_runs = Column(Integer, nullable=True)
    inn2_wickets = Column(Integer, nullable=True)
    potm_player_id = Column(Integer, nullable=True)
    potm_impact = Column(Integer, nullable=True)
    # Set when this match is played through a Challenge League Tournament command.
    # NULL for regular Challenge League / casual matches, which must never affect
    # tournament statistics.
    tournament_id = Column(Integer, ForeignKey("tournaments.id", ondelete="SET NULL"),
                           nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    # How the match finished — see ``services.match_outcome.END_REASONS``.
    # ``status`` alone can't answer this: every terminal path but the cleanup
    # job writes "completed", so a forfeit, an /endmatch and a /clearmatches
    # sweep are indistinguishable without it.
    end_reason = Column(String(30), nullable=True)
    # The user who pressed /endmatch or /clearmatches, when a person ended it.
    ended_by_id = Column(Integer, nullable=True)

    user1 = relationship("User", foreign_keys=[user1_id])
    user2 = relationship("User", foreign_keys=[user2_id])

    __table_args__ = (
        Index("ix_matches_status", "status"),
        Index("ix_matches_winner", "winner_id"),
        Index("ix_matches_end_reason", "end_reason"),
        Index("ix_matches_match_type", "match_type"),
    )


class AdminLog(Base):
    """Audit log for admin actions in the web panel."""
    __tablename__ = "admin_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(String(50), nullable=False)  # player_add, player_edit, player_delete, bulk_upload, etc
    target_type = Column(String(30), nullable=True)  # player, user, roster
    target_id = Column(Integer, nullable=True)
    target_name = Column(String(150), nullable=True)
    detail = Column(String(500), nullable=True)
    ip_address = Column(String(50), nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_admin_logs_timestamp", "timestamp"),
        Index("ix_admin_logs_action", "action"),
    )


# ══════════════════════════════════════════════════════════════════════
# TRAIT SYSTEM
# ══════════════════════════════════════════════════════════════════════

class Trait(Base):
    """Master definition of a trait. Seeded from
    ``services.trait_service.TRAIT_DEFINITIONS`` at startup."""
    __tablename__ = "traits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False)
    # Batting / Bowling / Fielding / Mental / Awareness / Special / Elite
    category = Column(String(30), nullable=False)
    description = Column(String(300), nullable=False)
    emoji = Column(String(10), default="✨")
    effect_key = Column(String(50), nullable=False)  # routed to trait_engine handlers
    # common / rare / epic / elite — drives the market roll odds and the price
    # (config.TRAIT_RARITIES). NULL is read as "common" everywhere.
    rarity = Column(String(20), default="common")
    # Gems for a Lv.1 copy. NULL means "derive from rarity", which is what the
    # seed leaves it as; an admin can pin a price per trait from the website.
    base_price = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PlayerTrait(Base):
    """Trait equipped on a user's roster entry. Max 3 per roster_id."""
    __tablename__ = "player_traits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    roster_id = Column(Integer, ForeignKey("user_roster.id"), nullable=False, index=True)
    trait_id = Column(Integer, ForeignKey("traits.id"), nullable=False)
    level = Column(Integer, default=1)
    acquired_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_pt_user_roster", "user_id", "roster_id"),
    )


class TraitInventory(Base):
    """Unequipped traits stockpiled by user."""
    __tablename__ = "trait_inventory"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    trait_id = Column(Integer, ForeignKey("traits.id"), nullable=False)
    level = Column(Integer, default=1)
    acquired_at = Column(DateTime, default=datetime.utcnow)


class TraitMarket(Base):
    """Daily shop snapshot — 5 slots per user, refreshes every 24h."""
    __tablename__ = "trait_market"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    slot_index = Column(Integer, nullable=False)
    trait_id = Column(Integer, ForeignKey("traits.id"), nullable=False)
    base_price = Column(Integer, nullable=False)
    discount_pct = Column(Integer, default=0)
    final_price = Column(Integer, nullable=False)
    purchased = Column(Boolean, default=False)
    refreshed_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_tm_user_slot", "user_id", "slot_index"),
    )


class TraitDaily(Base):
    """Per-user per-day counters (purchases cap)."""
    __tablename__ = "trait_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    day_key = Column(String(10), nullable=False)  # YYYY-MM-DD
    purchases = Column(Integer, default=0)
    rerolls = Column(Integer, default=0)
    last_refresh_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_td_user_day", "user_id", "day_key"),
    )


# ══════════════════════════════════════════════════════════════════════
# PLAYER MARKET — daily 5-slot 87+ shop
# ══════════════════════════════════════════════════════════════════════

class PlayerMarket(Base):
    """Daily player market snapshot. 5 slots per user, refreshes every 24h.
    Each slot = a high-rated (87+) player at the normal buy price."""
    __tablename__ = "player_market"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    slot_index = Column(Integer, nullable=False)  # 1..5 (display)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    base_price = Column(Integer, nullable=False)
    final_price = Column(Integer, nullable=False)  # sell price == base_price
    purchased = Column(Boolean, default=False)
    refreshed_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_pm_user_slot", "user_id", "slot_index"),
    )


# ══════════════════════════════════════════════════════════════════════
# BOT TEAMS (for /vsbot)
# ══════════════════════════════════════════════════════════════════════

class BotTeam(Base):
    """A pre-built team users can play against via /vsbot. Admin-managed."""
    __tablename__ = "bot_teams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(String(300), nullable=True)
    difficulty = Column(String(20), default="Medium")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BotTeamPlayer(Base):
    """Members of a bot team. Players are real Player records.
    batting_order = 1..N for batting position."""
    __tablename__ = "bot_team_players"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_team_id = Column(Integer, ForeignKey("bot_teams.id"), nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    batting_order = Column(Integer, default=1)
    is_captain = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_btp_team_order", "bot_team_id", "batting_order"),
    )


# ══════════════════════════════════════════════════════════════════════
# MATCH STATE — persistent state for in-progress matches
# Source of truth for the match flow; survives bot restarts/redeploys.
# ══════════════════════════════════════════════════════════════════════

class MatchState(Base):
    __tablename__ = "match_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id"), unique=True, nullable=False, index=True)
    # Full game state serialized as JSON
    state_json = Column(Text, nullable=False)
    # Explicit state-machine pointer: PICK_DELIVERY / PICK_LENGTH / PICK_SHOT /
    # PICK_NEW_BATSMAN / PICK_NEW_BOWLER / INNINGS_BREAK / COMPLETED
    next_action = Column(String(40), nullable=False, default="PICK_DELIVERY")
    # Optimistic concurrency token — incremented on every save
    version = Column(Integer, default=0, nullable=False)
    # Sequential ball number — used for callback idempotency
    ball_seq = Column(Integer, default=0, nullable=False)
    last_modified = Column(DateTime, default=datetime.utcnow)
    # ID of the message currently showing buttons (for re-rendering)
    last_prompt_msg_id = Column(Integer, nullable=True)


# ══════════════════════════════════════════════════════════════════════
# QUESTS — daily/monthly engagement objectives
# ══════════════════════════════════════════════════════════════════════

class Quest(Base):
    """Master definition of a quest. Admin-managed."""
    __tablename__ = "quests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(String(300), nullable=False)
    quest_type = Column(String(20), nullable=False)   # 'daily', 'weekly' or 'monthly'
    event_key = Column(String(50), nullable=False)
    target_count = Column(Integer, default=1, nullable=False)
    reward_points = Column(Integer, default=5, nullable=False)
    reward_coins = Column(Integer, default=0)
    reward_gems = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    emoji = Column(String(10), default="🎯")
    sort_order = Column(Integer, default=0)
    # When True, this quest is assigned to EVERY user each period (in addition
    # to their random picks) — used for pinned quests like "watch N ads daily".
    always_assign = Column(Boolean, default=False, nullable=False)
    # When True, the quest tracks the user's Career Player (/cmucareer) and is
    # only assigned to users who have one. Career quests fire the 'career_*'
    # event keys — see services/quest_service.track_user_match_quests.
    career_only = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserQuestProgress(Base):
    """Per-user progress on a single quest for a single period."""
    __tablename__ = "user_quest_progress"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    quest_id = Column(Integer, ForeignKey("quests.id"), nullable=False)
    period_key = Column(String(10), nullable=False)
    progress = Column(Integer, default=0, nullable=False)
    completed = Column(Boolean, default=False, nullable=False)
    claimed = Column(Boolean, default=False, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow)
    # Whether this quest is one of the user's randomly-selected quests for the
    # current period. Only assigned quests show in /mq, count for tracking,
    # and are eligible for auto-claim at period end.
    assigned = Column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("ix_uqp_user_period", "user_id", "period_key"),
        Index("ix_uqp_user_quest_period", "user_id", "quest_id", "period_key", unique=True),
    )


# ══════════════════════════════════════════════════════════════════════
# ACHIEVEMENTS — permanent unlockable badges
# ══════════════════════════════════════════════════════════════════════

class UserAchievement(Base):
    """Achievements unlocked by a user. The achievement key is hardcoded
    in services/achievement_service.py (CATALOG)."""
    __tablename__ = "user_achievements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    achievement_key = Column(String(50), nullable=False)
    unlocked_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_uach_user_key", "user_id", "achievement_key", unique=True),
    )


# ══════════════════════════════════════════════════════════════════════
# PLAYER FORM — last-5 match performances, drives in-match modifier
# ══════════════════════════════════════════════════════════════════════

class PlayerFormHistory(Base):
    """Recent match performance for a player owned by a user.
    Used to compute current 'form' which slightly modifies in-match outcomes.
    """
    __tablename__ = "player_form_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=True)
    # Batting performance (if batted)
    runs = Column(Integer, default=0)
    balls = Column(Integer, default=0)
    out = Column(Boolean, default=False)
    # Bowling performance (if bowled)
    wickets = Column(Integer, default=0)
    runs_conceded = Column(Integer, default=0)
    overs_bowled = Column(Float, default=0.0)
    recorded_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_pfh_user_player", "user_id", "player_id"),
    )


# ══════════════════════════════════════════════════════════════════════
# LIVE COMMENTARY — admin-managed text bank for in-match flavour
# ══════════════════════════════════════════════════════════════════════

class CommentaryEntry(Base):
    """A single commentary line. event_key buckets like 'dot', 'four', 'six',
    'wicket_bowled', etc. Text supports placeholders: {batsman}, {bowler},
    {fielder}, {keeper}, {runs}.
    """
    __tablename__ = "commentary_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(40), nullable=False, index=True)
    text = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    weight = Column(Integer, default=1)  # higher = more likely to be chosen
    created_at = Column(DateTime, default=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════
# CUSTOM PLAYER CARD IMAGES — admin can upload custom card art per player
# ══════════════════════════════════════════════════════════════════════

class PlayerImage(Base):
    """Custom card image for a player. If active, replaces the auto-generated
    card in /claim, /buy, /myroster previews, and match in-play cards.
    Falls back to the default generator if no row exists or is_active=False.
    """
    __tablename__ = "player_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False, unique=True, index=True)
    # Image kind: 'default' (regular card), 'batsman' (in-match), 'bowler' (in-match)
    # v1 supports just 'default' which overrides all card displays.
    image_kind = Column(String(20), default="default")
    # Path on disk relative to project root (e.g. data/player_images/123.png)
    image_path = Column(String(300), nullable=False)
    # Telegram file_id — set after first upload to the storage channel.
    # When present, the bot can send the image via this id without disk read.
    tg_file_id = Column(String(200), nullable=True, index=True)
    # Optional admin-set caption / variant name
    label = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    uploaded_by = Column(String(100), nullable=True)


# ══════════════════════════════════════════════════════════════════════
# CMU SHOP — website-managed image gallery shown by the /CMUshop command
# ══════════════════════════════════════════════════════════════════════

class CMUShopImage(Base):
    """One image in the /CMUshop gallery. Images + a single shared caption
    (GameConfig.cmushop_caption) are managed from the website. The bot sends
    a single photo when only one active image exists, or a Next/Prev carousel
    when several do. Durable across redeploys via the Telegram storage-channel
    file_id, exactly like PlayerImage.
    """
    __tablename__ = "cmu_shop_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Path on disk relative to project root (e.g. data/cmushop_images/3.png).
    image_path = Column(String(300), nullable=True)
    # Telegram file_id — set after upload to the storage channel; lets the bot
    # re-send without a disk read and survives ephemeral-host redeploys.
    tg_file_id = Column(String(200), nullable=True, index=True)
    # Display order in the carousel (ascending).
    sort_order = Column(Integer, default=0, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    uploaded_by = Column(String(100), nullable=True)


# ══════════════════════════════════════════════════════════════════════
# STORED ASSETS — uploads that must survive a redeploy
# ══════════════════════════════════════════════════════════════════════

class StoredAsset(Base):
    """One website upload, kept in the database so it is never lost.

    The app runs on hosts with an ephemeral filesystem: everything written under
    ``data/`` disappears on the next deploy, which used to mean re-uploading
    every card template, font, flag and wizard image by hand. Mirroring to a
    Telegram storage channel only helped deployments that had set
    ``STORAGE_CHAT_ID``, and never covered flags or wizard artwork at all.

    The database is the one store that survives unconditionally and needs no
    extra configuration, so every website upload is written here as well as to
    disk. Disk stays the fast path; this is the source of truth that refills it.
    See services/asset_store.py.
    """
    __tablename__ = "stored_assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Project-relative path with forward slashes, e.g.
    # "data/card_templates/template_career_1.png". Unique, so re-uploading
    # replaces rather than duplicates.
    key = Column(String(300), unique=True, nullable=False, index=True)
    filename = Column(String(200), nullable=True)
    content_type = Column(String(100), nullable=True)
    data = Column(LargeBinary, nullable=False)
    byte_size = Column(Integer, default=0, nullable=False)
    # Lets a restore skip rewriting a file that is already correct on disk.
    sha256 = Column(String(64), nullable=True, index=True)
    # The copy in the Telegram storage channel, when STORAGE_CHAT_ID is set.
    # A third tier behind disk and ``data``: Telegram keeps a file by id
    # forever, so an asset that reached the channel survives even a rebuilt
    # database. ``asset_store.ensure`` falls back to it.
    telegram_file_id = Column(String(200), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(100), nullable=True)


# ══════════════════════════════════════════════════════════════════════
# CAREER PLAYER — website-managed assets and name pool for /cmucareer
# ══════════════════════════════════════════════════════════════════════

class CareerFace(Base):
    """One selectable face in the /cmucareer creation wizard.

    Each face is a COMPLETE blank card: the portrait and the "CAREER PLAYER"
    banner are baked into the uploaded artwork, and the card generator only
    fills the empty text slots (name, category, OVR, batting power, bowling
    specs, flag + country, batting/bowling style).

    A face therefore *is* a card-template variant, keyed ``career_<slot>``, with
    its own blank image and its own independently tunable text layout on the
    website — see services/card_template_service.py.
    """
    __tablename__ = "career_faces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 1-based; maps to the card template variant key f"career_{slot}" and the
    # template file data/card_templates/template_career_<slot>.<ext>.
    slot = Column(Integer, unique=True, nullable=False)
    label = Column(String(60), nullable=True)             # "Face 1"
    # Path on disk of the blank card template for this face.
    template_path = Column(String(300), nullable=True)
    # Telegram file_id of the photo shown while browsing faces in the wizard.
    # Set after upload to the storage channel, exactly like CMUShopImage.
    preview_file_id = Column(String(200), nullable=True, index=True)
    sort_order = Column(Integer, default=0, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    uploaded_by = Column(String(100), nullable=True)


class CareerStepImage(Base):
    """The website-managed photo shown on one step of the /cmucareer wizard.

    Every wizard step is a photo message with buttons underneath; admins upload
    the artwork per step from the website. Durable across redeploys via the
    Telegram storage-channel file_id, exactly like CMUShopImage.
    """
    __tablename__ = "career_step_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 'intro' | 'country' | 'initial' | 'surname' | 'bat_hand' | 'bowl_hand'
    # | 'bowl_type' | 'face' | 'confirm'
    step_key = Column(String(30), unique=True, nullable=False)
    image_path = Column(String(300), nullable=True)
    tg_file_id = Column(String(200), nullable=True, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    uploaded_by = Column(String(100), nullable=True)


class CareerNamePool(Base):
    """One first name or surname available to career players of one country.

    Seeded from data/players.json by seed_career_names.py and editable on the
    website. The wizard combines a first name starting with the chosen initial
    and a surname starting with the chosen surname letter, and only accepts a
    combination that no existing player already uses.
    """
    __tablename__ = "career_name_pool"

    id = Column(Integer, primary_key=True, autoincrement=True)
    country = Column(String(60), nullable=False, index=True)
    name_kind = Column(String(10), nullable=False)        # 'first' | 'surname'
    value = Column(String(60), nullable=False)
    # First character upper-cased, stored so the wizard can offer only the
    # letters that actually have names behind them without scanning every row.
    letter = Column(String(1), nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_career_name_lookup", "country", "name_kind", "letter"),
        Index("ix_career_name_unique", "country", "name_kind", "value", unique=True),
    )


class CareerChangeRequest(Base):
    """One request to change a Career Player's name and/or country.

    The owner submits it from the bot or the Mini App; what happens next depends
    on where the new name came from (see services/career_change_service.py):

    * a name rolled from the admin-curated ``career_name_pool``, or a country
      change on its own, is already vetted — it is applied on the spot and the
      row is written straight to ``applied``
    * a **custom** name the owner typed has to be approved on the website, so
      the row stays ``pending`` and *nothing* about the card changes: the old
      name and the old country both continue until an admin approves it

    The gems are taken when the request is submitted, which is what stops the
    review queue filling with speculative names, and they are returned in full
    if the request is rejected or cancelled. The row is the receipt for that:
    ``gems_charged``, ``change_index`` and ``used_free_grant`` are everything
    the refund needs to put the owner back exactly where they were.
    """
    __tablename__ = "career_change_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Deliberately *not* a foreign key: this row is the receipt for gems that
    # were taken, and it has to outlive the card it was about. Support can
    # delete a career player at any time, and a FK here would either block that
    # delete or cascade the payment history away with it.
    player_id = Column(Integer, nullable=False, index=True)

    # What was asked for. A NULL new_* means that half was left alone.
    old_name = Column(String(150), nullable=True)
    new_name = Column(String(150), nullable=True)
    old_country = Column(String(60), nullable=True)
    new_country = Column(String(60), nullable=True)
    # 'pool' | 'custom' | None (country-only change)
    name_source = Column(String(10), nullable=True)

    # What it cost. change_index is the 1-based rung of the price ladder this
    # request consumed, or 0 when an admin-granted free change paid for it.
    change_index = Column(Integer, default=0, nullable=False)
    gems_charged = Column(Integer, default=0, nullable=False)
    used_free_grant = Column(Boolean, default=False, nullable=False)

    # 'pending' | 'applied' | 'approved' | 'rejected' | 'cancelled'
    status = Column(String(12), default="pending", nullable=False, index=True)
    review_note = Column(String(300), nullable=True)
    reviewed_by = Column(String(80), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    decided_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_career_change_status", "status", "created_at"),
    )


class TeamLogoRequest(Base):
    """One user's team logo, waiting on a bot admin's yes or no.

    Shaped after :class:`CareerChangeRequest`, for the same reason: a logo a
    user uploads is shown to every other player on every scorecard of every
    match their team appears in, so it is moderated before it goes anywhere,
    and the row is the receipt for that decision.

    Nothing about the team changes while a request is pending. The old crest
    (or none) keeps rendering until an admin approves this one, at which point
    ``asset_key``/``file_id`` are copied onto the ``users`` row. A rejection
    writes the reason to ``review_note`` and the bytes are dropped.
    """
    __tablename__ = "team_logo_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Deliberately *not* a foreign key, for the reason given on
    # CareerChangeRequest.player_id: this row is a moderation receipt and has
    # to outlive the account it was about.
    user_id = Column(Integer, nullable=False, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    # The team name as it stood when the logo was submitted. An admin reviewing
    # the queue needs to see the name the crest was meant for, not whatever the
    # owner has renamed to since.
    team_name = Column(String(50), nullable=True)

    asset_key = Column(String(300), nullable=True)
    file_id = Column(String(200), nullable=True)
    byte_size = Column(Integer, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)

    # 'pending' | 'approved' | 'rejected' | 'cancelled'
    status = Column(String(12), default="pending", nullable=False, index=True)
    # The rejection reason, shown to the owner verbatim.
    review_note = Column(String(300), nullable=True)
    reviewed_by = Column(String(80), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    decided_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_team_logo_status", "status", "created_at"),
    )


# ══════════════════════════════════════════════════════════════════════
# NOTIFICATIONS — scheduled FOMO-style push messages from the bot
# ══════════════════════════════════════════════════════════════════════

class NotificationSchedule(Base):
    """A notification rule. The cron-like job fires it when conditions match."""
    __tablename__ = "notification_schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)            # admin label
    message = Column(Text, nullable=False)                # template — supports {first_name}, {coins}, {gems}, {streak}
    # Timing
    schedule_type = Column(String(20), default="daily")   # 'daily' | 'interval' | 'one_off'
    # For 'daily': fires once per day at hour:minute IST
    fire_hour = Column(Integer, default=18)               # 0-23 IST
    fire_minute = Column(Integer, default=0)              # 0-59 IST
    # For 'interval': repeats every N hours after last_fired_at
    interval_hours = Column(Integer, default=24)
    # Time window — only fire if current IST hour is between these two
    window_start_hour = Column(Integer, default=10)       # 0-23 IST
    window_end_hour = Column(Integer, default=22)         # 0-23 IST (exclusive)
    # Targeting filters
    target_filter = Column(String(20), default="all")     # 'all' | 'inactive_24h' | 'active' | 'low_coins' | 'has_streak'
    # State
    is_active = Column(Boolean, default=True)
    last_fired_at = Column(DateTime, nullable=True)       # last time the job actually fired
    sent_count = Column(Integer, default=0)               # cumulative recipients
    created_at = Column(DateTime, default=datetime.utcnow)


class NotificationLog(Base):
    """Per-user delivery record for a single notification fire."""
    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    schedule_id = Column(Integer, ForeignKey("notification_schedules.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    sent_at = Column(DateTime, default=datetime.utcnow, index=True)
    delivered = Column(Boolean, default=True)
    error_text = Column(String(500), nullable=True)


# ══════════════════════════════════════════════════════════════════════
# CLAIM RARITY TIERS — admin-configurable distribution for /claim
# ══════════════════════════════════════════════════════════════════════

class ClaimRarityTier(Base):
    """Rating tier definition for /claim pulls. Sum of probabilities should be ~100.
    If empty (no rows), code falls back to CLAIM_RARITY in config.py.
    """
    __tablename__ = "claim_rarity_tiers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    label = Column(String(40), nullable=False)               # 'Bronze', 'Legendary', etc
    rating_min = Column(Integer, nullable=False)             # inclusive
    rating_max = Column(Integer, nullable=False)             # inclusive
    # Percent, 0.0-100.0. Stored as a float so odds as thin as 0.00001%
    # (1 in 10,000,000) survive a round-trip through the website form.
    probability = Column(Float, nullable=False)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    emoji = Column(String(10), default="🃏")


class RatingBlockRule(Base):
    """A rating band that random reward draws must never hand out.

    Blocking is a hard veto applied *after* the rarity weights have chosen a
    band, so an admin can retire a rating (say, everything 95+) without having
    to rewrite the rarity table — flip the rule off later and the ratings come
    back. Each rule names the sources it applies to, because "no 95+ from the
    free spin" and "no 95+ from either freebie" are different decisions:

      ``block_claim`` — /claim and /daily, including the streak milestone card
      ``block_gspin`` — the /gspin wheel and its Mini App twin

    Those two are the whole list. Blocks never apply to packs (bought, granted
    or free), the Mystery Box, the subscriber Weekly Card, buying, trading, the
    market or admin grants: a pack advertising a guaranteed 92-99 card must hand
    one over even while 92-99 is blocked from the free draws.

    ``block_drop`` is retired. It used to cover packs and other drops;
    :mod:`services.rating_block_service` no longer reads it and the website
    writes it False, but the column stays so existing rows still load.
    """
    __tablename__ = "rating_block_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rating_min = Column(Integer, nullable=False)             # inclusive
    rating_max = Column(Integer, nullable=False)             # inclusive
    block_claim = Column(Boolean, default=True, nullable=False)
    block_drop = Column(Boolean, default=False, nullable=False)  # retired, ignored
    block_gspin = Column(Boolean, default=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    note = Column(String(200), default="")                   # why it's blocked
    created_at = Column(DateTime, default=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════
# GAME CONFIG — admin-tunable economy values
# ══════════════════════════════════════════════════════════════════════

class GameConfig(Base):
    """Single-row configuration for tunable game values.
    Admin-managed via /economy. Code reads via config_service.get_config()
    which falls back to baked defaults if row missing.
    """
    __tablename__ = "game_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Match rewards
    match_win_coins_per_over = Column(Integer, default=300)
    match_win_gems_per_over = Column(Float, default=1.0)       # Float to allow 0.5
    match_loss_coins_per_over = Column(Integer, default=150)
    match_loss_gems_per_over = Column(Float, default=0.5)
    # GSpin gem range (for blue outcome)
    gspin_gem_min = Column(Integer, default=3)
    gspin_gem_max = Column(Integer, default=150)
    # Daily reward
    daily_coins = Column(Integer, default=1500)
    daily_gems = Column(Integer, default=0)
    daily_streak_bonus_coins = Column(Integer, default=60)    # extra per day of streak
    daily_streak_bonus_gems = Column(Integer, default=0)
    # ── Mini App ad-gated quotas (per 24h cycle) ──
    # Number of AD-watched spins/dailies allowed per cycle (in addition to
    # 1 free use). Admin-tunable via /admin/economy.
    spin_ad_quota = Column(Integer, default=5, nullable=False)
    daily_ad_quota = Column(Integer, default=5, nullable=False)
    # How many of those ad slots may be taken per cycle when the ad network has
    # nothing to serve. 0 disables the rescue entirely (ads become mandatory
    # again); raising it trades a little revenue for never dead-ending a player.
    spin_nofill_grace = Column(Integer, default=2, nullable=False)
    # Minimum gap between two AD-GATED rewards of the same kind (spin, daily),
    # in minutes. 0 (the default) means a watched ad pays out immediately;
    # 60 would allow one rewarded ad per hour per feature.
    #
    # Defaults to 0 because an hour here is indistinguishable from the feature
    # being broken: the player watches a full ad and is answered "next spin in
    # 59m". The ad is banked rather than lost, but nobody experiences a banked
    # credit as a reward. See services/quota_service.py.
    ad_reward_gap_minutes = Column(Integer, default=0, nullable=False)
    # Daily Quick Match limit per user. Resets at UTC midnight.
    daily_quick_match_limit = Column(Integer, default=5, nullable=False)
    # ── Free Pack (Mini App, ad-gated) ──
    free_pack_cooldown_minutes = Column(Integer, default=60, nullable=False)
    # JSON list of probability bands, e.g.
    # [{"min":75,"max":80,"weight":79},{"min":81,"max":83,"weight":7},...]
    # If null, a sensible default table is used.
    free_pack_bands_json = Column(Text, nullable=True)
    # ── Branding (admin-editable from /branding page) ──
    # Public Telegram channel + group usernames (without @). Used in welcome
    # messages, /howto, /invite, and Mini App footer. Blank = hide link.
    branding_channel_username = Column(String(64), nullable=True)
    branding_channel_label = Column(String(80), nullable=True)
    branding_group_username = Column(String(64), nullable=True)
    branding_group_label = Column(String(80), nullable=True)
    # Official group for buying players. /buypl only allows purchase here.
    # official_group_id is the numeric chat ID (e.g. -1001234567890) used to
    # compare the current chat; official_group_link is the t.me invite/handle.
    official_group_id = Column(BigInteger, nullable=True)
    official_group_link = Column(String(200), nullable=True)
    # Editable welcome message for new group members. Supports placeholders:
    #   @User or {name} → mentions/uses the new member's name
    welcome_message = Column(Text, nullable=True)
    branding_tagline = Column(String(200), nullable=True)
    # Debut bonus
    debut_coins = Column(Integer, default=1500)
    debut_gems = Column(Integer, default=30)

    # ── Simulation tuning (additive percentage points applied at end) ──
    # These nudge final probabilities to fix systemic biases (e.g. too many dots).
    # Applied AFTER all per-ball modifiers but BEFORE normalization.
    # Default 0 = no adjustment (matches old behavior).
    sim_dot_adjust = Column(Float, default=-10.0)       # -10 = fewer dot balls
    sim_one_adjust = Column(Float, default=+6.0)        # +6 = more singles
    sim_two_adjust = Column(Float, default=+3.0)        # +3 = more twos
    sim_four_adjust = Column(Float, default=0.5)
    sim_six_adjust = Column(Float, default=0.3)
    sim_wicket_adjust = Column(Float, default=0.0)
    sim_extras_adjust = Column(Float, default=0.0)
    # ── Player market settings ──
    market_min_rating = Column(Integer, default=87)
    market_default_slots = Column(Integer, default=6)
    market_last_refresh_at = Column(DateTime, nullable=True)
    # Refresh schedule (IST): every `interval` hours starting from
    # `market_refresh_hour_ist`. 0 = midnight IST, 9 = 9 AM IST, etc., and
    # interval 12 + start 0 means 12 AM and 12 PM, every day. Interval 24 (the
    # default) is the single daily reroll this market has always had, so a DB
    # migrated but not yet re-saved keeps exactly the schedule it had.
    market_refresh_hour_ist = Column(Integer, default=0)
    market_refresh_interval_hours = Column(Integer, default=24)
    # Trait market settings
    trait_market_default_slots = Column(Integer, default=5)
    trait_market_last_refresh_at = Column(DateTime, nullable=True)
    # The trait market runs the same kind of schedule on its own columns, so
    # the two markets can turn over at completely different rates. NULL start
    # hour falls back to market_refresh_hour_ist, so a DB migrated but not yet
    # re-saved keeps the time it had.
    trait_market_refresh_interval_hours = Column(Integer, default=24)
    trait_market_refresh_start_hour_ist = Column(Integer, nullable=True)
    # ── Scorecard color customization ──
    # Hex strings like "#c41e3a". Used as accent (header border, table-header
    # bottom border, RTG color, FoW labels, target line) on the per-innings
    # scorecard. Defaults match the original PRIMARY (red) / SECONDARY (teal).
    scorecard_color_inn1 = Column(String(9), default="#c41e3a")
    scorecard_color_inn2 = Column(String(9), default="#00c9a7")
    # Off renders the reference poster's literal "Game Changer!"; on names what
    # the Player of the Match actually did.
    scorecard_dynamic_flourish = Column(Boolean, default=False)
    # Post the Player of the Match's collectible card as a second photo after
    # the summary card — the card at full size, with its own caption. On by
    # default, but suppressed while the card is drawn into the summary card
    # itself: the two below are the same artwork in two places, and exactly one
    # of them runs. See services/scorecard_delivery.send_potm_card.
    scorecard_potm_card = Column(Boolean, default=True)
    # Draw that same card *into* the summary card, beside the Player of the
    # Match's name — which is where someone reading the result looks for it,
    # and where the chat cannot lose it. On by default, and it is what
    # suppresses the second photo above.
    scorecard_potm_card_inline = Column(Boolean, default=True)
    scorecard_text_settings = Column(Text, nullable=True)
    # ── /wpm and /cm completion cards ──
    # Comma-separated list of which cards to post to the lobby chat when a
    # Mini App match ends. Valid tokens: summary, bat1, bowl1, bat2, bowl2.
    # Empty/None falls back to "summary" (the historical behavior).
    wpm_result_cards = Column(String(120), default="summary")
    # ── Maintenance mode ──
    # When is_maintenance is True, all bot commands except those from a
    # bypass user return maintenance_message instead of running. Matches
    # already in progress are protected — only NEW matches/commands blocked.
    is_maintenance = Column(Boolean, default=False, nullable=False)
    maintenance_message = Column(Text, nullable=True)
    maintenance_until = Column(DateTime, nullable=True)  # optional ETA shown to users
    maintenance_started_at = Column(DateTime, nullable=True)
    # Comma-separated telegram IDs allowed to use commands during maintenance
    # (admins testing the bot, for example).
    maintenance_bypass_ids = Column(String(500), nullable=True)
    # ── Rookie mode (membership gate) ──
    # When rookie_mode is True, using the bot at all requires an active
    # subscription of at least the ``rookie`` tier: every command, button and
    # Mini App API is locked for everybody else, except the doorway commands
    # (/debut, /membership, /help …) listed in services/rookie_gate.py. The
    # maintenance bypass list doubles as the Rookie bypass list.
    # rookie_message replaces the default upsell shown to locked-out users.
    rookie_mode = Column(Boolean, default=False, nullable=False)
    rookie_message = Column(Text, nullable=True)
    # ── Forced Official GC join (services/gc_gate.py) ──
    # When force_gc_join is True (and official_group_id is set), every command
    # and button is locked until the user is a member of the Official GC,
    # apart from the doorway commands in services/gc_gate.py.
    # gc_join_message replaces the default join prompt.
    force_gc_join = Column(Boolean, default=False, nullable=False)
    gc_join_message = Column(Text, nullable=True)
    # ── Retention (services/onboarding_service.py, comeback_service.py) ──
    onboarding_enabled = Column(Boolean, default=True, nullable=False)
    comeback_enabled = Column(Boolean, default=True, nullable=False)
    # JSON {"1": {"coins": 500, "gems": 0}, "3": {...}, ...} keyed by days
    # inactive. NULL = the defaults in services/comeback_service.py.
    comeback_rewards_json = Column(Text, nullable=True)
    # Comma-separated telegram IDs allowed to use the Challenge League Tournament
    # command. Empty/None = open to everyone (restriction off).
    tournament_allowed_ids = Column(String(500), nullable=True)
    # Global gameplay style for all newly started matches: original Telegram
    # callback buttons (default) or the optional Mini App live board.
    match_style = Column(String(20), default="telegram", nullable=False)
    challenge_max_overs = Column(Integer, default=2, nullable=False)
    allow_same_team_challenge = Column(Boolean, default=False, nullable=False)
    # ── Challenge Draft pool (/cdraft, editable on the Match Gameplay page
    # and with /cdraftset) ──
    # The two ends of the draft's rating ladder: slot 1 is dealt around the max
    # and slot 11 around the min. A slot whose exact rating is empty looks
    # elsewhere inside this band before leaving it.
    cdraft_rating_min = Column(Integer, default=78, nullable=False)
    cdraft_rating_max = Column(Integer, default=88, nullable=False)
    # JSON array of the Player.version labels a draft may deal, e.g.
    # ["Base", "Legend"]. NULL or empty means EVERY version is allowed — the
    # alternative reading ("none") would be a mode that can never start.
    cdraft_versions_json = Column(Text, nullable=True)
    # How far apart the TWO CARDS OFFERED IN ONE SLOT may be on OVR. This is the
    # mechanic that makes the two squads provably fair, so it is kept small.
    cdraft_pair_spread = Column(Integer, default=1, nullable=False)
    # ── Player card rendering (admin-editable from /card-template page) ──
    # Which card design is active for all players: the built-in procedural
    # tier card ("tier") or the admin-uploaded template card ("template").
    card_style = Column(String(20), default="tier", nullable=False)
    # Path (under data/card_templates/) to the uploaded template background image.
    card_template_image_path = Column(String(300), nullable=True)
    # Raw HTML image-map <area> code defining where each player field is drawn.
    card_template_area_code = Column(Text, nullable=True)
    # Whether to composite the player's portrait into the template card.
    card_template_show_portrait = Column(Boolean, default=True, nullable=False)
    # Optional uploaded TTF/OTF font and JSON layout controls copied from the
    # standalone cricket_card_generator_website_v4-3.html editor.
    card_template_font_path = Column(String(300), nullable=True)
    card_template_settings = Column(Text, nullable=True)
    # Shared caption shown under every /CMUshop image (same for all images).
    cmushop_caption = Column(Text, nullable=True)
    # ── Career Player (/cmucareer) ──
    # Gems paid for each career weekly quest, and the streak jackpot: clearing
    # every career weekly quest for career_streak_weeks consecutive weeks pays
    # career_streak_bonus_gems.
    career_quest_gems = Column(Integer, default=15, nullable=False)
    career_streak_weeks = Column(Integer, default=4, nullable=False)
    career_streak_bonus_gems = Column(Integer, default=100, nullable=False)
    # ── Career Player name / country changes ──
    # The first change is free for everyone. Every change after it costs gems on
    # a rising ladder — 2nd, 3rd, then +step for each one after — and is only
    # sold at all while career_paid_changes_open is on, which is the switch the
    # website uses to open the paid changes up.
    career_change_price_2 = Column(Integer, default=300, nullable=False)
    career_change_price_3 = Column(Integer, default=500, nullable=False)
    career_change_price_step = Column(Integer, default=250, nullable=False)
    career_paid_changes_open = Column(Boolean, default=False, nullable=False)
    # Whether owners may type their own name at all, and whether a typed one
    # waits for website approval before it goes on the card. Turning approval
    # off makes custom names instant — the pool names always are.
    career_custom_names_open = Column(Boolean, default=True, nullable=False)
    career_custom_names_need_approval = Column(Boolean, default=True, nullable=False)
    # Newline/comma separated words a custom name may not contain. Checked
    # against the name with everything but letters and digits stripped out, so
    # spacing and punctuation can't be used to slip one through.
    career_name_blocklist = Column(Text, nullable=True)
    # ── Elite Signing Bonus (limited-time offer) ──
    # Buying a card rated at or above gem_bonus_min_rating pays gem_bonus_bps
    # basis points of the coins spent back as gems (10 bps = 0.1%). It is an
    # offer, not a permanent rule: gem_bonus_enabled is the admin's open/close
    # switch, and the optional window auto-opens and auto-closes it. A null
    # start means "already running", a null end means "until I close it".
    # Reading is centralised in services/buy_bonus.py — never read these raw.
    gem_bonus_enabled = Column(Boolean, default=True, nullable=False)
    gem_bonus_min_rating = Column(Integer, default=96, nullable=False)
    gem_bonus_bps = Column(Integer, default=10, nullable=False)
    gem_bonus_starts_at = Column(DateTime, nullable=True)
    gem_bonus_ends_at = Column(DateTime, nullable=True)
    # ── CMU News (Mini App) ──
    # Coins paid to a user when a bot admin approves their submitted article.
    news_submit_reward_coins = Column(Integer, default=100, nullable=False)
    # Auto-generated stories (tournament champions, record buys, …) go live
    # straight away when on; when off they wait in the review queue.
    news_auto_publish = Column(Boolean, default=True, nullable=False)
    # Comma list of auto-story kinds that are switched on; NULL means all.
    # See services/news_service.AUTO_KINDS.
    news_auto_kinds = Column(Text, nullable=True)
    # Also post auto stories to the branding channel/group. Off by default so
    # a busy auction does not flood the channel.
    news_auto_announce = Column(Boolean, default=False, nullable=False)
    # Updated tracking (existing)
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(String(80), nullable=True)


# ══════════════════════════════════════════════════════════════════════
# (Player versioning is implemented at the Player table level via
# parent_player_id — see services/version_service.py for behavior.)
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# MESSAGE TEMPLATES — admin-editable fixed strings used by the bot
# ══════════════════════════════════════════════════════════════════════

class MessageTemplate(Base):
    """Editable bot message. Bot code reads via message_service.get(key, ...).
    If a row exists with this key, it's used; otherwise the hardcoded default.
    """
    __tablename__ = "message_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(80), unique=True, nullable=False, index=True)
    label = Column(String(120), nullable=False)        # Human-readable name
    description = Column(String(400), nullable=True)   # explains where it shows + placeholders
    body = Column(Text, nullable=False)                # the message itself
    category = Column(String(40), default="general")   # group in admin UI
    is_active = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(String(80), nullable=True)


# ══════════════════════════════════════════════════════════════════════
# GLOBAL MARKETS — single shared market for ALL users (admin-managed)
# ══════════════════════════════════════════════════════════════════════
# These replace the per-user PlayerMarket / TraitMarket with a single
# market shared by all users. Admin controls slots, prices, quantities,
# and refreshes via the website.

class GlobalPlayerMarket(Base):
    """Single shared player market. Admin manages slots."""
    __tablename__ = "global_player_market"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slot_index = Column(Integer, nullable=False, unique=True, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    base_price = Column(Integer, nullable=False)        # normal /buy value
    # Sell price everyone pays; equals base_price when listed, admin-editable to
    # put a slot on sale. Platinum/Diamond discounts come off it per-buyer.
    final_price = Column(Integer, nullable=False)
    # How many copies may be bought. 0 (the default) means UNLIMITED — the
    # market never sells out, so a card listed today is still there tomorrow for
    # the captain who was 2,000 coins short. Set a positive number to make a
    # slot a limited run. See services.global_market.is_unlimited.
    quantity = Column(Integer, default=0)
    purchased_count = Column(Integer, default=0)        # how many already sold
    listed_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)


class GlobalTraitMarket(Base):
    """Single shared trait market. Admin manages slots."""
    __tablename__ = "global_trait_market"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slot_index = Column(Integer, nullable=False, unique=True, index=True)
    trait_id = Column(Integer, ForeignKey("traits.id"), nullable=False)
    base_price = Column(Integer, nullable=False)
    discount_pct = Column(Integer, default=0)
    final_price = Column(Integer, nullable=False)
    # 0 (the default) means UNLIMITED stock — see GlobalPlayerMarket.quantity.
    quantity = Column(Integer, default=0)
    purchased_count = Column(Integer, default=0)
    listed_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)


class MarketPurchase(Base):
    """Audit log of all global market purchases."""
    __tablename__ = "market_purchases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    market_type = Column(String(20), nullable=False)    # 'player' or 'trait'
    slot_index = Column(Integer, nullable=False)
    item_id = Column(Integer, nullable=False)            # player_id or trait_id
    item_name = Column(String(200), nullable=True)
    price_paid = Column(Integer, nullable=False)
    purchased_at = Column(DateTime, default=datetime.utcnow, index=True)


# ══════════════════════════════════════════════════════════════════════
# Pack system — buyable bundles that give 1 main + N bonus players
# ══════════════════════════════════════════════════════════════════════

class Pack(Base):
    """Admin-configurable pack definitions for /buypack."""
    __tablename__ = "packs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slot_number = Column(Integer, nullable=False, unique=True)  # display order 1, 2, 3...
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    emoji = Column(String(10), default="📦")

    # Cost — at least one of these should be > 0
    cost_coins = Column(Integer, default=0)
    cost_quest_points = Column(Integer, default=0)
    cost_gems = Column(Integer, default=0)

    # Filter mode for the MAIN player slot:
    #   'rating'  — match by rating range only (any version)
    #   'version' — match by version name only (any rating)
    #   'both'    — match BOTH (rating range AND version name)
    main_filter_mode = Column(String(10), default="rating")

    # Main player(s) — guaranteed
    main_min_rating = Column(Integer, default=85)
    main_max_rating = Column(Integer, default=87)
    main_count = Column(Integer, default=1)
    # JSON list of weights, one per integer rating in [min, max]. If null,
    # we use uniform distribution. Example for 85-87: "[60, 30, 10]"
    # Text, not String(500): the admin odds table writes decimal weights, and
    # a 50-100 band at five decimals each overflows 500 characters. Truncated
    # JSON parses as nothing, which reads back as "uniform" — a tuned pack
    # would silently pay flat odds. See services/pack_odds.py.
    main_weights_json = Column(Text, nullable=True)
    # JSON list of acceptable version names (case-insensitive match).
    # e.g. ["Star", "Star Card", "Star Player"]. Used when filter_mode is
    # 'version' or 'both'.
    main_versions_json = Column(String(500), nullable=True)

    # Bonus player(s) — extras included with the pack. Always rating-based,
    # any version (we don't restrict bonus pulls).
    bonus_min_rating = Column(Integer, default=74)
    bonus_max_rating = Column(Integer, default=80)
    bonus_count = Column(Integer, default=2)
    bonus_weights_json = Column(Text, nullable=True)

    # Limits
    daily_limit = Column(Integer, default=0)  # 0 = unlimited

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PackPurchase(Base):
    """Audit log + daily-limit enforcement source."""
    __tablename__ = "pack_purchases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    pack_id = Column(Integer, ForeignKey("packs.id"), nullable=False, index=True)
    cost_paid = Column(Integer, nullable=False)
    currency = Column(String(20), nullable=False)   # 'coins' / 'quest_points' / 'gems'
    players_json = Column(Text, nullable=True)      # serialized list of player_ids/names received
    purchased_at = Column(DateTime, default=datetime.utcnow, index=True)


class UnopenedPack(Base):
    """A pack the user has bought but not yet opened.
    Created on /buypack purchase, removed when /openpack opens it."""
    __tablename__ = "unopened_packs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    pack_id = Column(Integer, ForeignKey("packs.id"), nullable=False)
    acquired_at = Column(DateTime, default=datetime.utcnow, index=True)
    source = Column(String(40), default="buypack")  # 'buypack' / 'admin' / 'reward' etc.


class Tour(Base):
    """A tour: a multi-match challenge between two users."""
    __tablename__ = "tours"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user1_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # creator
    user2_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # invitee
    chat_id = Column(BigInteger, nullable=True)  # group chat where tour was created

    match_count = Column(Integer, nullable=False)        # 3, 4, 5, or 6
    overs_per_match = Column(Integer, nullable=False)    # 5+

    # Status flow:
    #   pending  → invite sent, awaiting user2's accept
    #   active   → user2 accepted; matches being played
    #   completed→ all matches played, winner decided
    #   expired  → either: user2 didn't accept in 30s,
    #              OR tour lifetime ended with unfinished matches
    #   declined → user2 explicitly declined
    status = Column(String(20), default="pending", index=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    accepted_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)        # invite expiry (30s after create)
    lifetime_expires_at = Column(DateTime, nullable=True)  # tour expiry (matches*2 days)
    completed_at = Column(DateTime, nullable=True)

    # Score after every completed match (denormalized for fast leaderboard)
    user1_wins = Column(Integer, default=0)
    user2_wins = Column(Integer, default=0)

    # Winner once tour is decided
    winner_id = Column(Integer, nullable=True)  # null = ongoing OR drawn

    user1 = relationship("User", foreign_keys=[user1_id])
    user2 = relationship("User", foreign_keys=[user2_id])

    __table_args__ = (
        Index("ix_tours_user1_status", "user1_id", "status"),
        Index("ix_tours_user2_status", "user2_id", "status"),
    )


class TourMatch(Base):
    """A single match within a tour."""
    __tablename__ = "tour_matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tour_id = Column(Integer, ForeignKey("tours.id"), nullable=False, index=True)
    match_number = Column(Integer, nullable=False)  # 1, 2, 3, ...
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=True)  # null until played

    # Pre-decided settings (so each tour match has its own venue)
    stadium = Column(String(100), nullable=True)
    pitch_type = Column(String(30), nullable=True)

    # Status:
    #   pending → not started yet
    #   playing → match_id set, match in progress
    #   done    → match completed
    #   forfeit → one side forfeited (idle)
    status = Column(String(20), default="pending")

    winner_id = Column(Integer, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_tour_matches_tour", "tour_id", "match_number"),
    )


class CLTour(Base):
    """A Challenge League Tour: a best-of series (3/5/7 matches) between two
    users using fixed Challenge-League teams. Each match reuses the /cipl engine
    (host picks pitch, both pick Playing XI, toss, over-by-over play)."""
    __tablename__ = "cl_tours"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user1_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # host
    user2_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # guest
    chat_id = Column(BigInteger, nullable=True)  # group chat where tour was created

    # SET NULL on delete (and nullable) so admins can still delete a retired
    # league/team that an old CL tour referenced — the tour's win record and
    # per-match results survive; the view already tolerates missing names. The
    # service requires these at creation, so live tours always have them set.
    league_id = Column(Integer, ForeignKey("challenge_leagues.id", ondelete="SET NULL"), nullable=True)
    host_team_id = Column(Integer, ForeignKey("challenge_teams.id", ondelete="SET NULL"), nullable=True)
    guest_team_id = Column(Integer, ForeignKey("challenge_teams.id", ondelete="SET NULL"), nullable=True)

    match_count = Column(Integer, nullable=False)  # 3, 5, or 7

    # Status flow:
    #   pending  → invite sent, awaiting guest's accept
    #   active   → guest accepted; matches being played
    #   completed→ a side clinched the series (or all matches played)
    #   declined → guest explicitly declined
    #   expired  → invite not accepted in time
    status = Column(String(20), default="pending", index=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    accepted_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)  # invite expiry
    completed_at = Column(DateTime, nullable=True)

    # Series score (denormalized)
    user1_wins = Column(Integer, default=0)
    user2_wins = Column(Integer, default=0)
    winner_id = Column(Integer, nullable=True)  # null = ongoing OR drawn

    user1 = relationship("User", foreign_keys=[user1_id])
    user2 = relationship("User", foreign_keys=[user2_id])
    league = relationship("ChallengeLeague")
    host_team = relationship("ChallengeTeam", foreign_keys=[host_team_id])
    guest_team = relationship("ChallengeTeam", foreign_keys=[guest_team_id])

    __table_args__ = (
        Index("ix_cl_tours_user1_status", "user1_id", "status"),
        Index("ix_cl_tours_user2_status", "user2_id", "status"),
    )


class CLTourMatch(Base):
    """A single match within a Challenge League Tour."""
    __tablename__ = "cl_tour_matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cl_tour_id = Column(Integer, ForeignKey("cl_tours.id"), nullable=False, index=True)
    match_number = Column(Integer, nullable=False)  # 1, 2, 3, ...
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=True)  # null until played

    # Status:
    #   pending → not started yet
    #   playing → a /cipl draft is live for this match
    #   done    → match completed
    status = Column(String(20), default="pending")

    winner_id = Column(Integer, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("cl_tour_id", "match_number",
                         name="uq_cl_tour_matches_tour_match_number"),
        Index("ix_cl_tour_matches_tour", "cl_tour_id", "match_number"),
    )


class PlayerMatchStats(Base):
    """Per-player per-match stats — written at match-end so we can compute
    tour aggregates (most runs in a tour, etc.).

    This is in addition to the cumulative PlayerGameStats. PlayerGameStats
    is the lifetime total; PlayerMatchStats is the single-match snapshot.
    """
    __tablename__ = "player_match_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id", ondelete="CASCADE"), nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Batting (this match)
    bat_runs = Column(Integer, default=0)
    bat_balls = Column(Integer, default=0)
    bat_fours = Column(Integer, default=0)
    bat_sixes = Column(Integer, default=0)
    bat_out = Column(Boolean, default=False)

    # Bowling (this match)
    bowl_wickets = Column(Integer, default=0)
    bowl_runs = Column(Integer, default=0)
    bowl_balls = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_pms_match_user", "match_id", "user_id"),
    )


class EventMedia(Base):
    """Media (GIF/animation) shown during matches for specific events.

    Admin uploads or links GIFs for each event_key. When the matching in-match
    event fires, a random enabled item (weighted) is picked and sent via
    Telegram's send_animation API.

    event_key values: see services.event_media_service.EVENT_KEYS
    source_type:
      'url'  → source is a public URL (Telegram fetches it directly)
      'file' → source is a path/filename under data/event_media/
    """
    __tablename__ = "event_media"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(30), nullable=False, index=True)
    source_type = Column(String(10), default="url")
    source = Column(Text, nullable=False)
    label = Column(String(120), nullable=True)
    weight = Column(Integer, default=1)
    enabled = Column(Boolean, default=True)
    # Admin-written message sent with the media (HTML). Supports {player},
    # {team}, {runs} and friends — substituted by event_media_service.render_caption,
    # which uses str.replace rather than str.format because admin text may
    # contain stray braces. NULL/empty keeps the original silent-GIF behaviour.
    caption = Column(Text, nullable=True)
    duration_ms = Column(Integer, default=3000)
    # MiniApp display controls. By default the browser sizes the box from the
    # uploaded GIF dimensions; fixed_16_9 is an admin-selected fallback mode.
    size_mode = Column(String(20), default="original")
    original_width = Column(Integer, nullable=True)
    original_height = Column(Integer, nullable=True)
    max_mobile_width = Column(Integer, default=440)
    media_type = Column(String(10), default="image")
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    uploaded_by = Column(String(80), nullable=True)


class EventSound(Base):
    """Audio played in the Crickidex Arena Mini App for gameplay events.

    One row per sound_key at most; a missing row (or source_type 'default')
    means the committed default file under static/cricket/sounds/ is served.
    Admin uploads replace a key's audio without touching the repo files.

    sound_key values: see services.event_sound_service.SOUND_KEYS
    source_type:
      'default'  → serve the committed default file (source unused)
      'telegram' → source is a Telegram file_id (storage-channel upload)
      'url'      → source is a public URL (client is redirected to it)
      'file'     → source is a filename under data/event_media/sounds/
    """
    __tablename__ = "event_sounds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sound_key = Column(String(30), nullable=False, unique=True, index=True)
    source_type = Column(String(10), default="default")
    source = Column(Text, nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)
    volume = Column(Integer, default=100)  # percent, 0-100
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(String(80), nullable=True)


class GSpinReward(Base):
    """Admin-configurable rewards for the Lucky Card wheel (/gspin).

    Each enabled row is a possible outcome. Probability of any outcome is
    `weight / sum(enabled weights)`. Inactive rows are skipped.

    ``weight`` is a float so the website can express it directly as a
    percentage down to 0.00001% (1 in 10,000,000) — enter values that sum to
    100 and each weight reads as its own probability. A weight of 0 parks a
    reward without deleting it.

    reward_type values:
      'coins'        → random in amount_min..amount_max
      'gems'         → random in amount_min..amount_max
      'quest_points' → random in amount_min..amount_max
      'player'       → random player in player_rating_min..max
      'pack'         → grants pack_id to user's inventory
    """
    __tablename__ = "gspin_rewards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    label = Column(String(60), nullable=False)
    emoji = Column(String(10), nullable=True)
    color = Column(String(7), default="888888")
    weight = Column(Float, default=10, nullable=False)
    sort_order = Column(Integer, default=100)
    enabled = Column(Boolean, default=True, nullable=False)
    reward_type = Column(String(20), nullable=False)
    amount_min = Column(Integer, default=0)
    amount_max = Column(Integer, default=0)
    player_rating_min = Column(Integer, default=0)
    player_rating_max = Column(Integer, default=0)
    pack_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BotCommand(Base):
    """Admin-editable metadata for each registered bot command.

    The command's slash name (`/claim`, `/daily`) and code are static — only
    metadata like description, cooldown_seconds, and enabled toggle are
    editable. Command handlers read these at runtime; the bot does NOT
    re-register command names from this table.

    If `enabled` is False, the command handler refuses with a polite message.
    If `cooldown_seconds` is > 0 and set, it overrides the config constant
    for that command.

    Reward parameters (coin amounts, etc.) live in CommandReward — linked
    one-to-one to BotCommand by command_key.
    """
    __tablename__ = "bot_commands"

    id = Column(Integer, primary_key=True, autoincrement=True)
    command_key = Column(String(40), nullable=False, unique=True, index=True)
    display_name = Column(String(60), nullable=False)
    aliases = Column(String(200), nullable=True)  # comma-separated
    description = Column(String(500), nullable=True)
    category = Column(String(30), default="general")
    enabled = Column(Boolean, default=True, nullable=False)
    cooldown_seconds = Column(Integer, default=0)  # 0 = use code default
    sort_order = Column(Integer, default=100)
    created_at = Column(DateTime, default=datetime.utcnow)


class CommandReward(Base):
    """Reward configuration for a specific BotCommand.

    Currently supports the simple-reward commands: claim, daily, debut, etc.
    For each, admin can set:
      - coin_amount (or min/max range)
      - gem_amount (or min/max range)
      - quest_points (or min/max range)
      - player_count (how many players are granted)
      - player_rating_min / max (rating band for granted players)

    Match rewards, trait costs etc. live in their own tables — not here.
    """
    __tablename__ = "command_rewards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    command_key = Column(String(40), nullable=False, unique=True, index=True)

    coin_amount = Column(Integer, default=0)
    coin_min = Column(Integer, default=0)
    coin_max = Column(Integer, default=0)

    gem_amount = Column(Integer, default=0)
    gem_min = Column(Integer, default=0)
    gem_max = Column(Integer, default=0)

    quest_points = Column(Integer, default=0)

    player_count = Column(Integer, default=0)
    player_rating_min = Column(Integer, default=0)
    player_rating_max = Column(Integer, default=0)

    # Bonus on milestone (e.g. daily streak)
    milestone_bonus_coins = Column(Integer, default=0)
    milestone_bonus_gems = Column(Integer, default=0)
    milestone_bonus_player_min = Column(Integer, default=0)
    milestone_bonus_player_max = Column(Integer, default=0)
    milestone_every_n = Column(Integer, default=0)  # e.g. 14 for daily streak

    notes = Column(String(500), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Bowlout(Base):
    """Bowl-out tiebreaker — spawned from a tied Match, or standalone /pbo."""
    __tablename__ = "bowlouts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, nullable=True)
    chat_id = Column(BigInteger, nullable=True)
    user1_id = Column(Integer, nullable=False)
    user2_id = Column(Integer, nullable=False)
    user1_team_name = Column(String(80), default="Team 1")
    user2_team_name = Column(String(80), default="Team 2")
    status = Column(String(20), default="pending_pick")
    current_picker = Column(Integer, nullable=True)
    current_ball = Column(Integer, default=0)
    user1_hits = Column(Integer, default=0)
    user2_hits = Column(Integer, default=0)
    winner_user_id = Column(Integer, nullable=True)
    message_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class BowloutBall(Base):
    """One delivery in a Bowlout."""
    __tablename__ = "bowlout_balls"
    id = Column(Integer, primary_key=True, autoincrement=True)
    bowlout_id = Column(Integer, nullable=False, index=True)
    ball_index = Column(Integer, nullable=False)
    bowler_user_id = Column(Integer, nullable=False)
    bowler_player_id = Column(Integer, nullable=True)
    bowler_name = Column(String(120), nullable=False)
    bowler_rating = Column(Integer, default=70)
    is_hit = Column(Boolean, default=False)
    bowled_at = Column(DateTime, default=datetime.utcnow)


class UserReport(Base):
    __tablename__ = "user_reports"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_read = Column(Boolean, default=False, nullable=False)
    is_resolved = Column(Boolean, default=False, nullable=False)
    admin_response = Column(Text, nullable=True)
    replied_at = Column(DateTime, nullable=True)
    replied_by = Column(String(80), nullable=True)


class ShotProbability(Base):
    __tablename__ = "shot_probabilities"
    id = Column(Integer, primary_key=True, autoincrement=True)
    shot_name = Column(String(40), nullable=False, unique=True, index=True)
    mod_dot = Column(Float, default=0.0)
    mod_1 = Column(Float, default=0.0)
    mod_2 = Column(Float, default=0.0)
    mod_3 = Column(Float, default=0.0)
    mod_4 = Column(Float, default=0.0)
    mod_6 = Column(Float, default=0.0)
    mod_wicket = Column(Float, default=0.0)
    mod_extras = Column(Float, default=0.0)
    description = Column(String(200), nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BotChat(Base):
    __tablename__ = "bot_chats"
    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False, unique=True, index=True)
    chat_type = Column(String(20), default="group")
    title = Column(String(255), nullable=True)
    username = Column(String(80), nullable=True)
    member_count = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    joined_at = Column(DateTime, default=datetime.utcnow)
    left_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    # Welcome message for new members (per-group toggle via /ewm /dwm)
    welcome_enabled = Column(Boolean, default=True, nullable=False)


class ChatMember(Base):
    """Which managers belong to which group — the roster behind /owners.

    Telegram gives a bot no way to list a group's members, so membership is
    *learned* from activity: the first time a debuted user is seen in a group,
    ``services.chat_tracker.record_chat_member`` writes a row here (throttled,
    so an active group costs one write per member per few hours). Joining via
    ``new_chat_members`` marks a row active immediately; ``left_chat_member``
    flips it off.

    This is a best-effort view of a group — a member who has never spoken since
    the bot arrived is unknown to us — which is why /owners always reports the
    group figure as "of N known members".
    """
    __tablename__ = "chat_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_chat_member_unique", "chat_id", "user_id", unique=True),
        Index("ix_chat_member_active", "chat_id", "is_active"),
    )


class Broadcast(Base):
    __tablename__ = "broadcasts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    message = Column(Text, nullable=False)
    target_type = Column(String(20), default="all")
    sent_at = Column(DateTime, default=datetime.utcnow)
    sent_by = Column(String(80), nullable=True)
    sent_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    status = Column(String(20), default="pending")
    attachment_type = Column(String(20), nullable=True)
    attachment_name = Column(String(255), nullable=True)


class PendingUndo(Base):
    """Single-row-per-user state for /cmuundo command (60s window)."""
    __tablename__ = "pending_undos"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, unique=True, index=True)
    action_type = Column(String(20), nullable=False)  # 'buy' | 'release'
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class AdReward(Base):
    """Records ad-watch postbacks received from the ad network's server.

    Adsgram fires GET /api/ads/reward?userid=<telegram_id> after a user
    finishes watching a rewarded ad. We log it here so the /api/webapp/spin
    endpoint can validate "this user really watched an ad in the last few
    minutes" before granting the spin.

    consumed_at is set when the user actually claims a spin using this
    postback, preventing replay.

    """
    __tablename__ = "adsgram_rewards"
    id = Column(Integer, primary_key=True, autoincrement=True)
    # BigInteger, not Integer: Telegram ids have already passed 2^31, and an
    # INTEGER column rejects them with "integer out of range", failing the
    # postback and silently costing the user their ad reward.
    telegram_id = Column(BigInteger, nullable=False, index=True)
    received_at = Column(DateTime, default=datetime.utcnow,
                         nullable=False, index=True)
    consumed_at = Column(DateTime, nullable=True)
    # Which network sent it. Always "adsgram"; the column is kept rather than
    # dropped because a live database has it and removing it buys nothing.
    provider = Column(String(20), nullable=True)
    # Optional fields for debugging — IP and full query string Adsgram sent
    source_ip = Column(String(64), nullable=True)
    query_string = Column(String(500), nullable=True)


# Legacy name kept so older imports (and any pickled references) keep resolving.
AdsgramReward = AdReward



class ActivityLogArchive(Base):
    """Pointer to an archived batch of activity_log rows that have been
    exported to the Telegram storage channel and deleted from Neon.

    Each row represents one archive file (JSON dump). The actual log data
    lives in a Telegram message identified by `tg_file_id`. To read old
    archived logs, admins can fetch the file from Telegram on demand.
    """
    __tablename__ = "activity_log_archive"

    id = Column(Integer, primary_key=True, autoincrement=True)
    range_start = Column(DateTime, nullable=False, index=True)
    range_end = Column(DateTime, nullable=False, index=True)
    row_count = Column(Integer, default=0, nullable=False)
    tg_file_id = Column(String(200), nullable=False)
    tg_message_id = Column(Integer, nullable=True)
    filename = Column(String(200), nullable=True)
    archived_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    archived_by = Column(String(100), nullable=True)


# ── Referral / invite system ─────────────────────────────────────────

class ReferralCompetition(Base):
    """A time-bound invite contest. Admin creates these from /competitions.

    Only one should be active at a time; the active one is the destination
    for new referrals during its window. Past competitions stay in the DB
    for leaderboard history.
    """
    __tablename__ = "referral_competitions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    start_date = Column(DateTime, nullable=False, index=True)
    end_date = Column(DateTime, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    # Rewards (coins) — admin-tunable. 0 = disabled for that slot.
    prize_top1 = Column(Integer, default=0, nullable=False)
    prize_top2 = Column(Integer, default=0, nullable=False)
    prize_top3 = Column(Integer, default=0, nullable=False)
    # Per-invite reward (coins) given to inviter when invitee completes debut.
    # 0 means no per-invite reward, only the top-N prizes at competition end.
    prize_per_invite = Column(Integer, default=0, nullable=False)
    # Per-invite GEM reward (alternative or additive to coins).
    prize_per_invite_gems = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(100), nullable=True)
    # Set when admin clicks "Declare winners" — prevents double-paying.
    winners_announced_at = Column(DateTime, nullable=True)
    winner_top1_user_id = Column(Integer, nullable=True)
    winner_top2_user_id = Column(Integer, nullable=True)
    winner_top3_user_id = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)


class Referral(Base):
    """A single invite event. Created when an invitee starts the bot via a
    referral deep-link. `completed_at` is set when the invitee finishes
    /debut — that's when the invite "counts" for the leaderboard and any
    per-invite reward is paid out.
    """
    __tablename__ = "referrals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    inviter_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    invitee_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                             nullable=False, unique=True, index=True)
    # Which competition the invite is attributed to (None if no active comp
    # at the time of invite — still recorded for history).
    competition_id = Column(Integer,
                            ForeignKey("referral_competitions.id",
                                       ondelete="SET NULL"),
                            nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    # Set when invitee completes /debut
    completed_at = Column(DateTime, nullable=True, index=True)
    # Admin can mark as invalid (suspected abuse) — invalid rows don't count
    # toward the leaderboard but stay for audit history.
    is_valid = Column(Boolean, default=True, nullable=False)
    # Set when per-invite reward was paid out (prevents double-pay)
    reward_paid_at = Column(DateTime, nullable=True)


class CompetitionTemplate(Base):
    """Reusable prize-structure template for ReferralCompetition.

    Admin saves a template once (e.g. "Monthly Standard") and applies it
    to new competitions to skip filling all 5 prize fields. Values are
    copied at apply-time — editing a template does NOT change competitions
    that were created from it.

    `duration_days` is optional: when set, applying the template can
    auto-compute end_date from start_date. If None, admin sets end_date
    manually.
    """
    __tablename__ = "competition_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False, unique=True)
    description = Column(String(300), nullable=True)
    duration_days = Column(Integer, nullable=True)
    prize_top1 = Column(Integer, default=0, nullable=False)
    prize_top2 = Column(Integer, default=0, nullable=False)
    prize_top3 = Column(Integer, default=0, nullable=False)
    prize_per_invite = Column(Integer, default=0, nullable=False)
    prize_per_invite_gems = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(100), nullable=True)
    # Soft order in dropdowns
    sort_order = Column(Integer, default=100, nullable=False)


# ══════════════════════════════════════════════════════════════════════
# GIVEAWAYS
# ══════════════════════════════════════════════════════════════════════

class Giveaway(Base):
    """An admin-created prize giveaway.

    Created from the admin website with an optional image banner. The giveaway
    is announced to every chat the bot is in when its ``start_time`` passes;
    users join by tapping a "Participate" button (only if they are members of
    the Official GC). When ``end_time`` passes, ``num_winners`` random winners
    are drawn, the prize is granted, and results are posted in the Official GC.

    All times are stored in **UTC**. The admin enters them in IST on the website
    (converted with the −5:30 offset, matching the rest of the codebase).

    prize_type values:
      'coins' / 'gems' / 'quest_points' → each winner gets ``prize_amount``.
      'player'                          → each winner gets ``prize_player_id``.

    status: scheduled → running → ended (or cancelled).
    """
    __tablename__ = "giveaways"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(120), nullable=False)
    prize_type = Column(String(20), nullable=False)   # coins|gems|quest_points|player
    prize_amount = Column(Integer, default=0)          # for currency prizes
    prize_player_id = Column(Integer, ForeignKey("players.id"), nullable=True)
    num_winners = Column(Integer, default=1, nullable=False)
    start_time = Column(DateTime, nullable=False, index=True)  # UTC
    end_time = Column(DateTime, nullable=False, index=True)    # UTC
    status = Column(String(16), default="scheduled", nullable=False, index=True)
    image_file_id = Column(String(300), nullable=True)  # durable Telegram file_id of banner
    announce_target = Column(String(16), default="groups")  # groups|all
    # Optional alt-account gate — when True, only users with prior game activity
    # (matches_played >= 1) may join. Default off to keep entry frictionless.
    require_min_activity = Column(Boolean, default=False, nullable=False)
    announced_at = Column(DateTime, nullable=True)
    winners_drawn_at = Column(DateTime, nullable=True)  # double-draw guard
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    notes = Column(Text, nullable=True)

    prize_player = relationship("Player")


class GiveawayEntry(Base):
    """One user's entry into a giveaway.

    The unique ``(giveaway_id, user_id)`` index is the single source of truth
    that a user can participate at most once — inserts race safely against it,
    so concurrent taps / replays collapse to one row.
    """
    __tablename__ = "giveaway_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    giveaway_id = Column(Integer, ForeignKey("giveaways.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    # Denormalized so we can DM the winner even if the User row changes later.
    telegram_id = Column(BigInteger, nullable=False)
    joined_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_winner = Column(Boolean, default=False, nullable=False)
    won_at = Column(DateTime, nullable=True)
    prize_detail = Column(String(200), nullable=True)  # e.g. "50,000 coins" / "Virat Kohli (91)"
    # Admin-set guaranteed winner. Priority entries are seated first at the draw
    # (in the order they were marked), and the remaining slots are filled at
    # random from everyone else — so marking a participant means they win,
    # provided they are still eligible when the draw runs (not banned, still in
    # the Official GC). See services.giveaway_service.draw_winners.
    is_priority = Column(Boolean, default=False, nullable=False)
    priority_set_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_giveaway_entry_uniq", "giveaway_id", "user_id", unique=True),
    )


# ══════════════════════════════════════════════════════════════════════
# MONTHLY SEASON + EVENTS
# ══════════════════════════════════════════════════════════════════════

class Season(Base):
    """A monthly competitive season. One row per month ('YYYY-MM').

    The live season is the one matching the current month. When the month
    rolls over, the old season is finalized (top players paid, standings
    archived into SeasonStanding) and a new Season row is created.
    """
    __tablename__ = "seasons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season_key = Column(String(7), nullable=False, unique=True, index=True)  # 'YYYY-MM'
    name = Column(String(80), nullable=True)  # e.g. "May 2026 Season"
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    finalized = Column(Boolean, default=False, nullable=False)
    # Prize config (coins) for top finishers
    prize_top1 = Column(Integer, default=50000)
    prize_top2 = Column(Integer, default=25000)
    prize_top3 = Column(Integer, default=10000)
    prize_top10 = Column(Integer, default=2000)   # paid to ranks 4-10
    prize_gems_top1 = Column(Integer, default=20)
    prize_gems_top2 = Column(Integer, default=10)
    prize_gems_top3 = Column(Integer, default=5)


class SeasonStanding(Base):
    """Archived final standings for a finalized season (top N snapshot)."""
    __tablename__ = "season_standings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season_key = Column(String(7), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    rank = Column(Integer, nullable=False)
    points = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    prize_coins = Column(Integer, default=0)
    prize_gems = Column(Integer, default=0)
    recorded_at = Column(DateTime, default=datetime.utcnow)


class Event(Base):
    """A time-limited game event (e.g. double coins weekend).

    event_type drives the effect:
      - 'coin_multiplier'  : multiply coin rewards by `multiplier`
      - 'pack_luck'        : (future) better pack odds
      - 'announcement'     : display-only banner, no mechanical effect
    """
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    emoji = Column(String(10), default="🎉")
    event_type = Column(String(40), default="coin_multiplier", nullable=False)
    multiplier = Column(Float, default=2.0)  # used by coin_multiplier
    start_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    end_at = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String(100), nullable=True)


# ══════════════════════════════════════════════════════════════════════
# CLUBS — social teams that compete on combined season points
# ══════════════════════════════════════════════════════════════════════

class Club(Base):
    """A player-formed club. Members' season points sum to the club's score.

    Club score is computed live from members (not stored), so it always
    reflects the current season and resets naturally each month.
    """
    __tablename__ = "clubs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(40), nullable=False)
    code = Column(String(8), nullable=False, unique=True, index=True)  # join code
    description = Column(String(160), nullable=True)
    emoji = Column(String(10), default="🛡️")
    founder_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    member_count = Column(Integer, default=1)
    max_members = Column(Integer, default=20)
    is_open = Column(Boolean, default=True, nullable=False)  # open-join vs code-only
    created_at = Column(DateTime, default=datetime.utcnow)
    # All-time aggregates (for flavor / club profile)
    total_seasons_won = Column(Integer, default=0)


class ClubSeasonResult(Base):
    """Archived club standing for a finalized season (top clubs snapshot)."""
    __tablename__ = "club_season_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season_key = Column(String(7), nullable=False, index=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False, index=True)
    club_name = Column(String(40), nullable=True)
    rank = Column(Integer, nullable=False)
    points = Column(Integer, default=0)
    member_count = Column(Integer, default=0)
    prize_coins_each = Column(Integer, default=0)
    recorded_at = Column(DateTime, default=datetime.utcnow)


class MatchScorecard(Base):
    """Persisted final scorecard for a completed match, so it can be viewed
    read-only later (via /recentmatches → Mini App). The live match_state is
    ephemeral and cleaned up after completion, so we snapshot it here."""
    __tablename__ = "match_scorecards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=False, unique=True, index=True)
    scorecard_json = Column(Text, nullable=False)  # full innings data as JSON
    result_text = Column(String(300), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MatchScorecardImage(Base):
    """One scorecard image of a match: the values it was drawn from, plus the
    Telegram ``file_id`` once the photo landed in the chat.

    The cards used to be a fire-and-forget render off live match state. If the
    render raised, the send timed out, or Telegram rate-limited the chat, the
    image was simply gone — the state it was built from is cleaned up minutes
    later, so there was nothing left to retry from and no way for the group to
    ask for it again. A row is written *before* the render for exactly that
    reason: the values outlive every failure downstream, so
    ``services.scorecard_delivery`` can redraw the card on demand.

    ``file_id`` is filled in once Telegram accepts the photo, which makes every
    later re-send free (no PIL render, no upload). ``chat_id`` is the chat the
    match was played in, so "the last scorecard of this group" is one indexed
    lookup rather than a walk back through the matches table.
    """
    __tablename__ = "match_scorecard_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=True, index=True)
    # 1 or 2 for an innings card; 0 for a whole-match card (the summary).
    innings = Column(Integer, nullable=False, default=0)
    # "batting" | "bowling" | "summary" — see scorecard_delivery.CARD_TYPES.
    card_type = Column(String(20), nullable=False)
    # Delivery order within a match, so a re-send reproduces the original
    # sequence (bat 1, bowl 1, bat 2, bowl 2, summary).
    sort_order = Column(Integer, nullable=False, default=0)
    caption = Column(String(300), nullable=True)
    # Render-ready keyword arguments for the generator named by ``card_type``.
    # Deliberately excludes the admin-tunable accent/text settings: those are
    # re-read live at render time so a redraw follows the current theme.
    payload_json = Column(Text, nullable=False)
    file_id = Column(String(200), nullable=True)
    # True once the image actually reached the match chat. A row that stays
    # False is the durable record of a card the group never saw — the thing
    # "the scorecard didn't come" reports used to leave no trace of.
    # /lastscorecard reads it to say how many cards it is repairing.
    delivered = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_scorecard_images_card", "match_id", "innings", "card_type",
              unique=True),
        Index("ix_scorecard_images_chat", "chat_id", "match_id"),
    )


# ══════════════════════════════════════════════════════════════════════
# CHALLENGE MODES — admin-managed Modes > Leagues > Teams > Players
# ══════════════════════════════════════════════════════════════════════

class ChallengeMode(Base):
    """Top-level challenge mode grouping for league/team/player data."""
    __tablename__ = "challenge_modes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    sort_order = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    leagues = relationship("ChallengeLeague", back_populates="mode", cascade="all, delete-orphan")


class ChallengeLeague(Base):
    """Admin-created challenge league command metadata."""
    __tablename__ = "challenge_leagues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode_id = Column(Integer, ForeignKey("challenge_modes.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    short_code = Column(String(30), nullable=True, index=True)
    command = Column(String(60), nullable=True, index=True)
    # Official tournament command for this league (e.g. "/cipl_tournament"). When a
    # match is started with this command it is recognised as a tournament match.
    tournament_command = Column(String(60), nullable=True, index=True)
    # Public, read-only tournament info command for this league (e.g.
    # "/iplfixtures"). Anyone may run it; it opens the active tournament's
    # hub card (overview / points table / fixtures / teams). Kept separate from
    # ``tournament_command`` because that one *starts* a match and is gated.
    fixtures_command = Column(String(60), nullable=True, index=True)
    image_url = Column(String(500), nullable=True)
    sort_order = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    same_team_allowed = Column(Boolean, default=True, nullable=False)
    # Overseas-player rules. ``home_country`` (when set) auto-flags any player
    # whose country differs from it as overseas when added to a team. The XI
    # picker then enforces ``min_overseas``/``max_overseas`` (11 = no cap).
    home_country = Column(String(60), nullable=True)
    min_overseas = Column(Integer, default=0, nullable=False)
    max_overseas = Column(Integer, default=11, nullable=False)
    # Match format for league play: "T20" (20 overs x 6 balls) or "The100"
    # (The Hundred -- 100 balls as 20 sets of 5). Drives the ball-by-ball engine
    # in services/cipl_match.py via the JSON match-state's ``ball_format`` key.
    # server_default keeps freshly created schemas aligned with migrated ones.
    match_format = Column(String(20), default="T20", server_default="T20",
                          nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    mode = relationship("ChallengeMode", back_populates="leagues")
    teams = relationship("ChallengeTeam", back_populates="league", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_challenge_league_mode_name", "mode_id", "name", unique=True),
    )


class ChallengeTeam(Base):
    """Team roster container inside a challenge league."""
    __tablename__ = "challenge_teams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    league_id = Column(Integer, ForeignKey("challenge_leagues.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    short_name = Column(String(30), nullable=True, index=True)
    logo_url = Column(String(500), nullable=True)
    primary_color = Column(String(9), nullable=True)
    secondary_color = Column(String(9), nullable=True)
    sort_order = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    league = relationship("ChallengeLeague", back_populates="teams")
    players = relationship("ChallengePlayer", back_populates="team", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_challenge_team_league_name", "league_id", "name", unique=True),
    )


class ChallengePlayer(Base):
    """Player assignment inside a challenge team, linked to master Player data."""
    __tablename__ = "challenge_players"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("challenge_teams.id", ondelete="CASCADE"), nullable=False, index=True)
    source_player_id = Column(Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String(150), nullable=False)
    details_json = Column(Text, nullable=True)
    # Counts toward the league's overseas-in-XI limit. Defaulted from the
    # league's home_country at add time; admin can toggle it per player.
    is_overseas = Column(Boolean, default=False, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    team = relationship("ChallengeTeam", back_populates="players")
    source_player = relationship("Player")

    __table_args__ = (
        Index("ix_challenge_player_team_name", "team_id", "name", unique=True),
    )


class UserTeamLastXI(Base):
    """Remembers a user's last confirmed Playing XI for a single challenge team.

    Keyed by Telegram id + ``team_id`` (a league-scoped ``ChallengeTeam.id``), so a
    user accumulates one saved XI per team they have captained — independently across
    leagues. Re-confirming the same team overwrites only that team's row, giving the
    "last XI" semantics. ``player_ids`` is a JSON list of ``ChallengePlayer`` ids in
    batting order.
    """
    __tablename__ = "user_team_last_xi"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_tg_id = Column(BigInteger, nullable=False, index=True)
    team_id = Column(Integer, ForeignKey("challenge_teams.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    player_ids = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_user_team_last_xi_user_team", "user_tg_id", "team_id", unique=True),
    )


# ══════════════════════════════════════════════════════════════════════
# CHALLENGE LEAGUE TOURNAMENTS — admin-run structured competitions
# ══════════════════════════════════════════════════════════════════════


class Tournament(Base):
    """A tournament of one competition family — see ``kind`` below.

    ``kind='challenge'`` is a Challenge League Tournament, contested by
    ``ChallengeTeam`` squads. ``kind='letsplay'`` is a Lets Play Tournament,
    contested by Telegram users playing their own rosters. One tournament **per
    kind** may be ``is_active`` at a time, so one of each can run side by side.
    """
    __tablename__ = "tournaments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # League this tournament belongs to. SET NULL (plus the name snapshot) keeps
    # completed-tournament history viewable even if the league is later removed.
    league_id = Column(Integer, ForeignKey("challenge_leagues.id", ondelete="SET NULL"),
                       nullable=True, index=True)
    league_name = Column(String(120), nullable=True)
    command_snapshot = Column(String(60), nullable=True)

    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)

    # Which competition family this tournament belongs to:
    #   "challenge" – Challenge League teams (/cipl & friends). ``league_id`` is
    #                 set and each TournamentTeam wraps a ``ChallengeTeam``.
    #   "letsplay"  – Lets Play tournament. There is no league; each
    #                 TournamentTeam *is* a user, keyed by ``user_tg_id``, and
    #                 matches are played with the users' own rosters via /lptour.
    # Rows written before this column existed are NULL and read as "challenge"
    # (see ``services.tournament_service.KIND_CHALLENGE``).
    kind = Column(String(20), default="challenge", server_default="challenge",
                  nullable=False, index=True)

    # Lifecycle: draft / scheduled / active / paused / completed / cancelled
    status = Column(String(20), default="draft", nullable=False, index=True)
    # The single currently-selected tournament whose command is live — one per
    # ``kind``, so a Challenge League tournament and a Lets Play tournament can
    # run side by side. Activating one deactivates the others of the same kind.
    is_active = Column(Boolean, default=False, nullable=False, index=True)

    format = Column(String(40), default="League", nullable=False)
    overs = Column(Integer, default=20, nullable=False)
    max_teams = Column(Integer, default=8, nullable=False)

    # League-stage structure (the new driver; legacy ``format`` is kept untouched).
    #   single_rr  – one round-robin across all teams
    #   double_rr  – home/away round-robin across all teams
    #   groups     – teams split into TournamentGroups, each round-robin internally
    league_format = Column(String(20), default="single_rr", nullable=False)
    # When ``league_format == 'groups'``: keep one points table per group
    # ("separate") or roll every group into a single combined table ("combined").
    group_points_mode = Column(String(20), default="separate", nullable=False)
    # True once a fixture schedule has been generated. While true, the bot only
    # lets two teams play if an uncompleted scheduled fixture exists for the pair.
    schedule_generated = Column(Boolean, default=False, nullable=False)

    # Knockout / playoff configuration (Phase 2).
    #   top4_sf | ipl_playoffs | groups_top2_sf | groups_top4_qf | custom
    knockout_type = Column(String(30), nullable=True)
    knockout_config_json = Column(Text, nullable=True)
    knockout_generated = Column(Boolean, default=False, nullable=False)

    # Points rules
    points_win = Column(Integer, default=2, nullable=False)
    points_tie = Column(Integer, default=1, nullable=False)
    points_loss = Column(Integer, default=0, nullable=False)
    points_no_result = Column(Integer, default=1, nullable=False)

    # Leaderboard qualification minimums (balls faced / balls bowled)
    min_balls_for_sr = Column(Integer, default=20, nullable=False)
    min_balls_for_econ = Column(Integer, default=12, nullable=False)

    # ── Tournament rules that override the league's own settings ──────────
    # Overseas-in-XI limits for *this* tournament. NULL means "inherit the
    # league's ``min_overseas`` / ``max_overseas``", so an existing tournament
    # keeps behaving exactly as it did before these columns existed. An
    # explicit 0 is a real value ("no overseas allowed"), which is why these
    # are nullable rather than defaulted.
    min_overseas = Column(Integer, nullable=True)
    max_overseas = Column(Integer, nullable=True)

    # Team ownership. When true, a participating team may only be picked by the
    # Telegram user set as its owner (``TournamentTeam.owner_tg_id``) — this is
    # what makes a draft-style tournament work, where each franchise belongs to
    # one person. Teams left without an owner stay open to anyone.
    enforce_team_owner = Column(Boolean, default=False, nullable=False)

    # How the surface for a tournament match is decided:
    #   "host"    – the host picks it during setup (the original behaviour)
    #   "fixture" – each fixture carries its own ``pitch_type``, assigned when
    #               the schedule is generated; nobody may change it
    #   "home"    – same as "fixture", but the generator seeds each fixture from
    #               the home team's ``home_pitch`` where one is set
    pitch_mode = Column(String(20), default="host", server_default="host",
                        nullable=False)

    # ── Injuries ──────────────────────────────────────────────────────────
    # A cricket injury system, off by default. When on, a completed tournament
    # match can leave a player carrying a knock that rules them out of their
    # team's next few tournament matches — they cannot be picked in the XI until
    # they are fit again. See ``services.injury_service``.
    injuries_enabled = Column(Boolean, default=False, nullable=False)
    # Percentage chance, per team per completed match, that somebody picks up an
    # injury. 0 turns generation off while leaving existing injuries in force.
    injury_chance = Column(Integer, default=12, nullable=False)
    # The hard ceiling on how many matches a single injury may rule a player out
    # for. Clamped to 1..5 when read; 3 is the default the severity ladder is
    # built around.
    injury_max_matches = Column(Integer, default=3, nullable=False)

    activated_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    league = relationship("ChallengeLeague")
    teams = relationship("TournamentTeam", back_populates="tournament",
                         cascade="all, delete-orphan")
    groups = relationship("TournamentGroup", back_populates="tournament",
                          cascade="all, delete-orphan")
    matches = relationship("TournamentMatch", back_populates="tournament",
                           cascade="all, delete-orphan")
    player_stats = relationship("TournamentPlayerStats", back_populates="tournament",
                                cascade="all, delete-orphan")
    injuries = relationship("TournamentInjury", back_populates="tournament",
                            cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_tournament_status_active", "status", "is_active"),
    )


class TournamentGroup(Base):
    """A group inside a ``groups``-format tournament (e.g. "Group A").

    Each group is round-robin internally; ``rr_mode`` decides single vs double.
    Teams are linked via ``TournamentTeam.group_id``.
    """
    __tablename__ = "tournament_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tournament_id = Column(Integer, ForeignKey("tournaments.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    name = Column(String(60), nullable=False)
    rr_mode = Column(String(10), default="single", nullable=False)  # single | double
    sort_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    tournament = relationship("Tournament", back_populates="groups")

    __table_args__ = (
        Index("ix_tournament_group_unique", "tournament_id", "name", unique=True),
    )


class TournamentTeam(Base):
    """A team participating in a tournament, with its running standings.

    Exactly one identity column is set, depending on ``Tournament.kind``:
    ``challenge_team_id`` for a Challenge League tournament, ``user_tg_id`` for a
    Lets Play tournament (where the team *is* a Telegram user, playing their own
    roster). Everything downstream — standings, net run-rate, the schedule
    generator and the knockout bracket — works off ``TournamentTeam.id`` alone
    and so is shared by both kinds.
    """
    __tablename__ = "tournament_teams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tournament_id = Column(Integer, ForeignKey("tournaments.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    challenge_team_id = Column(Integer, ForeignKey("challenge_teams.id", ondelete="SET NULL"),
                               nullable=True, index=True)
    # Lets Play tournaments: the Telegram id of the user who *is* this team. Kept
    # as the identity (rather than ``users.id``) so an admin can enter a squad
    # before that person has ever run /debut. NULL for Challenge League teams.
    user_tg_id = Column(BigInteger, nullable=True, index=True)
    # Group assignment for ``groups``-format tournaments; NULL otherwise.
    group_id = Column(Integer, ForeignKey("tournament_groups.id", ondelete="SET NULL"),
                      nullable=True, index=True)
    name = Column(String(120), nullable=False)
    short_name = Column(String(30), nullable=True)
    logo_url = Column(String(500), nullable=True)

    # ── Ownership ─────────────────────────────────────────────────────────
    # The Telegram id of the person who owns this franchise. When the
    # tournament has ``enforce_team_owner`` set, only this user may pick the
    # team in the team picker — everyone else is refused. NULL means the team
    # is unowned and stays open to anybody. Telegram id (not ``users.id``) for
    # the same reason ``user_tg_id`` is: an admin assigns owners from a list of
    # ids, and some of those people have never run /debut.
    owner_tg_id = Column(BigInteger, nullable=True, index=True)
    owner_name = Column(String(120), nullable=True)
    # Extra Telegram ids allowed to play this team, as a JSON list — a franchise
    # can be run by more than one person. Mirrors ``DraftTeam.co_owner_ids_json``
    # (and is inherited from it when the league came from a draft). A co-owner is
    # the owner's equal for every check the tournament makes; the owner is only
    # distinguished by being the name shown on the team.
    co_owner_ids_json = Column(Text, nullable=True)

    # This team's home surface. Used by the schedule generator when the
    # tournament's ``pitch_mode`` is "home": every fixture the team hosts is
    # played on it. NULL falls back to a random surface.
    home_pitch = Column(String(20), nullable=True)

    # Standings
    played = Column(Integer, default=0, nullable=False)
    won = Column(Integer, default=0, nullable=False)
    lost = Column(Integer, default=0, nullable=False)
    tied = Column(Integer, default=0, nullable=False)
    no_result = Column(Integer, default=0, nullable=False)
    # Points as the table shows them: the points earned from recorded matches
    # PLUS ``points_adjust``. Never written by hand — ``recompute_standings``
    # rebuilds it from the matches every time one is recorded or removed.
    points = Column(Integer, default=0, nullable=False)

    # ── Manual points adjustment ──────────────────────────────────────────
    # A deduction or award an admin applies on top of what the results earned:
    # −2 for a slow over rate, +2 for a walkover, and so on. It lives in its own
    # column precisely because ``points`` is derived — a hand-edit of ``points``
    # would be wiped by the next recompute, while this survives and is re-applied
    # every time. ``points_adjust_note`` is the reason, shown wherever the
    # adjustment is (so a table nobody can explain never appears).
    points_adjust = Column(Integer, default=0, server_default="0", nullable=False)
    points_adjust_note = Column(String(200), nullable=True)

    # Net run-rate data
    runs_for = Column(Integer, default=0, nullable=False)
    balls_for = Column(Integer, default=0, nullable=False)
    runs_against = Column(Integer, default=0, nullable=False)
    balls_against = Column(Integer, default=0, nullable=False)

    sort_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    tournament = relationship("Tournament", back_populates="teams")

    # Both unique indexes are partial in effect rather than in DDL: SQL treats
    # NULLs as distinct, so the challenge index ignores Lets Play rows (NULL
    # challenge_team_id) and the user index ignores Challenge League rows.
    __table_args__ = (
        Index("ix_tournament_team_unique", "tournament_id", "challenge_team_id", unique=True),
        Index("ix_tournament_team_user_unique", "tournament_id", "user_tg_id", unique=True),
    )


class TournamentMatch(Base):
    """A recorded match played within a tournament."""
    __tablename__ = "tournament_matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tournament_id = Column(Integer, ForeignKey("tournaments.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    match_id = Column(Integer, ForeignKey("matches.id", ondelete="SET NULL"),
                      nullable=True, index=True)
    team1_id = Column(Integer, ForeignKey("tournament_teams.id", ondelete="SET NULL"), nullable=True)
    team2_id = Column(Integer, ForeignKey("tournament_teams.id", ondelete="SET NULL"), nullable=True)
    winner_team_id = Column(Integer, ForeignKey("tournament_teams.id", ondelete="SET NULL"), nullable=True)

    # Lifecycle of this fixture: scheduled (planned, not yet played) | live |
    # completed (result recorded). Defaults to "completed" so every pre-existing
    # recorded match row keeps counting in the standings without a backfill.
    status = Column(String(20), default="completed", nullable=False, index=True)
    # Group this fixture belongs to (groups-format league stage); NULL otherwise.
    group_id = Column(Integer, ForeignKey("tournament_groups.id", ondelete="SET NULL"),
                      nullable=True, index=True)
    # Schedule ordering / display.
    round_no = Column(Integer, default=0, nullable=False)
    match_no = Column(Integer, default=0, nullable=False)

    # Stage: league | group | quarterfinal | semifinal | qualifier1 | eliminator |
    # qualifier2 | final. Only league/group rows contribute league points.
    stage = Column(String(30), default="league", nullable=False)
    result_text = Column(String(300), nullable=True)

    # ── Venue & conditions fixed for this fixture ─────────────────────────
    # The surface this fixture must be played on. Assigned when the schedule is
    # generated (see ``services.league_schedule_service.assign_fixture_venues``)
    # and, while the tournament's ``pitch_mode`` is not "host", enforced during
    # setup: the host's pitch picker is skipped and this surface is used. NULL
    # means "not fixed" — the host picks, exactly as before.
    pitch_type = Column(String(20), nullable=True)
    # Which of the two sides is at home. Always one of team1_id / team2_id (the
    # generator defaults it to team1); shown in the fixture list and used to
    # seed the pitch in "home" pitch mode.
    home_team_id = Column(Integer, ForeignKey("tournament_teams.id", ondelete="SET NULL"),
                          nullable=True)
    venue = Column(String(120), nullable=True)

    # Knockout bracket wiring (Phase 2): where this fixture's winner/loser advances,
    # plus human labels for slots that are still "To Be Decided".
    feeds_winner_to_id = Column(Integer, ForeignKey("tournament_matches.id", ondelete="SET NULL"), nullable=True)
    feeds_loser_to_id = Column(Integer, ForeignKey("tournament_matches.id", ondelete="SET NULL"), nullable=True)
    slot1_label = Column(String(60), nullable=True)
    slot2_label = Column(String(60), nullable=True)

    inn1_runs = Column(Integer, nullable=True)
    inn1_wickets = Column(Integer, nullable=True)
    inn1_balls = Column(Integer, nullable=True)
    inn2_runs = Column(Integer, nullable=True)
    inn2_wickets = Column(Integer, nullable=True)
    inn2_balls = Column(Integer, nullable=True)

    host_user_id = Column(Integer, nullable=True)
    target_user_id = Column(Integer, nullable=True)

    # Full per-player batting + bowling lines for this match (JSON).
    scorecard_json = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    tournament = relationship("Tournament", back_populates="matches")

    __table_args__ = (
        Index("ix_tournament_match_teams", "tournament_id", "team1_id", "team2_id"),
    )


class TournamentPlayerStats(Base):
    """Tournament-wide aggregate stats for a player as played by a given user."""
    __tablename__ = "tournament_player_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tournament_id = Column(Integer, ForeignKey("tournaments.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Master player id (preferred identity). NULL for challenge players that have
    # no source_player_id — those fall back to roster_id for identity/upserts.
    player_id = Column(Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=True, index=True)
    roster_id = Column(Integer, nullable=True, index=True)  # ChallengePlayer.id
    name = Column(String(150), nullable=True)
    team_name = Column(String(120), nullable=True)

    matches = Column(Integer, default=0, nullable=False)

    # Batting
    bat_innings = Column(Integer, default=0, nullable=False)
    bat_runs = Column(Integer, default=0, nullable=False)
    bat_balls = Column(Integer, default=0, nullable=False)
    bat_fours = Column(Integer, default=0, nullable=False)
    bat_sixes = Column(Integer, default=0, nullable=False)
    bat_outs = Column(Integer, default=0, nullable=False)
    highest_score = Column(Integer, default=0, nullable=False)

    # Bowling
    bowl_innings = Column(Integer, default=0, nullable=False)
    bowl_wickets = Column(Integer, default=0, nullable=False)
    bowl_runs = Column(Integer, default=0, nullable=False)
    bowl_balls = Column(Integer, default=0, nullable=False)
    best_bowl_wickets = Column(Integer, default=0, nullable=False)
    best_bowl_runs = Column(Integer, default=-1, nullable=False)  # -1 = no figure yet

    tournament = relationship("Tournament", back_populates="player_stats")

    # Non-unique: NULL player_ids would defeat a DB unique constraint (NULLs are
    # distinct in SQL), so the service enforces one row per identity in code.
    __table_args__ = (
        Index("ix_tournament_player_lookup", "tournament_id", "user_id", "player_id"),
        Index("ix_tournament_player_roster", "tournament_id", "user_id", "roster_id"),
    )


class TournamentInjury(Base):
    """A player carrying a knock, ruled out of their team's next few matches.

    Created when a tournament match finishes (see ``services.injury_service``)
    and counted down one per subsequent match that team plays. While
    ``matches_remaining`` is above zero the player is filtered out of the XI
    picker for that team, so an injured player simply cannot be selected.

    Identity is ``roster_id`` — the ``ChallengePlayer`` row, which is what the XI
    picker works in. ``player_id`` is the master catalogue id where one exists
    and is kept for display and history only; a challenge player with no source
    card still gets injured like anyone else.

    Injuries are per tournament, not global: the same card is fit everywhere
    else, including in another tournament running at the same time.
    """
    __tablename__ = "tournament_injuries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tournament_id = Column(Integer, ForeignKey("tournaments.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    tournament_team_id = Column(Integer, ForeignKey("tournament_teams.id", ondelete="CASCADE"),
                                nullable=False, index=True)
    # ChallengePlayer.id — the identity the Playing XI picker selects on.
    roster_id = Column(Integer, nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id", ondelete="SET NULL"),
                       nullable=True, index=True)
    player_name = Column(String(150), nullable=True)

    # Flavour + rules. ``severity`` is one of niggle | strain | serious and only
    # decides how long ``matches_out`` is; ``injury_type`` is the human name
    # ("Hamstring Strain") and ``how`` says what they were doing when it happened.
    injury_type = Column(String(80), nullable=False)
    severity = Column(String(20), default="niggle", nullable=False)
    how = Column(String(120), nullable=True)

    matches_out = Column(Integer, default=1, nullable=False)
    matches_remaining = Column(Integer, default=1, nullable=False, index=True)

    # Where it happened, for the injury report and for undo.
    match_id = Column(Integer, ForeignKey("matches.id", ondelete="SET NULL"),
                      nullable=True, index=True)
    tournament_match_id = Column(Integer, ForeignKey("tournament_matches.id", ondelete="SET NULL"),
                                 nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    recovered_at = Column(DateTime, nullable=True)

    tournament = relationship("Tournament", back_populates="injuries")

    __table_args__ = (
        # The lookup the XI picker makes on every squad open: "who is out for
        # this team right now".
        Index("ix_tournament_injury_team_active", "tournament_team_id",
              "matches_remaining"),
    )


class TournamentMatchReminder(Base):
    """One nudge sent about one unplayed fixture — the log, and the cooldown.

    A reminder tags people and lands in their DMs, so it is exactly the feature
    that becomes spam the third time it fires. This row is what stops that: the
    sender checks the last reminder for a fixture and refuses to send another
    inside the cooldown window unless it is overridden on purpose.

    It doubles as the audit trail. ``sent_by_tg_id`` is whoever ran the command
    and ``chat_id`` the group it was announced in, so "who pinged the whole
    league at 3am" has an answer. Rows are kept after the fixture is played —
    the fixture's own row is what gets deleted with the tournament.
    """
    __tablename__ = "tournament_match_reminders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tournament_id = Column(Integer, ForeignKey("tournaments.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    tournament_match_id = Column(Integer, ForeignKey("tournament_matches.id", ondelete="CASCADE"),
                                 nullable=False, index=True)
    # Who sent it, and where it was announced. ``chat_id`` is NULL for a
    # reminder sent from a DM (the DMs still go out; there is just no group).
    sent_by_tg_id = Column(BigInteger, nullable=True, index=True)
    chat_id = Column(BigInteger, nullable=True)
    # Delivery outcome, for the report and for "did this actually reach anyone?".
    recipients = Column(Integer, default=0, nullable=False)
    delivered = Column(Integer, default=0, nullable=False)
    failed = Column(Integer, default=0, nullable=False)
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        # The cooldown lookup: "when was this fixture last reminded about?"
        Index("ix_tournament_reminder_fixture", "tournament_match_id", "sent_at"),
    )


# ══════════════════════════════════════════════════════════════════════
# FANTASY LEAGUE — admin-controlled weekly fantasy cricket competition
# ══════════════════════════════════════════════════════════════════════

class FantasyLeague(Base):
    """A weekly fantasy cricket league. Admin creates it; users pick squads."""
    __tablename__ = "fantasy_leagues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    broadcast_message = Column(Text, nullable=True)
    week_number = Column(Integer, nullable=False, default=1)
    year = Column(Integer, nullable=False, default=2025)
    status = Column(String(20), default="open", nullable=False)  # open | locked | scored
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)
    # Auto-lock: when set (stored UTC), squads can no longer be created or
    # edited once this moment passes. Admin enters the time in IST on the
    # website; the background job locks the league when it elapses.
    lock_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class FantasyLeaguePlayer(Base):
    """Player eligibility list for one fantasy league.

    When a league has no rows in this table, all active players remain eligible
    for backwards compatibility. As soon as admins save at least one row, the
    Mini App and squad validation are restricted to this selected pool.
    """
    __tablename__ = "fantasy_league_players"

    id = Column(Integer, primary_key=True, autoincrement=True)
    league_id = Column(Integer, ForeignKey("fantasy_leagues.id", ondelete="CASCADE"), nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_flp_league_player", "league_id", "player_id", unique=True),
    )


class FantasyCountryRule(Base):
    """Per-country min/max squad limits for a fantasy league."""
    __tablename__ = "fantasy_country_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    league_id = Column(Integer, ForeignKey("fantasy_leagues.id", ondelete="CASCADE"), nullable=False, index=True)
    country = Column(String(60), nullable=False)
    min_players = Column(Integer, default=0, nullable=False)
    max_players = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_fcr_league_country", "league_id", "country", unique=True),
    )


class FantasyRoleRule(Base):
    """Per-player-role min/max squad limits for a fantasy league."""
    __tablename__ = "fantasy_role_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    league_id = Column(Integer, ForeignKey("fantasy_leagues.id", ondelete="CASCADE"), nullable=False, index=True)
    role_key = Column(String(20), nullable=False)  # bat | bowl | ar | wk
    min_players = Column(Integer, default=0, nullable=False)
    max_players = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_frr_league_role", "league_id", "role_key", unique=True),
    )


class FantasyMatch(Base):
    """A real-world cricket match added by admin within a fantasy league week."""
    __tablename__ = "fantasy_matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    league_id = Column(Integer, ForeignKey("fantasy_leagues.id", ondelete="CASCADE"), nullable=False, index=True)
    match_name = Column(String(200), nullable=False)  # e.g. "India vs Australia"
    match_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class FantasyPlayerScore(Base):
    """Admin-entered fantasy points for a player in one real-world match."""
    __tablename__ = "fantasy_player_scores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fantasy_match_id = Column(Integer, ForeignKey("fantasy_matches.id", ondelete="CASCADE"), nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    points = Column(Float, default=0.0, nullable=False)
    notes = Column(String(300), nullable=True)  # e.g. "50 runs + 1 wicket"
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_fps_match_player", "fantasy_match_id", "player_id", unique=True),
    )


class FantasyEntry(Base):
    """One user's fantasy squad for a specific league."""
    __tablename__ = "fantasy_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    league_id = Column(Integer, ForeignKey("fantasy_leagues.id", ondelete="CASCADE"), nullable=False, index=True)
    total_points = Column(Float, default=0.0, nullable=False)
    rank = Column(Integer, nullable=True)
    locked = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_fe_user_league", "user_id", "league_id", unique=True),
    )


class FantasyPick(Base):
    """An individual player in a user's fantasy squad."""
    __tablename__ = "fantasy_picks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_id = Column(Integer, ForeignKey("fantasy_entries.id", ondelete="CASCADE"), nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    role = Column(String(10), default="player", nullable=False)  # captain | vc | player
    total_points = Column(Float, default=0.0, nullable=False)

    __table_args__ = (
        Index("ix_fp_entry_player", "entry_id", "player_id", unique=True),
    )


class RosterOverflowClaim(Base):
    """A player rolled from a Mini App reward (daily / free pack / pack / gspin)
    that couldn't be auto-added because the roster was at ``config.MAX_ROSTER``.

    Instead of silently discarding the overflow player, we hold it here so the
    Mini App can offer a "Replace" flow (mirroring the bot's /claim replace):
    the user picks a roster player to drop and the pending player takes its
    slot. Rows are single-use (deleted on replace/release) and pruned after a
    short TTL so they can't accumulate. Storing the pending player_id
    server-side is what keeps the replace endpoint safe — a client can only
    claim a player the server actually rolled for them.
    """
    __tablename__ = "roster_overflow_claims"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(20), default="reward", nullable=False)  # daily|free_pack|pack|gspin
    sell_value = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


# ══════════════════════════════════════════════════════════════════════
# TOURNAMENT DRAFT — teams pick their squads, live, pick by pick
# ══════════════════════════════════════════════════════════════════════
#
# A draft is the front end the Challenge League never had: an admin uploads a
# player pool and a pick order, owners type /pick in a bound group chat, and the
# finished squads are published into a real ChallengeLeague so they can play.
#
# These tables are deliberately separate from ``tournaments``. A draft needs a
# pool of *candidate* players carrying tier / icon / gender / Indian status
# (none of which exist on ``players``), an order sheet, and a pick clock — all
# of which would otherwise become draft-only columns on every Challenge League
# and Lets Play tournament row. The draft has its own lifecycle that *ends* by
# producing a league, so it gets its own tables and a one-way publish step.


class PlayerDraft(Base):
    """One draft: a player pool, a pick order, and a clock.

    ``chat_id`` is the bound draft group. Every announcement goes there and
    ``/pick`` is refused anywhere else — a draft is a public event, and a pick
    made in a DM that nobody sees is how an order gets disputed.
    """
    __tablename__ = "player_drafts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    # setup | live | paused | completed | cancelled
    status = Column(String(20), default="setup", nullable=False, index=True)
    # The bound draft group chat. Unique so two drafts can't both own one chat
    # and leave /pick ambiguous; NULL until an admin runs /dbind.
    chat_id = Column(BigInteger, nullable=True)

    # ── The clock ──────────────────────────────────────────────────────
    # Seconds a team gets to make its pick, and how long before the deadline the
    # "you're running out of time" ping fires. Both admin-editable per draft.
    pick_seconds = Column(Integer, default=900, nullable=False)
    warn_seconds = Column(Integer, default=120, nullable=False)
    # The pick currently on the clock, and when it expires. The deadline lives
    # in the database rather than in a job because the host redeploys often and
    # an in-process timer would not survive it — services/draft_scheduler.py
    # reconciles this column instead. See services/giveaway_scheduler.py for the
    # same call made for giveaways.
    current_pick_id = Column(Integer, ForeignKey("draft_picks.id", ondelete="SET NULL"),
                             nullable=True)
    pick_deadline_at = Column(DateTime, nullable=True)
    warn_sent = Column(Boolean, default=False, nullable=False)

    # ── The pinned pick ────────────────────────────────────────────────
    # The draft group's pinned message is kept on the latest pick, so anyone
    # scrolling in — or arriving hours late — sees where the draft is without
    # reading back through a thousand messages. ``pinned_message_id`` is the
    # message currently pinned by the bot (unpinned when the next pick lands);
    # ``pin_picks`` turns the whole behaviour off for a group that would rather
    # keep its own pin (/dpin).
    pinned_message_id = Column(BigInteger, nullable=True)
    pin_picks = Column(Boolean, default=True, nullable=False)

    # ── The rules ──────────────────────────────────────────────────────
    # The tier ladder, highest first, as a JSON list. A pick slot's tier is a
    # CEILING: a Platinum slot accepts Platinum, Gold, Silver or Bronze.
    tier_order_json = Column(Text, nullable=True)
    # Overseas rule. A pool player is "overseas" when is_indian is False; the
    # column pair mirrors ChallengeLeague.home_country / max_overseas so the
    # published league inherits the same rule. 11 means no cap.
    home_country = Column(String(60), default="India", nullable=False)
    max_overseas = Column(Integer, default=11, nullable=False)
    # {"Wicket Keeper": 1, "Bowler": 4} — minimum squad composition by role.
    # Enforced as *reachability*: a pick is refused when it would leave too few
    # slots to still satisfy the minimums.
    role_minimums_json = Column(Text, nullable=True)

    # ── The trade window ───────────────────────────────────────────────
    # Whether owners may swap drafted players with /dtrade once the draft is
    # finished. On by default; an admin closes the window with /dtradelock
    # before a fixture so squads can't change under a live match.
    trades_open = Column(Boolean, default=True, nullable=False)

    # ── Publication ────────────────────────────────────────────────────
    # The Challenge League this draft was published into. SET NULL so deleting
    # the league leaves the draft's own record of what happened intact.
    league_id = Column(Integer, ForeignKey("challenge_leagues.id", ondelete="SET NULL"),
                       nullable=True, index=True)
    published_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    teams = relationship("DraftTeam", back_populates="draft",
                         cascade="all, delete-orphan")
    pool = relationship("DraftPlayer", back_populates="draft",
                        cascade="all, delete-orphan")
    # current_pick_id points into draft_picks, which points back at this row —
    # name the join explicitly so SQLAlchemy doesn't have to guess between them.
    picks = relationship("DraftPick", back_populates="draft",
                         cascade="all, delete-orphan",
                         foreign_keys="DraftPick.draft_id")
    trades = relationship("DraftTrade", back_populates="draft",
                          cascade="all, delete-orphan")
    squad_edits = relationship("DraftSquadEdit", back_populates="draft",
                               cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_player_draft_chat_unique", "chat_id", unique=True),
    )


class DraftTeam(Base):
    """A franchise in a draft, owned by a Telegram user.

    ``owner_tg_id`` is the identity (not ``users.id``) for the same reason
    TournamentTeam uses it: an admin builds the field from a list of Telegram
    ids, and some of those people have never messaged the bot.
    """
    __tablename__ = "draft_teams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    draft_id = Column(Integer, ForeignKey("player_drafts.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    name = Column(String(120), nullable=False)
    short_name = Column(String(30), nullable=True)
    logo_url = Column(String(500), nullable=True)
    owner_tg_id = Column(BigInteger, nullable=True, index=True)
    owner_name = Column(String(120), nullable=True)
    # Extra Telegram ids allowed to pick for this team, as a JSON list. Anyone
    # not the owner and not in here is refused, admins included.
    co_owner_ids_json = Column(Text, nullable=True)
    # The owner's wishlist, a JSON list of DraftPlayer ids. Auto-pick drains it
    # before falling back to the tier average, so being away from the phone
    # doesn't have to mean losing the player you wanted.
    queue_json = Column(Text, nullable=True)
    sort_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    draft = relationship("PlayerDraft", back_populates="teams")

    __table_args__ = (
        Index("ix_draft_team_unique", "draft_id", "name", unique=True),
    )


class DraftPlayer(Base):
    """One player in a draft's pool. NULL ``picked_by_team_id`` means available.

    The pool is scoped to its draft rather than shared with ``players`` because
    the uploaded sheet carries tier, icon eligibility, gender and the country the
    overseas rule is decided on, none of which the master catalogue models — and because a draft's pool is a
    curated list for one competition, not an edit to the global card database.
    ``source_player_id`` links back when the name matches a real card, which is
    what lets the bot post that card's image when the player is picked.
    """
    __tablename__ = "draft_players"

    id = Column(Integer, primary_key=True, autoincrement=True)
    draft_id = Column(Integer, ForeignKey("player_drafts.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    name = Column(String(150), nullable=False, index=True)
    rating = Column(Integer, default=70, nullable=False)
    tier = Column(String(20), nullable=False, index=True)
    icon_eligible = Column(Boolean, default=False, nullable=False)
    gender = Column(String(10), nullable=True)
    # Whether this player is a *home* player for the draft: their ``country``
    # matched ``PlayerDraft.home_country`` at import (the sheet's
    # ``indian_status`` is only a fallback for a row with no readable country).
    # Named for the India-only rule it started as; every consumer asks a yes/no
    # question, and ChallengePlayer.is_overseas — which this feeds on publish —
    # is already a boolean. ``draft_service.resync_home_status`` recomputes it
    # when a draft's home country changes.
    is_indian = Column(Boolean, default=True, nullable=False)
    category = Column(String(30), default="Batsman", nullable=False)
    country = Column(String(60), default="Unknown", nullable=False)
    bat_hand = Column(String(10), default="Right", nullable=False)
    bowl_hand = Column(String(10), default="Right", nullable=False)
    bowl_style = Column(String(30), default="Medium Pacer", nullable=False)
    bat_rating = Column(Integer, default=0, nullable=False)
    bowl_rating = Column(Integer, default=0, nullable=False)
    source_player_id = Column(Integer, ForeignKey("players.id", ondelete="SET NULL"),
                              nullable=True, index=True)
    picked_by_team_id = Column(Integer, ForeignKey("draft_teams.id", ondelete="SET NULL"),
                               nullable=True, index=True)
    picked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    draft = relationship("PlayerDraft", back_populates="pool")
    team = relationship("DraftTeam")
    source_player = relationship("Player")

    __table_args__ = (
        Index("ix_draft_player_unique", "draft_id", "name", unique=True),
        Index("ix_draft_player_available", "draft_id", "picked_by_team_id"),
    )


class DraftPick(Base):
    """One slot in the pick order — and, once taken, the record of the pick.

    The order sheet and the result log are the same rows on purpose: "R1 P3 is
    Mumbai's Platinum slot" and "R1 P3 was Bumrah" are the same fact at two
    points in time, and splitting them would let the two drift apart.
    """
    __tablename__ = "draft_picks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    draft_id = Column(Integer, ForeignKey("player_drafts.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    round_no = Column(Integer, default=1, nullable=False)
    pick_no = Column(Integer, default=1, nullable=False)
    # The flattened running order. Rounds and pick numbers are what people say
    # out loud ("R1 P3"); this is what the clock actually walks.
    overall_no = Column(Integer, default=1, nullable=False)
    # The slot's tier CEILING — this tier or anything below it on the ladder.
    tier = Column(String(20), nullable=False)
    team_id = Column(Integer, ForeignKey("draft_teams.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    draft_player_id = Column(Integer, ForeignKey("draft_players.id", ondelete="SET NULL"),
                             nullable=True)
    picked_by_tg_id = Column(BigInteger, nullable=True)
    # True when the clock made this pick rather than a person.
    is_auto = Column(Boolean, default=False, nullable=False)
    # pending | done | skipped ("skipped" = the clock expired and no legal
    # player was left, which must advance the draft rather than wedge it)
    status = Column(String(20), default="pending", nullable=False, index=True)
    picked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    draft = relationship("PlayerDraft", back_populates="picks",
                         foreign_keys=[draft_id])
    team = relationship("DraftTeam")
    player = relationship("DraftPlayer", foreign_keys=[draft_player_id])

    __table_args__ = (
        Index("ix_draft_pick_unique", "draft_id", "round_no", "pick_no", unique=True),
        Index("ix_draft_pick_order_unique", "draft_id", "overall_no", unique=True),
    )


class DraftTrade(Base):
    """A ``/dtrade`` between two franchises: the offer, and then the record of it.

    **The whole offer lives here, not in process memory.** ``/trade`` keeps its
    in-flight state in ``context.bot_data`` and loses every open trade to a
    redeploy; a draft group's trade window is open for days across a host that
    restarts often, so the half-built offer — who is trading with whom, which
    players each side has ticked, who has confirmed — is a row that survives it.
    The buttons carry only this row's id, so a message pressed after a restart
    picks up exactly where it was.

    ``players_a_json`` / ``players_b_json`` are JSON lists of ``DraftPlayer``
    ids: the players ``team_a`` sends to ``team_b`` and vice versa. They are
    kept after completion, which is what makes this table the trade log
    ``/dtrades`` prints — the pick rows are deliberately never rewritten, so
    this is the only place a squad change after the draft is recorded.
    """
    __tablename__ = "draft_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    draft_id = Column(Integer, ForeignKey("player_drafts.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    # The team that opened the trade, and the team it was opened with.
    team_a_id = Column(Integer, ForeignKey("draft_teams.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    team_b_id = Column(Integer, ForeignKey("draft_teams.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    players_a_json = Column(Text, nullable=True)
    players_b_json = Column(Text, nullable=True)
    # building_a | building_b | offered | completed | cancelled | expired
    status = Column(String(20), default="building_a", nullable=False, index=True)
    # Telegram ids, so a co-owner's confirmation is recorded as theirs rather
    # than as the owner's. NULL means that side has not confirmed yet.
    confirmed_a_by = Column(BigInteger, nullable=True)
    confirmed_b_by = Column(BigInteger, nullable=True)
    opened_by_tg_id = Column(BigInteger, nullable=True)
    closed_by_tg_id = Column(BigInteger, nullable=True)
    chat_id = Column(BigInteger, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    draft = relationship("PlayerDraft", back_populates="trades")
    team_a = relationship("DraftTeam", foreign_keys=[team_a_id])
    team_b = relationship("DraftTeam", foreign_keys=[team_b_id])

    __table_args__ = (
        Index("ix_draft_trade_live", "draft_id", "status"),
    )


class DraftSquadEdit(Base):
    """One admin override: a player put on a squad, moved, or sent to the pool.

    ``/dadd`` and ``/ddrop`` bypass every squad rule on purpose — an admin
    fixing a mess has to be able to pass *through* an illegal squad to get to a
    legal one (drop first, add second), and a rule that refuses the first half
    of that makes the tool useless. What replaces the gate is the record: the
    move is announced in the draft group and kept here, so a squad nobody
    remembers agreeing to can always be traced to the admin who typed it.

    A separate table from ``DraftTrade`` because it is a different fact. A
    trade is two consenting franchises and needs an offer, two selections and
    two confirmations; this is one row with no state machine at all. Sharing
    one table would mean a ``kind`` column plus three columns that mean
    different things depending on it.

    ``from_team_id`` and ``to_team_id`` are NULL for the pool, which is what
    makes the three actions one shape: NULL → team is an add, team → NULL is a
    release, team → team is a move.
    """
    __tablename__ = "draft_squad_edits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    draft_id = Column(Integer, ForeignKey("player_drafts.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    draft_player_id = Column(Integer, ForeignKey("draft_players.id",
                                                 ondelete="SET NULL"),
                             nullable=True, index=True)
    # Kept alongside the id so the log still reads after a pool row is deleted.
    player_name = Column(String(150), nullable=False)
    from_team_id = Column(Integer, ForeignKey("draft_teams.id", ondelete="SET NULL"),
                          nullable=True)
    to_team_id = Column(Integer, ForeignKey("draft_teams.id", ondelete="SET NULL"),
                        nullable=True)
    from_team_name = Column(String(120), nullable=True)
    to_team_name = Column(String(120), nullable=True)
    by_tg_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    draft = relationship("PlayerDraft", back_populates="squad_edits")

    __table_args__ = (
        Index("ix_draft_squad_edit_draft", "draft_id", "created_at"),
    )

    @property
    def action(self):
        """``"added"`` / ``"released"`` / ``"moved"``, from the two team ids."""
        if self.from_team_id is None and self.to_team_id is not None:
            return "added"
        if self.to_team_id is None and self.from_team_id is not None:
            return "released"
        return "moved"


# ══════════════════════════════════════════════════════════════════════
# PITCH STATISTICS — what the surfaces actually did, in real matches
# ══════════════════════════════════════════════════════════════════════
# Written at match end for /letsplay and Challenge League matches only (see
# services/pitch_stats.py) and read by /pitchstats. Practice matches against
# the AI captain are excluded: they are unranked, and a bot's approach picks
# would drown the human record they are meant to describe.


class PitchMatchStat(Base):
    """One finished match, reduced to what a pitch report needs.

    The ``matches`` row already carries the pitch, the scores and the winner, so
    this table exists for the two things it cannot answer: how many BALLS each
    innings lasted (a chase won in 17.2 makes runs/overs a lie) and how the
    innings split by phase. Both come from the live state, which is thrown away
    when the match ends.
    """
    __tablename__ = "pitch_match_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id", ondelete="CASCADE"),
                      unique=True, nullable=False, index=True)
    pitch_type = Column(String(30), nullable=False, index=True)
    # "letsplay" | "challenge" — the two modes /pitchstats counts. Kept as its
    # own column rather than joined from matches.match_type so the stats query
    # never has to touch the (much larger) matches table.
    mode = Column(String(20), nullable=False, index=True)
    is_tournament = Column(Boolean, default=False, nullable=False)
    ball_format = Column(String(20), default="T20")
    overs = Column(Integer, default=20)

    inn1_runs = Column(Integer, default=0)
    inn1_wickets = Column(Integer, default=0)
    inn1_balls = Column(Integer, default=0)
    inn2_runs = Column(Integer, default=0)
    inn2_wickets = Column(Integer, default=0)
    inn2_balls = Column(Integer, default=0)

    # "bat_first" | "bat_second" | "tie" — who won, in the only terms a pitch
    # has an opinion about. A Super Over is recorded as the side that won it.
    result = Column(String(16), nullable=False, default="tie")

    # What the toss winner elected, and whether that matched the surface's own
    # recommendation in engine.pitch_registry.
    toss_decision = Column(String(10), nullable=True)
    toss_by_the_book = Column(Boolean, nullable=True)
    # True when the side that won the toss also won the match.
    toss_winner_won = Column(Boolean, nullable=True)

    # Phase splits, both innings combined.
    pp_runs = Column(Integer, default=0)
    pp_wickets = Column(Integer, default=0)
    pp_balls = Column(Integer, default=0)
    mid_runs = Column(Integer, default=0)
    mid_wickets = Column(Integer, default=0)
    mid_balls = Column(Integer, default=0)
    death_runs = Column(Integer, default=0)
    death_wickets = Column(Integer, default=0)
    death_balls = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_pitch_match_pitch_mode", "pitch_type", "mode"),
    )


class PitchApproachStat(Base):
    """Rolling totals for one (pitch, phase, batting intent, bowling plan) cell.

    A rollup rather than a row per over: the whole table is at most
    pitches x phases x 5 x 5 x modes, so the answer to "what does Ultra Attack
    into Variation actually do on a turner" is one indexed read instead of a
    scan over every over ever bowled.
    """
    __tablename__ = "pitch_approach_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pitch_type = Column(String(30), nullable=False, index=True)
    mode = Column(String(20), nullable=False, index=True)
    phase = Column(String(16), nullable=False)      # powerplay | middle | death
    bat_approach = Column(String(20), nullable=False)
    bowl_approach = Column(String(20), nullable=False)

    overs = Column(Integer, default=0)
    balls = Column(Integer, default=0)
    runs = Column(Integer, default=0)
    wickets = Column(Integer, default=0)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("pitch_type", "mode", "phase", "bat_approach",
                         "bowl_approach", name="uq_pitch_approach_cell"),
        Index("ix_pitch_approach_lookup", "pitch_type", "mode"),
    )


# ══════════════════════════════════════════════════════════════════════
# FRANCHISE AUCTION — a season's squads, bought rather than drafted
#
# An auction is the Tournament Draft's sibling: the same franchises, the same
# bound group, the same restart-safe clock, the same one-way publish into a
# ChallengeLeague. What is new is money — a purse, a base price, a bidding
# loop — and money is why these are their own tables rather than a mode flag on
# ``player_drafts``. A draft's slot is a *ceiling on tier*; an auction's lot is
# a *price discovered by the room*, and half the draft's columns (tier ladder,
# pick order, queue) mean nothing here while none of the ones below mean
# anything there.
#
# Every amount is an INTEGER COUNT OF LAKH, and every such column is suffixed
# ``_lakh`` so a float can never get in by accident. ₹1.5 Cr is 150; ₹100 Cr is
# 10,000. It is a value unit of its own and has no relationship to the coin
# economy in ``config.BUY_VALUES`` — a franchise purse is not spendable on
# anything a player owns, and coins are not spendable at an auction.


class AuctionSeason(Base):
    """One auction: a player pool, a set of franchises, a purse each, a clock.

    ``chat_id`` is the bound auction group. Every announcement goes there and
    ``/bid`` is refused anywhere else — an auction is a public event, and a bid
    made in a DM that nobody sees is how a price gets disputed. Unique, so two
    auctions cannot both own one chat and leave ``/bid`` ambiguous (the same
    call ``PlayerDraft.chat_id`` makes).

    **The lot clock is not here.** ``AuctionLot`` carries ``deadline_at``,
    ``going_stage`` and ``extensions_used``, because the anti-snipe extension
    has to be applied in the *same statement* as the bid that earned it —
    otherwise two bidders on the same tick both read the deadline, both decide
    they are inside the snipe window, and both extend it. This row keeps only
    ``current_lot_id`` as a pointer, and ``auction_service.current_lot`` heals
    it from the lot table when it goes stale.
    """
    __tablename__ = "auction_seasons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    # setup | live | paused | completed | cancelled
    status = Column(String(20), default="setup", nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=True)

    # ── The clock ──────────────────────────────────────────────────────
    # Seconds a lot stays on the block with no bid. A bid resets it; the
    # anti-snipe rule below can extend it. The deadline itself is on the lot.
    bid_seconds = Column(Integer, default=30, nullable=False)
    current_lot_id = Column(Integer, nullable=True)

    # ── Anti-snipe ─────────────────────────────────────────────────────
    # A bid landing with ``snipe_window_seconds`` or less on the clock pushes
    # the deadline out to ``snipe_extend_seconds``, at most ``max_extensions``
    # times. Shipped at the proposal's numbers (2s / 10s / 5). Setting the
    # window EQUAL to the extension gives the rule every real auction has —
    # "any bid in the last 10 seconds gives everyone 10 more" — which is what
    # most rooms actually want; at a 2-second window a bid at 3s left buys
    # nobody a chance to answer.
    snipe_window_seconds = Column(Integer, default=2, nullable=False)
    snipe_extend_seconds = Column(Integer, default=10, nullable=False)
    max_extensions = Column(Integer, default=5, nullable=False)

    # ── Money ──────────────────────────────────────────────────────────
    opening_purse_lakh = Column(Integer, default=10000, nullable=False)
    currency_label = Column(String(10), default="₹", nullable=False)
    # [{"min_rating": 92, "max_rating": 96, "base_lakh": 200}, ...] — rating
    # RANGES, highest band first, and a row with no "max_rating" is an open top
    # ("97 and up"). Rows saved before ranges existed carry min_rating alone and
    # meant "up to just under the band above"; auction_service.base_price_rules()
    # fills that bound in on read, so those seasons price exactly as they did and
    # neither caller has to know which shape a row came from.
    #
    # Read by auction_service.base_price_for() ONCE, at pool-build time, and
    # stamped onto AuctionLot.base_price_lakh — so editing the ladder afterwards
    # never moves the price a lot already went on the block at. A JSON column
    # rather than a table for the same reason PlayerDraft.tier_order_json is
    # one: a handful of bands, edited as a whole, never queried by.
    base_price_rules_json = Column(Text, nullable=True)
    # [{"upto_lakh": 200, "step_lakh": 10}, ...] — the bid increment ladder.
    bid_increment_rules_json = Column(Text, nullable=True)

    # ── Squad rules ────────────────────────────────────────────────────
    min_squad_size = Column(Integer, default=15, nullable=False)
    max_squad_size = Column(Integer, default=25, nullable=False)
    # The cheapest base price in this pool, stamped at pool build. It is what
    # the reachability rule holds back per unfilled slot — see
    # auction_service.max_bid_now(). Deliberately static rather than "the
    # cheapest lot still available": a ceiling that drifts every time some
    # other franchise buys a cheap player is one nobody can steer by.
    min_base_price_lakh = Column(Integer, default=20, nullable=False)
    # {"Wicket Keeper": 1, "Bowler": 4} — same shape and same reachability
    # semantics as PlayerDraft.role_minimums_json. Empty by default.
    role_minimums_json = Column(Text, nullable=True)
    # {"Bowler": 8} — the other end of the same rule, and a much simpler one:
    # a minimum is a promise about a squad that does not exist yet and so has
    # to be enforced as *reachability* (see auction_service.max_bid_now), while
    # a maximum is a fact about the squad in front of you and refuses the bid
    # that would break it. A role with no entry has no ceiling, which is why
    # this is a sparse map rather than one row per role: "no rule" and "a
    # ceiling of 0" are different answers and a dense map cannot tell them
    # apart.
    role_maximums_json = Column(Text, nullable=True)
    home_country = Column(String(60), default="India", nullable=False)
    max_overseas = Column(Integer, default=8, nullable=False)

    # ── Retention ──────────────────────────────────────────────────────
    # Retention happens while the season is still in ``setup``: a franchise
    # keeps some of its existing players at a price, and that spend comes off
    # the top of its purse before a single lot opens. There is deliberately no
    # ``retention`` season status — ``max_retentions > 0`` says retention is
    # configured, and ``retention_locked_at`` says the window is shut. A status
    # would have to be threaded through the pool guard, the sweeper's filter,
    # every status pill and ``start()``'s resume path for nothing a timestamp
    # does not already give.
    #
    # There is no ``retention_enabled`` boolean for the same kind of reason,
    # plus a sharper one: a non-nullable Boolean added to a populated table
    # reads back NULL-as-falsy on every existing row (see
    # docs/player-draft.md on ``trades_open``), and ``max_retentions > 0``
    # carries the same fact with an Integer that defaults cleanly.
    max_retentions = Column(Integer, default=0, nullable=False)
    # Enforced at ``start()``, by name — nothing a retention *does* can fix a
    # franchise being under the minimum, so refusing at retention time would be
    # a complaint nobody can act on.
    min_retentions = Column(Integer, default=0, nullable=False)
    # NULL means uncapped. The cap is on the total, not per player, because
    # that is the number an admin actually reasons about.
    retention_max_spend_lakh = Column(Integer, nullable=True)
    # Lazily enforced: ``retain()`` refuses once it has passed. Nothing sweeps
    # during retention — the clock job only looks at live seasons — so there is
    # nothing to actively close the window with, and an admin closing it by
    # hand is ``retention_locked_at`` below.
    retention_deadline_at = Column(DateTime, nullable=True)
    # The proposal's optional rating / category restrictions. NULL and empty
    # both mean "no restriction".
    retention_min_rating = Column(Integer, nullable=True)
    retention_max_rating = Column(Integer, nullable=True)
    retention_categories_json = Column(Text, nullable=True)
    # The slab ladder: [{"slab": 1, "price_lakh": 1800}, ...]. It only
    # PRE-FILLS the price for a franchise's next retention; the caps are what
    # actually refuse. Making the slab binding would invite juggling the order
    # to dodge the expensive rungs, and an admin who can see the number before
    # committing does not need protecting from it.
    retention_price_rules_json = Column(Text, nullable=True)
    # Shut the window. ``start()`` stamps this too, so opening the auction
    # closes retention rather than leaving it ajar.
    retention_locked_at = Column(DateTime, nullable=True)

    # ── Right To Match ─────────────────────────────────────────────────
    # The IPL 2025 rule, in full: when a lot's clock expires, the franchise
    # that held the player last season is asked whether it wants to exercise
    # its RTM; if it says yes the standing top bidder gets ONE more raise; and
    # only then does the RTM holder match that final number or let it go. The
    # price is the final bid, not the one that triggered it.
    rtm_enabled = Column(Boolean, default=False, nullable=False)
    # Cards each franchise starts the season with. Copied onto
    # ``AuctionFranchise.rtm_cards_total`` when a franchise is created, so
    # changing it later never silently re-arms a franchise mid-auction.
    rtm_per_team = Column(Integer, default=0, nullable=False)
    # How long each of the three windows runs. One number rather than three:
    # the questions are equally urgent, and three knobs would only be three
    # things to get wrong.
    rtm_window_seconds = Column(Integer, default=30, nullable=False)
    # The proposal's optional "bid + ₹2 Cr" premium on top of the final price.
    # Zero by default, so what ships is the pure IPL rule and the premium is a
    # deliberate choice rather than a surprise.
    rtm_extra_lakh = Column(Integer, default=0, nullable=False)

    # ── Last season ────────────────────────────────────────────────────
    # The ChallengeLeague this season follows. Stored rather than passed in
    # from a form each time, because the retention picker needs it BEFORE any
    # lot exists — ``AuctionLot.previous_franchise_id`` can only be stamped
    # once the pool is built, and retention runs before that.
    previous_league_id = Column(Integer, nullable=True)
    # The auction this one was cloned from, which is a different fact: the
    # league above is where last season's SQUADS live, this is where its RULES
    # came from. A season linked to a league by hand has one and not the other.
    previous_season_id = Column(Integer, nullable=True)

    # ── Expansion teams ────────────────────────────────────────────────
    # How many players a brand-new franchise may sign before the auction
    # opens — the IPL 2022 rule that let Gujarat and Lucknow take three each
    # out of the un-retained pool. 0 turns the whole thing off, which is what
    # a season with no new sides wants.
    expansion_picks = Column(Integer, default=0, nullable=False)

    # ── The board, and the announcement cursor ─────────────────────────
    # ``board_message_id`` is the one pinned message the auction lives in; it
    # is EDITED, never re-sent, so a 30-second lot does not cost the room 30
    # messages. ``announced_event_id`` is how the website talks to the group
    # without touching Telegram: an admin route writes rows and an AuctionEvent
    # and commits, and the bot's sweeper announces everything past this cursor.
    # ``board_rendered_bid_count`` debounces the edit — ten bids inside one
    # two-second tick cost one edit, not ten.
    board_message_id = Column(BigInteger, nullable=True)
    announced_event_id = Column(Integer, default=0, nullable=False)
    board_rendered_bid_count = Column(Integer, default=0, nullable=False)
    # The lot the pinned board was posted for. Each lot gets a FRESH board
    # (a new message the room is notified about, and the new pin); within a
    # lot the board is still edited in place. A board whose lot has moved on
    # is replaced rather than edited, and this is how the sweeper tells.
    board_lot_id = Column(Integer, nullable=True)

    # ── The accelerated round, automatically ──
    # When the queue runs dry with players unsold, they come back once as the
    # "⚡ Accelerated" set before the auction finishes. Integers rather than
    # booleans for the NULL-reads-falsy reason database._migrate_add_columns
    # gives; 1/0 are on/off.
    auto_accelerated = Column(Integer, default=1, nullable=False)
    accelerated_done = Column(Integer, default=0, nullable=False)

    # ── Focus mode ─────────────────────────────────────────────────────
    # While the auction is live or paused, the bound group answers auction
    # commands and nothing else — see ``services/auction_focus.py`` for why a
    # thirty-second clock and a pinned board cannot share a room with /claim.
    # ON by default, because a room that wanted the other way round says so
    # once with ``/afocus off``. An integer, not a boolean, for the
    # NULL-reads-falsy reason above; 1/0 are on/off, and a season written
    # before the column existed reads as ON.
    focus_mode = Column(Integer, default=1, nullable=False)

    # ── Direct bids ────────────────────────────────────────────────────
    # ``/bid 12`` — naming your own number rather than taking the next
    # minimum. ON by default, because that is what an auction is; a room that
    # wants a strict ladder (every raise exactly one step, no jump bids) turns
    # it off with ``/adirect off`` and bare ``/bid`` and the board's buttons
    # still work. An integer for the NULL-reads-falsy reason above.
    direct_bids = Column(Integer, default=1, nullable=False)

    # ── The hammer countdown ───────────────────────────────────────────
    # Before a lot is sold or passed, the room is told in new messages:
    # "Selling X to Team for ₹…", then 3, 2, 1 — and then SOLD. This is how
    # many seconds that count runs; 0 turns it off. ``/acountdown`` sets it.
    countdown_seconds = Column(Integer, default=3, nullable=False)
    # The quiet gap after every bid: for this many seconds no franchise may
    # bid again, and a bid that tries is told who holds the lot. Not shown on
    # the board. 0 (the default) turns it off, so many franchises can bid at
    # once — spam is stopped per person instead; ``/abidgap`` sets it.
    bid_gap_seconds = Column(Integer, default=0, nullable=False)
    # The staged lot clock (``/atimer 60 40 20 10 5``). ``bid_seconds`` is the
    # opening clock and ``countdown_seconds`` the final count; these three are
    # the middle of it. A bid with less than ``reset_seconds`` left puts the
    # clock back to ``reset_seconds`` (as often as it takes — this replaces
    # anti-snipe), and the room is warned at ``warn1_seconds`` and
    # ``warn2_seconds``: "Selling X to Team for ₹…". 0 reset = the classic
    # clock, which is what every season written before this runs on.
    reset_seconds = Column(Integer, default=0, nullable=False)
    warn1_seconds = Column(Integer, default=0, nullable=False)
    warn2_seconds = Column(Integer, default=0, nullable=False)
    # The pool's order when the auction first started, as a JSON list of
    # ``[lot_id, set_name]``. ``/arestart`` puts the queue back in exactly this
    # order — the accelerated round renumbers and re-files unsold players, and
    # without a snapshot "from the first player" would have nothing to mean.
    opening_order_json = Column(Text, nullable=True)

    # ── Publication ────────────────────────────────────────────────────
    league_id = Column(Integer, ForeignKey("challenge_leagues.id", ondelete="SET NULL"),
                       nullable=True, index=True)
    published_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    franchises = relationship("AuctionFranchise", back_populates="season",
                              cascade="all, delete-orphan")
    # current_lot_id points into auction_lots, which points back here — name the
    # join explicitly so SQLAlchemy does not have to guess between them.
    lots = relationship("AuctionLot", back_populates="season",
                        cascade="all, delete-orphan",
                        foreign_keys="AuctionLot.season_id")
    bids = relationship("AuctionBid", back_populates="season",
                        cascade="all, delete-orphan")
    ledger = relationship("AuctionLedgerEntry", back_populates="season",
                          cascade="all, delete-orphan")
    events = relationship("AuctionEvent", back_populates="season",
                          cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_auction_season_chat_unique", "chat_id", unique=True),
    )


class AuctionFranchise(Base):
    """A franchise in an auction: a name, the people who may bid, and a purse.

    ``owner_tg_id`` is the identity (not ``users.id``) for the same reason
    ``DraftTeam`` and ``TournamentTeam`` use it: an admin builds the field from
    a list of Telegram ids, and some of those people have never messaged the
    bot. Owner and co-owners are equals for every check ``/bid`` makes.

    ``purse_remaining_lakh`` and ``squad_size`` are CACHES over
    ``auction_ledger`` and ``auction_lots``, and they exist for atomicity
    rather than for speed. The debit that must never overdraw is one statement
    — ``UPDATE ... SET purse_remaining_lakh = purse_remaining_lakh - :price
    WHERE id = :id AND purse_remaining_lakh >= :price`` — which either wins or
    reports zero rows affected; the same device ``draft_service.make_pick``
    uses to claim a player and a slot. The SUM-then-compare alternative is a
    read followed by a write, and the gap between them is exactly where the
    website's "Mark Sold" lives. ``auction_service.reconcile_purses`` re-sums
    the ledger and reports any drift, and repairs it by writing a
    ``correction`` row rather than by overwriting the column, which would
    destroy the evidence.
    """
    __tablename__ = "auction_franchises"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season_id = Column(Integer, ForeignKey("auction_seasons.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    name = Column(String(120), nullable=False)
    short_name = Column(String(30), nullable=True)
    city = Column(String(120), nullable=True)
    logo_url = Column(String(500), nullable=True)
    owner_tg_id = Column(BigInteger, nullable=True, index=True)
    owner_name = Column(String(120), nullable=True)
    # Extra Telegram ids allowed to bid for this franchise, as a JSON list.
    co_owner_ids_json = Column(Text, nullable=True)
    sort_order = Column(Integer, default=0, nullable=False)

    # ── Purse ──────────────────────────────────────────────────────────
    purse_total_lakh = Column(Integer, default=10000, nullable=False)
    purse_remaining_lakh = Column(Integer, default=10000, nullable=False)
    squad_size = Column(Integer, default=0, nullable=False)

    # ── Retention / RTM ─────────────────────────────────────────────────
    retained_count = Column(Integer, default=0, nullable=False)
    rtm_cards_total = Column(Integer, default=0, nullable=False)
    rtm_cards_used = Column(Integer, default=0, nullable=False)

    # ── Last season ─────────────────────────────────────────────────────
    # The franchise in the previous season that this one continues. Worth a
    # column of its own because the alternative — matching last season's team
    # NAME to this season's franchise name — breaks the moment somebody
    # renames a franchise between seasons, and breaks silently: every Right To
    # Match and every retention candidate simply vanishes.
    carried_from_id = Column(Integer, nullable=True)

    # ── Expansion picks ─────────────────────────────────────────────────
    # Per franchise rather than per season, because "how many picks does this
    # side get" is a decision an admin may want to make differently for each
    # one — a league adding one team and compensating it is as real a case as
    # adding two equal ones. ``draft_picks_used`` counts skips too: a turn
    # burned is a turn spent, or the order never moves on.
    draft_picks_total = Column(Integer, default=0, nullable=False)
    draft_picks_used = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    season = relationship("AuctionSeason", back_populates="franchises")

    __table_args__ = (
        Index("ix_auction_franchise_unique", "season_id", "name", unique=True),
    )


class AuctionLot(Base):
    """One player in an auction — the lot that goes on the block AND its result.

    The pool and the result log are the same rows on purpose, the same call
    ``DraftPick`` makes: "Bumrah is lot 4 at a base of ₹2 Cr" and "lot 4 went to
    Mumbai for ₹12.25 Cr" are the same fact at two points in time, and splitting
    them would need an unenforceable "exactly one result per lot" invariant and
    a LEFT JOIN on every dashboard query.

    The player columns are a SNAPSHOT of the master ``players`` row taken at
    pool-build time, not a view over it — the same reason ``DraftPlayer`` is its
    own table. The catalogue moves (a card is re-rated, retired, deactivated)
    and a finished season has to stay readable afterwards. ``player_id`` links
    back so the bot can post the real card image when the lot opens.

    **The clock lives here**, not on the season: the anti-snipe extension must
    be applied in the same UPDATE as the bid that earned it.
    """
    __tablename__ = "auction_lots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season_id = Column(Integer, ForeignKey("auction_seasons.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id", ondelete="SET NULL"),
                       nullable=True, index=True)

    # ── The snapshot ───────────────────────────────────────────────────
    name = Column(String(150), nullable=False, index=True)
    rating = Column(Integer, default=70, nullable=False)
    category = Column(String(30), default="Batsman", nullable=False)
    country = Column(String(60), default="Unknown", nullable=False)
    # Whether this player is overseas FOR THIS AUCTION: their country differed
    # from AuctionSeason.home_country at pool-build time. A boolean because
    # ChallengePlayer.is_overseas — which this feeds on publish — is one.
    is_overseas = Column(Boolean, default=False, nullable=False)
    version = Column(String(50), nullable=True)
    bat_hand = Column(String(10), default="Right", nullable=False)
    bowl_hand = Column(String(10), default="Right", nullable=False)
    bowl_style = Column(String(30), default="Medium Pacer", nullable=False)
    bat_rating = Column(Integer, default=0, nullable=False)
    bowl_rating = Column(Integer, default=0, nullable=False)

    # ── The order sheet ────────────────────────────────────────────────
    set_name = Column(String(40), nullable=True)
    lot_no = Column(Integer, default=1, nullable=False)
    base_price_lakh = Column(Integer, default=20, nullable=False)
    # queued | on_block | sold | unsold | withdrawn
    status = Column(String(20), default="queued", nullable=False, index=True)

    # ── The live half ──────────────────────────────────────────────────
    deadline_at = Column(DateTime, nullable=True)
    # 0 = running, 1 = going once (<=10s), 2 = going twice (<=5s). A counter
    # rather than a boolean because the board says two different things, and a
    # bid resets it to 0.
    going_stage = Column(Integer, default=0, nullable=False)
    extensions_used = Column(Integer, default=0, nullable=False)
    # When the standing bid landed. The next bid from ANY franchise waits
    # ``AuctionSeason.bid_gap_seconds`` after it — checked inside the bid's own
    # conditional UPDATE, so two bids on one tick cannot both slip through.
    last_bid_at = Column(DateTime, nullable=True)
    current_bid_lakh = Column(Integer, nullable=True)
    current_bidder_id = Column(Integer, ForeignKey("auction_franchises.id",
                                                   ondelete="SET NULL"),
                               nullable=True)
    bid_count = Column(Integer, default=0, nullable=False)
    opened_at = Column(DateTime, nullable=True)
    # How many times this lot has gone unsold. An unsold player is RE-LISTED on
    # this same row (a fresh lot_no at the tail), never copied to a second one —
    # which is what keeps "one row per player per auction", and with it the
    # unique index that makes /bid's name lookup unambiguous.
    times_unsold = Column(Integer, default=0, nullable=False)

    # ── The result ─────────────────────────────────────────────────────
    sold_to_id = Column(Integer, ForeignKey("auction_franchises.id", ondelete="SET NULL"),
                        nullable=True, index=True)
    sold_price_lakh = Column(Integer, nullable=True)
    sold_at = Column(DateTime, nullable=True)

    # ── Retention / RTM ────────────────────────────────────────────────
    # ``previous_franchise_id`` IS populated in phase 1, from the previous
    # season's squads at pool-build time, even though only phase 2 reads it:
    # who held this player last season gets harder to recover as time passes,
    # not easier, and it is the entire input to Right To Match.
    previous_franchise_id = Column(Integer, ForeignKey("auction_franchises.id",
                                                       ondelete="SET NULL"),
                                   nullable=True)
    # auction | retained | rtm — how this player came to be on a squad.
    acquisition = Column(String(20), default="auction", nullable=False)
    # Which of the three RTM windows is open: intent | final_offer | decision,
    # NULL whenever the lot is not in one. ``deadline_at`` is reused for
    # whichever window it is, so this is what says which question the clock is
    # actually counting down on.
    rtm_stage = Column(String(16), nullable=True)
    # The bid that triggered the RTM. Kept so "did the top bidder actually
    # raise?" is answerable afterwards — the difference between ₹6 Cr and
    # ₹9 Cr is the whole story of the lot, and the bid log alone cannot say
    # which of them was the final offer.
    rtm_base_bid_lakh = Column(Integer, nullable=True)
    rtm_offered_at = Column(DateTime, nullable=True)
    rtm_matched_by_id = Column(Integer, ForeignKey("auction_franchises.id",
                                                   ondelete="SET NULL"),
                               nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    season = relationship("AuctionSeason", back_populates="lots",
                          foreign_keys=[season_id])
    player = relationship("Player")
    sold_to = relationship("AuctionFranchise", foreign_keys=[sold_to_id])
    current_bidder = relationship("AuctionFranchise", foreign_keys=[current_bidder_id])

    __table_args__ = (
        Index("ix_auction_lot_player_unique", "season_id", "player_id", unique=True),
        Index("ix_auction_lot_no_unique", "season_id", "lot_no", unique=True),
        Index("ix_auction_lot_status", "season_id", "status"),
    )


class AuctionBid(Base):
    """One bid. Append-only, losing bids included — this is the price's history.

    A bid is deliberately NOT a purse-ledger row. Nothing moves when you bid:
    you are outbid twenty seconds later and nothing happened. The purse is
    debited exactly once, when a lot sells. A ledger that recorded bids would
    need a matching reversal for almost every row, and it would make "undo the
    last bid" a money operation instead of a price one.

    ``is_void`` rather than DELETE, because an undone bid is part of why the
    price moved and the room watched it happen — and because it makes undo
    idempotent across a retry.
    """
    __tablename__ = "auction_bids"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season_id = Column(Integer, ForeignKey("auction_seasons.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    lot_id = Column(Integer, ForeignKey("auction_lots.id", ondelete="CASCADE"),
                    nullable=False, index=True)
    franchise_id = Column(Integer, ForeignKey("auction_franchises.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    amount_lakh = Column(Integer, nullable=False)
    # The co-owner who actually typed it, so a bid is recorded as theirs rather
    # than as the owner's.
    by_tg_id = Column(BigInteger, nullable=True)
    # tg | web | button
    source = Column(String(10), default="tg", nullable=False)
    is_void = Column(Boolean, default=False, nullable=False)
    voided_by_tg_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    season = relationship("AuctionSeason", back_populates="bids")
    lot = relationship("AuctionLot")
    franchise = relationship("AuctionFranchise")

    __table_args__ = (
        Index("ix_auction_bid_lot", "lot_id", "id"),
    )


class AuctionLedgerEntry(Base):
    """One movement of money in a franchise's purse.

    ``amount_lakh`` is SIGNED: a purchase is negative, a refund positive. The
    opening purse is itself a row (``kind='opening'``), so
    ``SUM(amount_lakh)`` over a franchise equals its
    ``purse_remaining_lakh`` — the invariant the test suite asserts after every
    single mutation, not merely at the end.

    ``balance_after`` is the cache's value at the moment the row was written,
    which makes the ledger self-checking: any row whose ``balance_after`` is not
    the previous one plus this ``amount_lakh`` is corruption you can find with
    one query, and a statement renders without a window function.

    ``player_name`` is kept alongside ``lot_id`` so the row still reads after a
    lot is deleted — the same call ``DraftSquadEdit.player_name`` makes.
    """
    __tablename__ = "auction_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season_id = Column(Integer, ForeignKey("auction_seasons.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    franchise_id = Column(Integer, ForeignKey("auction_franchises.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    # opening | retention | purchase | refund | correction | rtm
    kind = Column(String(20), nullable=False)
    amount_lakh = Column(Integer, nullable=False)
    balance_after = Column(Integer, nullable=False)
    lot_id = Column(Integer, ForeignKey("auction_lots.id", ondelete="SET NULL"),
                    nullable=True, index=True)
    player_name = Column(String(150), nullable=True)
    note = Column(String(200), nullable=True)
    by_tg_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    season = relationship("AuctionSeason", back_populates="ledger")
    franchise = relationship("AuctionFranchise")

    __table_args__ = (
        Index("ix_auction_ledger_franchise", "franchise_id", "id"),
    )


class AuctionEvent(Base):
    """The permanent auction log — and the queue the group is announced from.

    A separate table from ``AuctionBid`` for the reason ``DraftSquadEdit`` is
    separate from ``DraftTrade``: a bid has a franchise, an amount and a lot,
    always, and every one of them means something. An event has a kind and a
    headline, and its other columns are meaningful only for some kinds. One
    table would leave ``amount_lakh`` NULL on two thirds of the rows and turn
    "the bid history for this lot" into a filtered scan over the room's whole
    narrative.

    Its second job is what makes a two-surface auction safe. The Flask admin
    panel runs in a thread of the bot's process and must never touch Telegram —
    so an admin action writes its rows and ONE row here, commits, and returns.
    The bot's sweeper drains everything past
    ``AuctionSeason.announced_event_id`` and announces it, in id order, whether
    it came from the website or from a command. Nothing but the database
    crosses the boundary.
    """
    __tablename__ = "auction_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season_id = Column(Integer, ForeignKey("auction_seasons.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    lot_id = Column(Integer, ForeignKey("auction_lots.id", ondelete="SET NULL"),
                    nullable=True)
    franchise_id = Column(Integer, ForeignKey("auction_franchises.id", ondelete="SET NULL"),
                          nullable=True)
    # season_started | season_paused | season_resumed | season_cancelled
    # | season_completed | lot_opened | lot_sold | lot_unsold | lot_withdrawn
    # | lot_relisted | bid_undone | sale_undone | timer_extended
    # | purse_corrected | franchise_added | published | bid | relist_all
    # | set_queued | set_order | franchise_removed | autofill | retained …
    kind = Column(String(24), nullable=False)
    # Plain text, rendered once by whoever wrote the event, so the announcer
    # and the website print the same sentence.
    headline = Column(String(300), nullable=False)
    detail_json = Column(Text, nullable=True)
    by_tg_id = Column(BigInteger, nullable=True)
    by_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    season = relationship("AuctionSeason", back_populates="events")

    __table_args__ = (
        Index("ix_auction_event_season", "season_id", "id"),
    )


class AuctionRetentionOffer(Base):
    """An admin's offer to retain a player, waiting on the franchise's answer.

    Retention moves a franchise's money, so the franchise says yes to it: an
    admin proposes *who* and *for how much*, and only that franchise's owner or
    a co-owner can press Accept. Accepting runs the ordinary ``retain()`` at
    the offered price, so every cap still refuses exactly as it did before.
    """
    __tablename__ = "auction_retention_offers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season_id = Column(Integer, ForeignKey("auction_seasons.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    franchise_id = Column(Integer, ForeignKey("auction_franchises.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id", ondelete="CASCADE"),
                       nullable=False)
    player_name = Column(String(150), nullable=False)
    # NULL = the retention ladder's next slab, resolved when it is accepted.
    price_lakh = Column(Integer, nullable=True)
    # pending | accepted | declined | cancelled
    status = Column(String(12), default="pending", nullable=False, index=True)
    offered_by_tg_id = Column(BigInteger, nullable=True)
    answered_by_tg_id = Column(BigInteger, nullable=True)
    chat_id = Column(BigInteger, nullable=True)
    message_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    answered_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # At most ONE pending offer per player per season, held by the
        # database rather than a read-then-insert: two admins offering the
        # same player on the same tick would otherwise both pass the check,
        # and two franchises could then both accept him.
        Index("ix_auction_ret_offer_pending", "season_id", "player_id",
              unique=True,
              sqlite_where=text("status = 'pending'"),
              postgresql_where=text("status = 'pending'")),
    )


class AuctionAdmin(Base):
    """Somebody trusted to run auctions without being a bot admin.

    Bot admins (``services.admin_ids``) add them; they get every auction admin
    command and nothing else — every other admin gate in the bot still reads
    ``is_admin`` and never sees this table.
    """
    __tablename__ = "auction_admins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tg_id = Column(BigInteger, nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=True)
    added_by_tg_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════
# RANKED LADDER, RIVALRIES, SPECTATOR PREDICTIONS, HALL OF FAME
# ══════════════════════════════════════════════════════════════════════

class RankedRating(Base):
    """A user's skill rating on the 1v1 ranked ladder (see services/ranked_service).

    One row per user. ``season_key`` is the monthly season the numbers belong
    to; when it falls behind the live season the row is soft-reset lazily on
    its next read or write (after the old season has been finalized and paid).
    """
    __tablename__ = "ranked_ratings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, unique=True, index=True)
    season_key = Column(String(7), nullable=False, index=True)
    rating = Column(Integer, nullable=False, default=1000)
    peak_rating = Column(Integer, nullable=False, default=1000)
    played = Column(Integer, nullable=False, default=0)
    wins = Column(Integer, nullable=False, default=0)
    losses = Column(Integer, nullable=False, default=0)
    draws = Column(Integer, nullable=False, default=0)
    # All-time best, never reset — the Hall of Fame reads this.
    career_peak = Column(Integer, nullable=False, default=1000)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RankedSeasonResult(Base):
    """Archived final ladder position for one user in one finished season."""
    __tablename__ = "ranked_season_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    season_key = Column(String(7), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    rank = Column(Integer, nullable=False)
    rating = Column(Integer, nullable=False)
    division = Column(String(20), nullable=False)
    played = Column(Integer, nullable=False, default=0)
    wins = Column(Integer, nullable=False, default=0)
    prize_coins = Column(Integer, nullable=False, default=0)
    prize_gems = Column(Integer, nullable=False, default=0)
    recorded_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("season_key", "user_id", name="uq_ranked_season_user"),
    )


class Rivalry(Base):
    """The running series between two users (see services/rivalry_service).

    ``user_a_id`` is always the lower user id so a pair has exactly one row.
    A pair is a *rivalry* once ``played`` reaches the threshold; from then on
    the series is also played in rounds of five, and each round pays its winner.
    """
    __tablename__ = "rivalries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_a_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    user_b_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(120), nullable=True)
    played = Column(Integer, nullable=False, default=0)
    a_wins = Column(Integer, nullable=False, default=0)
    b_wins = Column(Integer, nullable=False, default=0)
    ties = Column(Integer, nullable=False, default=0)
    # Current rivalry round (only counted once the pair is a rivalry).
    round_no = Column(Integer, nullable=False, default=1)
    round_played = Column(Integer, nullable=False, default=0)
    round_a_wins = Column(Integer, nullable=False, default=0)
    round_b_wins = Column(Integer, nullable=False, default=0)
    rounds_a = Column(Integer, nullable=False, default=0)
    rounds_b = Column(Integer, nullable=False, default=0)
    # Current run of consecutive wins in the series, and whose it is.
    streak_user_id = Column(Integer, nullable=True)
    streak = Column(Integer, nullable=False, default=0)
    # Bonus-eligible rivalry matches this pair has played on ``bonus_day``
    # (UTC 'YYYY-MM-DD') — caps the paid matches per day.
    bonus_day = Column(String(10), nullable=True)
    bonus_matches_today = Column(Integer, nullable=False, default=0)
    last_match_id = Column(Integer, nullable=True)
    became_rivalry_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_a_id", "user_b_id", name="uq_rivalry_pair"),
    )


class MatchPrediction(Base):
    """A spectator's coin stake on who wins a live match (pari-mutuel pool)."""
    __tablename__ = "match_predictions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id", ondelete="CASCADE"), nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    pick_user_id = Column(Integer, nullable=False)   # the side (user) backed to win
    stake = Column(Integer, nullable=False)
    # open | won | lost | refunded
    status = Column(String(12), nullable=False, default="open", index=True)
    payout = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("match_id", "user_id", name="uq_prediction_match_user"),
    )


class MatchPostResult(Base):
    """Bookkeeping for the post-match layer, one row per match.

    Makes every post-match effect idempotent: the ladder and rivalry update run
    once (``ranked_done``), and the Hall of Fame scan reads each scorecard once
    (``hof_done``). ``payload_json`` keeps what the chat announcement needs and
    which user fielded which team name (the scorecard only knows team names).
    """
    __tablename__ = "match_post_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id", ondelete="CASCADE"),
                      nullable=False, unique=True, index=True)
    counted = Column(Boolean, nullable=False, default=True)
    ranked_done = Column(Boolean, nullable=False, default=False)
    # True when this match actually moved the ladder (not capped or unrated).
    rated = Column(Boolean, nullable=False, default=False)
    hof_done = Column(Boolean, nullable=False, default=False)
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class HallOfFameEntry(Base):
    """One record-worthy performance, harvested from a finished match.

    ``category`` names the board (see services/hall_of_fame.CATEGORIES);
    ``value`` is what the board sorts on (higher is better) and ``tiebreak``
    breaks ties the same way (e.g. fewer runs conceded is stored negated).
    """
    __tablename__ = "hall_of_fame_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(30), nullable=False, index=True)
    value = Column(Integer, nullable=False)
    tiebreak = Column(Integer, nullable=False, default=0)
    label = Column(String(60), nullable=False)          # e.g. "124* (58)"
    player_name = Column(String(120), nullable=True)
    team_name = Column(String(120), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    match_id = Column(Integer, ForeignKey("matches.id", ondelete="CASCADE"), nullable=True, index=True)
    achieved_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_hof_category_value", "category", "value", "tiebreak"),
    )


# ══════════════════════════════════════════════════════════════════════
# CMU NEWS + POLLS — the Mini App's community strip
# ══════════════════════════════════════════════════════════════════════

class NewsArticle(Base):
    """One CMU News story shown in the Mini App.

    Three sources write here: admins from the website (``admin``), players from
    the Mini App (``user`` — held ``pending`` until a bot admin approves it, the
    same moderation rule team logos follow), and the game itself (``auto`` —
    champions, record buys, season winners; see news_service.auto_story).
    """
    __tablename__ = "news_articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    headline = Column(String(160), nullable=False)
    body = Column(Text, nullable=False, default="")
    # asset_store key of the (re-encoded) image, e.g. "data/news/12.jpg".
    image_key = Column(String(300), nullable=True)
    # 'pending' | 'published' | 'rejected' | 'archived'
    status = Column(String(12), nullable=False, default="pending", index=True)
    # 'admin' | 'user' | 'auto'
    source = Column(String(10), nullable=False, default="admin")
    # For auto stories: which moment produced it (see news_service.AUTO_KINDS).
    kind = Column(String(30), nullable=True)
    # Unique so a retried hook can never post the same story twice.
    dedupe_key = Column(String(120), nullable=True, unique=True)

    # Not a foreign key: like TeamLogoRequest, the row is a moderation receipt
    # and should outlive the account it came from.
    author_user_id = Column(Integer, nullable=True, index=True)
    author_telegram_id = Column(BigInteger, nullable=True)
    author_name = Column(String(80), nullable=True)

    review_note = Column(String(300), nullable=True)
    reviewed_by = Column(String(80), nullable=True)
    reward_coins = Column(Integer, nullable=False, default=0)
    reward_paid = Column(Boolean, nullable=False, default=False)

    is_pinned = Column(Boolean, nullable=False, default=False)
    view_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    published_at = Column(DateTime, nullable=True)
    decided_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_news_status_published", "status", "published_at"),
    )


class NewsRead(Base):
    """A user has opened an article — drives the unread badge and view count."""
    __tablename__ = "news_reads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    article_id = Column(Integer, ForeignKey("news_articles.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    read_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("article_id", "user_id", name="uq_news_read"),
    )


class NewsReaction(Base):
    """One emoji reaction per user per article; changing it overwrites."""
    __tablename__ = "news_reactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    article_id = Column(Integer, ForeignKey("news_articles.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    user_id = Column(Integer, nullable=False)
    emoji = Column(String(8), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("article_id", "user_id", name="uq_news_reaction"),
    )


class Poll(Base):
    """An admin-run Mini App poll. Shown while ``is_active`` and inside its window."""
    __tablename__ = "polls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    question = Column(String(200), nullable=False)
    # JSON list of 2–6 option labels.
    options_json = Column(Text, nullable=False)
    starts_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    ends_at = Column(DateTime, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    # Coins paid once to each voter.
    reward_coins = Column(Integer, nullable=False, default=0)
    created_by = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PollVote(Base):
    """A user's single vote; the unique constraint is the one-vote guarantee."""
    __tablename__ = "poll_votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    poll_id = Column(Integer, ForeignKey("polls.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    user_id = Column(Integer, nullable=False)
    option_index = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("poll_id", "user_id", name="uq_poll_vote"),
    )


class StartVisit(Base):
    """One row per Telegram user per IST day that they ran /start.

    ActivityLog needs a ``users`` row, so a visitor who opens the bot and
    leaves before /debut never shows up there. This table is the top of the
    onboarding funnel on the website (services/retention_stats.py).
    """
    __tablename__ = "start_visits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False)
    day = Column(String(10), nullable=False)  # IST 'YYYY-MM-DD'
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("telegram_id", "day", name="uq_start_visit_day"),
        Index("ix_start_visit_day", "day"),
    )
