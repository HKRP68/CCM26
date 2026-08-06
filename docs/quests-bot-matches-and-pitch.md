# Quests: AI matches, the daily cap, and pitch quests

Two changes to what moves a quest bar, plus the operational notes for both.

## AI matches complete quests again

`/wpmbot` and `/vsbot` — the two AI modes that pay coins, gems and a Win/Loss
record — now feed match-end quests. Previously every bot match was refused by
`services.quest_service.match_counts_for_quests`, so a player with nobody online
could win ten matches and watch "Play 3 matches today" sit at 0/3.

The anti-farming concern that closed the door originally is still real: the AI is
always available and never refuses. So the door is open with a limit rather than
wide open.

**The daily cap.** Each user may feed quests from the first
`BOT_QUEST_MATCH_DAILY_CAP` AI matches per UTC day (default **3**, settable via
the environment variable of the same name; `0` disables AI quest progress
entirely). The counter lives on the user row —

| column | meaning |
| --- | --- |
| `users.bot_quest_matches_today` | AI matches that fed quests today |
| `users.bot_quest_matches_date` | the UTC date those belong to (`YYYY-MM-DD`) |

— and resets lazily on the first bot match of a new day, the same pattern Quick
Match's daily limit uses. Both columns are added by
`database._migrate_add_columns` on boot; there is nothing to run by hand.

A bot match past the cap still pays out in full. It just stops moving quest bars.

**What still doesn't count.** `/lpbot` and `/ciplbot` are explicitly *unranked
practice* — `handlers.cipl_play.mark_bot_match` sets `stats_disabled` and
`unranked`, and they already pay no coins, gems, W/L or streak. They stay
refused, alongside spectator matches, bot-vs-bot matches, and matches the
fair-match gate voided for a 10+ Team Overall gap.

**Two event keys came back.** `vsbot_played` and `vsbot_won` were retired when
bot matches stopped counting; they now fire again, from AI matches only, and are
selectable on the admin quest form. A quest on either key can never be completed
in a PvP game — and its `target_count` must be at or under the daily cap, or it
cannot be completed at all.

## Pitch quests

`track_user_match_quests` now fires one event per finished match naming the
surface it was played on, so a quest can ask for "play 3 matches on a Green
pitch today".

The key is `pitch_<surface lowercased>`, built by
`services.quest_service.pitch_event_key` from the match state's own
`pitch_type`. The surfaces come from `services.match_constants.PITCH_TYPES`, so
adding a pitch there adds its quest key with no second list to update:

`pitch_dry` · `pitch_dusty` · `pitch_hard` · `pitch_even` · `pitch_flat` ·
`pitch_green` · `pitch_bouncy`

The pitch is randomised per match (or chosen by the host in Challenge League), so
these quests ask a player to keep playing until the surface comes up rather than
to grind a stat — which is why the seeded targets are 2–3, not 10.

## Rolling the new quests out

The seven pitch quests and the two AI quests are in `seed_quests_v3.py`. They are
**not** live until that script is run against the database:

```
python seed_quests_v3.py --dry-run   # report what would change
python seed_quests_v3.py             # replace the daily/monthly catalogue
```

The seeder is idempotent and self-cleaning: a quest already present by name is
updated in place, and any other active daily/monthly quest is deactivated.
Pinned (`always_assign`) and Career Player quests are never touched.

Until it runs, everything above is inert — the events fire, but no quest is
listening for them.
