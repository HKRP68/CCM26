# Challenge League squad commands

`/clsquad` (aliases `/csquad` and `/ctsquad`) displays a full Challenge League
roster with **Player · Category · Overall · Bat · Bowl** and its squad rating.

- `/clsquad` shows your owned or co-owned tournament team. If you run several
  current teams, choose one from the buttons. When you have no current team,
  teams from your other non-cancelled tournaments are available. Other users
  receive the league browser.
- `/clsquad IPL` opens that league's teams. Use its name, short code or command.
- `/clsquad Mumbai` searches teams across leagues. Ambiguous names show a picker.
- `/clsquad IPL | Mumbai` limits the team search to that league.
- `/clsquad #12 | Mumbai` selects an exact league ID when league names or codes
  are duplicated.

Squads are public and read-only. League/team lists paginate; selecting a team
sends the whole roster, split into messages when needed. Tournament team links
take precedence over names, so renamed franchises still open the correct squad.
An invalid link reports the missing assignment rather than choosing another
league's similarly named team. Teams without a roster show an empty-squad message.
These commands are also listed in `/tourhelp`.

The displayed numbers are printed squad ratings, never match-balancing values.
Stored league details override the master card; missing fields can fall back to
the linked master card. Unrecorded or invalid ratings show `—`. Squad rating is
the mean overall across the full squad's recorded values; Bat and Bowl are the
corresponding means. Unknown values are excluded from that field's average;
recorded zeroes count. This is not a best-XI or trait-adjusted rating.

`/asquad`, `/asquad all` and `/ateamsummary` use the same average-rating rule.
Each auction player row explicitly shows category and all three ratings,
alongside the existing edition, price, purse and acquisition details. Only signed
players count toward auction squad ratings; unretained rights remain separate.

No database migration or new configuration is required. Restart the bot after
deployment to register the command and its callback handler.
