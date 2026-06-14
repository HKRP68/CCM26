# Super Over Workflow for Telegram Cricket Bot

Create a complete Super Over workflow for my Telegram cricket bot.

The Super Over should work like a **1-over `/playmatch`**, but with special Super Over rules.

---

## 1. When Super Over Starts

If the main match ends in a tie, automatically start Super Over.

### Example

```text
Team 1: 170/6
Team 2: 170/8 after 20 overs

Result: Match tied
Next: Super Over
```

### Bot Message

```text
🏏 MATCH TIED!

{Team1}: {score1}/{wickets1}
{Team2}: {score2}/{wickets2}

🔥 SUPER OVER TIME

{SuperOverBattingTeam} will bat first because they batted second in the main match.
```

---

## 2. Super Over Basic Rules

Rules:

- Super Over is played like a 1-over `/playmatch`.
- Each team gets 1 over.
- Each Super Over innings has maximum 6 legal balls.
- Wides and no-balls do not count as legal balls.
- Each team can lose maximum 2 wickets.
- If 2 wickets fall, that Super Over innings ends immediately.
- Winner is decided by runs only.
- Wickets do not matter if runs are higher.
- If the chasing team reaches the target before 6 balls, the match ends immediately.
- If both teams score the same runs in Super Over, the Super Over is tied.
- If Super Over is tied, start another Super Over.
- Keep playing Super Overs until there is a winner.

---

## 3. Who Bats First in Super Over

### First Super Over

- The team that batted second in the main match will bat first in the first Super Over.
- The other team will bowl first.

### Example

```text
Main Match:
Team 1 batted first.
Team 2 batted second.

Super Over:
Team 2 bats first.
Team 1 bowls first.
```

### If Super Over Is Also Tied

- The team that batted second in the previous Super Over will bat first in the next Super Over.

### Example

```text
Super Over 1:
Team 2 bats first.
Team 1 bats second.

Super Over 2:
Team 1 bats first.
Team 2 bats second.
```

---

## 4. Player Selection Before Super Over 1st Innings

Before Super Over 1st innings starts:

### Batting Team Must Select

- 3 batters.
- 2 batters will be on crease.
- 1 batter will be backup if any batter gets out.
- All-rounders can be selected as batters.

### Bowling Team Must Select

- 1 bowler.
- All-rounders can be selected as bowlers.

Only players from the confirmed Playing XI can be selected.

Only the team owner can click buttons for their own team.

### Selection Buttons

For batting team:

```text
Button: {PlayerName}
Button: {PlayerName}
Button: {PlayerName}
Confirm Batters
```

For bowling team:

```text
Button: {PlayerName}
Button: {PlayerName}
Button: {PlayerName}
Confirm Bowler
```

### Rules

- Batting team owner can only select their 3 batters.
- Bowling team owner can only select their 1 bowler.
- Other users cannot click these buttons.
- If wrong user clicks, show error message:

```text
❌ Only {TeamOwner} can select players for this team.
```

When both teams confirm:

```text
Start Super Over 1st innings.
```

### Bot Message

```text
🔥 SUPER OVER PLAYER SELECTION

Batting Team: {TeamA}
Bowling Team: {TeamB}

{TeamA}, select 3 batters:
- 2 opening batters
- 1 backup batter

{TeamB}, select 1 bowler.

Only team owners can select and confirm.

Buttons:
{Player buttons}
Confirm Batters
Confirm Bowler
```

---

## 5. Start Super Over 1st Innings

Start Super Over Innings 1.

This innings should work like 1-over `/playmatch` with Super Over rules.

### Variables

```text
superOverRuns = 0
superOverWickets = 0
legalBalls = 0
```

### Innings Ends When

- `legalBalls == 6`
- OR `superOverWickets == 2`

### After Every Ball

- Add runs to `superOverRuns`.
- If the ball is legal, increase `legalBalls` by 1.
- If the ball is wide or no-ball, add runs but do not increase `legalBalls`.
- If wicket falls, increase `superOverWickets` by 1.
- If `superOverWickets == 2`, end innings immediately.
- If `legalBalls == 6`, end innings.

### Bot Live Message Format

```text
🔥 SUPER OVER - INNINGS 1

Batting: {TeamA}
Bowling: {TeamB}

Score: {runs}/{wickets} ({overs}/1)

On Strike: {strikerName} {strikerRuns}({strikerBalls})
Non-Striker: {nonStrikerName} {nonStrikerRuns}({nonStrikerBalls})

Bowler: {bowlerName} {bowlerWickets}-{bowlerRuns} ({bowlerOvers})

Recent Balls: {recentBalls}

Commentary:
{ballCommentary}
```

