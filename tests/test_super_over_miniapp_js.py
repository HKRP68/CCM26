"""Frontend regression tests for the Mini App Super Over render + charts.

The View Match renderers (``buildSuperOverHtml`` and ``buildCumulativeChart``)
are written as pure functions fenced by ``// ==…START==`` / ``==END==`` markers
in static/cricket/app.js so they can be evaluated in isolation under Node with
no DOM.  These tests slice those sections out, run them, and assert the HTML
contract: collapsible innings, ball-by-ball commentary, escaping of
user-controlled content, the "Commentary unavailable" fallback for totals-only
archives, turning-point badges, and the cumulative-score comparison chart.
"""

import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "static", "cricket", "app.js")


def _section(src, name):
    m = re.search(r"// ==%s_START==(.*?)// ==%s_END==" % (name, name),
                  src, re.S)
    if not m:
        raise AssertionError("marker %s not found in app.js" % name)
    return m.group(1)


def _run_node(body):
    with open(APP_JS, encoding="utf-8") as f:
        src = f.read()
    prelude = _section(src, "SUPER_OVER_RENDER") + _section(src, "CUMULATIVE_CHART")
    script = prelude + "\n" + body
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError("node failed:\n" + out.stderr)
    return out.stdout.strip()


@unittest.skipUnless(shutil.which("node"), "Node is needed for the Mini App render")
class SuperOverRenderJsTests(unittest.TestCase):

    def _sample(self):
        return """
        const so = {winner:'Tamil Dragon', marginText:'won by 1 run',
          turningPoints:[{kind:'six',ball:'0.3',team:'Vindhya',score:'10/0',text:'Massive hit'},
                         {kind:'wicket',ball:'0.4',team:'Vindhya',score:'10/1',text:'Gone!'},
                         {kind:'winning_boundary',ball:'0.6',team:'Tamil Dragon',score:'16/1',text:'Winning runs'}],
          innings:[
            {label:'Super Over Innings 1',round:1,team:'Vindhya Imperials',runs:15,wickets:1,overs:'1.0',
             batting:[{name:'<b>Striker</b>',runs:15,balls:5,fours:1,sixes:1,out:true,how_out:'c & b'}],
             bowling:[{name:'Bowler X',overs:'1.0',runs:15,wickets:1}],
             deliveries:[{index:1,ball:'0.1',legal:true,runs:1,outcome:'run',commentary:'Tapped for a single.'},
                         {index:2,ball:'0.2',legal:true,runs:4,outcome:'run',commentary:'FOUR! Driven through cover.'},
                         {index:3,ball:'0.3',legal:true,runs:6,outcome:'run',commentary:'SIX!'},
                         {index:4,ball:'0.3',legal:false,runs:0,extra:'Wide',commentary:'Wide down leg.'},
                         {index:5,ball:'0.4',legal:true,runs:0,wicket:true,wicketType:'Bowled',commentary:'WICKET!'}]},
            {label:'Super Over Innings 2',round:1,team:'Tamil Dragon',runs:16,wickets:1,overs:'1.0',deliveries:[]}
          ]};
        """

    def _assert_true_lines(self, out):
        for line in out.splitlines():
            key, _, val = line.partition("=")
            self.assertEqual(val, "true", "%s was %r" % (key, val))

    def test_render_contract(self):
        out = _run_node(self._sample() + r"""
        const h = buildSuperOverHtml(so);
        console.log('round=' + h.includes('ROUND 1'));
        console.log('collapsible=' + h.includes('<details class="so-innings"'));
        console.log('ball_commentary=' + h.includes('Tapped for a single.'));
        console.log('four=' + h.includes('FOUR! Driven through cover.'));
        console.log('batting_table=' + h.includes('so-card-table'));
        console.log('bowling_name=' + h.includes('Bowler X'));
        // wide keeps its own entry and the re-bowled ball shares the 0.3 label
        console.log('wide_repeated_label=' + ((h.match(/so-ball-num">0\.3</g)||[]).length >= 2));
        console.log('unavailable_fallback=' + h.includes('Commentary unavailable'));
        console.log('winner_line=' + h.includes('🏆 Tamil Dragon won the match won by 1 run'));
        console.log('turn_six=' + h.includes('🔥 Massive hit'));
        console.log('turn_wicket=' + h.includes('🎯 Gone!'));
        console.log('turn_winning=' + h.includes('🏆 Winning runs'));
        """)
        self._assert_true_lines(out)

    def test_escapes_user_content(self):
        out = _run_node(self._sample() + r"""
        const h = buildSuperOverHtml(so);
        console.log('no_raw_tag=' + !h.includes('<b>Striker</b>'));
        console.log('escaped=' + h.includes('&lt;b&gt;Striker&lt;/b&gt;'));
        """)
        self._assert_true_lines(out)

    def test_xss_in_team_name_escaped(self):
        out = _run_node(r"""
        const so = {winner:"<img src=x onerror=alert(1)>", innings:[
          {round:1,team:"<script>bad</script>",runs:5,wickets:0,deliveries:[]}]};
        const h = buildSuperOverHtml(so);
        console.log('no_script=' + !h.includes('<script>bad'));
        console.log('no_img=' + !h.includes('<img src=x'));
        console.log('escaped_team=' + h.includes('&lt;script&gt;bad'));
        """)
        self._assert_true_lines(out)

    def test_empty_super_over_is_blank(self):
        out = _run_node(r"""
        console.log('empty=' + (buildSuperOverHtml({innings:[]}) === ''));
        console.log('null=' + (buildSuperOverHtml(null) === ''));
        """)
        self._assert_true_lines(out)


@unittest.skipUnless(shutil.which("node"), "Node is needed for the Mini App render")
class CumulativeChartJsTests(unittest.TestCase):

    def test_cumulative_series(self):
        out = _run_node(
            "console.log('s=' + JSON.stringify(cumulativeSeries([10,8,12])));")
        self.assertEqual(out, "s=[10,18,30]")

    def test_two_curves_and_totals(self):
        out = _run_node(r"""
        const c = buildCumulativeChart([10,8,12],[9,11],'Vindhya','Tamil Dragon','overs');
        console.log('two_lines=' + ((c.match(/polyline/g)||[]).length === 2));
        console.log('inn1_total=' + c.includes('30'));
        console.log('inn2_total=' + c.includes('20'));
        console.log('unit=' + c.includes('by overs'));
        console.log('svg=' + c.includes('<svg'));
        """)
        for line in out.splitlines():
            key, _, val = line.partition("=")
            self.assertEqual(val, "true", "%s was %r" % (key, val))

    def test_the_hundred_unit_label(self):
        out = _run_node(
            "console.log('u=' + buildCumulativeChart([5,6],[4],'A','B','sets')"
            ".includes('by sets'));")
        self.assertEqual(out, "u=true")

    def test_empty_when_no_overs(self):
        out = _run_node(
            "console.log('e=' + (buildCumulativeChart([],[],'A','B','overs') === ''));")
        self.assertEqual(out, "e=true")

    def test_single_innings_only(self):
        # First innings in progress: only inn1 curve, no crash.
        out = _run_node(r"""
        const c = buildCumulativeChart([10,8,12],[],'Vindhya','Tamil Dragon','overs');
        console.log('one_line=' + ((c.match(/polyline/g)||[]).length === 1));
        """)
        self.assertEqual(out, "one_line=true")


if __name__ == "__main__":
    unittest.main()
