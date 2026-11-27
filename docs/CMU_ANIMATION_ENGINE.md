# CMU Animation Engine

A modular, presentation-only motion layer for the CricMaster Ultra Telegram
Mini App. It replaces ad-hoc cosmetic code scattered through `webapp.html` and
`static/cricket/app.js` with a shared engine, while preserving every existing
API the app already calls.

---

## The rule the whole design serves

**An animation can fail. A reward cannot.**

Everything below follows from that:

| Guarantee | How it is enforced |
| --- | --- |
| The engine cannot grant, request or re-request a reward | No engine module contains `fetch`, `XMLHttpRequest`, `sendBeacon`, `WebSocket`, `EventSource`, dynamic `import()` or a call to the app's `api()` helper. Tested. |
| A reveal starts only after server confirmation | `openPack` reveals after `confirmEconomy`; `openFreePack` after `submitReward`. Unchanged, and tested. |
| A confirmed reward is never behind an animation | Done is live as soon as the cards exist; a face-down deck self-reveals after 12s; the reward chest puts its contents in the DOM before the lid can lift and opens itself after 7s; both promise settlement paths render. |
| Interrupting never loses a value | Counters write the confirmed value to `data-cmu-value` *before* animating; the walkout writes the confirmed rating on close however it closes. |
| Nothing blocks gameplay | The match banner layer is `pointer-events: none` throughout, takes no focus, auto-dismisses, and runs through a bounded queue that drops a backlog. |
| Missing module = previous behaviour | Each feature module publishes an `available: false` no-op surface when the core is absent, and every call site checks. |

The engine also computes nothing about the game. Rarity, tiers and milestone
announcements are **readings** of values the server already published — a
fifty is announced because the scorecard says 50, not because the engine
decided so.

---

## Files

### Core

| File | Purpose |
| --- | --- |
| `static/js/cmu_animation_engine.js` | Settings store and motion level, timing, the bounded queue, the overlay factory, counters, meters, particles, confetti, light bursts, chests, artwork helpers, rarity, teardown. Exposed as `window.CMUAnim`. |
| `static/css/cmu_animation_engine.css` | Overlay shell, shared motion primitives, artwork sizing, motion gates, settings form styling. |

### Phase modules

| File | Phase | Exposes |
| --- | --- | --- |
| `static/js/cmu_pack_theatre.js` | 1 | `window.CMUPackTheatre` |
| `static/js/cmu_kitbag.js` | 2 | `window.CMUKitBag` |
| `static/js/cmu_walkout.js` | 3 | `window.CMUWalkout` |
| `static/js/cmu_rewards_fx.js` | 4 | `window.CMURewardsFX` |
| `static/js/cmu_match_fx.js` | 5 | `window.CMUMatchFX` |
| `static/js/cmu_settings.js` | 6 | `window.CmuMotionSettings` |
| `static/css/cmu_reveal_theatre.css` | 1–3 | pack, kit bag, walkout |
| `static/css/cmu_rewards_fx.css` | 4 | counters, chest, rank, level-up |
| `static/css/cmu_match_fx.css` | 5 | banners and match-end cards |

### Load order

The core loads **before** `miniapp_theme.js`, which mirrors the engine's
haptics preference at parse time so the two controls for that preference
cannot disagree. This applies in both `templates/webapp.html` and
`static/cricket/index.html`.

---

## Phase 1 — Premium pack opening

**Files changed:** `templates/webapp.html`, `static/js/cmu_pack_theatre.js`,
`static/css/cmu_reveal_theatre.css`, `static/css/cmu_animation_engine.css`,
`static/js/cmu_animation_engine.js`.

**Artwork.** Original CMU foil drawn in the module: a pouch with a crimped
tear strip in its own SVG group, a cricket-ball roundel carrying the CMU
monogram, a diagonal brand sash and tier pips. Six tiers keyed off the pack
catalogue in `services/pack_service.py` — Free, Bronze, Silver, Star, Legend,
Ultimate Legend — with a rating-band fallback for an admin-renamed pack.
Brand lines use `textLength`/`lengthAdjust`, so a device that resolves a
different font cannot push text off the pouch.

**Sequence.** Float and metallic sheen while idle → tap *or* swipe up → shake
→ the crimp strip tears off the shell while the shell brightens and swells →
controlled lighting burst, rotating rays and a particle scatter → staggered
card deal → individual 3D flips.

**Integration points.**

