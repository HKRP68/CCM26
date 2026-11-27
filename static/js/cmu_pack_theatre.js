/*
 * CMU Pack Theatre — Phase 1
 * ═══════════════════════════════════════════════════════════════════════════
 * Original CricMaster Ultra pack artwork and the premium opening sequence.
 *
 * Presentation only. This module is handed an opening record the server has
 * already written (pack consumed, cards saved, history row created) and does
 * nothing but show it. It never requests, grants, re-rolls or re-orders a
 * reward, and it has no network access of its own.
 *
 * Artwork is drawn here from scratch — a foil pouch with a crimped tear-strip,
 * a cricket-ball roundel carrying the CMU monogram, a brand sash and tier pips.
 * The tier palettes are CMU's own token colours. Nothing is traced from, or
 * licensed out of, any other game.
 *
 * Exposed as window.CMUPackTheatre.
 */
(() => {
  'use strict';

  const FX = window.CMUAnim;
  if (!FX) {
    // Without the core the app must still open packs. Every entry point below
    // is written to be optional, so publishing a no-op surface is the safe
    // fallback rather than throwing at parse time.
    window.CMUPackTheatre = {
      available: false,
      art: () => '',
      tierFor: () => 'bronze',
      mount: () => {},
      open: () => Promise.resolve('unavailable'),
      deck: () => '',
      wireDeck: () => ({ revealAll: () => {}, flipped: () => 0, total: () => 0 }),
    };
    return;
  }

  /* ════════════════════════ Tier palettes ════════════════════════
   * Keyed off the pack catalogue in services/pack_service.py (Bronze, Silver,
   * Star, Legend, Ultimate Legend) plus the Free Pack. `pips` is purely the
   * count of diamonds printed on the foil — it is decoration, not odds.
   */
  const TIERS = {
    free: {
      name: 'FREE PACK', short: 'F', pips: 1,
      foil: '#7FA7C9', mid: '#3B6184', deep: '#16293B', ink: '#0B1620',
      glow: '#91BEF1',
    },
    bronze: {
      name: 'BRONZE PACK', short: 'B', pips: 1,
      foil: '#E0A273', mid: '#A3602F', deep: '#4A2A16', ink: '#2A1508',
      glow: '#E0A273',
    },
    silver: {
      name: 'SILVER PACK', short: 'S', pips: 2,
      foil: '#E8ECEF', mid: '#8D959C', deep: '#3F474E', ink: '#1A1E22',
      glow: '#D9DCDE',
    },
    star: {
      name: 'STAR PACK', short: '★', pips: 3,
      foil: '#F8E3B4', mid: '#C3652E', deep: '#5E3410', ink: '#241508',
      glow: '#E7A443',
    },
    legend: {
      name: 'LEGEND PACK', short: 'L', pips: 4,
      foil: '#FFE9A8', mid: '#C99227', deep: '#5B400C', ink: '#241508',
      glow: '#FFD966',
    },
    ultimate: {
      name: 'ULTIMATE LEGEND', short: 'U', pips: 5,
      foil: '#D8C6FF', mid: '#7C5CE0', deep: '#2D1D5C', ink: '#150C2B',
      glow: '#C6B1FF',
    },
  };

  const tier = (key) => TIERS[String(key || '').toLowerCase()] || TIERS.bronze;

  /** Work out a pack's cosmetic tier from the data the Mini App already has.
   *  Order matters: "Ultimate Legend Pack" must not match "legend" first. */
  function tierFor(packName, { source = '', mainMaxRating = 0 } = {}) {
    const name = String(packName || '').toLowerCase();
    if (source === 'freepack' || name.includes('free')) return 'free';
    if (name.includes('ultimate')) return 'ultimate';
    if (name.includes('legend')) return 'legend';
    if (name.includes('star')) return 'star';
    if (name.includes('silver')) return 'silver';
    if (name.includes('bronze')) return 'bronze';
    // Unknown or admin-renamed pack: fall back to its headline rating band so a
    // custom pack still gets artwork that matches what is inside it.
    const ovr = Number(mainMaxRating) || 0;
    if (ovr >= 96) return 'ultimate';
    if (ovr >= 91) return 'legend';
    if (ovr >= 88) return 'silver';
    if (ovr >= 85) return 'bronze';
    return 'bronze';
  }

  /* ════════════════════════ Original pack artwork ════════════════════════ */

  /** Points for a four-pointed diamond pip. */
  function pip(cx, cy, r) {
    return `${cx},${cy - r} ${cx + r * 0.62},${cy} ${cx},${cy + r} ${cx - r * 0.62},${cy}`;
  }

  /** Zig-zag crimp edge, the serrated heat seal across a foil pouch. */
  function crimp(y, from, to, depth, step) {
    let path = `M${from} ${y}`;
    for (let x = from; x < to; x += step) {
      path += ` L${x + step / 2} ${y - depth} L${x + step} ${y}`;
    }
    return path;
  }

  /**
   * Pack foil artwork as inline SVG.
   *
   * The top crimp sits in its own <g class="cmu-pack-crimp"> and the shell in
   * <g class="cmu-pack-body">, so the stylesheet can tear the strip off the
   * body without the two moving together. `--cmu-pack-sheen` drives the
   * metallic band.
   *
   * @param {string} key   tier key
   * @param {object} opts  { title } accessible name override
   */
  function art(key, opts = {}) {
    const t = tier(key);
    const id = FX.uid('cmupk');
    const label = FX.esc(opts.title || t.name);
    const pips = Array.from({ length: t.pips }, (_, i) => {
      const cx = 60 + (i - (t.pips - 1) / 2) * 12;
      return `<polygon points="${pip(cx, 139, 4.4)}" fill="url(#${id}f)" opacity=".95"/>`;
    }).join('');

    return `<svg class="cmu-pack-svg" viewBox="0 0 120 172" role="img" aria-label="${label}"
  xmlns="http://www.w3.org/2000/svg" focusable="false">
  <defs>
    <linearGradient id="${id}f" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="${t.foil}"/>
      <stop offset=".52" stop-color="${t.mid}"/>
      <stop offset="1" stop-color="${t.foil}"/>
    </linearGradient>
    <linearGradient id="${id}b" x1=".2" y1="0" x2=".8" y2="1">
      <stop offset="0" stop-color="${t.deep}"/>
      <stop offset=".55" stop-color="${t.ink}"/>
      <stop offset="1" stop-color="${t.deep}"/>
    </linearGradient>
    <linearGradient id="${id}s" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#fff" stop-opacity="0"/>
      <stop offset=".48" stop-color="#fff" stop-opacity=".34"/>
      <stop offset="1" stop-color="#fff" stop-opacity="0"/>
    </linearGradient>
    <clipPath id="${id}c"><rect x="8" y="14" width="104" height="146" rx="9"/></clipPath>
  </defs>

  <ellipse cx="60" cy="166" rx="36" ry="4" fill="#000" opacity=".4"/>

  <g class="cmu-pack-body">
    <rect x="8" y="14" width="104" height="146" rx="9"
          fill="url(#${id}b)" stroke="url(#${id}f)" stroke-width="1.7"/>
    <g clip-path="url(#${id}c)">
      <!-- diagonal brand sash -->
      <path d="M8 104 L112 58 L112 78 L8 124 Z" fill="url(#${id}f)" opacity=".16"/>
      <path d="M8 46 L112 18" stroke="${t.foil}" stroke-opacity=".12" stroke-width="12"/>
      <!-- heat-seal foot -->
      <rect x="8" y="148" width="104" height="12" fill="url(#${id}f)" opacity=".88"/>
      <path d="M8 150 h104" stroke="${t.ink}" stroke-opacity=".35" stroke-width="1"/>
      <!-- metallic sheen band -->
      <g class="cmu-pack-sheen">
        <rect x="-44" y="0" width="30" height="172" fill="url(#${id}s)" transform="skewX(-17)"/>
      </g>
    </g>

    <!-- cricket-ball roundel with the CMU monogram -->
    <circle cx="60" cy="88" r="27" fill="${t.ink}" stroke="url(#${id}f)" stroke-width="2.1"/>
    <path d="M44 70 Q60 88 44 106" fill="none" stroke="${t.foil}" stroke-opacity=".45"
          stroke-width="1.5" stroke-dasharray="3 3"/>
    <path d="M76 70 Q60 88 76 106" fill="none" stroke="${t.foil}" stroke-opacity=".45"
          stroke-width="1.5" stroke-dasharray="3 3"/>
    <text x="60" y="95" text-anchor="middle" font-size="16" font-weight="900"
          letter-spacing="1" fill="url(#${id}f)"
          font-family="Geist, system-ui, sans-serif">CMU</text>

    <text x="60" y="34" text-anchor="middle" font-size="7" font-weight="900"
          textLength="84" lengthAdjust="spacingAndGlyphs" fill="${t.foil}"
          font-family="Geist, system-ui, sans-serif">CRICMASTER ULTRA</text>
    <text x="60" y="45" text-anchor="middle" font-size="5.2" font-weight="700"
          textLength="52" lengthAdjust="spacingAndGlyphs" fill="${t.foil}" opacity=".66"
          font-family="Geist, system-ui, sans-serif">PLAYER PACK</text>
    ${pips}
    <text x="60" y="157" text-anchor="middle" font-size="6.4" font-weight="900"
          textLength="${Math.min(88, t.name.length * 5.6)}" lengthAdjust="spacingAndGlyphs"
          fill="${t.ink}"
          font-family="Geist, system-ui, sans-serif">${FX.esc(t.name)}</text>
  </g>

  <!-- tear strip: its own group so the foil can part from the shell -->
  <g class="cmu-pack-crimp">
    <rect x="8" y="10" width="104" height="13" rx="4" fill="url(#${id}f)"/>
    <path d="${crimp(10, 8, 112, 4, 8)}" fill="url(#${id}f)"/>
    <path d="M14 16.5 h92" stroke="${t.ink}" stroke-opacity=".42" stroke-width="1.2"
          stroke-dasharray="2 3"/>
    <circle cx="103" cy="16.5" r="3.1" fill="${t.ink}" stroke="${t.foil}" stroke-width="1"/>
  </g>
</svg>`;
  }

  /* ════════════════════════ Pack shop emblem ════════════════════════ */

  /** Small floating pack emblem for a shop/inventory row. */
  function emblem(key, { title = '' } = {}) {
    return `<span class="cmu-pack-emblem cmu-fx-float">${art(key, { title })}</span>`;
  }

  /* ════════════════════════ Mount onto the opening button ════════════════════════ */

  /*
   * The existing overlay already has a pack button (#pack-open-pack) with a
   * CSS foil. `mount` swaps in the SVG artwork and wires tap *and* swipe,
   * leaving the button's own click handler — and therefore the existing
   * revealPackCards() contract — untouched.
   */
  function mount(button, key, { onOpen = null } = {}) {
    if (!button) return () => {};
    const slot = button.querySelector('[data-cmu-pack-art]')
      || button.querySelector('.pack-foil');
    if (!slot) return () => {};

    const holder = document.createElement('span');
    holder.className = 'cmu-pack-hero';
    holder.setAttribute('data-cmu-pack-art', '');
    holder.innerHTML = art(key);
    slot.replaceWith(holder);
    button.classList.add('cmu-pack-mounted');
    button.dataset.cmuTier = String(key || 'bronze');

    // Swipe-to-open: a vertical drag of at least 34px on the pack opens it,
    // the same gesture as tearing a real foil. Taps still work (the button's
    // click handler), and a horizontal drag is left to the scroller.
    let startY = null;
    let startX = null;
    let fired = false;

    const onStart = (event) => {
      if (button.disabled) return;
      const touch = event.touches ? event.touches[0] : event;
      startY = touch.clientY;
      startX = touch.clientX;
      fired = false;
      button.classList.add('is-gripped');
    };

    const onMove = (event) => {
      if (startY === null || fired || button.disabled) return;
      const touch = event.touches ? event.touches[0] : event;
      const dy = startY - touch.clientY;
      const dx = Math.abs(startX - touch.clientX);
      if (dy > 34 && dy > dx) {
        fired = true;
        button.classList.remove('is-gripped');
        // Route through the same handler a tap uses so there is exactly one
        // open path, and therefore exactly one place a double-open is guarded.
        if (onOpen) onOpen('swipe');
        else button.click();
      } else if (dy > 4 && dy > dx) {
        holder.style.setProperty('--cmu-pack-pull', `${Math.min(dy, 34)}px`);
      }
    };

    const onEnd = () => {
      startY = null;
      startX = null;
      button.classList.remove('is-gripped');
      holder.style.removeProperty('--cmu-pack-pull');
    };

    button.addEventListener('touchstart', onStart, { passive: true });
    button.addEventListener('touchmove', onMove, { passive: true });
    button.addEventListener('touchend', onEnd, { passive: true });
    button.addEventListener('touchcancel', onEnd, { passive: true });
    button.addEventListener('pointerdown', onStart);
    button.addEventListener('pointermove', onMove);
    button.addEventListener('pointerup', onEnd);
    button.addEventListener('pointercancel', onEnd);

    return () => {
      for (const [type, fn] of [['touchstart', onStart], ['touchmove', onMove],
        ['touchend', onEnd], ['touchcancel', onEnd], ['pointerdown', onStart],
        ['pointermove', onMove], ['pointerup', onEnd], ['pointercancel', onEnd]]) {
        button.removeEventListener(type, fn);
      }
      // Drop the marker class too. A Free Pack opened straight after a foil
      // pack reuses the same button, and a stale .cmu-pack-mounted would keep
      // the foil's rules (including the torn state's visibility: hidden)
      // applying to artwork that is no longer there.
      button.classList.remove('cmu-pack-mounted', 'is-shaking', 'is-tearing',
        'is-torn', 'is-gripped');
      delete button.dataset.cmuTier;
    };
  }

  /* ════════════════════════ The opening sequence ════════════════════════ */

  /**
   * Shake the pack, tear the foil, burst the light, scatter particles.
   *
   * Resolves when the stage is ready for cards. At motion level "off" it
   * resolves immediately without touching the DOM beyond marking the pack
   * torn, so the caller renders the deck in the same tick.
   *
   * @returns {Promise<'played'|'skipped'|'instant'>}
   */
  async function open(button, surface, key, { skip = false } = {}) {
    const t = tier(key);
    if (surface) surface.style.setProperty('--cmu-overlay-glow', t.glow);

    if (!button || skip || FX.isOff()) {
      button?.classList.add('is-torn');
      return skip ? 'skipped' : 'instant';
    }

    const holder = button.querySelector('[data-cmu-pack-art]');
    const rays = surface?.querySelector('.cmu-fx-rays');

    // 1. shake — the pack is alive and something wants out
    button.classList.add('is-shaking');
    FX.haptic('medium');
    FX.sound('pack_shake');
    await FX.beat(FX.isMinimal() ? 160 : 520);
    button.classList.remove('is-shaking');

    // 2. tear — the crimp strip parts from the shell
    button.classList.add('is-tearing');
    FX.haptic('heavy');
    FX.sound('pack_tear');
    await FX.beat(FX.isMinimal() ? 120 : 420);

    // 3. burst + rays + particles, all scoped to the stage
    rays?.classList.add('is-lit');
    const decoration = [
      FX.lightBurst(surface || document.body, { tier: rarityTierKey(key), hold: 640 }),
      FX.particles(holder || button, { count: 30, colors: [t.foil, t.mid, '#ffffff'], spread: 170 }),
    ];
    FX.notify('success');
    await FX.beat(FX.isMinimal() ? 80 : 300);
    button.classList.add('is-torn');
    // Decoration finishes on its own clock; the cards must not wait for it.
    Promise.allSettled(decoration);
    return 'played';
  }

  /** Map a pack tier onto a confetti/burst palette key from the core engine. */
  function rarityTierKey(key) {
    switch (String(key || '').toLowerCase()) {
      case 'ultimate': return 'epic';
      case 'legend': return 'legend';
      case 'star': return 'rare';
      default: return '';
    }
  }

  /* ════════════════════════ The card deck ════════════════════════ */

  /**
   * Build the staggered flip deck.
   *
   * Each card is a flip container holding a real <button> back face (the tap
   * target, so the deck is keyboard-operable and announced correctly) and a
   * non-interactive front face carrying markup the caller supplies — in the
   * Mini App that is playerRevealCard(), unchanged, so the card renderer stays
   * the single source of truth for what a player card looks like.
   *
   * @param {Array} players  confirmed players, already in display order
   * @param {object} opts    { packKey, renderCard, startFlipped }
   */
  function deck(players, { packKey = 'bronze', renderCard = null, startFlipped = false } = {}) {
    const list = Array.isArray(players) ? players : [];
    if (!list.length) return '';
    const backArt = art(packKey, { title: 'Face-down card' });
    const cards = list.map((player, index) => {
      const rating = Number(player.rating) || 0;
      const rarity = FX.rarityOf(rating);
      const slot = player.slot === 'main' ? 'Main player'
        : player.slot === 'bonus' ? 'Bonus player' : '';
      const name = FX.esc(player.name || 'Player');
      const face = renderCard ? renderCard(player, index) : '';
      // Delay is capped so a big pack does not make the last card feel late.
      const delay = Math.min(index, 5) * 90;
      return `<div class="cmu-pack-card cmu-rarity-${rarity}${startFlipped ? ' is-revealed' : ''}"
        data-cmu-card="${index}" style="--cmu-delay:${delay}ms">
        <div class="cmu-pack-card-inner cmu-fx-flip${startFlipped ? ' is-flipped' : ''}">
          <button type="button" class="cmu-fx-face cmu-fx-face-back cmu-pack-back"
                  data-cmu-flip="${index}"
                  ${startFlipped ? 'disabled aria-hidden="true" tabindex="-1"' : ''}
                  aria-label="Reveal card ${index + 1} of ${list.length}">
            <span class="cmu-pack-back-art">${backArt}</span>
            <span class="cmu-pack-back-hint">Tap to reveal</span>
          </button>
          <div class="cmu-fx-face cmu-fx-face-front cmu-pack-front">
            <span class="cmu-pack-ribbon">
              <span class="cmu-fx-chip">${FX.esc(FX.rarityLabel(rating))}</span>
              ${slot ? `<span class="cmu-pack-slot">${slot}</span>` : ''}
            </span>
            <div class="cmu-pack-front-body" data-cmu-card-body="${index}">${face}</div>
          </div>
        </div>
        <p class="cmu-fx-sr" data-cmu-card-sr="${index}">${startFlipped ? `${name}, ${rating} OVR` : ''}</p>
      </div>`;
    }).join('');
    return `<div class="cmu-pack-deck" data-cards="${list.length}">${cards}</div>`;
  }

  /**
   * Wire a rendered deck for tapping and Reveal All.
   *
   * Returns a small controller the caller uses to drive the Done button. Every
   * callback is optional and every path is idempotent: flipping an already
   * flipped card does nothing, and `revealAll` is safe to call repeatedly.
   */
  function wireDeck(root, { players = [], onFlip = null, onComplete = null } = {}) {
    const cards = Array.from(root?.querySelectorAll('.cmu-pack-card') || []);
    const total = cards.length;
    let flippedCount = cards.filter((card) => card.classList.contains('is-revealed')).length;
    let completed = false;

    for (const card of cards) {
      if (!card.classList.contains('is-revealed')) continue;
      const back = card.querySelector('.cmu-pack-back');
      if (back) {
        back.disabled = true;
        back.setAttribute('aria-hidden', 'true');
        back.tabIndex = -1;
      }
      const index = Number(card.dataset.cmuCard) || 0;
      const player = players[index] || {};
      const sr = card.querySelector('[data-cmu-card-sr]');
      if (sr && !sr.textContent.trim()) {
        sr.textContent = `${player.name || 'Player'}, ${Number(player.rating) || 0} OVR`;
      }
    }

    function announceComplete() {
      if (completed || flippedCount < total) return;
      completed = true;
      if (onComplete) {
        try { onComplete(); } catch (_) {}
      }
    }

    function flipCard(card, { quiet = false } = {}) {
      if (!card || card.classList.contains('is-revealed')) return false;
      const index = Number(card.dataset.cmuCard) || 0;
      const player = players[index] || {};
      const rating = Number(player.rating) || 0;
      card.classList.add('is-revealed');
      card.querySelector('.cmu-fx-flip')?.classList.add('is-flipped');
      const back = card.querySelector('.cmu-pack-back');
      if (back) {
        back.disabled = true;
        back.setAttribute('aria-hidden', 'true');
        back.tabIndex = -1;
      }
      // Announce the card to a screen reader only once it is face up —
      // reading it earlier would spoil the reveal it is describing.
      const sr = card.querySelector('[data-cmu-card-sr]');
      if (sr) sr.textContent = `${player.name || 'Player'}, ${rating} OVR`;
      flippedCount += 1;

      if (!quiet) {
        const rarity = FX.rarityOf(rating);
        const loud = rarity === 'legendary' || rarity === 'mythic';
        FX.haptic(loud ? 'heavy' : rarity === 'epic' ? 'medium' : 'light');
        FX.sound(loud ? 'card_legendary' : 'card_flip');
        if (loud) {
          card.classList.add('is-big-hit');
          FX.particles(card, { count: 22, tier: rarity === 'mythic' ? 'epic' : 'legend' });
        }
      }
      if (onFlip) {
        try { onFlip(player, index, card); } catch (_) {}
      }
      announceComplete();
      return true;
    }

    root?.addEventListener('click', (event) => {
      const back = event.target.closest('[data-cmu-flip]');
      if (!back) return;
      flipCard(back.closest('.cmu-pack-card'));
    });

    /** Flip everything still face-down, staggered unless motion is reduced. */
    async function revealAll() {
      const step = FX.ms(220);
      for (const card of cards) {
        if (!card.classList.contains('is-revealed')) {
          flipCard(card);
          if (step) await FX.sleep(step);
        }
      }
      announceComplete();
    }

    /** Flip everything at once with no feedback — the Skip path. */
    function revealInstant() {
      for (const card of cards) flipCard(card, { quiet: true });
      announceComplete();
    }

    // If a deck arrives already open (skip / motion off) the caller still needs
    // the completion callback.
    if (total && flippedCount >= total) announceComplete();

    return {
      revealAll,
      revealInstant,
      flipped: () => flippedCount,
      total: () => total,
      get complete() { return flippedCount >= total; },
    };
  }

  window.CMUPackTheatre = {
    available: true,
    TIERS,
    tier,
    tierFor,
    art,
    emblem,
    mount,
    open,
    deck,
    wireDeck,
    rarityTierKey,
  };
})();
