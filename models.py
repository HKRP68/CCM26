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

    user1 = relationship("User", foreign_keys=[user1_id])
    user2 = relationship("User", foreign_keys=[user2_id])

    __table_args__ = (
        Index("ix_matches_status", "status"),
        Index("ix_matches_winner", "winner_id"),
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
    # Refresh schedule (IST). 0 = midnight IST, 9 = 9 AM IST, etc.
    market_refresh_hour_ist = Column(Integer, default=0)
    # Trait market settings
    trait_market_default_slots = Column(Integer, default=5)
    trait_market_last_refresh_at = Column(DateTime, nullable=True)
    # The trait market runs its own schedule, independent of the player market
    # above: it refreshes every `interval` hours starting from `start_hour`
    # (IST). Interval 12 + start 0 means 12 AM and 12 PM, every day. NULL start
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
    # Comma-separated telegram IDs allowed to use the Challenge League Tournament
    # command. Empty/None = open to everyone (restriction off).
    tournament_allowed_ids = Column(String(500), nullable=True)
    # Global gameplay style for all newly started matches: original Telegram
    # callback buttons (default) or the optional Mini App live board.
    match_style = Column(String(20), default="telegram", nullable=False)
    challenge_max_overs = Column(Integer, default=2, nullable=False)
    allow_same_team_challenge = Column(Boolean, default=False, nullable=False)
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
    main_weights_json = Column(String(500), nullable=True)
    # JSON list of acceptable version names (case-insensitive match).
    # e.g. ["Star", "Star Card", "Star Player"]. Used when filter_mode is
    # 'version' or 'both'.
    main_versions_json = Column(String(500), nullable=True)

    # Bonus player(s) — extras included with the pack. Always rating-based,
    # any version (we don't restrict bonus pulls).
    bonus_min_rating = Column(Integer, default=74)
    bonus_max_rating = Column(Integer, default=80)
    bonus_count = Column(Integer, default=2)
    bonus_weights_json = Column(String(500), nullable=True)

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
    """A Challenge League Tournament. Only one may be ``is_active`` at a time."""
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

    # Lifecycle: draft / scheduled / active / paused / completed / cancelled
    status = Column(String(20), default="draft", nullable=False, index=True)
    # The single currently-selected tournament whose command is live. At most one
    # row may be True; activating one deactivates all others.
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
    """A team participating in a tournament, with its running standings."""
    __tablename__ = "tournament_teams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tournament_id = Column(Integer, ForeignKey("tournaments.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    challenge_team_id = Column(Integer, ForeignKey("challenge_teams.id", ondelete="SET NULL"),
                               nullable=True, index=True)
    # Group assignment for ``groups``-format tournaments; NULL otherwise.
    group_id = Column(Integer, ForeignKey("tournament_groups.id", ondelete="SET NULL"),
                      nullable=True, index=True)
    name = Column(String(120), nullable=False)
    short_name = Column(String(30), nullable=True)
    logo_url = Column(String(500), nullable=True)

    # Standings
    played = Column(Integer, default=0, nullable=False)
    won = Column(Integer, default=0, nullable=False)
    lost = Column(Integer, default=0, nullable=False)
    tied = Column(Integer, default=0, nullable=False)
    no_result = Column(Integer, default=0, nullable=False)
    points = Column(Integer, default=0, nullable=False)

    # Net run-rate data
    runs_for = Column(Integer, default=0, nullable=False)
    balls_for = Column(Integer, default=0, nullable=False)
    runs_against = Column(Integer, default=0, nullable=False)
    balls_against = Column(Integer, default=0, nullable=False)

    sort_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    tournament = relationship("Tournament", back_populates="teams")

    __table_args__ = (
        Index("ix_tournament_team_unique", "tournament_id", "challenge_team_id", unique=True),
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
