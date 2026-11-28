"""The Event Studio admin screen renders its wizard and keeps every bound field."""

import re
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class StudioWizardScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import admin
        cls.admin = admin

    def setUp(self):
        from database import Base
        from services import event_studio as studio
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.db = self.sessions()
        config = studio.defaults()
        config.update(kind="live", objective_scope="tournament",
                      opens_at="2026-12-01T09:00", closes_at="2026-12-24T07:00",
                      teams=[{"key": f"team-{n}", "name": f"Team {n}", "players": []}
                             for n in range(4)])
        self.event = studio.create(self.db, "Summer Cup", "summer-cup", "operator", config)
        self.db.commit()
        self.patches = [
            patch("services.event_studio_admin.get_session", self.sessions),
            patch.object(self.admin, "get_session", self.sessions),
            patch.dict(self.admin.app.config, TESTING=True, WTF_CSRF_ENABLED=False),
        ]
        for item in self.patches:
            item.start()
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as session:
            session["admin"] = True

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.db.close()
        self.engine.dispose()

    def page(self):
        response = self.client.get(f"/events/studio/{self.event.id}")
        self.assertEqual(response.status_code, 200, response.data[:4000])
        return response.get_data(as_text=True)

    def test_the_event_list_and_the_draft_screen_both_render(self):
        listing = self.client.get("/events/studio")
        self.assertEqual(listing.status_code, 200, listing.data[:4000])
        self.assertIn("Summer Cup", listing.get_data(as_text=True))
        self.assertIn("Summer Cup", self.page())

    def test_the_draft_is_laid_out_as_numbered_wizard_steps(self):
        page = self.page()
        steps = re.findall(r'data-step="([a-z-]+)"', page)
        # Every step the creation flow names, in its order, with a nav entry each.
        self.assertEqual(steps[: len(STEPS)], STEPS)
        for step in STEPS:
            self.assertIn(f'data-step-to="{step}"', page)

    def test_every_configuration_field_the_screen_had_is_still_bound(self):
        bound = set(re.findall(r'data-config-path="([^"]+)"', self.page()))
        missing = sorted(field for field in PREVIOUSLY_BOUND if field not in bound)
        self.assertEqual(missing, [], f"fields dropped from the screen: {missing}")

    def test_the_wizard_binds_the_settings_the_json_only_fields_used_to_hide(self):
        bound = set(re.findall(r'data-config-path="([^"]+)"', self.page()))
        missing = sorted(field for field in NEWLY_BOUND if field not in bound)
        self.assertEqual(missing, [], f"settings still unreachable from the form: {missing}")


# The creation flow from the specification, in order.
STEPS = ["event", "timing", "schedule", "participation", "match",
         "objectives", "rewards", "features", "advanced", "publish"]

# Everything the pre-wizard screen bound, so a restructure cannot silently drop one.
PREVIOUSLY_BOUND = [
    "ai_managers.allowlist", "ai_managers.blend", "ai_managers.enabled", "ai_managers.franchises",
    "attempts", "banner_url", "challenge_style", "chapter.min_stars", "chapter.name",
    "chapter.position", "chapter.prerequisite_slug", "closes_at", "community.boss_goal",
    "community.completion_xp", "community.daily_limit", "competition.auto_start_full",
    "competition.double_no_show_points", "competition.early_start", "competition.management_cap",
    "competition.management_weights", "competition.manager_xp_cap", "competition.manager_xp_weights",
    "competition.match_rewards", "competition.min_humans", "competition.modes",
    "competition.news_upset_gap", "competition.predictor_samples", "competition.rating_k",
    "competition.registration_minutes", "description", "difficulty", "featured",
    "fixture_overrides", "fixtures", "format", "impact_player", "kind", "ladder_generator",
    "max_users", "milestones", "mode", "moment.checkpoint", "moment.goal", "moment.goal_type",
    "moment.objective_baseline", "moment.overs", "moment.seed", "moment.side", "moment.start_over",
    "notifications.enabled", "notifications.reminders_hours", "objective_ladders",
    "objective_preset", "objective_scope", "objectives", "objectives_locked", "opens_at",
    "pass_inventory.auto_diamond", "pass_inventory.auto_free", "pass_inventory.cards",
    "pass_inventory.packs", "rewards", "rotation.cadence", "rotation.periods",
    "rotation.series_budget", "schedule.grace", "schedule.grand_final_reset", "schedule.groups",
    "schedule.legs", "schedule.playoffs", "schedule.preset", "schedule.previous_event_id",
    "schedule.rest_minutes", "schedule.seed", "schedule.template", "schedule.tour",
    "schedule.venue_countries", "schedule.window_hours", "teams", "timezone",
    "tournament_objectives",
]

# Settings the configuration always had but only raw JSON could reach.
NEWLY_BOUND = [
    "claim_days", "gender", "home_country", "max_overseas", "min_overseas",
    "points.loss", "points.no_result", "points.tie", "points.win",
    "schedule.blackouts", "schedule.fixtures_per_day", "schedule.games",
    "schedule.match_target", "schedule.min_rest_minutes", "schedule.playing_days",
    "schedule.qualify_per_group", "schedule.round_spacing_days", "schedule.third_place",
]
