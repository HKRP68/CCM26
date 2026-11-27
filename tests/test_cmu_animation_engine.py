"""CMU Animation Engine — contract tests.

The engine is a presentation layer bolted onto surfaces that hand out real
money, real players and real match results, so these tests are mostly about
what it must NOT do:

  * it must not be able to grant, request or re-request a reward — enforced by
    checking that no engine module can reach the network at all,
  * it must not gate a confirmed reward behind an animation that can fail,
  * it must not change the existing API contracts the Mini App already relies
    on (openPack, revealPackCards, playerRevealCard, …),
  * every sequence must degrade to the pre-engine behaviour when its module is
    missing.

Source-level, in the same style as test_miniapp_v3 and test_pack_purchase_lock:
these files ship to the client as text, so reading them is the check.
"""

import os
import re
import unittest


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ENGINE_MODULES = [
    "static/js/cmu_animation_engine.js",
    "static/js/cmu_pack_theatre.js",
    "static/js/cmu_kitbag.js",
    "static/js/cmu_walkout.js",
    "static/js/cmu_rewards_fx.js",
    "static/js/cmu_match_fx.js",
    "static/js/cmu_settings.js",
]

ENGINE_STYLES = [
    "static/css/cmu_animation_engine.css",
    "static/css/cmu_reveal_theatre.css",
    "static/css/cmu_rewards_fx.css",
    "static/css/cmu_match_fx.css",
]


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _function(src, signature):
    """Slice out one function body by brace matching from its signature.

    The parameter list is skipped first: these modules use destructured
    options objects with defaults (``function overlay({ className = '' })``),
    so the first ``{`` after the signature is a parameter, not the body.
    """
    start = src.index(signature)
    # Walk the parameter list to its matching ")", then take the next "{".
    depth = 0
    cursor = src.index("(", start)
    for index in range(cursor, len(src)):
        if src[index] == "(":
            depth += 1
        elif src[index] == ")":
            depth -= 1
            if depth == 0:
                cursor = index
                break
    else:
        raise AssertionError(f"unterminated parameter list: {signature}")

    depth = 0
    opening = src.index("{", cursor)
    for index in range(opening, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start:index + 1]
    raise AssertionError(f"unterminated function: {signature}")


# ════════════════════════════════════════════════════════════════════
# 1. The engine cannot grant anything
# ════════════════════════════════════════════════════════════════════

class EngineCannotReachTheServerTests(unittest.TestCase):
    """The strongest guarantee available: no network primitive, anywhere.

    An animation callback that could call the server is one refactor away from
    granting a reward twice. None of these modules may contain the means.
    """

    FORBIDDEN = (
        "fetch(",
        "XMLHttpRequest",
        "navigator.sendBeacon",
        "EventSource",
        "WebSocket",
        "import(",
    )

    def test_no_engine_module_can_make_a_request(self):
        for rel in ENGINE_MODULES:
            src = _read(rel)
            for token in self.FORBIDDEN:
                self.assertNotIn(
                    token, src,
                    f"{rel} contains {token!r}: an animation module must not be "
                    "able to reach the server")

    def test_no_engine_module_calls_the_app_api_helper(self):
        """`api(...)` is the Mini App's authenticated request helper."""
        for rel in ENGINE_MODULES:
            src = _read(rel)
            self.assertIsNone(
                re.search(r"(?<![\w.])api\s*\(", src),
                f"{rel} calls the app's api() helper")

    def test_no_engine_module_writes_a_balance_or_roster(self):
        for rel in ENGINE_MODULES:
            src = _read(rel)
            for token in ("userData.coins", "userData.gems", "userData.quest_points",
                          "pendingRevealPlayers ="):
                self.assertNotIn(token, src, f"{rel} assigns game state ({token})")


# ════════════════════════════════════════════════════════════════════
# 2. Existing API contracts are preserved
# ════════════════════════════════════════════════════════════════════

class PreservedMiniAppApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read("templates/webapp.html")

    def test_every_named_function_still_exists(self):
        for signature in (
            "async function openPack(", "async function openFreePack(",
            "function startPackOpenAnimation(", "function revealPackCards(",
            "function playerRevealCard(", "function closePackOpen(",
            "function showReward(", "function rewardTier(",
            "function launchConfetti(", "function haptic(", "function hapticNotif(",
        ):
            self.assertIn(signature, self.html, f"missing {signature}")

    def test_inline_handlers_still_target_the_same_functions(self):
        for handler in ('onclick="revealPackCards()"',
                        'onclick="revealPackCards(true)"',
                        'onclick="closePackOpen()"',
                        'onclick="openPack(${inv.inventory_id})"',
                        'onclick="openFreePack()"'):
            self.assertIn(handler, self.html, f"missing handler {handler}")

    def test_launch_confetti_keeps_its_signature_and_delegates(self):
        body = _function(self.html, "function launchConfetti(")
        self.assertIn("count = 60, tier = '', surface = document.body", body)
        self.assertIn("window.CMUAnim", body)
        # The pre-engine implementation has to survive as the fallback.
        self.assertIn("confetti-piece", body)

    def test_haptics_fall_back_when_the_engine_is_absent(self):
        for name in ("function haptic(", "function hapticNotif("):
            body = _function(self.html, name)
            self.assertIn("window.CMUAnim", body)
            self.assertIn("MiniappTheme.haptics", body,
                          f"{name} lost its pre-engine fallback")

    def test_reveal_still_drops_repeat_taps_before_it_animates(self):
        """The guard that stops a double tap opening one pack twice."""
        body = _function(self.html, "function revealPackCards(")
        self.assertIn("packRevealing", body)
        self.assertLess(body.index("packRevealing"),
                        body.index("classList.add('bursting')"))

    def test_open_pack_still_starts_the_animation_only_after_confirmation(self):
        body = _function(self.html, "async function openPack(")
        confirm = body.index("await confirmEconomy(")
        self.assertLess(confirm, body.index("startPackOpenAnimation()"),
                        "the reveal must not start before the server confirms")
        # A refused confirmation must return before anything is revealed.
        failure = body.index("if (!ok)")
        self.assertLess(confirm, failure)
        self.assertLess(failure, body.index("startPackOpenAnimation()"))

    def test_free_pack_still_starts_the_animation_only_after_confirmation(self):
        body = _function(self.html, "async function openFreePack(")
        submit = body.index("await submitReward('free-pack'")
        self.assertLess(submit, body.index("startPackOpenAnimation()"))
        self.assertIn("if (!ok)", body)


# ════════════════════════════════════════════════════════════════════
# 3. A confirmed reward is never trapped behind an animation
# ════════════════════════════════════════════════════════════════════

class RewardsStayReachableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read("templates/webapp.html")

    def test_done_is_not_gated_on_finishing_the_reveal(self):
        """Done appears with the cards, not when the last one is turned."""
        body = _function(self.html, "function revealPackCards(")
        self.assertIn("done.hidden = false;", body)
        # It must not be inside the deck's completion callback.
        complete = body.index("onComplete:")
        complete_end = body.index("},", complete)
        self.assertNotIn("done.hidden = false",
                         body[complete:complete_end],
                         "Done must not wait for every card to be turned")

    def test_a_face_down_deck_reveals_itself_eventually(self):
        body = _function(self.html, "function revealPackCards(")
        self.assertIn("PACK_AUTO_REVEAL_MS", body)
        self.assertIn("revealAll()", body)

    def test_both_settlement_paths_render_the_cards(self):
        """A sequence that throws must not cost the player the cards."""
        body = _function(self.html, "function revealPackCards(")
        self.assertIn(".then(render, render)", body,
                      "the rejection path must render too")

    def test_the_reward_chest_holds_its_contents_in_the_dom(self):
        src = _read("static/js/cmu_rewards_fx.js")
        chest = _function(src, "function chest(")
        self.assertIn("data-chest-reward", chest)
        self.assertIn("${rewardHtml}", chest,
                      "the reward must be rendered when the overlay opens, "
                      "not when the lid lifts")
        self.assertIn("setTimeout(openIt", chest,
                      "an untouched chest must open itself")

    def test_tapping_collect_first_opens_rather_than_closing(self):
        src = _read("static/js/cmu_rewards_fx.js")
        chest = _function(src, "function chest(")
        self.assertIn("if (!opened) { openIt(); return; }", chest)

    def test_the_walkout_never_blocks_its_caller(self):
        html = self.html
        body = _function(html, "function playWalkout(")
        self.assertIn(".catch(", body, "a failed walkout must still resolve")
        # Every caller fires and forgets.
        self.assertNotIn("await playWalkout", html,
                         "no reveal may wait on the walkout")

    def test_the_walkout_leaves_the_confirmed_rating_on_screen(self):
        src = _read("static/js/cmu_walkout.js")
        self.assertIn("overlay.onClose(() => {", src)
        self.assertIn("count.textContent = String(rating)", src)

    def test_counters_record_the_confirmed_value_before_animating(self):
        src = _read("static/js/cmu_rewards_fx.js")
        body = _function(src, "function setCount(")
        self.assertLess(body.index("el.dataset.cmuValue = String(target)"),
                        body.index("FX.countUp("),
                        "the confirmed value must be stored before the "
                        "animation that could be interrupted")


