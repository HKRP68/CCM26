"""Render special summaries from the user's original HTML, without a redraw."""

import base64
import io
import json
import logging
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from services import match_summary_card as base

logger = logging.getLogger(__name__)
WIDTH, HEIGHT = 1536, 864
WHITE, BLACK = (255, 255, 255), (0, 0, 0)
ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates/graphics/CMU_S5_Match_Summary.html"
# One browser at a time, on its own thread even when a caller has an event loop.
_RENDER_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="summary-html")


def relative_luminance(colour):
    def linear(channel):
        value = channel / 255
        return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
    return sum(weight * linear(channel) for weight, channel in
               zip((.2126, .7152, .0722), colour))


def contrast_ratio(first, second):
    light, dark = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (light + .05) / (dark + .05)


def readable_text(colour):
    return max((BLACK, WHITE), key=lambda ink: contrast_ratio(colour, ink))


def _hex(colour):
    return "#" + "".join(f"{channel:02x}" for channel in colour)


def header_palette(colour):
    """Keep the HTML's metallic gradient within the selected text's contrast range."""
    ink = readable_text(colour)
    def shaded(factor):
        stop = tuple(round(channel * factor) for channel in colour)
        for _ in range(9):
            if contrast_ratio(stop, ink) >= 4.5:
                return _hex(stop)
            stop = tuple(round((a + b) / 2) for a, b in zip(stop, colour))
        return _hex(colour)
    return {"team": _hex(colour), "light": _hex(base._mix(colour, WHITE, .45)),
            "header-top": shaded(.62), "header-mid": _hex(colour),
            "header-bottom": shaded(.45), "on-team": _hex(ink),
            "team-shadow": "0 2px 3px #000" if ink == WHITE else "none"}


@lru_cache(maxsize=1)
def original_html():
    """The attached HTML is stored byte-for-byte, including its embedded artwork."""
    return TEMPLATE.read_text(encoding="utf-8")


def _image_uri(blob):
    image = base._open_crest(blob, (220, 130))
    if image is None:
        return None
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _rows(rows, potm_name, bowling=False):
    normalise = base._normalise_bowlers if bowling else base._normalise_batters
    output = []
    for name, value, extra, impact, _not_out in normalise(rows):
        label = str(name)
        if impact and label.lower().endswith(base.IMPACT_SUFFIX):
            label = label[:-len(base.IMPACT_SUFFIX)].rstrip()
        output.append(dict(name=label.upper(), value=value.replace("-", "/") if bowling else value,
                           extra=f"({extra})" if extra != "—" else "—",
                           impact=impact, potm=base._same_player(name, potm_name)))
    return output


def _view_data(data):
    innings = []
    for number, fallback in ((1, (0, 127, 75)), (2, (0, 207, 217))):
        prefix = f"inn{number}"
        details = (data.get("top_per_team") or {}).get(prefix) or {}
        team = details.get("team") or data.get(f"{prefix}_team") or "Team"
        runs, wickets = data.get(f"{prefix}_runs"), data.get(f"{prefix}_wickets")
        innings.append(dict(
            team=str(team).upper(), overs=str(data.get(f"{prefix}_overs") or "0.0"),
            score=data.get(f"{prefix}_score_text") or
                  f"{wickets if wickets is not None else '—'}/{runs if runs is not None else '—'}",
            palette=header_palette(base._hex_to_rgb(data.get(f"{prefix}_color"), fallback)),
            logo=_image_uri(data.get(f"{prefix}_logo_png")), initials=base._initials(team),
            batters=_rows(details.get("batters"), data.get("potm_name")),
            bowlers=_rows(details.get("bowlers"), data.get("potm_name"), bowling=True)))
    pitch = data.get("pitch_type")
    for chip in data.get("conditions") or []:
        if isinstance(chip, (list, tuple)) and len(chip) >= 3 and str(chip[0]).lower() == "pitch":
            pitch = chip[2]
    metrics = base._potm_metrics(
        data.get("potm_stats"), data.get("potm_runs"), data.get("potm_balls"),
        data.get("potm_fours"), data.get("potm_sixes"), data.get("potm_sr"),
        wickets=data.get("potm_wickets"), conceded=data.get("potm_conceded"),
        overs=data.get("potm_overs"), economy=data.get("potm_economy"), dots=data.get("potm_dots"))
    figures = " · ".join(str(value) for value, _label in metrics[:2] if value != "—") or "—"
    margin = str(data.get("win_margin_text") or "").strip()
    margin = re.sub(r"^won\s+by\b", "WIN BY", margin, flags=re.I)
    if margin.lower().startswith("by "):
        margin = "WIN " + margin
    return dict(innings=innings, tournament=str(data.get("tournament_name") or "TOURNAMENT"),
                brand=str(data.get("header_right") or ("PREVIEW" if data.get("preview") else "CMU")),
                match=f"MATCH {data['match_no']}" if data.get("match_no") is not None else "MATCH SUMMARY",
                stadium=str(data.get("stadium") or "—"), pitch=str(pitch or "—").upper(),
                overs_label="BALLS" if data.get("is_hundred") else "OVERS",
                winner=str(data.get("winner_name") or "MATCH COMPLETE").upper(),
                margin=margin.upper(), potm=str(data.get("potm_name") or "NOT AWARDED").upper(),
                figures=figures)


