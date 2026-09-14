"""The tournament injury system: who gets hurt, for how long, and what it costs.

What these pin down:

  • the switch is real — off, nothing ever happens; on, the roll respects the
    configured chance and the layoff never exceeds the tournament's ceiling
  • an injury counts down one match per match that team plays, and the player is
    fit again the moment it reaches zero — never a match early or late
  • a player hurt in a match does not have that same match counted as served
  • a squad is never injured below a fieldable XI, and nobody is hurt twice over
  • the victim is drawn from players who actually batted or bowled, weighted by
    workload — a spectator on the bench is never injured
  • the Playing XI picker really is handed a squad without the injured in it,
    and an injury is scoped to its own tournament
  • resetting a tournament empties the treatment room with the results
  • Lets Play tournaments are excluded: there is no squad to rule anyone out of
"""

import itertools
import os
import random
import sys
import tempfile
import unittest

_TG = itertools.count(660_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.tournament_service", "services.injury_service",
                 "services.cl_tournament_view", "handlers.challenge",
                 "services.league_schedule_service", "services.knockout_service")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401  (registers the tables on Base)

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


SQUAD = 15


class InjuryCase(unittest.TestCase):
    """Two squads of 15 in a double round-robin, injuries on and certain."""

    CHANCE = 100
    ENABLED = True
    MAX_MATCHES = 3

    def setUp(self):
        from database import get_session
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            ChallengePlayer, Tournament, TournamentTeam, User)
        from services import (tournament_service, injury_service,
                              league_schedule_service)

        self.session = get_session()
        self.ts = tournament_service
        self.inj = injury_service
        self.lss = league_schedule_service

        mode = ChallengeMode(name=f"Mode {next(_TG)}")
        self.session.add(mode)
        self.session.flush()
        self.league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_TG)}",
                                      short_code="INJ")
        self.session.add(self.league)
        self.session.flush()

        self.tour = Tournament(
            name="Injury Cup", league_id=self.league.id,
            league_name=self.league.name, kind="challenge", status="active",
            league_format="double_rr", overs=20, max_teams=8,
            injuries_enabled=self.ENABLED, injury_chance=self.CHANCE,
            injury_max_matches=self.MAX_MATCHES)
        self.session.add(self.tour)
        self.session.flush()

        self.cteams, self.tteams, self.users = [], [], []
        for i, name in enumerate(("Alpha", "Bravo")):
            ct = ChallengeTeam(league_id=self.league.id, name=name, sort_order=i)
            self.session.add(ct)
            self.session.flush()
            for k in range(SQUAD):
                self.session.add(ChallengePlayer(team_id=ct.id,
                                                 name=f"{name}-P{k}", sort_order=k))
            tt = TournamentTeam(tournament_id=self.tour.id, challenge_team_id=ct.id,
                                name=name, sort_order=i)
            self.session.add(tt)
            user = User(telegram_id=next(_TG), username=f"inj{i}")
            self.session.add(user)
            self.cteams.append(ct)
            self.tteams.append(tt)
            self.users.append(user)
        self.session.flush()
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── helpers ──

    def squad(self, idx):
        from models import ChallengePlayer
        return (self.session.query(ChallengePlayer)
                .filter_by(team_id=self.cteams[idx].id)
                .order_by(ChallengePlayer.sort_order).all())

    def fit(self, idx):
        out = self.inj.injured_roster_ids(self.session, self.tour.id,
                                          self.tteams[idx].id)
        return [p for p in self.squad(idx) if p.id not in out]

    def play(self, *, seed=1, bench=()):
        """Record one Alpha-vs-Bravo match; returns its injury report.

        ``bench`` names players who take the field but neither bat nor bowl.
        """
        from models import Match
        random.seed(seed)
        match = Match(user1_id=self.users[0].id, user2_id=self.users[1].id,
                      status="active", match_type="cipl", overs=20,
                      tournament_id=self.tour.id)
        self.session.add(match)
        self.session.flush()

        def xi(idx):
            return [{"roster_id": p.id, "player_id": None, "name": p.name}
                    for p in self.fit(idx)[:11]]

        a, b = xi(0), xi(1)

        def stats(xi_rows, kind):
            out = {}
            for row in xi_rows:
                if row["name"] in bench:
                    continue
                out[str(row["roster_id"])] = (
                    {"runs": 20, "balls": 15, "out": True} if kind == "bat"
                    else {"wickets": 1, "runs": 30, "balls": 24})
            return out

        state = {
            "match_id": match.id, "tournament_id": self.tour.id,
            "tournament_team_by_user": {self.users[0].id: self.cteams[0].id,
                                        self.users[1].id: self.cteams[1].id},
            "inn1_bat_team_id": self.users[0].id,
            "inn1_bowl_team_id": self.users[1].id,
            "bat_team_id": self.users[1].id, "bowl_team_id": self.users[0].id,
            "bat_team_name": "Bravo", "bowl_team_name": "Alpha",
            "inn1_runs": 180, "inn1_wickets": 6,
            "total_runs": 170, "total_wickets": 9,
            "inn1_bat_xi": a, "inn1_bat_stats": stats(a, "bat"),
            "inn1_bowl_xi": b, "inn1_bowl_stats": stats(b, "bowl"),
            "bat_xi": b, "bat_stats": stats(b, "bat"),
            "bowl_xi": a, "bowl_stats": stats(a, "bowl"),
        }
        tm = self.ts.record_tournament_match(self.session, state)
        self.session.commit()
        return getattr(tm, "_injury_report", None) or {"new": [], "recovered": []}


