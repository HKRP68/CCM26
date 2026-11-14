"""Theme saves, previews, replay caches, and readable festival scorecards."""

import io
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image, ImageChops

from services import match_summary_card as summary
from services.scorecard_theme_catalog import THEMES, THEME_BY_KEY, NORMAL, SPACIAL, SPACIAL_TYPE2, HTML_SUMMARY_THEMES, theme_from_config


def _summary_payload():
    return dict(
        inn1_team="VIRAT_KOHLI786", inn1_runs=144, inn1_wickets=9, inn1_overs="20.0",
        inn2_team="CHOCKU0611", inn2_runs=149, inn2_wickets=1, inn2_overs="12.2",
        winner_name="CHOCKU0611", win_margin_text="by 9 wickets", overs_total=20,
        stadium="Sydney Cricket Ground", match_no=19905,
        inn1_color="#0065b3", inn2_color="#ed6500",
        potm_name="Sanju Samson", potm_team="CHOCKU0611", potm_stats="98*(44)",
        potm_runs=98, potm_balls=44, potm_sr=222.7,
        top_per_team={
            "inn1": {
                "batters": [dict(name=name, runs=runs, balls=balls, out=True)
                            for name, runs, balls in (("Rohit Sharma", 56, 34),
                                                      ("Ishan Kishan", 38, 23),
                                                      ("Marcus Stoinis", 20, 16),
                                                      ("Hardik Pandya", 7, 3))],
                "bowlers": [dict(name=name, wickets=w, runs=r, overs=ov)
                            for name, w, r, ov in (("Ravindra Jadeja", 4, 34, "4.0"),
                                                  ("Bhuvneshwar Kumar", 3, 12, "3.0"),
                                                  ("Mitchell Starc", 1, 14, "4.0"),
                                                  ("Mohammed Shami", 1, 35, "4.0"))],
            },
            "inn2": {
                "batters": [dict(name="Sanju Samson", runs=98, balls=44, out=False),
                            dict(name="Dewald Brevis", runs=31, balls=16, out=False),
                            dict(name="Virat Kohli", runs=17, balls=15, out=True)],
                "bowlers": [dict(name=name, wickets=w, runs=r, overs=ov)
                            for name, w, r, ov in (("Mohammed Shami", 1, 21, "3.0"),
                                                  ("Hardik Pandya", 0, 18, "1.0"),
                                                  ("Marcus Stoinis", 0, 16, "1.0"),
                                                  ("Varun Kohli", 0, 16, "1.0"))],
            },
        })


def _image(data):
    assert data and data.startswith(b"\x89PNG")
    return Image.open(io.BytesIO(data)).convert("RGB")


def _selected_theme(html):
    inputs = re.findall(r'<input[^>]*name="scorecard_theme"[^>]*>', html)
    selected = [tag for tag in inputs if "checked" in tag]
    assert len(inputs) == len(THEMES)
    assert len(selected) == 1
    return re.search(r'value="([^"]+)"', selected[0]).group(1)


@pytest.fixture(scope="module")
def summary_images():
    payload = _summary_payload()
    normal = summary.generate_match_summary(**payload)
    diwali = summary.generate_match_summary(**payload, theme="diwali")
    return payload, normal, diwali


def test_diwali_preserves_the_scores_rows_and_team_colours(summary_images):
    _, normal_png, diwali_png = summary_images
    normal, diwali = _image(normal_png), _image(diwali_png)
    assert normal.width == diwali.width
    assert diwali.height > normal.height  # lamps have their own space
    assert diwali.getpixel((800, 180)) != normal.getpixel((800, 180))
    for y in (summary.INN1_Y, summary.INN2_Y):
        # The actual data and the team's colour bar are identical inside the
        # gold frame; decorative artwork cannot paint over the cricket.
        region = (summary.BAR_NAME_X, y + 8, summary.CONTENT_R - 12,
                  y + summary.INN_H - 12)
        assert ImageChops.difference(normal.crop(region), diwali.crop(region)).getbbox() is None


