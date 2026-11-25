# CMU Community Arena & Engagement Engine

A self-contained engagement system for the Official CMU Telegram Group:
Community XP (CXP), daily check-ins, six automatic mini-games, community levels,
achievements, leaderboards, a restart-safe scheduler, an admin dashboard and a
Mini App section. It is **additive** — it does not modify the cricket simulation
engine, player ratings, original card images or the coin/gem economy, and every
feature path is best-effort so a Community Arena failure can never break core
gameplay.

---

## 1. What was implemented

| Area | Status |
|---|---|
| Community XP ledger (idempotent, capped, IST periods) | ✅ Complete |
| Chat XP + anti-spam (in-memory fast path, batched DB) | ✅ Complete |
| Daily `/checkin` with streaks | ✅ Complete |
| Match-completion XP (PvP + AI, Telegram & Mini App, origin-validated) | ✅ Complete |
| Successful-claim XP | ✅ Complete |
| Six games (Mystery Card, Odd One Out, Cricket Wordle, Fastest Fingers, Guess the Rating, Guess the Cricketer) | ✅ Complete |
| Centralized answer validation + single-winner atomic selection | ✅ Complete |
| Winner / timeout announcements (rich-text + HTML fallback) | ✅ Complete |
| Community levels (15) + one-time milestone rewards | ✅ Complete |
| Community achievements (13) | ✅ Complete |
| Leaderboards (`/gctop`, 4 periods, many categories, user rank, tie-breaks) | ✅ Complete |
| Automatic engagement scheduler (restart-safe, inactivity, weights, match protection) | ✅ Complete |
| Telegram commands (user + admin) | ✅ Complete |
| Admin dashboard (settings, analytics, event management, user management + audit) | ✅ Complete |
| Mini App Community Arena page + JSON API (Telegram initData auth) | ✅ Complete |
| Automated tests (30 required areas) | ✅ Complete |

### Known limitations / deliberate choices
- **Chat XP** records *rewarded eligible* messages only; by design the bot never
  stores raw group conversation text (only short-lived fingerprints), so admin
  analytics report "eligible rewarded messages", not total human messages.
- **Guess the Rating** is presented as a text card (name/country/category/version
  + "Overall Rating: ???") exactly as in the spec example, rather than a
  rating-masked card image, which would require fragile per-template pixel
  masking. **Mystery Card** blurs the whole generated card (name/photo/metadata
  all unreadable) with a configurable Gaussian radius; the original card is never
  modified and is revealed at the end.
- **Cricket Wordle** feedback is delivered by DM (to avoid flooding the group);
  if the user has never opened a DM with the bot, a short in-group hint is sent
  instead. Mini App answer submission is not wired — answers are read server-side
  from group messages (which is where fairness/first-correct is enforced).
- **Bot command menu**: the project's published menu is already over Telegram's
  100-command-per-scope ceiling (125 entries), so the new commands are **not**
  added to the menu to avoid dropping existing ones. They are fully functional
  and discoverable via `/gchelp`.
- **Milestone monetary rewards** (coins/gems/packs) are disabled by default and
  seeded with cosmetic badge/title only; enable and configure per level in the DB
  (`community_milestone_rewards`). The admin UI exposes the ladder read-only for
  now.

---

## 2. Files created

**Models**
- `models_community.py` — all Community Arena tables (imported at the end of `models.py`).

**Services**
- `services/community_config_service.py` — single-row config (defaults ⊕ JSON overrides), cache.
- `services/community_xp_service.py` — XP ledger, caps, levels, IST periods, admin adjust.
- `services/community_chat_service.py` — chat XP + anti-spam (in-memory fast path).
- `services/community_checkin_service.py` — daily check-in + streaks.
- `services/community_answer_service.py` — normalization, alias matching, (optional) fuzzy.
- `services/community_game_service.py` — generators, persistence, winner selection, scoring.
- `services/community_level_service.py` — level ladder + milestone grants.
- `services/community_achievement_service.py` — achievements.
- `services/community_leaderboard_service.py` — leaderboards over the ledger.
- `services/community_scheduler.py` — restart-safe sweep + auto launch.
- `services/community_hooks.py` — match/claim integration hooks.
- `services/community_profile_service.py` — profile/cxp/game summaries.
- `services/community_rich.py` — rich-text + HTML renderers.
- `services/community_admin_service.py` — analytics + settings parsing + user management.

**Handlers**
- `handlers/community.py` — user & admin commands.
- `handlers/community_games.py` — game announce/reveal + group answer router.

**Admin / Mini App**
- `admin_community.py` — admin routes + Mini App JSON API + Mini App page route.
- `templates/admin_community.html`, `templates/admin_community_users.html`, `templates/webapp_community.html`.

**Tests**
- `tests/test_community_arena.py` — 26 tests covering the 30 required areas.

## 3. Files modified (minimal, additive)
- `models.py` — appended Community Arena model imports.
- `database.py` — registered models in `init_db`, added `_seed_community_arena()` (levels + milestone placeholders; only fills empty tables).
- `bot.py` — handler imports, Community activity middleware (group −39), command/callback/message-handler registration (answer router at group 11), scheduler start.
- `services/match_manager.py`, `services/match_webapp_service.py` — call `on_match_completed` after a match commits.
- `handlers/claim.py` — call `on_claim_success` after a claim commits.
- `templates/base.html` — "Community" nav section.

