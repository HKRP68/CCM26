# CMU News and Polls (Mini App)

The Mini App home screen has a community strip under the quest teaser. It has three parts:

1. **💡 Suggestion Box**: collapsed by default. Tap the header to open it.
2. **📰 Today's CMU News [N Unread]**: collapsible, and it remembers whether it was left open. It lists the latest 5 stories (pinned first). Tapping a headline opens the full article with its image, reactions (👍🔥😂😮) and a view count. **✍️ Submit your own news** opens the submission form.
3. **🗳 Poll**: shown only while a poll is running. Results appear once you vote, and a poll can pay coins for voting.

The badge counts published stories from the last 24 hours that the user has not opened yet.

## Where stories come from

| Source | How | Goes live |
| --- | --- | --- |
| Admin | Website → **📰 CMU News** → Publish (headline, image, article, pin, announce) | Immediately |
| Player | Mini App → Submit your own news | After a bot admin approves it |
| Auto | The game itself — see below | Immediately, or held for review (setting) |

### Player submissions

A submission is stored as `pending`, and every bot admin gets a DM with the image, headline, a preview and **✅ Approve / ❌ Reject** buttons. Reject offers preset reasons. The same queue is on the website (the **Pending review** tab, which has a nav badge). `/newsqueue` re-sends the cards for anything still waiting.

- The first decision wins. A second press, or a decision from the other surface, is refused because the story is no longer pending.
- On approval the author gets their coins, **once**. The default is 100, and it can be changed under Settings or per story on the website.
- The author is DMed either way.
- Limits: headline 8–160 characters, article 30–5000, image up to 5 MB (re-encoded to JPEG, max 1280px wide, EXIF stripped). Each player can submit 3 stories a day.

### Auto-generated stories

| Kind | Fires when | Dedupe key |
| --- | --- | --- |
| 🏆 Tournament champions | A final is recorded: bot match, manual result or imported scorecard | `tourney:<id>` |
| 💰 Record auction buys | A sale (or RTM match) beats every earlier price in that auction, after the first 3 sales | `auction_record:<season>:<lot>` |
| 🔨 Auction wrap-ups | The auction completes: top 3 buys and the biggest spender | `auction_done:<season>` |
| 📅 Season winners | The monthly season is finalised: the podium | `season:<key>` |
| 🥇 Ranked champion | The ranked ladder is finalised | `ranked:<key>` |
| 🌟 Hall of Fame | A match takes the #1 spot on a Hall of Fame board (only records set in the last 48h, so the back-fill scan stays quiet) | `hof:<entry>` |

Each auto story gets a generated 1200×630 banner (`services/news_banner.py`). `news_service.auto_story` runs in a savepoint and never raises, so a news failure can never cost a match its result or a season its payout. The dedupe key is unique, so a retried hook writes one story.

Under **Settings** on the News page you can:

- switch each kind on or off;
- choose whether auto stories publish immediately;
- choose whether they are also posted to the channel/group (off by default, to avoid spam).

## Polls

Website → **🗳 Polls**. A poll has a question, 2–6 options, start and end times (entered in IST), coins for voting, and an optional announce. The newest running poll is the one shown. Each user votes once, which a unique `(poll_id, user_id)` constraint enforces, and the vote reward is paid with the vote. **End now** closes a poll early.

## Announcing

Ticking **📣** posts the story or poll to the branding channel and group (Website → Branding). The post carries a button that deep-links into the Mini App:

- `startapp=news_<id>` opens that article;
- `startapp=poll_<id>` scrolls to the poll.

## Code map

- Models: `NewsArticle`, `NewsRead`, `NewsReaction`, `Poll`, `PollVote` (`models.py`), plus the GameConfig `news_*` columns.
- Services: `services/news_service.py`, `services/poll_service.py`, `services/news_announce.py` (Telegram), `services/news_banner.py`.
- Bot: `handlers/news.py` (`news:` callbacks, `/newsqueue`).
- Website: `/news`, `/polls` in `admin.py`; `templates/admin_news.html`, `templates/admin_polls.html`.
- Mini App API: `/api/webapp/news/{feed,article,react,submit}`, `/api/webapp/poll/{active,vote}`, `/api/news/<id>/image`. Init also returns `news_unread` and `poll`.
- Tests: `tests/test_news_and_polls.py`.
