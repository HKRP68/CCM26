// Crickidex Mini App - Client Logic
const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
}

// Global UI State
let matchId = null;
let chatId = null;
let userId = null;
let matchState = null;
let pollingInterval = null;
let fetchInFlight = false;
let identitySelectionRequired = false;
let pollFailureCount = 0;
const POLL_REQUEST_TIMEOUT_MS = 8000;
const MAX_POLL_FAILURES = 10;
const MATCH_POLL_INTERVAL_MS = 150;
const AUTOPLAY_ACTION_DELAY_MS = 0;
// Fast ball-by-ball pacing vs the Bot. The bot's reply (shot/delivery/outcome)
// is already resolved server-side by the time these timers run, so they're
// kept just long enough to read the transition label before the next ball.
const BALL_FLOW_DELAY_MS = 150;
// How long the ball-outcome event box (GIF/text) lingers before reverting to
// idle. Kept short so the outcome reads instantly and the screen is ready for
// the next ball quickly instead of holding on the celebratory GIF/text.
const EVENT_FLASH_MS = 350;

// The Hundred (5-ball "sets", 100 balls/innings) vs T20 (6-ball overs) helpers.
// matchState.isHundred / matchState.totalBalls come straight from the backend.
function isHundredFormat() {
  return !!(matchState && matchState.isHundred);
}
function ballsPerUnit() {
  return isHundredFormat() ? 5 : 6;
}
// "63b" for The Hundred, "12.3" for over formats.
function formatBowlingUnit(overs, balls) {
  return isHundredFormat() ? `${(overs || 0) * 5 + (balls || 0)}b` : `${overs || 0}.${balls || 0}`;
}
// "63 Balls" for The Hundred, "12.3 Ov" for over formats.
function formatInningsProgress(overs, balls) {
  return isHundredFormat()
    ? `${(overs || 0) * 5 + (balls || 0)} Balls`
    : `${overs || 0}.${balls || 0} Ov`;
}

// Selected actions
let selectedDelivery = null;
let selectedSpeed = 'normal';
let autoplayActive = false;
let autoplayMatchId = null; // match id Autoplay was last applied to (reset per match)
let activeScorecardTab = 'innings1'; // 'innings1' or 'innings2'
let lastBallUniqueId = null;
let eventGifTimer = null;
let flowStageTimer = null;
let flowActionInFlight = false;
let preloadedEventGifIds = new Set();
let wasMyTurn = false;
let lastActionableSig = null; // tracks the current actionable turn for auto-expand
let selectionSubmitInFlight = false;
// `mountId` tracks which selection sheet currently hosts the picker so the
// 150ms poll can re-render it in place without losing an in-progress pick.
let impactSelection = { incoming: null, outgoing: null, open: false, mountId: null };

function isActiveXIPlayer(player) {
  return player?.active !== false;
}

// Initial Setup
async function init() {
  const cleanParam = (val) => {
    if (val === null || val === undefined || val === 'null' || val === 'undefined' || val === '') {
      return null;
    }
    return val.toString().trim();
  };

  // 1. Fallback / direct URL parsing
  const urlParams = new URLSearchParams(window.location.search);
  matchId = cleanParam(urlParams.get('matchId') || urlParams.get('match_id'));
  chatId = cleanParam(urlParams.get('chatId') || urlParams.get('chat_id'));
  userId = cleanParam(urlParams.get('userId') || urlParams.get('user_id'));

  // 2. Parse from Telegram WebApp SDK (Deep Link start_param)
  if (tg && tg.initDataUnsafe) {
    if (tg.initDataUnsafe.start_param) {
      let startParam = tg.initDataUnsafe.start_param;
      if (startParam.startsWith('cricket_')) {
        startParam = startParam.substring(8);
      }
      const lastUnderscore = startParam.lastIndexOf('_');
      if (lastUnderscore !== -1) {
        matchId = cleanParam(startParam.substring(0, lastUnderscore));
        chatId = cleanParam(startParam.substring(lastUnderscore + 1));
      } else {
        matchId = cleanParam(startParam);
      }
    }
    if (tg.initDataUnsafe.user) {
      userId = cleanParam(tg.initDataUnsafe.user.id);
    }
  }

  // 3. Direct/browser launches do not receive Telegram identity. Reuse an
  // explicitly chosen local identity when possible; otherwise poll once as a
  // spectator so the identity picker can render the current room participants.
  if (!userId) {
    userId = cleanParam(localStorage.getItem('crickidex_user_id'));
  }
  if (!userId) {
    userId = 'spectator';
    identitySelectionRequired = true;
  }

  // Bind exit handlers
  const handleExit = () => {
    if (tg) {
      tg.close();
    } else {
      window.location.reload();
    }
  };
  
  const btnBack = document.getElementById('exit-btn-back');
  if (btnBack) btnBack.addEventListener('click', handleExit);

  // Theme toggle (Light / Dark)
  const themeToggleBtn = document.getElementById('theme-toggle-btn');
  if (themeToggleBtn) {
    themeToggleBtn.addEventListener('click', () => {
      const root = document.documentElement;
      const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('crickidex_theme', next); } catch (e) {}
      if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
    });
  }

  startPolling();
  setupEventListeners();
}

// Event Listeners
function setupEventListeners() {
  // Setup confirm setup lineup button
  document.getElementById('submit-setup-btn').addEventListener('click', submitSetup);
  
  // Delivery Submit
  document.getElementById('submit-delivery-btn').addEventListener('click', submitDelivery);

  // Wicket batsman confirm
  const submitWicketBatsmanBtn = document.getElementById('submit-wicket-batsman-btn');
  if (submitWicketBatsmanBtn) {
    submitWicketBatsmanBtn.addEventListener('click', () => {
      const selectedEl = document.querySelector('#wicket-batsman-list .selection-item.selected');
      if (selectedEl) {
        document.getElementById('controls-sheet').classList.add('minimized');
        selectNextBatsman(parseInt(selectedEl.dataset.index));
      } else {
        alert("Please select a batsman first!");
      }
    });
  }

  // Over bowler confirm
  const submitOverBowlerBtn = document.getElementById('submit-over-bowler-btn');
  if (submitOverBowlerBtn) {
    submitOverBowlerBtn.addEventListener('click', () => {
      const selectedEl = document.querySelector('#over-bowler-list .selection-item.selected');
      if (selectedEl) {
        document.getElementById('controls-sheet').classList.add('minimized');
        selectNextBowler(parseInt(selectedEl.dataset.index));
      } else {
        alert("Please select a bowler first!");
      }
    });
  }

  // Innings break: skip the countdown and head straight to team selection
  const ibContinueBtn = document.getElementById('ib-continue-btn');
  if (ibContinueBtn) ibContinueBtn.addEventListener('click', continuePastInningsBreak);

  // Close app button
  document.getElementById('close-app-btn').addEventListener('click', () => {
    if (tg) {
      tg.close();
    } else {
      alert("Match completed! You can return to Telegram.");
      window.close();
    }
  });

  // The "Use Impact Player" banner is created dynamically per selection sheet,
  // so it binds its own onclick. Only the picker's persistent Cancel/Confirm
  // buttons need static listeners here.
  const cancelImpactBtn = document.getElementById('impact-player-cancel-btn');
  if (cancelImpactBtn) cancelImpactBtn.addEventListener('click', closeImpactPlayerPicker);
  const confirmImpactBtn = document.getElementById('confirm-impact-player-btn');
  if (confirmImpactBtn) confirmImpactBtn.addEventListener('click', submitImpactPlayer);

  // Completed-match scorecard toggle. Keep the finished board read-only,
  // but let players jump into the scorecard and reopen the result at any time.
  document.getElementById('view-match-btn').addEventListener('click', () => {
    document.getElementById('result-overlay').classList.add('hidden');
    switchGameplayTab('scorecard');
  });
  document.getElementById('tab-result').addEventListener('click', () => {
    if (matchState?.status === 'completed') renderResultScreen();
  });

  // Autoplay Pill Switch Toggle
  const autoplayToggleBtn = document.getElementById('autoplay-toggle-btn');
  autoplayToggleBtn.addEventListener('click', () => {
    handleAutoplayToggle(!autoplayActive);
  });

  // Speed selection
  const speedButtons = document.querySelectorAll('.btn-speed');
  speedButtons.forEach(btn => {
    btn.addEventListener('click', (e) => {
      speedButtons.forEach(b => b.classList.remove('active'));
      e.currentTarget.classList.add('active');
      selectedSpeed = e.currentTarget.dataset.speed;
    });
  });

  // Controls bottom sheet toggle (Minimize/Expand toggle)
  const controlsSheet = document.getElementById('controls-sheet');
  const toggleControlsBtn = document.getElementById('toggle-controls-btn');

  const updateToggleIcon = () => {
    const isMin = controlsSheet.classList.contains('minimized');
    const iconSvg = document.getElementById('toggle-controls-icon');
    if (iconSvg) {
      if (isMin) {
        // Chevron Up (points up to expand)
        iconSvg.innerHTML = '<polyline points="18 15 12 9 6 15"></polyline>';
      } else {
        // Chevron Down (points down to minimize)
        iconSvg.innerHTML = '<polyline points="6 9 12 15 18 9"></polyline>';
      }
    }
  };

  const toggleControls = () => {
    controlsSheet.classList.toggle('minimized');
    updateToggleIcon();
  };

  if (toggleControlsBtn) {
    toggleControlsBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleControls();
    });
  }

  // Click on header when minimized should expand it
  const sheetHeader = document.querySelector('.sheet-header');
  if (sheetHeader) {
    sheetHeader.addEventListener('click', () => {
      if (controlsSheet.classList.contains('minimized')) {
        controlsSheet.classList.remove('minimized');
        updateToggleIcon();
      }
    });
  }

  // Tab switching
  const tabs = ['match', 'scorecard', 'squads'];
  tabs.forEach(tab => {
    document.getElementById(`tab-${tab}`).addEventListener('click', () => switchGameplayTab(tab));
  });

  // Scorecard Team Switch tabs
  const tabHost = document.getElementById('scorecard-tab-host');
  const tabGuest = document.getElementById('scorecard-tab-guest');

  tabHost.addEventListener('click', () => {
    activeScorecardTab = 'innings1';
    tabHost.classList.add('active');
    tabGuest.classList.remove('active');
    renderScorecardPanel();
    renderPartnershipHistory();
    renderManhattanChart();
    renderOverByOver();
  });

  tabGuest.addEventListener('click', () => {
    activeScorecardTab = 'innings2';
    tabGuest.classList.add('active');
    tabHost.classList.remove('active');
    renderScorecardPanel();
    renderPartnershipHistory();
    renderManhattanChart();
    renderOverByOver();
  });
}

function switchGameplayTab(tab) {
  document.getElementById('tab-result').classList.remove('active');
  ['match', 'scorecard', 'squads'].forEach(t => {
    document.getElementById(`tab-${t}`).classList.toggle('active', t === tab);
    document.getElementById(`panel-${t}`).classList.toggle('active', t === tab);
  });
}

// Screen Switcher
function showScreen(screenId) {
  const screens = document.querySelectorAll('.screen');
  screens.forEach(s => s.classList.remove('active'));
  document.getElementById(screenId).classList.add('active');

  // Hide the sticky control sheet on non-gameplay screens
  const controlsSheet = document.getElementById('controls-sheet');
  if (controlsSheet) {
    if (screenId !== 'gameplay-screen') {
      controlsSheet.classList.add('hidden');
    } else {
      controlsSheet.classList.remove('hidden');
    }
  }
}

// Show identity selection screen
function showIdentitySelection(match) {
  showScreen('identity-screen');
  const container = document.getElementById('identity-options');
  container.innerHTML = '';

  // Header meta inside selection
  document.getElementById('header-host-name').innerText = (match.host.teamName || match.host.username || 'HOST').toUpperCase();
  document.getElementById('header-guest-name').innerText = (match.guest ? (match.guest.teamName || match.guest.username) : 'AI XI').toUpperCase();
  document.getElementById('header-match-uuid').innerText = `MATCH ID: ${match.id.substring(0, 6).toUpperCase()}`;

  // Host Option
  const hostBtn = document.createElement('button');
  hostBtn.className = 'btn-identity host';
  hostBtn.innerHTML = `
    <span class="user-role-icon">🏏</span>
    <div class="user-info-text">
      <span class="username">@${match.host.username || 'Host'}</span>
      <span class="team">${match.host.teamName || 'Host Team'}</span>
    </div>
  `;
  hostBtn.addEventListener('click', () => selectIdentity(match.host.telegramId));
  container.appendChild(hostBtn);

  // Guest Option
  if (match.guest && match.guest.telegramId !== 'ai') {
    const guestBtn = document.createElement('button');
    guestBtn.className = 'btn-identity guest';
    guestBtn.innerHTML = `
      <span class="user-role-icon">🎳</span>
      <div class="user-info-text">
        <span class="username">@${match.guest.username || 'Guest'}</span>
        <span class="team">${match.guest.teamName || 'Guest Team'}</span>
      </div>
    `;
    guestBtn.addEventListener('click', () => selectIdentity(match.guest.telegramId));
    container.appendChild(guestBtn);
  }

  // Spectator Option
  const specBtn = document.createElement('button');
  specBtn.className = 'btn-identity spectator';
  specBtn.innerHTML = `
    <span class="user-role-icon">👀</span>
    <div class="user-info-text">
      <span class="username">Spectator Mode</span>
      <span class="team">Watch the live matches</span>
    </div>
  `;
  specBtn.addEventListener('click', () => selectIdentity('spectator'));
  container.appendChild(specBtn);
}

function selectIdentity(selectedId) {
  userId = selectedId.toString();
  identitySelectionRequired = false;
  localStorage.setItem('crickidex_user_id', userId);

  // Refresh immediately. startPolling() is idempotent, so switching identity
  // cannot leak a second interval that races the existing room poller.
  startPolling();
}

// Polling Loop
function startPolling() {
  fetchState();
  if (!pollingInterval) {
    pollingInterval = setInterval(fetchState, MATCH_POLL_INTERVAL_MS);
  }
}

function stopPolling() {
  if (pollingInterval) {
    clearInterval(pollingInterval);
    pollingInterval = null;
  }
}

// Forced state fetch that bypasses the flowActionInFlight/autoplayInFlight
// guards in applyMatchState. Used to guarantee the post-setup transition into
// the match is never swallowed by an in-flight poll/action.
async function fetchStateForced() {
  try {
    const params = new URLSearchParams();
    if (userId) params.append('userId', userId);
    if (matchId) params.append('matchId', matchId);
    const response = await fetchWithTimeout(`/api/match?${params.toString()}`);
    if (!response.ok) return;
    const data = await response.json();
    applyMatchState(data, { force: true });
  } catch (err) {
    // Non-fatal — the watcher retries on its next tick.
  }
}

// After a lineup is confirmed, actively poll (with force) until the match
// leaves the XI-selection phase, then stop. Bounded by a safety window so it
// never polls forever. Idempotent — a re-arm while already watching is a no-op.
let setupTransitionTimer = null;
function startSetupTransitionWatcher() {
  if (setupTransitionTimer) return;
  const startedAt = Date.now();
  setupTransitionTimer = setInterval(() => {
    if (!matchState || matchState.status !== 'xi_selection' ||
        (Date.now() - startedAt) > 60000) {
      clearInterval(setupTransitionTimer);
      setupTransitionTimer = null;
      return;
    }
    fetchStateForced();
  }, 400);
}

// API Call - GET state
async function fetchWithTimeout(url, options = {}) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), POLL_REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timeoutId);
  }
}