class SwitchTests(InjuryCase):
    ENABLED = False

    def test_a_tournament_with_injuries_off_never_hurts_anybody(self):
        report = self.play()
        self.assertEqual(report["new"], [])
        self.assertEqual(self.inj.active_injuries(self.session, self.tour.id), [])

    def test_the_xi_picker_sees_a_full_squad(self):
        self.assertEqual(len(self.fit(0)), SQUAD)


class ZeroChanceTests(InjuryCase):
    CHANCE = 0

    def test_zero_chance_generates_nothing_but_leaves_the_system_on(self):
        self.assertTrue(self.inj.enabled_for(self.tour))
        self.assertEqual(self.play()["new"], [])

    def test_existing_injuries_still_count_down_at_zero_chance(self):
        # Turning generation off must not freeze the players already sidelined.
        victim = self.squad(0)[3]
        self.inj.add_manual(self.session, self.tour.id, self.tteams[0].id,
                            victim.id, injury_type="Strain", matches=2,
                            player_name=victim.name)
        self.session.commit()
        self.play()
        rows = self.inj.active_injuries(self.session, self.tour.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].matches_remaining, 1)


class RollTests(InjuryCase):
    def test_a_certain_roll_hurts_one_player_per_team(self):
        report = self.play()
        self.assertEqual(len(report["new"]), 2)
        teams = {r.tournament_team_id for r in report["new"]}
        self.assertEqual(teams, {self.tteams[0].id, self.tteams[1].id})

    def test_the_layoff_never_exceeds_the_tournaments_ceiling(self):
        for seed in range(25):
            self.setUp()
            for row in self.play(seed=seed)["new"]:
                self.assertGreaterEqual(row.matches_out, 1)
                self.assertLessEqual(row.matches_out, self.MAX_MATCHES)

    def test_an_injured_player_is_removed_from_the_pickable_squad(self):
        report = self.play()
        hurt = next(r for r in report["new"]
                    if r.tournament_team_id == self.tteams[0].id)
        self.assertNotIn(hurt.roster_id, [p.id for p in self.fit(0)])
        self.assertEqual(len(self.fit(0)), SQUAD - 1)

    def test_only_players_who_batted_or_bowled_can_be_hurt(self):
        # Everyone but one name is benched; the roll must land on that one name.
        played = "Alpha-P0"
        bench = {p.name for p in self.squad(0)[:11]} - {played}
        report = self.play(seed=7, bench=bench)
        for row in report["new"]:
            if row.tournament_team_id == self.tteams[0].id:
                self.assertEqual(row.player_name, played)

    def test_nobody_is_injured_twice_over(self):
        # Roll for the same team over and over with no match in between (so
        # nothing ever counts down): every roll must find a fresh victim, which
        # is what would break if the pool forgot to exclude the already-hurt.
        lines = [{"user_id": self.users[0].id, "roster_id": p.id,
                  "name": p.name, "bat_balls": 20, "bowl_balls": 24}
                 for p in self.squad(0)]
        for seed in range(12):
            random.seed(seed)
            self.inj.roll_for_team(self.session, self.tour, self.tteams[0],
                                   lines, self.users[0].id)
        self.session.commit()
        rows = self.inj.active_injuries(self.session, self.tour.id,
                                        self.tteams[0].id)
        ids = [r.roster_id for r in rows]
        self.assertEqual(len(ids), len(set(ids)),
                         "the same player is carrying two live injuries")
        # And the squad guard stopped it before the XI became unfieldable.
        self.assertGreaterEqual(len(self.fit(0)), 11)

    def test_a_squad_is_never_injured_below_a_fieldable_xi(self):
        # Rule out everyone bar eleven by hand, then play: the roll must decline
        # rather than leave the team unable to pick a side.
        for player in self.squad(0)[11:]:
            self.inj.add_manual(self.session, self.tour.id, self.tteams[0].id,
                                player.id, matches=3, player_name=player.name)
        self.session.commit()
        self.assertEqual(len(self.fit(0)), 11)
        report = self.play(seed=5)
        hurt_alpha = [r for r in report["new"]
                      if r.tournament_team_id == self.tteams[0].id]
        self.assertEqual(hurt_alpha, [])
        self.assertEqual(len(self.fit(0)), 11)


