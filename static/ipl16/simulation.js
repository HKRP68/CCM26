// ===================== 16-0 — Season simulation =====================

const USER_ID = "USER";
const USER_NAME = "Your XI";
// Fixed BCCI-style group split (2024 groupings). Your XI takes LSG's slot in
// Group B. Cross-group "rivals" are index-paired (A[i] <-> B[i]) and meet twice,
// so every team plays 8 in-group + 2 vs rival + 4 cross = 14 matches.
const GROUPS = {
  A: ["CSK", "RCB", "GT", "PBKS", "DC"],
  B: ["MI", "KKR", "SRH", "RR", USER_ID],
};
const PLAYOFF_LABELS = {
  q1: "Qualifier 1",
  eliminator: "Eliminator",
  q2: "Qualifier 2",
  final: "Final",
};

const state = {
  config: {},
  userXi: [],
  players: [],
  opponents: [],
  teams: [],
  rounds: [],
  roundIndex: 0,
  standings: {},
  leaders: {},
  playoff: null,
  // Combined W-L across league AND playoffs (drives the shareable result card).
  totalWins: 0,
  totalLosses: 0,
};

const els = {
  phasePill: document.getElementById("phasePill"),
  screenTitle: document.getElementById("screenTitle"),
  recordText: document.getElementById("recordText"),
  pointsText: document.getElementById("pointsText"),
  leagueScreen: document.getElementById("leagueScreen"),
  tableScreen: document.getElementById("tableScreen"),
  playoffScreen: document.getElementById("playoffScreen"),
  matchLabel: document.getElementById("matchLabel"),
  fixtureStatus: document.getElementById("fixtureStatus"),
  vsRow: document.getElementById("vsRow"),
  userVsStats: document.getElementById("userVsStats"),
  oppName: document.getElementById("oppName"),
  oppVsStats: document.getElementById("oppVsStats"),
  fixtureResult: document.getElementById("fixtureResult"),
  pitchBadge: document.getElementById("pitchBadge"),
  rosterBody: document.getElementById("rosterBody"),
  playLeagueBtn: document.getElementById("playLeagueBtn"),
  leagueResults: document.getElementById("leagueResults"),
  leagueTable: document.getElementById("leagueTable"),
  leadersPanel: document.getElementById("leadersPanel"),
  runLeaders: document.getElementById("runLeaders"),
  wicketLeaders: document.getElementById("wicketLeaders"),
  playoffBtn: document.getElementById("playoffBtn"),
  seasonEnd: document.getElementById("seasonEnd"),
  playoffTitle: document.getElementById("playoffTitle"),
  playoffOutcome: document.getElementById("playoffOutcome"),
  playoffTeams: document.getElementById("playoffTeams"),
  playoffResult: document.getElementById("playoffResult"),
  playoffLeaders: document.getElementById("playoffLeaders"),
  playoffActions: document.getElementById("playoffActions"),
  playPlayoffBtn: document.getElementById("playPlayoffBtn"),
  scorecardPanel: document.getElementById("scorecardPanel"),
  resultBanner: document.getElementById("resultBanner"),
  scorecardGrid: document.getElementById("scorecardGrid"),
  motm: document.getElementById("motm"),
  resultSlot: document.getElementById("resultSlot"),
};

function loadCsv(path) {
  return new Promise((resolve, reject) => {
    Papa.parse(path, {
      download: true,
      header: true,
      skipEmptyLines: true,
      complete: (res) => resolve(res.data),
      error: reject,
    });
  });
}

function boot() {
  const saved = loadSeasonState();
  if (!saved || saved.xi.length !== 11) {
    showMissingDraft();
    return;
  }

  state.config = saved.config || {};
  state.userXi = saved.xi.map(normalizeSavedPlayer);

  Promise.all([
    loadCsv("ipl_master_calibrated.csv"),
    loadCsv("mapped_names.csv").catch(() => []), // optional — fall back to master names
  ])
    .then(([playerRows, nameRows]) => {
      const names = buildNameMap(nameRows);
      state.players = playerRows
        .filter((r) => r.Player_Name && r.Franchise && r.Season)
        .map((r) => normalizeCsvPlayer(r, names));
      initSeason();
    })
    .catch((err) => {
      console.error(err);
      els.matchLabel.textContent = "Load error";
      els.vsRow.hidden = true;
      els.fixtureStatus.hidden = false;
      els.fixtureStatus.textContent = "Run this page from a local server";
      els.playLeagueBtn.disabled = true;
    });
}

function loadSeasonState() {
  try {
    return JSON.parse(localStorage.getItem("seasonState") || "null");
  } catch (_) {
    return null;
  }
}

function showMissingDraft() {
  els.matchLabel.textContent = "No XI found";
  els.vsRow.hidden = true;
  els.fixtureStatus.hidden = false;
  els.fixtureStatus.textContent = "Complete your XI first";
  els.playLeagueBtn.textContent = "Back to Draft";
  els.playLeagueBtn.dataset.backToDraft = "true";
}

// Master_DB_Name -> Impact_CSV_Name, so AI opponents read with full names
// (e.g. "B Kumar" -> "Bhuvneshwar Kumar") in scorecards and leaders.
function buildNameMap(rows = []) {
  const map = {};
  rows.forEach((r) => {
    const master = (r.Master_DB_Name || "").trim();
    const display = (r.Impact_CSV_Name || "").trim();
    if (master && display) map[master] = display;
  });
  return map;
}

function normalizeSavedPlayer(p) {
  return {
    id: `${p.name}|${p.fr}|${p.season}`,
    name: p.name,
    displayName: p.displayName || p.name,
    team: USER_ID,
    fr: p.fr,
    season: p.season,
    primaryRole: p.primaryRole,
    battingOrder: p.battingOrder,
    isWk: Boolean(p.isWk),
    isOverseas: Boolean(p.isOverseas),
    ovr: Number(p.simOvr || p.ovr || 70),
    bat: Number(p.bat || p.ovr || 70),
    bowl: Number(p.bowl || p.ovr || 60),
    slot: Number(p.slot || 0),
  };
}

function normalizeCsvPlayer(r, names) {
  const name = r.Player_Name.trim();
  return {
    id: `${name}|${r.Franchise}|${r.Season}`,
    name,
    displayName: names[name] || name,
    team: r.Franchise,
    fr: r.Franchise,
    frFull: r.Franchise_Full || r.Franchise,
    season: r.Season,
    primaryRole: r.Primary_Role,
    battingOrder: r.Batting_Order,
    isWk: r.Is_Wicketkeeper === "1",
    isOverseas: r.Nationality === "Overseas",
    ovr: Math.min(+r.OVR || 70, 92),
    bat: Math.min(+r.Bat_Rat || +r.OVR || 70, 92),
    bowl: Math.min(+r.Bowl_Rat || +r.OVR || 65, 92),
  };
}

function initSeason() {
  state.opponents = buildOpponentTeams();
  state.teams = [
    makeTeam(USER_ID, USER_NAME, state.userXi),
    ...state.opponents,
  ];
  applyCatchupBuff(state.teams);
  state.teams.forEach((team) => {
    state.standings[team.id] = {
      team,
      p: 0,
      w: 0,
      l: 0,
      pts: 0,
      runsFor: 0,
      ballsFor: 0,
      runsAgainst: 0,
      ballsAgainst: 0,
    };
  });
  state.leaders = {};
  state.teams.forEach((team) => {
    team.players.forEach((p) => {
      state.leaders[p.id] = {
        name: p.displayName,
        team: team.id === USER_ID ? USER_NAME : team.short,
        runs: 0,
        wickets: 0,
      };
    });
  });
  state.rounds = buildGroupFixtures();
  state.roundIndex = 0;
  console.log(
    "Team strengths:",
    state.teams.map((t) => ({
      name: t.name,
      total: t.strength.total,
      bat: t.strength.batting,
      bowl: t.strength.bowling,
    }))
  );
  renderStrengthReadout();
  renderUserRoster();
  renderLeagueFixture();
}

