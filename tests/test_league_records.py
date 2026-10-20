"""Records: exact ties, qualifications, evidence, archives and live deduplication."""
import asyncio
import json
import os
import sys
import unittest
from datetime import datetime
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_league_seasons_page as pages
setUpModule = pages.setUpModule
tearDownModule = pages.tearDownModule


class RecordTests(pages.PageCase):
    def setUp(self):
        super().setUp()
        from models import LeagueSeason, TournamentMatch, TournamentTeam
        from services import league_records as R, league_season_service as LS
        self.R, self.LS = R, LS
        self.series = LS.create_series(self.session, f'Record Cup {self.tag}')
        self.season = LS.attach_season(self.session, self.series, tournament=self.tour)
        self.teams = self.session.query(TournamentTeam).filter_by(tournament_id=self.tour.id).all()
        self.a, self.b = self.teams
        self.game = TournamentMatch(tournament_id=self.tour.id, match_id=90000+self.tag,
            team1_id=self.a.id, team2_id=self.b.id, winner_team_id=self.b.id,
            inn1_runs=150, inn2_runs=151, status='completed', stage='final',
            venue='Test Ground', completed_at=datetime(2026,1,1),
            scorecard_json=json.dumps([self.line('Alpha',60,30,2,20,12),self.line('Beta',60,30,2,20,12)]))
        self.session.add(self.game)
        R.save_config(self.session,self.season,30,12)
        self.session.commit()
        self.url=f'/league-seasons/{self.series.id}/records'

    def line(self,name,runs=0,balls=0,wkts=0,conceded=0,bowled=0):
        return dict(name=name,team_name=self.a.name,user_id=1,player_id=100 if name=='Alpha' else 101,
                    batted=balls>0,bat_runs=runs,bat_balls=balls,bat_out=True,bat_fours=2,bat_sixes=1,
                    bowled=bowled>0,bowl_wickets=wkts,bowl_runs=conceded,bowl_balls=bowled)

    def board(self,key,**kw):
        return next(b for b in self.R.boards(self.session,self.series,self.season,**kw)['boards'] if b['key']==key)

    def test_exact_ties_and_no_secondary_tiebreak(self):
        sr=self.board('sr')
        self.assertEqual([1,1],[e['rank'] for e in sr['entries']])
        self.assertEqual(['Alpha','Beta'],[h['name'] for h in sr['holders']])
        self.assertEqual(Fraction(200),sr['holders'][0]['value'])
        self.assertEqual('2/20',self.board('fig')['holders'][0]['display'])

    def test_minimum_changes_apply_immediately_including_zero(self):
        self.R.save_config(self.session,self.season,31,13)
        self.assertFalse(self.board('sr')['entries'])
        self.assertFalse(self.board('econ')['entries'])
        self.assertEqual(2,len(self.board('runs')['entries']))
        self.R.save_config(self.session,self.season,0,0)
        self.assertEqual(2,len(self.board('sr')['entries']))
        self.assertEqual(0,self.tour.min_balls_for_sr)
        for value in (-1,'1.5',100001,None):
            with self.assertRaises(ValueError):
                self.R.save_config(self.session,self.season,value,10)
        self.assertEqual(0,self.R.config(self.session,self.season).min_bat_balls)

    def test_zero_denominators_and_zero_run_economy(self):
        self.game.scorecard_json=json.dumps([self.line('Alpha',0,0,0,0,12)])
        self.session.flush()
        self.assertFalse(self.board('avg')['entries'])
        self.assertFalse(self.board('sr')['entries'])
        self.assertFalse(self.board('bavg')['entries'])
        self.assertEqual('0.00',self.board('econ')['holders'][0]['display'])

    def test_match_evidence_is_preserved_on_archive(self):
        from services import season_archive
        season_archive.delete_tournament(self.session,self.tour,keep_stats=True)
        self.session.commit()
        self.assertIsNone(self.season.tournament_id)
        self.assertEqual('Test Ground',self.board('hs')['holders'][0]['source']['venue'])
        response=self.client.get(f'{self.url}/{self.season.id}/match/{self.game.id}')
        self.assertEqual(200,response.status_code)
        self.assertIn(b'Alpha',response.data)
        self.assertEqual(2,len(self.board('runs')['holders']))

    def test_correction_and_status_exclusions(self):
        self.game.scorecard_json=json.dumps([self.line('Alpha',20,30),self.line('Beta',70,30)])
        self.session.flush()
        self.assertEqual('Beta',self.board('hs')['holders'][0]['name'])
        self.game.is_simulated=True
        self.session.flush()
        self.assertFalse(self.board('hs')['entries'])
        self.game.is_simulated=False
        self.game.status='scheduled'
        self.session.flush()
        self.assertFalse(self.board('hs')['entries'])

    def test_filters_and_scopes_do_not_mix_seasons(self):
        from models import LeagueSeason
        other=LeagueSeason(series_id=self.series.id,season_no=2,name='Second')
        self.session.add(other);self.session.flush()
        self.assertFalse(self.R.boards(self.session,self.series,other)['players'])
        self.assertFalse(self.board('hs',stage='league')['entries'])
        self.assertTrue(self.board('hs',stage='final')['entries'])
        self.assertEqual(150,self.R.boards(self.session,self.series,self.season,team=self.a.name)['summary']['runs'])
        all_time=self.R.boards(self.session,self.series)
        self.assertEqual(2,len(all_time['players']))
        self.assertEqual(2,len(self.board('hs')['entries']))

    def test_milestones_are_innings_based(self):
        self.game.scorecard_json=json.dumps([self.line('Alpha',100,40),self.line('Beta',50,30)])
        self.session.flush()
        self.assertEqual(['Beta'],[h['name'] for h in self.board('fifties')['holders']])
        self.assertEqual(['Alpha'],[h['name'] for h in self.board('hundreds')['holders']])

    def test_settings_page_and_source_access(self):
        response=self.client.get(self.url)
        self.assertEqual(200,response.status_code)
        for token in (b'Minimum batting balls',b'Default record GIF',b'Delivery insights',b'Alpha'):
            self.assertIn(token,response.data)
        response=self.client.post(self.url,data=dict(season=self.season.id,min_bat_balls=99,min_bowl_balls=88,alerts_enabled='on'))
        self.assertEqual(302,response.status_code)
        self.session.expire_all()
        self.assertEqual(99,self.R.config(self.session,self.season).min_bat_balls)
        self.client.post(self.url,data=dict(season=self.season.id,min_bat_balls=-1,min_bowl_balls=0))
        self.session.expire_all()
        self.assertEqual(99,self.R.config(self.session,self.season).min_bat_balls)
        self.assertEqual(404,self.client.get(f'{self.url}?season=999999').status_code)
        self.assertEqual(400,self.client.post(self.url,data=dict(season='all')).status_code)
        with self.client.session_transaction() as s:
            s.clear()
        self.assertEqual(302,self.client.get(self.url).status_code)

    def state(self):
        return dict(tournament_id=self.tour.id,match_id=99000+self.tag,chat_id=-100,
            bat_team_id=1,bowl_team_id=2,bat_team_name=self.a.name,bowl_team_name=self.b.name,
            bat_xi=[dict(roster_id=1,player_id=100,name='Alpha')],
            bowl_xi=[],bat_stats={'1':dict(runs=80,balls=35,fours=4,sixes=2,out=False)},
            bowl_stats={},total_runs=85,total_wickets=0)

    def test_live_alerts_deduplicate_and_only_claim_once(self):
        state=self.state()
        first=self.R.collect_alerts(self.session,state)
        self.session.commit()
        self.assertTrue(any(a[2]['record']=='Highest individual score' for a in first))
        self.assertFalse(self.R.collect_alerts(self.session,state))
        state['bat_stats']['1']['runs']=90
        self.assertFalse(self.R.collect_alerts(self.session,state))
        self.R.save_config(self.session,self.season,30,12,alerts=False)
        state['match_id']+=1
        self.assertFalse(self.R.collect_alerts(self.session,state))

    def test_equal_record_and_final_rate_alerts(self):
        state=self.state();state['bat_stats']['1']['runs']=60
        state['bat_xi'][0]['name']='Gamma'
        first=self.R.collect_alerts(self.session,state)
        self.assertTrue(any(a[0]=='record_equalled' and a[2]['record']=='Highest individual score' for a in first))
        self.assertFalse(any(a[2]['record']=='Highest strike rate' for a in first))
        state['match_id']=self.game.match_id
        final=self.R.collect_alerts(self.session,state,final=True)
        self.assertTrue(any(a[2]['record']=='Highest strike rate' for a in final))
        self.session.commit()
        self.assertFalse(self.R.collect_alerts(self.session,state,final=True))

    def test_live_records_never_send_messages_or_gifs(self):
        from services import event_media_service
        state = self.state()
        bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        context = SimpleNamespace(bot=bot, bot_data={})
        with patch.object(event_media_service, 'fire_event_media', new_callable=AsyncMock) as media:
            asyncio.run(self.R.announce(context, state))
            bot.send_message.assert_not_awaited()
            bot.send_document.assert_not_awaited()
            media.assert_not_awaited()

    def test_season_delete_cleans_configuration_and_alerts(self):
        from models import LeagueRecordAlert, LeagueRecordConfig
        self.R.collect_alerts(self.session,self.state())
        sid=self.season.id
        self.LS.detach_season(self.session,self.season)
        self.session.commit()
        self.assertIsNone(self.session.get(LeagueRecordConfig,sid))
        self.assertEqual(0,self.session.query(LeagueRecordAlert).filter_by(season_id=sid).count())

    def test_telegram_records_and_holder_gif_obey_opener(self):
        from database import get_session
        from handlers import league_seasons as H
        from services import event_media_service
        answers, edits = [], []
        async def answer(text=None,**kw): answers.append(text)
        async def edit_message_text(text,**kw): edits.append(text)
        query=SimpleNamespace(data=f'ls_r_{self.series.id}_{self.season.id}-sr_123',
            from_user=SimpleNamespace(id=123),answer=answer,edit_message_text=edit_message_text,
            message=SimpleNamespace(chat_id=-100,message_id=1))
        update=SimpleNamespace(callback_query=query)
        context=SimpleNamespace(bot=SimpleNamespace(),bot_data={})
        with patch.object(H,'LS',self.LS), patch.object(H,'get_session',get_session), \
                patch.object(event_media_service,'fire_event_media',new_callable=AsyncMock) as media:
            asyncio.run(H.seasons_callback(update,context))
            self.assertIn('Alpha',edits[-1])
            self.assertIn('Beta',edits[-1])
            query.data=f'ls_h_{self.series.id}_{self.season.id}-sr_123'
            asyncio.run(H.seasons_callback(update,context))
            self.assertEqual('record_default',media.call_args.args[2])
            query.from_user.id=999
            count=media.call_count
            asyncio.run(H.seasons_callback(update,context))
            self.assertEqual(count,media.call_count)
            self.assertIn('Only the person',answers[-1])

    def test_saved_season_deletion_removes_record_evidence(self):
        from services import season_archive
        saved=season_archive.delete_tournament(self.session,self.tour,keep_stats=True)
        self.session.commit()
        self.assertTrue(self.R.evidence(self.session,self.season))
        season_archive.delete_saved_season(self.session,saved.id)
        self.session.commit()
        self.assertFalse(self.R.evidence(self.session,self.season))

    def test_aggregate_only_legacy_season_is_not_fabricated(self):
        from models import LeagueSeason, StatsSeason, StatsSeasonPlayer
        saved=StatsSeason(name='Legacy',kind='challenge')
        self.session.add(saved);self.session.flush()
        self.session.add(StatsSeasonPlayer(season_id=saved.id,name='Old Player',bat_runs=250,
            bat_balls=100,bat_outs=5,highest_score=100,bowl_wickets=3,bowl_runs=30,bowl_balls=20,
            best_bowl_wickets=2,best_bowl_runs=10))
        legacy=LeagueSeason(series_id=self.series.id,season_no=2,stats_season_id=saved.id)
        self.session.add(legacy);self.session.flush()
        data=self.R.boards(self.session,self.series,legacy)
        run_board=next(b for b in data['boards'] if b['key']=='runs')
        self.assertEqual(250,run_board['holders'][0]['value'])
        self.assertIsNone(run_board['holders'][0]['source'])
        self.assertEqual([],data['matches'])
        self.assertFalse(next(b for b in data['boards'] if b['key']=='hundreds')['holders'])


    def test_qualifications_set_before_tournament_creation(self):
        from models import LeagueSeason
        future = LeagueSeason(series_id=self.series.id,season_no=2,league_id=self.league.id)
        self.session.add(future);self.session.flush()
        self.R.save_config(self.session,future,77,55)
        tour,_ = self.LS.create_season_tournament(self.session,future,hand_over=False)
        self.assertEqual(77,tour.min_balls_for_sr)
        self.assertEqual(55,tour.min_balls_for_econ)

    def test_fastest_milestones_rank_lower_balls_and_ignore_legacy(self):
        a=self.line('Alpha',160,70);b=self.line('Beta',170,75)
        a.update(balls_to_50=14,balls_to_100=29,balls_to_150=50)
        b.update(balls_to_50=14,balls_to_100=28,balls_to_150=51)
        self.game.scorecard_json=json.dumps([a,b,self.line('Legacy',190,80)])
        self.session.flush()
        self.assertEqual(['Alpha','Beta'],[h['name'] for h in self.board('f50')['holders']])
        self.assertEqual('Beta',self.board('f100')['holders'][0]['name'])
        self.assertEqual('Alpha',self.board('f150')['holders'][0]['name'])
        self.R.save_config(self.session,self.season,1000,1000)
        self.assertEqual(14,self.board('f50')['holders'][0]['value'])
        from services import season_archive
        season_archive.delete_tournament(self.session,self.tour,keep_stats=True)
        self.session.commit()
        self.assertEqual(28,self.board('f100')['holders'][0]['value'])

    def test_fastest_live_record_break_tie_and_duplicate(self):
        a=self.line('Alpha',110,45);a.update(balls_to_50=16,balls_to_100=40)
        self.game.scorecard_json=json.dumps([a]);self.session.flush()
        state=self.state();state['bat_xi'][0]['name']='Gamma'
        state['bat_stats']['1'].update(runs=110,balls=42,balls_to_50=14,balls_to_100=40)
        alerts=self.R.collect_alerts(self.session,state)
        self.assertTrue(any(kind=='new_record' and fields['record']=='Fastest fifty' and fields['value']=='14' for kind,text,fields in alerts))
        self.assertTrue(any(kind=='record_equalled' and fields['record']=='Fastest hundred' for kind,text,fields in alerts))
        self.session.commit()
        self.assertFalse(self.R.collect_alerts(self.session,state))

    def test_saved_milestone_balls_do_not_inflate_innings_overs(self):
        from services.tournament_service import record_tournament_match
        state=dict(tournament_id=self.tour.id,match_id=98000+self.tag,
            tournament_tteam_by_user={1:self.a.id,2:self.b.id},
            inn1_bat_team_id=1,inn1_bowl_team_id=2,bat_team_id=2,bowl_team_id=1,
            bat_team_name=self.b.name,bowl_team_name=self.a.name,
            inn1_runs=100,inn1_wickets=0,total_runs=101,total_wickets=0,
            inn1_bat_xi=[dict(roster_id=1,name='First')],
            inn1_bowl_xi=[dict(roster_id=2,name='Second')],
            bat_xi=[dict(roster_id=2,name='Second')],
            bowl_xi=[dict(roster_id=1,name='First')],
            inn1_bat_stats={'1':dict(runs=100,balls=20,balls_to_50=10,balls_to_100=20)},
            inn1_bowl_stats={'2':dict(runs=102,balls=18,wickets=0)},
            bat_stats={'2':dict(runs=101,balls=19,balls_to_50=9,balls_to_100=19)},
            bowl_stats={'1':dict(runs=101,balls=18,wickets=0)})
        match=record_tournament_match(self.session,state)
        self.assertEqual(18,match.inn1_balls)
        self.assertEqual(18,match.inn2_balls)
        first=next(l for l in json.loads(match.scorecard_json) if l['name']=='First')
        self.assertEqual(20,first['balls_to_100'])

    def test_career_profile_and_shareable_card_routes(self):
        response=self.client.get(f'/league-seasons/{self.series.id}/player?player=Alpha')
        self.assertEqual(200,response.status_code)
        self.assertIn(b'Recent form',response.data)
        self.assertIn(b'Personal bests',response.data)
        self.assertEqual(404,self.client.get(f'/league-seasons/{self.series.id}/player?player=Missing').status_code)
        card=self.client.get(f'/league-seasons/{self.series.id}/record-card/hs.png?season={self.season.id}')
        self.assertEqual(200,card.status_code)
        self.assertEqual('image/png',card.mimetype)
        from PIL import Image
        from io import BytesIO
        self.assertEqual((1200,675),Image.open(BytesIO(card.data)).size)
        self.assertEqual(404,self.client.get(f'/league-seasons/{self.series.id}/record-card/fake.png').status_code)

    def test_honours_and_official_best_xi_survive_archive(self):
        names=['Alpha','Beta']+[f'Player {i}' for i in range(3,12)]
        self.game.scorecard_json=json.dumps([self.line(n,30,20) for n in names]);self.session.commit()
        url=f'/league-seasons/{self.series.id}/honours/{self.season.id}'
        response=self.client.get(url)
        self.assertEqual(200,response.status_code)
        self.assertIn(b'Suggested statistical XI',response.data)
        self.client.post(url,data=dict(best_xi='\n'.join(names),criteria='Admin selection with keeper, batting and bowling roles'))
        self.session.expire_all()
        self.assertEqual(names,self.LS.honours(self.season)['best_xi']['players'])
        from services import season_archive
        season_archive.delete_tournament(self.session,self.tour,keep_stats=True);self.session.commit()
        response=self.client.get(url)
        self.assertIn(b'Official Best XI',response.data)
        self.assertIn(b'Player 11',response.data)

    def test_watch_stays_silent_even_when_enabled(self):
        from services import league_record_publications as P
        self.game.scorecard_json = json.dumps([self.line('Alpha',490,220),self.line('Beta',500,230)])
        self.session.flush()
        state = self.state()
        P.save_options(self.session, self.season, watch=True, recap=True)
        self.assertEqual([], P.queue_watch(self.session, state))

    def test_richer_alerts_include_previous_holder_opponent_and_link(self):
        with patch.dict(os.environ,WEBAPP_URL='https://league.example/webapp'):
            alerts=self.R.collect_alerts(self.session,self.state())
        hs=next(a for a in alerts if a[2]['record']=='Highest individual score')
        self.assertIn('by Alpha, Beta',hs[1])
        self.assertIn('Opponent:',hs[1])
        self.assertIn('https://league.example/cricket?match_id=',hs[1])

    def test_recap_idempotency_and_scorecard_correction_edits_original(self):
        from services import league_record_publications as P
        from services.tournament_service import recompute_tournament,delete_tournament_match
        state=self.state();state['match_id']=self.game.match_id
        row=P.queue_recap(self.session,state)
        self.assertEqual(row.id,P.queue_recap(self.session,state).id)
        self.session.commit()
        async def inline(fn,*args,**kw):return fn(*args,**kw)
        context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=432)),edit_message_text=AsyncMock()))
        with patch.object(P.asyncio,'to_thread',side_effect=inline):
            asyncio.run(P.dispatch(context))
        self.session.expire_all();self.assertEqual(432,row.message_id)
        self.game.scorecard_json=json.dumps([self.line('Alpha',35,30),self.line('Beta',90,40)])
        recompute_tournament(self.session,self.tour.id);self.session.commit()
        self.assertEqual('pending',row.status)
        self.assertIn('CORRECTED MATCH RECAP',row.text)
        with patch.object(P.asyncio,'to_thread',side_effect=inline):
            asyncio.run(P.dispatch(context))
        self.assertEqual(1,context.bot.send_message.call_count)
        self.assertEqual(432,context.bot.edit_message_text.call_args.kwargs['message_id'])
        recompute_tournament(self.session,self.tour.id);self.session.commit()
        self.session.expire_all();self.assertEqual('sent',row.status)
        delete_tournament_match(self.session,self.game.id);self.session.commit()
        self.assertIn('RETRACTED',row.text)

    def test_publication_failures_remain_retryable(self):
        from services import league_record_publications as P
        state=self.state();state['match_id']=self.game.match_id
        row=P.queue_recap(self.session,state);self.session.commit()
        async def inline(fn,*args,**kw):return fn(*args,**kw)
        context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('Telegram unavailable'))))
        with patch.object(P.asyncio,'to_thread',side_effect=inline):asyncio.run(P.dispatch(context))
        self.session.expire_all();self.assertEqual('pending',row.status)
        context.bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=100))
        with patch.object(P.asyncio,'to_thread',side_effect=inline):asyncio.run(P.dispatch(context))
        self.session.expire_all();self.assertEqual('sent',row.status)

    def test_deleted_season_cleans_publications_and_options(self):
        from services import league_record_publications as P
        from models import LeagueRecordPublication,LeagueRecordOptions
        sid=self.season.id
        P.save_options(self.session,self.season)
        state=self.state();state['match_id']=self.game.match_id
        P.queue_recap(self.session,state)
        self.LS.detach_season(self.session,self.season);self.session.commit()
        self.assertEqual(0,self.session.query(LeagueRecordPublication).filter_by(season_id=sid).count())
        self.assertIsNone(self.session.get(LeagueRecordOptions,sid))

    def test_shared_default_gif_overrides_legacy_for_every_record_event(self):
        from services import event_media_service as media
        context=SimpleNamespace()
        for event in ('new_record','record_equalled','record_holder'):
            with patch.object(media,'fire_event_media',new_callable=AsyncMock,return_value=True) as fire:
                asyncio.run(media.fire_record_media(context,-100,event,dict(player='<Player>')))
                self.assertEqual(1,fire.call_count)
                self.assertEqual('record_default',fire.call_args.args[2])
                self.assertEqual('&lt;Player&gt;',fire.call_args.kwargs['fields']['player'])

    def test_delivery_boards_survive_archive_and_drop_invalid_corrected_data(self):
        from services import config_service,cipl_match as CM
        from services.tournament_service import _collect_player_lines
        from tests.test_cipl_approach import _make_state
        state=_make_state(20)
        state.update(bat_team_name=self.a.name,bowl_team_name=self.b.name)
        with patch.dict(config_service._CACHE,data=dict(config_service.DEFAULTS)),patch.object(CM,'calculate_outcome',return_value=dict(type='run',runs=4,is_extra=False,batter_out=False)):
            for _ in range(3):
                state['current_bowler']=CM.eligible_bowlers(state)[0]
                state['batting_approach']=state['bowling_approach']='balanced'
                CM.simulate_over(state)
        lines=_collect_player_lines(state)
        self.game.scorecard_json=json.dumps(lines)
        self.game.inn1_runs=72;self.game.inn2_runs=0
        self.session.commit()
        self.assertEqual(72,self.board('partnership')['holders'][0]['value'])
        self.assertEqual(13,self.board('p50')['holders'][0]['value'])
        self.assertTrue(self.board('powerplay_runs')['holders'])
        events=self.client.get(f'{self.url}/{self.season.id}/match/{self.game.id}?download=events')
        self.assertEqual(18,len(events.json))
        lines[0]['bat_runs']+=5;self.game.scorecard_json=json.dumps(lines);self.session.flush()
        self.assertEqual([],self.board('partnership')['holders'])
        lines[0]['bat_runs']-=5;self.game.scorecard_json=json.dumps(lines);self.session.flush()
        from services import season_archive
        season_archive.delete_tournament(self.session,self.tour,keep_stats=True);self.session.commit()
        self.assertEqual(13,self.board('p50')['holders'][0]['value'])

    def test_post_match_file_is_single_durable_document_and_correctable(self):
        from services import league_record_publications as P
        from services.tournament_service import recompute_tournament, delete_tournament_match
        from models import LeagueRecordPublication
        async def inline(fn, *args, **kwargs): return fn(*args, **kwargs)
        bot = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(message_id=60)),
                              edit_message_media=AsyncMock(), send_message=AsyncMock())
        context = SimpleNamespace(bot=bot, bot_data={})
        state = self.state()
        state['match_id'] = self.game.match_id
        with patch.object(P.asyncio, 'to_thread', side_effect=inline):
            asyncio.run(self.R.announce(context, state))
            bot.send_document.assert_not_awaited()
            asyncio.run(self.R.announce(context, state, final=True))
            asyncio.run(self.R.announce(context, state, final=True))
        self.assertEqual(bot.send_document.await_count, 1)
        bot.send_message.assert_not_awaited()
        self.assertTrue(bot.send_document.call_args.kwargs['document'].filename.endswith('.md'))
        row = self.session.query(LeagueRecordPublication).filter_by(match_id=state['match_id'], kind='match_records').one()
        self.assertIn('All record achievements', json.loads(row.metadata_json)['markdown'])
        self.game.scorecard_json = json.dumps([self.line('Alpha',100,60)])
        recompute_tournament(self.session, self.tour.id)
        self.session.commit()
        with patch.object(P.asyncio, 'to_thread', side_effect=inline):
            asyncio.run(P.dispatch(context))
        self.assertEqual(bot.edit_message_media.call_args.kwargs['message_id'], 60)
        self.assertEqual(bot.send_document.await_count, 1)
        delete_tournament_match(self.session, self.game.id)
        self.session.commit()
        self.assertIn('RETRACTED', json.loads(row.metadata_json)['markdown'])

    def test_unfinished_match_is_not_mistaken_for_a_removed_scorecard(self):
        from services import league_record_publications as P
        row=P.queue(self.session,self.season,999999,-100,'record:new_record:example','Live provisional')
        self.session.flush()
        P.reconcile(self.session,self.tour.id)
        self.assertIsNone(row.fingerprint)
        self.assertEqual('Live provisional',row.text)

    def test_qualification_change_queues_amendment_without_scorecard_change(self):
        from services import league_record_publications as P
        state=self.state();state['match_id']=self.game.match_id
        row=P.queue_recap(self.session,state)
        row.message_id=19;row.status='sent';row.delivered_revision=0
        self.session.commit()
        self.R.save_config(self.session,self.season,31,12)
        self.session.commit()
        self.assertEqual('pending',row.status)
        self.assertIn('CORRECTED MATCH RECAP',row.text)
        self.assertEqual([],self.board('sr')['holders'])

    def test_missing_original_message_posts_an_amendment(self):
        from services import league_record_publications as P
        state=self.state();state['match_id']=self.game.match_id
        row=P.queue_recap(self.session,state)
        row.message_id=55;row.revision=1;row.text='CORRECTED MATCH RECAP'
        self.session.commit()
        bot=SimpleNamespace(edit_message_text=AsyncMock(side_effect=RuntimeError('Message to edit not found')),
                            send_message=AsyncMock(return_value=SimpleNamespace(message_id=56)))
        async def inline(fn,*args,**kw):return fn(*args,**kw)
        with patch.object(P.asyncio,'to_thread',side_effect=inline):asyncio.run(P.dispatch(SimpleNamespace(bot=bot)))
        self.session.expire_all()
        self.assertEqual(56,row.message_id)
        self.assertEqual('sent',row.status)

    def test_team_record_alert_uses_the_other_side_as_opponent(self):
        state=self.state();state['total_runs']=200
        alerts=self.R.collect_alerts(self.session,state)
        record=next(a for a in alerts if a[2]['record']=='Highest team total')
        self.assertEqual(self.b.name,record[2]['opponent'])
