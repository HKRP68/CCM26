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

// Selected actions
let selectedDelivery = null;
let selectedSpeed = 'normal';
let autoplayActive = false;
let activeScorecardTab = 'innings1'; // 'innings1' or 'innings2'
let lastBallUniqueId = null;
let wasMyTurn = false;

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

  // 3. Fallback to spectator if userId is still unresolved
  if (!userId) {
    userId = 'spectator';
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
      const selectedEl = document.querySelector('#wicket-batsman-list .dropdown-item.selected');
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
      const selectedEl = document.querySelector('#over-bowler-list .dropdown-item.selected');
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

  // View match button
  document.getElementById('view-match-btn').addEventListener('click', () => {
    document.getElementById('result-overlay').classList.add('hidden');
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
    document.getElementById(`tab-${tab}`).addEventListener('click', () => {
      tabs.forEach(t => {
        document.getElementById(`tab-${t}`).classList.remove('active');
        document.getElementById(`panel-${t}`).classList.remove('active');
      });
      document.getElementById(`tab-${tab}`).classList.add('active');
      document.getElementById(`panel-${tab}`).classList.add('active');
    });
  });

  // Scorecard Team Switch tabs
  const tabHost = document.getElementById('scorecard-tab-host');
  const tabGuest = document.getElementById('scorecard-tab-guest');

  tabHost.addEventListener('click', () => {
    activeScorecardTab = 'innings1';
    tabHost.classList.add('active');
    tabGuest.classList.remove('active');
    renderScorecardPanel();
  });

  tabGuest.addEventListener('click', () => {
    activeScorecardTab = 'innings2';
    tabGuest.classList.add('active');
    tabHost.classList.remove('active');
    renderScorecardPanel();
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
  localStorage.setItem('crickidex_user_id', userId);
  
  // Start the regular polling
  startPolling();
}

// Polling Loop
function startPolling() {
  fetchState();
  pollingInterval = setInterval(fetchState, 1500);
}

function stopPolling() {
  if (pollingInterval) {
    clearInterval(pollingInterval);
    pollingInterval = null;
  }
}

// API Call - GET state
async function fetchState() {
  try {
    // Build query safely without sending stringified "null" or "undefined"
    const params = new URLSearchParams();
    if (userId) params.append('userId', userId);
    if (matchId) params.append('matchId', matchId);

    const response = await fetch(`/api/match?${params.toString()}`);
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
    matchState = data;

    // Check for new ball events to trigger premium alerts & haptic feedback
    if (matchState.commentary && matchState.commentary.length > 0) {
      const latest = matchState.commentary[0];
      if (latest && (latest.type === 'ball' || latest.runs !== undefined)) {
        const uniqueKey = `${matchState.status}_${latest.over}`;
        if (lastBallUniqueId && lastBallUniqueId !== uniqueKey) {
          triggerMatchFlashAnimation(latest);
        }
        lastBallUniqueId = uniqueKey;
      }
    }
    
    // Update Header info
    document.getElementById('header-host-name').innerText = (matchState.host.teamName || matchState.host.username || 'HOST').toUpperCase();
    document.getElementById('header-guest-name').innerText = (matchState.guest ? (matchState.guest.teamName || matchState.guest.username) : 'AI XI').toUpperCase();
    document.getElementById('header-match-uuid').innerText = `MATCH ID: ${matchState.id.substring(0, 6).toUpperCase()}`;

    // Route to appropriate screen
    if (matchState.status === 'xi_selection') {
      renderSetupScreen();
    } else if (matchState.status === 'innings1' || matchState.status === 'innings2') {
      renderGameplayScreen();
    } else if (matchState.status === 'completed') {
      renderGameplayScreen();
      renderResultScreen();
      stopPolling();
    }

    // Trigger autoplay evaluation after state update
    setTimeout(runAutoplayAction, 1000);
  } catch (err) {
    console.error("Polling error:", err);
  }
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

  // Render the inline status badges
  renderInlineMatchStatusBar();

  // If executing simulation
  if (matchState.isProcessing) {
    waitingBlock.classList.remove('hidden');
    document.getElementById('waiting-status-text').innerText = "Simulating delivery...";
    
    promptText.innerText = "⚡ MATCH IN PROGRESS";
    promptSubtitle.innerText = "Simulating delivery...";

    document.getElementById('batting-controls').classList.add('hidden');
    document.getElementById('bowling-controls').classList.add('hidden');
    document.getElementById('wicket-batsman-controls').classList.add('hidden');
    document.getElementById('over-bowler-controls').classList.add('hidden');
    return;
  }

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

    document.getElementById('batting-controls').classList.add('hidden');
    document.getElementById('bowling-controls').classList.add('hidden');
    document.getElementById('wicket-batsman-controls').classList.add('hidden');
    document.getElementById('over-bowler-controls').classList.add('hidden');
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

    document.getElementById('batting-controls').classList.add('hidden');
    document.getElementById('bowling-controls').classList.add('hidden');
    document.getElementById('wicket-batsman-controls').classList.add('hidden');
    document.getElementById('over-bowler-controls').classList.add('hidden');
    return;
  }

  // It IS my turn!
  waitingBlock.classList.add('hidden');

  // Auto-expand sheet when it becomes the user's turn
  if (!wasMyTurn) {
    if (activeBlock.classList.contains('minimized')) {
      activeBlock.classList.remove('minimized');
      const iconSvg = document.getElementById('toggle-controls-icon');
      if (iconSvg) {
        iconSvg.innerHTML = '<polyline points="6 9 12 15 18 9"></polyline>';
      }
    }
    wasMyTurn = true;
  }

  // Hide all sections initially
  document.getElementById('batting-controls').classList.add('hidden');
  document.getElementById('bowling-controls').classList.add('hidden');
  document.getElementById('wicket-batsman-controls').classList.add('hidden');
  document.getElementById('over-bowler-controls').classList.add('hidden');

  if (matchState.turnState === 'bowling_delivery') {
    promptText.innerText = "🎳 BOWLER CONTROLS";
    promptSubtitle.innerText = "Deliver a variation to fool the batsman";
    document.getElementById('bowling-controls').classList.remove('hidden');
    document.getElementById('incoming-delivery-container').classList.add('hidden');
    renderBowlerVariations();
  } 
  else if (matchState.turnState === 'batting_shot') {
    promptText.innerText = "🏏 CHOOSE YOUR SHOT";
    
    const incomingCard = document.getElementById('incoming-delivery-container');
    if (matchState.currentDelivery) {
      let displayText = '';
      if (matchState.currentDelivery === 'mystery_ball') {
        displayText = '🔮 MYSTERY BALL';
      } else {
        const delName = matchState.currentDelivery.replace(/_/g, ' ').toUpperCase();
        const speedName = matchState.currentSpeed ? matchState.currentSpeed.toUpperCase() : 'NORMAL';
        displayText = `${speedName} ${delName}`;
      }

      promptSubtitle.innerHTML = `<span class="glow-text" style="color:var(--warning-accent); font-weight:800; font-size:12px; letter-spacing:0.5px;">INCOMING: ${displayText}</span>`;
      incomingCard.classList.remove('hidden');
      document.getElementById('incoming-delivery-val').innerText = displayText;
    } else {
      promptSubtitle.innerText = "Pick the best shot for this ball";
      incomingCard.classList.add('hidden');
    }
    document.getElementById('batting-controls').classList.remove('hidden');
    renderBattingShots();
  } 
  else if (matchState.turnState === 'selecting_wicket_batsman') {
    promptText.innerText = "⚠️ WICKET! SELECT REPLACEMENT";
    promptSubtitle.innerText = "Choose the next batsman to walk out";
    document.getElementById('wicket-batsman-controls').classList.remove('hidden');
    document.getElementById('incoming-delivery-container').classList.add('hidden');
    renderWicketBatsmanSelectionSheet();
  } 
  else if (matchState.turnState === 'selecting_over_bowler') {
    promptText.innerText = "🎳 SELECT BOWLER FOR NEXT OVER";
    promptSubtitle.innerText = "Choose who bowls the next over";
    document.getElementById('over-bowler-controls').classList.remove('hidden');
    document.getElementById('incoming-delivery-container').classList.add('hidden');
    renderOverBowlerSelectionSheet();
  }
}

// Render dynamic bowler variations grid based on bowler type
function renderBowlerVariations() {
  const deliveryGrid = document.getElementById('bowler-delivery-grid');
  const bowlerType = matchState.bowler?.bowler_type || 'fast';
  const isOffSpin = bowlerType === 'off_spin';
  const isLegSpin = bowlerType === 'leg_spin';
  const isSpin = isOffSpin || isLegSpin || bowlerType.toLowerCase().includes('spin');

  const cacheKey = isOffSpin ? 'off_spin' : (isLegSpin ? 'leg_spin' : 'fast');
  if (deliveryGrid.dataset.rendered === cacheKey) return;
  deliveryGrid.dataset.rendered = cacheKey;

  let deliveries = [];
  if (isOffSpin) {
    deliveries = [
      { id: 'off_break', name: 'Off Break' },
      { id: 'carrom_ball', name: 'Carrom Ball' },
      { id: 'arm_ball', name: 'Arm Ball' },
      { id: 'doosra', name: 'Doosra' },
      { id: 'top_spinner_off', name: 'Top Spinner' },
      { id: 'mystery_ball', name: 'Mystery Ball' }
    ];
    document.getElementById('bowling-speed-section').classList.add('hidden');
    selectedSpeed = 'normal';
  } else if (isSpin) {
    deliveries = [
      { id: 'leg_break', name: 'Leg Break' },
      { id: 'googly', name: 'Googly' },
      { id: 'flipper', name: 'Flipper' },
      { id: 'top_spinner_leg', name: 'Top Spinner' },
      { id: 'slider', name: 'Slider' },
      { id: 'mystery_ball', name: 'Mystery Ball' }
    ];
    document.getElementById('bowling-speed-section').classList.add('hidden');
    selectedSpeed = 'normal';
  } else {
    deliveries = [
      { id: 'yorker', name: 'Yorker' },
      { id: 'full_length', name: 'Full Length' },
      { id: 'good_length', name: 'Good Length' },
      { id: 'short', name: 'Short Ball' },
      { id: 'bouncer', name: 'Bouncer' }
    ];
    document.getElementById('bowling-speed-section').classList.remove('hidden');
    
    // Dynamically render speed variations for fast bowlers only
    const speedGroup = document.querySelector('.speed-button-group');
    if (speedGroup) {
      speedGroup.innerHTML = `
        <button class="btn btn-speed${selectedSpeed === 'fast' ? ' active' : ''}" data-speed="fast">Fast</button>
        <button class="btn btn-speed${selectedSpeed === 'normal' ? ' active' : ''}" data-speed="normal">Normal</button>
        <button class="btn btn-speed${selectedSpeed === 'slow' ? ' active' : ''}" data-speed="slow">Slow</button>
        <button class="btn btn-speed${selectedSpeed === 'inswinger' ? ' active' : ''}" data-speed="inswinger">Inswing</button>
        <button class="btn btn-speed${selectedSpeed === 'outswinger' ? ' active' : ''}" data-speed="outswinger">Outswing</button>
      `;
      // Re-bind listeners for speed buttons
      const speedButtons = speedGroup.querySelectorAll('.btn-speed');
      speedButtons.forEach(btn => {
        btn.addEventListener('click', (e) => {
          speedButtons.forEach(b => b.classList.remove('active'));
          e.currentTarget.classList.add('active');
          selectedSpeed = e.currentTarget.dataset.speed;
        });
      });
    }
  }

  deliveryGrid.innerHTML = '';
  deliveries.forEach(del => {
    const btn = document.createElement('button');
    btn.className = "btn-variation";
    btn.innerText = del.name;
    btn.dataset.delivery = del.id;
    if (del.id === 'mystery_ball' && matchState.mysteryBallBowledThisOver) {
      btn.disabled = true;
    }
    btn.addEventListener('click', (e) => {
      const allActionBtns = deliveryGrid.querySelectorAll('.btn-variation');
      allActionBtns.forEach(b => b.classList.remove('active'));
      e.currentTarget.classList.add('active');
      selectedDelivery = e.currentTarget.dataset.delivery;
    });
    deliveryGrid.appendChild(btn);
  });

  selectedDelivery = null;
}

// Render batting shot event listeners
function renderBattingShots() {
  const shotSection = document.getElementById('batting-controls');
  const actionBtns = shotSection.querySelectorAll('.btn-action-card');
  
  actionBtns.forEach(btn => {
    const newBtn = btn.cloneNode(true);
    btn.parentNode.replaceChild(newBtn, btn);
    
    newBtn.addEventListener('click', async (e) => {
      const shot = e.currentTarget.dataset.shot;
      // Minimize sheet immediately
      document.getElementById('controls-sheet').classList.add('minimized');
      
      try {
        const res = await fetch('/api/match/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            userId,
            type: 'shot',
            action: { shot }
          })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Failed to submit shot");
        fetchState();
      } catch (err) {
        alert(err.message);
      }
    });
  });
}

