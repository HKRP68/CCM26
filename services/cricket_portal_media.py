"""Public collectible cards and team artwork, isolated from live scoring."""


def player_image_url(player):
    if player is None:
        return None
    # A stable card URL is used even when a portrait URL is configured.
    return f'/stats/media/players/{player.id}.png?art=card'


def player_image(session, player_id):
    """The same custom/template/tier card used by the bot, rendered on demand.

    The card generator reuses this session and its bounded image caches. Remote
    artwork restoration is excluded; no work is added to match delivery.
    """
    from models import Player
    from services.card_generator import generate_card
    from services.tg_storage_service import local_assets_only
    from PIL import Image
    import io
    player = session.get(Player, player_id)
    if player is None:
        return None
    try:
        with local_assets_only():
            art = generate_card(player)
        if not art:
            return None
        with Image.open(io.BytesIO(art)) as card:
            card.thumbnail((960, 960))
            out = io.BytesIO()
            card.convert('RGBA').save(out, format='PNG')
            return out.getvalue(), 'image/png'

    except Exception:
        import logging
        logging.getLogger(__name__).warning('Player card unavailable for website player %s', player_id, exc_info=True)
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
