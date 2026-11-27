/*
 * CMU Rewards FX — Phase 4
 * ═══════════════════════════════════════════════════════════════════════════
 * The reusable pieces every reward and progression surface shares: animated
 * counters that know what they were showing before, progress meters, reward
 * chests, rank changes, level-ups and celebration beats.
 *
 * Presentation only, and the rule that matters most in this file:
 *
 *   NOTHING HERE GRANTS ANYTHING. No callback in this module credits a
 *   balance, claims a quest, opens a gift or awards XP. Each entry point is
 *   handed numbers the server has already settled and animates towards them.
 *   A counter interrupted halfway is written to its confirmed value; a chest
 *   that never opens still leaves the reward in the DOM behind it. The engine
 *   has no network access, so it could not re-request a reward even if a
 *   future caller asked it to.
 *
 * Exposed as window.CMURewardsFX.
 */
(() => {
  'use strict';

  const FX = window.CMUAnim;
  if (!FX) {
    const noop = () => Promise.resolve();
    window.CMURewardsFX = {
      available: false,
      setCount: (el, v) => { if (el) el.textContent = String(v); },
      wallet: noop, meter: noop, chest: noop, celebrate: noop,
      rankChange: noop, levelUp: noop, stagger: () => {}, pop: () => {},
    };
    return;
  }

  const groupFormat = (value) => {
    try { return Number(value).toLocaleString(); } catch (_) { return String(value); }
  };

  /** Parse what a counter element is currently showing, so a refresh animates
   *  from the old balance rather than from zero every time. */
  function readCurrent(el) {
    if (!el) return 0;
    const stored = Number(el.dataset.cmuValue);
    if (Number.isFinite(stored)) return stored;
    const text = String(el.textContent || '').replace(/[^\d.-]/g, '');
    const parsed = Number(text);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  /**
   * Set a counter to `value`, animating from whatever it showed before.
   *
   * The confirmed value is written to data-cmu-value first, so it is the
   * element's truth from this moment on even if the animation is cut short by
   * a re-render, a navigation or the app being backgrounded.
   *
   * @returns {Promise<number>} the confirmed value
   */
  function setCount(el, value, { format = groupFormat, duration = 760, prefix = '' } = {}) {
    if (!el) return Promise.resolve(Number(value) || 0);
    const target = Number(value) || 0;
    const from = readCurrent(el);
    el.dataset.cmuValue = String(target);
    const render = (n) => `${prefix}${format(n)}`;
    if (from === target || FX.isOff()) {
      el.textContent = render(target);
      return Promise.resolve(target);
    }
    el.classList.add('cmu-fx-count');
    el.classList.toggle('is-up', target > from);
    el.classList.toggle('is-down', target < from);
    return FX.countUp(el, target, { from, duration, format: render }).then((landed) => {
      el.classList.remove('is-up', 'is-down');
      return landed;
    });
  }

  /**
   * Animate a set of wallet counters together and float the change above the
   * first one that moved.
   *
   * @param {object} fields  { elementId|Element: confirmedValue }
   */
  function wallet(fields = {}, { float = true, format = groupFormat } = {}) {
    const jobs = [];
    let floated = false;
    for (const [key, value] of Object.entries(fields)) {
      const el = typeof key === 'string' ? document.getElementById(key) : key;
      if (!el) continue;
      const before = readCurrent(el);
      const after = Number(value) || 0;
      jobs.push(setCount(el, after, { format }));
      if (float && !floated && after !== before && before !== 0) {
        floated = true;
        floatDelta(el, after - before);
      }
    }
    return Promise.all(jobs);
  }

  /** A "+1,200" that rises off a counter and fades. Decorative; the counter
   *  underneath carries the real number. */
  function floatDelta(el, delta) {
    if (!el || !delta || FX.isOff() || FX.isMinimal()) return;
    const host = el.offsetParent || el.parentElement;
    if (!host) return;
    const chip = document.createElement('span');
    chip.className = `cmu-fx-delta ${delta > 0 ? 'is-up' : 'is-down'}`;
    chip.setAttribute('aria-hidden', 'true');
    chip.textContent = `${delta > 0 ? '+' : '−'}${groupFormat(Math.abs(delta))}`;
    // The host may be statically positioned; anchoring without moving it keeps
    // every existing layout intact.
    const anchor = document.createElement('span');
    anchor.className = 'cmu-fx-delta-anchor';
    anchor.appendChild(chip);
    el.insertAdjacentElement('afterend', anchor);
    setTimeout(() => anchor.remove(), FX.ms(1400) + 200);
  }

  /**
   * Fill a progress meter to `ratio` (0..1).
   *
   * Accepts the app's own <progress> elements and any element carrying
   * .cmu-fx-meter or a --cmu-fill width, so existing markup can be animated
   * without being rewritten.
   */
  function meter(el, ratio, { duration = 680, complete = false } = {}) {
    if (!el) return Promise.resolve();
    el.classList.toggle('is-complete', Boolean(complete));
    return FX.progressTo(el, ratio, { duration });
  }

  /** Fill every quest/achievement meter inside `scope` from its own data. */
  function meters(scope, selector = '[data-cmu-meter]') {
    const nodes = Array.from((scope || document).querySelectorAll(selector));
    return Promise.all(nodes.map((node, index) => {
      const ratio = Number(node.dataset.cmuMeter);
      const delay = FX.ms(Math.min(index, 6) * 60);
      return FX.sleep(delay).then(() => meter(node, Number.isFinite(ratio) ? ratio : 0, {
        complete: node.dataset.cmuMeterComplete === '1',
      }));
    }));
  }

  /* ════════════════════════ Reward chest ════════════════════════ */

  const CHEST_SVG = (() => {
    // One original chest, drawn once per call with unique gradient ids.
    return () => {
      const id = FX.uid('cmuch');
      return `<svg class="cmu-chest-svg" viewBox="0 0 120 96" aria-hidden="true" focusable="false"
  xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="${id}a" x1="0" y1="0" x2=".3" y2="1">
      <stop class="ch-a1" offset="0"/><stop class="ch-a2" offset="1"/>
    </linearGradient>
    <linearGradient id="${id}b" x1="0" y1="0" x2="1" y2="1">
      <stop class="ch-b1" offset="0"/><stop class="ch-b2" offset=".5"/><stop class="ch-b3" offset="1"/>
    </linearGradient>
  </defs>
  <ellipse class="ch-shadow" cx="60" cy="90" rx="42" ry="4"/>
  <g class="cmu-chest-lid">
    <path class="ch-shell" fill="url(#${id}a)" d="M16 44V34q0-18 44-18t44 18v10z"/>
    <path class="ch-band" fill="url(#${id}b)" d="M52 17h16v27H52z"/>
    <path class="ch-rim" d="M16 44h88"/>
  </g>
  <g class="cmu-chest-base">
    <path class="ch-shell" fill="url(#${id}a)" d="M16 46h88v30q0 8-8 8H24q-8 0-8-8z"/>
    <path class="ch-band" fill="url(#${id}b)" d="M52 46h16v38H52z"/>
    <rect class="ch-lock" x="53" y="46" width="14" height="13" rx="3"/>
    <circle class="ch-keyhole" cx="60" cy="52" r="2.2"/>
  </g>
  <g class="cmu-chest-light"><rect x="18" y="38" width="84" height="12" rx="6"/></g>
</svg>`;
    };
  })();

  /**
   * Present a confirmed reward in a chest the player opens.
   *
   * The reward markup is in the DOM from the moment the overlay opens — behind
   * the chest, but present — so Skip, Escape, a teardown or a thrown animation
   * all leave it on screen. The chest is a lid, not a gate.
   *
   * @param {object} opts
   *   title      {string} heading
   *   rewardHtml {string} the confirmed reward, already rendered and escaped
   *   tier       {string} '' | 'rare' | 'epic' | 'legend'
   *   autoOpen   {boolean} open without waiting for a tap
   *   doneLabel  {string}
   * @returns {Promise<'done'|'skipped'|'failed'>}
   */
  function chest({
    title = 'Reward claimed', rewardHtml = '', tier = '',
    autoOpen = false, doneLabel = 'Collect', eyebrow = '',
  } = {}) {
    return new Promise((resolve) => {
      let settled = false;
      const finish = (reason) => {
        if (settled) return;
        settled = true;
        release();
        resolve(reason);
      };

      let overlay;
      try {
        overlay = FX.overlay({
          className: `cmu-chest-overlay cmu-tier-${tier || 'base'}`,
          label: title,
          html: `<div class="cmu-fx-panel cmu-chest-panel">
            ${eyebrow ? `<span class="cmu-fx-eyebrow">${FX.esc(eyebrow)}</span>` : ''}
            <h3 class="cmu-chest-title">${FX.esc(title)}</h3>
            <button type="button" class="cmu-chest-stage" data-chest-open
              aria-label="Open the reward chest">${CHEST_SVG()}</button>
            <p class="cmu-chest-hint" data-chest-hint>Tap to open</p>
            <div class="cmu-chest-reward" data-chest-reward>${rewardHtml}</div>
            <div class="cmu-fx-actions">
              <button type="button" class="app-primary" data-chest-done>${FX.esc(doneLabel)}</button>
            </div>
          </div>`,
          onClose: (reason) => finish(reason === 'done' ? 'done' : 'skipped'),
        });
      } catch (error) {
        if (window.console) console.warn('[CMURewardsFX] chest could not open safely', error);
        resolve('failed');
        return;
      }

      const release = FX.onTeardown(() => overlay.close('teardown'));
      const root = overlay.root;
      const stage = root.querySelector('[data-chest-open]');
      const hint = root.querySelector('[data-chest-hint]');
      const reward = root.querySelector('[data-chest-reward]');
      let opened = false;

      const openIt = () => {
        if (opened || overlay.closed) return;
        opened = true;
        stage.disabled = true;
        stage.setAttribute('aria-hidden', 'true');
        stage.tabIndex = -1;
        if (hint) hint.hidden = true;
        root.classList.add('is-open');
        FX.haptic(tier === 'legend' ? 'heavy' : 'medium');
        FX.notify('success');
        FX.sound('chest_open');
        FX.particles(stage, { count: tier === 'legend' ? 34 : 22, tier });
        if (tier) FX.confetti(tier === 'legend' ? 56 : 40, tier, root);
        reward?.classList.add('is-on');
        // Counters inside the reward markup animate once it is visible.
        meters(reward);
        root.querySelector('[data-chest-done]')?.focus();
      };

      stage.addEventListener('click', openIt);
      root.querySelector('[data-chest-done]').addEventListener('click', () => {
        // Tapping Collect before opening still opens, so the player sees what
        // they already have rather than closing on an unopened chest.
        if (!opened) { openIt(); return; }
        overlay.close('done');
      });

      if (autoOpen || FX.isOff() || FX.isMinimal()) {
        setTimeout(openIt, FX.ms(260));
      } else {
        // Never strand a confirmed reward behind an unopened lid.
        setTimeout(() => { if (!overlay.closed) openIt(); }, 7000);
      }
    });
  }

  /* ════════════════════════ Rank and level ════════════════════════ */

  /**
   * Announce a rank movement. `from` may be null for a first placement.
   * Both numbers come from the server; this only decides up, down or steady.
   */
  function rankChange(el, { from = null, to = null, label = 'Rank' } = {}) {
    if (!el || to == null) return Promise.resolve();
    const before = Number(from);
    const after = Number(to);
    // A smaller rank number is a better position, so the arrow is inverted
    // relative to the arithmetic.
    const improved = Number.isFinite(before) && after < before;
    const worsened = Number.isFinite(before) && after > before;
    el.classList.remove('is-up', 'is-down');
    el.classList.add('cmu-fx-rank');
    if (improved) el.classList.add('is-up');
    if (worsened) el.classList.add('is-down');
    el.setAttribute('aria-label',
      `${label} ${after}${improved ? `, up from ${before}` : worsened ? `, down from ${before}` : ''}`);
    const number = el.querySelector('[data-cmu-rank-value]') || el;
    if (improved) { FX.haptic('medium'); FX.sound('rank_up'); }
    return setCount(number, after, { prefix: '#', format: (n) => String(n) });
  }

  /**
   * Community XP level-up. Fills the meter to full, then announces the new
   * level. Both the level and its title come from the server's profile.
   */
  async function levelUp(meterEl, { level = null, title = '', host = null } = {}) {
    if (meterEl) await meter(meterEl, 1, { complete: true });
    if (level == null) return;
    const target = host || meterEl?.parentElement;
    if (!target) return;
    const badge = document.createElement('div');
    badge.className = 'cmu-fx-levelup';
    badge.setAttribute('role', 'status');
    badge.innerHTML = `<strong>Level ${FX.esc(level)}</strong>${title ? `<span>${FX.esc(title)}</span>` : ''}`;
    target.appendChild(badge);
    FX.notify('success');
    FX.sound('level_up');
    FX.particles(target, { count: 24, tier: 'epic' });
    await FX.sleep(FX.ms(2400) + 400);
    badge.classList.add('is-leaving');
    await FX.sleep(300);
    badge.remove();
  }

  /* ════════════════════════ Small shared beats ════════════════════════ */

  /** Confetti + haptics for a celebration, scaled to tier. */
  function celebrate(tier = '', surface = document.body) {
    const counts = { legend: 80, epic: 60, rare: 45 };
    FX.notify('success');
    return FX.confetti(counts[tier] || 32, tier, surface);
  }

  /** Stagger a freshly rendered list in. Cheap and safe on any markup. */
  function stagger(scope, selector, { step = 55, max = 8 } = {}) {
    if (FX.isOff()) return;
    const nodes = Array.from((scope || document).querySelectorAll(selector));
    nodes.forEach((node, index) => {
      node.style.setProperty('--cmu-delay', `${Math.min(index, max) * step}ms`);
      node.classList.add('cmu-fx-rise');
    });
  }

  /** A one-shot attention pop, used when a row changes state in place. */
  function pop(el, { tier = '' } = {}) {
    if (!el || FX.isOff()) return;
    el.classList.remove('cmu-fx-pop');
    FX.reflow(el);
    el.classList.add('cmu-fx-pop');
    if (tier) FX.particles(el, { count: 16, tier });
  }

  window.CMURewardsFX = {
    available: true,
    setCount,
    wallet,
    floatDelta,
    meter,
    meters,
    chest,
    rankChange,
    levelUp,
    celebrate,
    stagger,
    pop,
  };
})();