| Function | Change |
| --- | --- |
| `openPack()` | Unchanged flow; still reveals only after `confirmEconomy`. |
| `openFreePack()` | Unchanged flow; routes to the kit bag (Phase 2). |
| `startPackOpenAnimation()` | Resolves the pack tier, mounts the artwork, releases any previous mount. |
| `revealPackCards(skip)` | Runs the theatre, then renders the deck. Keeps the `packRevealing` guard ahead of `classList.add('bursting')`. |
| `playerRevealCard(player, index)` | Same signature and markup; adds a rarity class and the engine's 3:2-safe artwork. |
| `closePackOpen()` | Also clears the deck controller, the auto-reveal timer and the mount. |
| `revealAllPackCards()` | New. Backs the Reveal all button. |
| `packEmblem(name, maxRating)` | New. Shop and inventory rows show the pack's own foil, with the emoji as fallback. |

**Controls.** *Reveal all* (staggered) and *Skip animation* (instant,
feedback-free). Main and bonus labels sit on each card's ribbon; the Pack
History reference and the existing squad-overflow Replace/Release workflow are
rendered exactly as before.

**Artwork handling.** Player cards are 3:2 landscape and stay that way:
`aspect-ratio: 3 / 2` plus `object-fit: contain`. Never cropped, never
stretched. A failed image collapses to a labelled placeholder.

---

## Phase 2 — Free Pack kit bag

**Files changed:** `static/js/cmu_kitbag.js`, `static/css/cmu_reveal_theatre.css`,
`templates/webapp.html`.

An original CMU cricket kit bag with four separately animated SVG groups:

- `.cmu-kb-body` — shell, gusset, panels, pocket, badge
- `.cmu-kb-handles` — straps and bound grip, swinging a beat behind the body
- `.cmu-kb-zip` — track, teeth and a slider that runs the full length
- `.cmu-kb-flap` — the lid, hinged on its own seam via `transform-box: fill-box`

**Sequence.** The bag is set down and settles → shakes → the slider runs the
zip while the teeth part behind it → the flap hinges back → the confirmed card
rises out → rarity effects → the full card, Pack History receipt and squad
actions appear below.

**Theming.** The SVG carries structure and class names only. Every colour is a
CSS custom property, including the gradient stops, so dark and light both work
with no JavaScript branch.

**Why it is drawn, not fetched.** No asset request means nothing to time out,
404 or be blocked by a regional CDN filter mid-reveal.

---

## Phase 3 — Legendary player walkout

**Files changed:** `static/js/cmu_walkout.js`, `static/css/cmu_reveal_theatre.css`,
`templates/webapp.html`.

**Sequence.** Night stadium → floodlights come up bank by bank (each pylon
staggered by its own `--wo-bank`) → the tunnel opens and its perspective ribs
light → nationality → player category → the rating counts up → the player's
own card is presented.

**No player figures.** The scene is tiers, steel and light. A drawn silhouette
walking out of the tunnel would be a model of a real cricketer CMU has no
likeness for, so what emerges is light and the player's own artwork.

**Thresholds.** `CMUAnim.rarity`:

```js
{ thresholds: { mythic: 96, legendary: 91, epic: 85, rare: 80 },
  walkoutFrom: 91 }
```

Retune with `CMUAnim.configureRarity({ walkoutFrom: 93 })`. These are cosmetic:
they decide how loudly a pull is announced, never what it was worth. The
walkout never plays at the Minimal or Off motion levels.

**Trigger points.** The pack deck as a qualifying card is turned; the kit bag
once the card has risen; `showReward` for a Lucky Card Pick. In every case the
reward is already rendered behind the overlay. No caller awaits it.

---

## Phase 4 — Rewards and progression

**Files changed:** `static/js/cmu_rewards_fx.js`, `static/css/cmu_rewards_fx.css`,
`templates/webapp.html`, `templates/webapp_community.html`,
`static/js/miniapp_clubhouse.js`, `static/js/miniapp_player_hub.js`.

**Components.** `setCount`, `wallet`, `floatDelta`, `meter`, `meters`,
`chest`, `rankChange`, `levelUp`, `celebrate`, `stagger`, `pop`.

**Wired into:**

