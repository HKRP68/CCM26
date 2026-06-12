// ===================== 16-0 — Phase 2: draft (real data) =====================
// Loads the real CSV, spins a tier-weighted franchise+season (slot-machine
// style), shows that squad, one pick per spin, drag to reorder the XI.

// ---------- config from the setup screen ----------
const DEFAULT_CONFIG = {
  difficulty: "normal",
  showRatings: "on",
  draftMode: "squad",
  playerRatings: "career",
  eraFrom: 2008,
  eraTo: 2026,
};
function loadConfig() {
  try {
    const raw = localStorage.getItem("draftConfig");
    return raw ? { ...DEFAULT_CONFIG, ...JSON.parse(raw) } : { ...DEFAULT_CONFIG };
  } catch (_) {
    return { ...DEFAULT_CONFIG };
  }
}
const config = loadConfig();

const DIFFICULTY = {
  easy: { respins: 3, enforceWk: false, label: "Easy" },
  normal: { respins: 1, enforceWk: true, label: "Normal" },
  hard: { respins: 0, enforceWk: true, label: "Hard" },
};
const diff = DIFFICULTY[config.difficulty] || DIFFICULTY.normal;
const isPrime = config.playerRatings === "prime";


// ---------- XI structure ----------
// 11 batting positions. Bands drive auto-placement; drag can override order.
const SLOT_LABELS = [
  "Opener", "Opener",
  "Opener / Middle Order", "Middle Order", "Middle Order", "Middle Order",
  "Finisher / All-Rounder",
  "Bowler / All-Rounder", "Bowler", "Bowler", "Bowler",
];
// Slots (0-indexed): 0,1 Opener · 2 Opener/Mid · 3,4,5 Mid · 6 Finisher · 7-10 Bowler.
// Batting_Order decides WHERE a player bats. Primary_Role only confines specialist
// bowlers — never blocks a batsman/all-rounder from a batting slot.
// Returns eligible slots in auto-placement preference order.
function eligibleSlots(p) {
  let slots;
  if (p.primaryRole === "Bowler") {
    slots = [7, 8, 9, 10]; // specialist bowler: slots 8-11
  } else {
    switch (p.battingOrder) {
      case "Opener": slots = [0, 1, 2]; break; // slots 1,2,3
      case "Middle Order": slots = [2, 3, 4, 5]; break; // slots 3-6
      case "Finisher": slots = [6, 5, 4]; break; // slots 5-7 (prefer 7)
      case "Lower Order": slots = [7, 8, 9, 10, 6]; break; // slots 7-11
      default: slots = [2, 3, 4, 5, 6, 7, 8, 9, 10];
    }
  }
  // Slot 7 (index 6) is the flexible Finisher/WK slot — see canFillSlot7.
  const has6 = slots.includes(6);
  if (canFillSlot7(p) && !has6) slots = [...slots, 6];
  else if (!canFillSlot7(p) && has6) slots = slots.filter((s) => s !== 6);
  return slots;
}

// Slot 7 (index 6): allow finishers, keepers, middle-order bats, and
// all-rounders. Block only openers and pure tail-enders (specialist bowlers
// who bat lower order).
function canFillSlot7(p) {
  if (p.battingOrder === "Opener") return false;
  if (p.primaryRole === "Bowler" && p.battingOrder === "Lower Order") return false;
  return (
    p.battingOrder === "Finisher" ||
    p.battingOrder === "Middle Order" ||
    p.isWk ||
    p.primaryRole === "All-Rounder"
  );
}

function displayRole(p) {
  return p.battingOrder; // Opener / Middle Order / Finisher / Lower Order
}

function roleBadgeClass(p) {
  switch (p.battingOrder) {
    case "Opener": return "role-opener";
    case "Middle Order": return "role-middle";
    case "Finisher": return "role-finisher";
    case "Lower Order": return "role-lower";
    default: return "";
  }
}

// The role that actually drives slot eligibility (for accurate toasts).
function slotRole(p) {
  return p.primaryRole === "Bowler" ? "Bowler" : p.battingOrder;
}

function wrongPosToast(p) {
  const role = slotRole(p);
  const article = /^[aeiou]/i.test(role) ? "an" : "a";
  showToast(`Wrong position — ${playerLabel(p)} is ${article} ${role}, can't bat here`, "error");
}