async function fetchState() {
  if (fetchInFlight || flowActionInFlight) return;
  fetchInFlight = true;
  try {
    // Build query safely without sending stringified "null" or "undefined"
    const params = new URLSearchParams();
    if (userId) params.append('userId', userId);
    if (matchId) params.append('matchId', matchId);

    const response = await fetchWithTimeout(`/api/match?${params.toString()}`);
    if (!response.ok) {
      if (response.status === 404) {
        const urlDebug = window.location.search || 'None';
        showError(`No active match room found.<br><span style="font-size:0.85em;opacity:0.85;font-family:monospace;white-space:pre-wrap;display:block;margin-top:10px">Debug: userId=${userId}, matchId=${matchId}, URL params: ${urlDebug}</span>`);
        stopPolling();
        return;
      }
      throw new Error(`HTTP error: ${response.status}`);
    }

    const data = await response.json();
    applyMatchState(data);
  } catch (err) {
    console.error("Polling error:", err);
    pollFailureCount += 1;
    if (pollFailureCount >= MAX_POLL_FAILURES) {
      const timedOut = err.name === 'AbortError';
      showError(timedOut
        ? 'The match is taking too long to load. Please close this window and tap Play Match again.'
        : 'Unable to load the match. Please close this window and tap Play Match again.');
      stopPolling();
    }
  } finally {
    fetchInFlight = false;
  }
}

function applyMatchState(nextState, { force = false } = {}) {
  if (!nextState) return;
  if ((flowActionInFlight || autoplayInFlight) && !force) return;
  const currentRevision = Number(matchState?.ballSeq ?? -1);
  const nextRevision = Number(nextState.ballSeq ?? currentRevision);
  if (Number.isFinite(currentRevision) && Number.isFinite(nextRevision) && nextRevision < currentRevision) return;

  matchState = nextState;
  preloadEventGifs(matchState.eventGifs || {});
  pollFailureCount = 0;

  // Autoplay must default OFF for every new match. The flag is a module global,
  // so in a reused webview it would otherwise leak from a previous match and
  // silently auto-play the human's deliveries/shots. Reset whenever the match
  // id changes so OFF is the deterministic per-match default.
  if (matchState.id && matchState.id !== autoplayMatchId) {
    autoplayMatchId = matchState.id;
    setAutoplayActive(false);
  } else if (!autoplayOffPending && matchState.autoplay?.isOnForMe !== undefined) {
    setAutoplayActive(!!matchState.autoplay.isOnForMe);
  }

  if (identitySelectionRequired) {
    showIdentitySelection(matchState);
    return;
  }

  if (matchState.commentary && matchState.commentary.length > 0) {
    // Find the latest actual delivery. Styled event cards (wicket / new_batsman
    // / over_complete) can sit ahead of it in the reversed feed, so we must not
    // assume index 0 is the ball — otherwise wicket/over deliveries skip the
    // configured outcome GIF/text/haptics.
    const latest = matchState.commentary.find(
      c => c && (c.type === 'ball' || c.runs !== undefined));
    if (latest) {
      const uniqueKey = `${matchState.status}_${matchState.ballSeq ?? latest.over}`;
      const shouldRevealOutcome = lastBallUniqueId
        ? lastBallUniqueId !== uniqueKey
        : flowActionInFlight;
      if (shouldRevealOutcome) triggerMatchEvent(latest);
      lastBallUniqueId = uniqueKey;
    }
  }

  document.getElementById('header-host-name').innerText = (matchState.host.teamName || matchState.host.username || 'HOST').toUpperCase();
  document.getElementById('header-guest-name').innerText = (matchState.guest ? (matchState.guest.teamName || matchState.guest.username) : 'AI XI').toUpperCase();
  document.getElementById('header-match-uuid').innerText = `MATCH ID: ${matchState.id.substring(0, 6).toUpperCase()}`;

  if (matchState.status !== 'innings_break') {
    document.getElementById('innings-break-overlay')?.classList.add('hidden');
  }

  if (matchState.status === 'xi_selection') renderSetupScreen();
  else if (matchState.status === 'innings_break') renderInningsBreakScreen();
  else if (matchState.status === 'innings1' || matchState.status === 'innings2') renderGameplayScreen();
  else if (matchState.status === 'completed') {
    renderGameplayScreen();
    lockCompletedMatchControls();
    renderResultScreen();
    stopPolling();
  }

  setTimeout(runAutoplayAction, AUTOPLAY_ACTION_DELAY_MS);
}

// Has the OTHER side already locked in their lineup? Used to keep the setup
// waiting message honest — once the opponent has submitted we must not keep
// telling the user we're "waiting for opponent to submit".
function opponentHasConfirmed() {
  if (!matchState || !matchState.host) return false;
  const amHost = matchState.host.telegramId != null &&
                 matchState.host.telegramId.toString() === userId.toString();
  if (amHost) {
    // No guest block means the opponent is the AI, which is always ready.
    return matchState.guest ? !!matchState.guest.confirmed : true;
  }
  return !!matchState.host.confirmed;
}

// 1. Setup Screen Rendering
function renderSetupScreen() {
  showScreen('setup-screen');
  const setupSubmitBtn = document.getElementById('submit-setup-btn');
  if (setupSubmitBtn) {
    setupSubmitBtn.disabled = false;
  }
  
  const host = matchState.host.teamName || matchState.host.username;
  const guest = matchState.guest ? (matchState.guest.teamName || matchState.guest.username) : 'AI';
  const pitch = matchState.pitch;
  const overs = matchState.totalOvers;
  
  const isSecondInnings = matchState.currentInningsIdx === 1;
  const inningsText = isSecondInnings ? "2nd Innings" : "1st Innings";
  document.getElementById('setup-match-details').innerText = 
    `${inningsText} | Pitch: ${pitch.toUpperCase()} | Length: ${overs} Over(s) | ${host} vs ${guest}`;

  const cardTitle = document.querySelector('#setup-screen .card-title');
  if (cardTitle) {
    cardTitle.innerText = isSecondInnings ? "Innings 2 Setup" : "Match Setup";
  }

  const isHost = matchState.host.telegramId.toString() === userId.toString();
  const isGuest = matchState.guest && matchState.guest.telegramId && (matchState.guest.telegramId.toString() === userId.toString());
  const isBatting = matchState.myRole === 'batting';
  
  const myConfirmed = isHost ? matchState.host.confirmed : (isGuest ? matchState.guest.confirmed : false);
  
  const battingSetup = document.getElementById('batting-setup');
  const bowlingSetup = document.getElementById('bowling-setup');
  
  // Show input forms only if not confirmed yet
  if (isBatting && !myConfirmed) {
    battingSetup.classList.remove('hidden');
    bowlingSetup.classList.add('hidden');
    
    const strikerContainer = document.getElementById('striker-list');
    const nonStrikerContainer = document.getElementById('non-striker-list');
    if (strikerContainer && nonStrikerContainer) {
      const getBatRating = (p) => p.batting_ovr || p.batting_rating || p.rating || p.ovr || 0;
      const sortedBatting = matchState.battingXI
        .map((p, idx) => ({ p, idx }))
        .filter(({ p }) => isActiveXIPlayer(p))
        .sort((a, b) => getBatRating(b.p) - getBatRating(a.p));

      // Preserve an in-progress pick across the 150ms re-render; otherwise
      // default to the two highest-rated batsmen.
      let sSel = strikerContainer.querySelector('select')?.value;
      let nsSel = nonStrikerContainer.querySelector('select')?.value;
      if (!sSel && !nsSel && sortedBatting.length >= 2) {
        sSel = sortedBatting[0].idx.toString();
        nsSel = sortedBatting[1].idx.toString();
      }

      // Build (or update in place) a native dropdown. Options are created once;
      // subsequent ticks only refresh the selected/disabled state so an open
      // native picker isn't yanked out from under the user.
      const buildDropdown = (container, currentSel, otherSel, onSelect) => {
        container.classList.add('is-dropdown');
        let sel = container.querySelector('select');
        if (!sel || sel.options.length !== sortedBatting.length) {
          container.innerHTML = '';
          sel = document.createElement('select');
          sel.className = 'selection-dropdown';
          sortedBatting.forEach(({ p, idx }) => {
            const opt = document.createElement('option');
            opt.value = idx.toString();
            opt.textContent = `${p.name} — ${getBatRating(p)} OVR · ${p.role || 'Batsman'}`;
            sel.appendChild(opt);
          });
          container.appendChild(sel);
        }
        Array.from(sel.options).forEach(opt => { opt.disabled = (opt.value === otherSel); });
        if (currentSel != null && sel.value !== currentSel) sel.value = currentSel;
        sel.onchange = () => onSelect(sel.value);
      };

      const updateDropdowns = (newStriker, newNonStriker) => {
        buildDropdown(strikerContainer, newStriker, newNonStriker, (idx) => updateDropdowns(idx, newNonStriker));
        buildDropdown(nonStrikerContainer, newNonStriker, newStriker, (idx) => updateDropdowns(newStriker, idx));
      };

      updateDropdowns(sSel, nsSel);
    }
  } else if (matchState.myRole === 'bowling' && !myConfirmed) {
    bowlingSetup.classList.remove('hidden');
    battingSetup.classList.add('hidden');
    
    const container = document.getElementById('bowler-list');
    if (container) {
      const getBowlRating = (p) => p.bowling_ovr || p.bowling_rating || p.rating || p.ovr || 0;
      const sortedBowling = matchState.bowlingXI
        .map((p, idx) => ({ p, idx }))
        .filter(({ p }) => isActiveXIPlayer(p))
        .sort((a, b) => getBowlRating(b.p) - getBowlRating(a.p));

      // Preserve an in-progress pick across the 150ms re-render; otherwise
      // default to the highest-rated bowler.
      let currentSel = container.querySelector('select')?.value;
      if (!currentSel && sortedBowling.length > 0) {
        currentSel = sortedBowling[0].idx.toString();
      }

      container.classList.add('is-dropdown');
      let sel = container.querySelector('select');
      if (!sel || sel.options.length !== sortedBowling.length) {
        container.innerHTML = '';
        sel = document.createElement('select');
        sel.className = 'selection-dropdown';
        sortedBowling.forEach(({ p, idx }) => {
          const opt = document.createElement('option');
          opt.value = idx.toString();
          opt.textContent = `${p.name} — ${getBowlRating(p)} OVR · ${p.bowler_type || 'Bowler'}`;
          sel.appendChild(opt);
        });
        container.appendChild(sel);
      }
      if (currentSel != null && sel.value !== currentSel) sel.value = currentSel;
    }
  } else {
    // Spectator or already confirmed
    battingSetup.classList.add('hidden');
    bowlingSetup.classList.add('hidden');
  }

  // Render Lineup Status Tracker
  const statusTracker = document.getElementById('setup-status-tracker');
  statusTracker.classList.remove('hidden');
  
  const guestName = matchState.guest ? `@${matchState.guest.username}` : 'AI';
  const guestTeam = matchState.guest ? matchState.guest.teamName : 'AI XI';
  const guestStatusText = (matchState.guest && matchState.guest.confirmed) ? '✅ SUBMITTED' : (matchState.guest ? '⏳ CHOOSING...' : '✅ READY');
  const guestStatusClass = (matchState.guest && matchState.guest.confirmed) ? 'submitted' : (matchState.guest ? 'pending' : 'submitted');

  statusTracker.innerHTML = `
    <h3 class="section-title" style="margin-bottom:12px;font-size:12px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px">Lineup Selection Status</h3>
    
    <div class="status-row-item">
      <div class="status-user-info">
        <span class="status-username">@${matchState.host.username} (Host)</span>
        <span class="status-team-name">${matchState.host.teamName}</span>
      </div>
      <span class="status-badge-val ${matchState.host.confirmed ? 'submitted' : 'pending'}">
        ${matchState.host.confirmed ? '✅ SUBMITTED' : '⏳ CHOOSING...'}
      </span>
    </div>
    
    <div class="status-row-item">
      <div class="status-user-info">
        <span class="status-username">${guestName} (Guest)</span>
        <span class="status-team-name">${guestTeam}</span>
      </div>
      <span class="status-badge-val ${guestStatusClass}">
        ${guestStatusText}
      </span>
    </div>
  `;

  // Submit button and waiting states
  const submitBtn = document.getElementById('submit-setup-btn');
  const waitingInd = document.getElementById('setup-waiting');

  if (matchState.myRole === 'spectator') {
    submitBtn.classList.add('hidden');
    waitingInd.classList.remove('hidden');
    waitingInd.querySelector('p').innerText = "Spectating: Waiting for teams to submit Playing XI...";
  } else if (myConfirmed) {
    submitBtn.classList.add('hidden');
    waitingInd.classList.remove('hidden');
    waitingInd.querySelector('p').innerText = opponentHasConfirmed()
      ? "Both lineups locked in — starting match…"
      : "Lineup confirmed! Waiting for opponent…";
  } else {
    submitBtn.classList.remove('hidden');
    waitingInd.classList.add('hidden');
  }

  renderSetupImpactControls();
}

// At the 2nd-innings break the Impact Player option is offered inline inside the
// setup screen (above the openers/bowler pickers). The bottom controls-sheet is
// not used here, so it stays hidden — the banner is non-blocking, so the player
// can simply confirm their setup without interacting with it.
function renderSetupImpactControls() {
  document.getElementById('controls-sheet')?.classList.add('hidden');
  renderImpactEntry('impact-entry-setup');
}