// Make team strength transparent so a weak department is visible at a glance.
function renderStrengthReadout() {
  const el = document.getElementById("strengthReadout");
  if (!el) return;
  const you = state.teams[0].strength;
  el.innerHTML = `
    <span class="sr-label">Your XI</span>
    <span class="sr-stat ${you.batting < 80 ? "is-weak" : ""}">BAT ${you.batting.toFixed(1)}</span>
    <span class="sr-stat ${you.bowling < 80 ? "is-weak" : ""}">BOWL ${you.bowling.toFixed(1)}</span>
    <span class="sr-stat">OVR ${you.total.toFixed(1)}</span>
  `;
}

// 🟩 Batting / ⬜ Neutral / 🟫 Bowling — shown on the fixture card after a sim.
const PITCH_BADGES = {
  batting: { icon: "🟩", label: "Batting" },
  neutral: { icon: "⬜", label: "Neutral" },
  bowling: { icon: "🟫", label: "Bowling" },
};

function ovrTierClass(ovr) {
  if (ovr >= 92) return "ovr-gold";
  if (ovr >= 89) return "ovr-blue";
  if (ovr >= 85) return "ovr-green";
  return "ovr-white";
}

// Compact role label + colour class for the roster list, mirroring the draft.
function rosterRole(p) {
  if (p.primaryRole === "Bowler") return { label: "Bowler", cls: "role-lower" };
  if (p.primaryRole === "All-Rounder") return { label: "All-Rounder", cls: "role-finisher" };
  switch (p.battingOrder) {
    case "Opener": return { label: "Opener", cls: "role-opener" };
    case "Middle Order": return { label: "Middle", cls: "role-middle" };
    case "Finisher": return { label: "Finisher", cls: "role-finisher" };
    case "Lower Order": return { label: "Lower", cls: "role-lower" };
    default: return { label: p.battingOrder || "—", cls: "role-middle" };
  }
}

// The user's XI as a compact, scrollable roster (slot · name · role · OVR).
function renderUserRoster() {
  const you = state.teams[0];
  els.rosterBody.innerHTML = you.players
    .map((p, i) => {
      const r = rosterRole(p);
      const plane = p.isOverseas
        ? ' <img class="r-plane" src="pngs/plane.png" alt="overseas" onerror="this.style.display=\'none\'" />'
        : "";
      return `
        <tr>
          <td class="r-slot">${i + 1}</td>
          <td class="r-name">${escapeHtml(p.displayName)}${plane}${p.isWk ? ' <span class="wk-tag">WK</span>' : ""}</td>
          <td class="r-role"><span class="role-badge ${r.cls}">${r.label}</span></td>
          <td class="r-ovr ${ovrTierClass(p.ovr)}">${p.ovr}</td>
        </tr>`;
    })
    .join("");
}

// Fill the two head-to-head boxes: your strength vs the opponent's top 3 + avg.
function renderFixtureMatchup(opp) {
  const you = state.teams[0].strength;
  els.userVsStats.innerHTML = `
    <span class="vs-stat"><b>BAT</b> ${you.batting.toFixed(1)}</span>
    <span class="vs-stat"><b>BOWL</b> ${you.bowling.toFixed(1)}</span>
    <span class="vs-stat"><b>OVR</b> ${you.total.toFixed(1)}</span>`;
  els.oppName.textContent = opp.name;
  els.oppVsStats.innerHTML = "";
}

function buildOpponentTeams() {
  // The 9 real franchises that fill the two groups (LSG is replaced by Your XI).
  const needed = new Set(
    [...GROUPS.A, ...GROUPS.B].filter((id) => id !== USER_ID)
  );
  const grouped = {};
  state.players
    .filter((p) => p.season === "2026" && needed.has(p.fr))
    .forEach((p) => {
      (grouped[p.fr] = grouped[p.fr] || []).push(p);
    });
  return Object.entries(grouped).map(([fr, squad]) =>
    makeTeam(fr, squad[0].frFull, squad)
  );
}

function makeTeam(id, name, squad) {
  const players =
    id === USER_ID
      ? [...squad].sort((a, b) => (a.slot ?? 99) - (b.slot ?? 99)).slice(0, 11)
      : selectBalancedXI(squad);
  const strength = teamStrength(players, id === USER_ID);
  // Catch-up buff applied later in initSeason (rank-based, dynamic) — no floor here.
  return {
    id,
    name,
    short: id === USER_ID ? USER_NAME : id,
    players,
    strength,
  };
}

// Dynamic catch-up: rank AI teams by total strength and close part of the gap to
// the strongest team. Bottom 4 close ~45%, mid-table close ~25%. Scales with the
// actual squad gap (no hardcoded floor) and never touches the user's XI.
function applyCatchupBuff(teams) {
  const sorted = [...teams].sort((a, b) => b.strength.total - a.strength.total);
  const top = sorted[0].strength.total;
  sorted.forEach((team, rank) => {
    if (team.id === USER_ID) return;
    const gap = top - team.strength.total;
    const buffFactor = rank >= 6 ? 0.45 : rank >= 4 ? 0.25 : 0;
    if (buffFactor === 0) return;
    const buff = gap * buffFactor;
    team.strength.batting += buff * 0.5;
    team.strength.bowling += buff * 0.5;
    team.strength.total += buff;
  });
}

// Build a position-balanced XI: 2 openers, 4 middle order, 1 finisher/WK,
// 4 bowlers. If a category is short, fill with the best player available.
function selectBalancedXI(squad) {
  const pool = [...squad].sort((a, b) => b.ovr - a.ovr);
  const used = new Set();
  const take = (n, predicate) => {
    const picked = [];
    for (const p of pool) {
      if (picked.length >= n) break;
      if (used.has(p) || !predicate(p)) continue;
      used.add(p);
      picked.push(p);
    }
    return picked;
  };

  const openers = take(2, (p) => p.battingOrder === "Opener");
  const middle = take(4, (p) => p.battingOrder === "Middle Order");
  const finisher = take(1, (p) => p.battingOrder === "Finisher" || p.isWk);
  const bowlers = take(4, (p) => p.primaryRole === "Bowler");
  const result = [...openers, ...middle, ...finisher, ...bowlers];
  result.push(...take(11 - result.length, () => true));
  return result.slice(0, 11);
}

function teamStrength(players, isUser = false) {
  const topSix = players.slice(0, 6);
  const bowlers = [...players]
    .sort((a, b) => b.bowl - a.bowl)
    .slice(0, 5);
  const batting = weightedAverage(topSix.map((p) => p.bat || p.ovr), [1.25, 1.18, 1.1, 1, 0.92, 0.85]);
  const bowling = weightedAverage(bowlers.map((p) => p.bowl || p.ovr), [1.22, 1.12, 1.04, 0.96, 0.88]);
  const depth = average(players.slice(6).map((p) => p.ovr));
  const chemistry = chemistryScore(players);
  let total = batting * 0.46 + bowling * 0.42 + depth * 0.08 + chemistry * 0.04;
  // User XI reality check — a drafted all-time XI faces modern AI opposition.
  // Slight era penalty keeps the title a genuine challenge.
  if (isUser) total *= 0.975;
  return { batting, bowling, depth, chemistry, total };
}

function weightedAverage(values, weights) {
  const totalWeight = weights.slice(0, values.length).reduce((a, b) => a + b, 0);
  return values.reduce((sum, value, i) => sum + value * weights[i], 0) / totalWeight;
}

function average(values) {
  if (!values.length) return 70;
  return values.reduce((a, b) => a + b, 0) / values.length;
}

function chemistryScore(players) {
  const penalties = players.reduce((sum, p, i) => {
    if (p.primaryRole === "Bowler" && i < 7) return sum + 7;
    if (p.battingOrder === "Opener" && i > 2) return sum + 3;
    if (p.battingOrder === "Lower Order" && i < 6) return sum + 3;
    return sum;
  }, 0);
  return Math.max(55, 92 - penalties);
}