// ---------- white card icons (PNG line-art from pngs/) ----------
// onerror hides any icon whose file hasn't been added yet (no broken glyphs).
const icon = (src, className = "") =>
  `<img class="ic${className ? ` ${className}` : ""}" src="pngs/${src}" alt="" onerror="this.style.display='none'" />`;
const IC_BAT = icon("bat.png");
const IC_BALL = icon("ball.png");
const IC_ALLROUND = icon("batandball.png", "ic-allround");
const IC_PLANE = icon("plane.png", "ic-plane");

// discipline icon from Primary_Role
function disciplineIcons(p) {
  if (p.primaryRole === "Bowler") return IC_BALL;
  if (p.primaryRole === "All-Rounder") return IC_ALLROUND;
  return IC_BAT; // Batsman / Wicketkeeper
}

// ---------- state ----------
const xi = new Array(SLOT_LABELS.length).fill(null);
let allPlayers = [];
let byTeamSeason = new Map(); // "FR|SEASON" -> [player]
let seasonsByFranchise = {}; // FR -> [season strings]
let fullNames = {}; // FR -> Franchise_Full
let primeByName = {}; // Player_Name -> best OVR across seasons
let mappedNames = {}; // Master_DB_Name -> Impact_CSV_Name
let franchises = [];
let teamStrength = {}; // "FR|SEASON" -> mean of top-5 OVR (the draftable stars)
let spinPool = [];    // flat array of { fr, season, avgOVR } for pickTeam

// Dynamic Probability Engine ("Gambler's Curve"). Reset on each fresh draft page.
const spinState = {
  tier1Hits: 0,
  tier2Hits: 0,
  spinNumber: 0,
};

let pendingSquad = null; // squad currently shown, not yet drafted from
let currentTeam = null; // { fr, season }
let respinsLeft = diff.respins;
let spinning = false;
let dragFrom = null;

// ---------- elements ----------
const spinBtn = document.getElementById("spinBtn");
const rerollBtn = document.getElementById("rerollBtn");
const reelClub = document.getElementById("reelClub");
const reelSeason = document.getElementById("reelSeason");
const spinMeta = document.getElementById("spinMeta");
const squadGrid = document.getElementById("squadGrid");
const xiSlotsEl = document.getElementById("xiSlots");
const pickCountEl = document.getElementById("pickCount");
const overseasCountEl = document.getElementById("overseasCount");
const completeBtn = document.getElementById("completeBtn");
const rulesBar = document.getElementById("rulesBar");
const body = document.body;

