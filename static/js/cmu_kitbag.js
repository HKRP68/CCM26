/*
 * CMU Kit Bag — Phase 2
 * ═══════════════════════════════════════════════════════════════════════════
 * The Free Pack opening: an original CricMaster Ultra cricket kit bag whose
 * body, handles, zipper and flap are four separately animated groups.
 *
 * Presentation only. The bag is shown a player the server has already granted
 * and recorded; it never asks for one, never decides which one, and resolves
 * its promise exactly once however the sequence ends — played, skipped,
 * interrupted or failed — so the caller always goes on to render the card.
 *
 * Artwork is drawn here rather than fetched, which removes a whole class of
 * failure: there is no asset request to time out, 404 or get blocked by a
 * regional CDN filter, and the bag inherits the app's theme tokens, so dark
 * and light both work with no JavaScript branch.
 *
 * Sequence: appear and float → shake → zipper runs and the flap lifts →
 * the confirmed card rises out → rarity effects → details and actions.
 *
 * Exposed as window.CMUKitBag.
 */
(() => {
  'use strict';

  const FX = window.CMUAnim;
  if (!FX) {
    window.CMUKitBag = {
      available: false,
      art: () => '',
      mount: () => () => {},
      open: () => Promise.resolve('unavailable'),
    };
    return;
  }

  /* ════════════════════════ Original kit bag artwork ════════════════════════
   * Colours come from CSS so the bag follows the app theme; the SVG only
   * carries structure and class names. The four animated groups are
   * .cmu-kb-body, .cmu-kb-handles, .cmu-kb-zip and .cmu-kb-flap.
   */
  function art({ title = 'CricMaster Ultra kit bag' } = {}) {
    const id = FX.uid('cmukb');
    return `<svg class="cmu-kb-svg" viewBox="0 0 184 148" role="img" aria-label="${FX.esc(title)}"
  xmlns="http://www.w3.org/2000/svg" focusable="false">
  <defs>
    <linearGradient id="${id}shell" class="kb-g-shell" x1="0" y1="0" x2=".35" y2="1">
      <stop class="kb-s1" offset="0"/><stop class="kb-s2" offset=".55"/><stop class="kb-s3" offset="1"/>
    </linearGradient>
    <linearGradient id="${id}lid" class="kb-g-lid" x1="0" y1="0" x2=".2" y2="1">
      <stop class="kb-l1" offset="0"/><stop class="kb-l2" offset="1"/>
    </linearGradient>
    <linearGradient id="${id}metal" class="kb-g-metal" x1="0" y1="0" x2="1" y2="0">
      <stop class="kb-m1" offset="0"/><stop class="kb-m2" offset=".5"/><stop class="kb-m3" offset="1"/>
    </linearGradient>
  </defs>

  <ellipse class="kb-shadow" cx="92" cy="140" rx="62" ry="6"/>

  <!-- ░░ body: shell, end gusset, panels, pocket and badge ░░ -->
  <g class="cmu-kb-body">
    <path class="kb-gusset" d="M150 48 l18 9 -3 62 -17 11z"/>
    <path class="kb-shell" fill="url(#${id}shell)"
          d="M22 54q0-10 11-11h118q11 1 12 11l4 61q1 11-10 14l-16 5H40l-14-6q-9-4-8-13z"/>
    <path class="kb-seam" d="M28 62q0-8 9-8h108q8 0 9 8l4 54q0 7-8 10l-13 4H42l-11-5q-7-3-7-9z"/>
    <path class="kb-stripe" d="M44 56h11l7 78H51z"/>
    <path class="kb-stripe kb-stripe-alt" d="M62 56h5l7 78h-5z"/>
    <path class="kb-stripe" d="M118 56h10l6 78h-10z"/>
    <!-- front pocket with its own short zip -->
    <path class="kb-pocket" d="M38 92q0-6 7-6h62q7 0 8 6l2 22q0 6-7 7l-60 2q-7 0-8-6z"/>
    <path class="kb-zipline" d="M43 95h71"/>
    <rect class="kb-badge" x="68" y="100" width="48" height="16" rx="4"/>
    <text class="kb-wordmark" x="92" y="108" text-anchor="middle"
          textLength="36" lengthAdjust="spacingAndGlyphs">CMU</text>
    <text class="kb-submark" x="92" y="113.6" text-anchor="middle"
          textLength="40" lengthAdjust="spacingAndGlyphs">PLAYER KIT</text>
    <path class="kb-piping" d="M30 66l-4 48q-1 7 5 10"/>
    <path class="kb-piping" d="M154 60l5 56"/>
  </g>

  <!-- ░░ handles: two straps meeting in a bound grip ░░ -->
  <g class="cmu-kb-handles">
    <path class="kb-strap" d="M66 46V32q0-13 26-13t26 13v14"/>
    <path class="kb-strap kb-strap-light" d="M67 45V33q0-11 25-11t25 11v12"/>
    <rect class="kb-grip" x="80" y="22" width="24" height="9" rx="4.5"/>
    <path class="kb-grip-bind" d="M86 22v9M98 22v9"/>
  </g>

  <!-- ░░ flap: the hinged lid, origin set in CSS to the hinge line ░░ -->
  <g class="cmu-kb-flap">
    <path class="kb-lid" fill="url(#${id}lid)"
          d="M24 52q0-13 14-15h108q14 2 15 15l-7 15q-2 4-7 3L31 66q-7-1-7-9z"/>
    <path class="kb-lid-inner" d="M36 50q2-6 10-6h92q8 0 10 6l-3 7-106-1z"/>
    <path class="kb-lid-edge" d="M30 58h124"/>
  </g>

  <!-- ░░ zipper: track, teeth and a slider that runs the length ░░ -->
  <g class="cmu-kb-zip">
    <path class="kb-zip-track" d="M32 57h120" stroke="url(#${id}metal)"/>
    <path class="kb-zip-teeth" d="M32 57h120"/>
    <g class="cmu-kb-slider">
      <rect class="kb-slider-body" x="-5" y="52.5" width="12" height="9" rx="2.4"/>
      <path class="kb-slider-pull" d="M1 61.5v6"/>
      <circle class="kb-slider-ring" cx="1" cy="69" r="2.6"/>
    </g>
  </g>
</svg>`;
  }

  /* ════════════════════════ Mounting ════════════════════════ */

  /**
   * Dress the opening button as a kit bag.
   *
   * Mirrors the pack theatre's mount(): it replaces whatever is in
   * [data-cmu-pack-art] and wires swipe-up as a second way to open, while the
   * button's own click handler stays untouched so there is still exactly one
   * open path to guard against a double tap.
   */
  function mount(button, { onOpen = null } = {}) {
    if (!button) return () => {};
    const slot = button.querySelector('[data-cmu-pack-art]') || button.querySelector('.pack-foil');
    if (!slot) return () => {};

    const holder = document.createElement('span');
    holder.className = 'cmu-kb-hero';
    holder.setAttribute('data-cmu-pack-art', '');
    holder.innerHTML = `<span class="cmu-kb-stage-art">${art()}</span>
      <span class="cmu-kb-card" data-cmu-kb-card hidden></span>`;
    slot.replaceWith(holder);
    button.classList.add('cmu-kb-mounted');

    let startY = null;
    let startX = null;
    let fired = false;

    const onStart = (event) => {
      if (button.disabled) return;
      const touch = event.touches ? event.touches[0] : event;
      startY = touch.clientY;
      startX = touch.clientX;
      fired = false;
    };
    const onMove = (event) => {
      if (startY === null || fired || button.disabled) return;
      const touch = event.touches ? event.touches[0] : event;
      const dy = startY - touch.clientY;
      const dx = Math.abs(startX - touch.clientX);
      if (dy > 34 && dy > dx) {
        fired = true;
        if (onOpen) onOpen('swipe');
        else button.click();
      } else if (dy > 4 && dy > dx) {
        holder.style.setProperty('--cmu-kb-pull', `${Math.min(dy, 30)}px`);
      }
    };
    const onEnd = () => {
      startY = null;
      startX = null;
      holder.style.removeProperty('--cmu-kb-pull');
    };

    const events = [['touchstart', onStart], ['touchmove', onMove], ['touchend', onEnd],
      ['touchcancel', onEnd], ['pointerdown', onStart], ['pointermove', onMove],
      ['pointerup', onEnd], ['pointercancel', onEnd]];
    for (const [type, fn] of events) {
      button.addEventListener(type, fn, type.startsWith('touch') ? { passive: true } : undefined);
    }
    return () => {
      for (const [type, fn] of events) button.removeEventListener(type, fn);
      button.classList.remove('cmu-kb-mounted');
    };
  }

  /* ════════════════════════ The opening sequence ════════════════════════ */

  /**
   * Run the kit bag open.
   *
   * @param {HTMLElement} button   the opening button the bag is mounted on
   * @param {HTMLElement} surface  the overlay, for rays and the light burst
   * @param {object} opts
   *   rating      {number} confirmed OVR — drives the rarity effects only
   *   cardMarkup  {string} the confirmed card, already rendered and escaped by
   *                        the caller; the bag only lifts it into view
   *   status      {function} optional callback for the live status line
   *   skip        {boolean} jump straight to the end state
   * @returns {Promise<'played'|'skipped'|'instant'|'failed'>}
   */
  async function open(button, surface, {
    rating = 0, cardMarkup = '', status = null, skip = false,
  } = {}) {
    const say = (text) => { if (status) { try { status(text); } catch (_) {} } };
    const rarity = FX.rarityOf(rating);

    if (!button || skip || FX.isOff()) {
      button?.classList.add('is-unzipped', 'is-lifted');
      return skip ? 'skipped' : 'instant';
    }

    try {
      const holder = button.querySelector('[data-cmu-pack-art]');
      const cardSlot = holder?.querySelector('[data-cmu-kb-card]');
      if (cardSlot && cardMarkup) {
        cardSlot.innerHTML = cardMarkup;
        cardSlot.hidden = false;
      }
      const rays = surface?.querySelector('.cmu-fx-rays');
      const brief = FX.isMinimal();

      // 1 · the bag is set down and settles
      button.classList.add('is-settling');
      say('Your kit bag has arrived…');
      await FX.beat(brief ? 100 : 420);
      button.classList.remove('is-settling');

      // 2 · something inside wants out
      button.classList.add('is-shaking');
      FX.haptic('medium');
      FX.sound('kitbag_shake');
      say('Something is moving inside…');
      await FX.beat(brief ? 140 : 620);
      button.classList.remove('is-shaking');

      // 3 · the slider runs the length of the zip, then the flap hinges back
      button.classList.add('is-unzipping');
      FX.haptic('light');
      FX.sound('kitbag_zip');
      say('Unzipping…');
      await FX.beat(brief ? 120 : 560);
      button.classList.add('is-unzipped');
      await FX.beat(brief ? 80 : 300);

      // 4 · the confirmed card rises out
      button.classList.add('is-lifting');
      rays?.classList.add('is-lit');
      say('Your player is here');
      const effects = [];
      if (rarity === 'legendary' || rarity === 'mythic' || rarity === 'epic') {
        effects.push(FX.lightBurst(surface || document.body, {
          tier: rarity === 'epic' ? 'epic' : 'legend', hold: 620,
        }));
        effects.push(FX.particles(holder || button, {
          count: rarity === 'epic' ? 24 : 34,
          tier: rarity === 'epic' ? 'epic' : 'legend',
          spread: 180,
        }));
        FX.notify('success');
      } else {
        effects.push(FX.particles(holder || button, { count: 14, spread: 130 }));
        FX.haptic('heavy');
      }
      await FX.beat(brief ? 160 : 900);
      button.classList.add('is-lifted');
      Promise.allSettled(effects);
      return 'played';
    } catch (error) {
      // A cosmetic failure must cost the animation, never the player.
      if (window.console) console.warn('[CMUKitBag] opening failed safely', error);
      button?.classList.add('is-unzipped', 'is-lifted');
      return 'failed';
    }
  }

  /** Put the bag back to its closed state for the next opening. */
  function reset(button) {
    if (!button) return;
    button.classList.remove('is-settling', 'is-shaking', 'is-unzipping',
      'is-unzipped', 'is-lifting', 'is-lifted');
    const slot = button.querySelector('[data-cmu-kb-card]');
    if (slot) {
      slot.hidden = true;
      slot.replaceChildren();
    }
    button.querySelector('[data-cmu-pack-art]')?.style.removeProperty('--cmu-kb-pull');
  }

  window.CMUKitBag = { available: true, art, mount, open, reset };
})();
