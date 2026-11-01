# Ranked and season scoring

Only completed, counted human PvP matches in supported ranked modes, scheduled
for at least five overs, qualify. Bot games, practice, spectator games, casual
modes, unfinished games and stat-disabled mismatches do not qualify. A tied
ranked match can adjust ratings but gives neither player season points.

## Opponent strength

Ranked rating continues to use Elo with K=40 for the first ten placements and
K=24 afterwards. An upset earns more; beating a weaker player earns less. A
win against someone at least 400 rating below earns zero ranked gain and zero
season points. Losses in rated matches keep their normal Elo cost, including
the larger cost of losing to a weaker player.

Let `E = 1 / (1 + 10 ** ((opponent_rating - player_rating) / 400))`.
An eligible win earns `floor(50 * (1 - E) * repeat_multiplier)` season points,
using both players' ratings before the match. The 400-point cutoff also applies.
Only a win that earns positive season points increments season wins.

| Opponent compared with you | Season points on first meeting |
| --- | ---: |
| 400 rating higher | 45 |
| 200 rating higher | 37 |
| Equal rating | 25 |
| 200 rating lower | 12 |
| 400+ rating lower | 0 |

Losses, draws, Quick Match results and generic coin/gem reward helpers award no
match season points. Successful game activities can earn the separate bonuses below.

## Game activity points

Activities add at most **40 season points per user per UTC day**, shared across
the bot and Mini App. Ranked match points do not use this daily allowance.

| Successful activity | Points each | Rewarded completions per UTC day |
| --- | ---: | ---: |
| Claim a player | 3 | 3 |
| Claim daily rewards | 5 | 1 |
| Claim login reward | 3 | 1 |
| Open a pack | 2 | 3 |
| Claim a free pack | 2 | 1 |
| Claim a quest reward | 3 | 3 |
| Upgrade a player | 2 | 1 |
| Upgrade your Career Player | 2 | 1 |
| Complete a player or trait trade (shared category) | 2 | 1 |
| Apply a trait | 2 | 1 |
| Unlock an achievement | 3 | 2 |
| Claim a Lucky Card reward | 1 | 1 |
| Claim weekly rewards | 5 | 1 |
| Open a reward chest | 3 | 1 |

Only completed actions count: previews, failed purchases, cooldown rejections,
repeat claims and empty claim-all requests earn nothing. Buying packs alone
does not award points; actually opening them does. Pack price, subscription
level and ad multipliers never multiply activity points. Category limits and
the shared cap limit spending-based progress. Activity points never change
ranked ratings or season wins. The season page shows today's allowances.

`SeasonActivityAward` records credited events and enforces unique event keys.
Awards lock the existing user row before checking the day's allowance, so
concurrent actions share the same budget. Atomic balance updates preserve
simultaneous match and activity credits. The ledger and credit roll back with
the action's transaction; failed bonus bookkeeping is isolated in a savepoint.

## Repeat opponents

Count the pair's rated meetings in the preceding rolling 24 hours. Both player
orders count as the same pair; crossing UTC midnight does not reset the count.

| Meeting | Multiplier on positive ranked gains and season points |
| --- | ---: |
| First | 100% |
| Second | 50% |
| Third | 25% |
| Fourth onward | Unrated; no ranked changes or season points |

Losses in the first three meetings are not discounted. Positive rating gains
are rounded down, with no guaranteed minimum point. Draw gains are discounted
too, preventing players from farming repeated draws between unequal ratings.
Playing a different opponent uses that pair's own history.

## Accounting and rollout

The post-match hook applies rating and season awards in the same savepoint.
`MatchPostResult` records the result once per match, including the point
breakdown used by the chat card. Retrying a completed result cannot pay season
points twice. A failed season award rolls back the ranked update too, allowing
a later retry.

The existing monthly reset, division thresholds and prizes are retained.
Startup creates the new `season_activity_awards` table through the existing
`Base.metadata.create_all` setup. Existing season totals remain; activities and
matches processed after rollout use the new rules. Historical activity logs are
not backfilled.

## Suggested follow-ups

* Require at least five distinct ranked opponents before season prizes can be
  earned. This would make coordinated small-group farming less attractive.
* Prefer matchmaking within 200 rating points, widening the search as waiting
  time increases, so most matches offer both players meaningful rewards.
* Review suspicious alternating wins across linked accounts. Repeat-opponent
  reductions limit one pair but cannot identify a network of alternate accounts.