// ---------- helpers ----------
function tierClass(ovr) {
  if (ovr >= 92) return "ovr-gold";
  if (ovr >= 89) return "ovr-blue";
  if (ovr >= 85) return "ovr-green";
  return "ovr-white";
}
function ovrOf(p) {
  return isPrime ? primeByName[p.name] ?? p.ovr : p.ovr;
}
function keyStat(p) {
  return p.primaryRole === "Bowler" ? `${p.wkts} wkts` : `${p.runs} runs`;
}
function ratingStats(p) {
  if (p.primaryRole === "Bowler") {
    return `<span class="rat"><b>BWL</b> ${p.bowl}</span>`;
  }
  if (p.primaryRole === "All-Rounder") {
    return `
        <span class="rat"><b>BAT</b> ${p.bat}</span>
        <span class="rat"><b>BWL</b> ${p.bowl}</span>
    `;
  }
  return `<span class="rat"><b>BAT</b> ${p.bat}</span>`;
}
function playerLabel(p) {
  return p.displayName || p.name;
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
function inXi(name) {
  return xi.some((p) => p && p.name === name);
}
function overseasCount() {
  return xi.filter((p) => p && p.isOverseas).length;
}

const MAX_OVERSEAS = 4;

// Can this player be drafted into the current XI right now?
function canDraft(p) {
  if (inXi(p.name)) return false;
  if (p.isOverseas && overseasCount() >= MAX_OVERSEAS) return false;
  return eligibleSlots(p).some((i) => xi[i] === null);
}

let toastTimer;
function showToast(message, kind = "") {
  const toast = document.querySelector(".toast");
  toast.textContent = message;
  toast.className = "toast is-visible" + (kind ? ` is-${kind}` : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 2600);
}

// ---------- load CSVs ----------
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

Promise.all([
  loadCsv("ipl_master_calibrated.csv"),
  loadCsv("mapped_names.csv"),
])
  .then(([playerRows, nameRows]) => {
    buildNameMap(nameRows);
    buildData(playerRows);
    initUI();
  })
  .catch((err) => {
    spinMeta.textContent = "Could not load data — run via a local server.";
    console.error(err);
  });

function buildNameMap(rows) {
  mappedNames = {};
  rows.forEach((r) => {
    const masterName = (r.Master_DB_Name || "").trim();
    const displayName = (r.Impact_CSV_Name || "").trim();
    if (masterName && displayName) mappedNames[masterName] = displayName;
  });
}

function buildData(rows) {
  allPlayers = rows
    .filter((r) => r.Player_Name && r.Franchise && r.Season)
    .map((r) => ({
      name: r.Player_Name,
      displayName: mappedNames[(r.Player_Name || "").trim()] || r.Player_Name,
      season: r.Season,
      fr: r.Franchise,
      frFull: r.Franchise_Full || r.Franchise,
      primaryRole: r.Primary_Role,
      battingOrder: r.Batting_Order,
      isWk: r.Is_Wicketkeeper === "1",
      isOverseas: r.Nationality === "Overseas",
      matches: +r.Matches_Played || 0,
      runs: +r.Batting_Runs || 0,
      sr: +r.Batting_Strike_Rate || 0,
      wkts: +r.Bowling_Wickets || 0,
      econ: +r.Bowling_Economy || 0,
      ovr: +r.OVR || 0,
      bat: +r.Bat_Rat || 0,
      bowl: +r.Bowl_Rat || 0,
    }));

  for (const p of allPlayers) {
    const key = `${p.fr}|${p.season}`;
    if (!byTeamSeason.has(key)) byTeamSeason.set(key, []);
    byTeamSeason.get(key).push(p);
    fullNames[p.fr] = p.frFull;
    primeByName[p.name] = Math.max(primeByName[p.name] ?? 0, p.ovr);
  }

  // seasons per franchise where the squad can field an XI (>= 11 players)
  const seasonsSet = {};
  for (const [key, squad] of byTeamSeason) {
    if (squad.length < 11) continue;
    const [fr, season] = key.split("|");
    (seasonsSet[fr] = seasonsSet[fr] || new Set()).add(season);
  }
  for (const fr of Object.keys(seasonsSet)) {
    seasonsByFranchise[fr] = [...seasonsSet[fr]].sort();
  }
  franchises = Object.keys(seasonsByFranchise);

  // squad strength = mean of the top-5 OVRs (the draftable stars)
  for (const [key, squad] of byTeamSeason) {
    if (squad.length < 11) continue;
    const top = squad
      .map((p) => p.ovr)
      .sort((a, b) => b - a)
      .slice(0, 5);
    teamStrength[key] = top.reduce((a, b) => a + b, 0) / top.length;
  }
  // Build spin pool: one entry per franchise-season. Dynamic tier weights are
  // applied at spin time so previous elite/strong hits can throttle future odds.
  spinPool = [];
  for (const [key, avgOVR] of Object.entries(teamStrength)) {
    const [fr, season] = key.split("|");
    spinPool.push({ fr, season, avgOVR });
  }
}

// ---------- rules bar ----------
function renderRulesBar() {
  const chips = [
    diff.label,
    config.showRatings === "on" ? "Ratings On" : "Blind Draft",
    config.draftMode === "position" ? "Position First" : "Squad First",
    isPrime ? "Prime Mode" : "Career Seasons",
    `Era ${config.eraFrom}–${config.eraTo}`,
  ];
  rulesBar.innerHTML = chips
    .map((c) => `<span class="rule-chip">${c}</span>`)
    .join("");
}

function updateSpinMeta(text) {
  if (text) {
    spinMeta.textContent = text;
    return;
  }
  const picked = xi.filter(Boolean).length;
  if (picked >= SLOT_LABELS.length) {
    spinMeta.textContent = "XI complete — drag to reorder, then finish";
  } else if (pendingSquad) {
    spinMeta.textContent = "Pick one player, or reroll for a new club";
  } else {
    spinMeta.textContent = "Spin for your next pick";
  }
}

// Show Spin before a squad, Reroll (with count) once a squad is on screen.
function updateControls() {
  const picked = xi.filter(Boolean).length;
  const full = picked >= SLOT_LABELS.length;

  if (full) {
    spinBtn.hidden = true;
    rerollBtn.hidden = true;
    return;
  }

  if (pendingSquad) {
    spinBtn.hidden = true;
    rerollBtn.hidden = false;
    if (respinsLeft <= 0) {
      rerollBtn.disabled = true;
      rerollBtn.innerHTML = "No rerolls left";
    } else {
      rerollBtn.disabled = false;
      rerollBtn.innerHTML = `Reroll · <span id="rerollLeft">${respinsLeft}</span> left`;
    }
  } else {
    spinBtn.hidden = false;
    rerollBtn.hidden = true;
  }
}

// ---------- dynamic probability franchise+season pick ----------
function getTeamTier(avgOVR) {
  if (avgOVR >= 84) return 1;
  if (avgOVR >= 80) return 2;
  if (avgOVR >= 76) return 3;
  return 4;
}

function getSpinWeights(state) {
  let w1 = 42, w2 = 33, w3 = 20, w4 = 5;

  const t1Penalty = Math.max(0, state.tier1Hits - 1) * 5;
  w1 = Math.max(30, w1 - t1Penalty);

  const t2Penalty = Math.max(0, state.tier2Hits - 2) * 2;
  w2 = Math.max(26, w2 - t2Penalty);

  const lost = t1Penalty + t2Penalty;
  w3 += lost * 0.75;
  w4 += lost * 0.25;

  return { w1, w2, w3, w4 };
}

function tierWeight(tier, weights) {
  if (tier === 1) return weights.w1;
  if (tier === 2) return weights.w2;
  if (tier === 3) return weights.w3;
  return weights.w4;
}

function weightedPick(items, weightOf) {
  const total = items.reduce((a, it) => a + weightOf(it), 0);
  let r = Math.random() * total;
  for (const it of items) {
    r -= weightOf(it);
    if (r <= 0) return it;
  }
  return items[items.length - 1];
}

function pickTeam() {
  const from = config.eraFrom;
  const to = config.eraTo;

  const valid = spinPool.filter((e) => +e.season >= from && +e.season <= to);
  if (!valid.length) return spinPool[Math.floor(Math.random() * spinPool.length)];

  const weights = getSpinWeights(spinState);
  const tierCounts = valid.reduce((counts, entry) => {
    const tier = getTeamTier(entry.avgOVR);
    counts[tier] = (counts[tier] || 0) + 1;
    return counts;
  }, {});

  const entry = weightedPick(valid, (e) => {
    const tier = getTeamTier(e.avgOVR);
    return tierWeight(tier, weights) / (tierCounts[tier] || 1);
  });

  const tier = getTeamTier(entry.avgOVR);
  if (tier === 1) spinState.tier1Hits++;
  if (tier === 2) spinState.tier2Hits++;
  spinState.spinNumber++;

  return { fr: entry.fr, season: entry.season };
}

// ---------- spin (slot-machine reels) ----------
// Shrink the text until the whole value fits on one line in the reel.
function fitReelText(item, box) {
  let size = parseFloat(getComputedStyle(item).fontSize);
  let guard = 0;
  while (item.scrollWidth > box.clientWidth && size > 9 && guard < 60) {
    size -= 1;
    item.style.fontSize = `${size}px`;
    guard += 1;
  }
}

// Render a single static value filling the reel window.
// If the text overflows, apply a slow horizontal marquee so it reads fully.
function setReel(box, value) {
  const h = box.clientHeight;
  const item = document.createElement("div");
  item.className = "reel-item";
  item.style.height = `${h}px`;
  item.textContent = value;
  box.innerHTML = "";
  box.appendChild(item);
  requestAnimationFrame(() => {
    const overflow = item.scrollWidth - box.clientWidth;
    if (overflow > 2) {
      item.style.setProperty("--scroll-dist", `-${overflow + 8}px`);
      item.classList.add("is-marquee");
    }
  });
}

// Build a tall strip of random values ending on finalValue, then scroll to it.
function rollReel(box, pool, finalValue, duration) {
  return new Promise((resolve) => {
    const itemH = box.clientHeight;
    const spinCount = 28;
    const values = [];
    for (let i = 0; i < spinCount; i++) {
      values.push(pool[Math.floor(Math.random() * pool.length)]);
    }
    values.push(finalValue);

    const strip = document.createElement("div");
    strip.className = "reel-strip";
    strip.innerHTML = values
      .map((v) => `<div class="reel-item" style="height:${itemH}px">${v}</div>`)
      .join("");
    box.innerHTML = "";
    box.appendChild(strip);

    const distance = itemH * (values.length - 1);
    // two frames so the browser registers the start position before transitioning
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        strip.style.transition = `transform ${duration}ms cubic-bezier(0.16, 0.85, 0.2, 1)`;
        strip.style.transform = `translateY(${-distance}px)`;
      });
    });

    setTimeout(() => {
      setReel(box, finalValue); // settle to a clean static value
      resolve();
    }, duration + 40);
  });
}