// Setup Submit handler
async function submitSetup() {
  const isBatting = matchState.myRole === 'batting';
  const body = { userId };
  
  if (isBatting) {
    const strikerEl = document.querySelector('#striker-list select');
    const nonStrikerEl = document.querySelector('#non-striker-list select');
    if (!strikerEl || !nonStrikerEl || strikerEl.value === '' || nonStrikerEl.value === '') {
      alert("Please select both Striker and Non-Striker!");
      return;
    }
    const sIdx = parseInt(strikerEl.value);
    const nsIdx = parseInt(nonStrikerEl.value);
    if (sIdx === nsIdx) {
      alert("Striker and Non-Striker cannot be the same player!");
      return;
    }
    body.strikerIdx = sIdx;
    body.nonStrikerIdx = nsIdx;
  } else {
    const bowlerEl = document.querySelector('#bowler-list select');
    if (!bowlerEl || bowlerEl.value === '') {
      alert("Please select opening bowler!");
      return;
    }
    body.bowlerIdx = parseInt(bowlerEl.value);
  }

  document.getElementById('submit-setup-btn').disabled = true;

  try {
    const res = await fetch('/api/match/select-players', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to confirm lineup");
    
    document.getElementById('submit-setup-btn').classList.add('hidden');
    // Hide the just-submitted picker so the stale dropdown doesn't linger
    // behind the waiting indicator while we transition into the match.
    document.getElementById('batting-setup')?.classList.add('hidden');
    document.getElementById('bowling-setup')?.classList.add('hidden');
    const waitingEl = document.getElementById('setup-waiting');
    waitingEl.querySelector('p').innerText = opponentHasConfirmed()
      ? "Both lineups locked in — starting match…"
      : "Lineup confirmed! Waiting for opponent…";
    waitingEl.classList.remove('hidden');
    // The passive 150ms poll applies state WITHOUT `force`, so the moment the
    // server flips xi_selection → innings1 the update can be swallowed by the
    // flowActionInFlight/autoplayInFlight guard in applyMatchState — leaving the
    // player stranded on this screen until they reopen the Mini App. Actively
    // drive the transition with a forced watcher that bypasses those guards.
    startSetupTransitionWatcher();
    fetchState();
  } catch (err) {
    alert(err.message);
    document.getElementById('submit-setup-btn').disabled = false;
  }
}

// 2. Gameplay Screen Rendering
function renderGameplayScreen() {
  showScreen('gameplay-screen');

  // Run display
  document.getElementById('live-runs-display').innerText = 
    `${matchState.score.runs}/${matchState.score.wickets}`;

  // Overs / Balls
  const totalBallsBowled = matchState.score.ballsBowled ??
    ((matchState.score.overs * ballsPerUnit()) + matchState.score.balls);
  const oversUnitEl = document.querySelector('.overs-display .ov-unit');
  if (isHundredFormat()) {
    document.getElementById('live-overs').innerText = totalBallsBowled;
    document.getElementById('total-overs').innerText = matchState.totalBalls || (matchState.totalOvers * 5);
    if (oversUnitEl) oversUnitEl.innerText = 'BALLS';
  } else {
    document.getElementById('live-overs').innerText =
      `${matchState.score.overs}.${matchState.score.balls}`;
    document.getElementById('total-overs').innerText = matchState.totalOvers;
    if (oversUnitEl) oversUnitEl.innerText = 'OV';
  }

  // CRR (always per-6-balls, regardless of the unit shown above)
  const crr = totalBallsBowled > 0 ? ((matchState.score.runs / totalBallsBowled) * 6).toFixed(1) : "0.0";
  document.getElementById('live-crr').innerText = crr;

  // Batting / bowling team labels in the score box
  const batLabel = document.getElementById('bat-team-label');
  const bowlLabel = document.getElementById('bowl-team-label');
  if (batLabel) batLabel.innerText = (matchState.score.batTeamName || '').toUpperCase();
  if (bowlLabel) bowlLabel.innerText = matchState.score.bowlTeamName ? `v ${matchState.score.bowlTeamName}` : '';

  // Projected score — 1st innings only (target bar covers the chase)
  const projBadge = document.getElementById('proj-badge');
  if (projBadge) {
    if (matchState.status !== 'innings2' && matchState.score.projected != null) {
      projBadge.classList.remove('hidden');
      document.getElementById('live-projected').innerText = matchState.score.projected;
    } else {
      projBadge.classList.add('hidden');
    }
  }

  // Recent-ball doodle timeline
  renderBallTimeline();

  // Toss/Election Badge
  const tossBadge = document.getElementById('toss-badge-display');
  const dec = matchState.tossDecision === 'bat' ? 'ELECTED TO BAT' : 'ELECTED TO BOWL';
  const winnerName = matchState.tossWinnerId === matchState.host.telegramId 
    ? (matchState.host.teamName || matchState.host.username || 'Host').toUpperCase() 
    : (matchState.guest ? (matchState.guest.teamName || matchState.guest.username || 'Guest').toUpperCase() : 'AI XI');
  tossBadge.innerText = `⚫ ${winnerName} WON & ${dec}`;

  // Innings 2 Target logic
  const targetDisplay = document.getElementById('target-display');
  if (matchState.status === 'innings2' && matchState.score.target !== null) {
    targetDisplay.classList.remove('hidden');
    const runsNeeded = Math.max(0, matchState.score.target - matchState.score.runs);
    const totalBalls = matchState.totalBalls || (matchState.totalOvers * ballsPerUnit());
    const ballsRemaining = Math.max(0, totalBalls - totalBallsBowled);
    
    document.getElementById('target-runs-needed').innerText = runsNeeded;
    document.getElementById('target-balls-left').innerText = ballsRemaining;
  } else {
    targetDisplay.classList.add('hidden');
  }

  // Free hit banner — armed after a no-ball (UnderCover /cric parity)
  const freeHitBanner = document.getElementById('free-hit-banner');
  if (freeHitBanner) {
    if (matchState.freeHit) freeHitBanner.classList.remove('hidden');
    else freeHitBanner.classList.add('hidden');
  }

  // Active Striker / Non-Striker / Bowler details
  const striker = matchState.striker;
  const nonStriker = matchState.nonStriker;
  const bowler = matchState.bowler;

  if (striker) {
    document.getElementById('striker-name').innerText = striker.name;
    document.getElementById('striker-stats').innerText = `${striker.stats.runs} (${striker.stats.balls})`;
    document.getElementById('card-striker').classList.add('active');
  } else {
    document.getElementById('striker-name').innerText = '--';
    document.getElementById('striker-stats').innerText = '0(0)';
    document.getElementById('card-striker').classList.remove('active');
  }

  if (nonStriker) {
    document.getElementById('non-striker-name').innerText = nonStriker.name;
    document.getElementById('non-striker-stats').innerText = `${nonStriker.stats.runs} (${nonStriker.stats.balls})`;
  } else {
    document.getElementById('non-striker-name').innerText = '--';
    document.getElementById('non-striker-stats').innerText = '0(0)';
  }

  if (bowler) {
    document.getElementById('bowler-name-stats').innerText = 
      `${bowler.name} ${bowler.stats.wickets}-${bowler.stats.runsConceded}`;
  } else {
    document.getElementById('bowler-name-stats').innerText = '0-0';
  }

  // Partnership
  if (matchState.partnership) {
    document.getElementById('partnership-display').innerText = 
      `${matchState.partnership.runs}(${matchState.partnership.balls})`;
  } else {
    document.getElementById('partnership-display').innerText = `0(0)`;
  }

  // Render controls block
  renderControlsSection();

  // Render commentary list
  renderCommentaryFeed();

  // Render Scorecard & Squads panels (so they are fresh if user switches tabs)
  renderScorecardPanel();
  renderManhattanChart();
  renderPartnershipHistory();
  renderOverByOver();
  renderSquadsPanel();
}

// 3. Render Interactive Controls
function renderControlsSection() {
  const activeBlock = document.getElementById('controls-sheet');
  const waitingBlock = document.getElementById('controls-waiting');
  
  if (matchState.status === 'completed') {
    activeBlock.classList.add('hidden');
    return;
  }
  
  activeBlock.classList.remove('hidden');

  const promptText = document.getElementById('controls-prompt-text');
  const promptSubtitle = document.getElementById('controls-prompt-subtitle');
  const incomingCard = document.getElementById('incoming-delivery-container');
  const battingControls = document.getElementById('batting-controls');
  const bowlingControls = document.getElementById('bowling-controls');
  const wicketBatsmanControls = document.getElementById('wicket-batsman-controls');
  const overBowlerControls = document.getElementById('over-bowler-controls');
  const hideActionSections = () => {
    battingControls.classList.add('hidden');
    bowlingControls.classList.add('hidden');
    wicketBatsmanControls.classList.add('hidden');
    overBowlerControls.classList.add('hidden');
  };

  renderAutoplayStatusMessage();

  // Spectator role
  if (matchState.myRole === 'spectator') {
    wasMyTurn = false;
    waitingBlock.classList.remove('hidden');
    
    let waitMsg = "Spectating match...";
    if (matchState.turnState === 'bowling_delivery') {
      waitMsg = "Bowler is preparing delivery...";
    } else if (matchState.turnState === 'batting_shot') {
      waitMsg = "Batsman is preparing shot...";
    } else if (matchState.turnState === 'selecting_wicket_batsman') {
      waitMsg = "Waiting for batsman selection...";
    } else if (matchState.turnState === 'selecting_over_bowler') {
      waitMsg = "Waiting for bowler selection...";
    }
    
    document.getElementById('waiting-status-text').innerText = waitMsg;
    promptText.innerText = "👁️ SPECTATOR MODE";
    promptSubtitle.innerText = waitMsg;

    hideActionSections();
    incomingCard.classList.add('hidden');
    return;
  }

  // Show the batsman's shot sheet before the bowler has delivered. Presentation
  // is independent from authorization: these cards preview the available shots,
  // but remain disabled until the batting_shot phase becomes actionable.
  const isBatsmanWaitingForDelivery = matchState.myRole === 'batting'
    && matchState.turnState === 'bowling_delivery'
    && matchState.isMyTurn === false;
  if (isBatsmanWaitingForDelivery) {
    wasMyTurn = false;
    waitingBlock.classList.toggle('hidden', autoplayActive);
    promptText.innerText = autoplayActive ? "🏏 BATTING AUTOPLAY" : "🏏 GET READY TO PLAY";
    promptSubtitle.innerText = autoplayActive ? "Autoplay on" : "Bowler running in…";
    hideActionSections();
    resetAutoplayQuickCards();
    incomingCard.classList.add('hidden');
    if (autoplayActive) {
      renderAutoplayQuickCard('batting_wait');
    } else {
      battingControls.classList.remove('hidden');
      renderBattingShots({ disabled: true });
    }
    return;
  }

  // It is NOT my turn — surface the live action rather than a generic wait.
  if (!matchState.isMyTurn) {
    wasMyTurn = false;
    waitingBlock.classList.remove('hidden');

    let waitMsg = "Waiting for opponent…";
    let header = "⏳ OPPONENT'S TURN";
    if (matchState.turnState === 'bowling_delivery') {
      waitMsg = "Bowler is running in…";
      header = "🎳 BOWLER IS BOWLING";
    } else if (matchState.turnState === 'batting_shot') {
      waitMsg = "Batsman is facing the delivery…";
      header = "🏏 BATSMAN ON STRIKE";
    } else if (matchState.turnState === 'selecting_wicket_batsman') {
      waitMsg = "New batsman walking in…";
      header = "⚠️ NEW BATSMAN INCOMING";
    } else if (matchState.turnState === 'selecting_over_bowler') {
      waitMsg = "Setting up the next over…";
      header = "🔄 NEXT OVER SETUP";
    }
    document.getElementById('waiting-status-text').innerText = waitMsg;

    promptText.innerText = header;
    promptSubtitle.innerText = waitMsg;

    hideActionSections();
    incomingCard.classList.add('hidden');
    return;
  }

  // It IS my turn!
  waitingBlock.classList.add('hidden');

  // Auto-expand the sheet on each NEW actionable turn (new ball / phase) so the
  // controls are never stranded behind a collapsed sheet — a collapsed sheet on
  // the batsman's turn would soft-lock the over (the bowler waits forever). A
  // per-ball signature means we expand once per turn but still let the user
  // manually minimize within the same turn.
  const turnSig = `${matchState.turnState}|${matchState.score.overs}.${matchState.score.balls}|${matchState.score.wickets}`;
  const isNewActionableTurn = lastActionableSig !== turnSig;
  if (isNewActionableTurn) {
    if (activeBlock.classList.contains('minimized')) {
      activeBlock.classList.remove('minimized');
      const iconSvg = document.getElementById('toggle-controls-icon');
      if (iconSvg) {
        iconSvg.innerHTML = '<polyline points="6 9 12 15 18 9"></polyline>';
      }
    }
    lastActionableSig = turnSig;
  }
  wasMyTurn = true;

  // Hide all sections initially
  hideActionSections();
  resetAutoplayQuickCards();

  if (autoplayActive && matchState.turnState === 'bowling_delivery') {
    promptText.innerText = "🎳 BOWLING AUTOPLAY";
    promptSubtitle.innerText = "Autoplay on";
    incomingCard.classList.add('hidden');
    renderAutoplayQuickCard('bowling');
    return;
  }

  if (autoplayActive && matchState.turnState === 'batting_shot') {
    promptText.innerText = "🏏 BATTING AUTOPLAY";
    promptSubtitle.innerText = "Autoplay on";
    incomingCard.classList.add('hidden');
    renderAutoplayQuickCard('batting');
    return;
  }

  if (matchState.turnState === 'bowling_delivery') {
    promptText.innerText = "🎳 BOWLER CONTROLS";
    promptSubtitle.innerText = matchState.deliveryOptions?.is_spinner
      ? "Tap a spin delivery"
      : "Pick a delivery & length";
    document.getElementById('bowling-controls').classList.remove('hidden');
    document.getElementById('incoming-delivery-container').classList.add('hidden');
    renderBowlerVariations();
  } 
  else if (matchState.turnState === 'batting_shot') {
    promptText.innerText = "🏏 CHOOSE YOUR SHOT";
    promptSubtitle.innerText = "Pick your shot";

    if (matchState.currentDelivery) {
      const delName = String(matchState.currentDelivery).replace(/_/g, ' ').toUpperCase();
      const speedName = matchState.currentSpeed ? String(matchState.currentSpeed).toUpperCase() : 'NORMAL';
      const speedKmh = Number.isFinite(Number(matchState.currentSpeedKmh))
        ? ` • ${Number(matchState.currentSpeedKmh)} KM/H`
        : '';
      const displayText = `${speedName} ${delName}${speedKmh}`;

      // Show the incoming delivery once — in its dedicated card only, not also
      // duplicated in the subtitle.
      incomingCard.classList.remove('hidden');
      document.getElementById('incoming-delivery-val').innerText = displayText;
    } else {
      incomingCard.classList.add('hidden');
    }
    document.getElementById('batting-controls').classList.remove('hidden');
    renderBattingShots({ disabled: false });
    if (isNewActionableTurn) {
      const sheetBody = document.querySelector('.sheet-body');
      if (sheetBody) sheetBody.scrollTop = 0;
    }
  } 
  else if (matchState.turnState === 'selecting_wicket_batsman') {
    promptText.innerText = "⚠️ WICKET! SELECT REPLACEMENT";
    promptSubtitle.innerText = "Tap a batsman to continue";
    document.getElementById('wicket-batsman-controls').classList.remove('hidden');
    document.getElementById('incoming-delivery-container').classList.add('hidden');
    renderWicketBatsmanSelectionSheet();
    renderImpactEntry('impact-entry-wicket');
  }
  else if (matchState.turnState === 'selecting_over_bowler') {
    promptText.innerText = "🎳 SELECT BOWLER FOR NEXT OVER";
    promptSubtitle.innerText = "Tap a bowler to continue";
    document.getElementById('over-bowler-controls').classList.remove('hidden');
    document.getElementById('incoming-delivery-container').classList.add('hidden');
    renderOverBowlerSelectionSheet();
    renderImpactEntry('impact-entry-bowler');
  }
}


// Renders the Impact Player entry point into the given selection sheet's mount:
//   • already used  → a small "✨ Impact Used" chip (the only lasting indicator)
//   • usable now    → a "✨ Use Impact Player" banner + availability hint
//   • otherwise     → nothing (mount cleared)
// If the picker is currently open in THIS mount, it re-renders the picker in
// place instead — keeping the 150ms poll idempotent so an in-progress pick
// (incoming/outgoing) is never wiped out.
function renderImpactEntry(mountId) {
  const mount = document.getElementById(mountId);
  if (!mount || !matchState) return;
  const impact = matchState.impactPlayer || {};

  // Picker is open here → keep it mounted and refresh its contents only.
  if (impactSelection.open && impactSelection.mountId === mountId) {
    const picker = document.getElementById('impact-player-picker');
    if (picker && picker.parentElement !== mount) mount.appendChild(picker);
    if (mount.parentElement) mount.parentElement.classList.add('impact-picking');
    renderImpactPlayerPicker();
    return;
  }

  if (matchState.myRole === 'spectator' || matchState.status === 'completed') {
    mount.innerHTML = '';
    return;
  }

  if (impact.used) {
    mount.innerHTML = '<div class="impact-used-chip">✨ Impact Player used</div>';
    return;
  }

  if (!impact.canUse) {
    mount.innerHTML = '';
    return;
  }

  const hint = impact.legalBreak
    ? `Available now — ${impact.legalBreak}.`
    : (impact.message || 'Bring on a substitute from outside your Playing XI.');
  mount.innerHTML = `
    <button type="button" class="impact-entry-banner">
      <span class="impact-entry-spark">✨</span>
      <span class="impact-entry-text">
        <span class="impact-entry-title">Use Impact Player</span>
        <span class="impact-entry-hint">${hint}</span>
      </span>
      <span class="impact-entry-chevron">›</span>
    </button>`;
  mount.querySelector('.impact-entry-banner').onclick = () => openImpactPlayerPicker(mountId);
}

// Move the single picker node back to its hidden home so a subsequent list
// rebuild (container.innerHTML = '') can never delete it, and drop the
// "picking" state so the underlying selection list reappears.
function homeImpactPicker() {
  const picker = document.getElementById('impact-player-picker');
  const home = document.getElementById('impact-player-picker-home');
  if (picker && home && picker.parentElement !== home) home.appendChild(picker);
  if (picker) picker.classList.add('hidden');
  document.querySelectorAll('.impact-picking').forEach(el => el.classList.remove('impact-picking'));
}

function openImpactPlayerPicker(mountId) {
  const mount = document.getElementById(mountId);
  if (!mount) return;
  impactSelection = { incoming: null, outgoing: null, open: true, mountId };
  const picker = document.getElementById('impact-player-picker');
  if (picker) {
    mount.appendChild(picker);
    picker.classList.remove('hidden');
  }
  // Hide the surrounding selection list/buttons for a focused, in-place swap.
  if (mount.parentElement) mount.parentElement.classList.add('impact-picking');
  renderImpactPlayerPicker();
}

// Cancel/Back: close the picker WITHOUT touching controls-sheet visibility (the
// old "Maybe Later" hid the whole sheet and could soft-lock a live selection).
// The underlying selection list is untouched, so it simply reappears.
function closeImpactPlayerPicker(opts = {}) {
  const mountId = impactSelection.mountId;
  impactSelection = { incoming: null, outgoing: null, open: false, mountId: null };
  homeImpactPicker();
  if (!opts.silent && mountId) renderImpactEntry(mountId);
}

function renderImpactPlayerPicker() {
  const impact = matchState.impactPlayer || {};
  const picker = document.getElementById('impact-player-picker');
  const incomingList = document.getElementById('impact-incoming-list');
  const outgoingWrap = document.getElementById('impact-outgoing-wrap');
  const outgoingList = document.getElementById('impact-outgoing-list');
  const confirmBtn = document.getElementById('confirm-impact-player-btn');
  const summary = document.getElementById('impact-confirm-summary');
  const stepText = document.getElementById('impact-player-step-text');
  if (!picker || !incomingList || !outgoingList || !confirmBtn) return;
  picker.classList.remove('hidden');

  const rating = (p) => p.ovr || p.rating || 0;
  if (stepText) stepText.innerText = impactSelection.incoming
    ? 'Step 2 — pick the player to replace:'
    : 'Step 1 — pick the substitute coming in:';

  const buildPlayerRow = (p, onClick, selected) => {
    const div = document.createElement('div');
    div.className = `selection-item ${selected ? 'selected' : ''} ${p.disabled ? 'disabled' : ''}`;
    div.innerHTML = `
      <span class="selection-item-name">${p.name}</span>
      <span class="selection-item-meta">${rating(p)} OVR · ${p.role || p.category || 'Player'}${p.disabledReason ? ' · ' + p.disabledReason : ''}</span>
    `;
    if (!p.disabled) div.onclick = onClick;
    return div;
  };

  incomingList.innerHTML = '';
  // Highest-rated bench options first so the strongest sub is easy to find.
  const incoming = (impact.incomingOptions || []).slice().sort((a, b) => rating(b) - rating(a));
  if (!incoming.length) {
    incomingList.innerHTML = '<div class="no-options">No bench players available outside Playing XI</div>';
  } else {
    incoming.forEach(p => incomingList.appendChild(buildPlayerRow(p, () => {
      impactSelection.incoming = p;
      impactSelection.outgoing = null;
      renderImpactPlayerPicker();
    }, impactSelection.incoming && impactSelection.incoming.id === p.id)));
  }

  outgoingList.innerHTML = '';
  if (impactSelection.incoming) {
    outgoingWrap.classList.remove('hidden');
    (impact.replaceablePlayers || []).forEach(p => outgoingList.appendChild(buildPlayerRow(p, () => {
      impactSelection.outgoing = p;
      renderImpactPlayerPicker();
    }, impactSelection.outgoing && impactSelection.outgoing.id === p.id)));
  } else {
    outgoingWrap.classList.add('hidden');
  }

  // Confirm-summary step: show "X comes in for Y" before the final tap so an
  // accidental swap is easy to catch.
  const ready = impactSelection.incoming && impactSelection.outgoing;
  if (summary) {
    if (ready) {
      summary.classList.remove('hidden');
      summary.innerHTML = `<b>${impactSelection.incoming.name}</b> comes in for <b>${impactSelection.outgoing.name}</b>`;
    } else {
      summary.classList.add('hidden');
      summary.innerHTML = '';
    }
  }
  confirmBtn.disabled = !ready;
}

async function submitImpactPlayer() {
  if (!impactSelection.incoming || !impactSelection.outgoing) return;
  const btn = document.getElementById('confirm-impact-player-btn');
  try {
    if (btn) btn.disabled = true;
    const res = await fetch('/api/match/impact-player', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        userId,
        matchId,
        inRosterId: impactSelection.incoming.id,
        outRosterId: impactSelection.outgoing.id
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || data.error || 'Failed to use Impact Player');
    closeImpactPlayerPicker({ silent: true });
    if (data.matchState) applyMatchState(data.matchState);
    else fetchState();
  } catch (err) {
    alert(err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Render a delivery dropdown first, then only the selected variation's lengths.
function renderBowlerVariations() {
  const deliverySelect = document.getElementById('bowler-delivery-select');
  const dropdownSection = document.getElementById('bowler-delivery-dropdown-section');
  const deliveryGrid = document.getElementById('bowler-delivery-grid');
  const lengthSection = document.getElementById('bowler-length-section');
  const lengthTitle = document.getElementById('bowler-length-title');
  const bowlerType = matchState.bowler?.bowler_type || matchState.bowler?.bowl_style || 'fast';
  const bowlerName = matchState.bowler?.name || '';
  const normalizedBowlerType = bowlerType.toLowerCase();
  const shared = matchState.deliveryOptions || {};
  const isOffSpin = bowlerType === 'off_spin' || normalizedBowlerType.includes('off spin') || normalizedBowlerType.includes('off-spin');
  const isLegSpin = bowlerType === 'leg_spin' || normalizedBowlerType.includes('leg spin') || normalizedBowlerType.includes('leg-spin');
  const isSpin = typeof shared?.is_spinner === 'boolean'
    ? shared.is_spinner
    : (isOffSpin || isLegSpin || normalizedBowlerType.includes('spin'));

  // Reuse the Telegram /playmatch vocabulary supplied by the backend. Keep a
  // defensive fallback for older servers so a cached client never soft-locks.
  const spinDeliveries = Array.isArray(shared.deliveries) && shared.deliveries.length
    ? shared.deliveries
    : (isSpin ? ['Leg Break', 'Googly'] : []);
  const variations = Array.isArray(shared.variations) && shared.variations.length
    ? shared.variations
    : (isSpin ? [] : ['Seam Up', 'Outswing']);
  const lengths = Array.isArray(shared.lengths) ? shared.lengths : [];
  const deliveryNames = spinDeliveries.length ? spinDeliveries : variations;
  const cacheKey = JSON.stringify({
    bowlerName,
    deliveryNames,
    lengths,
    isSpin,
    ballSeq: matchState.ballSeq,
  });
  if (deliverySelect.dataset.rendered === cacheKey) return;
  deliverySelect.dataset.rendered = cacheKey;
  selectedDelivery = null;

  deliverySelect.innerHTML = '<option value="">Choose a delivery...</option>';
  deliveryGrid.innerHTML = '';
  lengthSection.classList.add('hidden');
  dropdownSection.classList.toggle('hidden', isSpin);

  if (!isSpin) {
    deliveryNames.forEach(name => {
      const option = document.createElement('option');
      option.value = name;
      option.innerText = name;
      deliverySelect.appendChild(option);
    });
  }

  const renderSelectedDeliveryLengths = (variation) => {
    selectedDelivery = null;
    deliveryGrid.innerHTML = '';
    if (!variation) {
      lengthSection.classList.add('hidden');
      return;
    }

    const choices = spinDeliveries.length || !lengths.length
      ? [{ id: variation, name: variation }]
      : lengths.map(length => ({
          id: `${variation} ${length}`.trim(),
          name: `${variation} ${length}`.trim(),
        }));
    lengthTitle.innerText = choices.length === 1 ? 'Confirm Delivery' : `Choose ${variation} Length`;
    choices.forEach(delivery => {
      const btn = document.createElement('button');
      btn.className = 'btn-variation';
      btn.innerText = delivery.name;
      btn.dataset.delivery = delivery.id;
      btn.addEventListener('click', (event) => {
        deliveryGrid.querySelectorAll('.btn-variation').forEach(button => button.classList.remove('active'));
        event.currentTarget.classList.add('active');
        selectedDelivery = event.currentTarget.dataset.delivery;
      });
      deliveryGrid.appendChild(btn);
    });
    lengthSection.classList.remove('hidden');
  };
  deliverySelect.onchange = event => renderSelectedDeliveryLengths(event.currentTarget.value);
  if (isSpin) {
    lengthTitle.innerText = 'Select Delivery';
    spinDeliveries.forEach(delivery => {
      const btn = document.createElement('button');
      btn.className = 'btn-variation';
      btn.innerText = delivery;
      btn.dataset.delivery = delivery;
      btn.addEventListener('click', (event) => {
        deliveryGrid.querySelectorAll('.btn-variation').forEach(button => button.classList.remove('active'));
        event.currentTarget.classList.add('active');
        selectedDelivery = event.currentTarget.dataset.delivery;
      });
      deliveryGrid.appendChild(btn);
    });
    lengthSection.classList.remove('hidden');
  }

  // Dynamically render speed variations for pacers only.
  document.getElementById('bowling-speed-section').classList.toggle('hidden', isSpin);
  if (isSpin) {
    selectedSpeed = 'normal';
  } else {
    const speedGroup = document.querySelector('.speed-button-group');
    if (speedGroup) {
      speedGroup.innerHTML = `
        <button class="btn btn-speed${selectedSpeed === 'fast' ? ' active' : ''}" data-speed="fast">Fast</button>
        <button class="btn btn-speed${selectedSpeed === 'normal' ? ' active' : ''}" data-speed="normal">Normal</button>
        <button class="btn btn-speed${selectedSpeed === 'slow' ? ' active' : ''}" data-speed="slow">Slow</button>`;
      speedGroup.querySelectorAll('.btn-speed').forEach(btn => {
        btn.addEventListener('click', (event) => {
          speedGroup.querySelectorAll('.btn-speed').forEach(button => button.classList.remove('active'));
          event.currentTarget.classList.add('active');
          selectedSpeed = event.currentTarget.dataset.speed;
        });
      });
    }
  }
}

// Render batting shot event listeners
// Same shot vocabulary + icons as services.bowling_service
// (AVAILABLE_SHOTS / SHOT_ICONS). Keep this list in sync.
const SHOT_ICON_MAP = {
  'Defend': '🛡️', 'Drive': '☄️', 'Cut': '✂️', 'Pull': '🌪️',
  'Flick': '🪄', 'Sweep': '🧹', 'Loft': '🚀', 'Slog': '💣',
};
const BATTING_SHOTS = [
  'Defend', 'Drive', 'Cut', 'Pull',
  'Flick', 'Sweep', 'Loft', 'Slog',
].map(shot => ({ shot, label: shot, icon: SHOT_ICON_MAP[shot] || '🏏' }));

async function submitShot(shot) {
  // Guard against double-taps: a second tap before the first shot resolved used
  // to fire a second /api/match/action request, which could re-resolve the ball
  // and flip the displayed outcome (e.g. 4 → 2). Ignore re-entry while a shot is
  // already in flight, and disable the shot buttons for immediate feedback.
  if (flowActionInFlight) return;
  flowActionInFlight = true;
  disableShotButtons();
  // Collapse the shot sheet immediately so the resolved outcome (and the event
  // box) is fully visible. The per-turn auto-expand re-opens it on the next ball.
  minimizeControlsSheet();
  showFlowStage('🏏 Shot played', `${shot} selected`, 'evt-runs');
  try {
    const res = await fetch('/api/match/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId, matchId, type: 'shot', action: { shot } })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to submit shot");
    // The response already carries the resolved outcome (server applies it
    // synchronously) — apply it the instant it arrives and bypass any older
    // poll response that was still in flight when the shot was tapped.
    if (data.matchState) applyMatchState(data.matchState, { force: true });
    else { flowActionInFlight = false; fetchState(); }
  } catch (err) {
    alert(err.message);
  } finally {
    flowActionInFlight = false;
  }
}

// Disable the batting shot buttons immediately after a shot is tapped so a
// rapid second tap can't register before the sheet collapses / re-renders.
// The next actionable turn re-renders the grid fresh via renderBattingShots.
function disableShotButtons() {
  const grid = document.querySelector('#batting-controls .button-grid');
  if (!grid) return;
  grid.querySelectorAll('button').forEach(btn => { btn.disabled = true; });
}

// Minimize the bottom control sheet and flip its toggle chevron to "expand".
function minimizeControlsSheet() {
  const sheet = document.getElementById('controls-sheet');
  if (sheet) sheet.classList.add('minimized');
  const iconSvg = document.getElementById('toggle-controls-icon');
  if (iconSvg) iconSvg.innerHTML = '<polyline points="18 15 12 9 6 15"></polyline>';
}

// Build the shot grid from a fixed list rather than cloning static DOM, so the
// batsman always gets a usable grid. A missing/empty grid soft-locks the over
// (the bowler waits forever for a shot that can never be played).
function renderBattingShots({ disabled = false } = {}) {
  const shotSection = document.getElementById('batting-controls');
  if (!shotSection) return;
  let grid = shotSection.querySelector('.button-grid');
  if (!grid) {
    grid = document.createElement('div');
    grid.className = 'button-grid premium-grid';
    shotSection.insertBefore(grid, shotSection.firstChild);
  }

  grid.innerHTML = '';
  BATTING_SHOTS.forEach(s => {
    const btn = document.createElement('button');
    btn.className = 'btn btn-action-card';
    btn.dataset.shot = s.shot;
    btn.innerHTML = `<span class="shot-icon">${s.icon || '🏏'}</span><span class="label">${s.label}</span>`;
    if (disabled) {
      btn.disabled = true;
      btn.classList.add('is-waiting-for-delivery');
    } else {
      btn.addEventListener('click', (e) => submitShot(e.currentTarget.dataset.shot));
    }
    grid.appendChild(btn);
  });
}

// Bowling Submit Handler
async function submitDelivery() {
  if (!selectedDelivery) {
    alert("Please select a delivery and length first!");
    return;
  }
  // Guard against double-taps, same as submitShot — a second delivery request
  // fired before the first resolved could re-resolve the ball and flip the
  // outcome. Ignore re-entry while a delivery is already in flight.
  if (flowActionInFlight) return;
  flowActionInFlight = true;

  // Minimize sheet immediately and flash the delivery for tactile feedback —
  // the response below already carries the resolved outcome (vsbot turns
  // resolve synchronously on the server), so this must not gate anything.
  minimizeControlsSheet();
  showFlowStage('🎳 Bowler delivers', `${selectedDelivery} at ${selectedSpeed} pace`, 'evt-runs');

  try {
    const res = await fetch('/api/match/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        userId,
        matchId,
        type: 'delivery',
        action: {
          delivery: selectedDelivery,
          speed: selectedSpeed
        }
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to submit delivery");
    selectedDelivery = null;
    document.getElementById('bowler-delivery-select').value = '';
    document.getElementById('bowler-delivery-grid').innerHTML = '';
    document.getElementById('bowler-length-section').classList.add('hidden');
    // Only the human-vs-human case genuinely still has the batsman to come —
    // vsbot/instant resolution means the response already carries the bot's
    // shot AND the ball outcome, so showing "Batsman ready" would be stale/
    // misleading. Reveal the real result immediately via applyMatchState in
    // that case; keep the legit "waiting on opponent" message for true PvP.
    const stillPendingBatsman = data.matchState?.turnState === 'batting_shot'
      && data.matchState?.isMyTurn === false;
    if (stillPendingBatsman) showFlowStage('🏏 Batsman ready', 'Ready to play the shot', 'evt-dot');
    if (data.matchState) applyMatchState(data.matchState, { force: true });
    else { flowActionInFlight = false; fetchState(); }
  } catch (err) {
    alert(err.message);
  } finally {
    flowActionInFlight = false;
  }
}

// Selection list for incoming batsman
function renderWicketBatsmanSelectionSheet() {
  const container = document.getElementById('wicket-batsman-list');
  if (!container) return;

  const getBatRating = (p) => p.batting_ovr || p.batting_rating || p.rating || p.ovr || 0;

  const bench = matchState.battingXI.map((player, index) => ({ player, index }))
    .filter(item => {
      // Exclude inactive preserved identities after impact substitutions
      if (!isActiveXIPlayer(item.player)) return false;

      // Exclude players currently at the crease
      if (matchState.striker && item.player.id.toString() === matchState.striker.id.toString()) return false;
      if (matchState.nonStriker && item.player.id.toString() === matchState.nonStriker.id.toString()) return false;
      
      // Exclude players who are already out
      const stats = matchState.stats[item.player.id.toString()];
      if (stats && stats.isOut) return false;

      return true;
    })
    .sort((a, b) => getBatRating(b.player) - getBatRating(a.player));

  if (bench.length === 0) {
    container.innerHTML = '<div class="no-options">No batsmen remaining</div>';
    return;
  }

  let currentSel = container.querySelector('.selection-item.selected')?.dataset.index;
  if (!bench.some(item => item.index.toString() === currentSel) && bench.length > 0) {
    currentSel = bench[0].index.toString();
  }

  const build = (selectedIdx) => {
    container.innerHTML = '';
    bench.forEach(item => {
      const div = document.createElement('div');
      div.className = 'selection-item';
      div.dataset.index = item.index;
      
      const isSelected = selectedIdx === item.index.toString();
      if (isSelected) div.classList.add('selected');
      
      div.innerHTML = `
        <span class="selection-item-name">${item.player.name}${item.player.impact_replacement ? ' <span class="impact-badge">✨</span>' : ''}</span>
        <span class="selection-item-meta">${getBatRating(item.player)} OVR - ${item.player.role || 'Batsman'}</span>
      `;
      
      div.onclick = () => {
        if (selectionSubmitInFlight) return;
        build(item.index.toString());
        selectionSubmitInFlight = true;
        document.getElementById('controls-sheet').classList.add('minimized');
        selectNextBatsman(item.index).finally(() => { selectionSubmitInFlight = false; });
      };
      container.appendChild(div);
    });
  };

  build(currentSel);
}

async function selectNextBatsman(index) {
  try {
    const res = await fetch('/api/match/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        userId,
        matchId,
        type: 'wicket_batsman',
        action: { index }
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to select batsman");
    fetchState();
  } catch (err) {
    alert(err.message);
  }
}

// Selection list for selecting next bowler
// Selection list for selecting next bowler
function renderOverBowlerSelectionSheet() {
  const container = document.getElementById('over-bowler-list');
  if (!container) return;

  const getBowlRating = (p) => p.bowling_ovr || p.bowling_rating || p.rating || p.ovr || 0;
  const currentBowlIdx = matchState.bowler ? matchState.bowlingXI.findIndex(p => p.id.toString() === matchState.bowler.id.toString()) : null;

  const maxOvers = matchState.totalOvers <= 5 ? 1 : (matchState.totalOvers <= 10 ? 2 : (matchState.totalOvers <= 15 ? 3 : 4));

  // Fallback check: if ALL other bowlers have also exceeded limits, everyone except the consecutive bowler is allowed
  const otherEligible = matchState.bowlingXI.some((p, idx) => {
    if (!isActiveXIPlayer(p)) return false;
    if (idx === currentBowlIdx) return false;
    const stats = matchState.stats[p.id] || { overs: 0 };
    return (stats.overs || 0) < maxOvers;
  });

  const bench = matchState.bowlingXI.map((player, index) => {
    const stats = matchState.stats[player.id] || { overs: 0 };
    let eligible = true;
    let reason = '';

    if (index === currentBowlIdx) {
      eligible = false;
      reason = 'Bowled last over';
    } else if (stats.overs >= maxOvers) {
      if (otherEligible) {
        eligible = false;
        reason = `Max limit reached (${stats.overs}/${maxOvers} ov)`;
      } else {
        reason = `Quota full (Fallback allowed)`;
      }
    }

    return { player, index, eligible, reason, overs: stats.overs };
  }).filter(item => isActiveXIPlayer(item.player)).sort((a, b) => {
    if (a.eligible !== b.eligible) return b.eligible ? 1 : -1;
    return getBowlRating(b.player) - getBowlRating(a.player);
  });

  let currentSel = container.querySelector('.selection-item.selected')?.dataset.index;
  const firstEligible = bench.find(item => item.eligible);
  if (!bench.some(item => item.index.toString() === currentSel) && firstEligible) {
    currentSel = firstEligible.index.toString();
  }

  const build = (selectedIdx) => {
    container.innerHTML = '';
    bench.forEach(item => {
      const div = document.createElement('div');
      div.className = 'selection-item';
      div.dataset.index = item.index;
      
      const isSelected = selectedIdx === item.index.toString();
      if (isSelected) div.classList.add('selected');
      if (!item.eligible) div.classList.add('disabled');
      
      let metaText = `${getBowlRating(item.player)} OVR - ${item.player.bowler_type || 'Bowler'}`;
      const cleanOvers = item.overs || 0;
      if (cleanOvers > 0) {
        metaText += ` • ${cleanOvers} ov`;
      }
      if (item.reason) {
        metaText += ` [${item.reason}]`;
      }
      
      div.innerHTML = `
        <span class="selection-item-name">${item.player.name}${item.player.impact_replacement ? ' <span class="impact-badge">✨</span>' : ''}</span>
        <span class="selection-item-meta">${metaText}</span>
      `;
      
      if (item.eligible) {
        div.onclick = () => {
          if (selectionSubmitInFlight) return;
          build(item.index.toString());
          selectionSubmitInFlight = true;
          document.getElementById('controls-sheet').classList.add('minimized');
          selectNextBowler(item.index).finally(() => { selectionSubmitInFlight = false; });
        };
      }
      container.appendChild(div);
    });
  };

  build(currentSel);
}

async function selectNextBowler(index) {
  try {
    const res = await fetch('/api/match/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        userId,
        matchId,
        type: 'over_bowler',
        action: { index }
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to select bowler");
    fetchState();
  } catch (err) {
    alert(err.message);
  }
}

// Vivid commentary fallback templates used when DB commentary is not available
const COMM_TEMPLATES = {
  six: [
    "{bat} launches it into orbit! MAXIMUM — {bowler} is stunned!",
    "That's HUGE! {bat} clears the ropes with absolute authority! SIX!",
    "BOOM! {bat} hits it out of the ground! {bowler} can only watch!",
    "Muscle power! {bat} deposits it into the stands for SIX!",
    "What a shot! {bat} smashes {bowler} over the boundary for a MAXIMUM!",
    "Incredible! {bat} sends it sailing into the crowd! SIX runs!",
    "{bowler} tries hard but {bat} is in the zone — that's a SIX!",
  ],
  four: [
    "CRACKING shot! {bat} finds the gap and races away for FOUR!",
    "Beautiful timing from {bat} — it races to the boundary!",
    "{bat} punishes the loose delivery from {bowler} — FOUR!",
    "Exquisite placement! {bat} threads through the covers for FOUR!",
    "Sweetly timed by {bat} — the fielder had no chance!",
    "The ball races away! {bat} finds the boundary with a perfect drive!",
    "Well played {bat}! That's four runs to the fence!",
  ],
  wicket: [
    "BOWLED 'EM! {bowler} has {bat} completely deceived!",
    "OUT! {bat} walks back — brilliant delivery from {bowler}!",
    "WICKET! {bowler} strikes — {bat} is on his way back to the pavilion!",
    "CAUGHT! {bat} mistimes the shot and walks off — {bowler} is pumped!",
    "Huge breakthrough! {bowler} removes {bat} — this changes everything!",
    "What a delivery! {bowler} foxes {bat} completely — OUT!",
    "Clean bowled! {bowler} finds the gap and {bat} has to walk!",
  ],
  dot: [
    "Tight line from {bowler} — {bat} can't score off that one. Dot ball.",
    "Good length delivery — {bat} plays it back cautiously.",
    "Defended solidly by {bat}. {bowler} keeps it tight.",
    "{bat} has a look but leaves it — well bowled by {bowler}.",
    "No run. {bowler} generates good movement — {bat} watches it go.",
    "Excellent discipline from {bowler}! Dot ball.",
    "{bat} gets a good look but cannot score — tight bowling.",
  ],
  one: [
    "Quick single — {bat} rotates the strike smartly.",
    "Pushed into the gap, {bat} calls for one.",
    "Good running by {bat} — worked away for a single.",
    "Clever cricket from {bat} — finds a gap for one.",
    "{bat} nudges it into the leg side and takes the single.",
    "Strike rotation! {bat} tucks it away for a quick one.",
  ],
  two: [
    "Good running between the wickets — two for {bat}!",
    "{bat} places it well and they come back for two.",
    "Two runs — quick feet from {bat}!",
    "Driven into the outfield — they scamper back for two.",
    "Excellent running! {bat} and his partner grab a quick two.",
  ],
  three: [
    "Three runs! Excellent running between the wickets from {bat}!",
    "{bat} finds the gap and they run hard for three!",
    "Superb running — {bat} picks up three with sharp footwork!",
  ],
};

function _randomComm(pool, bat, bowler) {
  const t = pool[Math.floor(Math.random() * pool.length)];
  return t.replace(/{bat}/g, bat || 'Batsman').replace(/{bowler}/g, bowler || 'Bowler');
}

function _enrichCommentaryText(comm) {
  const bat = comm.batsmanName || '';
  const bowler = comm.bowlerName || '';
  // If DB commentary exists (>15 chars), use it — but still inject names if placeholders exist
  if (comm.text && comm.text.length > 15) {
    let t = comm.text;
    // Replace any {bat}/{bowler} style placeholders if they exist in DB commentary
    if (bat) t = t.replace(/\{bat\}/g, bat).replace(/\{batsman\}/gi, bat);
    if (bowler) t = t.replace(/\{bowler\}/g, bowler);
    return t;
  }
  if (comm.isWicket) return _randomComm(COMM_TEMPLATES.wicket, bat, bowler);
  if (comm.runs === 6) return _randomComm(COMM_TEMPLATES.six, bat, bowler);
  if (comm.runs === 4) return _randomComm(COMM_TEMPLATES.four, bat, bowler);
  if (comm.runs === 3) return _randomComm(COMM_TEMPLATES.three, bat, bowler);
  if (comm.runs === 0) return _randomComm(COMM_TEMPLATES.dot, bat, bowler);
  if (comm.runs === 1) return _randomComm(COMM_TEMPLATES.one, bat, bowler);
  if (comm.runs === 2) return _randomComm(COMM_TEMPLATES.two, bat, bowler);
  return comm.text || `${comm.runs} run${comm.runs !== 1 ? 's' : ''}`;
}

// Render the recent-ball timeline as coloured doodle chips.
function renderBallTimeline() {
  const el = document.getElementById('ball-timeline');
  if (!el) return;
  const tl = matchState.timeline || [];
  if (!tl.length) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = tl
    .map(c => `<span class="ball-chip chip-${c.kind || 'run'}">${c.label}</span>`)
    .join('');
}

// Render Commentary feed in Cricbuzz style
function renderCommentaryFeed() {
  const list = document.getElementById('commentary-list');
  if (!matchState.commentary || matchState.commentary.length === 0) {
    list.innerHTML = '<div class="no-commentary">First ball is about to be delivered...</div>';
    return;
  }

  list.innerHTML = '';

  matchState.commentary.forEach(comm => {
    const item = document.createElement('div');

    if (comm.type === 'end_of_over') {
      item.className = "cricbuzz-over-end";
      item.innerHTML = `
        <div class="over-end-header">
          <span class="over-num-title">END OF OVER ${comm.overNumber}</span>
          <span class="over-runs-badge">${comm.runsScored} Runs</span>
          <span class="over-total-score">${comm.totalRuns}/${comm.totalWickets}</span>
        </div>
        <div class="over-end-stats">
          <div class="stats-row">
            <div class="stat-batsmen">
              ${comm.striker ? `<span>${comm.striker.name}: <b>${comm.striker.runs}</b>(${comm.striker.balls}b)</span>` : ''}
              ${comm.nonStriker ? `<span>${comm.nonStriker.name}: <b>${comm.nonStriker.runs}</b>(${comm.nonStriker.balls}b)</span>` : ''}
            </div>
            <div class="stat-bowler">
              ${comm.bowler ? `<span>${comm.bowler.name}: <b>${comm.bowler.wickets}-${comm.bowler.runsConceded}</b> (${formatBowlingUnit(comm.bowler.overs, comm.bowler.balls)})</span>` : ''}
            </div>
          </div>
        </div>
      `;
    } else if (comm.type === 'end_of_innings') {
      item.className = "cricbuzz-innings-end";
      item.innerHTML = `
        <div class="innings-end-header">
          🎯 INNINGS ${comm.inningsIdx + 1} COMPLETE
        </div>
        <div class="innings-end-body">
          <div class="total-score">Total: <b>${comm.runs}/${comm.wickets}</b> in ${formatInningsProgress(comm.overs, comm.balls)}</div>
          ${comm.target ? `<div class="target-needed">🏹 Target: <b>${comm.target} runs</b></div>` : ''}
          ${comm.winner ? `<div class="match-winner">🏆 <b>${comm.winner} WIN!</b></div>` : ''}
          ${comm.motm ? `<div class="match-motm">⭐ MOTM: <b>${comm.motm.name}</b> — ${comm.motm.runs}R ${comm.motm.wickets}W</div>` : ''}
        </div>
      `;
    } else if (comm.type === 'impact_player') {
      // Impact Player substitution rows carry no runs/over of their own — render
      // them as a distinct feed card instead of treating them like a ball (which
      // would crash on comm.runs.toString()).
      item.className = "cricbuzz-impact-row comm-event comm-impact";
      const impactText = comm.text
        || `Impact Player used! ${comm.inPlayer || ''} replaces ${comm.outPlayer || ''}`;
      item.innerHTML = `<span class="comm-dot">🟢</span><span class="comm-text">${impactText}</span>`;
    } else if (comm.type === 'wicket') {
      item.className = "comm-event comm-wicket";
      item.innerHTML = `<span class="comm-dot">🔴</span><span class="comm-text">${comm.text || ''}</span>`;
    } else if (comm.type === 'new_batsman') {
      item.className = "comm-event comm-new-batsman";
      item.innerHTML = `<span class="comm-dot">🟢</span><span class="comm-text">${comm.text || ((comm.name || '') + ' comes in.')}</span>`;
    } else if (comm.type === 'new_bowler' || comm.type === 'returning_bowler') {
      item.className = "comm-event comm-new-bowler";
      item.innerHTML = `<span class="comm-dot">🔵</span><span class="comm-text">${comm.text || ((comm.name || '') + ' to bowl.')}</span>`;
    } else if (comm.type === 'over_complete') {
      item.className = "comm-event comm-over-complete";
      item.innerHTML = `<span class="comm-dot">⚪</span><span class="comm-text">${comm.text || ''}</span>`;
    } else {
      item.className = "cricbuzz-ball-row";

      const richText = _enrichCommentaryText(comm);
      let highlightedText = richText
        .replace(/\b([A-Z][a-z]+ [A-Z][a-z]+)\b/g, "<span class='hl-player'>$1</span>")
        .replace(/\b(WICKET!?|OUT!?|FOUR!?|SIX!?|MAXIMUM|BOUNDARY|BOWLED|CAUGHT|LBW|STUMPED)/gi,
                 "<b class='hl-event'>$1</b>");

      // Defensive: any non-ball entry that slips through here must not crash on
      // an undefined `runs` (see the impact_player case above).
      const runsVal = (comm.runs == null) ? 0 : comm.runs;
      const evKey = comm.eventKey || '';
      let outcomeText, outcomeClass;
      if (comm.isWicket) {
        outcomeText = 'W'; outcomeClass = 'outcome-wicket';
      } else if (evKey === 'wide') {
        outcomeText = 'Wd'; outcomeClass = 'outcome-extra';
      } else if (evKey === 'no_ball') {
        outcomeText = 'Nb'; outcomeClass = 'outcome-extra';
      } else if (evKey === 'legbye' || evKey === 'bye') {
        outcomeText = runsVal > 0 ? `${runsVal}lb` : 'Lb';
        outcomeClass = 'outcome-extra';
      } else {
        outcomeText = runsVal.toString();
        outcomeClass = (runsVal === 4 ? 'outcome-four' :
                       (runsVal === 6 ? 'outcome-six' :
                       (runsVal === 0 ? 'outcome-dot' :
                       (runsVal >= 3 ? 'outcome-boundary' : 'outcome-normal'))));
      }

      item.innerHTML = `
        <div class="ball-over-num">${comm.over}</div>
        <div class="ball-outcome-badge ${outcomeClass}">${outcomeText}</div>
        <div class="ball-comm-text">${highlightedText}</div>
      `;
    }

    list.appendChild(item);
  });
}

function getFirstInningsBattingId() {
  if (matchState.innings[0] && matchState.innings[0].battingId) {
    return matchState.innings[0].battingId;
  }
  if (matchState.tossWinnerId && matchState.host.telegramId) {
    const isHostTossWinner = matchState.tossWinnerId.toString() === matchState.host.telegramId.toString();
    const hostBatsFirst = (isHostTossWinner && matchState.tossDecision === 'bat') || (!isHostTossWinner && matchState.tossDecision === 'bowl');
    return hostBatsFirst ? matchState.host.telegramId : (matchState.guest ? matchState.guest.telegramId : 'ai');
  }
  return matchState.host.telegramId;
}

function getSecondInningsBattingId() {
  const firstId = getFirstInningsBattingId();
  return firstId.toString() === matchState.host.telegramId.toString()
    ? (matchState.guest ? matchState.guest.telegramId : 'ai')
    : matchState.host.telegramId;
}

// Bowling table header: B/M/W/R/E for The Hundred, O/M/R/W/ER for over formats.
function renderBowlingHeader() {
  const labels = isHundredFormat() ? ['B', 'M', 'W', 'R', 'E'] : ['O', 'M', 'R', 'W', 'ER'];
  labels.forEach((label, i) => {
    const el = document.getElementById(`th-bowl-${i + 1}`);
    if (el) el.innerText = label;
  });
}

// Render Tab panels
function renderScorecardPanel() {
  renderBowlingHeader();
  const hostLabel = document.getElementById('scorecard-tab-host');
  const guestLabel = document.getElementById('scorecard-tab-guest');

  // Auto-switch tab to current active innings on first render of that innings
  if (matchState.currentInningsIdx === 1 && !window.hasAutoSwitchedTab) {
    activeScorecardTab = 'innings2';
    window.hasAutoSwitchedTab = true;
    if (hostLabel) hostLabel.classList.remove('active');
    if (guestLabel) guestLabel.classList.add('active');
  } else if (matchState.currentInningsIdx === 0) {
    window.hasAutoSwitchedTab = false;
  }

  const firstBatId = getFirstInningsBattingId();
  const secondBatId = getSecondInningsBattingId();

  const aiTeam = {
    telegramId: 'ai',
    username: 'AI',
    teamName: 'AI XI',
    xi: []
  };
  const firstTeam = firstBatId.toString() === matchState.host.telegramId.toString() ? matchState.host : (matchState.guest || aiTeam);
  const secondTeam = secondBatId.toString() === matchState.host.telegramId.toString() ? matchState.host : (matchState.guest || aiTeam);

  const firstInn = matchState.innings[0] || { runs: 0, wickets: 0, overs: 0, balls: 0, extras: 0 };
  const secondInn = matchState.innings[1] || { runs: 0, wickets: 0, overs: 0, balls: 0, extras: 0 };

  const firstDisplayName = firstTeam.teamName || firstTeam.username || 'HOST';
  const secondDisplayName = secondTeam ? (secondTeam.teamName || secondTeam.username) : 'AI XI';

  const firstNameShort = firstDisplayName.substring(0, 8).toUpperCase();
  const secondNameShort = secondDisplayName.substring(0, 8).toUpperCase();

  hostLabel.innerText = `${firstNameShort} ${firstInn.runs}/${firstInn.wickets}`;
  guestLabel.innerText = `${secondNameShort} ${secondInn.runs}/${secondInn.wickets}`;

  const container = document.getElementById('scorecard-batting-rows');
  container.innerHTML = '';

  const activeTeam = activeScorecardTab === 'innings1' ? firstTeam : secondTeam;
  const opposingTeam = activeScorecardTab === 'innings1' ? secondTeam : firstTeam;
  const activeInnings = activeScorecardTab === 'innings1' ? firstInn : secondInn;

  // Use innings-specific stats pool (backend sends innings1Stats / innings2Stats)
  const statsPool = activeScorecardTab === 'innings1'
    ? (matchState.innings1Stats || matchState.stats)
    : (matchState.innings2Stats || matchState.stats);

  // Set titles
  const battingTitle = (activeTeam.teamName || 'BATTING').toUpperCase();
  const bowlingTitle = (opposingTeam.teamName || 'BOWLING').toUpperCase();
  document.getElementById('scorecard-batting-title').innerText = `${battingTitle} BATTING`;
  document.getElementById('scorecard-bowling-title').innerText = `${bowlingTitle} BOWLING`;

  // Render batting rows — always show striker/non-striker even with 0 balls
  let anyBatRow = false;
  activeTeam.xi.forEach((player) => {
    const pid = player.id ? player.id.toString() : '';
    const pStat = (pid && statsPool[pid]) ? statsPool[pid] : { runs: 0, balls: 0, fours: 0, sixes: 0 };

    const isStriker = matchState.striker && matchState.striker.id && matchState.striker.id.toString() === pid;
    const isNonStriker = matchState.nonStriker && matchState.nonStriker.id && matchState.nonStriker.id.toString() === pid;
    const isActive = isStriker || isNonStriker;

    if (!isActive && (!pStat || pStat.balls === 0)) return;

    anyBatRow = true;
    const runs = pStat.runs || 0;
    const balls = pStat.balls || 0;
    const fours = pStat.fours || 0;
    const sixes = pStat.sixes || 0;
    const sr = balls > 0 ? ((runs / balls) * 100).toFixed(1) : '0.0';

    let statusText = '';
    let statusClass = 'out';
    if (isActive) {
      statusText = isStriker ? 'batting*' : 'not out';
      statusClass = 'active';
    } else if (pStat.isOut) {
      statusText = pStat.how_out || pStat.outDetail || 'out';
      statusClass = 'out';
    } else if (balls > 0) {
      statusText = 'not out';
      statusClass = 'active';
    }

    const row = document.createElement('div');
    row.className = `tb-row ${isActive ? 'batting-active' : ''}`;
    row.innerHTML = `
      <span class="tb-row-name">
        <span class="tb-row-name-text">${player.name}</span>
        <span class="tb-row-status ${statusClass}">${statusText}</span>
      </span>
      <span class="tb-num-val">${runs}</span>
      <span class="tb-num-val">${balls}</span>
      <span class="tb-num-val">${fours}</span>
      <span class="tb-num-val">${sixes}</span>
      <span class="tb-num-val">${sr}</span>
    `;
    container.appendChild(row);
  });

  if (!anyBatRow) {
    container.innerHTML = '<div class="sc-empty-msg">Innings not started yet</div>';
  }

  // Extras & total
  document.getElementById('scorecard-extras').innerText = `Extras ${activeInnings.extras || 0} (w 0, nb 0, lb 0, b 0)`;
  document.getElementById('scorecard-total').innerText =
    `TOTAL ${activeInnings.runs}/${activeInnings.wickets} (${formatInningsProgress(activeInnings.overs, activeInnings.balls)})`;

  // Yet to bat
  const activeIds = [
    matchState.striker?.id?.toString(),
    matchState.nonStriker?.id?.toString()
  ].filter(Boolean);

  const ytbPlayers = (activeScorecardTab === activeScorecardTab) && activeTeam.xi.filter(p => {
    if (!p.id) return false;
    const pid = p.id.toString();
    const st = statsPool[pid] || {};
    const isOut = st.isOut;
    const hasBatted = (st.balls || 0) > 0;
    const isAtCrease = activeIds.includes(pid);
    return !isAtCrease && !isOut && !hasBatted;
  });
  document.getElementById('scorecard-yet-to-bat').innerText =
    (ytbPlayers && ytbPlayers.length > 0) ? ytbPlayers.map(p => p.name).join(', ') : 'None';

  renderImpactPlayerSummary('scorecard-impact-list', matchState.impactPlayer?.summary || matchState.result?.impactPlayers || []);

  // Render bowling rows
  const bowlContainer = document.getElementById('scorecard-bowling-rows');
  bowlContainer.innerHTML = '';

  opposingTeam.xi.forEach((player) => {
    const pid = player.id ? player.id.toString() : '';
    const isCurrent = matchState.bowler && matchState.bowler.id && matchState.bowler.id.toString() === pid;
    const pStat = (pid && statsPool[pid]) ? statsPool[pid] : null;

    const bStats = (isCurrent && matchState.bowler.stats) ? matchState.bowler.stats : pStat;
    const oversDisplay = bStats ? bStats.oversDisplay : 0;
    const runsC = bStats ? (bStats.runsConceded ?? bStats.runs ?? 0) : 0;
    const wkts = bStats ? bStats.wickets : 0;
    const maidens = bStats ? (bStats.maidens || 0) : 0;
    const er = bStats && bStats.economy != null ? bStats.economy.toFixed(2) : '0.00';

    if (!isCurrent && (parseFloat(oversDisplay) === 0 && runsC === 0 && wkts === 0)) return;

    const row = document.createElement('div');
    row.className = 'tb-row';
    row.innerHTML = isHundredFormat() ? `
      <span class="tb-row-name">
        <span class="tb-row-name-text">${player.name}</span>
        ${isCurrent ? '<span class="tb-row-status active">bowling*</span>' : ''}
      </span>
      <span class="tb-num-val">${oversDisplay || '0b'}</span>
      <span class="tb-num-val">${maidens}</span>
      <span class="tb-num-val">${wkts}</span>
      <span class="tb-num-val">${runsC}</span>
      <span class="tb-num-val">${er}</span>
    ` : `
      <span class="tb-row-name">
        <span class="tb-row-name-text">${player.name}</span>
        ${isCurrent ? '<span class="tb-row-status active">bowling*</span>' : ''}
      </span>
      <span class="tb-num-val">${oversDisplay || '0'}</span>
      <span class="tb-num-val">${maidens}</span>
      <span class="tb-num-val">${runsC}</span>
      <span class="tb-num-val">${wkts}</span>
      <span class="tb-num-val">${er}</span>
    `;
    bowlContainer.appendChild(row);
  });
}

function renderSquadsPanel() {
  const hostList = document.getElementById('squad-host-list');
  const guestList = document.getElementById('squad-guest-list');
  hostList.innerHTML = '';
  guestList.innerHTML = '';

  document.getElementById('squad-host-title').innerText = (matchState.host.teamName || matchState.host.username || 'Host').toUpperCase();
  document.getElementById('squad-guest-title').innerText = (matchState.guest ? (matchState.guest.teamName || matchState.guest.username) : 'AI').toUpperCase();

  matchState.host.xi.forEach(p => {
    const row = document.createElement('div');
    row.className = 'squad-player-row';
    row.innerHTML = `<span class="squad-player-name">${p.name}</span><span class="squad-player-ovr">${p.ovr}</span>`;
    hostList.appendChild(row);
  });

  if (matchState.guest && matchState.guest.xi) {
    matchState.guest.xi.forEach(p => {
      const row = document.createElement('div');
      row.className = 'squad-player-row';
      row.innerHTML = `<span class="squad-player-name">${p.name}</span><span class="squad-player-ovr">${p.ovr}</span>`;
      guestList.appendChild(row);
    });
  }
}

function lockCompletedMatchControls() {
  // A finished /wpm or /cm match is permanently read-only. The result tab is
  // the way back from the scorecard; no shot, bowling, selection, or autoplay
  // control remains actionable after innings two completes.
  document.getElementById('controls-sheet')?.classList.add('hidden');
  document.getElementById('autoplay-strip')?.classList.add('hidden');
  const autoplayToggle = document.getElementById('autoplay-toggle-btn');
  if (autoplayToggle) autoplayToggle.disabled = true;
  document.querySelectorAll('#controls-sheet button, #controls-sheet .selection-item').forEach(control => {
    control.disabled = true;
    control.classList.add('disabled');
  });
  document.getElementById('tab-result')?.classList.remove('hidden');
}

// Innings Break Overlay: target + 1st-innings scorecard summary, shown
// between the end of the 1st innings and 2nd-innings team selection.
function renderInningsBreakScreen() {
  showScreen('gameplay-screen');
  const overlay = document.getElementById('innings-break-overlay');
  overlay.classList.remove('hidden');

  const aiTeam = { telegramId: 'ai', username: 'AI', teamName: 'AI XI', xi: [] };
  const inn1 = matchState.innings?.[0] || { runs: 0, wickets: 0, overs: 0, balls: 0, battingId: null };
  const target = matchState.score?.target ?? matchState.innings?.[1]?.target;

  const teamForTgId = (tgId) => {
    if (tgId == null) return aiTeam;
    if (matchState.host?.telegramId?.toString() === tgId.toString()) return matchState.host;
    if (matchState.guest?.telegramId?.toString() === tgId.toString()) return matchState.guest;
    return aiTeam;
  };
  const settingTeam = teamForTgId(inn1.battingId);
  const chasingTeam = (settingTeam === matchState.host) ? (matchState.guest || aiTeam) : matchState.host;

  document.getElementById('ib-score-title').innerText =
    `${inn1.runs}/${inn1.wickets} (${formatInningsProgress(inn1.overs, inn1.balls)})`;
  document.getElementById('ib-team-line').innerText =
    `${(settingTeam.teamName || settingTeam.username || 'Team 1').toUpperCase()} set the target`;

  const ballsLeft = matchState.totalBalls || (matchState.totalOvers ? matchState.totalOvers * ballsPerUnit() : null);
  const targetText = document.getElementById('ib-target-text');
  targetText.innerText = target
    ? `🎯 ${(chasingTeam.teamName || chasingTeam.username || 'Chasing side').toUpperCase()} need ${target} run${target === 1 ? '' : 's'} to win${ballsLeft ? ` from ${ballsLeft} balls` : ''}`
    : '';

  // Top scorers / wicket-takers from the just-finished 1st innings
  const stats = matchState.innings1Stats || {};
  const statFor = (p) => (p && p.id != null && stats[p.id.toString()]) || {};
  const inn1BatXI = (settingTeam.xi && settingTeam.xi.length) ? settingTeam.xi : (matchState.battingXI || []);
  const inn1BowlXI = (chasingTeam.xi && chasingTeam.xi.length) ? chasingTeam.xi : (matchState.bowlingXI || []);

  const topBatters = inn1BatXI
    .map(p => ({ name: p.name, ...statFor(p) }))
    .filter(p => (p.balls || 0) > 0)
    .sort((a, b) => (b.runs || 0) - (a.runs || 0))
    .slice(0, 2)
    .map(p => `${p.name} ${p.runs || 0}(${p.balls || 0})`);
  document.getElementById('ib-top-batters').innerText = topBatters.length ? topBatters.join(' · ') : '-';

  const topBowlers = inn1BowlXI
    .map(p => ({ name: p.name, ...statFor(p) }))
    .filter(p => (p.ballsBowled || 0) > 0)
    .sort((a, b) => (b.wickets || 0) - (a.wickets || 0))
    .slice(0, 2)
    .map(p => `${p.name} ${p.wickets || 0}/${p.runsConceded || 0}`);
  document.getElementById('ib-top-bowlers').innerText = topBowlers.length ? topBowlers.join(' · ') : '-';

  const remaining = matchState.inningsBreak?.secondsRemaining;
  document.getElementById('ib-countdown-text').innerText =
    (remaining !== undefined && remaining !== null)
      ? `Heading to team selection in ${remaining}s…`
      : 'Heading to team selection shortly…';
}

let _ibContinueInFlight = false;
async function continuePastInningsBreak() {
  if (_ibContinueInFlight) return;
  _ibContinueInFlight = true;
  const btn = document.getElementById('ib-continue-btn');
  if (btn) btn.disabled = true;
  try {
    await fetch('/api/match/innings-break/continue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId, matchId })
    });
    fetchState();
  } catch (err) {
    console.error('continuePastInningsBreak failed:', err);
  } finally {
    _ibContinueInFlight = false;
    if (btn) btn.disabled = false;
  }
}

// 5. Render Final Result Overlay modal
function renderResultScreen() {
  const overlay = document.getElementById('result-overlay');
  overlay.classList.remove('hidden');
  ['match', 'scorecard', 'squads'].forEach(tab => {
    document.getElementById(`tab-${tab}`)?.classList.remove('active');
  });
  document.getElementById('tab-result')?.classList.add('active');

  const result = matchState.result;

  if (result && result.winner) {
    const winnerDisplayName = result.winner.teamName || result.winner.username || 'WINNER';
    document.getElementById('result-winner-title').innerText = 
      `${winnerDisplayName.toUpperCase()} WINS!`;
  } else {
    document.getElementById('result-winner-title').innerText = "TIE MATCH!";
  }

  // Calculate my exact awarded coins based on Telegram identity and outcome.
  const iAmWinner = Boolean(result && result.winner && result.winner.telegramId &&
    result.winner.telegramId.toString() === userId.toString());
  const myCoins = result ? (iAmWinner ? result.winnerReward : result.loserReward) : 0;
  document.getElementById('result-reward-amount').innerText = `+${myCoins.toLocaleString()}`;
  document.getElementById('result-reward-label').innerText = iAmWinner ? 'coins won' : 'coins earned after loss';
  document.getElementById('result-outcome-detail').innerText = result?.resultText || (iAmWinner ? 'You won the match.' : 'Match completed.');

  const inn1 = matchState.innings[0] || { runs: 0, wickets: 0, overs: 0, balls: 0 };
  const inn2 = matchState.innings[1] || { runs: 0, wickets: 0, overs: 0, balls: 0 };

  document.getElementById('result-inn1-score').innerText =
    `${inn1.runs}/${inn1.wickets} (${formatInningsProgress(inn1.overs, inn1.balls)})`;
  document.getElementById('result-inn2-score').innerText =
    `${inn2.runs}/${inn2.wickets} (${formatInningsProgress(inn2.overs, inn2.balls)})`;

  // Super Over (if the match was decided in one): show each Super Over innings'
  // total and the winner, after the two main innings. Guarded so normal matches
  // are unaffected.
  const soEl = document.getElementById('result-super-over');
  if (soEl) {
    const so = matchState.superOver || result?.superOver;
    const soInns = so && Array.isArray(so.innings) ? so.innings : [];
    if (soInns.length) {
      let html = '<div class="result-super-over-title">🔥 SUPER OVER</div>';
      soInns.forEach((s, i) => {
        const label = s.label || `Super Over Innings ${i + 1}`;
        const team = s.team ? `${s.team} ` : '';
        html += `<div class="result-super-over-row"><span>${label}</span>` +
                `<span>${team}${s.runs}/${s.wickets}</span></div>`;
      });
      if (so.winner) {
        html += `<div class="result-super-over-win">🏆 ${so.winner} won the match` +
                `${so.marginText ? ' ' + so.marginText : ''}</div>`;
      }
      soEl.innerHTML = html;
      soEl.classList.remove('hidden');
    } else {
      soEl.classList.add('hidden');
    }
  }

  // Render MOTM
  const motmSection = document.getElementById('result-motm-section');
  if (result && result.motm) {
    motmSection.classList.remove('hidden');
    document.getElementById('result-motm-name').innerText = result.motm.name;
    document.getElementById('result-motm-stats').innerText = 
      `${result.motm.impactPoints || 0} Impact Points · ${result.motm.runs} runs (${result.motm.balls}b) & ${result.motm.wickets} wickets (${result.motm.overs}${isHundredFormat() ? '' : ' ov'})`;
  } else {
    motmSection.classList.add('hidden');
  }

  const impactSection = document.getElementById('result-impact-section');
  const impactRows = result?.impactPlayers || matchState.impactPlayer?.summary || [];
  if (impactRows.length) {
    impactSection.classList.remove('hidden');
    renderImpactPlayerSummary('result-impact-list', impactRows);
  } else {
    impactSection.classList.add('hidden');
  }
}

function renderImpactPlayerSummary(elementId, rows) {
  const el = document.getElementById(elementId);
  if (!el) return;
  const usedRows = rows || [];
  const teamNames = [];
  if (matchState?.host) teamNames.push(matchState.host.teamName || matchState.host.username || 'Host');
  if (matchState?.guest) teamNames.push(matchState.guest.teamName || matchState.guest.username || 'Guest');

  const renderRow = (team, row) => {
    if (!row) return `<div class="impact-summary-row muted"><b>${team}</b>: Not used</div>`;
    const inp = row.inPlayer || row.in_player || 'Impact player';
    const outp = row.outPlayer || row.out_player || 'XI player';
    const when = row.usedAt || row.used_at || '';
    return `<div class="impact-summary-row"><b>${team}</b>: ${inp} replaced ${outp}${when ? ` <span>${when}</span>` : ''}</div>`;
  };

  if (teamNames.length) {
    el.innerHTML = teamNames.map(team => {
      const row = usedRows.find(r => (r.teamName || r.team_name || '').toLowerCase() === team.toLowerCase());
      return renderRow(team, row);
    }).join('');
    return;
  }

  if (!usedRows.length) {
    el.innerHTML = '<div class="impact-summary-row muted">Both teams: Not used</div>';
    return;
  }
  el.innerHTML = usedRows.map(r => renderRow(r.teamName || r.team_name || 'Team', r)).join('');
}

// When autoplay is ON, the user's own side is handled by the SERVER-SIDE bot AI
// (the same difficulty/phase-aware engine /vsbot uses) rather than fragile DOM
// clicking. Each cycle we POST /api/match/autoplay, which plays all of our
// pending turns (and, in a vs-bot match, the bot's reply) and returns the new
// state. The poll loop keeps calling until the match completes.
// Single source of truth for the Autoplay flag + its pill UI, so the toggle and
// the per-match reset can never drift apart.
function renderAutoplayStatusMessage() {
  const box = document.getElementById('autoplay-status-message');
  if (!box || !matchState) return;
  const messages = [];
  if (autoplayActive || matchState.autoplay?.isOnForMe) {
    messages.push('Autoplay is ON for your team');
  }
  if (matchState.autoplay?.opponent?.teamName) {
    messages.push(`${matchState.autoplay.opponent.teamName} is on Autoplay mode`);
  }
  if (!messages.length) {
    box.classList.add('hidden');
    box.innerText = '';
    return;
  }
  box.innerText = messages.join(' • ');
  box.classList.remove('hidden');
}

function resetAutoplayQuickCards() {
  ['bowling-controls', 'batting-controls'].forEach(id => {
    const controls = document.getElementById(id);
    if (!controls) return;
    controls.querySelectorAll(':scope > .autoplay-quick-card').forEach(card => card.remove());
    Array.from(controls.children).forEach(child => {
      child.style.display = '';
    });
  });
}

function renderAutoplayQuickCard(kind) {
  const displayKind = kind === 'batting_wait' ? 'batting' : kind;
  const controls = displayKind === 'bowling'
    ? document.getElementById('bowling-controls')
    : document.getElementById('batting-controls');
  if (!controls) return;
  Array.from(controls.children).forEach(child => {
    if (!child.classList.contains('autoplay-quick-card')) child.style.display = 'none';
  });
  let card = controls.querySelector(':scope > .autoplay-quick-card');
  if (!card) {
    card = document.createElement('div');
    card.className = 'autoplay-quick-card';
    controls.appendChild(card);
  }
  if (kind === 'batting_wait') {
    const delivery = matchState.lastAutoplayDelivery;
    const line = delivery?.delivery
      ? `${delivery.bowler || 'Bowler'} bowls ${delivery.delivery}`
      : 'Autoplay is waiting for the delivery';
    card.innerHTML = `${line}<span class="muted">Your shot will be selected automatically.</span>`;
    card.style.display = '';
    controls.classList.remove('hidden');
    return;
  }
  const playerName = displayKind === 'bowling'
    ? (matchState.lastAutoplayDelivery?.bowler || matchState.bowler?.name || 'Bowler')
    : (matchState.lastAutoplayShot?.batter || matchState.striker?.name || 'Batter');
  const actionName = displayKind === 'bowling'
    ? (matchState.lastAutoplayDelivery?.delivery || matchState.currentDelivery || 'delivery')
    : (matchState.lastAutoplayShot?.shot || matchState.lastBall?.shot || 'shot');
  const line = displayKind === 'bowling'
    ? `${playerName} bowls ${actionName}`
    : `${playerName} played ${actionName}`;
  // Note: matchState.lastBall is still the PREVIOUS completed ball here — the
  // outcome of the action named in `line` isn't known yet (runAutoplayAction
  // is about to resolve it via the burst endpoint). Pairing `line` with
  // lastBall would misreport this turn's result, so keep the subtitle neutral
  // and just communicate that resolution is instant, not "processing".
  const nextStepLine = displayKind === 'bowling'
    ? 'Delivery is selected and bowled instantly'
    : 'Shot is selected and played instantly';
  card.innerHTML = `${line}<span class="muted">${nextStepLine}</span>`;
  card.style.display = '';
  controls.classList.remove('hidden');
}


async function postAutoplayStatus(active) {
  if (!matchId || !userId || userId === 'spectator') return;
  try {
    const resp = await fetch('/api/match/autoplay-status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId, matchId, active: !!active }),
    });
    const data = await resp.json();
    if (data && data.matchState) applyMatchState(data.matchState);
  } catch (e) {
    console.error('[Autoplay] status update failed', e);
  }
}

function handleAutoplayToggle(active) {
  if (!active && autoplayInFlight) {
    autoplayOffPending = true;
    setAutoplayActive(false);
    return;
  }
  autoplayOffPending = false;
  setAutoplayActive(active);
  postAutoplayStatus(active);
  if (active) setTimeout(runAutoplayAction, AUTOPLAY_ACTION_DELAY_MS);
}

function setAutoplayActive(active) {
  autoplayActive = !!active;
  const btn = document.getElementById('autoplay-toggle-btn');
  if (!btn) return;
  btn.classList.toggle('active', autoplayActive);
  const label = btn.querySelector('.pill-label');
  if (label) label.innerText = autoplayActive ? 'ON' : 'OFF';
}

let autoplayInFlight = false;
let autoplayOffPending = false;
async function runAutoplayAction() {
  if (!autoplayActive || !matchState) return;
  if (autoplayInFlight || matchState.isProcessing) return;
  if (matchState.status === 'completed') return;

  // Autoplay only ever plays deliveries and shots — bowler/batsman SELECTION
  // (opener setup, new bowler, incoming batsman) always stays manual, so skip
  // setup and the selection turn states and leave those controls to the user.
  const inPlay = matchState.status === 'innings1' || matchState.status === 'innings2';
  const isDeliveryOrShot = matchState.turnState === 'bowling_delivery'
    || matchState.turnState === 'batting_shot';
  if (!(inPlay && matchState.isMyTurn && isDeliveryOrShot)) return;

  autoplayInFlight = true;
  try {
    const resp = await fetch('/api/match/autoplay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId, matchId }),
    });
    const data = await resp.json();
    if (data && data.matchState) applyMatchState(data.matchState, { force: true });
  } catch (e) {
    console.error('[Autoplay] server autoplay failed', e);
  } finally {
    autoplayInFlight = false;
    if (autoplayOffPending) {
      autoplayOffPending = false;
      postAutoplayStatus(false);
    } else if (autoplayActive) {
      setTimeout(runAutoplayAction, AUTOPLAY_ACTION_DELAY_MS);
    }
  }
}

// Manhattan Bar Chart (over-by-over runs)
function renderManhattanChart() {
  const container = document.getElementById('manhattan-chart-container');
  if (!container || !matchState) return;

  const inn1 = matchState.inn1OverRuns || [];
  const inn2 = matchState.inn2OverRuns || [];
  const totalOvers = matchState.totalOvers || 20;

  if (inn1.length === 0 && inn2.length === 0) {
    container.innerHTML = '<div class="sc-empty-msg">No completed overs yet</div>';
    return;
  }

  const maxRuns = Math.max(...inn1, ...inn2, 1);
  const chartHeight = 80;

  let bars = '';
  const numOvers = Math.max(inn1.length, inn2.length, 1);
  for (let i = 0; i < numOvers; i++) {
    const r1 = inn1[i] !== undefined ? inn1[i] : null;
    const r2 = inn2[i] !== undefined ? inn2[i] : null;
    const h1 = r1 !== null ? Math.max(4, Math.round((r1 / maxRuns) * chartHeight)) : 0;
    const h2 = r2 !== null ? Math.max(4, Math.round((r2 / maxRuns) * chartHeight)) : 0;
    const overLabel = i + 1;

    bars += `<div class="mh-bar-group">
      <div class="mh-bars">
        ${r1 !== null ? `<div class="mh-bar inn1" style="height:${h1}px" title="Inn1 Over ${overLabel}: ${r1}"></div>` : '<div class="mh-bar empty"></div>'}
        ${r2 !== null ? `<div class="mh-bar inn2" style="height:${h2}px" title="Inn2 Over ${overLabel}: ${r2}"></div>` : '<div class="mh-bar empty"></div>'}
      </div>
      <div class="mh-over-num">${overLabel}</div>
    </div>`;
  }

  // Legend
  const hostName = matchState.host ? (matchState.host.teamName || 'Host') : 'Inn 1';
  const guestName = matchState.guest ? (matchState.guest.teamName || 'Guest') : 'Inn 2';

  container.innerHTML = `
    <div class="mh-legend">
      <span class="mh-leg-dot inn1-dot"></span><span>${hostName.substring(0,8)}</span>
      <span class="mh-leg-dot inn2-dot" style="margin-left:12px"></span><span>${guestName.substring(0,8)}</span>
    </div>
    <div class="mh-chart">${bars}</div>
  `;
}

// Over-by-over breakdown table
function renderOverByOver() {
  const container = document.getElementById('over-by-over-container');
  if (!container || !matchState) return;

  const inn1 = matchState.inn1OverRuns || [];
  const inn2 = matchState.inn2OverRuns || [];

  if (inn1.length === 0 && inn2.length === 0) {
    container.innerHTML = '<div class="sc-empty-msg">No completed overs yet</div>';
    return;
  }

  const hostName = matchState.host ? (matchState.host.teamName || 'Host').substring(0, 6) : 'Inn 1';
  const guestName = matchState.guest ? (matchState.guest.teamName || 'Guest').substring(0, 6) : 'Inn 2';

  const numOvers = Math.max(inn1.length, inn2.length);
  let rows = `<div class="ovo-header-row">
    <span class="ovo-ov">OV</span>
    <span class="ovo-val">${hostName}</span>
    <span class="ovo-val">${guestName}</span>
  </div>`;

  for (let i = 0; i < numOvers; i++) {
    const r1 = inn1[i] !== undefined ? inn1[i] : null;
    const r2 = inn2[i] !== undefined ? inn2[i] : null;
    const cls1 = r1 !== null ? (r1 >= 15 ? 'ovo-high' : (r1 <= 4 ? 'ovo-low' : '')) : 'ovo-na';
    const cls2 = r2 !== null ? (r2 >= 15 ? 'ovo-high' : (r2 <= 4 ? 'ovo-low' : '')) : 'ovo-na';
    rows += `<div class="ovo-row">
      <span class="ovo-ov">${i + 1}</span>
      <span class="ovo-val ${cls1}">${r1 !== null ? r1 : '-'}</span>
      <span class="ovo-val ${cls2}">${r2 !== null ? r2 : '-'}</span>
    </div>`;
  }

  container.innerHTML = rows;
}

// Partnership history
function renderPartnershipHistory() {
  const container = document.getElementById('partnership-history-container');
  if (!container || !matchState) return;

  const isInn1 = activeScorecardTab === 'innings1';
  const history = isInn1
    ? (matchState.inn1PartnershipHistory || [])
    : (matchState.inn2PartnershipHistory || []);

  const curPart = matchState.partnership || { runs: 0, balls: 0 };
  const isCurrentInnings = (isInn1 && matchState.currentInningsIdx === 0) ||
                           (!isInn1 && matchState.currentInningsIdx === 1);

  if (history.length === 0 && (!isCurrentInnings || curPart.runs === 0)) {
    container.innerHTML = '<div class="sc-empty-msg">No partnership data yet</div>';
    return;
  }

  let rows = '';
  history.forEach((p, i) => {
    const wkt = p.wicket || (i + 1);
    rows += `<div class="part-row">
      <span class="part-wkt">${wkt}W</span>
      <span class="part-names">${p.batsman1 || ''} & ${p.batsman2 || ''}</span>
      <span class="part-score">${p.runs}(${p.balls})</span>
    </div>`;
  });

  // Current ongoing partnership (only for the active innings)
  if (isCurrentInnings && matchState.striker && matchState.nonStriker) {
    rows += `<div class="part-row active-part">
      <span class="part-wkt">NOW</span>
      <span class="part-names">${matchState.striker.name} & ${matchState.nonStriker.name}</span>
      <span class="part-score">${curPart.runs}(${curPart.balls})</span>
    </div>`;
  }

  container.innerHTML = rows || '<div class="sc-empty-msg">No partnership data yet</div>';
}

function showError(msg) {
  showScreen('loading-screen');
  document.querySelector('.loading-text').innerHTML = `<span style="color:#ef4444;font-weight:600">${msg}</span>`;
  document.querySelector('.cricket-ball-spinner').style.animationPlayState = 'paused';
  document.querySelector('.cricket-ball-spinner').style.borderColor = '#ef4444';
}

// Inline event GIFs. Falls back to the existing full-screen animation when
// the administrator has not published an enabled GIF for the event.
function eventKeyForCommentary(comm) {
  if (comm.eventKey) return comm.eventKey;
  const text = String(comm.text || '').toLowerCase();
  if (text.includes('implant')) return 'implant';
  if (text.includes('century') || text.includes('hundred')) return 'century';
  if (text.includes('fifty') || text.includes('half-century')) return 'fifty';
  if (comm.isWicket) return 'wicket';
  if (comm.runs === 6) return 'six';
  if (comm.runs === 4) return 'four';
  if (comm.runs === 0) return 'dot_ball';
  if (text.includes('no ball')) return 'no_ball';
  if (text.includes('wide')) return 'wide';
  return null;
}

function preloadEventGifs(config) {
  Object.values(config).flat().forEach(item => {
    if (!item?.url || preloadedEventGifIds.has(item.id)) return;
    if (item.kind === 'video') {
      const video = document.createElement('video');
      video.preload = 'auto';
      video.src = item.url;
    } else {
      const image = new Image();
      image.src = item.url;
    }
    preloadedEventGifIds.add(item.id);
  });
}

function applyEventGifSizing(box, media, item) {
  const maxMobileWidth = Math.max(240, Math.min(640, Number(item.maxMobileWidth) || 440));
  const viewportSafeWidth = Math.max(1, Math.min(maxMobileWidth, window.innerWidth - 32));
  const naturalWidth = Number(item.width) || media.naturalWidth || media.videoWidth || 0;
  const naturalHeight = Number(item.height) || media.naturalHeight || media.videoHeight || 0;
  const fallbackNeeded = naturalWidth <= 0 || naturalHeight <= 0 || viewportSafeWidth < 240;
  const useFixed = item.sizeMode === 'fixed_16_9' || fallbackNeeded;

  box.classList.toggle('fixed-16-9', useFixed);
  box.classList.toggle('natural-size', !useFixed);
  box.style.width = '';
  box.style.height = '';
  box.style.aspectRatio = '';
  media.style.width = '';
  media.style.height = '';

  if (useFixed) {
    box.style.width = `${viewportSafeWidth}px`;
    box.style.aspectRatio = '16 / 9';
    media.style.width = '100%';
    media.style.height = '100%';
    return;
  }

  const scale = Math.min(1, viewportSafeWidth / naturalWidth);
  const displayWidth = Math.max(1, Math.round(naturalWidth * scale));
  const displayHeight = Math.max(1, Math.round(naturalHeight * scale));
  box.style.width = `${displayWidth}px`;
  box.style.height = `${displayHeight}px`;
  box.style.aspectRatio = `${naturalWidth} / ${naturalHeight}`;
  media.style.width = `${displayWidth}px`;
  media.style.height = `${displayHeight}px`;
}

// ── Fixed event box helpers ────────────────────────────────────────────────
// The event box is always present (never hidden). It shows a GIF when one is
// configured for the event, the ball-outcome text otherwise, and a neutral
// idle state between balls.
function getEventBoxEls() {
  return {
    box: document.getElementById('event-gif-box'),
    image: document.getElementById('event-gif-image'),
    video: document.getElementById('event-gif-video'),
    idle: document.getElementById('event-gif-idle'),
    label: document.getElementById('event-gif-label'),
  };
}

function clearEventBoxParticles(box) {
  if (box) box.querySelectorAll('.flash-particle').forEach(p => p.remove());
}

function clearEventBoxMedia() {
  const { image, video } = getEventBoxEls();
  if (image) {
    image.onload = null;
    image.onerror = null;
    image.style.display = 'none';
    image.src = '';
  }
  if (video) {
    video.onloadedmetadata = null;
    video.onerror = null;
    video.style.display = 'none';
    video.pause();
    video.removeAttribute('src');
  }
}

// Return the box to its neutral, always-visible idle state.
function showEventBoxIdle() {
  const { box, idle, label } = getEventBoxEls();
  if (!box) return;
  clearEventBoxMedia();
  clearEventBoxParticles(box);
  box.className = 'event-gif-box is-idle';
  box.style.width = '';
  box.style.height = '';
  box.style.aspectRatio = '';
  if (idle) idle.style.display = 'flex';
  if (label) { label.textContent = ''; label.style.display = 'none'; }
}

// Native haptic feedback for the moment (Telegram WebApp only).
function fireEventHaptic(comm) {
  if (!(tg && tg.HapticFeedback)) return;
  try {
    if (comm.isWicket) tg.HapticFeedback.notificationOccurred('error');
    else if (comm.runs === 4 || comm.runs === 6) tg.HapticFeedback.notificationOccurred('success');
    else tg.HapticFeedback.impactOccurred('medium');
  } catch (e) {
    console.warn("Haptic trigger failed:", e);
  }
}

function showFlowStage(title, subtitle = '', mod = 'evt-runs') {
  const { box, idle, label } = getEventBoxEls();
  if (!box) return;
  clearTimeout(eventGifTimer);
  clearTimeout(flowStageTimer);
  clearEventBoxMedia();
  clearEventBoxParticles(box);
  box.className = `event-gif-box is-text is-flow-stage ${mod}`;
  box.style.width = '';
  box.style.height = '';
  box.style.aspectRatio = '';
  if (idle) idle.style.display = 'none';
  if (label) {
    label.style.display = 'flex';
    label.innerHTML = `<span class="evt-title">${title}</span>` +
      (subtitle ? `<span class="evt-sub">${subtitle}</span>` : '');
  }
  flowStageTimer = setTimeout(() => {
    if (box.classList.contains('is-flow-stage')) showEventBoxIdle();
  }, BALL_FLOW_DELAY_MS);
}

// Render the ball outcome as text inside the fixed box (used when no GIF is
// configured for the event — replaces the old floating popup overlay).
function showEventBoxText(comm) {
  const { box, idle, label } = getEventBoxEls();
  if (!box) return;
  clearTimeout(eventGifTimer);
  // Outcomes now apply the instant the response arrives, so a still-pending
  // showFlowStage() auto-hide timer can fire mid-reveal and stomp this box
  // back to idle. Cancel it whenever we're about to show a real outcome.
  clearTimeout(flowStageTimer);
  clearEventBoxMedia();
  clearEventBoxParticles(box);

  let title = "";
  let mod = "evt-runs";
  if (comm.isWicket) { title = "🔴 OUT!"; mod = "evt-wicket"; }
  else if (comm.runs === 6) { title = "🚀 SIX!"; mod = "evt-six"; }
  else if (comm.runs === 4) { title = "⚡ FOUR!"; mod = "evt-four"; }
  else if (comm.runs === 0) { title = "⚫ DOT BALL"; mod = "evt-dot"; }
  else { title = `🏏 ${comm.runs} RUN${comm.runs === 1 ? '' : 'S'}`; mod = "evt-runs"; }

  box.className = `event-gif-box is-text ${mod}`;
  box.style.width = '';
  box.style.height = '';
  box.style.aspectRatio = '';
  if (idle) idle.style.display = 'none';
  if (label) {
    label.style.display = 'flex';
    label.innerHTML = `<span class="evt-title">${title}</span>` +
      (comm.text ? `<span class="evt-sub">${comm.text}</span>` : '');
  }

  fireEventHaptic(comm);

  // Confetti for the big moments, scoped to the box.
  if (comm.runs === 6 || comm.isWicket || comm.runs === 4) {
    const color = comm.runs === 6 ? '#ffa502' : (comm.runs === 4 ? '#1e90ff' : '#ff4757');
    createParticles(box, color);
  }

  // Revert to idle after a short beat so the box stays present but neutral.
  eventGifTimer = setTimeout(showEventBoxIdle, EVENT_FLASH_MS);
}

function triggerMatchEvent(comm) {
  const key = eventKeyForCommentary(comm);
  const choices = key ? (matchState?.eventGifs?.[key] || []) : [];
  if (!choices.length) {
    showEventBoxText(comm);
    return;
  }
  const totalWeight = choices.reduce((sum, choice) => sum + Math.max(1, Number(choice.weight) || 1), 0);
  let draw = Math.random() * totalWeight;
  const item = choices.find(choice => ((draw -= Math.max(1, Number(choice.weight) || 1)) <= 0)) || choices[0];
  const { box, image, video, idle, label } = getEventBoxEls();
  if (!box || !image || !video || !item?.url) {
    showEventBoxText(comm);
    return;
  }
  clearTimeout(eventGifTimer);
  // See showEventBoxText: cancel any pending flow-stage auto-hide so it can't
  // fire mid-load and clear the media src before the GIF/video reveals.
  clearTimeout(flowStageTimer);
  clearEventBoxParticles(box);
  if (idle) idle.style.display = 'none';
  image.src = '';
  video.pause();
  video.removeAttribute('src');
  image.style.display = item.kind === 'video' ? 'none' : 'block';
  video.style.display = item.kind === 'video' ? 'block' : 'none';
  const media = item.kind === 'video' ? video : image;
  media.onload = null;
  media.onloadedmetadata = null;
  media.onerror = null;
  const reveal = () => {
    box.className = 'event-gif-box is-media';
    applyEventGifSizing(box, media, item);
  };
  media.onerror = () => {
    media.onerror = null;
    // Fall back to the in-box text rendering instead of leaving an empty box.
    showEventBoxText(comm);
  };
  if (item.kind === 'video') {
    video.onloadedmetadata = reveal;
    video.src = item.url;
    video.play().catch(() => {});
    if (item.width && item.height) reveal();
  } else {
    image.onload = reveal;
    image.alt = `${item.label || key} animation`;
    image.src = item.url;
    if (item.width && item.height) reveal();
  }
  if (label) {
    label.style.display = 'flex';
    label.textContent = item.label || key.replaceAll('_', ' ');
  }
  fireEventHaptic(comm);
  // Flash the GIF briefly, then revert to idle — the box itself stays visible.
  // The outcome (scorecard/commentary) already rendered instantly via
  // applyMatchState, so the overlay is just a quick celebratory beat.
  eventGifTimer = setTimeout(showEventBoxIdle, EVENT_FLASH_MS);
}

function createParticles(container, color) {
  for (let i = 0; i < 35; i++) {
    const p = document.createElement('div');
    p.className = 'flash-particle';
    p.style.backgroundColor = color;
    p.style.left = '50%';
    p.style.top = '50%';
    
    const angle = Math.random() * Math.PI * 2;
    const distance = 60 + Math.random() * 160;
    const tx = Math.cos(angle) * distance;
    const ty = Math.sin(angle) * distance;
    
    p.style.setProperty('--tx', `${tx}px`);
    p.style.setProperty('--ty', `${ty}px`);
    
    const size = 5 + Math.random() * 7;
    p.style.width = `${size}px`;
    p.style.height = `${size}px`;
    p.style.animationDuration = `${0.5 + Math.random() * 0.7}s`;
    
    container.appendChild(p);
  }
}

// Start execution
init();