class CountdownTests(InjuryCase):
    CHANCE = 0  # no noise from fresh rolls while the clock is under test

    def _rule_out(self, matches):
        victim = self.squad(0)[4]
        self.inj.add_manual(self.session, self.tour.id, self.tteams[0].id,
                            victim.id, injury_type="Side Strain",
                            matches=matches, player_name=victim.name)
        self.session.commit()
        return victim

    def test_a_two_match_injury_lasts_exactly_two_matches(self):
        victim = self._rule_out(2)
        self.assertNotIn(victim.id, [p.id for p in self.fit(0)])
        self.play(seed=1)
        self.assertNotIn(victim.id, [p.id for p in self.fit(0)],
                         "still out after one match")
        self.play(seed=2)
        self.assertIn(victim.id, [p.id for p in self.fit(0)],
                      "fit again after two")

    def test_recovery_is_announced_once(self):
        self._rule_out(1)
        report = self.play(seed=1)
        self.assertEqual(len(report["recovered"]), 1)
        self.assertEqual(self.play(seed=2)["recovered"], [])

    def test_healing_by_hand_returns_a_player_immediately(self):
        victim = self._rule_out(3)
        rows = self.inj.active_injuries(self.session, self.tour.id)
        self.inj.heal(self.session, rows[0].id)
        self.session.commit()
        self.assertIn(victim.id, [p.id for p in self.fit(0)])

    def test_a_manual_injury_replaces_rather_than_stacks(self):
        victim = self._rule_out(3)
        self.inj.add_manual(self.session, self.tour.id, self.tteams[0].id,
                            victim.id, matches=1, player_name=victim.name)
        self.session.commit()
        rows = self.inj.active_injuries(self.session, self.tour.id,
                                        self.tteams[0].id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].matches_remaining, 1)

    def test_a_manual_injury_is_capped_by_the_tournament_ceiling(self):
        victim = self.squad(0)[6]
        row = self.inj.add_manual(self.session, self.tour.id, self.tteams[0].id,
                                  victim.id, matches=99, player_name=victim.name)
        self.session.commit()
        self.assertEqual(row.matches_out, self.MAX_MATCHES)


class HurtInThisMatchTests(InjuryCase):
    def test_a_player_hurt_today_does_not_have_today_counted_as_served(self):
        # Count-down runs before the roll, so a fresh injury keeps its full span.
        for row in self.play(seed=9)["new"]:
            self.assertEqual(row.matches_remaining, row.matches_out)


class ResetTests(InjuryCase):
    def test_resetting_a_tournament_empties_the_treatment_room(self):
        self.play(seed=2)
        self.assertTrue(self.inj.active_injuries(self.session, self.tour.id))
        self.ts.reset_tournament(self.session, self.tour.id)
        self.session.commit()
        self.assertEqual(self.inj.active_injuries(self.session, self.tour.id), [])
        self.assertEqual(len(self.fit(0)), SQUAD)


class SettingsTests(InjuryCase):
    def test_values_are_clamped_when_read(self):
        self.tour.injury_chance = 900
        self.tour.injury_max_matches = 40
        enabled, chance, cap = self.inj.settings(self.tour)
        self.assertTrue(enabled)
        self.assertEqual(chance, 100)
        self.assertEqual(cap, 5)

    def test_a_missing_tournament_reads_as_off(self):
        self.assertEqual(self.inj.settings(None), (False, 0, 3))
        self.assertFalse(self.inj.enabled_for(None))

    def test_lets_play_tournaments_are_excluded(self):
        # A Lets Play "team" is a person playing their own roster — there is no
        # squad to rule anybody out of.
        self.tour.kind = "letsplay"
        self.session.commit()
        self.assertFalse(self.inj.enabled_for(self.tour))
        self.assertEqual(self.play()["new"], [])


