"""Available public cricket artwork; no image generation on website requests."""
from services.league_record_insights import safe_image_url


def player_image_url(player):
    if player is None:
        return None
    raw = safe_image_url(player.image_url)
    # Uploaded portraits are often absolute filesystem paths, not Flask URLs.
    if raw and raw.startswith(('https://', 'http://', '/static/')):
        return raw
    return f'/stats/media/players/{player.id}.png'


def player_image(session, player_id):
    from models import Player
    from services import player_portrait_service as portraits, player_image_service
    from services.tg_storage_service import local_assets_only
    import io
    player = session.get(Player, player_id)
    if player is None:
        return None
    with local_assets_only():
        portrait = portraits.get_player_portrait(player, include_global=False)
        if portrait is not None:
            out = io.BytesIO()
            portrait.thumbnail((600, 600))
            portrait.save(out, format='PNG')
            return out.getvalue(), 'image/png'
        art = player_image_service.get_custom_image_bytes(player.id, session=session)
        if art:
            from PIL import Image
            with Image.open(io.BytesIO(art)) as image:
                image.thumbnail((600, 600))
                out = io.BytesIO()
                image.convert('RGBA').save(out, format='PNG')
                return out.getvalue(), 'image/png'
    return None


def team_image(session, team_id):
    from models import TournamentTeam, LeagueSeason, LeagueSeries
    from services import card_identity
    from services.tg_storage_service import local_assets_only
    team = (session.query(TournamentTeam).join(LeagueSeason,
        LeagueSeason.tournament_id == TournamentTeam.tournament_id).join(LeagueSeries,
        LeagueSeries.id == LeagueSeason.series_id).filter(TournamentTeam.id == team_id,
        LeagueSeries.is_active.is_(True)).first())
    if team is None:
        return None
    with local_assets_only():
        art = card_identity.team_logo_png(session, tournament_team_id=team.id, team_name=team.name)
    return (art, 'image/png') if art else None
