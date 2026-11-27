/*
 * CMU Legendary Walkout — Phase 3
 * ═══════════════════════════════════════════════════════════════════════════
 * The cinematic a high-rated pull earns: a stadium tunnel, the floodlights
 * coming up bank by bank, the nationality and player category announced, the
 * rating counting up, and the player's own card presented at the end.
 *
 * Presentation only, and deliberately literal about it:
 *
 *   • every value shown — name, country, category, version, rating, artwork —
 *     is passed in by the caller from a record the server has confirmed. The
 *     module invents nothing. There is no player figure in the artwork for the
 *     same reason: a drawn silhouette would be a model of a real cricketer
 *     that CMU has no likeness for. What walks out of the tunnel is light and
 *     the player's own card.
 *   • the rating counter animates towards the confirmed rating and always
 *     lands exactly on it, including when skipped mid-count.
 *   • whether a pull earns the walkout at all is a cosmetic threshold on the
 *     engine (CMUAnim.rarity.walkoutFrom, configurable), never a rule about
 *     what the pull was worth.
 *
 * Exposed as window.CMUWalkout.
 */
(() => {
  'use strict';

  const FX = window.CMUAnim;
  if (!FX) {
    window.CMUWalkout = { available: false, play: () => Promise.resolve('unavailable'), stadium: () => '' };
    return;
  }

  /* ════════════════════════ Stadium artwork ════════════════════════
   * Four floodlight pylons, a tiered stand, the tunnel mouth with perspective
   * ribs and a strip of pitch. Classes drive the animation; colours come from
   * CSS so the scene works in both themes.
   */
  function stadium() {
    const id = FX.uid('cmuwo');
    // Stand tiers: abstract blocks, no crowd figures.
    let tiers = '';
    for (let x = 0; x < 360; x += 9) {
      const h = 7 + ((x * 7) % 11);
      tiers += `<rect class="wo-tier" x="${x}" y="${104 - h}" width="7" height="${h}"/>`;
    }
    // Tunnel perspective ribs, narrowing towards the vanishing point.
    let ribs = '';
    for (let i = 0; i < 5; i++) {
      const k = i / 5;
      const w = 86 - k * 54;
      const h = 74 - k * 46;
      ribs += `<path class="wo-rib" style="--wo-rib:${i}"
        d="M${180 - w / 2} 214 V${214 - h} Q180 ${214 - h - 13} ${180 + w / 2} ${214 - h} V214"/>`;
    }
    const pylon = (x, flip, bank) => `
      <g class="cmu-wo-pylon" style="--wo-bank:${bank}" transform="translate(${x} 0)${flip ? ' scale(-1 1) translate(-0 0)' : ''}">
        <path class="wo-mast" d="M0 112 L4 44"/>
        <path class="wo-mast wo-mast-thin" d="M0 112 L-5 112 L1 44"/>
        <rect class="wo-lamp-plate" x="-13" y="26" width="30" height="19" rx="3"/>
        <g class="cmu-wo-lamps">
          <circle class="wo-lamp" cx="-6" cy="32" r="3.1"/>
          <circle class="wo-lamp" cx="2" cy="32" r="3.1"/>
          <circle class="wo-lamp" cx="10" cy="32" r="3.1"/>
          <circle class="wo-lamp" cx="-6" cy="40" r="3.1"/>
          <circle class="wo-lamp" cx="2" cy="40" r="3.1"/>
          <circle class="wo-lamp" cx="10" cy="40" r="3.1"/>
        </g>
        <path class="cmu-wo-beam" d="M2 44 L92 232 L-78 232 Z"/>
      </g>`;

    return `<svg class="cmu-wo-svg" viewBox="0 0 360 240" preserveAspectRatio="xMidYMax meet"
  aria-hidden="true" focusable="false" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="${id}sky" x1="0" y1="0" x2="0" y2="1">
      <stop class="wo-sky1" offset="0"/><stop class="wo-sky2" offset="1"/>
    </linearGradient>
    <radialGradient id="${id}haze" cx=".5" cy=".92" r=".7">
      <stop class="wo-haze1" offset="0"/><stop class="wo-haze2" offset="1"/>
    </radialGradient>
  </defs>

  <rect class="wo-sky" width="360" height="240" fill="url(#${id}sky)"/>
  ${pylon(40, false, 0)}
  ${pylon(320, true, 1)}
  ${pylon(104, false, 2)}
  ${pylon(256, true, 3)}

  <g class="cmu-wo-stand">${tiers}</g>
  <rect class="wo-rail" y="103" width="360" height="3"/>

  <rect class="wo-turf" y="106" width="360" height="134"/>
  <rect class="wo-haze-layer" y="106" width="360" height="134" fill="url(#${id}haze)"/>
  <path class="wo-crease" d="M96 206 h168 M118 176 h124"/>

  <!-- tunnel mouth -->
  <g class="cmu-wo-tunnel">
    <path class="wo-tunnel-dark" d="M137 214 V152 Q180 128 223 152 V214 Z"/>
    ${ribs}
    <path class="wo-tunnel-lip" d="M137 214 V152 Q180 128 223 152 V214"/>
    <rect class="cmu-wo-glow" x="146" y="150" width="68" height="64" rx="30"/>
  </g>
</svg>`;
  }

  /* ════════════════════════ The sequence ════════════════════════ */

  const CATEGORY_LINE = {
    Batter: 'BATTER', Batsman: 'BATTER', Bowler: 'BOWLER',
    'All-rounder': 'ALL-ROUNDER', Allrounder: 'ALL-ROUNDER',
    'Wicket-keeper': 'WICKET-KEEPER', Keeper: 'WICKET-KEEPER',
  };

  let active = null;

  /**
   * Play the walkout for one confirmed player.
   *
   * @param {object} player  { name, rating, country, category, version,
   *                           cardMarkup }  — all from the server's record
   * @param {object} opts    { title } eyebrow text
   * @returns {Promise<'played'|'skipped'|'closed'|'unavailable'|'failed'>}
   *   Resolves exactly once. The caller always continues to its own reveal,
   *   whatever this returns.
   */
  function play(player = {}, opts = {}) {
    const rating = Number(player.rating) || 0;
    // A second walkout must never stack on the first: a double tap, or two
    // qualifying cards flipped in quick succession, closes the old one.
    if (active) { try { active.close('superseded'); } catch (_) {} }
    if (!FX.deservesWalkout(rating)) return Promise.resolve('unavailable');

    const rarity = FX.rarityOf(rating);
    const name = FX.esc(player.name || 'Player');
    const country = String(player.country || '').trim();
    const category = String(player.category || '').trim();
    const version = String(player.version || '').trim();
    const eyebrow = FX.esc(opts.title || (rarity === 'mythic' ? 'Mythic pull' : 'Legendary pull'));

    return new Promise((resolve) => {
      let settled = false;
      const finish = (reason) => {
        if (settled) return;
        settled = true;
        if (active === overlay) active = null;
        releaseTeardown();
        resolve(reason);
      };

      let overlay;
      try {
        overlay = FX.overlay({
          className: `cmu-wo cmu-rarity-${rarity}`,
          label: `${player.name || 'Player'} walkout`,
          dismissLabel: 'Skip',
          html: `
            <div class="cmu-wo-scene" aria-hidden="true">${stadium()}</div>
            <div class="cmu-wo-copy">
              <span class="cmu-fx-eyebrow cmu-wo-eyebrow">${eyebrow}</span>
              <p class="cmu-wo-line cmu-wo-country" data-wo="country">${FX.esc(country)}</p>
              <p class="cmu-wo-line cmu-wo-category" data-wo="category">${FX.esc(CATEGORY_LINE[category] || category.toUpperCase())}</p>
              <p class="cmu-wo-rating" data-wo="rating">
                <span class="cmu-fx-count" data-wo="count">0</span><small>OVR</small>
              </p>
              <h3 class="cmu-wo-name" data-wo="name">${name}</h3>
              ${version && version !== 'Base' ? `<p class="cmu-wo-version" data-wo="version">${FX.esc(version)}</p>` : ''}
              <div class="cmu-wo-card" data-wo="card">${player.cardMarkup || ''}</div>
            </div>
            <p class="cmu-fx-sr" role="status">${name}, ${rating} OVR${country ? `, ${FX.esc(country)}` : ''}${category ? `, ${FX.esc(category)}` : ''}</p>`,
          onClose: (reason) => finish(reason === 'done' ? 'played' : 'skipped'),
        });
      } catch (error) {
        if (window.console) console.warn('[CMUWalkout] could not open safely', error);
        finish('failed');
        return;
      }

      active = overlay;
      // Closing the Mini App or navigating away must not leave the stadium
      // pinned over the app when the player comes back.
      const releaseTeardown = FX.onTeardown(() => overlay.close('teardown'));

      const root = overlay.root;
      const step = (selector) => root.querySelector(`[data-wo="${selector}"]`);
      const show = (key) => { const el = step(key); if (el) el.classList.add('is-on'); };

      (async () => {
        try {
          // 1 · the tunnel, lit only by what is beyond it
          await FX.beat(320);
          if (overlay.closed) return;

          // 2 · floodlights, one bank at a time
          root.classList.add('is-lighting');
          FX.sound('floodlights');
          FX.haptic('light');
          await FX.beat(260);
          if (overlay.closed) return;
          root.classList.add('is-lit');
          FX.haptic('medium');
          await FX.beat(520);
          if (overlay.closed) return;

          // 3 · the tunnel opens
          root.classList.add('is-emerging');
          FX.haptic('heavy');
          await FX.beat(460);
          if (overlay.closed) return;

          // 4 · nationality, then category — each only if we were given one
          if (country) { show('country'); FX.haptic('light'); await FX.beat(560); }
          if (overlay.closed) return;
          if (category) { show('category'); FX.haptic('light'); await FX.beat(520); }
          if (overlay.closed) return;

          // 5 · the rating counts up to the confirmed value and stops there
          show('rating');
          FX.sound('rating_climb');
          await FX.countUp(step('count'), rating, { duration: 1100, format: (n) => String(n) });
          if (overlay.closed) return;
          FX.notify('success');

          // 6 · the player's own card is presented
          show('name');
          show('version');
          await FX.beat(260);
          if (overlay.closed) return;
          show('card');
          root.classList.add('is-presented');
          await FX.waitForArt(root, 1200);
          if (overlay.closed) return;
          FX.confetti(rarity === 'mythic' ? 64 : 48, 'legend', root);
          FX.particles(step('card') || root, { count: 30, tier: 'legend', spread: 200 });
          overlay.focusFirst();
          await FX.beat(2200);
          overlay.close('done');
        } catch (error) {
          if (window.console) console.warn('[CMUWalkout] sequence failed safely', error);
          overlay.close('failed');
        }
      })();

      // However the overlay ends — Skip, Escape, backdrop, teardown or the
      // sequence running out — the end state is written before it goes, so a
      // skipped counter never leaves a wrong number on screen.
      overlay.onClose(() => {
        const count = step('count');
        if (count) count.textContent = String(rating);
      });
    });
  }

  window.CMUWalkout = { available: true, play, stadium };
})();
