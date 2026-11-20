# CricMaster Mini App UX Plan

## Product direction

The Mini App is a daily cricket companion. Players can go from opening the app
to resuming a match, claiming a reward, or managing their XI in a few taps.

The interface uses charcoal surfaces, burgundy for live and ready states, and
willow tones for primary actions. Outfit is self-hosted for headings, interface
text and score displays. The Quick Match artwork shows a cricket ball and bat;
the WebP is served with the rest of the app's static assets.

## Home and navigation

1. Resume a live match or an active Quick Match from the main match action.
2. Show a ready Free Pack inline. The shortcut opens the pack screen.
3. Show quest progress and rewards for Today and Monthly. Players can claim
   completed quests as a group from Home or open Quests to claim individually.
4. Keep Home, Matches, Squad, Market and Rewards available in the main
   navigation. Leaderboard and Tournament centre are under Matches and Home.

## Match flow and motion

- Keep the Quick Match step and score when a player moves between Mini App
  screens in the same session. Ending a match takes confirmation and uses the
  existing abandon action.
- Show the toss outcome and phase scores where the player picks their next
  tactic. The score update uses a short reveal and leaves the result available.
- Use numeric progress alongside quest bars. Reduced-motion settings retain
  selected states and score feedback with static visuals.

## Next iterations

### Phase 2 — Guided daily loop

- Add a single **Recommended next action** button based on the nearest incomplete daily quest.
- Add a compact reset timer below Today's Quests.
- Add completion celebrations when all daily quests are finished.
- Move lower-frequency features such as Clubs and Achievements into an **Explore** section to reduce home-screen density.

### Phase 3 — Personalization

- Personalize the home greeting with the player's team name and current season rank.
- Reorder secondary tiles using recent activity while keeping the mission hub fixed.
- Add a first-week checklist for new players: debut, claim a free pack, set the playing XI, and play the first quick match.

### Phase 4 — Retention and accessibility

- Add notification preferences for free-pack availability, daily reset, and match reminders.
- Review color contrast, reduced-motion behavior, keyboard focus, and screen-reader labels across all interactive components.
- Measure the funnel from home open → quest interaction → claim and from Free Pack shortcut → pack open.
