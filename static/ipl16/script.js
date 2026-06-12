// ===================== 16-0 — Phase 2: setup screen =====================

const MIN_YEAR = 2008;
const MAX_YEAR = 2026;
const TOTAL_SEASONS = MAX_YEAR - MIN_YEAR + 1; // 19

// Chosen config — read by the wheel in the next phase.
const draftConfig = {
  difficulty: "normal",
  showRatings: "on",
  draftMode: "squad",
  playerRatings: "career",
  eraFrom: MIN_YEAR,
  eraTo: MAX_YEAR,
};

// ---------- screen switching ----------
const body = document.body;
const screens = {
  landing: document.getElementById("landing"),
  setup: document.getElementById("setup"),
};

function showScreen(name) {
  for (const [key, el] of Object.entries(screens)) {
    const active = key === name;
    el.classList.toggle("is-active", active);
    el.setAttribute("aria-hidden", String(!active));
  }
  body.classList.toggle("setup-active", name === "setup");
  if (name === "setup") screens.setup.scrollTop = 0;
}

document
  .querySelector(".start-button")
  .addEventListener("click", () => showScreen("setup"));

document
  .querySelector(".back-btn")
  .addEventListener("click", () => showScreen("landing"));

// ---------- option groups (single-select) ----------
document.querySelectorAll(".group .options").forEach((groupEl) => {
  groupEl.addEventListener("click", (e) => {
    const opt = e.target.closest(".opt");
    if (!opt || opt.classList.contains("is-selected")) return;

    const setting = opt.closest(".group").dataset.setting;
    groupEl.querySelectorAll(".opt").forEach((o) => {
      const on = o === opt;
      o.classList.toggle("is-selected", on);
      o.setAttribute("aria-pressed", String(on));
    });

    const value = opt.dataset.value;
    if (setting === "era") {
      applyEraPreset(value);
    } else {
      draftConfig[setting] = value;
    }
  });
});

// ---------- era dual-range slider ----------
const eraMin = document.getElementById("eraMin");
const eraMax = document.getElementById("eraMax");
const eraFromLbl = document.getElementById("eraFrom");
const eraToLbl = document.getElementById("eraTo");
const eraCountLbl = document.getElementById("eraCount");
const eraFill = document.querySelector(".era-fill");

function updateEra(source) {
  let lo = Number(eraMin.value);
  let hi = Number(eraMax.value);

  // keep the handles from crossing
  if (lo > hi) {
    if (source === "min") {
      lo = hi;
      eraMin.value = String(lo);
    } else {
      hi = lo;
      eraMax.value = String(hi);
    }
  }

  draftConfig.eraFrom = lo;
  draftConfig.eraTo = hi;

  eraFromLbl.textContent = String(lo);
  eraToLbl.textContent = String(hi);
  eraCountLbl.textContent = `${hi - lo + 1} of ${TOTAL_SEASONS} seasons`;

  const leftPct = ((lo - MIN_YEAR) / (TOTAL_SEASONS - 1)) * 100;
  const rightPct = ((hi - MIN_YEAR) / (TOTAL_SEASONS - 1)) * 100;
  eraFill.style.left = `${leftPct}%`;
  eraFill.style.width = `${rightPct - leftPct}%`;
}

function applyEraPreset(value) {
  const from = value === "all" ? MIN_YEAR : Number(value);
  eraMin.value = String(from);
  eraMax.value = String(MAX_YEAR);
  updateEra();
}

function clearEraPreset() {
  document
    .querySelectorAll('.group[data-setting="era"] .opt')
    .forEach((o) => {
      o.classList.remove("is-selected");
      o.setAttribute("aria-pressed", "false");
    });
}

eraMin.addEventListener("input", () => {
  updateEra("min");
  clearEraPreset();
});
eraMax.addEventListener("input", () => {
  updateEra("max");
  clearEraPreset();
});

updateEra(); // initialise readout + fill

// ---------- begin draft (placeholder until the wheel exists) ----------
let toastTimer;
function showToast(message) {
  const toast = document.querySelector(".toast");
  toast.textContent = message;
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 2600);
}

document.getElementById("beginDraft").addEventListener("click", () => {
  try {
    localStorage.setItem("draftConfig", JSON.stringify(draftConfig));
  } catch (_) {
    /* storage may be unavailable — draft page falls back to defaults */
  }
  window.location.href = "draft.html";
});