async function doSpin() {
  if (spinning) return;
  const picked = xi.filter(Boolean).length;
  if (picked >= SLOT_LABELS.length) {
    showToast("Your XI is already full", "error");
    return;
  }
  // re-spin only costs when rejecting a squad you haven't drafted from
  if (pendingSquad) {
    if (respinsLeft <= 0) {
      showToast(`No re-spins left (${diff.label})`, "error");
      return;
    }
    respinsLeft -= 1;
  }

  spinning = true;
  spinBtn.disabled = true;
  squadGrid.innerHTML = "";
  pendingSquad = null;
  reelClub.classList.remove("spent");
  reelSeason.classList.remove("spent");
  updateSpinMeta("Spinning…");

  const wkFilled = xi.slice(0, 7).some((p) => p && p.isWk);

  // After pick 8 with no WK yet — warn
  if (picked === 8 && !wkFilled && diff.enforceWk) {
    showToast("No wicketkeeper yet — pick one soon or you can't finish", "error");
  }

  // After pick 9 with no WK — force spin pool to squads that have a draftable WK
  if (picked >= 9 && !wkFilled && diff.enforceWk) {
    const from = config.eraFrom;
    const to = config.eraTo;
    let wkPool = spinPool.filter((e) => {
      if (+e.season < from || +e.season > to) return false;
      const squad = byTeamSeason.get(`${e.fr}|${e.season}`) || [];
      return squad.some((p) => p.isWk);
    });
    if (!wkPool.length) {
      showToast("Relaxing era filter to find a wicketkeeper", "error");
      wkPool = spinPool.filter((e) => {
        const squad = byTeamSeason.get(`${e.fr}|${e.season}`) || [];
        return squad.some((p) => p.isWk);
      });
    }
    const wkEntry = wkPool[Math.floor(Math.random() * wkPool.length)];
    const clubPool = franchises.map((fr) => fullNames[fr]);
    const seasonPool = seasonsByFranchise[wkEntry.fr];
    await Promise.all([
      rollReel(reelClub, clubPool, fullNames[wkEntry.fr], 2200),
      rollReel(reelSeason, seasonPool, wkEntry.season, 2900),
    ]);
    currentTeam = { fr: wkEntry.fr, season: wkEntry.season };
    pendingSquad = (byTeamSeason.get(`${wkEntry.fr}|${wkEntry.season}`) || [])
      .slice()
      .sort((a, b) => ovrOf(b) - ovrOf(a));
    renderSquad();
    spinning = false;
    spinBtn.disabled = false;
    updateControls();
    updateSpinMeta();
    return;
  }

  const target = pickTeam();
  const clubPool2 = franchises.map((fr) => fullNames[fr]);
  const seasonPool2 = seasonsByFranchise[target.fr];

  await Promise.all([
    rollReel(reelClub, clubPool2, fullNames[target.fr], 2200),
    rollReel(reelSeason, seasonPool2, target.season, 2900),
  ]);

  currentTeam = target;
  pendingSquad = (byTeamSeason.get(`${target.fr}|${target.season}`) || [])
    .slice()
    .sort((a, b) => ovrOf(b) - ovrOf(a));

  renderSquad();
  spinning = false;
  spinBtn.disabled = false;
  updateControls();
  updateSpinMeta();
}