// IPL group-stage fixtures (2 groups of 5). Each team plays its 4 group rivals
// twice (home + away = 8), its index-paired cross-group rival twice (2), and the
// other 4 cross-group teams once (4) → 14 matches each, 70 total. Every match is
// really simulated, so table points always total exactly 2 × matches played.
function buildGroupFixtures() {
  const byId = {};
  state.teams.forEach((t) => (byId[t.id] = t));
  const groupA = GROUPS.A.map((id) => byId[id]);
  const groupB = GROUPS.B.map((id) => byId[id]);

  const userMatches = [];
  const aiMatches = [];
  const add = (home, away) => {
    const pair = [home, away];
    if (home.id === USER_ID || away.id === USER_ID) userMatches.push(pair);
    else aiMatches.push(pair);
  };

  // Within each group: double round-robin (home and away).
  [groupA, groupB].forEach((group) => {
    for (let i = 0; i < group.length; i++) {
      for (let j = i + 1; j < group.length; j++) {
        add(group[i], group[j]);
        add(group[j], group[i]);
      }
    }
  });

  // Cross-group: every pair once, plus a reverse-venue second leg for rivals.
  for (let i = 0; i < groupA.length; i++) {
    for (let j = 0; j < groupB.length; j++) {
      add(groupA[i], groupB[j]);
      if (i === j) add(groupB[j], groupA[i]); // index-paired rival, played twice
    }
  }

  return assembleRounds(userMatches, aiMatches);
}

// Spread fixtures into matchdays: each round = 1 user match + a slice of the
// AI-vs-AI games, so the table fills in as the user clicks through their season.
function assembleRounds(userMatches, aiMatches) {
  const users = shuffle(userMatches);
  const ai = shuffle(aiMatches);
  const perRound = Math.ceil(ai.length / users.length);
  const rounds = [];
  let cursor = 0;
  users.forEach((userPair) => {
    const round = [userPair];
    for (let k = 0; k < perRound && cursor < ai.length; k++) {
      round.push(ai[cursor++]);
    }
    rounds.push(round);
  });
  while (cursor < ai.length) rounds[rounds.length - 1].push(ai[cursor++]);
  return rounds;
}

// The user's fixture for a given round (always present in the group format).
function userMatchInRound(round) {
  return round.find((pair) => pair[0].id === USER_ID || pair[1].id === USER_ID);
}

// Show the upcoming fixture (matchup preview, no result yet).
function renderLeagueFixture() {
  updateRecord();
  els.playLeagueBtn.dataset.awaitingNext = "";
  els.fixtureResult.hidden = true;
  els.pitchBadge.hidden = true;
  if (state.roundIndex >= state.rounds.length) {
    leagueComplete();
    return;
  }
  els.fixtureStatus.hidden = true;
  els.vsRow.hidden = false;
  els.playLeagueBtn.textContent = "Simulate";
  const round = state.rounds[state.roundIndex];
  const userPair = userMatchInRound(round);
  els.matchLabel.textContent = `Round ${state.roundIndex + 1} of ${state.rounds.length}`;
  if (userPair) {
    const opp = userPair[0].id === USER_ID ? userPair[1] : userPair[0];
    renderFixtureMatchup(opp);
  }
}

// Show the result of the user match just simulated, with its pitch badge. The
// button becomes "Next Match" — a second click advances to the next fixture.
function renderPlayedFixture(match) {
  if (!match) {
    renderLeagueFixture();
    return;
  }
  updateRecord();
  const userWon = match.winner.id === USER_ID;
  const opp = match.home.id === USER_ID ? match.away : match.home;
  const userScore = match.scoreFor(USER_ID);
  const oppScore = match.scoreFor(opp.id);

  els.matchLabel.textContent = `Round ${state.roundIndex} of ${state.rounds.length}`;
  els.fixtureStatus.hidden = true;
  els.vsRow.hidden = false;
  renderFixtureMatchup(opp);

  els.fixtureResult.hidden = false;
  els.fixtureResult.className = `fixture-result ${userWon ? "is-win" : "is-loss"}`;
  els.fixtureResult.innerHTML = `
    <span class="fr-verdict">${userWon ? "WON" : "LOST"} ${resultMargin(match)}</span>
    <span class="fr-score">${USER_NAME} ${formatScore(userScore)} · ${escapeHtml(opp.short)} ${formatScore(oppScore)}</span>`;

  els.pitchBadge.hidden = true;

  // button is managed by autoSimLeague during the loop
}

function leagueComplete() {
  els.matchLabel.textContent = "League complete";
  els.vsRow.hidden = true;
  els.fixtureResult.hidden = true;
  els.pitchBadge.hidden = true;
  els.fixtureStatus.hidden = false;
  els.fixtureStatus.textContent = `${state.rounds.length} rounds played`;
  els.playLeagueBtn.textContent = "View Table";
  els.playLeagueBtn.disabled = false;
  els.playLeagueBtn.dataset.viewTable = "true";
  els.playLeagueBtn.dataset.awaitingNext = "";
}

// Simulate EVERY match in the current round (user + all AI-vs-AI), so the table
// is built only from real results — points always total 2 × matches played.
function simulateRound() {
  const round = state.rounds[state.roundIndex];
  if (!round) return null;
  let userMatch = null;
  round.forEach(([home, away]) => {
    const match = simulateMatch(home, away, { full: false });
    applyMatchToTable(match);
    updatePlayerStats(match);
    if (home.id === USER_ID || away.id === USER_ID) {
      userMatch = match;
      recordUserResult(match);
      renderScoreBox(match);
    }
  });
  state.roundIndex += 1;
  return userMatch;
}

let autoSimRunning = false;

async function autoSimLeague() {
  if (autoSimRunning) return;
  autoSimRunning = true;
  els.playLeagueBtn.disabled = true;
  els.playLeagueBtn.dataset.awaitingNext = "";
  els.playLeagueBtn.dataset.viewTable = "";

  while (state.roundIndex < state.rounds.length) {
    const userMatch = simulateRound();
    renderPlayedFixture(userMatch);
    // keep button locked during auto-sim
    els.playLeagueBtn.textContent = `Round ${state.roundIndex} of ${state.rounds.length}`;
    els.playLeagueBtn.disabled = true;
    await new Promise((r) => setTimeout(r, 900));
  }

  autoSimRunning = false;
  leagueComplete();
}

function renderScoreBox(match) {
  const userWon = match.winner.id === USER_ID;
  const userScore = match.scoreFor(USER_ID);
  const opp = match.home.id === USER_ID ? match.away : match.home;
  const oppScore = match.scoreFor(opp.id);
  const box = document.createElement("div");
  box.className = `score-box ${userWon ? "is-win" : "is-loss"}`;
  box.innerHTML = `
    <strong>${escapeHtml(opp.short)} · ${formatScore(oppScore)} vs ${USER_NAME} · ${formatScore(userScore)} — ${match.winner.id === USER_ID ? USER_NAME : escapeHtml(opp.short)} won ${resultMargin(match)}</strong>
    <span>Round ${state.roundIndex + 1} · ${escapeHtml(opp.name)}</span>
  `;
  els.leagueResults.prepend(box);
}

