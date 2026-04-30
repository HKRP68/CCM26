# 🏏 Cricket Simulator Telegram Bot — Phase 1 + 2

A Telegram bot where users collect cricket player cards, build rosters, trade with friends, and maintain daily streaks. 3,165 real cricket players.

## All Commands

| Command | Description | Cooldown |
|---------|-------------|----------|
| `/start` | Welcome message | — |
| `/debut` | Create account, 8 starter players + 5,000 coins + 100 gems | Once |
| `/claim` | Get 1 rarity-weighted player + 500 coins | 1 hour |
| `/daily` | 5,000 coins + 2 random players + streak tracking | 24 hours |
| `/gspin` | Spin reward wheel (coins / gems / players) | 8 hours |
| `/myroster` | Paginated roster with stats | — |
| `/playerinfo [name]` | Full stats + card image | — |
| `/release [name]` | Release a player for sell value coins | — |
| `/releasemultiple` | Release duplicate players in bulk | — |
| `/trade @user` | Trade same-rating players with another user | — |

## Phase 2 Features

### Roster Management
- Paginated roster display (10 per page with ◀️▶️ navigation)
- Roster stats: average rating, total value, duplicate count
- `/release` with confirm/cancel buttons and sell value display
- `/releasemultiple` auto-detects duplicates, release 1 or N at a time

### Player Trading
- `/trade @username` starts a 4-step inline flow:
  1. Find matching ratings (both users need 75+ OVR at same rating)
  2. Select your player to offer
  3. Select their player to receive
  4. Confirm trade with fee breakdown
- 5% trade fee deducted from both users (based on buy value)
- 20-second expiry on trade offers
- Receiver gets DM notification with Accept/Reject buttons
- Both parties notified on completion
- Only same-rating trades allowed (rating >= 75 OVR)

### Trade Rules
- Minimum rating: 75 OVR
- Fee: 5% of card buy value, from both sides
- Offer expires: 20 seconds
- Max 1 pending trade per user
- Only same-rating swaps

## Project Structure

```
cricket_bot/
├── bot.py                          # Entry point (all handlers registered)
├── config.py                       # Constants, values, trade rules
├── database.py                     # SQLAlchemy engine & session
├── models.py                       # User, Player, UserRoster, UserStats, Trade
├── logger.py                       # Logging setup
├── seed_players.py                 # 3,165 player seed from JSON
├── data/
│   └── players.json                # Real cricket player dataset
├── services/
│   ├── player_service.py           # Random player by rarity
│   ├── cooldown_service.py         # Cooldown checking
│   ├── streak_service.py           # Daily streak logic
│   ├── card_generator.py           # PNG card generation (Pillow)
│   ├── roster_service.py           # Roster stats, release, duplicates
│   ├── rating_matcher_service.py   # Trade matching & validation
│   └── trading_service.py          # Initiate/accept/reject/expire trades
├── handlers/
│   ├── debut.py                    # /debut
│   ├── claim.py                    # /claim + Retain/Release buttons
│   ├── gspin.py                    # /gspin
│   ├── daily.py                    # /daily
│   ├── myroster.py                 # /myroster (paginated)
│   ├── playerinfo.py               # /playerinfo
│   ├── release.py                  # /release + /releasemultiple
│   └── trade.py                    # /trade (4-step inline flow)
├── logs/
├── Procfile                        # Railway / Render
├── Dockerfile
├── runtime.txt
├── requirements.txt
└── .env.example
```

## Setup

### Local
```bash
cd cricket_bot
pip install -r requirements.txt
cp .env.example .env
# Add your BOT_TOKEN from @BotFather
python bot.py
```
Auto-creates SQLite DB and seeds 3,165 players on first run.

### Railway
1. Push to GitHub
2. railway.app → New Project → Deploy from GitHub
3. Add env var: `BOT_TOKEN=your_token`
4. Optional: add PostgreSQL plugin, set `DATABASE_URL`

### Render
1. Push to GitHub
2. render.com → New → Background Worker
3. Build: `pip install -r requirements.txt` | Start: `python bot.py`
4. Add env var: `BOT_TOKEN=your_token`

### Docker
```bash
docker build -t cricket-bot .
docker run -d --env-file .env cricket-bot
```

## Database Models
- **User** — account, coins, gems, roster count
- **Player** — 3,165 cricket players with batting/bowling stats
- **UserRoster** — ownership (supports duplicates)
- **UserStats** — cooldowns, streak data
- **Trade** — trade records with status, fees, expiry

## Tech Stack
- Python 3.11 + python-telegram-bot 21.x (async)
- SQLAlchemy 2.0 ORM
- Pillow (card image generation)
- SQLite (default) / PostgreSQL (production)