| Surface | What was added |
| --- | --- |
| Wallet (Home, balance pill) | `setWalletCounters()` counts from the old balance with a floating delta, through the app's own compact `formatNum`. |
| Daily rewards | A chest over the receipt, tiered by what was won. |
| Quests | Staggered rows, meters filling from empty, a celebration on a confirmed claim (per-quest and Claim all). |
| Login streak | Staggered ladder, celebration on a confirmed claim. |
| Gifts | A chest showing the confirmed prize, after the server answered. |
| Achievements | Staggered rows, an animated unlocked counter, a celebration when the server reports more unlocked than last seen. |
| Wallet ledger | Balances count to the confirmed figures. |
| Community XP | Lifetime/weekly counters, level meter, rank movement, level-up badge. |
| Lucky Card Pick | Flip timings routed through the engine; particles on the winning card. |

**One deliberate exception.** The daily chest carries the **totals only**. The
receipt's Replace and Release controls carry element ids (`ov-<claim_id>`) and
roster ids; a second live copy in the overlay would leave
`markOverflowResolved()` updating whichever came first in the document.

---

## Phase 5 — Match broadcast overlays

**Files changed:** `static/js/cmu_match_fx.js`, `static/css/cmu_match_fx.css`,
`static/cricket/app.js`, `static/cricket/index.html`.

**Events.** Toss · FOUR · SIX · Wicket · Dropped catch · Fifty · Century ·
Three-for · Five-for · Hat-trick · Team milestones · Super Over · Record broken ·
Player of the Match · Match victory · Tournament championship.

**Reading, not deciding.** `fromBall(comm, state)` is pure: it reads the
published commentary entry and scorecard and returns descriptors. It never
writes to `matchState`.

- A **hat-trick** is announced only where the published entry marks one. It is
  never inferred from three wickets in a bowling card, which can span overs.
- A **dropped catch** needs an explicit flag or the server's own wording.
- **Milestones** are crossings of confirmed figures, de-duplicated per match so
  a poll that replays an over cannot replay a century.

**Non-blocking by construction.** The banner layer is `pointer-events: none`
throughout, uses `aria-live` rather than `role="dialog"` so it never steals
focus mid-tap, auto-dismisses, and is fed by a queue capped at three. Autoplay
switches the layer into fast play, collapsing holds and dropping the backlog.

Only the match-end cards take focus — and only because nothing is waiting on
the player's next tap by then. Each is dismissible, backed by a result screen
already rendered underneath, and lets itself out if nobody taps.

**Integration.** `triggerMatchEvent()` — the arena's single per-ball entry
point — calls `announceMatchMoment()`. `applyMatchState()` calls
`announceMatchState()` after every guard has passed, for the toss, Super Over,
result, Player of the Match and championship.

---

## Phase 6 — Animation settings

**Files changed:** `static/js/cmu_settings.js`, `templates/webapp.html`,
`static/js/miniapp_theme.js`, `static/css/cmu_animation_engine.css`.

On **Explore → Appearance**, below the theme controls:

- **Animation level** — Full cinematic / Standard / Minimal / Animations off
- **Sound effects**, **Haptic feedback**, **Particle effects**
- **Fast play in matches**
- **Preview** — plays a sample at the current settings
- A note when the device asks apps to reduce motion

**Behaviour.**

- Settings **apply on change**, not on submit: a motion level that only took
  effect after Save could not be previewed, and closing the screen would lose a
  choice the player had just watched work. Save confirms what is stored.
- A system reduced-motion preference is a **floor**, never an override. The
  chosen level stays selected and returns when the device setting changes.
- Blocked storage is reported honestly ("applied for this session") rather than
  claimed as saved.
- Haptics has two controls (here and in Appearance). Both write through the
  engine, so they cannot disagree.

**Level semantics.**

| Level | Duration scale | Particles | Walkout | Idle motion |
| --- | --- | --- | --- | --- |
| Cinematic | 1.0 | full | yes | yes |
| Standard | 0.68 | 60% | yes | yes |
| Minimal | 0.26 | none | no | none |
| Off | 0 (end state rendered immediately) | none | no | none |

`data-cmu-motion` and `data-cmu-particles` on `<html>` let CSS branch without
per-element JavaScript, and act as a safety net: a sequence that forgets to
check the setting still cannot animate at Off.

---

## Testing performed

### Automated

`tests/test_cmu_animation_engine.py` — 48 source-level contract tests:
no network primitives in any engine module; preserved API signatures and inline
handlers; reveal-after-confirmation ordering; Done not gated on the animation;
chest contents in the DOM; counters storing before animating; no-op surfaces;
hat-trick not inferred; banner layer non-interactive; queue bounded; all four
levels; reduced motion as a floor only; persistence; 3:2 artwork; focus trap;
safe areas; teardown; and the queue-drain regression.

Full existing suite re-run — see **Known limitations** for the one pre-existing
failure unrelated to this work.

