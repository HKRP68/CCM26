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
const POLL_REQUEST_TIMEOUT_MS = 3000;
const MAX_POLL_FAILURES = 2;
const MATCH_POLL_INTERVAL_MS = 150;
const AUTOPLAY_ACTION_DELAY_MS = 0;

// Selected actions
let selectedDelivery = null;
let selectedSpeed = 'normal';
let autoplayActive = false;
let activeScorecardTab = 'innings1'; // 'innings1' or 'innings2'
let lastBallUniqueId = null;
let wasMyTurn = false;
let lastActionableSig = null; // tracks the current actionable turn for auto-expand
let selectionSubmitInFlight = false;

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

  // Close app button
  document.getElementById('close-app-btn').addEventListener('click', () => {
    if (tg) {
      tg.close();
    } else {
      alert("Match completed! You can return to Telegram.");
      window.close();
    }
  });

  // Completed-match scorecard toggle. Keep the finished board read-only,
  // but let players jump straight into the full scorecard and back.
  document.getElementById('view-match-btn').addEventListener('click', () => {
    document.getElementById('result-overlay').classList.add('hidden');
    switchGameplayTab('scorecard');
  });

  // Autoplay Pill Switch Toggle
  const autoplayToggleBtn = document.getElementById('autoplay-toggle-btn');
  autoplayToggleBtn.addEventListener('click', () => {
    autoplayActive = !autoplayActive;
    if (autoplayActive) {
      autoplayToggleBtn.classList.add('active');
      autoplayToggleBtn.querySelector('.pill-label').innerText = 'ON';
    } else {
      autoplayToggleBtn.classList.remove('active');
      autoplayToggleBtn.querySelector('.pill-label').innerText = 'OFF';
    }
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
  if (fetchInFlight) return;
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

function applyMatchState(nextState) {
  if (!nextState) return;
  const currentRevision = Number(matchState?.ballSeq ?? -1);
  const nextRevision = Number(nextState.ballSeq ?? currentRevision);
  if (Number.isFinite(currentRevision) && Number.isFinite(nextRevision) && nextRevision < currentRevision) return;

  matchState = nextState;
  pollFailureCount = 0;

  if (identitySelectionRequired) {
    showIdentitySelection(matchState);
    return;
  }

  if (matchState.commentary && matchState.commentary.length > 0) {
    const latest = matchState.commentary[0];
    if (latest && (latest.type === 'ball' || latest.runs !== undefined)) {
      const uniqueKey = `${matchState.status}_${latest.over}`;
      if (lastBallUniqueId && lastBallUniqueId !== uniqueKey) triggerMatchFlashAnimation(latest);
      lastBallUniqueId = uniqueKey;
    }
  }

  document.getElementById('header-host-name').innerText = (matchState.host.teamName || matchState.host.username || 'HOST').toUpperCase();
  document.getElementById('header-guest-name').innerText = (matchState.guest ? (matchState.guest.teamName || matchState.guest.username) : 'AI XI').toUpperCase();
  document.getElementById('header-match-uuid').innerText = `MATCH ID: ${matchState.id.substring(0, 6).toUpperCase()}`;

  if (matchState.status === 'xi_selection') renderSetupScreen();
  else if (matchState.status === 'innings1' || matchState.status === 'innings2') renderGameplayScreen();
  else if (matchState.status === 'completed') {
    renderGameplayScreen();
    renderResultScreen();
    stopPolling();
  }

  setTimeout(runAutoplayAction, AUTOPLAY_ACTION_DELAY_MS);
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
      const sortedBatting = matchState.battingXI.map((p, idx) => ({ p, idx }))
        .sort((a, b) => getBatRating(b.p) - getBatRating(a.p));

      let sSel = strikerContainer.querySelector('.selection-item.selected')?.dataset.index;
      let nsSel = nonStrikerContainer.querySelector('.selection-item.selected')?.dataset.index;

      if (sSel === undefined && nsSel === undefined && sortedBatting.length >= 2) {
        sSel = sortedBatting[0].idx.toString();
        nsSel = sortedBatting[1].idx.toString();
      }

      const buildList = (container, currentSel, otherSel, onSelect) => {
        container.innerHTML = '';
        sortedBatting.forEach(({ p, idx }) => {
          const div = document.createElement('div');
          div.className = 'selection-item';
          div.dataset.index = idx;
          
          const isSelected = currentSel === idx.toString();
          const isDisabled = otherSel === idx.toString();
          
          if (isSelected) div.classList.add('selected');
          if (isDisabled) div.classList.add('disabled');
          
          div.innerHTML = `
            <span class="selection-item-name">${p.name}</span>
            <span class="selection-item-meta">${getBatRating(p)} OVR - ${p.role || 'Batsman'}</span>
          `;
          
          if (!isDisabled) {
            div.onclick = () => {
              onSelect(idx.toString());
            };
          }
          container.appendChild(div);
        });
      };

      const updateLists = (newStriker, newNonStriker) => {
        buildList(strikerContainer, newStriker, newNonStriker, (idx) => updateLists(idx, newNonStriker));
        buildList(nonStrikerContainer, newNonStriker, newStriker, (idx) => updateLists(newStriker, idx));
      };

      updateLists(sSel, nsSel);
    }
  } else if (matchState.myRole === 'bowling' && !myConfirmed) {
    bowlingSetup.classList.remove('hidden');
    battingSetup.classList.add('hidden');
    
    const container = document.getElementById('bowler-list');
    if (container) {
      const getBowlRating = (p) => p.bowling_ovr || p.bowling_rating || p.rating || p.ovr || 0;
      const sortedBowling = matchState.bowlingXI.map((p, idx) => ({ p, idx }))
        .sort((a, b) => getBowlRating(b.p) - getBowlRating(a.p));

      let currentSel = container.querySelector('.selection-item.selected')?.dataset.index;
      if (currentSel === undefined && sortedBowling.length > 0) {
        currentSel = sortedBowling[0].idx.toString();
      }

      const build = (selectedIdx) => {
        container.innerHTML = '';
        sortedBowling.forEach(({ p, idx }) => {
          const div = document.createElement('div');
          div.className = 'selection-item';
          div.dataset.index = idx;
          
          const isSelected = selectedIdx === idx.toString();
          if (isSelected) div.classList.add('selected');
          
          div.innerHTML = `
            <span class="selection-item-name">${p.name}</span>
            <span class="selection-item-meta">${getBowlRating(p)} OVR - ${p.bowler_type || 'Bowler'}</span>
          `;
          
          div.onclick = () => {
            build(idx.toString());
          };
          container.appendChild(div);
        });
      };

      build(currentSel);
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
    waitingInd.querySelector('p').innerText = "Lineup confirmed! Waiting for opponent...";
  } else {
    submitBtn.classList.remove('hidden');
    waitingInd.classList.add('hidden');
  }
}

// Setup Submit handler
async function submitSetup() {
  const isBatting = matchState.myRole === 'batting';
  const body = { userId };
  
  if (isBatting) {
    const strikerEl = document.querySelector('#striker-list .selection-item.selected');
    const nonStrikerEl = document.querySelector('#non-striker-list .selection-item.selected');
    if (!strikerEl || !nonStrikerEl) {
      alert("Please select both Striker and Non-Striker!");
      return;
    }
    const sIdx = parseInt(strikerEl.dataset.index);
    const nsIdx = parseInt(nonStrikerEl.dataset.index);
    if (sIdx === nsIdx) {
      alert("Striker and Non-Striker cannot be the same player!");
      return;
    }
    body.strikerIdx = sIdx;
    body.nonStrikerIdx = nsIdx;
  } else {
    const bowlerEl = document.querySelector('#bowler-list .selection-item.selected');
    if (!bowlerEl) {
      alert("Please select opening bowler!");
      return;
    }
    body.bowlerIdx = parseInt(bowlerEl.dataset.index);
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
    document.getElementById('setup-waiting').classList.remove('hidden');
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

  // Overs
  document.getElementById('live-overs').innerText = 
    `${matchState.score.overs}.${matchState.score.balls}`;
  document.getElementById('total-overs').innerText = matchState.totalOvers;

  // CRR
  const totalBallsBowled = (matchState.score.overs * 6) + matchState.score.balls;
  const crr = totalBallsBowled > 0 ? ((matchState.score.runs / totalBallsBowled) * 6).toFixed(1) : "0.0";
  document.getElementById('live-crr').innerText = crr;

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
    const totalBalls = matchState.totalOvers * 6;
    const ballsRemaining = Math.max(0, totalBalls - totalBallsBowled);
    
    document.getElementById('target-runs-needed').innerText = runsNeeded;
    document.getElementById('target-balls-left').innerText = ballsRemaining;
  } else {
    targetDisplay.classList.add('hidden');
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

// Render inline match status bar inside the controls sheet header
function renderInlineMatchStatusBar() {
  const container = document.getElementById('controls-match-status-bar');
  if (!container) return;
  container.innerHTML = '';

  const striker = matchState.striker;
  const nonStriker = matchState.nonStriker;
  const bowler = matchState.bowler;

  if (striker) {
    const getBatRating = (p) => p.batting_ovr || p.batting_rating || p.rating || p.ovr || 0;
    const badge = document.createElement('div');
    badge.className = 'status-badge-inline highlight';
    badge.innerHTML = `<span>🏏 Striker:</span> <strong>${striker.name} (${getBatRating(striker)})</strong>`;
    container.appendChild(badge);
  }

  if (nonStriker) {
    const getBatRating = (p) => p.batting_ovr || p.batting_rating || p.rating || p.ovr || 0;
    const badge = document.createElement('div');
    badge.className = 'status-badge-inline';
    badge.innerHTML = `<span>Non-Striker:</span> <strong>${nonStriker.name} (${getBatRating(nonStriker)})</strong>`;
    container.appendChild(badge);
  }

  if (bowler) {
    const getBowlRating = (p) => p.bowling_ovr || p.bowling_rating || p.rating || p.ovr || 0;
    const badge = document.createElement('div');
    badge.className = 'status-badge-inline confirmed';
    badge.innerHTML = `<span>🎳 Bowler:</span> <strong>${bowler.name} (${getBowlRating(bowler)})</strong>`;
    container.appendChild(badge);
  }
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

  // Render the inline status badges
  renderInlineMatchStatusBar();

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
    waitingBlock.classList.add('hidden');
    promptText.innerText = "🏏 CHOOSE YOUR SHOT";
    promptSubtitle.innerText = "Opponent bowler is preparing delivery...";
    hideActionSections();
    incomingCard.classList.add('hidden');
    battingControls.classList.remove('hidden');
    renderBattingShots({ disabled: true });
    return;
  }

  // It is NOT my turn
  if (!matchState.isMyTurn) {
    wasMyTurn = false;
    waitingBlock.classList.remove('hidden');
    
    let waitMsg = "Waiting for opponent...";
    if (matchState.turnState === 'bowling_delivery') {
      waitMsg = "Opponent bowler is preparing delivery...";
    } else if (matchState.turnState === 'batting_shot') {
      waitMsg = "Opponent batsman is preparing shot...";
    } else if (matchState.turnState === 'selecting_wicket_batsman') {
      waitMsg = "Opponent is picking next batsman...";
    } else if (matchState.turnState === 'selecting_over_bowler') {
      waitMsg = "Opponent is picking next bowler...";
    }
    document.getElementById('waiting-status-text').innerText = waitMsg;
    
    promptText.innerText = "⏳ OPPONENT'S TURN";
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

  if (matchState.turnState === 'bowling_delivery') {
    promptText.innerText = "🎳 BOWLER CONTROLS";
    promptSubtitle.innerText = matchState.deliveryOptions?.is_spinner
      ? "Tap a spin delivery"
      : "Select a delivery, then choose its length";
    document.getElementById('bowling-controls').classList.remove('hidden');
    document.getElementById('incoming-delivery-container').classList.add('hidden');
    renderBowlerVariations();
  } 
  else if (matchState.turnState === 'batting_shot') {
    promptText.innerText = "🏏 CHOOSE YOUR SHOT";
    
    if (matchState.currentDelivery) {
      const delName = String(matchState.currentDelivery).replace(/_/g, ' ').toUpperCase();
      const speedName = matchState.currentSpeed ? String(matchState.currentSpeed).toUpperCase() : 'NORMAL';
      const speedKmh = Number.isFinite(Number(matchState.currentSpeedKmh))
        ? ` • ${Number(matchState.currentSpeedKmh)} KM/H`
        : '';
      const displayText = `${speedName} ${delName}${speedKmh}`;

      promptSubtitle.innerHTML = `<span class="glow-text" style="color:var(--warning-accent); font-weight:800; font-size:12px; letter-spacing:0.5px;">INCOMING: ${displayText}</span>`;
      incomingCard.classList.remove('hidden');
      document.getElementById('incoming-delivery-val').innerText = displayText;
    } else {
      promptSubtitle.innerText = "Pick the best shot for this ball";
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
  } 
  else if (matchState.turnState === 'selecting_over_bowler') {
    promptText.innerText = "🎳 SELECT BOWLER FOR NEXT OVER";
    promptSubtitle.innerText = "Tap a bowler to continue";
    document.getElementById('over-bowler-controls').classList.remove('hidden');
    document.getElementById('incoming-delivery-container').classList.add('hidden');
    renderOverBowlerSelectionSheet();
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
// Same shot vocabulary as Telegram /playmatch (services.bowling_service).
const BATTING_SHOTS = [
  'Drive', 'Cut', 'Pull', 'Leg Glance', 'Flick', 'Sweep',
  'Switch Hit', 'Slog', 'Loft', 'Defend', 'Leave',
].map(shot => ({ shot, label: shot }));

async function submitShot(shot) {
  // Keep the match sheet visible so the resolved outcome appears immediately.
  try {
    const res = await fetch('/api/match/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId, matchId, type: 'shot', action: { shot } })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to submit shot");
    if (data.matchState) applyMatchState(data.matchState);
    else fetchState();
  } catch (err) {
    alert(err.message);
  }
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
    btn.innerHTML = `<span class="label">${s.label}</span>`;
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

  // Minimize sheet immediately
  document.getElementById('controls-sheet').classList.add('minimized');

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
    fetchState();
  } catch (err) {
    alert(err.message);
  }
}

// Selection list for incoming batsman
function renderWicketBatsmanSelectionSheet() {
  const container = document.getElementById('wicket-batsman-list');
  if (!container) return;

  const getBatRating = (p) => p.batting_ovr || p.batting_rating || p.rating || p.ovr || 0;

  const bench = matchState.battingXI.map((player, index) => ({ player, index }))
    .filter(item => {
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
  if (currentSel === undefined && bench.length > 0) {
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
        <span class="selection-item-name">${item.player.name}</span>
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
  }).sort((a, b) => {
    if (a.eligible !== b.eligible) return b.eligible ? 1 : -1;
    return getBowlRating(b.player) - getBowlRating(a.player);
  });

  let currentSel = container.querySelector('.selection-item.selected')?.dataset.index;
  const firstEligible = bench.find(item => item.eligible);
  if (currentSel === undefined && firstEligible) {
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
        <span class="selection-item-name">${item.player.name}</span>
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
              ${comm.bowler ? `<span>${comm.bowler.name}: <b>${comm.bowler.wickets}-${comm.bowler.runsConceded}</b> (${comm.bowler.overs}ov)</span>` : ''}
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
          <div class="total-score">Total: <b>${comm.runs}/${comm.wickets}</b> in ${comm.overs} ov</div>
          ${comm.target ? `<div class="target-needed">🏹 Target: <b>${comm.target} runs</b></div>` : ''}
          ${comm.winner ? `<div class="match-winner">🏆 <b>${comm.winner} WIN!</b></div>` : ''}
          ${comm.motm ? `<div class="match-motm">⭐ MOTM: <b>${comm.motm.name}</b> — ${comm.motm.runs}R ${comm.motm.wickets}W</div>` : ''}
        </div>
      `;
    } else {
      item.className = "cricbuzz-ball-row";

      const richText = _enrichCommentaryText(comm);
      let highlightedText = richText
        .replace(/\b([A-Z][a-z]+ [A-Z][a-z]+)\b/g, "<span class='hl-player'>$1</span>")
        .replace(/\b(WICKET!?|OUT!?|FOUR!?|SIX!?|MAXIMUM|BOUNDARY|BOWLED|CAUGHT|LBW|STUMPED)/gi,
                 "<b class='hl-event'>$1</b>");

      const outcomeClass = comm.isWicket ? 'outcome-wicket' :
                           (comm.runs === 4 ? 'outcome-four' :
                           (comm.runs === 6 ? 'outcome-six' :
                           (comm.runs === 0 ? 'outcome-dot' :
                           (comm.runs >= 3 ? 'outcome-boundary' : 'outcome-normal'))));

      const outcomeText = comm.isWicket ? 'W' : comm.runs.toString();

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

// Render Tab panels
function renderScorecardPanel() {
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
    `TOTAL ${activeInnings.runs}/${activeInnings.wickets} (${activeInnings.overs}.${activeInnings.balls} Ov)`;

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

  // Render bowling rows
  const bowlContainer = document.getElementById('scorecard-bowling-rows');
  bowlContainer.innerHTML = '';

  opposingTeam.xi.forEach((player) => {
    const pid = player.id ? player.id.toString() : '';
    const isCurrent = matchState.bowler && matchState.bowler.id && matchState.bowler.id.toString() === pid;
    const pStat = (pid && statsPool[pid]) ? statsPool[pid] : null;

    const overs = (isCurrent && matchState.bowler.stats) ? matchState.bowler.stats.overs : (pStat ? pStat.overs : 0);
    const runsC = (isCurrent && matchState.bowler.stats) ? matchState.bowler.stats.runsConceded : (pStat ? pStat.runsConceded : 0);
    const wkts = (isCurrent && matchState.bowler.stats) ? matchState.bowler.stats.wickets : (pStat ? pStat.wickets : 0);

    if (!isCurrent && (parseFloat(overs) === 0 && runsC === 0 && wkts === 0)) return;

    const er = parseFloat(overs) > 0 ? (runsC / parseFloat(overs)).toFixed(2) : '0.00';

    const row = document.createElement('div');
    row.className = 'tb-row';
    row.innerHTML = `
      <span class="tb-row-name">
        <span class="tb-row-name-text">${player.name}</span>
        ${isCurrent ? '<span class="tb-row-status active">bowling*</span>' : ''}
      </span>
      <span class="tb-num-val">${overs || '0'}</span>
      <span class="tb-num-val">0</span>
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

// 5. Render Final Result Overlay modal
function renderResultScreen() {
  const overlay = document.getElementById('result-overlay');
  overlay.classList.remove('hidden');

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
    `${inn1.runs}/${inn1.wickets} (${inn1.overs}.${inn1.balls} ov)`;
  document.getElementById('result-inn2-score').innerText =
    `${inn2.runs}/${inn2.wickets} (${inn2.overs}.${inn2.balls} ov)`;

  // Render MOTM
  const motmSection = document.getElementById('result-motm-section');
  if (result && result.motm) {
    motmSection.classList.remove('hidden');
    document.getElementById('result-motm-name').innerText = result.motm.name;
    document.getElementById('result-motm-stats').innerText = 
      `${result.motm.impactPoints || 0} Impact Points · ${result.motm.runs} runs (${result.motm.balls}b) & ${result.motm.wickets} wickets (${result.motm.overs} ov)`;
  } else {
    motmSection.classList.add('hidden');
  }
}

function runAutoplayAction() {
  if (!autoplayActive || !matchState || matchState.isProcessing) return;

  // 1. Check if it's xi_selection phase and we haven't confirmed yet
  if (matchState.status === 'xi_selection') {
    const isHost = matchState.myRole === 'host';
    const isGuest = matchState.myRole === 'guest';
    const confirmedHost = matchState.host.confirmed;
    const confirmedGuest = matchState.guest && matchState.guest.confirmed;

    const myConfirmed = isHost ? confirmedHost : confirmedGuest;
    if (!myConfirmed) {
      // Batsman setup auto-click
      const sItem = document.querySelector('#striker-list .selection-item:not(.disabled)');
      if (sItem) sItem.click();
      const nsItem = document.querySelector('#non-striker-list .selection-item:not(.disabled)');
      if (nsItem) nsItem.click();

      // Bowler setup auto-click
      const bItem = document.querySelector('#bowler-list .selection-item:not(.disabled)');
      if (bItem) bItem.click();

      console.log("[Autoplay] Auto-submitting Setup Selection...");
      submitSetup();
    }
    return;
  }

  // 2. Check if gameplay phase and it is my turn
  if ((matchState.status === 'innings1' || matchState.status === 'innings2') && matchState.isMyTurn) {
    if (matchState.turnState === 'bowling_delivery') {
      const deliveryGrid = document.getElementById('bowler-delivery-grid');
      const buttons = deliveryGrid.querySelectorAll('.btn-variation');
      if (buttons.length > 0) {
        const randBtn = buttons[Math.floor(Math.random() * buttons.length)];
        randBtn.click();

        const speedButtons = document.querySelectorAll('.btn-speed');
        if (speedButtons.length > 0 && !document.getElementById('bowling-speed-section').classList.contains('hidden')) {
          const randSpeedBtn = speedButtons[Math.floor(Math.random() * speedButtons.length)];
          randSpeedBtn.click();
        }

        setTimeout(submitDelivery, 75);
      }
    }
    else if (matchState.turnState === 'batting_shot') {
      const shotSection = document.getElementById('batting-controls');
      const buttons = shotSection.querySelectorAll('.btn-action-card');
      if (buttons.length > 0) {
        const randBtn = buttons[Math.floor(Math.random() * buttons.length)];
        setTimeout(() => randBtn.click(), 75);
      }
    }
    else if (matchState.turnState === 'selecting_wicket_batsman') {
      const item = document.querySelector('#wicket-batsman-list .selection-item:not(.disabled)');
      if (item) {
        item.click();
        const index = parseInt(item.dataset.index);
        setTimeout(() => {
          document.getElementById('controls-sheet').classList.add('minimized');
          selectNextBatsman(index);
        }, 75);
      }
    }
    else if (matchState.turnState === 'selecting_over_bowler') {
      const item = document.querySelector('#over-bowler-list .selection-item:not(.disabled)');
      if (item) {
        item.click();
        const index = parseInt(item.dataset.index);
        setTimeout(() => {
          document.getElementById('controls-sheet').classList.add('minimized');
          selectNextBowler(index);
        }, 75);
      }
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

// Event Flash Animation & Haptic Feedback
function triggerMatchFlashAnimation(comm) {
  let title = "";
  let sub = comm.text || "";
  let animationClass = "";
  
  if (comm.isWicket) {
    title = "🔴 OUT!";
    animationClass = "flash-wicket";
  } else if (comm.runs === 6) {
    title = "🚀 SIX!";
    animationClass = "flash-six";
  } else if (comm.runs === 4) {
    title = "⚡ FOUR!";
    animationClass = "flash-four";
  } else if (comm.runs === 0) {
    title = "⚫ DOT BALL";
    animationClass = "flash-dot";
  } else {
    title = `🏏 ${comm.runs} RUNS`;
    animationClass = "flash-runs";
  }
  
  // Trigger Telegram WebApp native haptic notifications if available
  if (tg && tg.HapticFeedback) {
    try {
      const haptic = tg.HapticFeedback;
      if (comm.isWicket) {
        haptic.notificationOccurred('error');
      } else if (comm.runs === 4 || comm.runs === 6) {
        haptic.notificationOccurred('success');
      } else {
        haptic.impactOccurred('medium');
      }
    } catch (e) {
      console.warn("Haptic trigger failed:", e);
    }
  }

  // Create flash overlay
  let overlay = document.getElementById('event-flash-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'event-flash-overlay';
    document.body.appendChild(overlay);
  }
  
  overlay.className = `event-flash-overlay ${animationClass}`;
  overlay.innerHTML = `
    <div class="flash-content">
      <div class="flash-title">${title}</div>
      <div class="flash-subtitle">${sub}</div>
    </div>
  `;
  
  // Confetti/particles for major events
  if (comm.runs === 6 || comm.isWicket || comm.runs === 4) {
    const color = comm.runs === 6 ? '#ffa502' : (comm.runs === 4 ? '#1e90ff' : '#ff4757');
    createParticles(overlay, color);
  }
  
  overlay.style.display = 'flex';
  setTimeout(() => {
    overlay.style.display = 'none';
  }, 1200);
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
