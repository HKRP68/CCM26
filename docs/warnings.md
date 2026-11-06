# Account warnings

Bot admins and owners can moderate registered players from a group or the bot's
DM. Group administrator status, logo/news reviewer status, and maintenance bypass
access alone do not grant these commands.

| Command | Action |
| --- | --- |
| `/cmuwarn @player Repeated abuse` | Add one active warning |
| `/cmuunwarn @player Appeal accepted` | Remove the newest active warning |
| `/cmuwarns @player` | View the active count, ban status and warning history |
| `/cmuwarns @player 2` | View the next history page |

Use a positive Telegram user ID in place of `@player`, or reply to the player's
message and omit the target: `/cmuwarn Repeated abuse`. An explicit target takes
precedence over the reply. Usernames match exactly, ignoring case; ambiguous
stored usernames require a Telegram ID. Players must have run `/debut` first.
Send either mutation command without a target or reply to see help.

Reasons are optional and limited to 500 characters. Each warning records its
reason, issuing admin and UTC timestamp. Removed warnings retain the original
record as well as the removal reason, admin and timestamp. The existing web
admin audit log also records warnings, removals and automatic bans/unbans.
History is admin-only and shows three records per page, with shortened reasons
to fit Telegram's message limit. The original reasons remain in the database.

Five active warnings automatically set the existing account ban. The fourth
warning explicitly tells the admin that the next warning will trigger a ban.
The bot attempts to DM the player when a warning is issued or removed; an
undeliverable DM does not undo moderation, and the admin sees a delivery notice.
These are bans from using CricMaster Ultra. Telegram group membership is
unaffected. Warning counts are account-wide and survive restarts and account
progress resets; they do not expire automatically.

Removing a warning drops the count by one. If this brings the count below five,
the bot lifts a ban created by the warning system. A manual ban set in the web
admin panel remains in place. Web admins can also manually unban a player;
existing warnings remain in their history. At five active warnings, remove a
warning before issuing another. Bot admins and owners cannot receive warnings.
Already-banned players cannot receive additional warnings.

Warning changes and their audit entries commit together. Changes serialize on
the account row, and successful commands are deduplicated by Telegram chat and
message IDs, so retrying the same command cannot add or remove a second warning.
After a command commits, the running bot's ban cache is invalidated for that
player so their next command or button sees the updated status. Other processes
and web-panel changes follow the existing ban cache TTL (60 seconds by default).

Startup creates the `user_warnings` table and migrates the `users.warning_ban`
column automatically. Existing bans are treated as manual bans.
