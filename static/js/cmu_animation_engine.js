/*
 * CMU Animation Engine — core
 * ═══════════════════════════════════════════════════════════════════════════
 * A presentation-only motion layer for the CricMaster Ultra Mini App.
 *
 * Contract, and the reason this file exists at all:
 *
 *   1. NOTHING here decides, grants, alters or re-requests a reward. Every
 *      card, coin, gem and match event shown through this engine is data the
 *      server has already confirmed. There is no `fetch` in this module and
 *      there never should be: the engine cannot grant anything because it
 *      cannot talk to the server.
 *   2. Every sequence can be skipped, interrupted, or fail outright, and the
 *      caller still shows the confirmed result. Each entry point resolves a
 *      promise exactly once — on completion, on skip, on error, on teardown.
 *      Callers await it and then render the reward the normal way.
 *   3. Motion level is a user setting layered on top of the OS
 *      prefers-reduced-motion preference. "off" means the engine renders the
 *      end state immediately rather than rendering nothing.
 *
 * Exposed as window.CMUAnim. Loaded before any feature module that uses it.
 */
(() => {
  'use strict';

  // ═══════════════════ Settings ═══════════════════
  const STORE_KEY = 'cmu-animation-settings';
  // Legacy keys the engine keeps honouring so a player who turned haptics off
  // in Appearance before this engine existed does not get them back.
  const LEGACY_HAPTIC_KEY = 'cmu_haptic_feedback';

  const LEVELS = ['cinematic', 'standard', 'minimal', 'off'];

  // Duration multiplier per level. "minimal" keeps a short confirmation beat so
  // a reveal still reads as a reveal; "off" collapses to a single frame.
  const LEVEL_SCALE = { cinematic: 1, standard: 0.68, minimal: 0.26, off: 0 };
  // How much decoration each level allows. Particle count is multiplied by this
  // and separately gated by the particles toggle.
  const LEVEL_PARTICLES = { cinematic: 1, standard: 0.6, minimal: 0, off: 0 };

  const DEFAULTS = {
    level: 'cinematic',
    sound: true,
    haptics: true,
    particles: true,
    // "Fast play" collapses queued match overlays without touching the rest of
    // the app's motion. Separate from `level` on purpose: a player can want a
    // cinematic pack opening and a quick match.
    fastPlay: false,
  };

  const reduceQuery = window.matchMedia
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : { matches: false, addEventListener() {}, addListener() {} };

  let settings = { ...DEFAULTS };
  const listeners = new Set();

  function readStore() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') {
          if (LEVELS.includes(parsed.level)) settings.level = parsed.level;
          for (const key of ['sound', 'haptics', 'particles', 'fastPlay']) {
            if (typeof parsed[key] === 'boolean') settings[key] = parsed[key];
          }
        }
      }
      // A pre-engine haptics opt-out wins on first load, because the player
      // made that choice explicitly and nothing has asked them since.
      if (raw === null && localStorage.getItem(LEGACY_HAPTIC_KEY) === 'off') {
        settings.haptics = false;
      }
    } catch (_) {
      // Private mode / blocked storage. Defaults apply for this session.
    }
  }

  function writeStore() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(settings));
      // Keep the old key in step so miniapp_theme.js and the Appearance form
      // agree with the engine either way round.
      localStorage.setItem(LEGACY_HAPTIC_KEY, settings.haptics ? 'on' : 'off');
    } catch (_) {
      return false;
    }
    return true;
  }

  readStore();

  /** Effective motion level: the user's choice, floored by the OS preference.
   *  A system reduced-motion request can only reduce motion, never add it. */
  function level() {
    if (reduceQuery.matches) {
      return settings.level === 'off' ? 'off' : 'minimal';
    }
    return settings.level;
  }

  const isOff = () => level() === 'off';
  const isMinimal = () => { const l = level(); return l === 'off' || l === 'minimal'; };
  const isCinematic = () => level() === 'cinematic';

  /** Scale a design duration (ms) to the active motion level. */
  function ms(base) {
    const scaled = Math.round(Number(base || 0) * LEVEL_SCALE[level()]);
    return isOff() ? 0 : scaled;
  }

  /** Particle count for the active level, 0 when particles are off. */
  function particleBudget(count) {
    if (!settings.particles) return 0;
    return Math.round(Number(count || 0) * LEVEL_PARTICLES[level()]);
  }

  function notifyChange() {
    applyRootFlags();
    for (const fn of listeners) {
      try { fn(settings, level()); } catch (_) { /* a bad listener is not fatal */ }
    }
  }

  /** Mirror the settings onto <html> so CSS can branch without JS per element. */
  function applyRootFlags() {
    const root = document.documentElement;
    root.dataset.cmuMotion = level();
    root.dataset.cmuParticles = settings.particles && particleBudget(1) > 0 ? 'on' : 'off';
    root.style.setProperty('--cmu-motion-scale', String(LEVEL_SCALE[level()]));
  }

  function set(key, value) {
    if (key === 'level') {
      if (!LEVELS.includes(value)) return false;
      settings.level = value;
    } else if (key in DEFAULTS) {
      settings[key] = Boolean(value);
    } else {
      return false;
    }
    const persisted = writeStore();
    notifyChange();
    return persisted;
  }

  function setAll(patch) {
    let persisted = true;
    for (const [key, value] of Object.entries(patch || {})) {
      if (key === 'level') {
        if (LEVELS.includes(value)) settings.level = value;
      } else if (key in DEFAULTS) {
        settings[key] = Boolean(value);
      }
    }
    persisted = writeStore();
    notifyChange();
    return persisted;
  }

  applyRootFlags();
  const onReduceChange = () => notifyChange();
  if (reduceQuery.addEventListener) reduceQuery.addEventListener('change', onReduceChange);
  else reduceQuery.addListener?.(onReduceChange);

  // ═══════════════════ Small helpers ═══════════════════
  let uidCounter = 0;
  /** Unique id suffix. SVG gradient/clip ids must not collide between
   *  instances or the second copy inherits the first one's paint server. */
  const uid = (prefix = 'cmu') => `${prefix}${++uidCounter}-${Date.now().toString(36).slice(-4)}`;

  const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ESC_MAP[c]);

  const sleep = (duration) => new Promise((resolve) => {
    const wait = Math.max(0, Number(duration) || 0);
    if (!wait) { resolve(); return; }
    setTimeout(resolve, wait);
  });

  /** Sleep in engine time — already scaled to the motion level. */
  const beat = (base) => sleep(ms(base));

  const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));

  /** Force a style recalculation so a re-added animation class restarts. */
  const reflow = (el) => { if (el) void el.offsetWidth; };

  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

  // ═══════════════════ Feedback bridges ═══════════════════
  const telegram = () => window.Telegram?.WebApp;

  function haptic(style = 'light') {
    if (!settings.haptics) return;
    try { telegram()?.HapticFeedback?.impactOccurred(style); } catch (_) {}
  }

  function notify(type = 'success') {
    if (!settings.haptics) return;
    try { telegram()?.HapticFeedback?.notificationOccurred(type); } catch (_) {}
  }

  // Sound is intentionally a thin registry rather than a bundled asset set:
  // the arena already owns the audio files and its own unlock dance, so the
  // engine only decides *whether* a cue is allowed and forwards it.
  let soundSink = null;
  function registerSoundSink(fn) { soundSink = typeof fn === 'function' ? fn : null; }
  function sound(key) {
    if (!settings.sound || !soundSink || isOff()) return;
    try { soundSink(key); } catch (_) {}
  }

  // ═══════════════════ Animation queue ═══════════════════
  /*
   * Match overlays arrive faster than they can be watched: a tap can reveal a
   * ball while the previous ball's overlay is still on screen, and an autoplay
   * burst can deliver a whole over at once. The queue keeps those sequential
   * without ever becoming the thing the player is waiting on:
   *
   *   • capacity is bounded; past it the lowest-priority waiting task is
   *     dropped, so a long backlog cannot accumulate,
   *   • `drain()` empties the backlog instantly (fast-play, Skip),
   *   • a task that throws or hangs past its budget is abandoned, not awaited
   *     forever.
   */
  function createQueue(options = {}) {
    const capacity = Number(options.capacity) || 4;
    const taskTimeout = Number(options.taskTimeout) || 6000;
    const pending = [];
    let running = false;
    let draining = false;
    let generation = 0;

    function sortPending() {
      // Stable high-priority-first ordering.
      pending.sort((a, b) => (b.priority - a.priority) || (a.seq - b.seq));
    }

    let seq = 0;

    async function run() {
      if (running) return;
      running = true;
      try {
        while (pending.length) {
          const entry = pending.shift();
          const myGeneration = generation;
          if (entry.key && entry.superseded) { entry.settle(); continue; }
          try {
            const outcome = entry.task({
              // A task asks whether it should still bother with decoration.
              get skipped() { return draining || myGeneration !== generation; },
            });
            // Never let one sequence stall the queue: bound every task.
            await Promise.race([
              Promise.resolve(outcome),
              sleep(taskTimeout).then(() => 'cmu-timeout'),
            ]);
          } catch (error) {
            if (window.console) console.warn('[CMUAnim] queued animation failed safely', error);
          }
          entry.settle();
        }
      } finally {
        running = false;
        draining = false;
      }
    }

    /** Queue a task. Resolves when the task has run (or was dropped). */
    function push(task, { priority = 0, key = '' } = {}) {
      if (typeof task !== 'function') return Promise.resolve();
      let settle;
      const done = new Promise((resolve) => { settle = resolve; });
      const entry = { task, priority, key, seq: seq++, settle, superseded: false };
      if (key) {
        // Same key already waiting → the newer state wins; the old one is
        // dropped rather than played to an audience that has moved on.
        for (const queued of pending) {
          if (queued.key === key) queued.superseded = true;
        }
      }
      pending.push(entry);
      sortPending();
      while (pending.length > capacity) {
        // Drop from the tail: lowest priority, newest among equals.
        const dropped = pending.pop();
        dropped.settle();
      }
      run();
      return done;
    }

    /** Abandon the backlog. In-flight tasks see `ctx.skipped === true`. */
    function drain() {
      draining = true;
      generation += 1;
      while (pending.length) pending.pop().settle();
    }

    return {
      push,
      drain,
      get size() { return pending.length + (running ? 1 : 0); },
      get busy() { return running; },
    };
  }

  // One shared queue for match overlays; feature modules may make their own.
  const matchQueue = createQueue({ capacity: 3, taskTimeout: 5000 });

  // ═══════════════════ Overlay factory ═══════════════════
  /*
   * Every full-screen sequence in the engine uses this. It owns the things that
   * are easy to get wrong once per feature and impossible to get wrong here:
   * focus capture and restore, Escape, a Tab trap, body scroll lock, the
   * Telegram safe-area padding, aria wiring, and a close path that runs exactly
   * once however it is reached.
   */
  let openOverlays = 0;

  function overlay({
    className = '',
    label = 'Animation',
    html = '',
    describedBy = '',
    onClose = null,
    closeOnBackdrop = false,
    dismissLabel = '',
  } = {}) {
    const previousFocus = document.activeElement;
    const root = document.createElement('div');
    root.className = `cmu-fx-overlay ${className}`.trim();
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-label', label);
    if (describedBy) root.setAttribute('aria-describedby', describedBy);
    root.tabIndex = -1;
    root.innerHTML = `<div class="cmu-fx-backdrop"></div><div class="cmu-fx-stage">${html}</div>`;

    const stage = root.querySelector('.cmu-fx-stage');
    let closed = false;
    const closeHandlers = [];

    function focusables() {
      return Array.from(root.querySelectorAll(
        'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'
      )).filter((el) => el.getClientRects().length);
    }

    function onKeyDown(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        close('escape');
        return;
      }
      if (event.key !== 'Tab') return;
      const items = focusables();
      if (!items.length) { event.preventDefault(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    function close(reason = 'done') {
      if (closed) return;
      closed = true;
      root.removeEventListener('keydown', onKeyDown);
      root.classList.add('is-leaving');
      openOverlays = Math.max(0, openOverlays - 1);
      if (!openOverlays) document.body.classList.remove('cmu-fx-locked');
      const remove = () => {
        if (root.isConnected) root.remove();
        // Hand focus back where the player left it, or to the active screen
        // heading when that element has gone (a re-render during the overlay).
        const target = previousFocus?.isConnected && !previousFocus.disabled
          ? previousFocus
          : document.querySelector('.screen.active h2[tabindex="-1"]');
        try { target?.focus?.(); } catch (_) {}
      };
      if (ms(220)) setTimeout(remove, ms(220)); else remove();
      for (const fn of closeHandlers) {
        try { fn(reason); } catch (_) {}
      }
      if (onClose) {
        try { onClose(reason); } catch (_) {}
      }
    }

    root.addEventListener('keydown', onKeyDown);
    if (closeOnBackdrop) {
      root.querySelector('.cmu-fx-backdrop').addEventListener('click', () => close('backdrop'));
    }
    if (dismissLabel) {
      const dismiss = document.createElement('button');
      dismiss.type = 'button';
      dismiss.className = 'cmu-fx-dismiss';
      dismiss.textContent = dismissLabel;
      dismiss.addEventListener('click', () => close('dismiss'));
      root.appendChild(dismiss);
    }

    document.body.appendChild(root);
    openOverlays += 1;
    document.body.classList.add('cmu-fx-locked');
    requestAnimationFrame(() => root.classList.add('is-open'));
    try { root.focus({ preventScroll: true }); } catch (_) { root.focus(); }

    return {
      root,
      stage,
      close,
      get closed() { return closed; },
      onClose(fn) { if (typeof fn === 'function') closeHandlers.push(fn); },
      /** Replace the stage content, restarting its entry animation. */
      swap(markup) {
        if (closed) return null;
        stage.classList.remove('cmu-fx-stage-in');
        reflow(stage);
        stage.innerHTML = markup;
        stage.classList.add('cmu-fx-stage-in');
        return stage;
      },
      focusFirst() {
        const items = focusables();
        if (items.length) {
          try { items[0].focus(); } catch (_) {}
        }
      },
    };
  }

  // ═══════════════════ Reusable components ═══════════════════
  const groupFormat = (value) => {
    try { return Number(value).toLocaleString(); } catch (_) { return String(value); }
  };

  /** Count a number up into `el`. Resolves when it has landed on `to`.
   *  At motion level "off" it writes the final value and resolves. */
  function countUp(el, to, { from = 0, duration = 760, format = groupFormat } = {}) {
    const target = Number(to) || 0;
    if (!el) return Promise.resolve(target);
    const span = ms(duration);
    if (!span) {
      el.textContent = format(target);
      return Promise.resolve(target);
    }
    const start = Number(from) || 0;
    const t0 = performance.now();
    return new Promise((resolve) => {
      const tick = (now) => {
        if (!el.isConnected) { resolve(target); return; }
        const k = clamp((now - t0) / span, 0, 1);
        // Ease-out cubic: fast enough to read, slow enough to land.
        const eased = 1 - Math.pow(1 - k, 3);
        el.textContent = format(Math.round(start + (target - start) * eased));
        if (k < 1) requestAnimationFrame(tick);
        else { el.textContent = format(target); resolve(target); }
      };
      requestAnimationFrame(tick);
    });
  }

  /** Animate a progress bar to `ratio` (0..1). Works on any element whose
   *  width is driven by --cmu-fill, plus <progress> and [role=progressbar]. */
  function progressTo(el, ratio, { duration = 680 } = {}) {
    if (!el) return Promise.resolve();
    const value = clamp(Number(ratio) || 0, 0, 1);
    const pct = `${(value * 100).toFixed(2)}%`;
    const apply = () => {
      el.style.setProperty('--cmu-fill', pct);
      if (el.tagName === 'PROGRESS') { el.max = 1; el.value = value; }
      if (el.getAttribute('role') === 'progressbar') {
        el.setAttribute('aria-valuenow', String(Math.round(value * 100)));
      }
    };
    const span = ms(duration);
    if (!span) { el.style.setProperty('--cmu-fill-ms', '0ms'); apply(); return Promise.resolve(); }
    el.style.setProperty('--cmu-fill-ms', `${span}ms`);
    requestAnimationFrame(apply);
    return sleep(span);
  }

  const TIER_COLORS = {
    legend: ['#ffd966', '#ff8c00', '#fff2b3', '#ffb347'],
    legendary: ['#ffd966', '#ff8c00', '#fff2b3', '#ffb347'],
    epic: ['#a78bfa', '#7c5ce0', '#c4b5fd', '#ffd966'],
    elite: ['#a78bfa', '#7c5ce0', '#c4b5fd', '#ffd966'],
    rare: ['#4a9eff', '#91bef1', '#c9e2ff', '#ffd966'],
    '': ['#ffd966', '#f0c188', '#4a9eff', '#ff4d6d', '#a78bfa'],
  };

  const tierColors = (tier) => TIER_COLORS[String(tier || '')] || TIER_COLORS[''];

  /** Falling confetti over `surface`. Returns a promise that settles when the
   *  layer is gone, so a caller can await the celebration if it wants to. */
  function confetti(count = 60, tier = '', surface = document.body) {
    const pieces = particleBudget(count);
    const host = surface && surface.isConnected ? surface : document.body;
    if (!pieces) return Promise.resolve();
    const colors = tierColors(tier);
    const layer = document.createElement('div');
    layer.className = 'cmu-fx-confetti';
    layer.setAttribute('aria-hidden', 'true');
    let longest = 0;
    for (let i = 0; i < pieces; i++) {
      const piece = document.createElement('i');
      piece.style.background = colors[i % colors.length];
      piece.style.left = `${Math.random() * 100}%`;
      piece.style.borderRadius = Math.random() > 0.5 ? '50%' : '2px';
      const seconds = (1.5 + Math.random() * 1.6) * (LEVEL_SCALE[level()] || 1);
      const delay = Math.random() * 0.3;
      piece.style.animationDuration = `${seconds}s`;
      piece.style.animationDelay = `${delay}s`;
      piece.style.setProperty('--cmu-drift', `${(Math.random() * 2 - 1) * 90}px`);
      piece.style.setProperty('--cmu-spin', `${Math.round(Math.random() * 900 - 450)}deg`);
      piece.style.transform = `scale(${0.6 + Math.random() * 0.9})`;
      longest = Math.max(longest, (seconds + delay) * 1000);
      layer.appendChild(piece);
    }
    host.appendChild(layer);
    const lifetime = Math.min(4200, longest + 200);
    return sleep(lifetime).then(() => { if (layer.isConnected) layer.remove(); });
  }

  /** Radial particle burst from the centre of `host`. Cheap: one element per
   *  particle, one transform animation each, removed together. */
  function particles(host, { count = 26, colors = null, spread = 150, tier = '' } = {}) {
    const total = particleBudget(count);
    if (!host || !host.isConnected || !total) return Promise.resolve();
    const palette = colors || tierColors(tier);
    const layer = document.createElement('div');
    layer.className = 'cmu-fx-particles';
    layer.setAttribute('aria-hidden', 'true');
    for (let i = 0; i < total; i++) {
      const dot = document.createElement('i');
      const angle = (i / total) * Math.PI * 2 + Math.random() * 0.4;
      const distance = spread * (0.45 + Math.random() * 0.8);
      dot.style.background = palette[i % palette.length];
      dot.style.setProperty('--cmu-tx', `${Math.cos(angle) * distance}px`);
      dot.style.setProperty('--cmu-ty', `${Math.sin(angle) * distance}px`);
      const size = 4 + Math.random() * 6;
      dot.style.width = `${size}px`;
      dot.style.height = `${size}px`;
      dot.style.animationDuration = `${ms(520 + Math.random() * 520)}ms`;
      layer.appendChild(dot);
    }
    host.appendChild(layer);
    return sleep(ms(1100) + 80).then(() => { if (layer.isConnected) layer.remove(); });
  }

  /** A single controlled lighting burst centred on `host`. */
  function lightBurst(host, { tier = '', hold = 620 } = {}) {
    if (!host || !host.isConnected || isOff()) return Promise.resolve();
    const flash = document.createElement('div');
    flash.className = 'cmu-fx-burst';
    flash.setAttribute('aria-hidden', 'true');
    flash.style.setProperty('--cmu-burst', tierColors(tier)[0]);
    flash.style.animationDuration = `${ms(hold)}ms`;
    host.appendChild(flash);
    return sleep(ms(hold) + 60).then(() => { if (flash.isConnected) flash.remove(); });
  }

  /** Mark an element as a reward chest and play its open beat. */
  function openChest(el, { tier = '' } = {}) {
    if (!el) return Promise.resolve();
    el.classList.add('cmu-fx-chest');
    reflow(el);
    el.classList.add('is-opening');
    haptic('medium');
    const effects = [sleep(ms(520))];
    effects.push(particles(el, { count: 22, tier }));
    return Promise.all(effects).then(() => { el.classList.add('is-open'); });
  }

  /** Flip a two-faced card element (see .cmu-fx-flip in the stylesheet). */
  function flip(el) {
    if (!el || el.classList.contains('is-flipped')) return Promise.resolve();
    el.classList.add('is-flipped');
    return sleep(ms(560));
  }

  // ═══════════════════ Image safety ═══════════════════
  /*
   * Player artwork is 3:2 landscape and must never be cropped or stretched;
   * it also must never be the reason a reveal stalls. `artwork()` emits an
   * <img> that keeps its aspect ratio with object-fit: contain, and whose
   * failure collapses to a labelled placeholder instead of a broken icon.
   */
  function artwork(url, alt, { className = '', eager = false } = {}) {
    const safeUrl = String(url || '').trim();
    const safeAlt = esc(alt || 'Player card');
    if (!safeUrl) {
      return `<span class="cmu-fx-art-missing ${esc(className)}" role="img" aria-label="${safeAlt}">
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 5h16v14H4z" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="m7 15 3-3.5 2.5 2.5L15 11l2 4z" fill="currentColor"/></svg>
      </span>`;
    }
    return `<img class="cmu-fx-art ${esc(className)}" src="${esc(safeUrl)}" alt="${safeAlt}"
      loading="${eager ? 'eager' : 'lazy'}" decoding="async"
      onerror="window.CMUAnim&&window.CMUAnim.artFailed(this)">`;
  }

  /** onerror target for artwork(): swap a failed image for the placeholder. */
  function artFailed(img) {
    if (!img || !img.parentNode) return;
    const holder = document.createElement('span');
    holder.className = `cmu-fx-art-missing ${img.className.replace('cmu-fx-art', '').trim()}`;
    holder.setAttribute('role', 'img');
    holder.setAttribute('aria-label', img.alt || 'Player card');
    holder.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 5h16v14H4z" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="m7 15 3-3.5 2.5 2.5L15 11l2 4z" fill="currentColor"/></svg>';
    img.replaceWith(holder);
  }

  /** Wait for an image to settle, but never longer than `timeout`.
   *  A slow CDN must not hold a confirmed reward off the screen. */
  function waitForArt(scope, timeout = 1200) {
    const images = Array.from((scope || document).querySelectorAll('img.cmu-fx-art'));
    if (!images.length) return Promise.resolve();
    const settled = images.map((img) => (img.complete
      ? Promise.resolve()
      : new Promise((resolve) => {
        img.addEventListener('load', resolve, { once: true });
        img.addEventListener('error', resolve, { once: true });
      })));
    return Promise.race([Promise.all(settled), sleep(timeout)]);
  }

  // ═══════════════════ Rarity ═══════════════════
  /*
   * Rarity is cosmetic only. The thresholds are configurable so the look can be
   * retuned without touching any sequence, and so a deployment whose rating
   * curve differs can move them. Nothing downstream of these numbers changes
   * what a player received — only how loudly it is announced.
   */
  const RARITY = {
    thresholds: { mythic: 96, legendary: 91, epic: 85, rare: 80 },
    order: ['mythic', 'legendary', 'epic', 'rare', 'standard'],
    labels: {
      mythic: 'Mythic', legendary: 'Legendary', epic: 'Epic',
      rare: 'Rare', standard: 'Standard',
    },
    // OVR at or above which the full walkout sequence plays.
    walkoutFrom: 91,
  };

  function configureRarity(patch = {}) {
    if (patch.thresholds) Object.assign(RARITY.thresholds, patch.thresholds);
    if (Number.isFinite(patch.walkoutFrom)) RARITY.walkoutFrom = Number(patch.walkoutFrom);
    if (patch.labels) Object.assign(RARITY.labels, patch.labels);
    return { ...RARITY, thresholds: { ...RARITY.thresholds } };
  }

  function rarityOf(rating) {
    const ovr = Number(rating) || 0;
    const t = RARITY.thresholds;
    if (ovr >= t.mythic) return 'mythic';
    if (ovr >= t.legendary) return 'legendary';
    if (ovr >= t.epic) return 'epic';
    if (ovr >= t.rare) return 'rare';
    return 'standard';
  }

  const rarityLabel = (rating) => RARITY.labels[rarityOf(rating)] || 'Standard';

  /** Does this rating earn the walkout? Never true at motion level off/minimal:
   *  a cinematic is the first thing to go when the player asked for less. */
  function deservesWalkout(rating) {
    if (isMinimal()) return false;
    return (Number(rating) || 0) >= RARITY.walkoutFrom;
  }

  // ═══════════════════ Visibility / teardown ═══════════════════
  /*
   * Closing the Mini App, backgrounding it, or navigating away mid-sequence
   * must not leave an overlay pinned over the app on return. Anything the
   * engine opened is torn down on hide, and the caller's promise resolves, so
   * the confirmed reward lands in the normal UI.
   */
  const teardownHandlers = new Set();
  function onTeardown(fn) {
    if (typeof fn !== 'function') return () => {};
    teardownHandlers.add(fn);
    return () => teardownHandlers.delete(fn);
  }
  function teardown(reason = 'teardown') {
    matchQueue.drain();
    for (const fn of Array.from(teardownHandlers)) {
      try { fn(reason); } catch (_) {}
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') teardown('hidden');
  });
  window.addEventListener('pagehide', () => teardown('pagehide'));

  // ═══════════════════ Public surface ═══════════════════
  window.CMUAnim = {
    // settings
    LEVELS,
    get settings() { return { ...settings }; },
    level,
    isOff,
    isMinimal,
    isCinematic,
    prefersReducedMotion: () => reduceQuery.matches,
    set,
    setAll,
    onChange(fn) { if (typeof fn === 'function') listeners.add(fn); return () => listeners.delete(fn); },
    get fastPlay() { return settings.fastPlay || isMinimal(); },

    // timing
    ms,
    beat,
    sleep,
    nextFrame,
    reflow,
    clamp,

    // text / ids
    uid,
    esc,

    // feedback
    haptic,
    notify,
    sound,
    registerSoundSink,

    // structure
    overlay,
    createQueue,
    matchQueue,

    // components
    countUp,
    progressTo,
    confetti,
    particles,
    lightBurst,
    openChest,
    flip,
    particleBudget,
    tierColors,

    // artwork
    artwork,
    artFailed,
    waitForArt,

    // rarity
    rarity: RARITY,
    rarityOf,
    rarityLabel,
    configureRarity,
    deservesWalkout,

    // lifecycle
    onTeardown,
    teardown,
  };
})();