spinBtn.addEventListener("click", doSpin);
rerollBtn.addEventListener("click", doSpin);

// ---------- squad render ----------
function renderSquad() {
  squadGrid.innerHTML = "";
  if (!pendingSquad) return;

  // pickable players first (by OVR); unpickable ones greyed at the bottom
  const ordered = [...pendingSquad].sort((a, b) => {
    const ca = canDraft(a);
    const cb = canDraft(b);
    if (ca !== cb) return ca ? -1 : 1;
    return ovrOf(b) - ovrOf(a);
  });

  ordered.forEach((p) => {
    const ovr = ovrOf(p);
    const blocked = !canDraft(p);
    const name = escapeHtml(playerLabel(p));
    const card = document.createElement("button");
    card.type = "button";
    card.className = "player-card" + (blocked ? " is-blocked" : "");
    card.innerHTML = `
      <div class="card-top">
        <span class="card-name">${name}</span>
        <span class="ovr ${tierClass(ovr)}">${ovr}</span>
      </div>
      <div class="card-meta">
        <span class="role-badge ${roleBadgeClass(p)}">${displayRole(p)}</span>
        <span class="ic-group">${disciplineIcons(p)}${p.isOverseas ? IC_PLANE : ""}</span>
        ${p.isWk ? '<span class="wk-badge">WK</span>' : ""}
      </div>
      <div class="card-stats">
        ${ratingStats(p)}
        <span class="key-stat" style="margin-left:auto">${keyStat(p)}</span>
      </div>
    `;
    if (!blocked) card.addEventListener("click", () => draftPlayer(p));
    squadGrid.appendChild(card);
  });
}