def test_turning_off_restores_the_normal_pixels_without_leaking_palette(summary_images):
    payload, normal_png, _ = summary_images
    assert summary.generate_match_summary(**payload, theme="normal") == normal_png
    assert summary.generate_match_summary(**payload, theme="unknown") == normal_png


def test_spacial_renders_the_original_html_without_a_tournament_switch():
    from services.tournament_summary_card import preview_payload
    payload = preview_payload()
    payload.pop("special_graphics")
    image = _image(summary.generate_match_summary(**payload, theme=SPACIAL))
    assert image.size == (1536, 864)


@pytest.mark.parametrize("theme", [t.key for t in THEMES if t.key != NORMAL and t.key not in HTML_SUMMARY_THEMES])
def test_each_theme_preserves_live_table_values_and_has_its_own_art(theme, summary_images):
    payload, normal_png, _ = summary_images
    image = _image(summary.generate_match_summary(**payload, theme=theme))
    normal = _image(normal_png)
    assert image.height > normal.height
    for y in (summary.INN1_Y, summary.INN2_Y):
        region = (summary.NAME_L_X - 12, y + summary.BAR_H + summary.HEAD_ROW_H + 3,
                  summary.CONTENT_R - 12, y + summary.INN_H - 12)
        assert ImageChops.difference(normal.crop(region), image.crop(region)).getbbox() is None
    assert image.crop((300, 0, 1250, 180)).tobytes() != normal.crop((300, 0, 1250, 180)).tobytes()


def test_artwork_is_bundled_and_preview_images_are_available():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "static" / "scorecard_themes"
    for theme in THEMES:
        with Image.open(root / f"{theme.key}-thumb.webp") as image:
            image.verify()
        with Image.open(root / f"{theme.key}.webp") as image:
            assert image.width >= 1536 and image.height >= 1024
            image.verify()


def test_missing_artwork_still_produces_a_readable_scorecard():
    with patch("services.scorecard_themes._artwork", side_effect=OSError("missing artwork")):
        assert _image(summary.generate_match_summary(**_summary_payload(), theme="winter"))


def test_legacy_setting_is_only_a_fallback_to_the_single_selection():
    assert theme_from_config({"scorecard_diwali_theme": True}) == "diwali"
    for theme in THEME_BY_KEY:
        assert theme_from_config({"scorecard_theme": theme, "scorecard_diwali_theme": True}) == theme
    assert theme_from_config({"scorecard_theme": "winter,christmas", "scorecard_diwali_theme": True}) == NORMAL


def test_festival_card_supports_conditions_and_missing_artwork():
    data = summary.generate_match_summary(
        **_summary_payload(), theme="diwali", inn1_logo_png=b"invalid crest",
        potm_card_png=b"invalid card", conditions=[("pitch", "Pitch", "Flat")])
    image = _image(data)
    assert image.height > summary.CANVAS_H + summary.STRIP_H


@pytest.mark.parametrize("theme", [t.key for t in THEMES if t.key != NORMAL])
def test_test_match_theme_renders_all_four_innings(theme):
    from services.test_summary_card import generate_test_summary
    kwargs = dict(
        innings=[dict(team="Alpha" if i % 2 == 0 else "Beta", runs=200 + i,
                      wickets=10, overs="60.0", score_text=str(200 + i),
                      meta_text=f"INNINGS {i + 1}") for i in range(4)],
        winner_name="Beta", win_margin_text="by 2 wickets",
        days=[dict(label="DAY 5", line="Beta won", final=True)],
        conditions=[("pitch", "Pitch", "Dry")])
    normal = _image(generate_test_summary(**kwargs))
    festive = _image(generate_test_summary(**kwargs, theme=theme))
    assert festive.width == normal.width
    assert festive.height > normal.height