class XiPickerTests(InjuryCase):
    """The bot half: the squad the Playing XI picker is actually handed."""

    CHANCE = 0

    def _squad_for_picker(self, idx):
        """What ``handlers.challenge`` loads for this side of a tournament draft."""
        import handlers.challenge as challenge
        draft = {
            "league_key": "inj", "league_id": self.league.id,
            "is_tournament": True, "tournament_id": self.tour.id,
            "host_team": self.cteams[0].name, "target_team": self.cteams[1].name,
        }
        return challenge._load_team_players_with_retry(
            draft, "host" if idx == 0 else "target")

    def test_an_injured_player_is_not_in_the_list_the_picker_shows(self):
        victim = self.squad(0)[2]
        self.inj.add_manual(self.session, self.tour.id, self.tteams[0].id,
                            victim.id, injury_type="Side Strain", matches=2,
                            player_name=victim.name)
        self.session.commit()

        players, _team_id, cfg = self._squad_for_picker(0)
        self.assertNotIn(victim.id, [p.id for p in players])
        self.assertEqual(len(players), SQUAD - 1)
        # And the picker is told who is missing, so a shorter list isn't a mystery.
        self.assertEqual([r["name"] for r in cfg["injured_out"]], [victim.name])
        self.assertEqual(cfg["injured_out"][0]["matches"], 2)

    def test_a_fit_squad_comes_back_whole(self):
        players, _team_id, cfg = self._squad_for_picker(0)
        self.assertEqual(len(players), SQUAD)
        self.assertEqual(cfg["injured_out"], [])

    def test_a_casual_league_match_ignores_tournament_injuries(self):
        # The same team outside the tournament fields everybody: an injury is
        # scoped to the competition it happened in.
        import handlers.challenge as challenge
        victim = self.squad(0)[2]
        self.inj.add_manual(self.session, self.tour.id, self.tteams[0].id,
                            victim.id, matches=2, player_name=victim.name)
        self.session.commit()
        players, _tid, _cfg = challenge._load_team_players_with_retry(
            {"league_key": "inj", "league_id": self.league.id,
             "is_tournament": False, "tournament_id": None,
             "host_team": self.cteams[0].name}, "host")
        self.assertIn(victim.id, [p.id for p in players])

    def test_the_note_names_the_injury_and_the_games_left(self):
        import handlers.challenge as challenge
        draft = {"injured_out": {"host": [
            {"name": "Bumrah", "injury": "Side Strain", "matches": 2}]}}
        note = "\n".join(challenge._injury_note(draft, "host"))
        self.assertIn("Bumrah", note)
        self.assertIn("Side Strain", note)
        self.assertIn("2 more matches", note)
        self.assertEqual(challenge._injury_note({}, "host"), [])


class ReportTests(InjuryCase):
    def test_the_chat_card_names_the_player_the_team_and_the_layoff(self):
        report = self.play(seed=6)
        text = self.inj.render_report(self.session, self.tour.id, report)
        self.assertIn("Injury news", text)
        row = report["new"][0]
        self.assertIn(row.player_name, text)
        self.assertIn(row.injury_type, text)

    def test_nothing_happened_renders_nothing(self):
        self.assertEqual(
            self.inj.render_report(self.session, self.tour.id,
                                   {"new": [], "recovered": []}), "")

    def test_the_injury_list_groups_by_team_and_flags_your_own(self):
        from services import cl_tournament_view as ctv
        self.tteams[0].owner_tg_id = 55501
        self.play(seed=8)
        text = ctv.render_injuries(self.session, self.tour, viewer_tg_id=55501)
        self.assertIn("Alpha", text)
        self.assertIn("yours", text)

    def test_the_list_says_so_when_the_system_is_off(self):
        from services import cl_tournament_view as ctv
        self.tour.injuries_enabled = False
        self.session.commit()
        self.assertIn("no injury system",
                      ctv.render_injuries(self.session, self.tour))


if __name__ == "__main__":
    unittest.main()