// ---------- draft (one pick per spin) ----------
function draftPlayer(p) {
  if (!pendingSquad || !p) return;

  if (inXi(p.name)) {
    showToast(`${playerLabel(p)} is already in your XI`, "error");
    return;
  }
  if (p.isOverseas && overseasCount() >= MAX_OVERSEAS) {
    showToast(`Max ${MAX_OVERSEAS} overseas players in an XI`, "error");
    return;
  }

  const slot = eligibleSlots(p).find((i) => xi[i] === null);
  if (slot === undefined) {
    if (xi.every((x) => x !== null)) {
      showToast("Your XI is already full", "error");
    } else {
      showToast(`No open ${slotRole(p)} position left`, "error");
    }
    return;
  }

  xi[slot] = p;
  // pick is locked in — one pick per team, clear the squad, must spin again
  pendingSquad = null;
  squadGrid.innerHTML = "";
  reelClub.classList.add("spent");
  reelSeason.classList.add("spent");

  renderXI();
  updateControls();
  updateSpinMeta();
}

// ---------- XI sidebar + drag reorder ----------
function renderXI() {
  xiSlotsEl.innerHTML = "";

  SLOT_LABELS.forEach((label, i) => {
    const p = xi[i];
    const li = document.createElement("li");
    li.className = "xi-slot" + (p ? " is-filled" : "");
    li.dataset.idx = String(i);

    if (p) {
      const ovr = ovrOf(p);
      const name = escapeHtml(playerLabel(p));
      const origin = escapeHtml(`${p.fr} ${p.season}`);
      li.draggable = true;
      li.innerHTML = `
        <span class="slot-num">${i + 1}</span>
        <span class="slot-body">
          <span class="slot-role">${label}</span>
          <span class="slot-player-row">
            <span class="slot-player">${name}${p.isWk ? " (wk)" : ""}</span>
            <span class="slot-icons">${disciplineIcons(p)}</span>
          </span>
          <span class="slot-origin">${origin}</span>
        </span>
        <span class="slot-ovr ${tierClass(ovr)}">${ovr}</span>
      `;
    } else {
      li.innerHTML = `
        <span class="slot-num">${i + 1}</span>
        <span class="slot-body">
          <span class="slot-role">${label}</span>
          <span class="slot-player" style="color:#5f5f5f">Empty</span>
        </span>
      `;
    }

    addDragHandlers(li, i);
    xiSlotsEl.appendChild(li);
  });

  const picks = xi.filter(Boolean);
  const picked = picks.length;
  pickCountEl.textContent = String(picked);
  overseasCountEl.textContent = String(overseasCount());
  completeBtn.hidden = picked !== SLOT_LABELS.length;

  const ovrDisplay = document.getElementById("xiOvrDisplay");
  if (picked > 0 && config.showRatings !== "off") {
    const avg = Math.round(picks.reduce((a, p) => a + ovrOf(p), 0) / picked);
    const topBat = [...picks].sort((a, b) => (b.bat || b.ovr) - (a.bat || a.ovr)).slice(0, Math.min(6, picked));
    const topBowl = [...picks].sort((a, b) => (b.bowl || b.ovr) - (a.bowl || a.ovr)).slice(0, Math.min(5, picked));
    const bat = Math.round(topBat.reduce((a, p) => a + (p.bat || p.ovr), 0) / topBat.length);
    const bowl = Math.round(topBowl.reduce((a, p) => a + (p.bowl || p.ovr), 0) / topBowl.length);

    const batVal = document.getElementById("xiBatVal");
    const bowlVal = document.getElementById("xiBowlVal");
    const ovrVal = document.getElementById("xiOvrVal");
    batVal.textContent = bat;   batVal.className = "xi-ovr-val " + tierClass(bat);
    bowlVal.textContent = bowl; bowlVal.className = "xi-ovr-val " + tierClass(bowl);
    ovrVal.textContent = avg;   ovrVal.className = "xi-ovr-val " + tierClass(avg);
    ovrDisplay.hidden = false;
  } else {
    ovrDisplay.hidden = true;
  }
}