---

## 6. After Super Over 1st Innings

After Super Over 1st innings ends:

```text
Target = SuperOverFirstInningsRuns + 1
```

### Bot Message

```text
🔥 SUPER OVER - INNINGS 1 COMPLETE

{TeamA}: {runs}/{wickets} in {overs}

🎯 Target for {TeamB}: {target} runs

{TeamB} need {target} runs to win the match.
```

---

## 7. Player Selection Before Super Over 2nd Innings

After Super Over 1st innings finishes, swap roles.

Now:

- Previous bowling team becomes batting team.
- Previous batting team becomes bowling team.

Before Super Over 2nd innings starts:

### New Batting Team Must Select

- 3 batters.
- 2 batters will be on crease.
- 1 batter will be backup if any batter gets out.
- All-rounders can be selected as batters.

### New Bowling Team Must Select

- 1 bowler.
- All-rounders can be selected as bowlers.

Only players from the confirmed Playing XI can be selected.

Only the team owner can click buttons for their own team.

When both teams confirm:

```text
Start Super Over 2nd innings.
```

### Bot Message

```text
🔁 SUPER OVER ROLE SWAP

Now {TeamB} will bat.
{TeamA} will bowl.

🎯 Target: {target}

{TeamB}, select 3 batters:
- 2 opening batters
- 1 backup batter

{TeamA}, select 1 bowler.

Only team owners can select and confirm.
```

---

## 8. Start Super Over 2nd Innings

Start Super Over Innings 2.

### Variables

```text
chaseRuns = 0
chaseWickets = 0
legalBalls = 0
target = FirstInningsRuns + 1
```

### After Every Ball, Check Result Immediately

```text
If chaseRuns >= target:
    Chasing team wins the match immediately.

Else if chaseWickets == 2:
    First Super Over batting team wins.

Else if legalBalls == 6:
    If chaseRuns < FirstInningsRuns:
        First Super Over batting team wins.
    Else if chaseRuns == FirstInningsRuns:
        Super Over tied → Start next Super Over.
```

### Important

- If chasing team reaches target in 0.1, 0.2, 0.3, 0.4, or 0.5 overs, end the match immediately.
- Do not play remaining balls after target is reached.
- If chasing team loses 2 wickets before reaching target, they lose immediately.
- If both teams score equal runs after 6 legal balls, Super Over is tied.

### Bot Live Message Format

```text
🔥 SUPER OVER - INNINGS 2

Batting: {TeamB}
Bowling: {TeamA}

Target: {target}

Score: {runs}/{wickets} ({overs}/1)

Need: {runsNeeded} runs from {ballsLeft} balls

On Strike: {strikerName} {strikerRuns}({strikerBalls})
Non-Striker: {nonStrikerName} {nonStrikerRuns}({nonStrikerBalls})

Bowler: {bowlerName} {bowlerWickets}-{bowlerRuns} ({bowlerOvers})

Recent Balls: {recentBalls}

Commentary:
{ballCommentary}
```

---

## 9. Super Over Winner Message

If any team wins the Super Over:

```text
🏆 SUPER OVER RESULT

{TeamA}: {superOverScoreA}/{superOverWicketsA}
{TeamB}: {superOverScoreB}/{superOverWicketsB}

🎉 {WinningTeam} win the Super Over!

🏆 Match Winner: {WinningTeam}

Result:
{WinningTeam} won by Super Over.
```

---

## 10. If Super Over Is Tied

If Super Over is tied:

```text
🔥 SUPER OVER TIED!

{TeamA}: {superOverScoreA}/{superOverWicketsA}
{TeamB}: {superOverScoreB}/{superOverWicketsB}

No winner yet.

Starting Super Over {superOverNumber}...

{NextBattingTeam} will bat first because they batted second in the previous Super Over.
```

Then repeat the same workflow:

1. Ask batting team to select 3 batters.
2. Ask bowling team to select 1 bowler.
3. Only team owners can click their team buttons.
4. Both teams must confirm.
5. Start next Super Over innings 1.
6. Swap roles.
7. Ask new batting team to select 3 batters.
8. Ask new bowling team to select 1 bowler.
9. Both teams must confirm.
10. Start next Super Over innings 2.
11. Compare scores again.

---

## 11. Repeated Super Over Player Restrictions

If Super Over is tied and another Super Over starts:

### Batting Restriction

- Any batter who got out in any previous Super Over cannot bat again.
- Batters who remained not out can be selected again.

### Bowling Restriction

- A bowler who bowled the previous Super Over cannot bowl the next Super Over.
- That bowler can bowl again only after sitting out one Super Over.