@pytest.fixture
def panel(monkeypatch):
    import admin
    import database
    import models
    from services import config_service
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    models.GameConfig.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(admin, "get_session", factory)
    monkeypatch.setattr(database, "get_session", factory)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(config_service, "_CACHE", {"data": None, "loaded_at": None})
    monkeypatch.setitem(admin.app.config, "TESTING", True)
    monkeypatch.setitem(admin.app.config, "WTF_CSRF_ENABLED", False)
    client = admin.app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True
    yield SimpleNamespace(client=client, config=config_service, models=models,
                          sessions=factory, engine=engine, database=database)
    engine.dispose()


def _form(**extra):
    return dict(scorecard_color_inn1="#0065b3", scorecard_color_inn2="#ed6500",
                scorecard_potm_card="on", scorecard_potm_card_inline="on", **extra)


def _cached_card(panel):
    with panel.sessions() as session:
        panel.config.save_config(session, {"scorecard_diwali_theme": False})
        session.add(panel.models.MatchScorecardImage(
            match_id=17, card_type="summary", innings=0, payload_json='{"runs":144}',
            caption="Match 17", delivered=True, file_id="normal-photo"))
        session.commit()


def test_switch_is_saved_and_replay_uses_the_selected_theme(panel):
    assert panel.config.get_scorecard_theme() == "normal"
    _cached_card(panel)
    response = panel.client.post("/settings/scorecard", data=_form(scorecard_theme="diwali"))
    assert response.status_code == 302
    assert panel.config.get_scorecard_theme() == "diwali"
    page = panel.client.get("/settings/scorecard")
    assert page.status_code == 200
    assert _selected_theme(page.text) == "diwali"
    with panel.sessions() as session:
        card = session.query(panel.models.MatchScorecardImage).one()
        assert card.file_id is None
        assert card.delivered and card.caption == "Match 17"
        assert card.payload_json == '{"runs":144}'
        card.file_id = "diwali-photo"
        session.commit()

    assert panel.client.post("/settings/scorecard", data=_form(scorecard_theme="normal")).status_code == 302
    assert panel.config.get_scorecard_theme() == "normal"
    with panel.sessions() as session:
        assert session.query(panel.models.MatchScorecardImage).one().file_id is None
        settings = session.query(panel.models.GameConfig).one()
        assert settings.scorecard_color_inn1 == "#0065b3"
        assert settings.scorecard_color_inn2 == "#ed6500"
        assert settings.scorecard_potm_card_inline is True


def test_resaving_the_same_theme_keeps_cached_images_and_rollback_is_atomic(panel):
    _cached_card(panel)
    with panel.sessions() as session:
        panel.config.save_config(session, {"scorecard_diwali_theme": False})
        session.commit()
        assert session.query(panel.models.MatchScorecardImage).one().file_id == "normal-photo"
        panel.config.save_config(session, {"scorecard_diwali_theme": True})
        session.rollback()
    assert panel.config.get_scorecard_theme() == "normal"
    with panel.sessions() as session:
        assert session.query(panel.models.MatchScorecardImage).one().file_id == "normal-photo"


def test_spacial_is_named_saved_previewed_and_invalidates_old_images(panel):
    _cached_card(panel)
    response = panel.client.post("/settings/scorecard", data=_form(scorecard_theme=SPACIAL))
    assert response.status_code == 302
    assert panel.config.get_scorecard_theme() == SPACIAL
    page = panel.client.get("/settings/scorecard")
    assert _selected_theme(page.text) == SPACIAL
    assert '<strong>Spacial</strong>' in page.text
    assert 'spacial-thumb.webp' in page.text
    with panel.sessions() as session:
        assert session.query(panel.models.MatchScorecardImage).one().file_id is None
    preview = panel.client.get("/settings/scorecard/preview?card_type=summary&theme=spacial")
    assert preview.status_code == 200
    assert _image(preview.data).size == (1536, 864)


