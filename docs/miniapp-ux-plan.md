# CricMaster Mini App UX Plan

## Product direction

The Mini App should feel like a daily cricket companion, not a list of unrelated features. Every screen should answer one clear question: **what is the best thing for the player to do next?**

The visual language should stay premium and sport-focused: a deep stadium-night background, gold highlights for progress and value, green for completed actions, purple for special rewards, rounded cards, and short action-oriented copy. Use **Plus Jakarta Sans** for headings and **Outfit** for interface text so the hierarchy remains clear on small screens.

## Home-screen priority order

1. **Resume an active match** when one exists. This remains the highest-priority interruption because the player is already in a live flow.
2. **Show today's quests immediately** with progress and rewards. Players should not need to open a separate menu to understand their daily plan.
3. **Offer a Today / Monthly toggle** beside the quest heading. Monthly goals stay discoverable without competing with the daily loop.
4. **Pop the free-pack reward once per app session** when it is ready. Keep a pulsing `READY` badge on the Free Pack tile after dismissal so the reward remains visible without repeatedly interrupting the player.
5. **Keep feature tiles below the mission hub** as secondary navigation.

## Interaction rules

- Prefer one-tap navigation from a progress item to its detailed screen.
- Use haptic feedback only for meaningful moments: toggles, claims, reward reveals, and match actions.
- Avoid repeated popups. A reward popup may appear once per session; after dismissal, convert it into a visible tile state.
- Use plain labels such as **Today**, **Monthly**, **View all**, and **Open free pack** instead of unexplained abbreviations.
- Always show progress numerically as well as visually so the UI remains understandable without relying on color alone.

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
- Measure the funnel from home open → quest interaction → claim and from free-pack popup → pack open.