function showTableScreen() {
  els.leagueScreen.classList.remove("is-active");
  els.tableScreen.classList.add("is-active");
  els.phasePill.textContent = "Table";
  els.screenTitle.textContent = "League Table";
  renderTable();
  renderLeaders();
  const rank = tableRows().findIndex((row) => row.team.id === USER_ID) + 1;
  if (rank <= 4) {
    els.playoffBtn.hidden = false;
    els.leadersPanel.hidden = false;
    els.seasonEnd.hidden = true;
  } else {
    // Outside top 4 — left panel gets rank/awards, right panel shows result card.
    els.playoffBtn.hidden = true;
    els.leadersPanel.hidden = true;
    els.seasonEnd.hidden = false;

    // Inject elim info at the bottom of the left table panel
    const tablePanel = document.querySelector(".table-panel");
    let elimInfo = tablePanel.querySelector(".elim-left-info");
    if (!elimInfo) {
      elimInfo = document.createElement("div");
      elimInfo.className = "elim-left-info";
      tablePanel.appendChild(elimInfo);
    }
    elimInfo.innerHTML = `
      <p class="season-end-msg">You finished ${ordinal(rank)}. Top 4 qualify.</p>
      <a class="primary-btn wide elim-back-btn" href="draft.html">Back to Draft</a>
    `;

    // Right panel: result card only
    els.seasonEnd.innerHTML = `<div class="result-slot" id="seasonResultSlot"></div>`;
    showResultCard(buildOutcome("ELIMINATED — LEAGUE"), els.seasonEnd.querySelector("#seasonResultSlot"));
    document.body.classList.add("is-endgame");
  }
}

function renderTable() {
  els.leagueTable.innerHTML = tableRows()
    .map((row, i) => `
      <tr class="${row.team.id === USER_ID ? "is-user" : ""}">
        <td>${i + 1}. ${escapeHtml(row.team.name)}</td>
        <td>${row.p}</td>
        <td>${row.w}</td>
        <td>${row.l}</td>
        <td>${nrr(row)}</td>
        <td>${row.pts}</td>
      </tr>
    `)
    .join("");
}

// Sanity caps — values above these are treated as accumulation errors and clamped.
const RUNS_CAP = 950;
const WKTS_CAP = 35;

function runsTier(runs) {
  if (runs >= 700) return "exceptional";
  if (runs >= 550) return "strong";
  if (runs >= 400) return "decent";
  return "average";
}
function wktsTier(wkts) {
  if (wkts >= 28) return "exceptional";
  if (wkts >= 20) return "strong";
  if (wkts >= 14) return "decent";
  return "average";
}
function tierBadge(tier, type) {
  if (tier !== "exceptional") return "";
  return type === "runs" ? ' <span class="tier-badge">🟠</span>' : ' <span class="tier-badge">🟣</span>';
}

function renderLeaders() {
  const leaders = Object.values(state.leaders);
  els.runLeaders.innerHTML = [...leaders]
    .sort((a, b) => b.runs - a.runs)
    .slice(0, 8)
    .map((p) => {
      const r = Math.min(p.runs, RUNS_CAP);
      return `<li>${escapeHtml(p.name)} <span>${p.team} · ${r} runs</span></li>`;
    })
    .join("");
  els.wicketLeaders.innerHTML = [...leaders]
    .sort((a, b) => b.wickets - a.wickets)
    .slice(0, 8)
    .map((p) => {
      const w = Math.min(p.wickets, WKTS_CAP);
      return `<li>${escapeHtml(p.name)} <span>${p.team} · ${w} wickets</span></li>`;
    })
    .join("");
}

// Top scorer + top wicket-taker of the whole tournament (league + playoffs).
function leadersSummaryHtml() {
  const leaders = Object.values(state.leaders);
  if (!leaders.length) return "";
  const topRun = [...leaders].sort((a, b) => b.runs - a.runs)[0];
  const topWk = [...leaders].sort((a, b) => b.wickets - a.wickets)[0];
  if (topRun.runs > 800) console.warn(`Suspicious runs: ${topRun.name} ${topRun.runs}`);
  if (topWk.wickets > 32) console.warn(`Suspicious wickets: ${topWk.name} ${topWk.wickets}`);
  const r = Math.min(topRun.runs, RUNS_CAP);
  const w = Math.min(topWk.wickets, WKTS_CAP);
  return `
    <div class="mini-leaders">
      <div>
        <span>Top Scorer</span>
        <strong>${escapeHtml(topRun.name)}${tierBadge(runsTier(r), "runs")}</strong>
        <em>${escapeHtml(topRun.team)} · ${r} runs</em>
      </div>
      <div>
        <span>Top Wickets</span>
        <strong>${escapeHtml(topWk.name)}${tierBadge(wktsTier(w), "wkts")}</strong>
        <em>${escapeHtml(topWk.team)} · ${w} wkts</em>
      </div>
    </div>
  `;
}

function renderPlayoffLeaders() {
  els.playoffLeaders.innerHTML = "";
}

// IPL-style end-of-season awards: Orange Cap (most runs) + Purple Cap (most wickets).
function awardsHtml() {
  const leaders = Object.values(state.leaders);
  if (!leaders.length) return "";
  const orange = [...leaders].sort((a, b) => b.runs - a.runs)[0];
  const purple = [...leaders].sort((a, b) => b.wickets - a.wickets)[0];
  const r = Math.min(orange.runs, RUNS_CAP);
  const w = Math.min(purple.wickets, WKTS_CAP);
  const rTier = runsTier(r);
  const wTier = wktsTier(w);
  return `
    <div class="cap-awards">
      <div class="cap-award is-orange">
        <img class="cap-img" src="assets/orangecap.png" alt="Orange Cap"
             onerror="this.style.display='none'" />
        <div class="cap-meta">
          <span class="cap-label">Orange Cap</span>
          <strong>${escapeHtml(orange.name)}${tierBadge(rTier, "runs")}</strong>
          <em>${escapeHtml(orange.team)} · ${r} runs</em>
        </div>
      </div>
      <div class="cap-award is-purple">
        <img class="cap-img" src="assets/purplecap.png" alt="Purple Cap"
             onerror="this.style.display='none'" />
        <div class="cap-meta">
          <span class="cap-label">Purple Cap</span>
          <strong>${escapeHtml(purple.name)}${tierBadge(wTier, "wkts")}</strong>
          <em>${escapeHtml(purple.team)} · ${w} wkts</em>
        </div>
      </div>
    </div>
  `;
}

// The user XI's own leading run-scorer and wicket-taker this season.
function userBests() {
  const mine = Object.values(state.leaders).filter((l) => l.team === USER_NAME);
  if (!mine.length) return { bat: null, bowl: null };
  return {
    bat: [...mine].sort((a, b) => b.runs - a.runs)[0],
    bowl: [...mine].sort((a, b) => b.wickets - a.wickets)[0],
  };
}

// ===================== Shareable result card =====================
// Inline brand logos (currentColor so they match the button text on hover).
const X_LOGO = `<svg class="share-ico" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>`;
const WA_LOGO = `<svg class="share-ico" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.71.306 1.263.489 1.694.625.712.227 1.36.195 1.872.118.571-.085 1.758-.719 2.006-1.413.247-.694.247-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.999-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884M20.52 3.449C18.24 1.245 15.24 0 12.045 0 5.463 0 .104 5.359.101 11.892c0 2.096.547 4.142 1.588 5.945L0 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413"/></svg>`;

// Assemble the data the card renders, drawn from the finished season.
function buildOutcome(stage) {
  const rank = tableRows().findIndex((r) => r.team.id === USER_ID) + 1;
  const { bat, bowl } = userBests();
  const you = state.teams[0];
  const diff = (state.config.difficulty || "normal");
  const mode = `${diff.charAt(0).toUpperCase()}${diff.slice(1)} · ${state.config.playerRatings === "prime" ? "Prime" : "Career"}`;
  return {
    wins: state.totalWins,
    losses: state.totalLosses,
    pts: state.totalWins * 2,
    leagueFinish: rank,
    stage,
    mode,
    teamOvr: Math.round(you.strength.total),
    topScorer: bat ? { name: bat.name, runs: bat.runs } : null,
    topWicketer: bowl ? { name: bowl.name, wickets: bowl.wickets } : null,
    xi: you.players.map((p, i) => ({
      name: p.displayName,
      ovr: p.ovr,
      isWk: p.isWk,
      battingOrder: p.battingOrder,
      primaryRole: p.primaryRole,
      slot: i,
    })),
  };
}

