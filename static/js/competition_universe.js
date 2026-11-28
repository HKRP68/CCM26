/* Competition discovery and projections; official match actions remain in their own hubs. */
const CompetitionUniverse = (() => {
  let requestNumber = 0;
  let back = null;

  function el(tag, content, css) {
    const node = document.createElement(tag);
    if (content != null) node.textContent = content;
    if (css) node.className = css;
    return node;
  }

  function button(text, listener) {
    const control = el('button', text);
    control.type = 'button';
    control.addEventListener('click', listener);
    return control;
  }

  function selection(label, options, current, update) {
    const wrapper = el('label', label);
    const select = el('select');
    select.setAttribute('aria-label', label);
    for (const [key, name] of options) {
      const option = el('option', name);
      option.value = key;
      option.selected = String(key) === String(current ?? '');
      select.append(option);
    }
    select.addEventListener('change', () => update(select.value));
    wrapper.append(select);
    return wrapper;
  }

  function dateFilter(label, current, update) {
    const wrapper = el('label', label);
    const control = el('input');
    control.setAttribute('aria-label', label);
    control.type = 'date';
    control.value = current || '';
    control.addEventListener('change', () => update(control.value || null));
    wrapper.append(control);
    return wrapper;
  }

  function table(title, headings, rows) {
    const section = el('section', null, 'ev-panel');
    section.append(el('h3', title));
    if (!rows.length) {
      section.append(el('p', 'No recorded data for this view.', 'ev-meta'));
      return section;
    }
    const wrapper = el('div', null, 'ev-scroll');
    wrapper.tabIndex = 0;
    wrapper.setAttribute('aria-label', title);
    const grid = el('table');
    const head = el('thead');
    const tr = el('tr');
    for (const label of headings) {
      const th = el('th', label);
      th.scope = 'col';
      tr.append(th);
    }
    head.append(tr);
    const body = el('tbody');
    for (const values of rows) {
      const row = el('tr');
      for (const value of values) {
        const cell = el('td');
        if (value instanceof Node) cell.append(value);
        else cell.textContent = value ?? '—';
        row.append(cell);
      }
      body.append(row);
    }
    grid.append(head, body);
    wrapper.append(grid);
    section.append(wrapper);
    return section;
  }

  function date(value) {
    return value ? new Date(value).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}) : '';
  }

  function humanize(value) {
    return (value || '').replaceAll('_', ' ').replace(/\b\w/g, ch => ch.toUpperCase());
  }

  async function request(body) {
    const {ok, data} = await api('/api/webapp/competition-universe', body);
    if (!ok || !data?.ok) throw new Error(data?.message || 'Competition Universe is unavailable.');
    return data;
  }

  function openOfficial(card, next = false) {
    if (card.source === 'live') {
      if (next && card.next.status === 'in_progress') {
        EventHub.openFixture(card.next.fixture);
        return;
      }
      navigate('events');
      if (card.status === 'lobby') EventHub.openLobby(null, card.instance);
      else EventHub.open(card.event_id);
    } else {
      openDirectoryTournament(card.instance, card.status === 'completed', next ? 'matches' : 'overview');
    }
  }

  function links(root, body, more) {
    const choices = el('div', null, 'ev-actions');
    if (body.page) choices.append(button('Previous', () => load({...body, page: body.page - 1})));
    if (more) choices.append(button('Next', () => load({...body, page: (body.page || 0) + 1})));
    if (choices.childElementCount) root.append(choices);
  }

  function renderDirectory(root, body, data) {
    const update = fields => load({...body, ...fields, page: 0});
    const tabs = el('nav', null, 'ev-actions');
    tabs.setAttribute('aria-label', 'Competition directory');
    const views = [
      ['For You', 'all', 'all'], ['Live Events', 'live', 'all'],
      ['Challenge League', 'challenge', 'all'], ['My Competitions', 'all', 'joined'], ['History', 'all', 'history']
    ];
    for (const [caption, source, status] of views) {
      const control = button(caption, () => update({source, status}));
      control.setAttribute('aria-pressed', String((body.source || 'all') === source && (body.status || 'all') === status));
      tabs.append(control);
    }
    root.append(tabs, selection('Match format', [['', 'All formats'], ['T20', 'T20'], ['ODI', 'ODI']],
      body.format, format => update({format: format || null})));
    if (data.personal?.pvp_rating != null || data.personal?.titles > 0) {
      const items = [];
      if (data.personal.pvp_rating != null) items.push(`PvP rating ${data.personal.pvp_rating}`);
      if (data.personal.titles > 0) items.push(`${data.personal.titles} tournament ${data.personal.titles === 1 ? 'title' : 'titles'}`);
      root.append(el('p', items.join(' · '), 'ev-meta'));
    }
    if (data.next.length && !body.page && ['all', 'joined', undefined].includes(body.status)) {
      const next = el('section', null, 'ev-panel');
      next.append(el('h2', 'Next fixtures'));
      for (const card of data.next.slice(0, 5)) {
        const row = el('div', null, 'ev-between');
        const future = card.next.opens && new Date(card.next.opens) > new Date();
        const detail = future ? 'Opens ' + date(card.next.opens)
          : card.next.in_grace && card.next.grace ? 'Grace ends ' + date(card.next.grace)
          : card.next.deadline ? 'Deadline ' + date(card.next.deadline) : 'Fixture open';
        const action = card.source === 'live' && card.next.status === 'in_progress' ? 'Resume match' : card.action;
        row.append(el('p', `${card.name} · ${detail}`), button(action, () => openOfficial(card, true)));
        next.append(row);
      }
      root.append(next);
    }
    for (const card of data.items) {
      const article = el('article', null, 'ev-row');
      article.append(el('h3', card.name),
        el('p', `${humanize(card.mode)} · ${card.format} · ${humanize(card.status)} · ${humanize(card.access)}`, 'ev-meta'));
      if (card.teams.length) article.append(el('p', card.teams.join(' · ')));
      if (card.total != null) article.append(el('p', `${card.played}/${card.total} fixtures completed`, 'ev-meta'));
      const deadline = card.next?.deadline || card.deadline || card.enrollment_deadline;
      if (deadline) article.append(el('p', (card.next?.deadline ? 'Match deadline ' :
        card.deadline ? 'Lobby closes ' : 'Enrollment closes ') + date(deadline), 'ev-meta'));
      article.append(button(card.action, () => openOfficial(card)));
      if (card.capabilities?.analytics) article.append(button('Analytics', () =>
        load({action: 'analytics', source: card.source, instance: card.instance})));
      root.append(article);
    }
    if (!data.items.length) root.append(el('p', 'No competitions match these filters.', 'ev-meta'));
    links(root, body, data.more);
  }

  function renderRankings(root, body, data) {
    if (!data.available) {
      root.append(el('p', 'World Rankings are unavailable.'));
      return;
    }
    const update = fields => load({...body, ...fields, page: 0});
    const year = new Date().getUTCFullYear();
    root.append(selection('Board', [['overall', 'Overall PvP'], ['challenge', 'Challenge League'],
      ['live', 'Live Events Multiplayer'], ['solo', 'My Solo record']], body.scope || 'overall', scope => update({scope})),
      selection('Format', [['all', 'All formats'], ['T20', 'T20'], ['ODI', 'ODI']],
        body.format || 'all', format => update({format})),
      selection('Season', [[0, 'All time'], [year, String(year)], [year - 1, String(year - 1)]],
        body.season || 0, season => update({season: Number(season)})));
    if (data.solo) {
      root.append(table('My Solo record', ['Format', 'Played', 'Won', 'Titles', 'Stars'],
        data.solo.map(row => [row.format, row.completed_matches, row.wins, row.titles, row.stars])));
      return;
    }
    if (data.own) root.append(el('p', `Your rating ${data.own.rating} · ${data.own.matches} verified matches`
      + (data.own.provisional ? ' · Provisional' : '')));
    root.append(el('p', data.explanation || 'Waiting for verified results.', 'ev-meta'));
    root.append(table('World Rankings', ['Rank', 'Manager', 'Rating', 'Played', 'Won'],
      data.rows.map(row => [row.rank, row.own ? 'You' : row.name, row.rating, row.matches, row.wins])));
    if (data.history?.length) root.append(table('Your recent changes', ['Date', 'Source', 'Before', 'After'],
      data.history.map(row => [date(row.date), humanize(row.source), row.before, row.after])));
    links(root, body, data.more);
  }

  function renderCollection(root, body, data) {
    if (!data.available) {
      root.append(el('p', 'Collection Book is unavailable.'));
      return;
    }
    const year = new Date().getUTCFullYear();
    const update = fields => load({...body, ...fields, page: 0});
    root.append(selection('Source', [['', 'All competitions'], ['live', 'Live Events'], ['challenge', 'Challenge League']],
      body.source, source => update({source: source || null})),
    selection('Mode', [['', 'All modes'], ['solo', 'Solo'], ['multi', 'Multiplayer'], ['challenge', 'Challenge League']],
      body.mode, mode => update({mode: mode || null})),
    selection('Format', [['', 'All formats'], ['T20', 'T20'], ['ODI', 'ODI']],
      body.format, format => update({format: format || null})),
    selection('Edition year', [['', 'All years'], [year, String(year)], [year - 1, String(year - 1)]],
      body.season, season => update({season: season ? Number(season) : null})));
    if (data.progress) root.append(el('p', `${data.progress.titles} titles · ${data.progress.families}/${data.progress.available_families} active families`));
    if (data.sets.length) root.append(table('Collection progress', ['Family', 'Set', 'Status', 'Editions won'],
      data.sets.map(row => [row.label, humanize(row.category), row.retired ? 'Retired' : row.earned ? 'Earned' : 'Locked', row.earned])));
    for (const item of data.rows) {
      const section = el('article', null, 'ev-row');
      section.append(el('h3', item.name), el('p', `${humanize(item.kind)} · ${item.team || 'Team'} · ${item.format || ''}`),
        el('p', `${humanize(item.mode)} · ${item.role} · ${date(item.earned_at)}${item.revoked ? ' · Superseded' : ''}`, 'ev-meta'));
      if (item.archived) section.append(el('p', 'Archived season', 'ev-meta'));
      if (!item.revoked && !item.archived) section.append(button('View tournament', () => openOfficial({...item, status: 'completed'})));
      root.append(section);
    }
    if (!data.rows.length) root.append(el('p', 'Your verified tournament honours will appear here.', 'ev-meta'));
    links(root, body, data.more);
  }

  function renderAnalytics(root, body, data) {
    const update = fields => load({...body, ...fields});
    const team = data.team_options.map(row => [row.key, row.name]);
    root.append(selection('Team', [['', 'All teams'], ...team], body.team, key => {
      update({team: team.find(row => String(row[0]) === key)?.[0] ?? null});
    }), selection('Format', [['', 'All formats'], ...data.format_options.map(key => [key, key])],
      body.format, format => update({format: format || null})),
    selection('Participation', [['all', 'All results'], ['human', 'Verified human PvP'], ['ai', 'AI fixtures']],
      body.participation || 'all', participation => update({participation})),
    selection('Stage', [['', 'All stages'], ...(data.stage_options || []).map(key => [key, humanize(key)])],
      body.stage, stage => update({stage: stage || null})),
    dateFilter('From (UTC)', body.from, from => update({from})),
    dateFilter('Before (UTC)', body.until, until => update({until})));
    root.append(table('Team overview', ['Team', 'Format', 'Played', 'Won', 'Lost', 'Tied', 'No result', 'Win %',
      'AI fixtures', 'Form', 'Runs for', 'Runs against', 'Wickets taken', 'Wickets lost'],
      data.teams.map(row => [row.team, row.format, row.played, row.wins, row.losses, row.ties, row.no_results,
        row.win_rate, row.ai_fixtures, row.form?.join(' '), row.runs_for, row.runs_against, row.wickets_for, row.wickets_against])));
    root.append(table('Official standings', ['Team', 'Points', 'NRR'],
      data.standings.map(row => [row.name, row.points, row.nrr])));
    root.append(table('Batting', ['Player', 'Team', 'Format', 'Innings', 'Not out', 'Runs', 'Balls', 'Average',
      'Strike rate', '50s', '100s', '4s', '6s', 'Boundary %', 'High score', 'Recent scores'],
      data.players.map(row => [row.name, row.team, row.format, row.innings, row.not_outs, row.bat_runs, row.bat_balls,
        row.average, row.strike_rate, row.fifties, row.centuries, row.bat_fours, row.bat_sixes, row.boundary_rate,
        row.high, row.recent?.join(', ')])));
    root.append(table('Bowling', ['Player', 'Team', 'Format', 'Overs', 'Runs', 'Wickets', 'Economy', 'Average',
      'Strike rate', 'Dots %', 'Maidens', '3-wicket hauls', '5-wicket hauls', 'Best'],
      data.players.map(row => [row.name, row.team, row.format, row.overs, row.bowl_runs, row.bowl_wickets, row.economy,
        row.bowling_average, row.bowling_strike_rate, row.dot_rate, row.maidens, row.three_fors, row.five_fors,
        row.best?.join('/') ])));
    if (data.phases.some(row => row.rpo != null && row.balls > 0)) root.append(phaseComparison(data.phases));
    root.append(table('Phases', ['Team', 'Format', 'Phase', 'Legal balls', 'Runs per over', 'Dots %', 'Wickets per 100 balls'],
      data.phases.map(row => [row.team, row.format, humanize(row.phase), row.balls, row.rpo, row.dot_rate, row.wickets_per_100])));
    root.append(table('Approach matchups', ['Team', 'Batting', 'Bowling', 'Recorded overs', 'Runs per over', 'Sample'],
      data.tactics.map(row => [row.team, humanize(row.bat), humanize(row.bowl), row.overs, row.rpo, row.small_sample ? 'Small' : 'Recorded'])));
    root.append(table('Conditions', ['Format', 'Condition', 'Value', 'Recorded matches', 'Bat-first wins', 'Chase wins'],
      (data.contexts || []).map(row => [row.format, humanize(row.condition), row.value, row.recorded_matches,
        row.bat_first_wins, row.chase_wins])));
    root.append(el('p', data.notice, 'ev-meta'));
    root.append(table('Recent fixtures', ['Match', 'Format', 'Stage', 'Result', 'Evidence', 'Scorecard'],
      data.fixtures.map(row => [[row.home, row.away].filter(Boolean).join(' v '), row.format, humanize(row.stage),
        row.winner || humanize(row.status), humanize(row.eligibility), fixtureScores(row.innings || [])])));
    if (data.players.length) root.append(button('Export player CSV', async () => {
      const status = document.getElementById('universe-status');
      try {
        const csv = await request({...body, action: 'csv'});
        const url = URL.createObjectURL(new Blob([csv.csv], {type: 'text/csv;charset=utf-8'}));
        const link = el('a');
        link.href = url;
        link.download = 'competition-analytics.csv';
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) {
        status.textContent = error.message;
      }
    }));
  }

  function fixtureScores(innings) {
    if (!innings.length) return null;
    const control = el('details');
    control.append(el('summary', 'Scores'));
    for (const inning of innings) {
      const score = [inning.runs, inning.wickets].filter(value => value != null).join('/');
      control.append(el('p', `${inning.team || 'Team'} ${score}${inning.overs ? ` (${inning.overs} ov)` : ''}`));
    }
    return control;
  }

  function phaseComparison(rows) {
    const section = el('section', null, 'ev-panel');
    section.append(el('h3', 'Phase run rates'));
    const grouped = new Map();
    for (const row of rows.filter(row => row.rpo != null && row.balls > 0)) {
      if (!grouped.has(row.format)) grouped.set(row.format, []);
      grouped.get(row.format).push(row);
    }
    for (const [format, phases] of grouped) {
      section.append(el('h4', format));
      const max = Math.max(1, ...phases.map(row => row.rpo));
      const list = el('ul');
      for (const row of phases.slice(0, 24)) {
        const item = el('li');
        const label = `${row.team} · ${humanize(row.phase)} · ${row.rpo} runs per over`;
        const bar = el('meter');
        bar.max = max;
        bar.value = row.rpo;
        bar.setAttribute('aria-hidden', 'true');
        item.append(el('span', label), document.createTextNode(' '), bar);
        list.append(item);
      }
      section.append(list);
    }
    return section;
  }

  async function load(body) {
    const root = document.getElementById('universe-content');
    const status = document.getElementById('universe-status');
    if (!root || !status) return;
    const sequence = ++requestNumber;
    root.replaceChildren();
    status.textContent = 'Loading…';
    const title = document.getElementById('universe-title');
    title.textContent = ({directory:'Competition Universe', rankings:'World Rankings',
      collection:'Collection Book', analytics:'Tournament Analytics'})[body.action] || 'Competition Universe';
    try {
      const data = await request(body);
      if (sequence !== requestNumber) return;
      status.textContent = '';
      const nav = el('nav', null, 'ev-actions');
      nav.setAttribute('aria-label', 'Competition tools');
      if (body.action !== 'directory') nav.append(button('Competitions', () => load({action: 'directory'})));
      if (back && body.action === 'analytics') nav.append(button('Back to tournament', back));
      if (data.capabilities?.rankings) nav.append(button('World Rankings', () => load({action:'rankings'})));
      if (data.capabilities?.collection) nav.append(button('Collection Book', () => load({action:'collection'})));
      if (nav.childElementCount) root.append(nav);
      ({directory:renderDirectory, rankings:renderRankings, collection:renderCollection, analytics:renderAnalytics})[body.action](root, body, data);
    } catch (error) {
      if (sequence !== requestNumber) return;
      status.textContent = error.message;
      root.replaceChildren(button('Try again', () => load(body)), button('Competitions', () => load({action:'directory'})));
    }
  }

  function open(action = 'directory', params = {}, origin = null) {
    back = origin;
    navigate('competition-universe');
    return load({action, ...params});
  }

  return {open};
})();