def build_tournament_summary_html(data=None, *, editable=False):
    """Bind match values to the original DOM; no data returns the original file."""
    source = original_html()
    if data is None:
        return source
    payload = json.dumps(_view_data(data), ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    script = (ROOT / "static/js/tournament-summary-bindings.js").read_text(encoding="utf-8")
    style = (ROOT / "static/css/tournament-summary-bindings.css").read_text(encoding="utf-8")
    injection = (f'<style>{style}</style><script type="application/json" id="cmu-match-data">'
                 f'{payload}</script><script>window.CMU_SUMMARY_EDITABLE={str(editable).lower()};'
                 f'{script}</script>')
    return source.replace("</body>", injection + "</body>")


def _screenshot(html):
    from playwright.sync_api import sync_playwright
    executable = os.environ.get("SCORECARD_CHROMIUM_EXECUTABLE") or shutil.which("chromium") or shutil.which("google-chrome")
    with sync_playwright() as engine:
        browser = engine.chromium.launch(
            executable_path=executable, headless=True, timeout=15000,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT},
                                          device_scale_factor=1, offline=True)
            page = context.new_page()
            page.set_default_timeout(15000)
            page.set_content(html, wait_until="load")
            page.evaluate("document.fonts.ready")
            return page.locator("#viewport").screenshot(type="png", animations="disabled")
        finally:
            browser.close()


def generate_tournament_summary(data):
    """Export the actual HTML to PNG, including its original CSS and artwork."""
    try:
        return _RENDER_POOL.submit(_screenshot, build_tournament_summary_html(data)).result(timeout=40)
    except Exception:
        logger.exception("Original HTML summary export failed; install Chromium or set SCORECARD_CHROMIUM_EXECUTABLE")
        return None
def preview_payload(teams=(), *, tournament_name="CMU · SEASON 5"):
    """Explicitly labelled sample data for the admin preview and design tools."""
    a = teams[0].name if teams else "Melbourne Stars"
    b = teams[1].name if len(teams) > 1 else "Brisbane Heat"
    def batter(name, runs, balls, out=True, **extra):
        return dict(name=name, runs=runs, balls=balls, out=out, **extra)
    def bowler(name, wickets, runs, overs="4.0"):
        return dict(name=name, wickets=wickets, runs=runs, overs=overs)
    return dict(
        inn1_team=a, inn1_runs=195, inn1_wickets=6, inn1_overs="20.0",
        inn2_team=b, inn2_runs=199, inn2_wickets=6, inn2_overs="19.4",
        winner_name=b, win_margin_text="by 4 wickets", overs_total=20,
        match_no=20, stadium="MCG, Melbourne", tournament_name=tournament_name,
        potm_name="Max Bryant", potm_team=b, potm_stats="48* (26)",
        potm_runs=48, potm_balls=26, potm_fours=4, potm_sixes=2,
        inn1_color="#007f4b", inn2_color="#00cfd9", special_graphics=True,
        conditions=[("pitch", "Pitch", "Hard")],
        top_per_team={
            "inn1": {"batters": [batter("Marcus Stoinis", 43, 35),
                        batter("Macdonald", 37, 12, False),
                        batter("Sam Harper", 37, 23), batter("Kellaway", 29, 14)],
                     "bowlers": [bowler("Balkin", 2, 35), bowler("Renshaw", 1, 13),
                        bowler("Kuhnemann", 1, 22), bowler("Patterson", 1, 41)]},
            "inn2": {"batters": [batter("Max Bryant", 48, 26, False),
                        batter("McSweeney", 43, 31),
                        batter("Matt Renshaw", 41, 27, impact=True),
                        batter("Xavier Bartlett", 21, 9, False)],
                     "bowlers": [bowler("Mitchell Swepson", 2, 30),
                        bowler("Peter Siddle", 2, 38), bowler("Haris Rauf", 1, 41),
                        bowler("Tom Curran", 1, 47, "3.4")]}})