### Browser (Chromium via Playwright, 390×844)

Driven against the real app served from a throwaway SQLite database.

| Area | Checked |
| --- | --- |
| Pack opening | Cinematic / Standard / Minimal / Off / system reduced-motion; tap, Reveal all and Skip; receipt; 3:2 ratios held; screen-reader text appears only once a card is face up. |
| Pack edge cases | Triple tap → one deck; close mid-reveal → clean state; re-open → no stacked artwork or listeners; single-card Free Pack; squad overflow preserved; Escape before and after reveal. |
| Kit bag | Dark and light; the four groups present; play / skip / Off / Minimal; re-open resets the bag and empties the card slot. |
| Walkout | Below-threshold pull plays nothing; floodlights; counter lands on the confirmed rating; skip mid-count still leaves it; two pulls do not stack; teardown on hide. |
| Rewards | Counter interrupted by a re-render lands on the confirmed value; chest contents present before opening; quest meters fill from empty; rank movement and level-up; no duplicate element ids between the daily receipt and its chest. |
| Match overlays | Classification pure and de-duplicated; layer `pointer-events: none` and a tap reaching the game underneath; focus unmoved; queue capped at 4; fast play drains; result card renders and cleans up. |
| Arena | A ball through the real `triggerMatchEvent` raises a banner. With both engine scripts blocked the arena still boots, still handles the ball and logs no errors. |
| Settings | Every control applies and persists; survives reload; particles disabled and budget 0 at Minimal; `ms(1000) === 0` at Off; both haptics controls stay in step; reduced motion floors the level, shows the note and suppresses the walkout. |

**Full degradation path.** With every engine script blocked, the Mini App
still opens a pack through the pre-engine flat reveal (CSS foil intact, cards,
receipt, overflow note), claims a daily reward, animates nothing, shows the
wallet, fires the original confetti, and leaves the theme form usable. With
only `cmu_settings.js` blocked, the Animations form stays hidden behind an
honest note rather than rendering controls that do nothing.

### Three real bugs found and fixed during testing

1. **Cards rendered already-flipped** (Skip / Animations off / reduced motion)
   left their back `<button>` enabled and in the tab order behind the card, so
   a keyboard user could land on a control that revealed nothing.
2. **`drain()` while the queue was idle** set a flag only the run loop cleared,
   so it stayed set and every task pushed afterwards was silently skipped. The
   generation counter alone now does the job.
3. **The Animations form rendered live but dead** when `cmu_settings.js` alone
   failed to download: the markup was visible and nothing was left to hide it.
   The form now starts hidden behind a note that `open()` swaps out.

A fourth, smaller one: the pack theatre's unmount left `.cmu-pack-mounted` on
the shared opening button, so a Free Pack opened straight after a foil pack
inherited the foil's rules — including the torn state's `visibility: hidden`.

---

## Known limitations

- **Sound effects are a registry, not a bundle.** `CMUAnim.sound(key)` gates
  cues by the setting and forwards them to a sink. The arena already owns its
  audio files and its WebView unlock dance, so the engine deliberately ships no
  audio of its own; nothing calls `registerSoundSink()` yet, so the Sound
  effects toggle currently governs cues that the arena plays through its own
  path. Wiring the sink to `fireEventSound` is a small follow-up.
- **Championship announcements need a server field.** `announceMatchState()`
  reads `result.championship` / `result.tournamentWin`. Until the match result
  payload carries one, the championship card will not fire — by design, rather
  than inferring a title from a win.
- **Dropped catches depend on the payload.** Announced from an explicit flag or
  the server's own wording. A build whose commentary phrases it differently
  will not raise the banner; add the phrase or the flag rather than guessing.
- **Record broken is an API, not yet a caller.** `CMUMatchFX.record()` exists
  and is tested; nothing in the arena calls it, because no confirmed "record
  broken" event is published in the match payload today.
- **Live match overlays only reach the arena.** `templates/webapp.html` still
  contains a legacy inline live-match renderer, but `openLiveMatch()` routes to
  `/cricket`, so only the arena is wired.
- **Browser coverage.** Verified in Chromium at 390×844 and in a reduced-motion
  context. Not verified on a physical low-end Android device or in the iOS
  Telegram WebView; the 60 FPS target is a design target (transform/opacity
  only, one element per particle, `backdrop-filter` dropped below Standard)
  rather than a measured result.
- **Pack history receipt** shows the reference the server returned. It is not a
  new surface; the existing History tab is unchanged.
