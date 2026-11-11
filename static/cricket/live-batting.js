// Keep the captain's draft stable while match polling continues behind it.
let liveBattingDraft = null;
let liveBattingBusy = false;

function renderLiveBatting() {
  const entry = document.getElementById('live-batting-entry');
  const opts = matchState?.liveBatting;
  const show = matchState?.myRole === 'batting' && matchState?.status !== 'completed';
  entry.classList.toggle('hidden', !show);
  const playback = typeof playbackRunning !== 'undefined' && playbackRunning;
  const quick = document.getElementById('manage-batting-quick-btn');
  quick.classList.toggle('hidden', !show || !opts?.canManage || playback);
  quick.disabled = liveBattingBusy;
  quick.onclick = openLiveBatting;
  document.getElementById('manage-batting-btn').disabled = !opts?.canManage || liveBattingBusy || playback;
  document.getElementById('manage-batting-btn').onclick = openLiveBatting;
  document.getElementById('live-batting-hint').textContent = opts?.message || '';
  const dialog = document.getElementById('live-batting-dialog');
  if (dialog.open && liveBattingDraft) {
    const stale = !opts?.canManage || opts.token !== liveBattingDraft.token || playback;
    if (stale) {
      document.getElementById('live-batting-message').textContent = 'The match moved on. Refresh the choices before making a change.';
    }
    document.getElementById('refresh-batting-btn').classList.toggle('hidden', !stale);
    document.getElementById('refresh-batting-btn').disabled = !opts?.canManage || playback || liveBattingBusy;
    updateLiveBattingButtons(stale);
  }
}

function openLiveBatting() {
  const opts = matchState?.liveBatting;
  if (!opts?.canManage || liveBattingBusy) return;
  liveBattingDraft = JSON.parse(JSON.stringify(opts));
  liveBattingDraft.originalIds = opts.waiting.map(p => String(p.id));
  const label = p => `${p.name} · ${p.runs} (${p.balls})${p.retired ? ' · Resume' : ''}`;
  const fill = (id, players) => {
    const select = document.getElementById(id);
    select.replaceChildren();
    players.forEach(p => select.add(new Option(label(p), String(p.id))));
  };
  fill('retire-batter', opts.atCrease);
  fill('incoming-batter', opts.waiting);
  const dialog = document.getElementById('live-batting-dialog');
  document.getElementById('live-batting-message').textContent = '';
  document.getElementById('refresh-batting-btn').onclick = openLiveBatting;
  document.getElementById('refresh-batting-btn').classList.add('hidden');
  document.getElementById('close-batting-btn').onclick = () => dialog.close();
  document.getElementById('confirm-retire-btn').onclick = () => submitLiveBatting('retire_batsman', {
    outRosterId: document.getElementById('retire-batter').value,
    inRosterId: document.getElementById('incoming-batter').value,
  });
  document.getElementById('save-live-order-btn').onclick = () => submitLiveBatting('live_batting_order', {
    rosterIds: liveBattingDraft.waiting.map(p => p.id),
  });
  dialog.onclose = () => { liveBattingDraft = null; };
  paintLiveBattingOrder();
  if (!dialog.open) dialog.showModal();
}

function liveOrderDirty() {
  return liveBattingDraft && JSON.stringify(liveBattingDraft.originalIds) !==
    JSON.stringify(liveBattingDraft.waiting.map(p => String(p.id)));
}

function updateLiveBattingButtons(stale = false) {
  const blocked = stale || liveBattingBusy;
  document.getElementById('live-batting-fields').disabled = blocked || !liveBattingDraft.waiting.length;
  document.getElementById('confirm-retire-btn').disabled = blocked || liveOrderDirty();
  document.getElementById('save-live-order-btn').disabled = blocked || !liveOrderDirty();
  document.querySelectorAll('#live-batting-order button').forEach(b => {
    b.disabled = blocked || b.dataset.edge === 'true';
  });
}

function paintLiveBattingOrder() {
  const list = document.getElementById('live-batting-order');
  list.replaceChildren();
  liveBattingDraft.waiting.forEach((p, i) => {
    const row = document.createElement('li');
    const name = document.createElement('span');
    name.textContent = `${i + 1}. ${p.name} · ${p.runs} (${p.balls})${p.retired ? ' · Retired, can return' : ''}`;
    row.appendChild(name);
    [-1, 1].forEach(step => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = step < 0 ? '↑' : '↓';
      button.setAttribute('aria-label', `Move ${p.name} ${step < 0 ? 'up' : 'down'}`);
      const other = i + step;
      button.dataset.edge = String(other < 0 || other >= liveBattingDraft.waiting.length);
      button.onclick = () => {
        [liveBattingDraft.waiting[i], liveBattingDraft.waiting[other]] =
          [liveBattingDraft.waiting[other], liveBattingDraft.waiting[i]];
        paintLiveBattingOrder();
      };
      row.appendChild(button);
    });
    list.appendChild(row);
  });
  if (!liveBattingDraft.waiting.length) list.textContent = 'No available batsmen.';
  document.getElementById('live-batting-message').textContent = liveOrderDirty()
    ? 'Save this batting order before retiring a player.' : '';
  updateLiveBattingButtons();
}

async function submitLiveBatting(type, action) {
  if (liveBattingBusy || !liveBattingDraft) return;
  liveBattingBusy = true;
  updateLiveBattingButtons();
  try {
    const res = await matchFetch('/api/match/action', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId, matchId, type,
        action: { ...action, token: liveBattingDraft.token } }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.message || data.error || 'Could not update batting.');
    document.getElementById('live-batting-dialog').close();
    if (data.matchState) applyMatchState(data.matchState, { force: true });
  } catch (err) {
    document.getElementById('live-batting-message').textContent = err.message;
  } finally {
    liveBattingBusy = false;
    renderLiveBatting();
    fetchState();
  }
}
