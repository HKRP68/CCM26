"""Delivery observations survive JSON and separate attribution from inference."""
import json
import unittest
from unittest.mock import patch
from services import record_telemetry as T, cipl_match as CM
from tests.test_cipl_approach import _make_state


class TelemetryTests(unittest.TestCase):
    def state(self):
        state=_make_state(20)
        state['current_bowler']=CM.eligible_bowlers(state)[0]
        state['batting_approach']=state['bowling_approach']='balanced'
        return state

    def test_actual_over_records_partnership_runs_and_phase_balls(self):
        from services import config_service
        state=self.state()
        with patch.dict(config_service._CACHE,data=dict(config_service.DEFAULTS)),patch.object(CM,'calculate_outcome',return_value=dict(type='run',runs=4,is_extra=False,batter_out=False)):
            CM.simulate_over(state)
        detail=json.loads(json.dumps(T.detail(state)))
        result=T.analyse(detail)
        self.assertEqual(6,len(result['deliveries']))
        self.assertEqual(24,result['partnerships'][0]['runs'])
        self.assertEqual(6,result['partnerships'][0]['balls'])
        batting=next(p for p in result['phases'] if p['side']=='bat')
        self.assertEqual(('powerplay',24,6),(batting['phase'],batting['runs'],batting['balls']))
        from services.tournament_service import _collect_player_lines
        lines=_collect_player_lines(state)
        self.assertIn('record_detail',lines[0])
        self.assertNotIn('invalid_reason',T.analyse(detail,lines=lines))

    def test_partnership_milestone_keeps_exact_legal_balls_and_contributions(self):
        state=self.state();striker=state['batting_order'][0];bowler=state['current_bowler']
        for n in range(26):
            before=T.snapshot(state,striker,bowler)
            bat=state['bat_stats'][before['rid']];bw=state['bowl_stats'][before['bid']]
            run=4
            state['total_runs']+=run;bat['runs']+=run;bat['balls']+=1;bw['runs']+=run;bw['balls']+=1
            T.capture(state,before,dict(type='runs',runs=run))
        stand=T.analyse(T.detail(state))['partnerships'][0]
        self.assertEqual({'50':13,'100':25},stand['milestones'])
        self.assertEqual(104,stand['contributions'][striker['name']])

    def test_wide_adds_stand_runs_without_a_legal_ball(self):
        state=self.state();before=T.snapshot(state,state['batting_order'][0],state['current_bowler'])
        state['total_runs']+=1;state['bowl_stats'][before['bid']]['runs']+=1
        T.capture(state,before,dict(type='wide',runs=0))
        stand=T.analyse(T.detail(state))['partnerships'][0]
        self.assertEqual((1,0),(stand['runs'],stand['balls']))

    def test_partial_innings_never_manufactures_complete_partnership(self):
        state=self.state();state['total_runs']=80
        before=T.snapshot(state,state['batting_order'][0],state['current_bowler'])
        state['total_runs']+=4;state['bat_stats'][before['rid']]['runs']=4;state['bat_stats'][before['rid']]['balls']=1
        T.capture(state,before,dict(type='runs',runs=4))
        self.assertFalse(T.analyse(T.detail(state))['partnerships'][0]['complete'])
        from services.league_record_insights import advanced
        self.assertEqual([],advanced([dict(analytics=T.analyse(T.detail(state)))])['partnerships'])

    def wicket(self,how,*,keeper=False,direct=None,assists=None):
        state=self.state()
        state['bowl_xi'][1]['name']='Fielder'
        state['bowl_xi'][1]['category']='Wicket Keeper' if keeper else 'Batsman'
        before=T.snapshot(state,state['batting_order'][0],state['current_bowler'])
        state['total_wickets']=1
        state['bat_stats'][before['rid']].update(out=True,how_out=how,fielder='Fielder',balls=1)
        oc=dict(type='wicket',runs=0,assists=assists or [])
        if direct is not None:oc['direct_hit']=direct
        T.capture(state,before,oc)
        return T.analyse(T.detail(state))['fielding']

    def test_fielding_and_keeper_credits_stay_separate(self):
        self.assertEqual(1,self.wicket('Caught')[0]['catches'])
        keeper=self.wicket('Caught',keeper=True)[0]
        self.assertEqual((0,1),(keeper['catches'],keeper['keeper_catches']))
        self.assertEqual(1,self.wicket('Stumped',keeper=True)[0]['stumpings'])

    def test_runout_types_are_not_guessed(self):
        self.assertEqual(1,self.wicket('Run Out')[0]['unclassified_runouts'])
        self.assertEqual(1,self.wicket('Run Out',direct=True)[0]['direct_runouts'])
        fielders=self.wicket('Run Out',direct=False,assists=['Helper','Helper','Fielder'])
        self.assertEqual(2,sum(p['assisted_runouts'] for p in fielders))

    def test_phase_windows_follow_match_format_and_length(self):
        state=self.state()
        for over,want in [(1,'powerplay'),(6,'powerplay'),(7,'middle'),(15,'middle'),(16,'death')]:
            state['current_over']=over;self.assertEqual(want,T.phase(state))
        state.update(overs=5,current_over=5);self.assertEqual('death',T.phase(state))
        state.update(overs=20,ball_format='The100',current_over=6);self.assertEqual('middle',T.phase(state))

    def test_corrected_aggregate_invalidates_stale_advanced_evidence(self):
        from services import config_service
        state=self.state()
        with patch.dict(config_service._CACHE,data=dict(config_service.DEFAULTS)),patch.object(CM,'calculate_outcome',return_value=dict(type='run',runs=4,is_extra=False,batter_out=False)):
            CM.simulate_over(state)
        from services.tournament_service import _collect_player_lines
        lines=_collect_player_lines(state)
        lines[0]['bat_runs']+=10
        result=T.analyse(T.detail(state),lines=lines)
        self.assertIn('invalid_reason',result)
        self.assertEqual([],result['partnerships'])
