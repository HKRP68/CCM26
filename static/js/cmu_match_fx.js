/*
 * CMU Match FX — Phase 5
 * ═══════════════════════════════════════════════════════════════════════════
 * Original broadcast-style overlays for confirmed match events: the toss,
 * FOUR and SIX, wickets and dropped catches, fifties and centuries, three-
 * and five-wicket hauls, hat-tricks, team milestones, the Super Over, records
 * broken, Player of the Match, victory and a tournament championship.
 *
 * Presentation only, and in this file that has a sharp edge:
 *
 *   • This module READS confirmed state. It never decides a ball, a wicket, a
 *     milestone or a result, and it never writes back to match state. Every
 *     announcement is derived from numbers the server has already published
 *     in the commentary feed and the scorecard — noticing that a batter's
 *     confirmed score has reached 50 is reading, not generating.
 *   • Nothing here can block gameplay. Banners are pointer-events: none, take
 *     no focus, auto-dismiss, and run through a bounded queue that drops a
 *     backlog rather than making the player wait for it. Fast-play collapses
 *     the queue entirely.
 *   • Announcements are de-duplicated per match, so a poll that replays the
 *     same over does not replay the same century.
 *
 * Exposed as window.CMUMatchFX.
 */
(() => {
  'use strict';

  const FX = window.CMUAnim;
  if (!FX) {
    const noop = () => Promise.resolve();
    window.CMUMatchFX = {
      available: false,
      announce: noop, present: noop, toss: noop, result: noop,
      potm: noop, championship: noop, superOver: noop, record: noop,
      reset: () => {}, setFastPlay: () => {}, clear: () => {},
    };
    return;
  }

  /* ════════════════════════ Event catalogue ════════════════════════
   * `hold` is the design duration before the banner leaves; `weight` is the
   * queue priority, so a wicket outranks a single. All cosmetic.
   */
  const EVENTS = {
    four:        { label: 'FOUR',      tone: 'blue',   hold: 1150, weight: 2, haptic: 'heavy', sound: 'four' },
    six:         { label: 'SIX',       tone: 'violet', hold: 1350, weight: 3, haptic: 'heavy', sound: 'six' },
    wicket:      { label: 'WICKET',    tone: 'red',    hold: 1450, weight: 4, haptic: 'rigid', notify: 'warning', sound: 'wicket' },
    dropped:     { label: 'DROPPED!',  tone: 'amber',  hold: 1200, weight: 3, haptic: 'medium', sound: 'dropped' },
    fifty:       { label: 'FIFTY',     tone: 'amber',  hold: 1700, weight: 5, haptic: 'heavy', notify: 'success', sound: 'milestone' },
    century:     { label: 'CENTURY',   tone: 'gold',   hold: 2300, weight: 7, haptic: 'heavy', notify: 'success', sound: 'milestone_big' },
    three_wickets: { label: 'THREE-FOR', tone: 'red',  hold: 1700, weight: 5, haptic: 'heavy', notify: 'success', sound: 'milestone' },
    five_wickets:  { label: 'FIVE-FOR',  tone: 'gold', hold: 2300, weight: 7, haptic: 'heavy', notify: 'success', sound: 'milestone_big' },
    hat_trick:   { label: 'HAT-TRICK', tone: 'gold',   hold: 2400, weight: 8, haptic: 'heavy', notify: 'success', sound: 'milestone_big' },
    team_milestone: { label: '',       tone: 'teal',   hold: 1500, weight: 4, haptic: 'medium', sound: 'milestone' },
    super_over:  { label: 'SUPER OVER', tone: 'violet', hold: 2400, weight: 9, haptic: 'heavy', notify: 'warning', sound: 'super_over' },
    record:      { label: 'RECORD BROKEN', tone: 'gold', hold: 2400, weight: 8, haptic: 'heavy', notify: 'success', sound: 'record' },
    toss:        { label: 'TOSS',      tone: 'teal',   hold: 2100, weight: 6, haptic: 'medium', sound: 'toss' },
  };

  // Cosmetic thresholds. Configurable so a format with different conventions
  // (the Hundred, a 5-over slog) can retune what gets announced without any
  // change to what the server records.
  const CONFIG = {
    battingMilestones: [50, 100, 150, 200],
    bowlingMilestones: [3, 5],
    teamMilestoneStep: 50,
    // A banner is suppressed if more than this many are already waiting.
    queueCapacity: 3,
  };

  function configure(patch = {}) {
    if (Array.isArray(patch.battingMilestones)) CONFIG.battingMilestones = patch.battingMilestones.slice();
    if (Array.isArray(patch.bowlingMilestones)) CONFIG.bowlingMilestones = patch.bowlingMilestones.slice();
    if (Number.isFinite(patch.teamMilestoneStep)) CONFIG.teamMilestoneStep = Number(patch.teamMilestoneStep);
    return { ...CONFIG };
  }

  /* ════════════════════════ Banner layer ════════════════════════
   * Deliberately NOT FX.overlay(): an in-play banner must not lock scrolling,
   * trap focus or swallow a tap. It is a transparent layer over the arena that
   * the player can keep playing straight through.
   */
  let layer = null;
  function bannerLayer() {
    if (layer && layer.isConnected) return layer;
    layer = document.createElement('div');
    layer.className = 'cmu-mx-layer';
    // aria-live, not a dialog: the feed below is the record, this is colour
    // commentary, and it must never take focus off a control mid-tap.
    layer.setAttribute('aria-live', 'polite');
    layer.setAttribute('aria-atomic', 'false');
    document.body.appendChild(layer);
    return layer;
  }

  const queue = FX.createQueue({ capacity: CONFIG.queueCapacity, taskTimeout: 4000 });
  let fastPlay = false;
  function setFastPlay(on) { fastPlay = Boolean(on); if (fastPlay) queue.drain(); }
  function effectiveFastPlay() { return fastPlay || FX.fastPlay; }

  /** Drop everything queued and clear the layer. Used on navigation, on the
   *  match ending, and whenever the player asks for fast play. */
  function clear() {
    queue.drain();
    if (layer && layer.isConnected) layer.replaceChildren();
  }
  FX.onTeardown(clear);

  /**
   * Show one banner. Resolves when it has left (or immediately if dropped).
   * Never rejects; a failed banner is simply not shown.
   */
  function present(kind, payload = {}, { priority = null, key = '' } = {}) {
    const spec = EVENTS[kind];
    if (!spec || FX.isOff()) return Promise.resolve();
    return queue.push(async (ctx) => {
      if (ctx.skipped) return;
      const host = bannerLayer();
      const headline = FX.esc(payload.label || spec.label || kind.replace(/_/g, ' ').toUpperCase());
      const el = document.createElement('div');
      el.className = `cmu-mx-banner tone-${spec.tone} kind-${kind}`;
      el.innerHTML = `
        <span class="cmu-mx-flare" aria-hidden="true"></span>
        <span class="cmu-mx-body">
          ${payload.eyebrow ? `<span class="cmu-mx-eyebrow">${FX.esc(payload.eyebrow)}</span>` : ''}
          <strong class="cmu-mx-headline">${headline}</strong>
          ${payload.detail ? `<span class="cmu-mx-detail">${FX.esc(payload.detail)}</span>` : ''}
          ${payload.sub ? `<span class="cmu-mx-sub">${FX.esc(payload.sub)}</span>` : ''}
        </span>`;
      host.appendChild(el);
      FX.reflow(el);
      el.classList.add('is-in');

      if (spec.haptic) FX.haptic(spec.haptic);
      if (spec.notify) FX.notify(spec.notify);
      if (spec.sound) FX.sound(spec.sound);
      if (!FX.isMinimal() && (kind === 'six' || kind === 'century' || kind === 'hat_trick'
        || kind === 'five_wickets' || kind === 'record')) {
        FX.particles(el, { count: 24, tier: spec.tone === 'gold' ? 'legend' : 'epic', spread: 160 });
      }

      const hold = effectiveFastPlay() ? Math.min(FX.ms(420), 420) : FX.ms(spec.hold);
      await FX.sleep(Math.max(140, hold));
      el.classList.remove('is-in');
      el.classList.add('is-out');
      await FX.sleep(FX.ms(260) + 40);
      el.remove();
    }, { priority: priority == null ? spec.weight : priority, key });
  }

  /* ════════════════════════ Reading confirmed state ════════════════════════ */

  /*
   * Everything announced once is remembered, so a poll that re-delivers the
   * same over cannot re-announce the same century. Keyed per match; reset()
   * clears it when a new match starts.
   */
  let seen = new Set();
  let matchKey = '';
  function reset(key = '') {
    if (key && key === matchKey) return;
    matchKey = key;
    seen = new Set();
    clear();
  }
  const once = (token) => {
    if (!token || seen.has(token)) return false;
    seen.add(token);
    return true;
  };

  /** Highest crossed threshold in `list` at or below `value`, else null. */
  function crossed(value, list) {
    let best = null;
    for (const step of list) if (value >= step) best = step;
    return best;
  }

  /**
   * Derive the announcements a confirmed ball earns.
   *
   * Pure: it reads `comm` (the commentary entry the server published) and
   * `state` (the published scorecard) and returns descriptors. It mutates no
   * match state and computes no outcome — a fifty is announced because the
   * server already says the batter has 50, not because this decided so.
   *
   * @returns {Array<{kind, payload, token}>}
   */
  function fromBall(comm = {}, state = {}) {
    const events = [];
    if (!comm || comm.type === 'end_of_over' || comm.type === 'end_of_innings') return events;

    const ballId = comm.id || comm.ballId
      || `${state.inningsIndex ?? 0}:${comm.over ?? ''}:${comm.text ?? ''}`;
    const text = String(comm.text || '').toLowerCase();
    const runs = Number(comm.runs);

    // ── the ball itself
    if (comm.isWicket) {
      events.push({ kind: 'wicket', token: `w:${ballId}`, payload: {
        eyebrow: comm.over ? `Over ${comm.over}` : '',
        detail: comm.dismissal || comm.dis || '',
        sub: comm.bowler?.name ? `${comm.bowler.name}` : '',
      } });
    } else if (runs === 6) {
      events.push({ kind: 'six', token: `6:${ballId}`, payload: {
        eyebrow: comm.over ? `Over ${comm.over}` : '', detail: comm.batter?.name || '',
      } });
    } else if (runs === 4) {
      events.push({ kind: 'four', token: `4:${ballId}`, payload: {
        eyebrow: comm.over ? `Over ${comm.over}` : '', detail: comm.batter?.name || '',
      } });
    }

    // A dropped catch is only announced when the published entry says so —
    // either with an explicit flag or in the commentary the server wrote.
    if (comm.dropped || comm.droppedCatch || comm.eventKey === 'dropped_catch'
      || /\bdropped\b|\bput down\b|\bshelled\b/.test(text)) {
      events.push({ kind: 'dropped', token: `d:${ballId}`, payload: {
        eyebrow: 'Chance missed', detail: comm.fielder?.name || '',
      } });
    }

    // ── batting milestones, read from the confirmed striker card
    const striker = state.striker || comm.batter || comm.striker;
    const strikerRuns = Number(striker?.stats?.runs ?? striker?.runs);
    if (striker?.name && Number.isFinite(strikerRuns)) {
      const mark = crossed(strikerRuns, CONFIG.battingMilestones);
      if (mark) {
        const kind = mark >= 100 ? 'century' : 'fifty';
        events.push({
          kind,
          token: `bm:${striker.id ?? striker.name}:${mark}`,
          payload: {
            label: mark >= 100 && mark > 100 ? `${mark}` : undefined,
            eyebrow: striker.name,
            detail: `${strikerRuns}${Number.isFinite(Number(striker?.stats?.balls ?? striker?.balls))
              ? ` off ${striker.stats?.balls ?? striker.balls}` : ''}`,
          },
        });
      }
    }

    // ── bowling milestones and the hat-trick, from the confirmed bowler card
    const bowler = state.bowler || comm.bowler;
    const bowlerWickets = Number(bowler?.stats?.wickets ?? bowler?.wickets);
    if (bowler?.name && Number.isFinite(bowlerWickets)) {
      const mark = crossed(bowlerWickets, CONFIG.bowlingMilestones);
      if (mark) {
        events.push({
          kind: mark >= 5 ? 'five_wickets' : 'three_wickets',
          token: `bw:${bowler.id ?? bowler.name}:${mark}`,
          payload: {
            eyebrow: bowler.name,
            detail: `${bowlerWickets}-${bowler.stats?.runsConceded ?? bowler.runsConceded ?? ''}`.replace(/-$/, ''),
          },
        });
      }
      // A hat-trick is announced only when the published state marks one. It
      // is never inferred from three wickets in a card, which can span overs.
      if (comm.hatTrick || bowler.hatTrick || /\bhat[- ]?trick\b/.test(text)) {
        events.push({
          kind: 'hat_trick',
          token: `ht:${bowler.id ?? bowler.name}:${bowlerWickets}`,
          payload: { eyebrow: bowler.name, detail: 'Three in three' },
        });
      }
    }

    // ── team milestones, from the confirmed innings total
    const total = Number(state.score?.runs ?? state.runs);
    if (Number.isFinite(total) && CONFIG.teamMilestoneStep > 0) {
      const mark = Math.floor(total / CONFIG.teamMilestoneStep) * CONFIG.teamMilestoneStep;
      if (mark >= CONFIG.teamMilestoneStep) {
        events.push({
          kind: 'team_milestone',
          token: `tm:${state.inningsIndex ?? 0}:${mark}`,
          payload: {
            label: `${mark} UP`,
            eyebrow: state.battingTeamName || '',
            detail: Number.isFinite(Number(state.score?.wickets))
              ? `${total}/${state.score.wickets}` : '',
          },
        });
      }
    }

    return events;
  }

  /**
   * Announce everything a confirmed ball earns, de-duplicated.
   * Safe to call on every delivery, including replays of the same over.
   */
  function announce(comm, state) {
    let events;
    try {
      events = fromBall(comm, state);
    } catch (error) {
      // Classification is best-effort. A shape this build has not seen must
      // never break the arena's own ball handling.
      if (window.console) console.warn('[CMUMatchFX] could not classify a ball safely', error);
      return Promise.resolve();
    }
    const jobs = [];
    for (const event of events) {
      if (!once(event.token)) continue;
      jobs.push(present(event.kind, event.payload, { key: event.kind }));
    }
    return Promise.all(jobs);
  }

  /* ════════════════════════ Named moments ════════════════════════ */

  /** The toss. `winner` and `decision` come from the published match state. */
  function toss({ winner = '', decision = '', token = '' } = {}) {
    if (!winner || !once(`toss:${token || winner}:${decision}`)) return Promise.resolve();
    return present('toss', {
      eyebrow: 'Toss',
      label: winner.toUpperCase(),
      detail: decision ? `Chose to ${decision}` : '',
    });
  }

  function superOver({ token = '' } = {}) {
    if (!once(`so:${token || 'super'}`)) return Promise.resolve();
    return present('super_over', { eyebrow: 'Scores level', detail: 'One over each' });
  }

  function record({ title = '', detail = '', token = '' } = {}) {
    if (!title || !once(`rec:${token || title}`)) return Promise.resolve();
    return present('record', { eyebrow: 'Record broken', label: title.toUpperCase(), detail });
  }

  /* ════════════════════════ Match-end presentations ════════════════════════
   * These are the only overlays that take focus, and only because the match is
   * over: nothing is waiting on the player's next tap. Each is dismissible and
   * sits on top of a result screen that is already fully rendered underneath.
   */
  function card({ className, eyebrow, headline, lines = [], tone = 'gold', tier = 'legend', dismiss = 'Close' }) {
    return new Promise((resolve) => {
      if (FX.isOff()) { resolve('off'); return; }
      let overlay;
      try {
        overlay = FX.overlay({
          className: `cmu-mx-card tone-${tone} ${className || ''}`.trim(),
          label: `${eyebrow || ''} ${headline || ''}`.trim() || 'Match moment',
          closeOnBackdrop: true,
          html: `<div class="cmu-fx-panel cmu-mx-card-panel">
            <span class="cmu-fx-eyebrow">${FX.esc(eyebrow || '')}</span>
            <h3 class="cmu-mx-card-headline">${FX.esc(headline || '')}</h3>
            ${lines.map((line) => `<p class="cmu-mx-card-line">${FX.esc(line)}</p>`).join('')}
            <div class="cmu-fx-actions">
              <button type="button" class="app-primary" data-mx-close>${FX.esc(dismiss)}</button>
            </div>
          </div>`,
          onClose: () => { release(); resolve('done'); },
        });
      } catch (error) {
        if (window.console) console.warn('[CMUMatchFX] card could not open safely', error);
        resolve('failed');
        return;
      }
      const release = FX.onTeardown(() => overlay.close('teardown'));
      overlay.root.querySelector('[data-mx-close]')?.addEventListener('click', () => overlay.close('done'));
      overlay.focusFirst();
      FX.notify('success');
      FX.confetti(tier === 'legend' ? 80 : 56, tier, overlay.root);
      // Never hold the screen: the card lets itself out if nobody taps.
      setTimeout(() => overlay.close('done'), FX.ms(6500) + 1500);
    });
  }

  /** Match victory. Everything shown is the server's published result. */
  function result({ winner = '', detail = '', token = '', tied = false } = {}) {
    if (!once(`res:${token || winner || 'result'}`)) return Promise.resolve();
    if (tied) {
      return card({
        className: 'is-tie', eyebrow: 'Full time', headline: 'MATCH TIED',
        lines: [detail].filter(Boolean), tone: 'teal', tier: 'epic',
      });
    }
    if (!winner) return Promise.resolve();
    return card({
      eyebrow: 'Full time', headline: `${winner.toUpperCase()} WIN`,
      lines: [detail].filter(Boolean), tone: 'gold', tier: 'legend',
    });
  }

  /** Player of the Match. Name and figures come from result.motm. */
  function potm({ name = '', detail = '', token = '' } = {}) {
    if (!name || !once(`potm:${token || name}`)) return Promise.resolve();
    return card({
      className: 'is-potm', eyebrow: 'Player of the Match',
      headline: name.toUpperCase(), lines: [detail].filter(Boolean),
      tone: 'violet', tier: 'epic',
    });
  }

  /** Tournament championship. */
  function championship({ team = '', competition = '', token = '' } = {}) {
    if (!team || !once(`champ:${token || team}`)) return Promise.resolve();
    return card({
      className: 'is-champions', eyebrow: competition || 'Champions',
      headline: `${team.toUpperCase()}`, lines: ['Tournament champions'],
      tone: 'gold', tier: 'legend', dismiss: 'Celebrate',
    });
  }

  window.CMUMatchFX = {
    available: true,
    EVENTS,
    configure,
    present,
    fromBall,
    announce,
    toss,
    superOver,
    record,
    result,
    potm,
    championship,
    reset,
    clear,
    setFastPlay,
    get fastPlay() { return effectiveFastPlay(); },
    get queued() { return queue.size; },
  };
})();