// Bowling Submit Handler
async function submitDelivery() {
  if (!selectedDelivery) {
    alert("Please select a delivery variation first!");
    return;
  }

  // Minimize sheet immediately
  document.getElementById('controls-sheet').classList.add('minimized');

  const isMysteryBall = selectedDelivery === 'mystery_ball';

  try {
    const res = await fetch('/api/match/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        userId,
        type: 'delivery',
        action: {
          delivery: selectedDelivery,
          speed: selectedSpeed,
          isMysteryBall
        }
      })
    });
    
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to submit delivery");
    fetchState();
  } catch (err) {
    alert(err.message);
  }
}

// Selection list for incoming batsman
function renderWicketBatsmanSelectionSheet() {
  const container = document.getElementById('wicket-batsman-list');
  const dropdown = document.getElementById('batsman-dropdown');
  const trigger = document.getElementById('batsman-dropdown-trigger');
  if (!container || !dropdown || !trigger) return;

  // Set up dropdown toggle
  trigger.onclick = (e) => {
    e.stopPropagation();
    // Close other dropdowns
    document.querySelectorAll('.custom-dropdown').forEach(d => {
      if (d !== dropdown) d.classList.remove('open');
    });
    dropdown.classList.toggle('open');
  };

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
    trigger.querySelector('.selected-value').innerText = "No batsmen remaining";
    return;
  }

  let currentSel = container.dataset.selectedIndex;
  if (currentSel === undefined && bench.length > 0) {
    currentSel = bench[0].index.toString();
    container.dataset.selectedIndex = currentSel;
  }

  const selectedItem = bench.find(item => item.index.toString() === currentSel);
  if (selectedItem) {
    trigger.querySelector('.selected-value').innerText = `${selectedItem.player.name} (${getBatRating(selectedItem.player)} OVR)`;
  }

  const build = (selectedIdx) => {
    container.innerHTML = '';
    bench.forEach(item => {
      const div = document.createElement('div');
      div.className = 'dropdown-item';
      div.dataset.index = item.index;
      
      const isSelected = selectedIdx === item.index.toString();
      if (isSelected) div.classList.add('selected');
      
      div.innerHTML = `
        <div class="dropdown-item-info">
          <span class="dropdown-item-name">${item.player.name}</span>
          <span class="dropdown-item-meta">${item.player.role || 'Batsman'}</span>
        </div>
        <span class="dropdown-item-stat">${getBatRating(item.player)} OVR</span>
      `;
      
      div.onclick = (e) => {
        e.stopPropagation();
        container.dataset.selectedIndex = item.index.toString();
        trigger.querySelector('.selected-value').innerText = `${item.player.name} (${getBatRating(item.player)} OVR)`;
        dropdown.classList.remove('open');
        build(item.index.toString());
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
function renderOverBowlerSelectionSheet() {
  const container = document.getElementById('over-bowler-list');
  const dropdown = document.getElementById('bowler-dropdown');
  const trigger = document.getElementById('bowler-dropdown-trigger');
  if (!container || !dropdown || !trigger) return;

  // Set up dropdown toggle
  trigger.onclick = (e) => {
    e.stopPropagation();
    // Close other dropdowns
    document.querySelectorAll('.custom-dropdown').forEach(d => {
      if (d !== dropdown) d.classList.remove('open');
    });
    dropdown.classList.toggle('open');
  };

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
        reason = `Max limit (${stats.overs}/${maxOvers} ov)`;
      } else {
        reason = `Quota full (Fallback allowed)`;
      }
    }

    return { player, index, eligible, reason, overs: stats.overs };
  }).sort((a, b) => {
    if (a.eligible !== b.eligible) return b.eligible ? 1 : -1;
    return getBowlRating(b.player) - getBowlRating(a.player);
  });

  let currentSel = container.dataset.selectedIndex;
  const firstEligible = bench.find(item => item.eligible);
  if (currentSel === undefined && firstEligible) {
    currentSel = firstEligible.index.toString();
    container.dataset.selectedIndex = currentSel;
  }

  const selectedItem = bench.find(item => item.index.toString() === currentSel);
  if (selectedItem) {
    trigger.querySelector('.selected-value').innerText = `${selectedItem.player.name} (${getBowlRating(selectedItem.player)} OVR)`;
  }

  const build = (selectedIdx) => {
    container.innerHTML = '';
    bench.forEach(item => {
      const div = document.createElement('div');
      div.className = 'dropdown-item';
      if (!item.eligible) div.classList.add('disabled');
      div.dataset.index = item.index;
      
      const isSelected = selectedIdx === item.index.toString();
      if (isSelected) div.classList.add('selected');
      
      let metaText = item.player.bowler_type || 'Bowler';
      const cleanOvers = item.overs || 0;
      if (cleanOvers > 0) {
        metaText += ` • ${cleanOvers} ov`;
      }
      if (item.reason) {
        metaText += ` [${item.reason}]`;
      }
      
      div.innerHTML = `
        <div class="dropdown-item-info">
          <span class="dropdown-item-name">${item.player.name}</span>
          <span class="dropdown-item-meta">${metaText}</span>
        </div>
        <span class="dropdown-item-stat">${getBowlRating(item.player)} OVR</span>
      `;
      
      if (item.eligible) {
        div.onclick = (e) => {
          e.stopPropagation();
          container.dataset.selectedIndex = item.index.toString();
          trigger.querySelector('.selected-value').innerText = `${item.player.name} (${getBowlRating(item.player)} OVR)`;
          dropdown.classList.remove('open');
          build(item.index.toString());
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
          <span class="over-total-score">Score: ${comm.totalRuns}/${comm.totalWickets}</span>
        </div>
        <div class="over-end-stats">
          <div class="stats-row">
            <div class="stat-batsmen">
              ${comm.striker ? `<span>${comm.striker.name}: <b>${comm.striker.runs}</b> (${comm.striker.balls}b)</span>` : ''}
              ${comm.nonStriker ? `<span>${comm.nonStriker.name}: <b>${comm.nonStriker.runs}</b> (${comm.nonStriker.balls}b)</span>` : ''}
            </div>
            <div class="stat-bowler">
              <span>${comm.bowler.name}: <b>${comm.bowler.wickets}-${comm.bowler.runsConceded}</b> (${comm.bowler.overs} ov)</span>
            </div>
          </div>
        </div>
      `;
    } else if (comm.type === 'end_of_innings') {
      item.className = "cricbuzz-innings-end";
      item.innerHTML = `
        <div class="innings-end-header">
          🎯 Innings ${comm.inningsIdx + 1} Completed
        </div>
        <div class="innings-end-body">
          <div class="total-score">Total: <b>${comm.runs}/${comm.wickets}</b> in ${comm.overs} ov</div>
          ${comm.target ? `<div class="target-needed">Target: <b>${comm.target} runs</b></div>` : ''}
          ${comm.winner ? `<div class="match-winner">🏆 Winner: <b>${comm.winner}</b></div>` : ''}
          ${comm.motm ? `<div class="match-motm">🌟 Man of the Match: <b>${comm.motm.name}</b> (${comm.motm.runs} runs, ${comm.motm.wickets} wkts)</div>` : ''}
        </div>
      `;
    } else {
      item.className = "cricbuzz-ball-row";
      
      // Clean text highlights
      let highlightedText = comm.text
        .replace(/([A-Z][a-zA-Z\s0-9]+(?=\s+to\s+|\s+bowls\s+))/g, "<span class='hl-player'>$1</span>")
        .replace(/(OUT!|WICKET!|FOUR|SIX|runs|runs conceded)/gi, "<b>$1</b>");

      const outcomeClass = comm.isWicket ? 'outcome-wicket' : 
                           (comm.runs === 4 ? 'outcome-four' : 
                           (comm.runs === 6 ? 'outcome-six' : 
                           (comm.runs === 0 ? 'outcome-dot' : 'outcome-normal')));

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

  // Set titles
  document.getElementById('scorecard-batting-title').innerText = `${activeTeam.teamName.toUpperCase()} BATTING`;
  document.getElementById('scorecard-bowling-title').innerText = `${opposingTeam.teamName.toUpperCase()} BOWLING`;

  // Render batting rows
  activeTeam.xi.forEach((player) => {
    // Check stats
    const pStat = matchState.stats[player.id.toString()] || { runs: 0, balls: 0, fours: 0, sixes: 0 };
    const isActive = (matchState.striker && matchState.striker.id.toString() === player.id.toString()) || 
                     (matchState.nonStriker && matchState.nonStriker.id.toString() === player.id.toString());

    if (!isActive && (!pStat || !pStat.balls || pStat.balls === 0)) return;

    const runs = pStat.runs;
    const balls = pStat.balls;
    const fours = pStat.fours || 0;
    const sixes = pStat.sixes || 0;
    const sr = balls > 0 ? ((runs / balls) * 100).toFixed(1) : '0.0';

    // Status label
    let statusText = '';
    let statusClass = 'out';
    if (isActive) {
      statusText = 'not out*';
      statusClass = 'active';
    } else if (pStat.isOut) {
      statusText = pStat.outDetail || 'out';
      statusClass = 'out';
    } else if (pStat.balls > 0) {
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

  // Extras
  document.getElementById('scorecard-extras').innerText = `Extras ${activeInnings.extras || 0} (w 0, nb 0, lb 0, b 0, p 0)`;
  document.getElementById('scorecard-total').innerText = 
    `TOTAL ${activeInnings.runs}/${activeInnings.wickets} (${activeInnings.overs}.${activeInnings.balls} Ov)`;

  // Yet to bat
  const dismissedNames = matchState.commentary.filter(c => c.isWicket).map(c => {
    // try to match any name from xi
    const matched = activeTeam.xi.find(p => c.text.includes(p.name));
    return matched ? matched.id : null;
  }).filter(Boolean);

  const activeIds = [matchState.striker?.id?.toString(), matchState.nonStriker?.id?.toString()].filter(Boolean);
  const dismissedNamesString = dismissedNames.map(id => id ? id.toString() : '');
  const ytbPlayers = activeTeam.xi.filter(p => p.id && !activeIds.includes(p.id.toString()) && !dismissedNamesString.includes(p.id.toString()));
  document.getElementById('scorecard-yet-to-bat').innerText = ytbPlayers.map(p => p.name).join(', ') || 'None';

  // Render bowling rows
  const bowlContainer = document.getElementById('scorecard-bowling-rows');
  bowlContainer.innerHTML = '';

  opposingTeam.xi.forEach((player) => {
    const isCurrent = matchState.bowler && matchState.bowler.id.toString() === player.id.toString();
    const pStat = matchState.stats[player.id.toString()] || (isCurrent ? matchState.bowler.stats : null);
    if (!isCurrent && (!pStat || (parseFloat(pStat.overs) === 0 && pStat.runsConceded === 0 && pStat.wickets === 0))) return;

    const overs = pStat ? (pStat.overs || '0.0') : '0.0';
    const maidens = 0;
    const runs = pStat ? (pStat.runsConceded || 0) : 0;
    const wickets = pStat ? (pStat.wickets || 0) : 0;
    const er = pStat && parseFloat(overs) > 0 ? (runs / parseFloat(overs)).toFixed(2) : '0.00';

    const row = document.createElement('div');
    row.className = 'tb-row';
    row.innerHTML = `
      <span class="tb-row-name">
        <span class="tb-row-name-text">${player.name}</span>
        ${isCurrent ? '<span class="tb-row-status active">bowling</span>' : ''}
      </span>
      <span class="tb-num-val">${overs}</span>
      <span class="tb-num-val">${maidens}</span>
      <span class="tb-num-val">${runs}</span>
      <span class="tb-num-val">${wickets}</span>
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

  // Calculate my coins based on role and outcome
  const isHost = matchState.host.telegramId.toString() === userId.toString();
  const isGuest = matchState.guest && matchState.guest.telegramId && (matchState.guest.telegramId.toString() === userId.toString());
  
  let myCoins = 0;
  if (result) {
    const iAmWinner = result.winner && (
      (isHost && result.winner.username === matchState.host.username) ||
      (isGuest && result.winner.username === matchState.guest.username)
    );
    myCoins = iAmWinner ? result.winnerReward : result.loserReward;
  } else {
    myCoins = matchState.myRole === 'batting' ? 1500 : 500;
  }
  document.getElementById('result-reward-amount').innerText = `+${myCoins.toLocaleString()}`;
  
  const hostId = matchState.host.telegramId;
  const guestId = matchState.guest ? matchState.guest.telegramId : 'ai';
  const hostInn = matchState.innings.find(i => i.battingId.toString() === hostId.toString()) || { runs: 0, wickets: 0, overs: 0, balls: 0 };
  const guestInn = matchState.innings.find(i => i.battingId.toString() === guestId.toString()) || { runs: 0, wickets: 0, overs: 0, balls: 0 };

  document.getElementById('result-inn1-score').innerText = 
    `${hostInn.runs}/${hostInn.wickets} (${hostInn.overs}.${hostInn.balls} ov)`;
  document.getElementById('result-inn2-score').innerText = 
    `${guestInn.runs}/${guestInn.wickets} (${guestInn.overs}.${guestInn.balls} ov)`;

  // Render MOTM
  const motmSection = document.getElementById('result-motm-section');
  if (result && result.motm) {
    motmSection.classList.remove('hidden');
    document.getElementById('result-motm-name').innerText = result.motm.name;
    document.getElementById('result-motm-stats').innerText = 
      `${result.motm.runs} runs (${result.motm.balls}b) & ${result.motm.wickets} wickets (${result.motm.overs} ov)`;
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

        console.log("[Autoplay] Auto-submitting Bowling Delivery: " + selectedDelivery + ", Speed: " + selectedSpeed);
        setTimeout(submitDelivery, 500);
      }
    } 
    else if (matchState.turnState === 'batting_shot') {
      const shotSection = document.getElementById('batting-controls');
      const buttons = shotSection.querySelectorAll('.btn-action-card');
      if (buttons.length > 0) {
        const randBtn = buttons[Math.floor(Math.random() * buttons.length)];
        console.log("[Autoplay] Auto-clicking Batting Shot: " + randBtn.dataset.shot);
        setTimeout(() => randBtn.click(), 500);
      }
    } 
    else if (matchState.turnState === 'selecting_wicket_batsman') {
      const item = document.querySelector('#wicket-batsman-list .selection-item:not(.disabled)');
      if (item) {
        item.click();
        const index = parseInt(item.dataset.index);
        console.log("[Autoplay] Auto-selecting Replacement Batsman...");
        setTimeout(() => {
          document.getElementById('controls-sheet').classList.add('minimized');
          selectNextBatsman(index);
        }, 500);
      }
    } 
    else if (matchState.turnState === 'selecting_over_bowler') {
      const item = document.querySelector('#over-bowler-list .selection-item:not(.disabled)');
      if (item) {
        item.click();
        const index = parseInt(item.dataset.index);
        console.log("[Autoplay] Auto-selecting Over Bowler...");
        setTimeout(() => {
          document.getElementById('controls-sheet').classList.add('minimized');
          selectNextBowler(index);
        }, 500);
      }
    }
  }
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
  }, 1800);
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

// Close dropdowns when clicking outside
document.addEventListener('click', () => {
  document.querySelectorAll('.custom-dropdown').forEach(d => d.classList.remove('open'));
});

// Start execution
init();
