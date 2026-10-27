# Dynamic 1.5 Round Robin

## Recommended for any even number of teams: Two-Group 1.5 RR

Select **Two-Group 1.5 RR · Any even team count** on Create. It supports any even
number of at least four teams, including 8, 10, 12, 14, 16 and larger fields.
For `N` teams, split them into two equal groups of `N / 2`:

| Opponents | Games per opponent | Games per team |
| --- | --- | --- |
| `N / 2 − 1` teams in the same group | 1 | `N / 2 − 1` |
| `N / 2` teams in the other group | 2, home and away | `N` |
| Total | | **`3N / 2 − 1`** |

| Teams | Equal groups | Games per team | League fixtures | Rounds |
| --- | --- | --- | --- | --- |
| 8 | 4 + 4 | 11 | 44 | 11 |
| 10 | 5 + 5 | 14 | 70 | 14 |
| 12 | 6 + 6 | 17 | 102 | 17 |
| 14 | 7 + 7 | 20 | 140 | 20 |
| 16 | 8 + 8 | 23 | 184 | 23 |
| 20 | 10 + 10 | 29 | 290 | 29 |
| 32 | 16 + 16 | 47 | 752 | 47 |

Every round is full, with no byes. Home and away totals differ by at most one;
even game totals split exactly. Repeat meetings are at least `N / 2` rounds
apart. The preview reports the calculated home/away totals, rematch spacing
and longest home/away run for the actual number of registered teams.

Use **one combined points table** and qualify the top four for **Qualifier 1,
Eliminator, Qualifier 2 and Final**. This is the preset's default for any supported
even field, adding four playoffs to the calculated league fixture count. No
playoffs, semifinals or a configurable Top N are also available. Group membership
decides repeat opponents; it does not reserve qualifying places for either group.

The draw splits each adjacent seed pair between the groups in snake order:
seeds 1, 4, 5, 8, 9, ... enter A; 2, 3, 6, 7, 10, ... enter B. Seed order comes from the
admin's team priority, with team id resolving equal priorities. This does not
infer team strength from player ratings. Set priorities by strength or prior
season standings, then use **Draw balanced groups from team order** on Manage.
Memberships can also be assigned manually; exactly two equal groups must be
complete before confirming fixtures. Redrawing never touches a live or completed
schedule. Groups and fixtures persist across restarts.

For `N = 2m`,
each team plays `3m − 1` games, the league contains `N × (3m − 1) / 2` fixtures,
and there are exactly `3m − 1` rounds. With odd group sizes, the two internal bye
teams play one another across groups, so league rounds remain full. The other
cross-group matches form complete bipartite matchings. Every repeated pair gets
one home match each; internal fixtures distribute home games evenly. Even game
totals have an exact home/away split; odd totals differ by one.

Recommended season-to-season improvements: seed from the last season's final
table, review or rotate group membership to vary repeat opponents, keep top-four
IPL playoffs to reward the top two, and use shared round pitches and release
batches. These use the existing admin controls; the system does not silently
reseed or redraw a tournament on restart.

For example, ten teams produce 70 league fixtures in 14 rounds and seven home
plus seven away games each, adding two fixtures compared with the 68-fixture
nearest-target scheduler below. The two-group preset is separate so odd-team
schedules, custom targets and existing formats keep their behavior.

## Standard dynamic 1.5 RR and custom targets

On **Tournaments → Create**, select **1.5 Round Robin**, choose at least two
teams and optionally enter a **league games per team target**. Blank means
`1.5 × (N − 1)`. Choose no playoffs, top-four semifinals, IPL playoffs, or
**Top N → Seeded elimination** with a configurable number of qualifiers.

Creation saves a draft and opens an exact schedule preview. It shows all
fixtures, both phases, round and team filters, team game totals and byes.
**Confirm and save** persists those fixtures. A stale preview cannot overwrite
a schedule after its team list or target changes. Manage and Schedule both
link back to the preview for regeneration. Live or completed fixtures block
replacement; invalid targets are rejected before any fixtures are removed.

Existing tournaments can select **Manage → League stage → 1.5 Round Robin**.
Structural changes require a reset once league play starts. The shared scheduler
also supports Lets Play teams through that tournament's Manage page.

## Actual default schedules