# ════════════════════════════════════════════════════════════════════
# 4. Graceful degradation
# ════════════════════════════════════════════════════════════════════

class DegradesWithoutTheEngineTests(unittest.TestCase):
    def test_every_feature_module_publishes_a_no_op_surface(self):
        """Loading a feature module without the core must not throw."""
        for rel in ("static/js/cmu_pack_theatre.js", "static/js/cmu_kitbag.js",
                    "static/js/cmu_walkout.js", "static/js/cmu_rewards_fx.js",
                    "static/js/cmu_match_fx.js"):
            src = _read(rel)
            self.assertIn("const FX = window.CMUAnim;", src, rel)
            self.assertIn("available: false", src,
                          f"{rel} has no no-op surface for a missing core")

    def test_the_mini_app_checks_availability_before_use(self):
        html = _read("templates/webapp.html")
        theatre = _function(html, "function packTheatre(")
        self.assertIn("theatre.available ? theatre : null", theatre)
        bag = _function(html, "function kitBag(")
        self.assertIn("bag.available ? bag : null", bag)

    def test_the_pack_overlay_keeps_its_css_fallback_artwork(self):
        html = _read("templates/webapp.html")
        self.assertIn('class="pack-foil" data-cmu-pack-art', html,
                      "the CSS foil is the no-JavaScript fallback")

    def test_the_arena_bridge_is_wrapped_against_failure(self):
        src = _read("static/cricket/app.js")
        for name in ("function announceMatchMoment(", "function announceMatchState("):
            body = _function(src, name)
            self.assertIn("try {", body)
            self.assertIn("catch", body)
            self.assertIn("matchFX()", body)

    def test_player_reveal_card_still_renders_without_the_engine(self):
        body = _function(_read("templates/webapp.html"), "function playerRevealCard(")
        self.assertIn("window.CMUAnim", body)
        self.assertIn("cardImg(url", body, "lost the pre-engine image fallback")


# ════════════════════════════════════════════════════════════════════
# 5. Match overlays read confirmed state and never block play
# ════════════════════════════════════════════════════════════════════

class MatchOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read("static/js/cmu_match_fx.js")
        cls.css = _read("static/css/cmu_match_fx.css")
        cls.arena = _read("static/cricket/app.js")

    def test_the_banner_layer_cannot_swallow_a_tap(self):
        self.assertIn("pointer-events: none", self.css)
        self.assertIn(".cmu-mx-layer * { pointer-events: none; }", self.css)

    def test_in_play_banners_do_not_use_the_focus_trapping_overlay(self):
        present = _function(self.src, "function present(")
        self.assertNotIn("FX.overlay(", present,
                         "an in-play banner must not lock scroll or take focus")

    def test_announcements_are_deduplicated_per_match(self):
        self.assertIn("function once(", self.src.replace("const once =", "function once("))
        announce = _function(self.src, "function announce(")
        self.assertIn("if (!once(event.token)) continue;", announce)

    def test_a_hat_trick_is_never_inferred_from_a_wicket_count(self):
        """Three wickets in a card can span overs; only the server knows."""
        body = _function(self.src, "function fromBall(")
        self.assertIn("comm.hatTrick || bowler.hatTrick", body)
        self.assertNotIn("bowlerWickets >= 3 ?", body)

    def test_the_module_never_writes_to_match_state(self):
        for token in ("matchState =", "matchState.score =", "state.score ="):
            self.assertNotIn(token, self.src)

    def test_the_arena_announces_from_its_single_ball_entry_point(self):
        trigger = _function(self.arena, "function triggerMatchEvent(")
        self.assertIn("announceMatchMoment(comm)", trigger)

    def test_autoplay_switches_the_overlays_into_fast_play(self):
        body = _function(self.arena, "function setAutoplayActive(")
        self.assertIn("setFastPlay", body)

    def test_the_queue_is_bounded(self):
        self.assertIn("queueCapacity", self.src)
        self.assertIn("FX.createQueue({ capacity: CONFIG.queueCapacity", self.src)


# ════════════════════════════════════════════════════════════════════
# 6. Motion settings
# ════════════════════════════════════════════════════════════════════

class MotionSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = _read("static/js/cmu_animation_engine.js")
        cls.settings = _read("static/js/cmu_settings.js")
        cls.html = _read("templates/webapp.html")

    def test_all_four_levels_exist(self):
        self.assertIn("const LEVELS = ['cinematic', 'standard', 'minimal', 'off'];",
                      self.engine)
        for value in ("cinematic", "standard", "minimal", "off"):
            self.assertIn(f'name="motion" value="{value}"', self.html)

    def test_every_required_toggle_is_offered(self):
        for name in ("sound", "haptics", "particles"):
            self.assertIn(f'name="{name}"', self.html)

    def test_a_system_preference_can_only_reduce_motion(self):
        body = _function(self.engine, "function level(")
        self.assertIn("reduceQuery.matches", body)
        self.assertIn("'minimal'", body)
        # It must never be able to raise the level.
        self.assertNotIn("'cinematic'", body)

    def test_settings_persist(self):
        self.assertIn("localStorage.setItem(STORE_KEY", self.engine)
        self.assertIn("localStorage.getItem(STORE_KEY)", self.engine)

    def test_blocked_storage_is_reported_rather_than_claimed_as_saved(self):
        self.assertIn("applied for this session", self.settings)
        self.assertIn("const persisted = FX.setAll(", self.settings,
                      "the form must report what the engine actually stored")

    def test_the_legacy_haptics_preference_is_honoured_and_kept_in_step(self):
        self.assertIn("LEGACY_HAPTIC_KEY", self.engine)
        self.assertIn("window.CMUAnim?.set('haptics'", _read("static/js/miniapp_theme.js"))

    def test_the_engine_loads_before_the_theme_module(self):
        """miniapp_theme.js mirrors the engine's haptics at parse time."""
        html = self.html
        self.assertLess(html.index("js/cmu_animation_engine.js"),
                        html.index("js/miniapp_theme.js"))
        arena = _read("static/cricket/index.html")
        self.assertLess(arena.index('src="/static/js/cmu_animation_engine.js'),
                        arena.index('src="/static/js/miniapp_theme.js'))

    def test_the_walkout_is_suppressed_at_the_reduced_levels(self):
        body = _function(self.engine, "function deservesWalkout(")
        self.assertIn("if (isMinimal()) return false;", body)

    def test_rarity_thresholds_are_configurable(self):
        self.assertIn("function configureRarity(", self.engine)
        self.assertIn("walkoutFrom", self.engine)


# ════════════════════════════════════════════════════════════════════
# 7. Accessibility, artwork and safe areas
# ════════════════════════════════════════════════════════════════════

class PresentationContractTests(unittest.TestCase):
    def test_overlays_trap_focus_and_restore_it(self):
        src = _read("static/js/cmu_animation_engine.js")
        body = _function(src, "function overlay(")
        for token in ("aria-modal", "role', 'dialog", "event.key === 'Escape'",
                      "event.key !== 'Tab'", "previousFocus"):
            self.assertIn(token, body, f"overlay() is missing {token}")

    def test_player_artwork_keeps_its_three_by_two_ratio(self):
        css = _read("static/css/cmu_animation_engine.css")
        self.assertIn("aspect-ratio: 3 / 2;", css)
        self.assertIn("object-fit: contain;", css,
                      "player cards must never be cropped or stretched")

    def test_missing_artwork_degrades_to_a_labelled_placeholder(self):
        src = _read("static/js/cmu_animation_engine.js")
        self.assertIn("function artFailed(", src)
        self.assertIn("cmu-fx-art-missing", src)
        self.assertIn('role', _function(src, "function artFailed("))

    def test_a_slow_image_cannot_hold_back_a_confirmed_reward(self):
        src = _read("static/js/cmu_animation_engine.js")
        body = _function(src, "function waitForArt(")
        self.assertIn("Promise.race", body)
        self.assertIn("sleep(timeout)", body)

    def test_overlays_respect_the_telegram_safe_area(self):
        css = _read("static/css/cmu_animation_engine.css")
        self.assertIn("--safe-top", css)
        self.assertIn("--safe-bottom", css)
        self.assertIn("env(safe-area-inset-top", css)

    def test_every_stylesheet_honours_the_system_preference(self):
        for rel in ENGINE_STYLES:
            css = _read(rel)
            self.assertIn("@media (prefers-reduced-motion: reduce)", css, rel)

    def test_the_engine_tears_overlays_down_when_the_app_is_hidden(self):
        src = _read("static/js/cmu_animation_engine.js")
        self.assertIn("visibilitychange", src)
        self.assertIn("pagehide", src)
        self.assertIn("teardown('hidden')", src)

    def test_a_drain_while_idle_does_not_disable_the_queue(self):
        """Regression: a sticky flag silently skipped every later task."""
        src = _read("static/js/cmu_animation_engine.js")
        body = _function(src, "function createQueue(")
        self.assertNotIn("draining", body,
                         "drain() must work through the generation counter "
                         "alone, leaving nothing set behind it")
        self.assertIn("generation += 1;", body)


if __name__ == "__main__":
    unittest.main()
