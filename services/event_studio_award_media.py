"""Recoverable award artwork generation for finalized honour snapshots."""

from base64 import b64encode
from datetime import datetime, timedelta
import logging

from sqlalchemy import select, update

from database import get_session
from models_events import StudioAwardMedia, StudioCompetition, StudioEvent, StudioHonourSnapshot
from services.tournament_ceremony import compose_award_photo


logger = logging.getLogger(__name__)
LEASE_MINUTES = 5
MAX_ATTEMPTS = 5
LABELS = {"runs": "Orange Cap", "wickets": "Purple Cap", "high_score": "Highest score",
          "figures": "Best figures", "strike_rate": "Best strike rate", "economy": "Best economy",
          "fours": "Most fours", "sixes": "Most sixes", "fifties": "Most fifties", "centuries": "Most centuries",
          "player_of_tournament": "Player of the Tournament", "player_of_final": "Player of the Final",
          "winning_captain": "Winning Captain", "team_of_tournament": "Team of the Tournament"}


def build(name, data, db=None):
    images = {}
    champion = data.get("champion_name")
    if champion:
        facts = f'Runner-up: {data["runner_up"]}' if data.get("runner_up") else ""
        images["champion"] = compose_award_photo(name, "Champion", None, champion, facts)
    for key, rows in data["awards"].items():
        if key not in LABELS or not rows or key == "team_of_tournament":
            continue
        player = rows[0]
        stat = player["value"]
        caption = f'{stat[0]}/{stat[1]}' if isinstance(stat, list) else str(stat)
        if len(rows) > 1:
            caption = f'Shared award · {len(rows)} recipients · {caption}'
        images[key] = compose_award_photo(name, LABELS[key], player["name"], player["team"], caption)
    xi = data["awards"].get("team_of_tournament", [])
    if db is not None and len(xi) == 11:
        from types import SimpleNamespace
        from models import Player
        from services.team_of_tournament import render_image

        candidates = [SimpleNamespace(player=db.get(Player, row["player_id"]), captain=index == 0)
                      for index, row in enumerate(xi)]
        image = render_image(SimpleNamespace(name=name), candidates)
        if image:
            images["team_of_tournament"] = image
    return {key: b64encode(blob).decode("ascii") for key, blob in images.items()}


def sweep(now=None, limit=2):
    now = now or datetime.utcnow()
    with get_session() as db:
        ids = db.scalars(select(StudioAwardMedia.competition_id).where(
            StudioAwardMedia.generated_at.is_(None), StudioAwardMedia.due_at <= now,
            StudioAwardMedia.attempts < MAX_ATTEMPTS).order_by(StudioAwardMedia.due_at).limit(limit)).all()
    for room_id in ids:
        with get_session() as db:
            job = db.get(StudioAwardMedia, room_id)
            attempt = job.attempts + 1
            claimed = db.execute(update(StudioAwardMedia).where(StudioAwardMedia.competition_id == room_id,
                StudioAwardMedia.generated_at.is_(None), StudioAwardMedia.attempts == attempt - 1,
                StudioAwardMedia.due_at <= now).values(attempts=attempt, due_at=now + timedelta(minutes=LEASE_MINUTES)))
            if not claimed.rowcount:
                db.rollback()
                continue
            snapshot = db.get(StudioHonourSnapshot, (room_id, 1))
            room = db.get(StudioCompetition, room_id)
            name = db.get(StudioEvent, room.event_id).name
            data = snapshot.detail
            db.commit()
        try:
            with get_session() as db:
                result = build(name, data, db)
                db.execute(update(StudioAwardMedia).where(StudioAwardMedia.competition_id == room_id,
                    StudioAwardMedia.generated_at.is_(None), StudioAwardMedia.attempts == attempt)
                    .values(images=result, generated_at=now))
                db.commit()
        except Exception:
            logger.exception("Could not render Live Event awards for competition %s", room_id)
