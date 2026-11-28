(() => {
  const form = document.getElementById('studio-draft');
  if (!form) return;
  const source = form.elements.config;
  const fields = [...form.querySelectorAll('[data-config-path]')];
  const errors = new Map();
  const passEditor = form.querySelector('[data-pass-editor]');
  const milestoneField = form.querySelector('[data-config-path="milestones"]');
  let editingMilestones = false;
  form.querySelector('[data-studio-source]').open = false;
  form.querySelector('[data-studio-fields]').hidden = false;

  function path(field) {
    return field.dataset.configPath.split('.');
  }

  function showError(field, message) {
    field.setCustomValidity(message);
    let note = errors.get(field);
    if (!note && message) {
      note = document.createElement('span');
      note.id = `studio-field-error-${errors.size + 1}`;
      note.className = 'studio-field-error';
      note.setAttribute('role', 'status');
      field.setAttribute('aria-describedby', note.id);
      errors.set(field, note);
      field.after(note);
    }
    if (note) {
      note.textContent = message;
      note.hidden = !message;
    }
    if (message) field.setAttribute('aria-invalid', 'true');
    else field.removeAttribute('aria-invalid');
  }

  // A local wall-clock stamp, which is all a datetime-local input can hold.
  const LOCAL_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/;

  function teamList(config) {
    return Array.isArray(config.teams) ? config.teams : [];
  }

  // Thrown for a problem the operator can read and fix, rather than "invalid JSON".
  function fail(message) {
    const error = new Error(message);
    error.studio = true;
    throw error;
  }

  function display(field, value, config) {
    switch (field.dataset.valueType) {
      case 'team-details':
        return JSON.stringify(teamList(config).map(({players, xi, ...rest}) => rest), null, 2);
      case 'team-squads': {
        // One line per team: a forty-card squad one ID per line is unreadable.
        const rows = teamList(config).map(team =>
          `  ${JSON.stringify(team.key ?? null)}: ${JSON.stringify(team.players ?? [])}`);
        return rows.length ? `{\n${rows.join(',\n')}\n}` : '{}';
      }
      case 'json':
        return JSON.stringify(value ?? null, null, 2);
      case 'datetime': {
        // A picker cannot represent a UTC offset, so an offset-bearing stamp is shown in
        // a text box instead of being silently blanked.
        const text = value ?? '';
        const local = LOCAL_TIME.test(text);
        field.type = !text || local ? 'datetime-local' : 'text';
        return local ? text.slice(0, 16) : text;
      }
      default:
        return value ?? '';
    }
  }

  // Team details and Squads are two views of config.teams, so each merges back into the
  // other's half of a team rather than replacing the list.
  function mergedDetails(config, rows) {
    if (!Array.isArray(rows)) fail('Team details must be a list of team objects.');
    const existing = new Map(teamList(config).map(team => [team.key, team]));
    return rows.map(row => {
      if (!row || typeof row !== 'object' || Array.isArray(row)) fail('Each team must be an object.');
      const old = existing.get(row.key) ?? {};
      const team = {...old, ...row, players: row.players ?? old.players ?? []};
      if (!row.xi && !old.xi) delete team.xi;
      return team;
    });
  }

  function mergedSquads(config, squads) {
    if (!squads || typeof squads !== 'object' || Array.isArray(squads))
      fail('Squads must be an object keyed by team key.');
    const teams = teamList(config);
    const known = new Set(teams.map(team => team.key));
    const unknown = Object.keys(squads).filter(key => !known.has(key));
    if (unknown.length) fail(`Add ${unknown.join(', ')} under Team details first.`);
    return teams.map(team => {
      if (!(team.key in squads)) return team;
      const players = squads[team.key];
      if (!Array.isArray(players)) fail(`${team.key}: the squad must be a list of card IDs.`);
      const next = {...team, players};
      // A changed squad invalidates a fixed XI, as a squad CSV import does.
      if (String(team.players ?? []) !== String(players)) delete next.xi;
      return next;
    });
  }

  function refresh(config, except = null) {
    form.querySelector('[data-kind-boss]').hidden = config.kind !== 'boss';
    form.querySelector('[data-kind-moment]').hidden =
      !(config.kind === 'moment' || config.kind === 'boss' && config.challenge_style === 'moment');
    const tournament = config.kind === 'live' && config.objective_scope === 'tournament';
    form.querySelector('[data-kind-live]').hidden = config.kind !== 'live';
    for (const section of form.querySelectorAll('[data-tournament-objectives]')) section.hidden = !tournament;
    form.querySelector('[data-legacy-objectives]').hidden = tournament;
    form.querySelector('[data-event-budget-label]').textContent = tournament ? 'Total human capacity' : 'Players covered by the budget';
    form.querySelector('[data-auto-objectives]').textContent = tournament ? 'Auto Tournament Objectives' : 'Auto Objectives';
    for (const field of fields) {
      if (field === except || field.getAttribute('aria-invalid') === 'true') continue;
      const value = path(field).reduce((object, key) => object?.[key], config);
      if (field.type === 'checkbox') field.checked = Boolean(value);
      else field.value = display(field, value, config);
    }
    drawMilestones(config, tournament);
  }

  function saveMilestones(rows, rebuild = false) {
    milestoneField.value = JSON.stringify(rows, null, 2);
    editingMilestones = !rebuild;
    milestoneField.dispatchEvent(new Event('input'));
    editingMilestones = false;
  }

  function drawMilestones(config, tournament) {
    if (!passEditor) return;
    passEditor.hidden = !(tournament && config.competition?.event_pass);
    if (passEditor.hidden || editingMilestones) return;
    const body = passEditor.querySelector('[data-pass-rows]');
    const rows = Array.isArray(config.milestones) ? config.milestones : [];
    body.replaceChildren();
    const add = passEditor.querySelector('[data-pass-add]');
    add.disabled = rows.length >= 20;
    add.onclick = () => {
      const next = (rows.at(-1)?.stars || 0) + 1;
      rows.push({id: 'milestone-' + next, stars: next, free: {}, diamond: {}});
      saveMilestones(rows, true);
      body.lastElementChild?.querySelector('input')?.focus();
    };
    for (const [index, row] of rows.entries()) {
      if (!row || typeof row !== 'object' || Array.isArray(row)) continue;
      const item = body.insertRow();
      const cell = item.insertCell();
      function input(parent, label, type, value, change) {
        const wrapper = document.createElement('label');
        wrapper.append(label);
        const control = document.createElement('input');
        control.type = type;
        if (type === 'checkbox') control.checked = Boolean(value);
        else control.value = value ?? '';
        if (type === 'number') control.min = '0';
        wrapper.append(control);
        parent.append(wrapper);
        control.addEventListener('change', () => {
          change(type === 'checkbox' ? control.checked : type === 'number' ? Number(control.value) : control.value);
          saveMilestones(rows);
        });
        return control;
      }
      const id = input(cell, 'Stable ID', 'text', row.id, value => row.id = value);
      id.maxLength = 40;
      const stars = input(cell, 'Claimed stars', 'number', row.stars, value => row.stars = value);
      stars.min = '1'; stars.max = '120';
      for (const tier of ['free', 'diamond']) {
        const box = document.createElement('div');
        box.className = 'studio-pass-bundle';
        item.insertCell().append(box);
        for (const [asset, label] of [['coins', 'Coins'], ['gems', 'Gems'], ['quest_points', 'Quest Points']]) {
          const amount = input(box, label, 'number', row[tier]?.[asset], value => {
            const bundle = row[tier] ??= {};
            if (value) bundle[asset] = value;
            else delete bundle[asset];
          });
          amount.max = '1000000';
        }
      }
      const actions = item.insertCell();
      input(actions, 'Lock', 'checkbox', row.locked, value => row.locked = value);
      input(actions, 'Optional', 'checkbox', row.bonus, value => row.bonus = value);
      const remove = document.createElement('button');
      remove.className = 'btn btn-ghost';
      remove.type = 'button';
      remove.textContent = 'Remove';
      remove.addEventListener('click', () => {
        rows.splice(index, 1);
        saveMilestones(rows, true);
        add.focus();
      });
      actions.append(remove);
    }
  }

  function readSource() {
    const config = JSON.parse(source.value);
    if (!config || Array.isArray(config) || typeof config !== 'object') throw new Error('Invalid configuration');
    return config;
  }

  for (const field of fields) {
    field.addEventListener('input', () => {
      let config;
      let value = field.value;
      try {
        config = readSource();
      } catch (_error) {
        showError(source, 'Correct the JSON in Full configuration before changing a section.');
        source.closest('details').open = true;
        return;
      }
      try {
        const type = field.dataset.valueType;
        if (type === 'team-details' || type === 'team-squads') {
          const parsed = JSON.parse(value);
          config.teams = type === 'team-details'
            ? mergedDetails(config, parsed) : mergedSquads(config, parsed);
        } else {
          if (type === 'json') value = JSON.parse(value);
          else if (type === 'nullable') value = value || null;
          if (field.type === 'number')
            value = type === 'nullable' && field.value === '' ? null : field.valueAsNumber;
          if (field.type === 'checkbox') value = field.checked;
          const keys = path(field);
          const target = keys.slice(0, -1).reduce((object, key) => object[key] ??= {}, config);
          target[keys.at(-1)] = value;
          if (keys[0] === 'kind' && config.kind !== 'live') {
            config.objective_scope = 'fixture';
            config.tournament_objectives = [];
          } else if (keys[0] === 'objective_scope') {
            if (value === 'fixture') config.tournament_objectives = [];
            else if (value === 'tournament') {
              config.objectives = [];
              config.mode = ['tournament_team'];
            }
          }
        }
      } catch (error) {
        showError(field, error?.studio ? error.message
          : 'Enter valid JSON before saving this draft.');
        return;
      }
      showError(field, '');
      source.value = JSON.stringify(config, null, 2);
      refresh(config, field);
    });
  }
  source.addEventListener('input', () => {
    try {
      refresh(readSource());
      showError(source, '');
    } catch (_error) {
      showError(source, 'Enter a valid configuration object before saving.');
    }
  });
  form.addEventListener('invalid', event => {
    const details = event.target.closest('details');
    if (details) details.open = true;
  }, true);
  refresh(readSource());
})();