---

## 4. Database models (all new tables, auto-created on boot)

`community_profiles`, `community_xp_ledger`, `community_daily_counters`,
`community_games`, `community_game_participants`, `community_game_stats`,
`community_levels`, `community_milestone_rewards`, `community_milestone_claims`,
`community_achievements`, `community_config`, `community_scheduler_state`,
`community_audit_log`.

Indexes and unique constraints enforce idempotency (`community_xp_ledger.event_key`
unique; `(user_id, day, activity)`, `(game_id, user_id)`, `(user_id, level)`,
`(user_id, code)` uniques) and efficient period/leaderboard queries. New tables
are created by the existing `_create_missing_tables()` path on both PostgreSQL and
SQLite; no existing table is altered, so migrations are safe and non-destructive.

---

## 5. Telegram commands

| Command | Function |
|---|---|
| `/community [@user]` | Community profile (self, reply, or @username) |
| `/cxp` | XP balance & progress |
| `/checkin` | Daily group check-in (Official GC only) |
| `/gctop` | Leaderboards with Today/Week/Month/All tabs |
| `/gcgames` | Community game stats |
| `/gclevels` | Level ladder |
| `/gcrewards` | Milestone rewards |
| `/gchelp` | Community guide |
| `/gcevent <game> [difficulty]` | **Admin** — launch a game in the Official GC |
| `/gcstop` | **Admin** — cancel the active game |
| `/gcsettings [on\|off] [auto on\|off]` | **Admin** — toggle + view settings |

Players never type a command to answer a game — they just reply in the group.

---

## 6. Admin dashboard (website → Community → Community Engagement)

- **General settings**: enable/disable arena + individual games; XP values, daily
  limits, chat cooldown/min-chars; per-game duration/reward/daily-win-limit;
  scheduler active hours, max/day, min gap, inactivity, weights, match protection;
  eligible rating range; fuzzy matching. (Official group reuses existing Branding config.)
- **Event management**: launch any game now (via the bot bridge), preview a
  question, cancel the active game.
- **Analytics** (real DB metrics, IST periods): active members, CXP awarded, CXP
  by activity, eligible messages, matches, claims, check-ins, games launched/won/
  unanswered, completion rate, avg winning response time, avg/unique participants,
  most popular game, peak hours, new vs returning members, retention %
  (explicitly defined; "n/a" where not derivable).
- **User management** (`Community Members`): search, inspect profile + XP ledger,
  adjust CXP (mandatory reason → audit), suspend/re-enable reward eligibility,
  reset game stats (typed confirmation). Every change writes a `community_audit_log` row.

---

## 7. Mini App

- Page: `GET /webapp/community` (self-contained, dark/light themed).
- API (Telegram `initData`-authenticated, CSRF-exempt, ban/maintenance-gated):
  - `GET /api/webapp/community/overview` — profile, level, XP, check-in status, active game, milestone rewards.
  - `GET /api/webapp/community/leaderboard?category=&period=&page=`.
  - `GET /api/webapp/community/games` — game stats + achievements.
- Never trusts client-supplied XP/results/answers; all state is server-derived.

---

## 8. Required environment / configuration

No new environment variables are required. Existing ones used:
- `BOT_TOKEN`, `BOT_USERNAME` — bot identity & Mini App auth.
- `WEBAPP_URL` (https base, e.g. `https://host/webapp`) — the `/community` and
  `/cxp` replies add an "Open Community Arena" button pointing at
  `…/webapp/community` when this is set.
- Admin auth via existing admin website login; admin bot commands via
  `BOT_ADMIN_IDS`/`OWNER_IDS` (`services.admin_ids`).

**To go live:**
1. Set the **Official Group** under Website → Branding (chat id + join link) — the
   arena reads `GameConfig.official_group_id`; nothing is hardcoded.
2. Add the bot to the Official Group with permission to read ordinary messages.
   **Disable BotFather group privacy mode** (`/setprivacy` → Disable) so the bot
   receives non-command group messages — required for chat XP and game answers.
3. Website → Community → Community Engagement → tick **Enable Community Arena**
   (and **Automatic engagement** for auto games), review settings, Save.

**Deployment:** standard — no new dependencies (uses the already-required Pillow
for card blurring). New tables auto-create on next boot via `init_db`; the level
ladder + milestone placeholders seed automatically (only when empty).

---

## 9. Tests

`tests/test_community_arena.py` (26 tests) covers: chat eligibility, cooldown,
daily limits, duplicate/spam rejection, official-group restriction, claim
integration (and exclusion), PvP/AI match integration, abandoned/non-official
exclusion, check-ins & streaks, IST boundaries, all six generators, Guess-the-
Cricketer uniqueness, Odd-One-Out validity, Wordle repeated-letter scoring, exact
rating validation, alias/normalization, single-winner & duplicate-reward safety,
expiry, scheduler restart recovery, single-active-game protection, level
calculation, milestone idempotency, leaderboard periods, rich HTML fallback, and
admin settings/adjust/suspend + audit.

```
python -m pytest tests/test_community_arena.py -q
```

Existing suites were re-run for the touched areas (match rewards, streaks, season
activity) with no regressions.