function addDragHandlers(li, index) {
  li.addEventListener("dragstart", () => {
    dragFrom = index;
    li.classList.add("is-dragging");
  });
  li.addEventListener("dragend", () => {
    dragFrom = null;
    li.classList.remove("is-dragging");
    xiSlotsEl
      .querySelectorAll(".drag-over")
      .forEach((el) => el.classList.remove("drag-over"));
  });
  li.addEventListener("dragover", (e) => {
    if (dragFrom === null || dragFrom === index) return;
    e.preventDefault();
    li.classList.add("drag-over");
  });
  li.addEventListener("dragleave", () => li.classList.remove("drag-over"));
  li.addEventListener("drop", (e) => {
    e.preventDefault();
    li.classList.remove("drag-over");
    if (dragFrom === null || dragFrom === index) return;

    const moving = xi[dragFrom];
    const displaced = xi[index]; // may be null (move into empty slot)

    // moved player must be allowed in the target slot
    if (!eligibleSlots(moving).includes(index)) {
      wrongPosToast(moving);
      return;
    }
    // on a swap, the displaced player must be allowed in the vacated slot
    if (displaced && !eligibleSlots(displaced).includes(dragFrom)) {
      wrongPosToast(displaced);
      return;
    }

    xi[index] = moving;
    xi[dragFrom] = displaced;
    renderXI();
    updateSpinMeta();
  });
}

// ---------- complete ----------
completeBtn.addEventListener("click", () => {
  if (diff.enforceWk) {
    const wkInTop7 = xi.slice(0, 7).some((p) => p && p.isWk);
    if (!wkInTop7) {
      showToast("Your XI needs a wicketkeeper in the top 7", "error");
      return;
    }
  }
  try {
    localStorage.setItem(
      "seasonState",
      JSON.stringify({
        config,
        xi: xi.map((p, slot) => ({
          ...p,
          slot,
          simOvr: ovrOf(p),
        })),
        createdAt: Date.now(),
      })
    );
  } catch (_) {
    showToast("Could not save XI for simulation", "error");
    return;
  }
  window.location.href = "simulation.html";
});

// ---------- init ----------
function initUI() {
  if (config.showRatings === "off") body.classList.add("hide-ratings");
  setReel(reelClub, "—");
  setReel(reelSeason, "—");
  renderRulesBar();
  renderXI();
  updateControls();
  updateSpinMeta();
}