// Slot-number badge colour: blue=Opener, green=Middle, gold=WK/Finisher, red=Bowler.
function slotBadgeClass(p) {
  if (p.isWk || p.battingOrder === "Finisher") return "pos-gold";
  if (p.primaryRole === "Bowler" || p.battingOrder === "Lower Order") return "pos-red";
  if (p.battingOrder === "Opener") return "pos-blue";
  return "pos-green"; // Middle Order / All-Rounder
}

function badgeStyle(stage) {
  if (stage.startsWith("CHAMPIONS")) return "badge-gold";
  if (stage.startsWith("RUNNERS")) return "badge-silver";
  return "badge-red";
}

function resultCardHtml(o) {
  const badge = badgeStyle(o.stage);
  const playerRow = (p) => `
    <div class="rc-player">
      <span class="rc-slot ${slotBadgeClass(p)}">${p.slot + 1}</span>
      <span class="rc-pname">${escapeHtml(p.name)}</span>
      <span class="rc-povr ${ovrTierClass(p.ovr)}">${p.ovr}</span>
    </div>`;
  const left = o.xi.slice(0, 6).map(playerRow).join("");
  const right = o.xi.slice(6).map(playerRow).join("");
  return `
    <div class="rc-head">
      <span class="rc-wordmark">16-0</span>
      <span class="rc-tags">
        <span class="rc-tag">${escapeHtml(o.mode)}</span>
        <span class="rc-tag rc-ovr-tag">OVR ${o.teamOvr}</span>
      </span>
    </div>
    <div class="rc-record">${o.wins} - ${o.losses}</div>
    <div class="rc-wl">WON · LOST</div>
    <div class="rc-sub">${o.pts} pts · Finished ${ordinal(o.leagueFinish)}</div>
    <div class="rc-badge-wrap"><span class="rc-badge ${badge}">${escapeHtml(o.stage)}</span></div>
    <div class="rc-xi">
      <div class="rc-col">${left}</div>
      <div class="rc-col">${right}</div>
    </div>
    <div class="rc-leaders">
      <div class="rc-leader">
        <span class="rc-leader-label">Top Scorer</span>
        <strong>${o.topScorer ? escapeHtml(o.topScorer.name) : "—"}</strong>
        <em>${o.topScorer ? `${o.topScorer.runs} runs` : ""}</em>
      </div>
      <div class="rc-leader">
        <span class="rc-leader-label">Top Wkts</span>
        <strong>${o.topWicketer ? escapeHtml(o.topWicketer.name) : "—"}</strong>
        <em>${o.topWicketer ? `${o.topWicketer.wickets} wkts` : ""}</em>
      </div>
    </div>
    <div class="rc-foot">
      <span>Think you can beat this?</span>
      <span class="rc-brand">16-0game.vercel.app</span>
    </div>`;
}

// Render the result card inline in the season-end panel (no modal) so it stays
// on screen and is downloadable. `container` is the slot to drop it into.
function showResultCard(outcome, container) {
  if (!container) return;
  container.innerHTML = `
    <div class="result-card">${resultCardHtml(outcome)}</div>
    <div class="result-actions">
      <button class="primary-btn" type="button" data-act="download">Download</button>
      <button class="primary-btn ghost" type="button" data-act="x">${X_LOGO} Share</button>
    </div>
    <a class="primary-btn wide play-again-btn" href="draft.html" data-act="again">Play Again</a>`;

  const card = container.querySelector(".result-card");
  container.querySelector('[data-act="download"]').onclick = () => {
    html2canvas(card, {
      backgroundColor: "#0f0f0f",
      scale: 2,
      useCORS: true,
    }).then((canvas) => {
      const link = document.createElement("a");
      link.download = `16-0-result-${Date.now()}.png`;
      link.href = canvas.toDataURL("image/png");
      link.click();
    });
  };

  const tweetText = `I went ${outcome.wins}-${outcome.losses} with my drafted IPL XI on 16-0game.vercel.app! ${outcome.stage} Can you beat it?`;
  container.querySelector('[data-act="x"]').onclick = () =>
    window.open(`https://twitter.com/intent/tweet?text=${encodeURIComponent(tweetText)}`, "_blank");
}

function tableRows() {
  return Object.values(state.standings)
    .sort((a, b) => b.pts - a.pts || Number(nrr(b)) - Number(nrr(a)) || b.w - a.w);
}

function nrr(row) {
  const forRate = row.runsFor / Math.max(row.ballsFor / 6, 1);
  const againstRate = row.runsAgainst / Math.max(row.ballsAgainst / 6, 1);
  return (forRate - againstRate).toFixed(3);
}

function updateRecord() {
  const row = state.standings[USER_ID];
  els.recordText.textContent = `${row.w}-${row.l}`;
  els.pointsText.textContent = `${row.pts} pts`;
}

function startPlayoffs() {
  const top4 = tableRows().slice(0, 4).map((row) => row.team);
  // Safety net: the playoffs are only for top-4 finishers. If the user didn't
  // qualify, never enter the knockout flow (it would mislabel them as a finalist).
  if (!top4.some((t) => t.id === USER_ID)) return;
  // Season's done — the card carries the summary, so drop the redundant top hero.
  document.body.classList.add("is-endgame");
  state.playoff = {
    top4,
    stage: top4[0].id === USER_ID || top4[1].id === USER_ID ? "q1" : "eliminator",
    q1Winner: null,
    q1Loser: null,
    eliminatorWinner: null,
    finalistA: null,
  };
  els.tableScreen.classList.remove("is-active");
  els.playoffScreen.classList.add("is-active");
  els.phasePill.textContent = "Playoffs";
  els.screenTitle.textContent = "Playoffs";
  renderPlayoffStage();
}

function renderPlayoffStage() {
  const match = currentPlayoffMatch();
  els.playoffTitle.textContent = PLAYOFF_LABELS[state.playoff.stage];
  els.playoffOutcome.hidden = true;
  els.playoffTeams.textContent = `${match.home.short} vs ${match.away.short}`;
  els.playoffResult.textContent = playoffPromptText(match);
  els.playPlayoffBtn.textContent = `Play ${PLAYOFF_LABELS[state.playoff.stage]}`;
  els.playPlayoffBtn.disabled = false;
  els.playPlayoffBtn.dataset.nextStage = "";
  renderPlayoffLeaders();
  // Reset the scorecard panel to its placeholder until this match is played.
  els.resultBanner.className = "result-banner";
  els.resultBanner.textContent = "Play the match to see the scorecard.";
  els.scorecardGrid.innerHTML = "";
  els.motm.textContent = "";
}

function currentPlayoffMatch() {
  const [first, second, third, fourth] = state.playoff.top4;
  switch (state.playoff.stage) {
    case "q1": return { home: first, away: second };
    case "eliminator": return { home: third, away: fourth };
    case "q2": return { home: state.playoff.q1Loser, away: state.playoff.eliminatorWinner };
    case "final": return { home: state.playoff.finalistA, away: state.playoff.q2Winner };
    default: return { home: first, away: second };
  }
}

function playoffPromptText(match) {
  if (match.home.id === USER_ID || match.away.id === USER_ID) {
    return "No skip. Play the knockout and inspect the full scorecard.";
  }
  return "This non-user knockout will resolve here so your path can continue.";
}

function playPlayoffMatch() {
  const matchTeams = currentPlayoffMatch();
  const match = simulateMatch(matchTeams.home, matchTeams.away, { full: true, knockout: true });
  updatePlayerStats(match);
  recordUserResult(match);
  renderFullScorecard(match);
  advancePlayoff(match);
}

// Tally the user's own W-L (ignores AI-vs-AI games) for the combined record.
function recordUserResult(match) {
  if (match.home.id !== USER_ID && match.away.id !== USER_ID) return;
  if (match.winner.id === USER_ID) state.totalWins += 1;
  else state.totalLosses += 1;
}