def test_switching_between_festivals_keeps_exactly_one_active_and_redraws_replays(panel):
    _cached_card(panel)
    for theme in [t.key for t in THEMES if t.key != NORMAL] + [NORMAL]:
        with panel.sessions() as session:
            archived = session.query(panel.models.MatchScorecardImage).one()
            archived.file_id = "previous-theme-photo"
            session.commit()
        response = panel.client.post("/settings/scorecard", data=_form(scorecard_theme=theme))
        assert response.status_code == 302
        assert panel.config.get_scorecard_theme() == theme
        with panel.sessions() as session:
            row = session.query(panel.models.GameConfig).one()
            assert row.scorecard_theme == theme
            assert row.scorecard_diwali_theme is (theme == "diwali")
            assert session.query(panel.models.MatchScorecardImage).one().file_id is None
        assert _selected_theme(panel.client.get("/settings/scorecard").text) == theme


@pytest.mark.parametrize("selection", ["not-a-theme", "winter,christmas", '["winter","christmas"]'])
def test_invalid_theme_does_not_replace_the_active_theme(panel, selection):
    with panel.sessions() as session:
        panel.config.save_config(session, {"scorecard_theme": "winter"})
        session.commit()
    response = panel.client.post("/settings/scorecard", data=_form(scorecard_theme=selection))
    assert response.status_code == 200
    assert "Choose one valid scorecard theme" in response.text
    assert panel.config.get_scorecard_theme() == "winter"


def test_multiple_selections_are_rejected_without_changing_settings(panel):
    from werkzeug.datastructures import MultiDict
    with panel.sessions() as session:
        panel.config.save_config(session, {"scorecard_theme": "diwali"})
        session.commit()
    form = MultiDict(list(_form().items()) + [("scorecard_theme", "winter"),
                                              ("scorecard_theme", "christmas")])
    response = panel.client.post("/settings/scorecard", data=form)
    assert response.status_code == 200
    assert "Only one scorecard theme" in response.text
    assert panel.config.get_scorecard_theme() == "diwali"


def test_other_processes_pick_up_saves_without_restarting(panel):
    assert panel.config.get_config()["scorecard_theme"] is None
    with panel.sessions() as session:
        # Bypass the cache invalidation, as a different website process would.
        session.add(panel.models.GameConfig(scorecard_theme="winter"))
        session.commit()
    assert panel.config.get_config()["scorecard_theme"] is None
    page = panel.client.get("/settings/scorecard")
    assert page.status_code == 200
    assert _selected_theme(page.text) == "winter"
    assert panel.config.get_scorecard_theme() == "winter"


@pytest.mark.parametrize("card_type", ["batting", "bowling", "summary"])
def test_live_preview_can_turn_off_a_saved_theme_without_saving(panel, card_type):
    with panel.sessions() as session:
        panel.config.save_config(session, {"scorecard_theme": "winter"})
        session.commit()
    url = f"/settings/scorecard/preview?card_type={card_type}"
    normal = panel.client.get(url + "&theme=normal")
    festive = panel.client.get(url)
    assert normal.status_code == festive.status_code == 200
    assert festive.mimetype == "image/png"
    assert _image(festive.data).height > _image(normal.data).height
    assert panel.config.get_scorecard_theme() == "winter"


def test_summary_preview_uses_the_colour_controls_in_both_themes(panel):
    with patch("services.match_summary_card.generate_match_summary", return_value=b"preview") as render:
        for theme in THEME_BY_KEY:
            response = panel.client.get("/settings/scorecard/preview", query_string={
                "card_type": "summary", "theme": theme,
                "scorecard_color_inn1": "#0088aa", "scorecard_color_inn2": "#dd9900"})
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert render.call_args.kwargs["theme"] == theme
            assert render.call_args.kwargs["inn1_color"] == "#0088aa"
            assert render.call_args.kwargs["inn2_color"] == "#dd9900"
    assert panel.config.get_scorecard_theme() == "normal"


