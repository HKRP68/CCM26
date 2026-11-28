/* Event Studio draft editor.
 *
 * The hidden "config" textarea is the one source of truth: every control reads from
 * it and writes back into it, and the form posts that JSON. Controls come in two
 * kinds. A [data-config-path] field binds to one value by path. An editor draws a
 * table of rows from a list or object in the config, and is redrawn only when rows
 * are added or removed, so typing in a cell is never interrupted.
 */
(() => {
  const form = document.getElementById('studio-draft');
  if (!form) return;
  const source = form.elements.config;
  const fields = [...form.querySelectorAll('[data-config-path]')];
  const stepField = form.querySelector('[data-studio-step]');
  const errors = new Map();
  // Editors suspended because the operator is mid-edit in one of their cells.
  const settled = new Set();

  form.querySelector('[data-studio-source]').open = false;
  form.querySelector('[data-studio-fields]').hidden = false;

  const cards = new Map(Object.entries(
    JSON.parse(form.querySelector('[data-studio-cards]')?.textContent || '{}'))
    .map(([id, card]) => [Number(id), card]));

  const options = JSON.parse(form.querySelector('[data-studio-options]')?.textContent || '{}');
  const PITCHES = options.pitches ?? [];
  const WEATHERS = options.weathers ?? [];
  const METRICS = options.metrics ?? [];

  function path(field) {
    return field.dataset.configPath.split('.');
  }

  function at(config, keys) {
    return keys.reduce((object, key) => object?.[key], config);
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

  function fixtureList(config) {
    return Array.isArray(config.fixtures) ? config.fixtures : [];
  }

  // Thrown for a problem the operator can read and fix, rather than "invalid JSON".
  function fail(message) {
    const error = new Error(message);
    error.studio = true;
    throw error;
  }

  function readSource() {
    const config = JSON.parse(source.value);
    if (!config || Array.isArray(config) || typeof config !== 'object') throw new Error('Invalid configuration');
    return config;
  }

  /* Write a changed config back to the source of truth and redraw.
   * `editing` names an editor whose rows must not be rebuilt under the cursor. */
  function commit(config, {editing = null, except = null} = {}) {
    if (editing) settled.add(editing);
    source.value = JSON.stringify(config, null, 2);
    refresh(config, except);
    settled.delete(editing);
  }

  /* One labelled control inside an editor cell. Changes land on `change`, not
   * `input`, so a row is only written back once the value is settled. */
  function cell(parent, label, type, value, apply, {min, max, step, maxLength, options} = {}) {
    const wrapper = document.createElement('label');
    if (label) wrapper.append(label);
    const control = document.createElement(options ? 'select' : 'input');
    if (options) {
      for (const option of options) {
        const item = document.createElement('option');
        const [key, title] = Array.isArray(option) ? option : [option, option];
        item.value = key;
        item.textContent = title;
        control.append(item);
      }
      control.value = value ?? '';
    } else {
      control.type = type;
      if (type === 'checkbox') control.checked = Boolean(value);
      else control.value = value ?? '';
      if (min !== undefined) control.min = String(min);
      if (max !== undefined) control.max = String(max);
      if (step !== undefined) control.step = String(step);
      if (maxLength !== undefined) control.maxLength = maxLength;
    }
    wrapper.append(control);
    parent.append(wrapper);
    control.addEventListener('change', () => apply(
      type === 'checkbox' ? control.checked
        : type === 'number' ? (control.value === '' ? null : Number(control.value))
        : control.value));
    return control;
  }

  function button(parent, label, click, {primary = false} = {}) {
    const control = document.createElement('button');
    control.type = 'button';
    control.className = primary ? 'btn btn-primary' : 'btn btn-ghost';
    control.textContent = label;
    control.addEventListener('click', click);
    parent.append(control);
    return control;
  }

  /* ---- Step navigation -------------------------------------------------- */

  const steps = [...form.querySelectorAll('[data-step]')].map(section => section.dataset.step);
  const navButtons = [...form.querySelectorAll('[data-step-to]')];
  const progress = form.querySelector('[data-studio-progress]');
  const progressText = form.querySelector('[data-studio-progress-text]');
  const visited = new Set();
  // Remembering the step survives the save round trip when the server does not echo it.
  const stepKey = `studio-step:${form.querySelector('[name="revision"]')?.value ?? ''}:${location.pathname}`;
  let current = steps.includes(stepField?.value) ? stepField.value : steps[0];

  function show(step, {focus = false} = {}) {
    if (!steps.includes(step)) return;
    current = step;
    visited.add(step);
    if (stepField) stepField.value = step;
    for (const section of form.querySelectorAll('[data-step]')) {
      section.hidden = section.dataset.step !== step;
    }
    for (const control of navButtons) {
      const target = control.dataset.stepTo;
      // Only the nav marks the current step; Back and Next are plain controls.
      if (control.closest('.studio-steps')) {
        if (target === step) control.setAttribute('aria-current', 'step');
        else control.removeAttribute('aria-current');
        control.toggleAttribute('data-visited', visited.has(target));
      }
    }
    const index = steps.indexOf(step);
    if (progress) progress.value = index + 1;
    if (progressText) progressText.textContent = `Step ${index + 1} of ${steps.length}`;
    if (focus) {
      const heading = form.querySelector(`[data-step="${step}"] h3`);
      if (heading) {
        heading.tabIndex = -1;
        heading.focus();
      }
    }
    try {
      sessionStorage.setItem(stepKey, step);
    } catch (_error) {
      // A browser refusing storage is no reason to stop editing.
    }
  }

  for (const control of navButtons) {
    control.addEventListener('click', () => show(control.dataset.stepTo, {focus: true}));
  }

  /* Mark in the nav any step holding a control the browser considers invalid, so a
   * refused save says where the problem is rather than only that there is one. */
  function markInvalidSteps() {
    for (const control of navButtons) {
      if (!control.closest('.studio-steps')) continue;
      const section = form.querySelector(`[data-step="${control.dataset.stepTo}"]`);
      const broken = section && [...section.querySelectorAll(':invalid, [aria-invalid="true"]')].length > 0;
      control.toggleAttribute('data-invalid', Boolean(broken));
    }
  }

  // A control the browser refuses sits on a hidden step until we open it.
  form.addEventListener('invalid', event => {
    const section = event.target.closest('[data-step]');
    if (section && section.dataset.step !== current) show(section.dataset.step);
    const details = event.target.closest('details');
    if (details) details.open = true;
    markInvalidSteps();
  }, true);

  /* ---- Teams and squads ------------------------------------------------- */

  const teamsRoot = form.querySelector('[data-teams-editor]');
  const teamCount = form.querySelector('[data-team-count]');
  const MAX_TEAMS = 16;
  const MAX_SQUAD = 40;

  function teamKey(config, name) {
    const used = new Set(teamList(config).map(team => team.key));
    const base = (name || 'team').toLowerCase().replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '').slice(0, 30) || 'team';
    let key = base;
    let index = 2;
    while (used.has(key)) key = `${base}-${index++}`;
    return key;
  }

  function describe(id) {
    const card = cards.get(id);
    if (!card) return `Card ${id}`;
    return `${card.name} · ${card.version} · ${card.rating} OVR`;
  }

  function drawTeams(config) {
    if (!teamsRoot) return;
    const teams = teamList(config);
    if (teamCount) {
      teamCount.textContent = teams.length
        ? `${teams.length} team${teams.length === 1 ? '' : 's'}` : '';
    }
    const add = form.querySelector('[data-team-add]');
    if (add) add.disabled = teams.length >= MAX_TEAMS;
    if (settled.has('teams')) return;
    teamsRoot.replaceChildren();
    if (!teams.length) {
      const empty = document.createElement('p');
      empty.textContent = 'No teams yet. Import a Challenge League above, or add one by hand.';
      teamsRoot.append(empty);
      return;
    }
    for (const [index, team] of teams.entries()) {
      if (!team || typeof team !== 'object' || Array.isArray(team)) continue;
      const box = document.createElement('details');
      box.className = 'studio-panel';
      box.open = teams.length <= 4;
      const title = document.createElement('summary');
      const squad = Array.isArray(team.players) ? team.players : [];
      title.textContent = `${team.name || team.key || 'Unnamed team'} — ${squad.length} player${squad.length === 1 ? '' : 's'}`;
      box.append(title);

      const identity = document.createElement('div');
      identity.className = 'studio-grid';
      box.append(identity);
      const save = (editing = 'teams') => commit(config, {editing});
      cell(identity, 'Team name', 'text', team.name, value => {
        team.name = value;
        save();
      }, {maxLength: 60});
      cell(identity, 'Short name', 'text', team.short_name, value => {
        team.short_name = value;
        save();
      }, {maxLength: 8});
      cell(identity, 'Logo URL', 'url', team.logo_url, value => {
        team.logo_url = value;
        save();
      }, {maxLength: 500});
      cell(identity, 'Primary colour', 'color', team.primary_color || '#1d4ed8', value => {
        team.primary_color = value;
        save();
      });
      cell(identity, 'Secondary colour', 'color', team.secondary_color || '#d1ab3e', value => {
        team.secondary_color = value;
        save();
      });
      cell(identity, 'Home stadium', 'text', team.home_venue, value => {
        team.home_venue = value;
        save();
      }, {maxLength: 80});

      const roster = document.createElement('div');
      box.append(roster);
      const heading = document.createElement('h5');
      heading.textContent = `Squad (${squad.length} of ${MAX_SQUAD})`;
      roster.append(heading);
      const list = document.createElement('ol');
      roster.append(list);
      for (const [slot, id] of squad.entries()) {
        const row = document.createElement('li');
        const label = document.createElement('span');
        label.textContent = describe(id);
        if (cards.get(id)?.retired) {
          label.textContent += ' · not selectable';
          label.className = 'studio-field-error';
        }
        row.append(label);
        button(row, 'Remove', () => {
          squad.splice(slot, 1);
          delete team.xi;  // A changed squad invalidates a fixed XI.
          team.players = squad;
          commit(config);
        });
        list.append(row);
      }
      picker(roster, config, team, squad);

      const actions = document.createElement('div');
      actions.className = 'studio-toolbar';
      box.append(actions);
      button(actions, 'Remove team', () => {
        config.teams = teams.filter((_, position) => position !== index);
        commit(config);
      });
      teamsRoot.append(box);
    }
  }

  /* Search active master cards by name and add one to a squad. The config stores
   * card IDs; the names shown come from this search and the draft's own card map. */
  function picker(parent, config, team, squad) {
    const endpoint = form.dataset.playerSearch;
    if (!endpoint) return;
    const box = document.createElement('div');
    box.className = 'studio-toolbar';
    parent.append(box);
    const field = document.createElement('label');
    field.append('Add a player by name');
    const search = document.createElement('input');
    search.type = 'search';
    search.placeholder = 'At least two letters';
    search.disabled = squad.length >= MAX_SQUAD;
    field.append(search);
    box.append(field);
    const results = document.createElement('div');
    results.setAttribute('role', 'status');
    parent.append(results);

    let timer = null;
    let generation = 0;
    search.addEventListener('input', () => {
      clearTimeout(timer);
      const term = search.value.trim();
      if (term.length < 2) {
        results.replaceChildren();
        return;
      }
      timer = setTimeout(async () => {
        const ticket = ++generation;
        try {
          const response = await fetch(`${endpoint}?q=${encodeURIComponent(term)}`,
            {headers: {Accept: 'application/json'}, credentials: 'same-origin'});
          if (!response.ok) throw new Error(String(response.status));
          const found = (await response.json()).players ?? [];
          if (ticket !== generation) return;  // A later search already answered.
          results.replaceChildren();
          if (!found.length) {
            results.textContent = `No active card matches ${term}.`;
            return;
          }
          const options = document.createElement('div');
          options.className = 'studio-toolbar';
          results.append(options);
          for (const card of found) {
            if (squad.includes(card.id)) continue;
            cards.set(card.id, card);
            button(options, `${card.name} · ${card.version} · ${card.rating} OVR`, () => {
              if (squad.length >= MAX_SQUAD) return;
              squad.push(card.id);
              delete team.xi;
              team.players = squad;
              commit(config);
            });
          }
        } catch (_error) {
          if (ticket === generation) results.textContent = 'Could not search cards. Try again.';
        }
      }, 250);
    });
  }

  form.querySelector('[data-team-add]')?.addEventListener('click', () => {
    let config;
    try {
      config = readSource();
    } catch (_error) {
      showError(source, 'Correct the JSON in Full configuration before adding a team.');
      return;
    }
    const teams = teamList(config);
    if (teams.length >= MAX_TEAMS) return;
    const name = `Team ${teams.length + 1}`;
    config.teams = [...teams, {key: teamKey(config, name), name, players: []}];
    commit(config);
  });

  /* ---- Generated schedule: five views over the same fixture rows --------- */

  const fixturesRoot = form.querySelector('[data-fixture-views]');
  const fitNote = form.querySelector('[data-fixture-fit]');
  const VIEWS = [['table', 'Fixture table'], ['calendar', 'Calendar'], ['rounds', 'Round by round'],
                 ['teams', 'Team by team'], ['bracket', 'Playoff bracket']];
  let view = 'table';

  function teamName(config, slot) {
    const team = teamList(config).find(item => item.key === slot);
    if (team) return team.name || team.key;
    // A playoff slot is a reference, not a team: show it as the bracket reads it.
    const [kind, ...rest] = String(slot).split(':');
    const target = rest.join(':');
    if (kind === 'rank') return `Rank ${target}`;
    if (kind === 'group_rank') {
      const [group, rank] = target.split(':');
      return `Group ${group.toUpperCase()} rank ${rank}`;
    }
    if (kind === 'winner') return `Winner of ${label(target)}`;
    if (kind === 'loser') return `Loser of ${label(target)}`;
    return String(slot);
  }

  function label(key) {
    return String(key).replace(/-/g, ' ').replace(/\b\w/g, letter => letter.toUpperCase());
  }

  /* The offset a named zone was at, at one UTC instant. */
  function zoneOffset(utcMillis, zone) {
    try {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: zone, hour12: false, year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      }).formatToParts(new Date(utcMillis));
      const part = type => Number(parts.find(item => item.type === type)?.value);
      return Date.UTC(part('year'), part('month') - 1, part('day'),
                      part('hour') % 24, part('minute'), part('second')) - utcMillis;
    } catch (_error) {
      return 0;  // An unknown zone: compare as UTC rather than refuse to report.
    }
  }

  /* The UTC instant of a timestamp, read the way the server reads it.
   *
   * The event's own dates are bare wall-clock stamps in config.timezone, which
   * Date.parse would read as the *browser's* zone instead -- so an event opening at
   * 09:00 in Kolkata looked, to a UTC browser, like it opened five and a half hours
   * after its own first fixture. Generated fixture rows already carry an offset. */
  function instant(stamp, zone) {
    const text = String(stamp ?? '');
    if (!text) return NaN;
    if (/(?:Z|[+-]\d{2}:?\d{2})$/.test(text)) return Date.parse(text);
    const asUTC = Date.parse(`${text.length === 16 ? text + ':00' : text}Z`);
    if (!Number.isFinite(asUTC)) return NaN;
    return asUTC - zoneOffset(asUTC, zone);
  }

  // A fixture stamp without seconds. A generated row carries a UTC offset.
  function when(value) {
    const text = String(value ?? '');
    return text.slice(0, 16).replace('T', ' ') + (text.endsWith('+00:00') ? ' UTC' : '');
  }

  function localDay(value, zone) {
    const utcMillis = instant(value, zone);
    if (!Number.isFinite(utcMillis)) return String(value).slice(0, 10);
    try {
      return new Intl.DateTimeFormat('en-CA', {timeZone: zone}).format(new Date(utcMillis));
    } catch (_error) {
      return new Date(utcMillis).toISOString().slice(0, 10);
    }
  }

  function ordered(config) {
    return [...fixtureList(config)].sort((first, second) =>
      (first.round ?? 0) - (second.round ?? 0) || String(first.key).localeCompare(String(second.key)));
  }

  /* Does the whole schedule fit inside the event window? Reported, never corrected
   * on its own: the event's dates are the admin's to change. */
  function fit(config) {
    const rows = fixtureList(config);
    if (!rows.length) return null;
    const zone = config.timezone;
    const opens = instant(config.opens_at, zone);
    const closes = instant(config.closes_at, zone);
    const first = Math.min(...rows.map(row => instant(row.opens_at, zone)));
    const last = Math.max(...rows.map(row =>
      instant(row.deadline, zone) + (Number(row.grace) || 0) * 60000));
    if (!Number.isFinite(first) || !Number.isFinite(last)) return null;
    return {first, last, opens, closes,
            early: Number.isFinite(opens) && first < opens,
            late: Number.isFinite(closes) && last > closes,
            overrun: Number.isFinite(closes) ? Math.ceil((last - closes) / 86400000) : 0};
  }

  function drawFit(config) {
    if (!fitNote) return;
    const report = fit(config);
    const rows = fixtureList(config);
    if (!report) {
      fitNote.textContent = rows.length ? '' : 'No fixtures yet. Generate the schedule.';
      fitNote.className = '';
      return;
    }
    const stages = rows.filter(row => row.stage === 'playoff').length;
    const summary = `${rows.length} fixture${rows.length === 1 ? '' : 's'}`
      + (stages ? `, ${stages} of them playoffs` : '')
      + `, from ${when(rows.reduce((first, row) =>
            instant(first.opens_at, config.timezone) <= instant(row.opens_at, config.timezone)
              ? first : row).opens_at)}.`;
    if (report.late) {
      fitNote.className = 'studio-field-error';
      fitNote.textContent = `${summary} The schedule runs ${report.overrun} day`
        + `${report.overrun === 1 ? '' : 's'} past the event's end date. Use Auto Fix Schedule to `
        + `tighten the round interval, or change the end date yourself on the Dates step.`;
    } else if (report.early) {
      fitNote.className = 'studio-field-error';
      fitNote.textContent = `${summary} A fixture opens before the event's start date.`;
    } else {
      fitNote.className = 'studio-count';
      fitNote.textContent = `${summary} It fits inside the event window.`;
    }
  }

  function drawFixtures(config) {
    if (!fixturesRoot) return;
    drawFit(config);
    if (settled.has('fixtures')) return;
    fixturesRoot.replaceChildren();
    const rows = ordered(config);
    if (!rows.length) return;
    const tabs = document.createElement('div');
    tabs.className = 'studio-toolbar';
    tabs.setAttribute('role', 'tablist');
    fixturesRoot.append(tabs);
    for (const [key, title] of VIEWS) {
      if (key === 'bracket' && !rows.some(row => row.stage === 'playoff')) continue;
      const tab = button(tabs, title, () => {
        view = key;
        commit(config);
      }, {primary: view === key});
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(view === key));
    }
    const panel = document.createElement('div');
    panel.className = 'studio-scroll';
    panel.setAttribute('role', 'tabpanel');
    fixturesRoot.append(panel);
    ({table: fixtureTable, calendar: calendarView, rounds: roundView,
      teams: teamView, bracket: bracketView}[view] ?? fixtureTable)(panel, config, rows);
  }

  function table(parent, headings, className = '') {
    const element = document.createElement('table');
    if (className) element.className = className;
    const head = element.createTHead().insertRow();
    for (const heading of headings) {
      const cellElement = document.createElement('th');
      cellElement.scope = 'col';
      cellElement.textContent = heading;
      head.append(cellElement);
    }
    element.append(document.createElement('tbody'));
    parent.append(element);
    return element.tBodies[0];
  }

  function text(row, value) {
    const element = row.insertCell();
    element.textContent = value;
    return element;
  }

  /* The editable view. A field changed here becomes an authored override the next
   * generation keeps; Lock pins the whole row against regeneration. */
  function fixtureTable(parent, config, rows) {
    const body = table(parent, ['Fixture', 'Round', 'Stage', 'Home', 'Away', 'Opens',
                                'Deadline', 'Grace', 'Venue', 'Pitch', 'Weather', ''],
                       'studio-fixtures');
    for (const row of rows) {
      const line = body.insertRow();
      text(line, row.key);
      text(line, row.round ?? '');
      text(line, row.stage ?? '');
      for (const side of ['home', 'away']) {
        const holder = text(line, '');
        if (teamList(config).some(team => team.key === row[side])) {
          cell(holder, '', 'text', row[side], value => {
            row[side] = value;
            authored(row, side);
            commit(config, {editing: 'fixtures'});
          }, {options: teamList(config).map(team => [team.key, team.name || team.key])});
        } else {
          holder.textContent = teamName(config, row[side]);
        }
      }
      for (const field of ['opens_at', 'deadline']) {
        const holder = text(line, '');
        cell(holder, '', 'text', when(row[field]), value => {
          row[field] = value;
          authored(row, field);
          commit(config, {editing: 'fixtures'});
        });
      }
      cell(text(line, ''), '', 'number', row.grace ?? 0, value => {
        row.grace = value ?? 0;
        authored(row, 'grace');
        commit(config, {editing: 'fixtures'});
      }, {min: 0, max: 240});
      for (const [field, choices] of [['venue', null], ['pitch', PITCHES], ['weather', WEATHERS]]) {
        const holder = text(line, '');
        cell(holder, '', 'text', row[field] ?? '', value => {
          if (value) row[field] = value;
          else delete row[field];
          authored(row, field);
          commit(config, {editing: 'fixtures'});
        }, choices ? {options: ['', ...choices]} : {maxLength: 80});
      }
      const actions = text(line, '');
      const locked = Array.isArray(row.locks) && row.locks.length > 0;
      button(actions, locked ? 'Unlock' : 'Lock', () => {
        if (locked) delete row.locks;
        else row.locks = LOCKABLE.filter(field => field in row);
        commit(config);
      }, {primary: locked});
    }
  }

  const LOCKABLE = ['home', 'away', 'round', 'stage', 'opens_at', 'deadline', 'grace',
                    'pitch', 'venue', 'weather'];

  /* An edited field is recorded as an override so the next generation keeps it. */
  function authored(row, field) {
    if (!LOCKABLE.includes(field)) return;
    const overrides = new Set(Array.isArray(row.overrides) ? row.overrides : []);
    overrides.add(field);
    row.overrides = [...overrides].sort();
  }

  function calendarView(parent, config, rows) {
    const days = new Map();
    for (const row of rows) {
      const day = localDay(row.opens_at, config.timezone);
      if (!days.has(day)) days.set(day, []);
      days.get(day).push(row);
    }
    const body = table(parent, ['Date', 'Fixtures', 'Matches']);
    for (const [day, games] of [...days].sort()) {
      const line = body.insertRow();
      text(line, day);
      text(line, games.length);
      text(line, games.map(row =>
        `${teamName(config, row.home)} v ${teamName(config, row.away)}`).join(' · '))
        .style.whiteSpace = 'normal';
    }
  }

  function roundView(parent, config, rows) {
    const body = table(parent, ['Round', 'Stage', 'Opens', 'Matches']);
    const byRound = new Map();
    for (const row of rows) {
      const key = row.round ?? 0;
      if (!byRound.has(key)) byRound.set(key, []);
      byRound.get(key).push(row);
    }
    for (const [number, games] of [...byRound].sort((a, b) => a[0] - b[0])) {
      const line = body.insertRow();
      text(line, number);
      text(line, [...new Set(games.map(row => row.stage))].join(', '));
      text(line, when(games[0].opens_at));
      text(line, games.map(row =>
        `${teamName(config, row.home)} v ${teamName(config, row.away)}`).join(' · '))
        .style.whiteSpace = 'normal';
    }
  }

  function teamView(parent, config, rows) {
    const body = table(parent, ['Team', 'Matches', 'Home', 'Away', 'Fixtures']);
    for (const team of teamList(config)) {
      const own = rows.filter(row => row.home === team.key || row.away === team.key);
      const line = body.insertRow();
      text(line, team.name || team.key);
      text(line, own.length);
      text(line, own.filter(row => row.home === team.key).length);
      text(line, own.filter(row => row.away === team.key).length);
      text(line, own.map(row => row.home === team.key
        ? `v ${teamName(config, row.away)}` : `at ${teamName(config, row.home)}`).join(' · '))
        .style.whiteSpace = 'normal';
    }
  }

  function bracketView(parent, config, rows) {
    const playoffs = rows.filter(row => row.stage === 'playoff');
    const byRound = new Map();
    for (const row of playoffs) {
      const key = row.round ?? 0;
      if (!byRound.has(key)) byRound.set(key, []);
      byRound.get(key).push(row);
    }
    const wrapper = document.createElement('div');
    wrapper.className = 'studio-grid';
    parent.append(wrapper);
    for (const [number, games] of [...byRound].sort((a, b) => a[0] - b[0])) {
      const column = document.createElement('div');
      const heading = document.createElement('h5');
      heading.textContent = games.length === 1 && games[0].key === 'final'
        ? 'Final' : `Round ${number}`;
      column.append(heading);
      const list = document.createElement('ul');
      for (const row of games) {
        const item = document.createElement('li');
        item.textContent = `${label(row.key)}: ${teamName(config, row.home)} v ${teamName(config, row.away)}`;
        list.append(item);
      }
      column.append(list);
      wrapper.append(column);
    }
  }

  /* ---- Shuffle and Auto Fix --------------------------------------------- */

  /* Submit the draft with one generator action, the way its own button does. */
  function runGenerator(action) {
    const control = form.querySelector(`button[name="action"][value="${action}"]`);
    if (control) control.click();
    else form.requestSubmit();
  }

  /* Re-roll which teams meet when. The generator seeds its team order from
   * schedule.seed, so a fresh seed is a genuine reshuffle and stays reproducible. */
  form.querySelector('[data-fixture-shuffle]')?.addEventListener('click', () => {
    let config;
    try {
      config = readSource();
    } catch (_error) {
      showError(source, 'Correct the JSON in Full configuration before shuffling.');
      return;
    }
    config.schedule = {...config.schedule, seed: Math.floor(Math.random() * 2 ** 32)};
    source.value = JSON.stringify(config, null, 2);
    refresh(config);
    runGenerator('schedule');
  });

  /* Pull a schedule that overruns the event back inside its window by tightening the
   * round interval and then the match window. The event's own dates are never
   * touched: if nothing fits, say so and leave the draft to the admin. */
  form.querySelector('[data-fixture-autofix]')?.addEventListener('click', () => {
    let config;
    try {
      config = readSource();
    } catch (_error) {
      showError(source, 'Correct the JSON in Full configuration before fixing the schedule.');
      return;
    }
    const report = fit(config);
    if (!report || !(report.late || report.early)) {
      if (fitNote) {
        fitNote.className = 'studio-count';
        fitNote.textContent = 'The schedule already fits inside the event window.';
      }
      return;
    }
    const rounds = new Set(fixtureList(config).map(row => row.round ?? 0)).size;
    const days = Math.floor((report.closes - report.opens) / 86400000);
    const settings = {...config.schedule};
    const grace = Number(settings.grace) || 0;
    let fixed = false;
    if (rounds > 1 && Number.isFinite(days) && days > 0) {
      // Leave the last round room for its own window plus grace.
      const tail = Math.ceil((Number(settings.window_hours) * 60 + grace) / 1440);
      const spacing = Math.floor((days - tail) / (rounds - 1));
      if (spacing >= 1 && spacing < Number(settings.round_spacing_days)) {
        settings.round_spacing_days = spacing;
        fixed = true;
      }
    }
    if (!fixed && Number(settings.window_hours) > 1) {
      // One round per day at the tightest interval still overran, so shorten the
      // match window instead. A day of play minus the grace it must still allow.
      const window = Math.max(1, Math.min(Number(settings.window_hours), 24) - Math.ceil(grace / 60) - 1);
      if (window < Number(settings.window_hours)) {
        settings.window_hours = window;
        settings.round_spacing_days = 1;
        fixed = true;
      }
    }
    if (!fixed) {
      if (fitNote) {
        fitNote.className = 'studio-field-error';
        fitNote.textContent = `This schedule needs more than the ${days} day`
          + `${days === 1 ? '' : 's'} between the event's dates, even at a one-day round`
          + ` interval. Extend the end date on the Dates step, or cut fixtures.`;
      }
      return;
    }
    config.schedule = settings;
    source.value = JSON.stringify(config, null, 2);
    refresh(config);
    runGenerator('schedule');
  });

  /* ---- Objectives -------------------------------------------------------- */

  const objectivesRoot = form.querySelector('[data-objectives-editor]');
  const objectiveCount = form.querySelector('[data-objective-count]');
  const MAX_OBJECTIVES = 40;
  const STAGES = [['', 'Every stage'], ['league', 'League only'], ['playoff', 'Playoffs only']];

  function objectiveRows(config) {
    return Array.isArray(config.tournament_objectives) ? config.tournament_objectives : [];
  }

  function objectiveId(rows, metric) {
    const used = new Set(rows.map(row => row?.id));
    let index = 1;
    let id = `${metric}_${index}`;
    while (used.has(id)) id = `${metric}_${++index}`;
    return id;
  }

  function drawObjectives(config) {
    if (!objectivesRoot) return;
    const rows = objectiveRows(config);
    const stars = rows.reduce((total, row) => total + (Number(row?.stars) || 0), 0);
    if (objectiveCount) {
      objectiveCount.textContent = rows.length
        ? `${rows.length} objective${rows.length === 1 ? '' : 's'}, ${stars} stars` : '';
    }
    const add = form.querySelector('[data-objective-add]');
    if (add) add.disabled = rows.length >= MAX_OBJECTIVES;
    if (settled.has('objectives')) return;
    objectivesRoot.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement('p');
      empty.textContent = 'No objectives yet. Use Auto Generate Objectives, or add one by hand.';
      objectivesRoot.append(empty);
      return;
    }
    const scroll = document.createElement('div');
    scroll.className = 'studio-scroll';
    objectivesRoot.append(scroll);
    const body = table(scroll, ['Objective', 'Metric', 'Target', 'Stars', 'Stage',
                                'Minimum balls', 'Options']);
    for (const [index, row] of rows.entries()) {
      if (!row || typeof row !== 'object' || Array.isArray(row)) continue;
      const line = body.insertRow();
      const save = () => commit(config, {editing: 'objectives'});
      cell(line.insertCell(), '', 'text', row.id, value => {
        row.id = value;
        save();
      }, {maxLength: 40});
      cell(line.insertCell(), '', 'text', row.metric, value => {
        row.metric = value;
        save();
      }, {options: METRICS});
      cell(line.insertCell(), '', 'number', row.target, value => {
        row.target = value ?? 0;
        save();
      }, {min: 0, step: 'any'});
      cell(line.insertCell(), '', 'number', row.stars, value => {
        row.stars = value ?? 0;
        save();
      }, {min: 0, max: 120});
      const stages = Array.isArray(row.stages) ? row.stages : [];
      cell(line.insertCell(), '', 'text', stages.length === 1 ? stages[0] : '', value => {
        if (value) row.stages = [value];
        else delete row.stages;
        save();
      }, {options: STAGES});
      cell(line.insertCell(), '', 'number', row.min_balls, value => {
        if (value) row.min_balls = value;
        else delete row.min_balls;
        save();
      }, {min: 0});
      const actions = line.insertCell();
      cell(actions, 'Lock', 'checkbox', row.locked, value => {
        if (value) row.locked = true;
        else delete row.locked;
        save();
      });
      button(actions, 'Remove', () => {
        config.tournament_objectives = rows.filter((_, position) => position !== index);
        commit(config);
      });
    }
  }

  form.querySelector('[data-objective-add]')?.addEventListener('click', () => {
    let config;
    try {
      config = readSource();
    } catch (_error) {
      showError(source, 'Correct the JSON in Full configuration before adding an objective.');
      return;
    }
    const rows = objectiveRows(config);
    if (rows.length >= MAX_OBJECTIVES) return;
    const metric = METRICS[0] ?? 'runs';
    config.tournament_objectives = [...rows,
      {id: objectiveId(rows, metric), metric, target: 1, stars: 1}];
    commit(config);
  });

  /* ---- Per match rewards ------------------------------------------------- */

  const rewardsRoot = form.querySelector('[data-rewards-editor]');
  const ASSETS = [['coins', 'Coins'], ['gems', 'Gems'], ['quest_points', 'Quest Points']];
  const TIERS = [['free', 'Free users'], ['diamond', 'Diamond membership']];

  function drawRewards(config) {
    if (!rewardsRoot) return;
    if (settled.has('rewards')) return;
    rewardsRoot.replaceChildren();
    // A Live Event is decided win, loss or tie; a Moment or Boss Battle is pass or fail.
    const results = config.kind === 'live'
      ? [['win', 'Win'], ['loss', 'Loss'], ['tie', 'Tie']] : [['success', 'Success']];
    const bundles = (config.rewards && typeof config.rewards === 'object') ? config.rewards : {};
    for (const [tier, title] of TIERS) {
      const heading = document.createElement('h4');
      heading.textContent = title;
      rewardsRoot.append(heading);
      const scroll = document.createElement('div');
      scroll.className = 'studio-scroll';
      rewardsRoot.append(scroll);
      const body = table(scroll, ['Result', ...ASSETS.map(([, name]) => name)]);
      for (const [result, resultTitle] of results) {
        const line = body.insertRow();
        text(line, resultTitle);
        for (const [asset] of ASSETS) {
          cell(line.insertCell(), '', 'number', bundles[tier]?.[result]?.[asset], value => {
            const all = {...config.rewards};
            const track = {...(all[tier] ?? {})};
            const bundle = {...(track[result] ?? {})};
            if (value) bundle[asset] = value;
            else delete bundle[asset];
            if (Object.keys(bundle).length) track[result] = bundle;
            else delete track[result];
            all[tier] = track;
            config.rewards = all;
            commit(config, {editing: 'rewards'});
          }, {min: 0, max: 1000000});
        }
      }
    }
    const note = document.createElement('p');
    note.className = 'studio-hint';
    note.textContent = 'Diamond must match or beat Free for every asset. Leave a cell empty to '
      + 'award nothing. Packs and player cards are configured under Pack and card reward items.';
    rewardsRoot.append(note);
  }

  /* ---- Participation modes ----------------------------------------------- */

  /* Three independent checkboxes over two stored lists: Solo and Multiplayer are
   * competition.modes, Own Roster is a participation track in mode. */
  const modeToggles = [...form.querySelectorAll('[data-mode-toggle]')];
  const modeWarning = form.querySelector('[data-mode-warning]');

  function modeState(config) {
    const modes = Array.isArray(config.competition?.modes) ? config.competition.modes : [];
    const tracks = Array.isArray(config.mode) ? config.mode : [];
    return {solo: modes.includes('solo'), multi: modes.includes('multi'),
            own_roster: tracks.includes('own_roster')};
  }

  function drawModes(config) {
    const state = modeState(config);
    const tournament = config.kind === 'live' && config.objective_scope === 'tournament';
    for (const toggle of modeToggles) {
      const key = toggle.dataset.modeToggle;
      const box = toggle.querySelector('input') ?? toggle;
      box.checked = state[key];
      // Solo and Multiplayer are shared-tournament modes; a Moment has neither.
      box.disabled = !tournament && key !== 'own_roster';
    }
    if (modeWarning) {
      const enabled = Object.entries(state).filter(([, on]) => on).map(([key]) => key);
      modeWarning.textContent = tournament && !state.solo && !state.multi
        ? 'Enable Solo, Team, or both: a shared tournament needs at least one of them.'
        : enabled.length ? '' : 'Enable at least one participation mode.';
      modeWarning.className = modeWarning.textContent ? 'studio-field-error' : '';
    }
  }

  for (const toggle of modeToggles) {
    const box = toggle.querySelector('input') ?? toggle;
    box.addEventListener('change', () => {
      let config;
      try {
        config = readSource();
      } catch (_error) {
        showError(source, 'Correct the JSON in Full configuration before changing a mode.');
        return;
      }
      const key = toggle.dataset.modeToggle;
      if (key === 'own_roster') {
        const tracks = new Set(Array.isArray(config.mode) ? config.mode : []);
        if (box.checked) tracks.add('own_roster');
        else tracks.delete('own_roster');
        tracks.add('tournament_team');  // The official-franchise track is always present.
        config.mode = [...tracks];
      } else {
        const modes = new Set(Array.isArray(config.competition?.modes) ? config.competition.modes : []);
        if (box.checked) modes.add(key);
        else modes.delete(key);
        config.competition = {...config.competition, modes: [...modes]};
      }
      commit(config);
    });
  }

  /* ---- Playing days ------------------------------------------------------ */

  const dayBoxes = [...form.querySelectorAll('[data-playing-day]')];

  function drawPlayingDays(config) {
    const days = new Set((Array.isArray(config.schedule?.playing_days)
      ? config.schedule.playing_days : []).map(Number));
    for (const box of dayBoxes) box.checked = days.has(Number(box.dataset.playingDay));
  }

  for (const box of dayBoxes) {
    box.addEventListener('change', () => {
      let config;
      try {
        config = readSource();
      } catch (_error) {
        showError(source, 'Correct the JSON in Full configuration before changing playing days.');
        return;
      }
      const days = new Set((Array.isArray(config.schedule?.playing_days)
        ? config.schedule.playing_days : []).map(Number));
      const day = Number(box.dataset.playingDay);
      if (box.checked) days.add(day);
      else days.delete(day);
      config.schedule = {...config.schedule, playing_days: [...days].sort((a, b) => a - b)};
      commit(config);
    });
  }

  /* ---- Which settings a schedule type actually uses ---------------------- */

  /* Only show the settings the chosen schedule type reads, so the step is a short
   * form rather than every field the generator has ever supported. */
  const PRESET_FIELDS = {
    legs: ['round_robin', 'groups'],
    target: ['one_half_round_robin'],
    series: ['series'],
    groups: ['groups'],
    knockout: ['knockout', 'groups'],
    double: ['double_elimination'],
    playoffs: ['round_robin', 'one_half_round_robin', 'manual'],
  };

  const PLAYOFF_SHAPES = {
    none: 'The league table decides the title. No playoff fixtures are written.',
    top2: 'Ranks 1 and 2 meet in a single final.',
    top4_semis: 'Rank 1 v 4 and rank 2 v 3, then the winners meet in the final.',
    top4: 'Qualifier 1 (ranks 1 and 2) and the Eliminator (ranks 3 and 4); the Eliminator '
      + 'winner meets the Qualifier 1 loser in Qualifier 2, whose winner meets the Qualifier 1 '
      + 'winner in the final.',
    top6: 'Two eliminators (ranks 3 v 6 and 4 v 5), then ranks 1 and 2 host the survivors in '
      + 'the semi-finals, then the final.',
    top8: 'Four quarter-finals seeded 1 v 8, 4 v 5, 2 v 7 and 3 v 6, then semi-finals, then '
      + 'the final.',
    custom: 'Nothing is generated: author the playoff rows yourself in the fixture table or as '
      + 'JSON, and generating the league will keep them.',
  };

  const PLAYOFF_TEAMS = {top2: 2, top4_semis: 4, top4: 4, top6: 6, top8: 8};

  function drawPresets(config) {
    const preset = config.schedule?.preset ?? 'round_robin';
    for (const [name, presets] of Object.entries(PRESET_FIELDS)) {
      for (const element of form.querySelectorAll(`[data-preset-${name}]`)) {
        element.hidden = !presets.includes(preset);
      }
    }
    const shape = form.querySelector('[data-playoff-shape]');
    if (shape) {
      const playoffs = config.schedule?.playoffs ?? 'none';
      const needed = PLAYOFF_TEAMS[playoffs] ?? 0;
      const have = teamList(config).length;
      shape.textContent = PLAYOFF_SHAPES[playoffs] ?? '';
      if (needed && have && have < needed) {
        shape.textContent += ` This bracket needs ${needed} teams and the draft has ${have}.`;
        shape.className = 'studio-field-error';
      } else {
        shape.className = 'studio-hint';
      }
      if (preset === 'manual' && playoffs !== 'none' && playoffs !== 'custom') {
        shape.textContent += ' Your authored league rows keep their own timings; only the '
          + 'bracket is generated.';
      }
    }
  }

  /* ---- Event window ------------------------------------------------------ */

  function drawWindow(config) {
    const note = form.querySelector('[data-studio-window]');
    if (!note) return;
    const opens = instant(config.opens_at, config.timezone);
    const closes = instant(config.closes_at, config.timezone);
    if (!Number.isFinite(opens) || !Number.isFinite(closes)) {
      note.textContent = '';
      note.className = '';
      return;
    }
    const days = (closes - opens) / 86400000;
    if (days <= 0) {
      note.textContent = 'The end date must come after the start date.';
      note.className = 'studio-field-error';
      return;
    }
    const whole = Math.round(days * 10) / 10;
    note.textContent = `The event runs for ${whole} day${whole === 1 ? '' : 's'}.`;
    note.className = 'studio-hint';
  }

  /* ---- Review summary ---------------------------------------------------- */

  function drawSummary(config) {
    const body = form.querySelector('[data-studio-summary]');
    if (!body) return;
    const rows = fixtureList(config);
    const state = modeState(config);
    const enabled = [state.solo && 'Solo', state.multi && 'Team (multiplayer)',
                     state.own_roster && 'Own Roster'].filter(Boolean);
    const objectives = objectiveRows(config);
    const stars = objectives.reduce((total, row) => total + (Number(row?.stars) || 0), 0);
    const report = fit(config);
    const league = rows.filter(row => row.stage !== 'playoff').length;
    const entries = [
      ['Event name', form.closest('.studio')?.querySelector('h2')?.textContent?.trim() ?? ''],
      ['Event type', {live: 'Live Event', moment: 'Cricket Moment', boss: 'Boss Battle'}[config.kind] ?? config.kind],
      ['Match format', config.format],
      ['Teams', `${teamList(config).length} team(s), ${teamList(config)
        .reduce((total, team) => total + (team.players?.length ?? 0), 0)} squad cards`],
      ['Total fixtures', rows.length ? `${rows.length} (${league} league, ${rows.length - league} playoff)` : 'None generated'],
      ['Event duration', config.opens_at && config.closes_at
        ? `${when(config.opens_at)} to ${when(config.closes_at)} (${config.timezone})` : 'Not set'],
      ['Schedule fits the window', report ? (report.late || report.early ? 'No' : 'Yes') : 'No fixtures'],
      ['Participation modes', enabled.length ? enabled.join(', ') : 'None enabled'],
      ['Maximum participants', config.max_users],
      ['Objectives', objectives.length ? `${objectives.length} objective(s)` : 'None'],
      ['Maximum stars', stars || 0],
      ['Event Pass', config.competition?.event_pass
        ? `${(config.milestones ?? []).length} milestone(s)` : 'Disabled'],
    ];
    body.replaceChildren();
    for (const [name, value] of entries) {
      const line = body.insertRow();
      const heading = document.createElement('th');
      heading.scope = 'row';
      heading.textContent = name;
      line.append(heading);
      const holder = line.insertCell();
      holder.textContent = String(value ?? '');
      holder.style.whiteSpace = 'normal';
    }
  }

  /* ---- Field binding ----------------------------------------------------- */

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
        const stamp = value ?? '';
        const local = LOCAL_TIME.test(stamp);
        field.type = !stamp || local ? 'datetime-local' : 'text';
        return local ? stamp.slice(0, 16) : stamp;
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

  for (const field of fields) {
    field.addEventListener('input', () => {
      let config;
      let value = field.value;
      try {
        config = readSource();
      } catch (_error) {
        showError(source, 'Correct the JSON in Full configuration before changing a section.');
        source.closest('details').open = true;
        show('advanced');
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
          else if (type === 'number') value = Number(value);
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

  /* ---- Event Pass milestones --------------------------------------------- */

  const passEditor = form.querySelector('[data-pass-editor]');
  const milestoneField = form.querySelector('[data-config-path="milestones"]');

  function saveMilestones(rows, rebuild = false) {
    milestoneField.value = JSON.stringify(rows, null, 2);
    if (!rebuild) settled.add('pass');
    milestoneField.dispatchEvent(new Event('input'));
    settled.delete('pass');
  }

  function drawMilestones(config, tournament) {
    if (!passEditor) return;
    passEditor.hidden = !(tournament && config.competition?.event_pass);
    if (passEditor.hidden || settled.has('pass')) return;
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
      const holder = item.insertCell();
      const id = cell(holder, 'Stable ID', 'text', row.id, value => {
        row.id = value;
        saveMilestones(rows);
      }, {maxLength: 40});
      cell(holder, 'Claimed stars', 'number', row.stars, value => {
        row.stars = value ?? 0;
        saveMilestones(rows);
      }, {min: 1, max: 120});
      for (const [tier] of TIERS) {
        const box = document.createElement('div');
        box.className = 'studio-pass-bundle';
        item.insertCell().append(box);
        for (const [asset, name] of ASSETS) {
          cell(box, name, 'number', row[tier]?.[asset], value => {
            const bundle = row[tier] ??= {};
            if (value) bundle[asset] = value;
            else delete bundle[asset];
            saveMilestones(rows);
          }, {min: 0, max: 1000000});
        }
      }
      const actions = item.insertCell();
      cell(actions, 'Lock', 'checkbox', row.locked, value => {
        row.locked = value;
        saveMilestones(rows);
      });
      cell(actions, 'Optional', 'checkbox', row.bonus, value => {
        row.bonus = value;
        saveMilestones(rows);
      });
      button(actions, 'Remove', () => {
        rows.splice(index, 1);
        saveMilestones(rows, true);
        add.focus();
      });
      void id;
    }
  }

  /* ---- Redraw ------------------------------------------------------------ */

  function refresh(config, except = null) {
    for (const element of form.querySelectorAll('[data-kind-boss]')) {
      element.hidden = config.kind !== 'boss';
    }
    for (const element of form.querySelectorAll('[data-kind-moment]')) {
      element.hidden = !(config.kind === 'moment' || config.kind === 'boss' && config.challenge_style === 'moment');
    }
    for (const element of form.querySelectorAll('[data-kind-live]')) {
      element.hidden = config.kind !== 'live';
    }
    const tournament = config.kind === 'live' && config.objective_scope === 'tournament';
    for (const section of form.querySelectorAll('[data-tournament-objectives]')) section.hidden = !tournament;
    const legacy = form.querySelector('[data-legacy-objectives]');
    if (legacy) legacy.hidden = tournament;
    const budgetLabel = form.querySelector('[data-event-budget-label]');
    if (budgetLabel) {
      budgetLabel.textContent = tournament ? 'Total human capacity' : 'Players covered by the budget';
    }
    const autoObjectives = form.querySelector('[data-auto-objectives]');
    if (autoObjectives) {
      autoObjectives.textContent = tournament
        ? 'Auto Generate Objectives' : 'Auto Generate Match Objectives';
    }
    for (const field of fields) {
      if (field === except || field.getAttribute('aria-invalid') === 'true') continue;
      if (field.type === 'checkbox') field.checked = Boolean(at(config, path(field)));
      else field.value = display(field, at(config, path(field)), config);
    }
    drawMilestones(config, tournament);
    drawTeams(config);
    drawPresets(config);
    drawPlayingDays(config);
    drawFixtures(config);
    drawObjectives(config);
    drawRewards(config);
    drawModes(config);
    drawWindow(config);
    drawSummary(config);
    markInvalidSteps();
  }

  let stored = null;
  try {
    stored = sessionStorage.getItem(stepKey);
  } catch (_error) {
    // No stored step is fine; the server's own value or the first step is used.
  }
  show(steps.includes(stepField?.value) && stepField.value ? stepField.value
    : steps.includes(stored) ? stored : steps[0]);
  refresh(readSource());
})();