function advancePlayoff(match) {
  const stage = state.playoff.stage;
  const winner = match.winner;
  const loser = match.loser;

  if (stage === "q1") {
    state.playoff.q1Winner = winner;
    state.playoff.q1Loser = loser;
    state.playoff.finalistA = winner;
    state.playoff.stage = "eliminator";
  } else if (stage === "eliminator") {
    state.playoff.eliminatorWinner = winner;
    if (loser.id === USER_ID) {
      showUserEliminated("Eliminator");
      return;
    }
    if (!state.playoff.q1Winner) {
      const [first, second] = state.playoff.top4;
      const q1 = simulateMatch(first, second, { full: false, knockout: true });
      state.playoff.q1Winner = q1.winner;
      state.playoff.q1Loser = q1.loser;
      state.playoff.finalistA = q1.winner;
    }
    state.playoff.stage = "q2";
  } else if (stage === "q2") {
    state.playoff.q2Winner = winner;
    if (loser.id === USER_ID) {
      showUserEliminated("Qualifier 2");
      return;
    }
    state.playoff.stage = "final";
  } else if (stage === "final") {
    if (winner.id === USER_ID) {
      endPlayoffs("Champions — the 16-0 dream is real.", "champion");
    } else {
      showUserEliminated("Final");
    }
    return;
  }

  els.playoffResult.textContent = `${match.winner.short} won ${resultMargin(match)}.`;
  els.playPlayoffBtn.textContent = `Next: ${PLAYOFF_LABELS[state.playoff.stage]}`;
  els.playPlayoffBtn.dataset.nextStage = "true";
  renderPlayoffLeaders();
}

function endPlayoffs(text, outcome) {
  els.phasePill.textContent = "Complete";
  els.screenTitle.textContent = "Season Complete";
  const labels = {
    champion: "CHAMPIONS",
    runnerup: "RUNNERS UP",
    eliminated: "ELIMINATED",
  };
  els.playoffOutcome.hidden = false;
  els.playoffOutcome.className = `playoff-outcome is-${outcome}`;
  els.playoffOutcome.textContent = labels[outcome] || "SEASON OVER";
  els.playoffResult.textContent = text;
  els.playoffLeaders.innerHTML = awardsHtml();
  // Swap the single Play button for Back to Draft + Play Again.
  els.playoffActions.innerHTML = `
    <a class="primary-btn" href="draft.html">Back to Draft</a>
    <button class="primary-btn ghost" type="button" id="playAgainBtn">Play Again</button>
  `;
  document.getElementById("playAgainBtn").addEventListener("click", goToDraftFresh);

  const cardStage = outcome === "champion" ? "CHAMPIONS"
    : outcome === "runnerup" ? "RUNNERS-UP" : "ELIMINATED";
  showResultCard(buildOutcome(cardStage), els.resultSlot);
}

// User XI lost a knockout (Eliminator, Q2, or Final) — show a dedicated
// elimination screen with their season record and best performers, plus the
// tournament Orange/Purple cap awards. "Try Again" restarts from the draft.
function showUserEliminated(stageLabel) {
  els.phasePill.textContent = "Eliminated";
  els.screenTitle.textContent = "Season Complete";
  els.playoffTitle.textContent = stageLabel;
  els.playoffTeams.hidden = true;
  els.playoffOutcome.hidden = false;
  els.playoffOutcome.className = "playoff-outcome is-eliminated";
  els.playoffOutcome.textContent = "ELIMINATED";

  // The card below carries record/finish/top performers — keep this to one line.
  els.playoffResult.innerHTML = `
    <p class="elim-stage">You were knocked out in the ${escapeHtml(stageLabel)}.</p>
  `;
  els.playoffLeaders.innerHTML = awardsHtml();

  els.playoffActions.innerHTML = `
    <button class="primary-btn" type="button" id="tryAgainBtn">Try Again</button>
  `;
  document.getElementById("tryAgainBtn").addEventListener("click", goToDraftFresh);

  showResultCard(buildOutcome(`ELIMINATED — ${stageLabel.toUpperCase()}`), els.resultSlot);
}

function goToDraftFresh() {
  try {
    localStorage.removeItem("seasonState");
  } catch (_) {
    /* ignore */
  }
  window.location.href = "draft.html";
}

// Pitch is a property of the match — both innings share it. Low-scoring
// bowling tracks are deliberately rare (10%); most games are flat or neutral.
function getPitchType() {
  const r = Math.random();
  if (r < 0.40) return "batting";
  if (r < 0.90) return "neutral";
  return "bowling"; // only 10% of matches
}

const PITCH_MODS = {
  batting: { runs: +25, wickets: -1.5, srBonus: +18 },
  neutral: { runs: +8, wickets: 0, srBonus: +5 },
  bowling: { runs: -12, wickets: +1.2, srBonus: -8 },
};

function simulateMatch(home, away, options = {}) {
  const pitchType = getPitchType();
  const pitch = PITCH_MODS[pitchType];
  const homeInnings = buildInnings(home, away, { ...options, pitch });
  const chaseTarget = homeInnings.runs + 1;
  const awayInnings = buildInnings(away, home, { ...options, pitch, target: chaseTarget });

  // Match ends the moment the target is reached — cap the chase at exactly target.
  if (awayInnings.runs > homeInnings.runs) {
    awayInnings.runs = chaseTarget;
  } else if (awayInnings.runs === homeInnings.runs) {
    awayInnings.runs += 1;
  }

  const winner = homeInnings.runs > awayInnings.runs ? home : away;
  const loser = winner.id === home.id ? away : home;

  return {
    home,
    away,
    pitchType,
    innings: {
      [home.id]: homeInnings,
      [away.id]: awayInnings,
    },
    winner,
    loser,
    scoreFor: (teamId) => teamId === home.id ? homeInnings : awayInnings,
  };
}

function buildInnings(battingTeam, bowlingTeam, options = {}) {
  const batAdv = battingTeam.strength.batting - bowlingTeam.strength.bowling;
  const totalAdv = battingTeam.strength.total - bowlingTeam.strength.total;
  const pressure = options.knockout ? randomBetween(-14, 14) : randomBetween(-18, 18);
  const pitch = options.pitch || { runs: 0, wickets: 0, srBonus: 0 };
  let projected = 172 + batAdv * 2.4 + totalAdv * 1.6 + pressure + pitch.runs;
  if (options.target) {
    projected = Math.min(projected, options.target + randomBetween(-16, 10));
  }
  const runs = clamp(Math.round(projected), 148, 230);
  const wickets = clamp(Math.round(randomBetween(3, 8) - batAdv / 18 + pitch.wickets), 2, 10);
  const balls = wickets >= 10 ? Math.round(randomBetween(103, 120)) : 120;
  const batting = distributeBatting(battingTeam, bowlingTeam, runs, wickets, balls, options.knockout, pitch.srBonus);
  const bowling = distributeBowling(bowlingTeam, battingTeam, runs, wickets);
  return { runs, wickets, balls, batting, bowling };
}

// A bowler shoved into a top-order batting slot, or a lower-order batter sent
// up high, performs well below their nominal rating.
function penalizedBat(player, slotIndex) {
  let rat = player.bat || player.ovr;
  if (player.primaryRole === "Bowler" && slotIndex < 7) rat *= 0.72;
  if (player.battingOrder === "Lower Order" && slotIndex < 5) rat *= 0.8;
  return rat;
}

// A pure batter pressed into the bowling attack leaks runs and takes fewer wickets.
function penalizedBowl(player) {
  let rat = player.bowl || player.ovr;
  if (player.battingOrder === "Opener" && player.primaryRole !== "All-Rounder") rat *= 0.75;
  if (player.battingOrder === "Middle Order" && player.primaryRole !== "All-Rounder") rat *= 0.85;
  return rat;
}

