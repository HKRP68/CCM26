"""Public portal routes are read-only, scoped, cached, and publication-aware."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_league_seasons_page as pages
setUpModule = pages.setUpModule
tearDownModule = pages.tearDownModule


class PortalRoutesTests(pages.PageCase):
    def setUp(self):
        super().setUp()
        from models import LeagueSeries, LeagueSeason
        from services import cricket_portal_cache
        cricket_portal_cache.invalidate()
        self.series = LeagueSeries(name=f'Portal Cup {self.tag}')
        self.session.add(self.series)
        self.session.flush()
        self.season = LeagueSeason(series_id=self.series.id, season_no=1, tournament_id=self.tour.id)
        self.session.add(self.season)
        self.session.commit()
        with self.client.session_transaction() as state:
            state.clear()
        self.base = f'/api/stats/{self.series.id}'

    def test_portal_response_is_cached_and_read_only(self):
        from services import cricket_portal_competitions as competitions
        with patch.object(competitions, 'portal_snapshot', return_value={'fixtures': [], 'teams': []}) as build:
            first = self.client.get(self.base + '/portal')
            second = self.client.get(self.base + '/portal')
        self.assertEqual(200, first.status_code)
        self.assertEqual(first.json, second.json)
        self.assertEqual(1, build.call_count)
        self.assertEqual('no-store', first.headers['Cache-Control'])
        self.assertEqual(405, self.client.post(self.base + '/portal').status_code)

    def test_profile_names_and_competition_scope_are_validated(self):
        from services import cricket_portal_statistics as statistics
        self.assertEqual(400, self.client.get(self.base + '/player').status_code)
        self.assertEqual(400, self.client.get(self.base + '/team?name=' + 'A'*151).status_code)
        with patch.object(statistics, 'player_profile', return_value=None):
            self.assertEqual(404, self.client.get(self.base + '/player?name=Nobody').status_code)
        self.series.is_active = False
        self.session.commit()
        self.assertEqual(404, self.client.get(self.base + '/portal').status_code)

    def test_explorer_accepts_only_public_search_filters(self):
        from services import cricket_portal_statistics as statistics
        with patch.object(statistics, 'explorer', return_value={'rows': []}) as explore:
            response = self.client.get(self.base + '/explorer?pitch=Green&opponent=Mumbai&user_id=991&secret=hidden')
        self.assertEqual(200, response.status_code)
        filters = explore.call_args.args[-1]
        self.assertEqual({'pitch': 'Green', 'opponent': 'Mumbai'}, filters)

    def test_only_published_articles_are_readable_without_admin_metadata_or_writes(self):
        from models import NewsArticle
        published = NewsArticle(headline='Final victory', body='The recorded champions celebrate.',
            status='published', author_name='Reporter', author_user_id=123, author_telegram_id=987,
            reviewed_by='private-admin', review_note='private-note', view_count=7)
        pending = NewsArticle(headline='Pending draft', body='Unapproved.', status='pending')
        self.session.add_all([published, pending])
        self.session.commit()
        response = self.client.get(f'/api/stats/news/{published.id}')
        self.assertEqual(200, response.status_code)
        self.assertEqual('Final victory', response.json['title'])
        self.assertNotIn('author_user_id', response.json)
        self.assertNotIn('author_telegram_id', response.json)
        self.assertNotIn('private-note', response.text)
        self.assertEqual(404, self.client.get(f'/api/stats/news/{pending.id}').status_code)
        self.session.refresh(published)
        self.assertEqual(7, published.view_count)
