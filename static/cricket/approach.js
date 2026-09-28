// Crickidex Mini App — over-by-over "Approach" play (/lpbot, /ciplbot).
//
// Loaded after app.js and sharing its globals (matchState, userId, matchId,
// fetchState, applyMatchState, triggerMatchEvent, renderControlsSection).
// Each over: the bowling side picks a bowler and a bowling approach, the
// batting side picks a batting approach, the server bowls the whole over, and
// this file plays it back ball by ball. A wicket mid-over pauses the over so
// the batting captain can pick who walks in.

let approachPending = null;       // { sig, at, label } after a pick is sent
let approachSubmitInFlight = false;
let approachChoice = null;        // { turn, key } — the highlighted approach card
let playbackRunning = false;
let playbackTimer = null;
let playbackFinish = null;
let lastOverShownKey = null;      // lastOver.key already played back
let partialShown = { key: null, count: 0 };   // balls of the current over shown
const APPROACH_BALL_MS = 750;
const APPROACH_PENDING_MS = 25000;

function escHtml(v) {
  return String(v ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function haptic(kind) {
  try {
    const h = window.Telegram?.WebApp?.HapticFeedback;
    if (!h) return;
    if (kind === 'select') h.selectionChanged();
    else if (kind === 'wicket') h.notificationOccurred('error');
    else if (kind === 'big') h.notificationOccurred('success');
    else h.impactOccurred(kind || 'light');
  } catch (e) { /* haptics are a nicety */ }
}

// The Impact Player picker (app.js) lives in this mount on the pick sheets.
// Swaps are legal before the over is bowled: bowler and both plan picks.
const APPROACH_IMPACT_MOUNT = 'impact-entry-approach';
const IMPACT_TURNS = ['selecting_over_bowler', 'bowling_approach', 'batting_approach'];

function impactMountHtml() {
  return IMPACT_TURNS.includes(matchState.turnState)
    ? `<div class="impact-entry-mount" id="${APPROACH_IMPACT_MOUNT}"></div>` : '';
}

function refreshApproachImpact() {
  if (typeof renderImpactEntry !== 'function') return;
  if (document.getElementById(APPROACH_IMPACT_MOUNT)) renderImpactEntry(APPROACH_IMPACT_MOUNT);
}

function approachData() {
  return (matchState && matchState.approach) || {};
}

function approachTurnSig() {
  const a = approachData();
  return [
    matchState.turnState, matchState.currentInningsIdx, a.currentOver,
    matchState.score?.wickets, a.lastOver?.key,
    (a.overInProgress?.timeline || []).length,
  ].join('|');
}

function ballChipKind(mark) {
  const m = String(mark);
  if (m === 'W') return 'wkt';
  if (m === '4') return 'four';
  if (m === '6') return 'six';
  if (m === '0') return 'dot';
  if (['WD', 'NB', 'LB'].includes(m)) return 'extra';
  return 'run';
}

function ballChipLabel(mark) {
  const m = String(mark);
  return { WD: 'wd', NB: 'nb', LB: 'lb' }[m] || m;
}

function unitWord(cap = false) {
  const w = approachData().unitWord || 'over';
  return cap ? w.charAt(0).toUpperCase() + w.slice(1) : w;
}

function ballsPerUnitApproach() {
  return typeof ballsPerUnit === 'function' ? ballsPerUnit() : 6;
}

// ── strip of ball chips: "1 4 W _ _ _" ───────────────────────────────
function overStripHtml(timeline, revealed) {
  const marks = (timeline || []).slice(0, revealed ?? (timeline || []).length);
  const legal = marks.filter(m => !['WD', 'NB'].includes(String(m))).length;
  const chips = marks.map(m =>
    `<span class="ov-chip ov-${ballChipKind(m)}">${escHtml(ballChipLabel(m))}</span>`);
  for (let i = legal; i < ballsPerUnitApproach(); i++) {
    chips.push('<span class="ov-chip ov-empty"></span>');
  }
  return `<div class="ov-strip">${chips.join('')}</div>`;
}

// ══════════════════════════════════════════════════════════════════════
// Controls (bottom sheet)
// ══════════════════════════════════════════════════════════════════════

function renderApproachControls({ promptText, promptSubtitle, waitingBlock }) {
  const section = document.getElementById('approach-controls');
  if (!section) return;
  const a = approachData();
  const sig = approachTurnSig();
  const isBot = !!a.isBotMatch;

  const showWaiting = (header, msg) => {
    section.classList.add('hidden');
    waitingBlock.classList.remove('hidden');
    document.getElementById('waiting-status-text').innerText = msg;
    promptText.innerText = header;
    promptSubtitle.innerText = msg;
  };

  // The over is being revealed — let it finish before the next pick.
  if (playbackRunning) {
    showWaiting(`🏏 ${unitWord(true).toUpperCase()} IN PROGRESS`, 'Watch the balls come down…');
    return;
  }

  // A pick was just sent: hold a "thinking" state until the match moves on.
  if (approachPending) {
    if (approachPending.sig === sig && Date.now() - approachPending.at < APPROACH_PENDING_MS) {
      showWaiting(approachPending.header, approachPending.label);
      return;
    }
    approachPending = null;
  }

  if (matchState.myRole === 'spectator' || !matchState.isMyTurn) {
    const opp = isBot ? '🤖 Bot' : 'Opponent';
    const bowling = matchState.myRole === 'bowling';
    const waits = {
      selecting_over_bowler: bowling ? ['⏳ SETTING UP', 'Getting the next over ready…']
        : [`${opp.toUpperCase()} IS PICKING A BOWLER`, `${opp} is choosing who bowls the next ${unitWord()}…`],
      bowling_approach: bowling ? ['⏳ SETTING UP', 'Getting the next over ready…']
        : [`${opp.toUpperCase()} IS PLANNING`, `${opp} is choosing its bowling plan — hidden until the ${unitWord()} is bowled.`],
      batting_approach: bowling ? [`${opp.toUpperCase()} IS PLANNING`, `${opp} is choosing how to bat this ${unitWord()}…`]
        : ['⏳ SETTING UP', 'Getting the next over ready…'],
      selecting_wicket_batsman: ['⚠️ NEW BATSMAN INCOMING', 'The next batsman is walking out…'],
    };
    const [h, m] = waits[matchState.turnState] || ['⏳ PLEASE WAIT', 'The match is moving on…'];
    if (matchState.myRole === 'spectator' && isBot && a.playMode === 'chat') {
      showWaiting('💬 PLAYED IN THE CHAT', 'Make your picks with the chat buttons — this board follows along live. Start with /lpbot app to play here.');
      return;
    }
    showWaiting(matchState.myRole === 'spectator' ? '👁️ SPECTATOR MODE' : h, m);
    return;
  }

  // It's my pick.
  waitingBlock.classList.add('hidden');
  const sheet = document.getElementById('controls-sheet');
  if (sheet && sheet.dataset.approachSig !== sig) {
    sheet.classList.remove('minimized');
    sheet.dataset.approachSig = sig;
  }
  // The app polls several times a second. Rebuilding the list on every poll
  // would swap the buttons out from under a finger mid-tap, so keep the DOM
  // while this pick (and what it offers) is unchanged.
  const renderKey = `${sig}|${matchState.turnState}|${(a.overBowlers || []).length}|${(a.incomingBatsmen || []).length}|${a.hints?.repeat}`;
  const impactKey = `${matchState.impactPlayer?.canUse}|${matchState.impactPlayer?.used}`;
  const unchanged = section.dataset.renderKey === `${renderKey}|${impactKey}`
    && !section.classList.contains('hidden') && section.childElementCount > 0;
  section.classList.remove('hidden');
  const pickerOpenHere = typeof impactSelection !== 'undefined'
    && impactSelection.open && impactSelection.mountId === APPROACH_IMPACT_MOUNT;
  if (unchanged || (pickerOpenHere && document.getElementById(APPROACH_IMPACT_MOUNT))) {
    refreshApproachImpact();
    return;
  }
  if (typeof homeImpactPicker === 'function') homeImpactPicker();
  if (pickerOpenHere && typeof closeImpactPlayerPicker === 'function') {
    closeImpactPlayerPicker({ silent: true });
  }
  section.dataset.renderKey = `${renderKey}|${impactKey}`;

  if (matchState.turnState === 'selecting_over_bowler') {
    promptText.innerText = '🎳 PICK YOUR BOWLER';
    promptSubtitle.innerText = `${unitWord(true)} ${a.currentOver || ''} — who bowls it?`;
    renderApproachBowlers(section, a);
  } else if (matchState.turnState === 'bowling_approach') {
    promptText.innerText = '🎯 BOWLING PLAN';
    const bowlerName = matchState.bowler?.name ? ` for ${matchState.bowler.name}` : '';
    promptSubtitle.innerText = `Choose the plan${bowlerName} — the batter won't see it.`;
    renderApproachCards(section, a, 'bowling');
  } else if (matchState.turnState === 'batting_approach') {
    promptText.innerText = '🏏 BATTING PLAN';
    const bowlerName = matchState.bowler?.name ? `${matchState.bowler.name} to bowl. ` : '';
    promptSubtitle.innerText = `${bowlerName}How do you play this ${unitWord()}?`;
    renderApproachCards(section, a, 'batting');
  } else if (matchState.turnState === 'selecting_wicket_batsman') {
    promptText.innerText = '⚠️ WICKET! WHO WALKS IN?';
    promptSubtitle.innerText = 'Pick your next batsman — the over carries on.';
    renderApproachBatsmen(section, a);
  } else {
    section.classList.add('hidden');
    showWaiting('⏳ PLEASE WAIT', 'The match is moving on…');
  }
}

function renderApproachBowlers(section, a) {
  const list = a.overBowlers || [];
  if (!list.length) {
    section.innerHTML = '<div class="no-options">No bowler available</div>';
    return;
  }
  const partTimeOnly = list.every(p => p.partTime);
  section.innerHTML = `
    ${impactMountHtml()}
    ${approachHintsHtml(a, 'bowling', false)}
    ${partTimeOnly ? '<p class="warning-text">Front-line bowlers are bowled out — only part-timers (🧤) left.</p>' : ''}
    <div class="selection-list mb-3" id="approach-bowler-list"></div>`;
  const box = section.querySelector('#approach-bowler-list');
  list.forEach(p => {
    const div = document.createElement('div');
    div.className = 'selection-item';
    const left = Number(p.oversLeft ?? 0);
    div.innerHTML = `
      <span class="selection-item-name">${escHtml(p.name)}${p.partTime ? ' 🧤' : ''}</span>
      <span class="selection-item-meta">${escHtml(p.displayRating ?? p.bowling_ovr ?? '')} BOWL •
        ${escHtml(p.bowler_type || 'Bowler')} • ${p.figures ? `${escHtml(p.figures)} • ` : ''}<b>${left}</b> ${unitWord()}${left === 1 ? '' : 's'} left</span>`;
    div.onclick = () => {
      if (approachSubmitInFlight) return;
      haptic('medium');
      div.classList.add('selected');
      submitApproach('cipl_bowler', { rosterId: p.roster_id ?? p.id },
        '🎳 BOWLER SET', `${p.name} has the ball…`);
    };
    box.appendChild(div);
  });
  refreshApproachImpact();
}

function approachHintsHtml(a, side, withRepeat = true) {
  const h = a.hints || {};
  const chips = [];
  if (h.phase) {
    const phase = { powerplay: '⚡ Powerplay', middle: '🧭 Middle overs', death: '🔥 Death overs' }[h.phase] || h.phase;
    chips.push(`<span class="ap-chip">${escHtml(phase)}</span>`);
  }
  if (h.botStyle) chips.push(`<span class="ap-chip">🤖 ${escHtml(h.botStyle)}</span>`);
  if (h.botDifficulty) chips.push(`<span class="ap-chip">${escHtml(h.botDifficulty)}</span>`);
  if (h.chasingChance != null && side === 'batting') {
    chips.push(`<span class="ap-chip">🎯 Chase ${Math.round(h.chasingChance)}%</span>`);
  } else if (h.chasingChance != null) {
    chips.push(`<span class="ap-chip">🛡 Defend ${Math.round(100 - h.chasingChance)}%</span>`);
  }
  let warn = '';
  const repeatFrom = Number(h.repeatFrom || 4);
  if (withRepeat && h.lastPick && Number(h.repeat || 0) >= repeatFrom - 1) {
    const opts = side === 'batting' ? a.battingOptions : a.bowlingOptions;
    const label = (opts || []).find(o => o.key === h.lastPick)?.label || h.lastPick;
    warn = `<p class="ap-warn">👀 ${Number(h.repeat)} ${unitWord()}s of <b>${escHtml(label)}</b> in a row —
      once more and the ${a.isBotMatch ? 'bot' : 'opponent'} reads it. Mix it up!</p>`;
  }
  return `${chips.length ? `<div class="ap-chips">${chips.join('')}</div>` : ''}${warn}`;
}

function renderApproachCards(section, a, side) {
  const opts = (side === 'batting' ? a.battingOptions : a.bowlingOptions) || [];
  const turn = `${side}|${approachTurnSig()}`;
  if (!approachChoice || approachChoice.turn !== turn) approachChoice = { turn, key: null };
  const lastPick = a.hints?.lastPick;
  section.innerHTML = `
    ${impactMountHtml()}
    ${approachHintsHtml(a, side)}
    <div class="ap-grid"></div>
    <button class="btn btn-primary btn-block btn-green mt-3 ap-confirm" disabled>
      ${side === 'batting' ? '🏏 Face the ' + unitWord() : '🎳 Bowl the ' + unitWord()}
    </button>`;
  const grid = section.querySelector('.ap-grid');
  const confirm = section.querySelector('.ap-confirm');
  const paint = () => {
    grid.querySelectorAll('.ap-card').forEach(el => {
      el.classList.toggle('selected', el.dataset.key === approachChoice.key);
    });
    confirm.disabled = !approachChoice.key || approachSubmitInFlight;
  };
  opts.forEach(o => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'ap-card';
    card.dataset.key = o.key;
    const risk = Math.max(1, Math.min(5, Number(o.risk || 3)));
    const dots = Array.from({ length: 5 }, (_, i) =>
      `<span class="ap-risk-dot${i < risk ? ' on' : ''}"></span>`).join('');
    card.innerHTML = `
      <span class="ap-emoji">${escHtml(o.emoji)}</span>
      <span class="ap-label">${escHtml(o.label)}${o.key === lastPick ? ' <span class="ap-last">last</span>' : ''}</span>
      <span class="ap-desc">${escHtml(o.desc)}</span>
      <span class="ap-risk" title="Risk">${dots}</span>`;
    card.onclick = () => {
      if (approachSubmitInFlight) return;
      approachChoice.key = o.key;
      haptic('select');
      paint();
    };
    grid.appendChild(card);
  });
  confirm.onclick = () => {
    const o = opts.find(x => x.key === approachChoice.key);
    if (!o || approachSubmitInFlight) return;
    haptic('medium');
    if (side === 'batting') {
      submitApproach('cipl_bat_approach', { key: o.key },
        `🏏 ${o.label.toUpperCase()}`, `Bowling the ${unitWord()}…`);
    } else {
      submitApproach('cipl_bowl_approach', { key: o.key },
        `🎯 ${o.label.toUpperCase()}`,
        a.isBotMatch ? '🤖 Bot is choosing how to bat…' : 'Waiting for the batting plan…');
    }
  };
  paint();
  refreshApproachImpact();
}

function renderApproachBatsmen(section, a) {
  const list = a.incomingBatsmen || [];
  const out = a.outBatsman;
  const partial = a.overInProgress?.timeline || [];
  section.innerHTML = `
    ${out ? `<div class="ap-out">🔴 <b>${escHtml(out.name)}</b> ${escHtml(out.runs)}(${escHtml(out.balls)}) — ${escHtml(out.dismissal)}</div>` : ''}
    <div class="ap-partial"><span class="ap-partial-label">This ${unitWord()}</span>${overStripHtml(partial)}</div>
    <div class="selection-list mb-3" id="approach-batsman-list"></div>
    <div class="controls-hint">No pick in time and the next batsman in your order walks in.</div>`;
  const box = section.querySelector('#approach-batsman-list');
  if (!list.length) {
    box.innerHTML = '<div class="no-options">No batsmen remaining</div>';
    return;
  }
  list.forEach((p, i) => {
    const div = document.createElement('div');
    div.className = 'selection-item';
    div.innerHTML = `
      <span class="selection-item-name">${escHtml(p.name)}${i === 0 ? ' <span class="ap-last">next</span>' : ''}</span>
      <span class="selection-item-meta">${escHtml(p.displayRating ?? p.batting_ovr ?? '')} BAT • ${escHtml(p.role || 'Batsman')}</span>`;
    div.onclick = () => {
      if (approachSubmitInFlight) return;
      haptic('medium');
      div.classList.add('selected');
      submitApproach('cipl_new_batsman', { rosterId: p.roster_id ?? p.id },
        '🏏 NEW BATSMAN', `${p.name} walks out to the middle…`);
    };
    box.appendChild(div);
  });
}

async function submitApproach(type, action, header, label) {
  if (approachSubmitInFlight) return;
  approachSubmitInFlight = true;
  approachPending = { sig: approachTurnSig(), at: Date.now(), header, label };
  renderControlsSection();
  try {
    const res = await fetch('/api/match/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId, matchId, type, action }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      throw new Error(data.message || data.error || 'That pick did not go through.');
    }
    if (data.matchState) applyMatchState(data.matchState, { force: true });
  } catch (err) {
    approachPending = null;
    alert(err.message);
  } finally {
    approachSubmitInFlight = false;
    renderControlsSection();
    fetchState();
  }
}

// ══════════════════════════════════════════════════════════════════════
// Over playback
// ══════════════════════════════════════════════════════════════════════

function currentOverKey() {
  const a = approachData();
  return `${(matchState.currentInningsIdx ?? 0) + 1}-${a.currentOver}`;
}

function handleApproachPlayback() {
  const a = approachData();
  const lo = a.lastOver;
  const partial = a.overInProgress;

  // First state after opening the app: show where things stand, no replay.
  if (lastOverShownKey === null) {
    lastOverShownKey = lo ? lo.key : '';
    if (partial) partialShown = { key: currentOverKey(), count: (partial.timeline || []).length };
    if (lo) renderOverSummary(lo);
    return;
  }
  if (playbackRunning) return;

  // Balls bowled so far in a paused over (a wicket, waiting on the new batsman).
  if (partial) {
    const key = currentOverKey();
    const total = (partial.timeline || []).length;
    const from = partialShown.key === key ? partialShown.count : 0;
    if (total > from) {
      partialShown = { key, count: total };
      playOver({
        timeline: partial.timeline, balls: partial.balls || [], from,
        title: `${unitWord(true)} ${a.currentOver}`, done: () => renderPartialCard(partial),
      });
    }
    return;
  }

  if (lo && lo.key !== lastOverShownKey) {
    lastOverShownKey = lo.key;
    const from = partialShown.key === lo.key ? partialShown.count : 0;
    partialShown = { key: null, count: 0 };
    playOver({
      timeline: lo.timeline, balls: lo.balls || [], from,
      title: `${unitWord(true)} ${lo.overNo}`, done: () => renderOverSummary(lo),
    });
  }
}

function playOver({ timeline, balls, from, title, done }) {
  const box = document.getElementById('over-playback');
  if (!box) { done && done(); return; }
  timeline = timeline || [];
  let shown = Math.max(0, Math.min(from || 0, timeline.length));
  playbackRunning = true;
  box.classList.remove('hidden');

  const paint = (latestText) => {
    box.innerHTML = `
      <div class="op-head">
        <span class="op-title">${escHtml(title)}</span>
        <button type="button" class="op-skip">Skip ▸▸</button>
      </div>
      ${overStripHtml(timeline, shown)}
      <div class="op-comm">${escHtml(latestText || 'Bowler running in…')}</div>`;
    box.querySelector('.op-skip').onclick = () => finish();
  };

  const finish = () => {
    clearTimeout(playbackTimer);
    playbackTimer = null;
    playbackFinish = null;
    shown = timeline.length;
    playbackRunning = false;
    done && done();
    renderControlsSection();
  };
  playbackFinish = finish;

  const step = () => {
    if (shown >= timeline.length) {
      playbackTimer = setTimeout(finish, APPROACH_BALL_MS);
      return;
    }
    const ball = balls[shown] || { runs: Number(timeline[shown]) || 0,
      isWicket: String(timeline[shown]) === 'W', text: '' };
    shown += 1;
    paint(ball.text);
    if (ball.isWicket) haptic('wicket');
    else if (Number(ball.runs) >= 4) haptic('big');
    try {
      triggerMatchEvent({ type: 'ball', runs: ball.runs, isWicket: ball.isWicket,
        eventKey: ball.eventKey, text: ball.text });
    } catch (e) { /* the event box is cosmetic */ }
    playbackTimer = setTimeout(step, APPROACH_BALL_MS);
  };
  paint();
  renderControlsSection();
  playbackTimer = setTimeout(step, 350);
}

function renderPartialCard(partial) {
  const box = document.getElementById('over-playback');
  if (!box) return;
  const a = approachData();
  box.classList.remove('hidden');
  box.innerHTML = `
    <div class="op-head"><span class="op-title">${unitWord(true)} ${escHtml(a.currentOver)} — paused</span></div>
    ${overStripHtml(partial.timeline)}
    <div class="op-comm">🔴 Wicket! ${matchState.isMyTurn ? 'Pick your next batsman below.' : 'A new batsman is on the way.'}</div>`;
}

function renderOverSummary(lo) {
  const box = document.getElementById('over-playback');
  if (!box || !lo) return;
  box.classList.remove('hidden');
  const bat = lo.battingApproach || {};
  const bowl = lo.bowlingApproach || {};
  const a = approachData();
  const batWho = lo.botBatted ? '🤖 Bot' : (matchState.myRole === 'batting' ? 'You' : 'Batting');
  const bowlWho = lo.botBatted ? (matchState.myRole === 'spectator' ? 'Bowling' : 'You') : (a.isBotMatch ? '🤖 Bot' : 'Bowling');
  box.innerHTML = `
    <div class="op-head">
      <span class="op-title">End of ${unitWord()} ${escHtml(lo.overNo)}</span>
      <span class="op-score">${escHtml(lo.runs)} run${lo.runs === 1 ? '' : 's'}${lo.wickets ? ` · ${escHtml(lo.wickets)} wkt${lo.wickets === 1 ? '' : 's'}` : ''}</span>
    </div>
    ${overStripHtml(lo.timeline)}
    <div class="op-duel">
      <span class="op-side"><small>${escHtml(batWho)}</small>${escHtml(bat.label || '')}</span>
      <span class="op-vs">vs</span>
      <span class="op-side"><small>${escHtml(bowlWho)}</small>${escHtml(bowl.label || '')}</span>
    </div>
    ${lo.combo ? `<div class="op-combo">✨ ${escHtml(lo.combo)}</div>` : ''}
    ${lo.flavour ? `<div class="op-comm">${escHtml(lo.flavour)}</div>` : ''}
    ${lo.bowler ? `<div class="op-fig">🎳 ${escHtml(lo.bowler)}${lo.bowlerFigures ? ` · ${escHtml(lo.bowlerFigures)}` : ''}</div>` : ''}
    ${(bat.hidden || bowl.hidden) ? '<div class="op-hidden-note">🔒 The bot keeps its plan secret — it is only revealed on 🟢 Easy.</div>' : ''}`;
}