function distributeBatting(team, opponent, runs, wickets, balls, isKnockout = false, srBonus = 0) {
  // Always use at least 7 batters so middle order gets ball-time even when wickets are low.
  const battersUsed = wickets >= 10 ? 11 : clamp(wickets + 3, 7, 11);
  const activePlayers = team.players.slice(0, battersUsed);

  // Avg bowling quality of the opponent's top-4 bowlers — better attack suppresses all batters.
  const oppBowlAvg = [...opponent.players]
    .sort((a, b) => b.bowl - a.bowl)
    .slice(0, 4)
    .reduce((s, p) => s + (p.bowl || p.ovr), 0) / 4;

  const weights = activePlayers.map((p, i) => {
    // Base: batting rating penalized by position vs opposition bowling matchup.
    const rat = penalizedBat(p, i);
    const matchup = Math.max(5, rat - oppBowlAvg * 0.35);

    // Positional decay: opener gets ~28% share, middle order meaningful but less.
    const posBonus = Math.pow(0.75, i);

    // Wider noise = hero potential on any given day.
    const noise = isKnockout ? randomBetween(0.3, 2.5) : randomBetween(0.4, 2.0);

    // Off day: ~25% chance, including stars.
    const flop = Math.random() < 0.25 ? randomBetween(0.05, 0.18) : 1;

    return Math.max(0.5, matchup * posBonus * noise * flop);
  });

  const rawRuns = splitByWeights(runs, weights);

  // Hero guarantee: if no top-3 batter reached 35 in a 160+ total, boost the best one.
  const top3Max = Math.max(...rawRuns.slice(0, Math.min(3, rawRuns.length)));
  if (runs >= 160 && top3Max < 35) {
    const bestIdx = weights.slice(0, 3).indexOf(Math.max(...weights.slice(0, 3)));
    const boost = 40 - rawRuns[bestIdx];
    rawRuns[bestIdx] += boost;
    const deductFrom = [3, 4, 5].filter((j) => j < rawRuns.length && rawRuns[j] > 5);
    deductFrom.forEach((j) => {
      rawRuns[j] = Math.max(3, rawRuns[j] - Math.floor(boost / deductFrom.length));
    });
  }

  // Cap any single batter at 90 — keeps per-match contribution realistic.
  const INNINGS_CAP = 90;
  rawRuns.forEach((r, i) => {
    if (r > INNINGS_CAP) {
      const excess = r - INNINGS_CAP;
      rawRuns[i] = INNINGS_CAP;
      const others = rawRuns.map((_, j) => j).filter((j) => j !== i);
      const othersTotal = others.reduce((a, j) => a + rawRuns[j], 0);
      others.forEach((j) => {
        if (othersTotal > 0)
          rawRuns[j] += Math.round((excess * rawRuns[j]) / othersTotal);
      });
    }
  });

  // Hero guarantee: on a 150+ total, ensure a standout knock (45+ in knockouts,
  // 35+ in league games), pulled proportionally from the other batters.
  const heroMin = isKnockout ? 45 : 35;
  if (runs >= 150 && Math.max(...rawRuns) < heroMin) {
    const topIdx = weights.indexOf(Math.max(...weights));
    const boost = heroMin - rawRuns[topIdx];
    rawRuns[topIdx] += boost;
    const othersTotal = rawRuns.reduce((a, b, i) => (i === topIdx ? a : a + b), 0);
    rawRuns.forEach((_, i) => {
      if (i !== topIdx && othersTotal > 0)
        rawRuns[i] = Math.max(0, rawRuns[i] - Math.round((boost * rawRuns[i]) / othersTotal));
    });
  }

  // Bug 2: the hero-boost rounding above can drift the total by a few runs, so
  // force the batters' runs to sum to exactly the innings total shown in the header.
  const runDiff = runs - rawRuns.reduce((a, b) => a + b, 0);
  if (runDiff !== 0) {
    const topIdx = rawRuns.indexOf(Math.max(...rawRuns));
    rawRuns[topIdx] = Math.max(0, rawRuns[topIdx] + runDiff);
  }

  // Bug 3: realistic T20 strike rates by batting position (never below 95). The
  // pitch srBonus still nudges this so flat/bowling tracks read differently.
  const rawBalls = activePlayers.map((p, i) => {
    const baseSR =
      i < 2 ? randomBetween(128, 168) :
      i < 5 ? randomBetween(118, 158) :
      i < 7 ? randomBetween(135, 178) :
              randomBetween(100, 138);
    const ratBonus = ((p.bat || p.ovr) - 70) * 0.35;
    const sr = Math.max(95, baseSR + ratBonus + srBonus + randomBetween(-10, 10));
    return Math.max(1, Math.round((rawRuns[i] / sr) * 100));
  });

  // Bug 1: scale per-batter balls so the innings is exactly 120 (20 overs),
  // matching the bowling card's 5 × 4 overs.
  const totalRawBalls = rawBalls.reduce((a, b) => a + b, 0);
  const scaledBalls = rawBalls.map((b) =>
    Math.max(1, Math.round((b * 120) / totalRawBalls))
  );
  // Park the rounding remainder on the batter who faced the most balls — a
  // tail-ender on 1 ball would underflow the floor and leave the total off 120.
  const ballsDiff = 120 - scaledBalls.reduce((a, b) => a + b, 0);
  const topBallsIdx = scaledBalls.indexOf(Math.max(...scaledBalls));
  scaledBalls[topBallsIdx] = Math.max(1, scaledBalls[topBallsIdx] + ballsDiff);

  return team.players.map((p, i) => {
    if (i >= battersUsed) {
      return {
        player: p,
        runs: 0,
        balls: 0,
        out: false,
        didBat: false,
        howOut: "DNB",
      };
    }

    const out = i < wickets;
    return {
      player: p,
      runs: rawRuns[i],
      balls: scaledBalls[i],
      out,
      didBat: true,
      howOut: out ? dismissal(opponent) : "not out",
    };
  });
}

function distributeBowling(team, opponent, runs, wickets) {
  const bowlers = [...team.players]
    .sort((a, b) => b.bowl - a.bowl)
    .slice(0, 5);
  const overs = [4, 4, 4, 4, 4];
  // Run share: lower rating leaks more. ~20% chance a bowler — even a gun — gets
  // taken apart, inflating their share into the odd expensive spell.
  const runWeights = bowlers.map((p) => {
    const base = 110 - penalizedBowl(p);
    const smashed = Math.random() < 0.2 ? randomBetween(1.6, 2.6) : 1;
    return Math.max(1, base * smashed + randomBetween(-10, 10));
  });
  // Wicket share: higher rating takes more, but ~22% chance of a cold spell that
  // yields little or nothing — so a star bowler can finish wicketless.
  const wicketWeights = bowlers.map((p) => {
    const cold = Math.random() < 0.33 ? randomBetween(0.08, 0.35) : 1;
    return Math.max(0.5, penalizedBowl(p) * cold + randomBetween(-8, 8));
  });
  const runSplit = splitByWeights(runs, runWeights);
  const wicketSplit = splitByWeights(wickets, wicketWeights);
  return bowlers.map((p, i) => ({
    player: p,
    overs: overs[i],
    runs: runSplit[i],
    wickets: wicketSplit[i],
    economy: (runSplit[i] / overs[i]).toFixed(2),
  }));
}

// Bug 2/4: largest-remainder split — the returned values sum to EXACTLY `total`,
// so batting cards match the innings header and bowling figures match the total
// conceded. Weights are floored at a tiny positive so a zero weight still places.
function splitByWeights(total, weights) {
  const positive = weights.map((w) => Math.max(0.01, w));
  const sum = positive.reduce((a, b) => a + b, 0);
  const raw = positive.map((w) => Math.floor((w / sum) * total));
  const remainder = total - raw.reduce((a, b) => a + b, 0);
  const fracs = positive
    .map((w, i) => ({ i, f: (w / sum) * total - raw[i] }))
    .sort((a, b) => b.f - a.f);
  for (let k = 0; k < remainder; k++) raw[fracs[k].i]++;
  return raw;
}