@pytest.mark.parametrize("card_type", ["batting", "bowling", "summary"])
@pytest.mark.parametrize("theme", [t.key for t in THEMES if t.key != NORMAL])
def test_stored_payload_theme_is_overridden_by_current_settings(panel, card_type, theme):
    from services import scorecard_delivery
    with panel.sessions() as session:
        panel.config.save_config(session, {"scorecard_theme": theme})
        session.commit()
    path = ("services.match_summary_card.generate_match_summary" if card_type == "summary"
            else f"services.scorecard_card.generate_{card_type}_scorecard")
    payload = {"theme": "normal"}
    with patch(path, return_value=b"rendered") as render:
        assert scorecard_delivery.render_card(card_type, payload) == b"rendered"
    assert render.call_args.kwargs["theme"] == theme
    assert payload == {"theme": "normal"}


@pytest.mark.parametrize("legacy_on", [False, True])
def test_existing_databases_keep_their_theme_during_migration(panel, monkeypatch, legacy_on):
    with panel.sessions() as session:
        panel.config.save_config(session, {"scorecard_diwali_theme": legacy_on})
        session.commit()
    with panel.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE game_config DROP COLUMN scorecard_theme")
    monkeypatch.setattr(panel.database, "engine", panel.engine)
    monkeypatch.setattr(panel.database, "_is_postgres", False)
    panel.database._migrate_add_columns()
    panel.database._migrate_add_columns()  # warm starts must be safe too
    assert panel.config.get_scorecard_theme() == ("diwali" if legacy_on else "normal")


def test_toggle_refreshes_every_preview_and_full_size_link():
    import shutil
    import subprocess
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("Node is required for the preview controls")
    template = Path(__file__).resolve().parents[1] / "templates" / "admin_scorecard_settings.html"
    script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8').split('<script>')[1].split('</script>')[0];
const handlers = {}, elements = {
  scorecard_theme_state: {textContent:''}, color_inn1:{value:'#0065b3'}, color_inn2:{value:'#ed6500'}
};
const choices = [['normal','Normal'],['spacial','Spacial'],['spacial_type2','Spacial Type 2'],['diwali','Diwali'],['winter','Winter'],['christmas','Christmas'],
 ['new_year','New Year'],['makar_sankranti','Makar Sankranti'],['republic_day','Republic Day']];
const radios = choices.map(([value,label])=>({value,checked:value==='normal',dataset:{themeLabel:label},
 addEventListener:(event, callback)=>handlers[event]=callback}));
const ids = ['batting_inn1','bowling_inn1','batting_inn2','bowling_inn2','summary_inn1'];
ids.forEach(id => {
  elements['preview_'+id] = {dataset:{baseSrc:'/settings/scorecard/preview?card_type='+id.split('_')[0]}};
  elements['preview_link_'+id] = {};
});
const context = vm.createContext({ URL, URLSearchParams, Date, setTimeout:callback=>callback(), clearTimeout(){},
  window:{location:{origin:'http://scorecards.test'}}, document:{getElementById:id=>elements[id],
    querySelector:()=>radios.find(r=>r.checked),
    querySelectorAll:selector=>selector.includes('input[name="scorecard_theme"]') ? radios : []}
});
vm.runInContext(source, context);
for (const [theme,label] of [...choices,choices[0]]) {
  radios.forEach(r=>r.checked=r.value===theme);
  handlers.change();
  ids.forEach(id => {
    const preview = elements['preview_'+id], url = new URL(preview.src, 'http://scorecards.test');
    assert.equal(url.searchParams.get('theme'), theme);
    assert.equal(url.searchParams.getAll('theme').length, 1);
    assert.equal(url.searchParams.get('scorecard_color_inn1'), '#0065b3');
    assert.equal(url.searchParams.get('scorecard_color_inn2'), '#ed6500');
    assert.equal(elements['preview_link_'+id].href, preview.src);
  });
  assert.equal(elements.scorecard_theme_state.textContent, 'Selected: '+label);
}
'''
    result = subprocess.run(["node", "-e", script, str(template)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
