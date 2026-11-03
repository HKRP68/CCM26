# Tournament-only access

Bot admins can restrict individual players who are delaying their tournament
matches. A restricted player can play tournament fixtures, but cannot play other
game modes. Their squad, account and tournament information remain available.

```text
/tournamentonly block @player Complete your pending tournament fixtures
/tournamentonly warn @player Please finish this round
/tournamentonly status @player
/tournamentonly list
/tournamentonly unblock @player
```

`/touronly` is an alias. `/touronly @player [reason]` is a shortcut for blocking;
`/touronly off @player` removes the restriction. Replace `@player` with a Telegram
user ID, or reply to the player's message and omit the target:

```text
/touronly block Complete your pending tournament fixtures
/touronly status
/touronly unblock
```

Use a Telegram ID if a stored username is outdated or ambiguous. The player must
already have an account. Reasons are optional and limited to 500 characters.
`/tournamentonly list 2` shows the next page of restrictions.

`block` and the target-first shortcut **block other game modes immediately**.
They also send the player a DM showing the active restriction and remaining
fixtures. No grace option is needed for immediate enforcement.

To warn without blocking immediately, explicitly request a grace period:

```text
/touronly warn @player
/touronly warn @player --grace=12 Please finish the current round
```

`warn` defaults to 24 hours. `--grace=0` through `--grace=168` is also supported
on `block` for compatibility; a positive value explicitly allows other modes
until its deadline. The response clearly labels **ACTIVE** or **GRACE PERIOD**.
Existing blocks with future deadlines from the old implicit grace default take
effect immediately after this update. New explicit warnings retain their grace.

All restriction timestamps, including deadlines, list entries and status, use
**IST (UTC+05:30)**. Required fixtures include the tournament name, round,
match number and both teams. Players can use `/touronly status` (or `/touronly`)
to inspect their own progress; only bot admins can inspect other accounts,
change access, or list restrictions.

If Telegram cannot deliver the DM, the admin is told to relay the notice.
Repeating a warning cannot extend an existing grace period. Use `block` to
activate an existing warning immediately without resetting its required
fixtures; use `unblock` first to grant a new warning or exception.

The player must finish **all their own unfinished fixtures from the currently
released round/window**. Fixtures are captured when the warning is issued, so
next rounds never become extra requirements as the schedule advances. In
all-at-once schedules, only the earliest unfinished round is captured. Both
Challenge League owners/co-owners and Lets Play participants are supported.
Starting a match does not count: its official fixture must have a recorded
completed result. Finishing during grace avoids the block altogether.

Access automatically returns when those requirements finish, without waiting
for other teams. Recorded admin results, cancelled/deleted fixtures, and ended
or cancelled tournaments also clear the corresponding requirements. Result
recording clears the restriction in the same transaction; a minute-by-minute
reconciliation covers direct schedule edits and upgrades earlier manual locks.
Gameplay checks also recognize completed requirements immediately. This does not
automatically select inactive players: admins still choose whom to warn.
Only configured bot admins/owners can run the command. Manual unblock remains an
exception for approved leave, unavailable opponents, or technical problems.

Restart the bot/web workers after deploying so the normal database startup
migration adds the new access fields. Existing accounts keep normal access.
Each block/unblock is recorded in the admin audit log.

Official Challenge League and Lets Play tournament fixtures remain playable,
including their shared buttons and Mini App controls. Friendly tours and
standalone Super Overs count as other modes. The checks cover both participants,
old invitations, saved games and pending match launches; cancelling or leaving a
match remains available. Games already running autonomously are not rewound.

The IPL simulator also has a public, browser-only page. Its bot launch and server
sharing action are restricted, but offline/public-page simulation cannot be
controlled by an account restriction.