The target is an average number of league games per team, including both phases.
The nearest whole fixture total is selected; exact half-fixture ties round up.
An explicit target can range from `N − 1` to `2 × (N − 1)`, inclusive.
When equal integer team totals cannot meet that fixture count, totals differ
by exactly one. The preview explains the chosen distribution.

| Teams | Target per team | Actual per team | Phase 1 fixtures | Repeat fixtures | League fixtures | Rounds (phase 1 + phase 2) |
| --- | --- | --- | --- | --- | --- | --- |
| 8 | 10.5 | 10–11 | 28 | 14 | 42 | 7 + 4 |
| 10 | 13.5 | 13–14 | 45 | 23 | 68 | 9 + 5 |
| 11 | 15 | 15–16 | 55 | 28 | 83 | 11 + 6 |
| 12 | 16.5 | 16–17 | 66 | 33 | 99 | 11 + 6 |
| 16 | 22.5 | 22–23 | 120 | 60 | 180 | 15 + 8 |

For 11 teams, ten teams play 15 games and one plays 16. A different explicit
target changes the actual fixture total and distribution; there are no
team-count-specific scheduling branches.

## Construction and validation

`services.partial_round_robin.build_schedule` is a deterministic, database-free
planner. Phase 1 uses the existing circle method: every pair meets once, with
`N − 1` rounds for even fields or `N` for odd fields.

For `M` repeat fixtures, a round holds at most `floor(N / 2)` games. The repeat
phase starts with `ceil(M / floor(N / 2))` circle rounds, truncating the last.
Alternating-component exchanges balance each team's repeat count. Each exchange
swaps two team labels along linked fixtures sharing either a round or an
opponent. This preserves unique opponent pairs and one game per team per round
while moving one game from an over-scheduled team to an under-scheduled team.
Rounds stay fixed, so the capacity lower bound is attained. Repeat fixtures
reverse the first meeting's home side.

Validation checks all pair frequencies, complete phase-1 coverage, round
ordering, team membership, self-fixtures, round conflicts, team balance,
fixture totals, minimum repeat rounds and every bye before anything is saved.

## Standings, playoffs and existing controls

New 1.5-RR tournaments use **points → NRR → wins → head-to-head → admin
priority**. Lower admin priorities rank first; equal priorities fall back to
team id. Set priorities per team on Manage. Other formats retain their existing
standings rule; the new rule is also available in their settings and through
`/ttiebreak nrr_wins_h2h`.

Default points remain win 2, loss 0, tie 1 and no result 1. A manual **No result**
records that outcome separately from a tie, awards the configured no-result
points, and excludes abandoned innings from NRR. Completed results from both
phases update the same points table. Playoffs wait for the whole league,
including the repeat phase, to finish; configured brackets then seed
automatically. Top-N elimination supports non-power-of-two fields through
byes, with N − 1 played fixtures. Playoff results advance the bracket without
changing league standings.

Round pitches, release batches, deadlines, ownership, manual result recording,
simulation and replay controls all use the existing tournament services.
The saved schedule supports a team filter and displays phase numbers and byes.
Qualification places are shared across the admin dashboard, bot and Mini App.

## Persistence and rollout

The existing startup migration adds nullable `tournaments.league_match_target`,
`tournament_matches.league_phase` and `tournament_matches.result_type` columns.
It is idempotent and preserves existing rows. Fixtures keep the existing
`round_no`, `match_no`, stage and pitch fields; no extra round table or background
regeneration is needed. Restart the admin and bot processes after deployment to
apply the schema additions. Restarting reads saved fixtures without rebuilding
them.

`tests/test_partial_round_robin.py` covers default fields from 2–65 teams, every
feasible repeat total for 2–17 teams, the PDF examples, invalid plans and the
standings order. `tests/test_partial_round_robin_admin.py` covers draft/preview/
confirmation, stale previews, custom targets and qualification, persisted
fixtures, migrations, manual ties/no results, simulation and automatic playoff
progression through the champion.
`tests/test_cross_group_schedule.py` independently checks every even field from
4–64 teams, larger fields through 128 teams and all group partitions through ten teams, including opponent
frequencies, optimal round counts, home balance and repeat spacing. The admin
suite also simulates all 70 league matches and the four IPL playoffs, and checks
group persistence, stale previews, redraw protection and invalid group sizes.
The admin integration tests also create, preview and persist 8-, 10-, 12-, 14-,
16- and 20-team tournaments through the same preset.