function dismissal(opponent) {
  const bowler = [...opponent.players].sort((a, b) => b.bowl - a.bowl)[Math.floor(randomBetween(0, 5))];
  const types = ["c", "b", "lbw", "run out", "st"];
  const type = types[Math.floor(Math.random() * types.length)];
  return `${type} ${bowler.displayName}`;
}

function applyMatchToTable(match) {
  [match.home, match.away].forEach((team) => {
    const row = state.standings[team.id];
    const own = match.scoreFor(team.id);
    const other = match.scoreFor(team.id === match.home.id ? match.away.id : match.home.id);
    row.p += 1;
    row.w += match.winner.id === team.id ? 1 : 0;
    row.l += match.winner.id === team.id ? 0 : 1;
    row.pts += match.winner.id === team.id ? 2 : 0;
    row.runsFor += own.runs;
    row.ballsFor += own.balls;
    row.runsAgainst += other.runs;
    row.ballsAgainst += other.balls;
  });
}

let _matchNum = 0;
function updatePlayerStats(match) {
  _matchNum++;
  // Home team batting (home bats in home innings)
  match.innings[match.home.id].batting.forEach((b) => {
    if (!b.didBat) return;
    if (state.leaders[b.player.id]) state.leaders[b.player.id].runs += b.runs;
  });
  // Away team batting (away bats in away innings)
  match.innings[match.away.id].batting.forEach((b) => {
    if (!b.didBat) return;
    if (state.leaders[b.player.id]) state.leaders[b.player.id].runs += b.runs;
  });
  // Away team bowling (away bowled in home innings)
  match.innings[match.home.id].bowling.forEach((bwl) => {
    if (state.leaders[bwl.player.id]) state.leaders[bwl.player.id].wickets += bwl.wickets;
  });
  // Home team bowling (home bowled in away innings)
  match.innings[match.away.id].bowling.forEach((bwl) => {
    if (state.leaders[bwl.player.id]) state.leaders[bwl.player.id].wickets += bwl.wickets;
  });
  // Debug: log top scorer after every match to detect double-counting
  const top = Object.values(state.leaders).sort((a, b) => b.runs - a.runs)[0];
  if (top) console.log(`Match ${_matchNum} (${match.home.id} v ${match.away.id}): top scorer ${top.name} = ${top.runs} runs`);
  const maxRuns = top ? top.runs : 0;
  if (maxRuns > 800) console.warn(`STATS OVERFLOW: top scorer at ${maxRuns} runs after match ${_matchNum}`);
}

function renderFullScorecard(match) {
  const focusTeam = match.home.id === USER_ID || match.away.id === USER_ID
    ? state.teams.find((team) => team.id === USER_ID)
    : match.home;
  const otherTeam = focusTeam.id === match.home.id ? match.away : match.home;
  const focusScore = match.scoreFor(focusTeam.id);
  const otherScore = match.scoreFor(otherTeam.id);
  const focusWon = match.winner.id === focusTeam.id;
  els.scorecardPanel.hidden = false;
  els.resultBanner.className = `result-banner ${focusWon ? "is-win" : "is-loss"}`;
  els.resultBanner.textContent = `${focusTeam.short}: ${formatScore(focusScore)} vs ${otherTeam.short}: ${formatScore(otherScore)} — ${match.winner.short} won ${resultMargin(match)}`;

  // Show both innings: each block is the batting side + the bowling side that
  // operated against them.
  els.scorecardGrid.innerHTML =
    inningsBlock(match.home, match) + inningsBlock(match.away, match);

  els.motm.textContent = `Man of the Match: ${manOfTheMatch(match)}`;
}

function inningsBlock(battingTeam, match) {
  const innings = match.scoreFor(battingTeam.id);
  const battingRows = innings.batting
    .map((row) => `
      <div class="score-row bat ${row.didBat ? "" : "is-dnb"}">
        <div class="c-name">${escapeHtml(row.player.displayName)}</div>
        <div class="c-dis">${row.didBat ? escapeHtml(row.howOut) : "DNB"}</div>
        <div>${row.didBat ? row.runs : "-"}</div>
        <div>${row.didBat ? row.balls : "-"}</div>
        <div>${row.didBat ? strikeRate(row.runs, row.balls) : "-"}</div>
      </div>
    `)
    .join("");
  const bowlingRows = innings.bowling
    .map((row) => `
      <div class="score-row bowl">
        <div class="c-name">${escapeHtml(row.player.displayName)}</div>
        <div>${row.overs}</div>
        <div>${row.wickets}</div>
        <div>${row.runs}</div>
        <div>${row.economy}</div>
      </div>
    `)
    .join("");
  return `
    <div class="innings-block">
      <h3>${escapeHtml(battingTeam.short)} ${formatScore(innings)}</h3>
      <div class="score-table">
        <div class="score-row bat head">
          <div class="c-name">Batter</div>
          <div class="c-dis">Dismissal</div>
          <div>R</div>
          <div>B</div>
          <div>SR</div>
        </div>
        ${battingRows}
      </div>
      <div class="score-table">
        <div class="score-row bowl head">
          <div class="c-name">Bowler</div>
          <div>O</div>
          <div>W</div>
          <div>R</div>
          <div>Econ</div>
        </div>
        ${bowlingRows}
      </div>
    </div>
  `;
}

function manOfTheMatch(match) {
  const winnerInnings = match.scoreFor(match.winner.id);
  const topBat = [...winnerInnings.batting].sort((a, b) => b.runs - a.runs)[0];
  const topBowl = [...winnerInnings.bowling].sort((a, b) => b.wickets - a.wickets || a.runs - b.runs)[0];
  return topBat.runs >= 55 || topBat.runs > topBowl.wickets * 18
    ? `${topBat.player.displayName} (${topBat.runs})`
    : `${topBowl.player.displayName} (${topBowl.wickets}/${topBowl.runs})`;
}

function resultMargin(match) {
  const chaseInnings = match.scoreFor(match.away.id);
  const defenseInnings = match.scoreFor(match.home.id);
  if (match.winner.id === match.away.id) {
    // chasing team got there — won by wickets still in hand
    return `by ${10 - chaseInnings.wickets} wickets`;
  } else {
    // defending team held on — won by runs
    return `by ${defenseInnings.runs - chaseInnings.runs} runs`;
  }
}

function formatScore(score) {
  return `${score.runs}/${score.wickets}`;
}

function strikeRate(runs, balls) {
  return ((runs / Math.max(balls, 1)) * 100).toFixed(1);
}

function ordinal(n) {
  const suffix = n === 1 ? "st" : n === 2 ? "nd" : n === 3 ? "rd" : "th";
  return `${n}${suffix}`;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function randomBetween(min, max) {
  return min + Math.random() * (max - min);
}

function shuffle(items) {
  return items
    .map((item) => ({ item, sort: Math.random() }))
    .sort((a, b) => a.sort - b.sort)
    .map(({ item }) => item);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}

let toastTimer;
function showToast(message) {
  const toast = document.querySelector(".toast");
  toast.textContent = message;
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 2400);
}

els.playLeagueBtn.addEventListener("click", () => {
  if (els.playLeagueBtn.dataset.backToDraft === "true") {
    window.location.href = "draft.html";
    return;
  }
  if (els.playLeagueBtn.dataset.viewTable === "true") {
    showTableScreen();
    return;
  }
  autoSimLeague();
});
els.playoffBtn.addEventListener("click", startPlayoffs);
els.playPlayoffBtn.addEventListener("click", () => {
  if (els.playPlayoffBtn.dataset.nextStage === "true") {
    renderPlayoffStage();
    return;
  }
  playPlayoffMatch();
});

boot();
