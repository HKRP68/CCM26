"""Composite market image — single image showing all market slots in a grid.

Builds ONE PNG that combines individual player cards into a 2-column grid,
so the bot can send one image instead of N separate cards.
"""

import io
import logging

logger = logging.getLogger(__name__)


def _safe_get_card_bytes(player):
    """Get the card image bytes for a player (custom image OR generated)."""
    try:
        from services.card_generator import generate_card
        return generate_card(player)
    except Exception:
        logger.exception(f"Failed to get card for player {player.id}")
        return None


def build_market_composite(slots_with_players, columns=2, padding=20,
                           bg_color=(15, 17, 25),
                           header_height=110, max_width=1200):
    """Build a single composite PNG.

    Args:
      slots_with_players: list of (slot_row, player) tuples
      columns: 2 looks best on phones; 3 for many slots
      padding: pixels between cards and around the edge
      bg_color: dark navy by default
      header_height: space for the "PLAYER MARKET" title at the top
      max_width: cap output width so Telegram doesn't shrink it badly

    Returns: PNG bytes, or None on failure.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.error("PIL not available")
        return None

    if not slots_with_players:
        return None

    # Generate / fetch each card
    cards = []
    for slot, player in slots_with_players:
        b = _safe_get_card_bytes(player)
        if b:
            try:
                img = Image.open(io.BytesIO(b)).convert("RGB")
                cards.append((slot, player, img))
            except Exception:
                logger.exception(f"Failed to open card image for player {player.id}")

    if not cards:
        return None

    # Standardize card size — pick smallest dimensions to keep output reasonable
    target_card_w = min(450, max(c[2].size[0] for c in cards))
    # Maintain aspect ratio
    resized = []
    for slot, player, img in cards:
        ratio = target_card_w / img.size[0]
        new_h = int(img.size[1] * ratio)
        img2 = img.resize((target_card_w, new_h), Image.LANCZOS)
        resized.append((slot, player, img2))

    # Compute layout
    cols = columns
    rows = (len(resized) + cols - 1) // cols
    cell_w = target_card_w + padding
    # Use the tallest card height as cell height (keeps grid uniform)
    cell_h = max(img.size[1] for _, _, img in resized) + padding + 40
    # +40 for the slot/price label below each card

    grid_w = cols * cell_w + padding
    grid_h = rows * cell_h + padding + header_height

    # Cap width
    if grid_w > max_width:
        scale = max_width / grid_w
        grid_w = max_width
        grid_h = int(grid_h * scale)
        cell_w = int(cell_w * scale)
        cell_h = int(cell_h * scale)
        target_card_w = int(target_card_w * scale)
        # Re-resize cards
        resized = [(s, p, img.resize((target_card_w, int(img.size[1] * scale)), Image.LANCZOS))
                   for s, p, img in resized]

    # Build canvas
    canvas = Image.new("RGB", (grid_w, grid_h), bg_color)
    draw = ImageDraw.Draw(canvas)

    # Header
    try:
        title_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
        sub_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
        slot_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
    except Exception:
        title_font = ImageFont.load_default()
        sub_font = ImageFont.load_default()
        slot_font = ImageFont.load_default()

    # Title
    title = "🌟 PLAYER MARKET"
    try:
        bbox = draw.textbbox((0, 0), title, font=title_font)
        tw = bbox[2] - bbox[0]
    except Exception:
        tw = 400
    draw.text(((grid_w - tw) // 2, 25), title, fill=(255, 215, 60), font=title_font)
    sub = f"{len(resized)} player(s) available — same for all"
    try:
        bbox = draw.textbbox((0, 0), sub, font=sub_font)
        sw = bbox[2] - bbox[0]
    except Exception:
        sw = 300
    draw.text(((grid_w - sw) // 2, 75), sub, fill=(180, 180, 200), font=sub_font)

    # Place cards
    for i, (slot, player, img) in enumerate(resized):
        row = i // cols
        col = i % cols
        x = padding + col * cell_w
        y = header_height + padding + row * cell_h
        canvas.paste(img, (x, y))

        # Label below card: "Slot N · 60,000 🪙"
        sold_out = slot.purchased_count >= slot.quantity
        label_text = f"#{slot.slot_index}"
        try:
            bbox = draw.textbbox((0, 0), label_text, font=slot_font)
            lw = bbox[2] - bbox[0]
        except Exception:
            lw = 30
        # Slot number — left side
        label_y = y + img.size[1] + 6
        draw.text((x + 10, label_y), label_text,
                  fill=(255, 215, 60), font=slot_font)

        # Price — right side
        price_text = f"{slot.final_price:,} 🪙" if not sold_out else "❌ SOLD OUT"
        price_color = (180, 180, 200) if not sold_out else (220, 70, 70)
        try:
            bbox = draw.textbbox((0, 0), price_text, font=slot_font)
            pw = bbox[2] - bbox[0]
        except Exception:
            pw = 100
        draw.text((x + img.size[0] - pw - 10, label_y),
                  price_text, fill=price_color, font=slot_font)

    # Save to bytes
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()