### Example

```text
Super Over 1:
Team A bowler: Bumrah

Super Over 2:
Team A cannot use Bumrah as bowler.

Super Over 3:
Team A can use Bumrah again if available.
```

---

## 12. Final Function Structure

### Function: `checkMatchResult()`

```text
If Team2 score > Team1 score:
    Declare Team2 winner.

Else if Team2 score < Team1 score:
    Declare Team1 winner.

Else:
    Match tied.
    Start Super Over.
```

---

### Function: `startSuperOver()`

```text
If superOverNumber == 1:
    battingFirst = teamThatBattedSecondInMainMatch
Else:
    battingFirst = teamThatBattedSecondInPreviousSuperOver

bowlingFirst = oppositeTeam

Ask battingFirst team owner to select 3 batters.
Ask bowlingFirst team owner to select 1 bowler.

Wait until both teams confirm.

Start Super Over Innings 1.
```

---

### Function: `selectSuperOverPlayers(team, role)`

```text
If role == batting:
    Team owner selects 3 batters.
    2 batters start on crease.
    1 batter is backup.

If role == bowling:
    Team owner selects 1 bowler.

Validation:
    Only team owner can click.
    Only Playing XI players can be selected.
    All-rounders are allowed for both batting and bowling.
    Confirm button is required.
```

---

### Function: `playSuperOverInnings()`

```text
runs = 0
wickets = 0
legalBalls = 0

While legalBalls < 6 and wickets < 2:
    Simulate ball like /playmatch.

    If wide or no-ball:
        Add extra runs.
        Do not increase legalBalls.

    Else:
        Increase legalBalls by 1.

    If wicket:
        Increase wickets by 1.

    If wickets == 2:
        End innings.

    If legalBalls == 6:
        End innings.

Return runs, wickets, legalBalls.
```

---

### Function: `playSuperOverChase()`

```text
chaseRuns = 0
chaseWickets = 0
legalBalls = 0
target = firstInningsRuns + 1

While legalBalls < 6 and chaseWickets < 2:
    Simulate ball like /playmatch.

    If wide or no-ball:
        Add extra runs.
        Do not increase legalBalls.

    Else:
        Increase legalBalls by 1.

    If wicket:
        Increase chaseWickets by 1.

    If chaseRuns >= target:
        Chasing team wins immediately.
        End match.

    If chaseWickets == 2:
        First innings team wins.
        End match.

    If legalBalls == 6:
        If chaseRuns < firstInningsRuns:
            First innings team wins.
        Else if chaseRuns == firstInningsRuns:
            Super Over tied.
            Start next Super Over.
```

---

### Function: `compareSuperOverScores()`

```text
If secondBattingTeamRuns > firstBattingTeamRuns:
    secondBattingTeam wins.

Else if secondBattingTeamRuns < firstBattingTeamRuns:
    firstBattingTeam wins.

Else:
    Super Over tied.
    superOverNumber += 1
    Apply repeated Super Over restrictions.
    Start next Super Over.
```

---

## 13. Edge Cases

- Store Super Over score separately from main match score.
- Main match score should not change during Super Over.
- Super Over should use same match engine as 1-over `/playmatch`.
- Super Over must use special rules: 2 wickets max, 6 legal balls max.
- Wides and no-balls add runs but do not count as legal balls.
- No-ball wicket should only count if dismissal type is valid on no-ball, such as run out.
- Chasing team wins immediately after reaching target.
- Do not decide winner by wickets.
- Do not use boundary countback.
- Keep playing Super Overs until a winner is found.
- Only team owners can select players.
- Confirm button is required for both teams before innings starts.
- If a player is not in Playing XI, block selection.
- If a batter was dismissed in previous Super Over, block them from batting again.
- If a bowler bowled the previous Super Over, block them from bowling next Super Over.
- Final scorecard must show main match score and Super Over score.

---

## 14. Final Scorecard Format

```text
🏏 MATCH RESULT

{Team1}: {mainScore1}/{mainWickets1} ({mainOvers1})
{Team2}: {mainScore2}/{mainWickets2} ({mainOvers2})

Match tied.

🔥 SUPER OVER

Super Over 1:
{TeamA}: {soScoreA}/{soWicketsA}
{TeamB}: {soScoreB}/{soWicketsB}

If multiple Super Overs:

Super Over 2:
{TeamA}: {so2ScoreA}/{so2WicketsA}
{TeamB}: {so2ScoreB}/{so2WicketsB}

🏆 Winner: {WinningTeam}

Result:
{WinningTeam} won by Super Over.
```
