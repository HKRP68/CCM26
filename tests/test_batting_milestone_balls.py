"""Milestone balls are captured at the crossing, not at over/innings end."""
import json
import unittest
from unittest.mock import patch
from services.batting_milestones import capture, saved
from services import cipl_match as cm
from tests.test_cipl_approach import _make_state


class MilestoneBallTests(unittest.TestCase):
    def test_crossings_survive_more_balls_and_json(self):
        st = dict(runs=52,balls=14)
        capture(st,48)
        st.update(runs=101,balls=29);capture(st,97)
        st.update(runs=152,balls=44);capture(st,148)
        st.update(runs=170,balls=50);capture(st,152)
        restored=json.loads(json.dumps(st))
        self.assertEqual([14,29,44],[saved(restored,n) for n in (50,100,150)])

    def test_existing_innings_are_not_backfilled(self):
        st=dict(runs=85,balls=30)
        capture(st,80)
        self.assertIsNone(saved(st,50))
        st.update(runs=100,balls=35);capture(st,96)
        self.assertEqual(35,saved(st,100))
        self.assertIsNone(saved(st,50))

    def test_invalid_milestones_are_excluded(self):
        for value in (None,True,0,-1,'14',31):
            self.assertIsNone(saved(dict(runs=60,balls=30,balls_to_50=value),50))
        self.assertIsNone(saved(dict(runs=49,balls=30,balls_to_50=14),50))

    def state(self,runs,balls):
        st=_make_state(overs=20)
        st['current_bowler']=cm.eligible_bowlers(st)[0]
        st['batting_approach']=st['bowling_approach']='balanced'
        rid=str(st['batting_order'][st['striker_idx']]['roster_id'])
        st['bat_stats'][rid].update(runs=runs,balls=balls)
        st['total_runs']=runs
        return st,rid

    def test_over_records_exact_first_ball_for_each_threshold(self):
        from services import config_service
        with patch.dict(config_service._CACHE,data=dict(config_service.DEFAULTS)):
            for mark in (50,100,150):
                with self.subTest(mark=mark):
                    st,rid=self.state(mark-2,12)
                    with patch.object(cm,'calculate_outcome',return_value=dict(type='run',runs=4,is_extra=False,batter_out=False)):
                        cm.simulate_over(st)
                    self.assertEqual(18,st['bat_stats'][rid]['balls'])
                    self.assertEqual(13,st['bat_stats'][rid][f'balls_to_{mark}'])

    def test_wide_is_not_faced_but_no_ball_is(self):
        from services import config_service
        st,rid=self.state(48,12)
        bowler_rid=str(st['current_bowler']['roster_id'])
        outcomes=[dict(type='extra',runs=0,is_extra=True,extra_type='Wide'),
                  dict(type='extra',runs=4,is_extra=True,extra_type='No Ball')]
        outcomes += [dict(type='run',runs=0,is_extra=False,batter_out=False) for _ in range(6)]
        with patch.dict(config_service._CACHE,data=dict(config_service.DEFAULTS)), \
                patch.object(cm,'calculate_outcome',side_effect=outcomes):
            cm.simulate_over(st)
        self.assertEqual(13,st['bat_stats'][rid]['balls_to_50'])
        self.assertEqual(19,st['bat_stats'][rid]['balls'])
        bw=st['bowl_stats'][bowler_rid]
        self.assertEqual(6,bw['balls'])

    def test_tournament_scorecard_keeps_both_innings_milestones(self):
        from services.tournament_service import _collect_player_lines
        state=dict(bat_team_id=2,bowl_team_id=1,bat_team_name='B',bowl_team_name='A',
            inn1_bat_team_id=1,inn1_bat_xi=[dict(roster_id=1,player_id=1,name='First')],
            inn1_bat_stats={'1':dict(runs=152,balls=60,balls_to_50=15,balls_to_100=34,balls_to_150=58)},
            bat_xi=[dict(roster_id=2,player_id=2,name='Second')],
            bat_stats={'2':dict(runs=101,balls=40,balls_to_50=19,balls_to_100=39)})
        lines=_collect_player_lines(json.loads(json.dumps(state)))
        first,second=lines
        self.assertEqual((15,34,58),tuple(first[f'balls_to_{n}'] for n in (50,100,150)))
        self.assertEqual(19,second['balls_to_50'])
        self.assertEqual(39,second['balls_to_100'])
        self.assertNotIn('balls_to_150',second)


    def test_browser_ball_path_preserves_crossing_and_scorecard(self):
        from services import match_webapp_service as web
        st,rid=self.state(48,12)
        st.update(wides=0,noballs=0,legbyes=0,byes=0)
        striker=st['batting_order'][st['striker_idx']]
        bowler=st['current_bowler']
        web._apply_outcome(st,dict(type='runs',runs=4),'Drive','Stock',striker,bowler)
        web._apply_outcome(st,dict(type='runs',runs=6),'Drive','Stock',striker,bowler)
        self.assertEqual(13,st['bat_stats'][rid]['balls_to_50'])
        with patch.object(web.mwa,'get_state',return_value=st):
            card=web.build_scorecard(1,10)
        # The final-scorecard builder uses the same batting serializer.
        self.assertIn('"balls_to_50": 13',json.dumps(card))
