/* global api, navigate */
const EventHub = (() => {
  const root = document.getElementById('events-hub-content');
  const heading = document.getElementById('events-hub-title');
  const labels = {live: 'Live Event', moment: 'Cricket Moment', boss: 'Boss Battle',
    tournament_team: 'Tournament Team · Hard', own_roster: 'Own Roster · Hard++',
    coins: 'Coins', gems: 'Gems', quest_points: 'Quest Points'};
  const statusNames = {in_progress: 'In progress', grace: 'In grace', under_review: 'Under review', scheduled: 'Scheduled',
    locked: 'Awaiting qualification', simulating: 'Simulating', technical_void: 'Technical void', forfeited: 'Forfeited', no_result: 'No result'};
  const label = value => labels[value] || String(value).replaceAll('_', ' ');
  const title = status => statusNames[status] || status[0].toUpperCase() + status.slice(1);
  let pending = false;
  let activeEvent = null;
  let clockOffset = 0;
  let countdown = null;
  let screenEpoch = 0;

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function button(text, action) {
    const node = element('button', text, 'hub-button');
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
  }

  function say(text, error = false) {
    const status = document.getElementById('events-hub-status');
    status.textContent = text;
    status.classList.toggle('ev-error', error);
  }

  function date(value, timeZone) {
    const options = {year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric',
      minute: '2-digit', timeZone, timeZoneName: 'short'};
    return new Intl.DateTimeFormat(undefined, options).format(new Date(value));
  }

  function duration(value) {
    const remaining = Math.max(0, Math.ceil((Date.parse(value) - Date.now() - clockOffset) / 1000));
    if (!remaining) return 'Window ended';
    const hours = Math.floor(remaining / 3600);
    const minutes = Math.floor(remaining / 60) % 60;
    return hours ? `${hours}h ${minutes}m remaining` : `${minutes}m ${remaining % 60}s remaining`;
  }

  function timeNode(value, timezone) {
    const node = element('time', date(value, timezone));
    node.dateTime = value;
    return node;
  }

  async function request(path, body = {}) {
    const {ok, data} = await api('/api/webapp/events-hub' + path, body);
    if (!ok || !data?.ok) {
      const error = new Error(data?.message || 'Events are unavailable. Try again.');
      error.conflict = data?.error === 'conflict';
      throw error;
    }
    clockOffset = Date.parse(data.server_time) - Date.now();
    return data;
  }

  async function load(eventId = null) {
    stop();
    const epoch = screenEpoch;
    activeEvent = eventId;
    say('Loading events…');
    root.replaceChildren();
    try {
      const data = await request(eventId ? '/' + eventId : '');
      if (epoch !== screenEpoch) return;
      say('');
      if (data.competition) renderCompetition(data.competition);
      else if (eventId) renderDetail(data.event);
      else { heading.textContent = 'Events Hub'; renderList(data.events); }
    } catch (error) {
      if (epoch !== screenEpoch) return;
      say(error.message, true);
      const controls = [button('Retry', () => load(eventId))];
      if (eventId) controls.push(button('All events', () => load()));
      root.replaceChildren(...controls);
    }
  }

  async function mutate(buttonNode, body) {
    if (pending) return;
    pending = true;
    const eventId = activeEvent;
    const epoch = screenEpoch;
    buttonNode.disabled = true;
    say('Saving…');
    try {
      const response = await request('/action', body);
      if (response.game_id && ['start', 'attempt'].includes(body.action)) {
        openArena(response.game_id);
        return;
      }
      if (epoch === screenEpoch) {
        await load(eventId);
        if (activeEvent === eventId && document.getElementById('screen-events').classList.contains('active')) {
          const next = root.querySelector('.ev-primary');
          if (next) next.focus();
          else {
            heading.tabIndex = -1;
            heading.focus();
          }
        }
      }
    } catch (error) {
      if (epoch === screenEpoch) {
        if (error.conflict) await load(eventId);
        say(error.message, true);
      }
    } finally {
      pending = false;
      buttonNode.disabled = false;
    }
  }

  function action(text, body) {
    return button(text, event => mutate(event.currentTarget, body));
  }

  function renderList(events) {
    if (!events.length) {
      root.append(element('p', 'There are no published events available right now.', 'ev-meta'));
      lobbyEntry(root);

      return;
    }
    lobbyEntry(root);
    const groups = [
      ['My events', events.filter(e => e.joined && !['closed', 'cancelled'].includes(e.availability))],
      ['Featured', events.filter(e => e.featured && !e.joined && !['closed', 'cancelled'].includes(e.availability))],
      ['Paused', events.filter(e => !e.joined && !e.featured && e.availability === 'paused')],
      ['Daily Moments', events.filter(e => !e.joined && !e.featured && e.availability === 'live' && e.cadence === 'daily')],
      ['Weekly Moments', events.filter(e => !e.joined && !e.featured && e.availability === 'live' && e.cadence === 'weekly')],
      ['Live now', events.filter(e => !e.joined && !e.featured && e.availability === 'live' && e.cadence === 'none')],
      ['Upcoming', events.filter(e => !e.joined && !e.featured && e.availability === 'upcoming')],
      ['History', events.filter(e => e.joined && ['closed', 'cancelled'].includes(e.availability))],
    ];
    for (const [group, items] of groups) {
      if (!items.length) continue;
      root.append(element('h2', group));
      for (const item of items) {
        const row = element('article', null, 'ev-row');
        const sub = element('p', `${label(item.kind)} · ${item.format} · ${title(item.availability)}`, 'ev-meta');
        const open = item.availability === 'upcoming';
        const when = element('p', open ? 'Opens ' : 'Closes ', 'ev-meta');
        when.append(timeNode(open ? item.opens_at : item.closes_at, item.timezone));
        row.append(element('h3', item.name), sub, when,
          button(item.joined ? 'Open campaign' : 'View event', () => load(item.id)));
        root.append(row);
      }
    }
  }

  function select(items, descriptor) {
    const field = element('label', descriptor);
    const control = element('select');
    for (const item of items) {
      const option = element('option', item.text);
      option.value = item.value;
      control.append(option);
    }
    field.append(control);
    return {field, control};
  }

  function assets(values) {
    return Object.entries(values).filter(([, value]) => value)
      .map(([key, value]) => `${value.toLocaleString()} ${label(key)}`).join(' · ');
  }

  function rewardsPreview(event) {
    const box = element('details', null, 'ev-panel');
    box.append(element('summary', 'Reward preview'));
    if (event.community_xp) box.append(element('p',
      `${event.community_xp} Community XP for eligible play, subject to the Arena's limits.`, 'ev-meta'));
    for (const tier of ['free', 'diamond']) {
      box.append(element('h3', title(tier)));
      const rows = Object.entries(event.rewards_preview[tier] || {}).filter(([, value]) => assets(value));
      for (const [key, value] of rows) box.append(element('p', `${title(key)} · ${assets(value)}`, 'ev-meta'));
      if (!rows.length) box.append(element('p', 'No wallet rewards for this track.', 'ev-meta'));
    }
    return box;
  }

  function renderDetail(event) {
    heading.textContent = event.name;
    const header = element('header', null, event.joined ? 'ev-campaign-header' : 'ev-panel');
    // A Live Event draws a ground per fixture, so it reports no event-wide pitch.
    const identity = [label(event.kind), event.format, event.pitch && `${event.pitch} pitch`];
    header.append(button('All events', () => load()),
      element('p', identity.filter(Boolean).join(' · '), 'ev-meta'));
    if (event.chapter.name) header.append(element('p',
      `${event.chapter.name} · Moment ${event.chapter.position}`, 'ev-meta'));
    if (event.boss_goal) {
      const goal = element('p', `${event.boss_goal.winners.toLocaleString()} of ${event.boss_goal.target.toLocaleString()} distinct boss winners`);
      header.append(goal);
    }
    if (event.banner_url && !event.joined) {
      const banner = element('img', null, 'ev-banner');
      banner.src = event.banner_url;
      banner.alt = '';
      banner.loading = 'lazy';
      header.append(banner);
    }
    if (event.description && !event.joined) header.append(element('p', event.description));

    const dates = element('p', 'Opens ', 'ev-meta');
    dates.append(timeNode(event.opens_at, event.timezone), ' · Closes ', timeNode(event.closes_at, event.timezone));
    if (!event.joined) {
      header.append(dates);
      const rules = element('details');
      rules.append(element('summary', 'Competition rules'));
      if (event.kind === 'live' && !event.survival) rules.append(element('p', event.rules.ties));
      rules.append(element('p', event.rules.attempts), element('p', event.rules.tier));
      header.append(rules);
    }
    if (event.moment) {
      const m = event.moment;
      const start = `Start at ${m.start_runs}/${m.start_wickets} after ${m.start_over} overs.`;
      const overs = `${m.overs} over${m.overs === 1 ? '' : 's'}`;
      const goal = m.goal_type === 'chase'
        ? (m.side === 'bat' ? `Chase ${m.goal} runs in ${overs}.` : `Defend the target of ${m.target} in ${overs}.`)
        : `${m.side === 'bat' ? 'Score' : 'Take'} ${m.goal} ${m.side === 'bat' ? 'runs' : 'wickets'} in ${overs}.`;
      header.append(element('p', start + ' ' + goal));
    }
    if (!event.joined && event.objectives.length) {
      const objectives = element('details');
      objectives.append(element('summary', 'Objectives'));
      const items = element('ul');
      for (const objective of event.objectives) {
        items.append(element('li', objective.key === 'win' ? objective.title : `${objective.title} · ${objective.threshold}`));
      }
      objectives.append(items);
      header.append(objectives);
    }
    root.append(header);
    if (!event.joined) {
      root.append(rewardsPreview(event));
      if (event.chapter_lock) root.append(element('p', event.chapter_lock, 'ev-meta'));
      if (!event.can_join) return;
      const form = element('section', null, 'ev-panel');
      form.append(element('h3', 'Join event'));
      const modes = select(event.modes.map(mode => ({value: mode, text: label(mode)})), 'Participation');
      const teams = select(event.teams.map(team => ({value: team.key, text: team.name})), 'Team');
      const cutoff = element('p', 'Enroll by ', 'ev-meta');
      cutoff.append(timeNode(event.enrollment_cutoff, event.timezone));
      form.append(teams.field, modes.field, cutoff, element('p', event.kind === 'live'
        ? 'Your team and track are locked after joining. Each fixture has one official result.'
        : `You have ${event.attempt_limit} official attempts. A started attempt can be resumed.`, 'ev-meta'),
      button('Confirm and join', e => mutate(e.currentTarget, {action: 'join', event_id: event.id,
        team: teams.control.value, mode: modes.control.value})));
      root.append(form);
      return;
    }
    header.append(element('p', `${event.team} · ${label(event.mode)}`, 'ev-meta'));
    if (event.trophy) header.append(element('p', event.trophy, 'ev-status'));
    const personal = event.games.filter(game => game.personal);
    const active = personal.find(game => ['in_progress', 'grace', 'paused', 'under_review'].includes(game.status));
    if (active) root.append(activeGame(event, active));
    if (event.kind !== 'live') {
      const attempts = element('section', null, 'ev-panel');
      attempts.append(element('h3', 'Official attempts'),
        element('p', `${event.attempts_used} of ${event.attempt_limit} used · Best stars ${event.best_stars}`, 'ev-meta'));
      if (event.can_attempt) {
        const identifiers = {event_id: event.id, game_id: 0, previous_attempt: personal.length};
        attempts.append(event.moment ? action('Start official attempt', { ...identifiers, action: 'attempt' })
          : xiSetup(event, identifiers));
      }
      root.append(attempts);
    }
    const fixtures = element('section', null, 'ev-panel');
    fixtures.append(element('h3', event.kind === 'live' ? 'Fixtures' : 'Attempt history'));
    for (const game of event.games) {
      if (game.id === active?.id) continue;
      const row = element('div', null, 'ev-row');
      const name = game.home ? `${game.home} v ${game.away}` : `Playoff · Round ${game.round}`;
      row.append(element('h3', event.kind === 'live' ? name : `Attempt ${game.attempt}`),
        element('p', game.result?.winner ? `${title(game.status)} · ${game.result.winner} won` :
          title(game.status), 'ev-status'));
      if (!game.result && game.home) {
        const cutoff = element('p', 'Deadline ', 'ev-meta');
        cutoff.append(timeNode(game.cutoff, event.timezone));
        if (Date.parse(game.opens_at) > Date.now() + clockOffset) {
          const opens = element('p', 'Opens ', 'ev-meta');
          opens.append(timeNode(game.opens_at, event.timezone));
          row.append(opens);
        }
        row.append(cutoff);
      }
      if (game.can_start) row.append(xiSetup(event, {event_id: event.id, game_id: game.id, previous_attempt: 0}));
      if (game.result && ['completed', 'success', 'failed'].includes(game.result.kind)) {
        row.append(button('Open match viewer', () => openArena(game.id)));
      }
      if (game.result) resultDetails(row, game);
      fixtures.append(row);
    }
    if (!event.games.length) fixtures.append(element('p', 'Your official attempts will appear here.', 'ev-meta'));
    root.append(fixtures);
    if (event.table.length) root.append(standings(event.table));
    root.append(progression(event), rewards(event));
  }

  function openArena(gameId) {
    const dev = new URLSearchParams(location.search).get('dev');
    location.assign(`/cricket?event_match_id=${gameId}${dev ? '&dev=' + encodeURIComponent(dev) : ''}`);
  }

  function xiSetup(event, identifiers) {
    const details = element('details');
    details.append(element('summary', 'Match setup'));
    const content = element('div');
    details.append(content);

    let selected = [];
    let setupPending = false;

    function render(setup) {
      const level = {easy: 'Easy', normal: 'Normal', hard: 'Hard', hard_plus: 'Hard++'}[setup.difficulty];
      const info = [setup.format, setup.pitch + ' pitch', setup.venue, setup.weather, level].filter(Boolean);
      const deadline = element('p', 'Start by ', 'ev-meta');
      deadline.append(timeNode(setup.cutoff, event.timezone));
      content.replaceChildren(element('p', `${setup.team} v ${setup.opponent}`),
        element('p', info.join(' · '), 'ev-meta'), deadline);
      for (const goal of setup.objectives) {
        content.append(element('p', goal.key === 'win' ? goal.title : `${goal.title} · ${goal.threshold}`, 'ev-meta'));
      }
      const actions = element('div', null, 'ev-actions');

      const toss = setup.toss;
      if (!toss) {
        actions.append(button('Heads', () => show({call: 'heads'})), button('Tails', () => show({call: 'tails'})));
        content.append(element('h3', '🪙 Toss'), element('p', 'Call the coin.'), actions);
        return;
      }
      if (!toss.decision) {
        actions.append(button('🏏 Bat First', () => show({decision: 'bat'})),
          button('🎳 Bowl First', () => show({decision: 'bowl'})));
        content.append(element('p', `The coin lands on ${toss.coin.toUpperCase()} · You won the toss.`), actions);
        return;
      }

      const winner = toss.human_won ? setup.team : setup.opponent;
      content.append(element('p', `${winner} won the toss and elected to ${toss.decision} first.`));
      const confirmed = setup.confirmed_xi;
      const order = confirmed || (setup.fixed_xi ? setup.default_xi : selected);
      const squad = new Map(setup.squad.map(player => [player.roster_id, player]));
      const players = order.map(id => squad.get(id)).filter(Boolean);
      if (confirmed) {
        content.append(element('h3', '🏏 Match ready'));
        const start = button('Start Match', e => mutate(e.currentTarget, {
          ...identifiers, action: identifiers.game_id ? 'start' : 'attempt',
        }));
        start.classList.add('ev-primary');
        actions.append(start);
        if (!setup.fixed_xi) actions.append(button('✏️ Edit XI', () => show({edit: true})));
      } else {
        content.append(element('h3', `${setup.team} Playing XI Selection`),
          element('p', `Selected: ${players.length}/11`, 'ev-meta'));
        if (!setup.fixed_xi) {
          const picks = element('div', null, 'ev-xi-picks');
          setup.squad.forEach((player, index) => {
            const picked = selected.includes(player.roster_id);
            const control = button(`${picked ? '✅ ' : ''}${index + 1}. ${player.name}`, () => {
              if (!picked && selected.length >= 11) return say('Remove a player before adding another.', true);
              selected = picked ? selected.filter(id => id !== player.roster_id) : [...selected, player.roster_id];
              say('');
              render(setup);
              content.querySelectorAll('.ev-xi-picks button')[index]?.focus();
            });
            control.setAttribute('aria-pressed', String(picked));
            picks.append(control);
          });
          content.append(picks);
          if (!selected.length) {
            actions.append(button('⚡ Use suggested XI', () => show({xi: setup.default_xi})));
          } else {
            actions.append(button('🧹 Clear', () => { selected = []; render(setup); }));
          }
        }
        if (order.length === 11) actions.prepend(button('✅ Confirm XI', () => show({xi: order})));
      }
      if (players.length) {
        const lineup = element('ol');
        for (const player of players) lineup.append(element('li', `${player.name} · ${player.category}`));
        content.append(element('p', 'Batting order'), lineup);
      }
      content.append(actions);
    }

    async function show(update = {}) {
      if (setupPending) return;
      setupPending = true;
      content.querySelectorAll('button').forEach(control => { control.disabled = true; });
      if (!content.childNodes.length) content.append(element('p', 'Loading match setup…', 'ev-meta'));
      try {
        const {setup} = await request('/action', {action: 'setup', ...identifiers, ...update});
        if (setup.editing_xi) selected = setup.editing_xi;
        say('');
        render(setup);
      } catch (error) {
        say(error.message, true);
        if (content.querySelector('button')) {
          content.querySelectorAll('button').forEach(control => { control.disabled = false; });
        } else {
          content.replaceChildren(button('Retry setup', () => show()));
        }
      } finally {
        setupPending = false;
      }
    }
    details.addEventListener('toggle', () => { if (details.open && !content.childNodes.length) show(); });
    return details;
  }

  function activeGame(event, game) {
    const box = element('section', null, 'ev-panel');
    const score = game.score;
    box.append(element('h3', `${game.home} v ${game.away}`),
      element('p', title(game.status), 'ev-status'),
      element('div', `${score.runs}/${score.wickets}`, 'ev-score'),
      element('p', `${score.batting} batting · ${score.overs} overs${score.target ? ' · Target ' + score.target : ''}`, 'ev-meta ev-innings'));
    const clock = element('p', duration(game.cutoff), 'ev-meta');
    countdown = setInterval(() => { clock.textContent = duration(game.cutoff); }, 1000);
    const end = element('p', 'Play until ', 'ev-meta');
    end.append(timeNode(game.cutoff, event.timezone));
    box.append(clock, end);
    const open = button('Open CIPL Arena', () => openArena(game.id));
    open.classList.add('ev-primary');
    box.append(open);
    return box;
  }

  function resultDetails(row, game) {
    if (game.result.reason) row.append(element('p', label(game.result.reason), 'ev-meta'));
    if (game.result.continuation) {
      const m = game.result.continuation;
      row.append(element('p', `Goal progress ${m.progress}/${m.goal} · ${m.balls_left} legal balls left`, 'ev-meta'));
    }
    if (game.result.objectives?.length) {
      const details = element('details');
      details.append(element('summary', `${game.result.stars} stars · Objective results`));
      for (const obj of game.result.objectives) {
        details.append(element('p', `${obj.title} · ${obj.measured}/${obj.threshold} · ${obj.met ? 'Met' : obj.status === 'not_applicable' ? 'Insufficient opportunity' : 'Missed'}`, 'ev-meta'));
      }
      row.append(details);
    }
    if (game.result.innings) row.append(scorecardButton(game, row));
  }

  function dataTable(headers, columns) {
    const wrap = element('div', null, 'ev-scroll');
    wrap.tabIndex = 0;
    const grid = element('table');
    const head = element('thead');
    const header = element('tr');
    for (const label of headers) {
      const cell = element('th', label);
      cell.scope = 'col';
      header.append(cell);
    }
    head.append(header);
    const body = element('tbody');
    for (const values of columns) {
      const row = element('tr');
      for (const value of values) row.append(element('td', value));
      body.append(row);
    }
    grid.append(head, body);
    wrap.append(grid);
    return wrap;
  }

  function standings(rows) {
    const section = element('section', null, 'ev-panel');
    section.append(element('h3', 'Personal standings'),
      dataTable(['Team', 'P', 'W', 'L', 'Pts', 'NRR'],
        rows.map(row => [row.name, row.played, row.won, row.lost, row.points, row.nrr.toFixed(3)])));
    return section;
  }

  function progression(event) {
    const box = element('section', null, 'ev-panel');
    box.append(element('h3', 'Progression'));
    const trigger = button('View stats and track leaderboard', async () => {
      trigger.disabled = true;
      try {
        const result = await request('/progression/' + event.id);
        if (result.leaderboard.length) {
          const rows = result.leaderboard.map(r => [r.rank, r.name, r.wins, r.stars, r.played]);
          box.append(element('h3', `${label(event.mode)} leaderboard`),
            dataTable(['Rank', 'Campaign', 'Wins', 'Stars', 'Played'], rows));
        } else box.append(element('p', 'Official results will appear on the leaderboard.', 'ev-meta'));
        if (result.stats) {
          const {batting, bowling} = result.stats;
          box.append(element('h3', 'Your team stats'));
          if (!batting.length && !bowling.length) {
            box.append(element('p', 'Finish a match to record player statistics.', 'ev-meta'));
          }
          if (batting.length) box.append(dataTable(['Batter', 'Innings', 'Runs', 'High', 'Avg', 'SR'],
            batting.map(r => [r.name, r.innings, r.runs, r.high, r.average ?? '—', r.strike_rate ?? '—'])));
          if (bowling.length) box.append(dataTable(['Bowler', 'Innings', 'Wickets', 'Best', 'Economy'],
            bowling.map(r => [r.name, r.innings, r.wickets, r.best.join('/'), r.economy ?? '—'])));
        }
        trigger.hidden = true;
        const destination = box.querySelector('h3');
        destination.tabIndex = -1;
        destination.focus();
      } catch (error) {
        say(error.message, true);
      } finally {
        trigger.disabled = false;
      }
    });
    box.append(trigger);
    return box;
  }

  function scorecardButton(game, parent) {
    return button('Scorecard', async event => {
      const trigger = event.currentTarget;
      trigger.disabled = true;
      try {
        const {scorecard} = await request('/scorecard/' + game.id);
        const card = element('section');
        for (const innings of scorecard.innings) {
          card.append(element('h3', `${innings.bat_team} · ${innings.runs}/${innings.wickets} (${innings.overs})`),
            dataTable(['Batter', 'Runs', 'Balls', 'Dismissal'],
              innings.batting.map(b => [b.name, b.runs, b.balls, b.how_out])),
            dataTable(['Bowler', 'Overs', 'Runs', 'Wickets'],
              innings.bowling.map(b => [b.name, b.overs, b.runs, b.wickets])));
        }
        parent.append(card);
        trigger.hidden = true;
        const first = card.querySelector('h3');
        if (first) {
          first.tabIndex = -1;
          first.focus();
        }
      } catch (error) {
        say(error.message, true);
      } finally {
        trigger.disabled = false;
      }
    });
  }

  function rewards(event) {
    const box = element('section', null, 'ev-panel');
    box.append(element('h3', 'Rewards'));
    if (event.milestones.length) {
      box.append(element('p', `${event.total_stars} stars earned`, 'ev-meta'));
      for (const milestone of [...event.milestones].sort((a, b) => a.stars - b.stars)) {
        box.append(element('p', `${milestone.stars} stars · ${event.total_stars >= milestone.stars ? 'Unlocked' : 'Locked'}
          · Free ${assets(milestone.free || {}) || '—'} · Diamond ${assets(milestone.diamond || {}) || '—'}`, 'ev-meta'));
      }
    }
    if (event.rewards.some(item => item.status === 'claimable')) box.append(action('Claim all', {
      action: 'claim', event_id: event.id
    }));
    for (const item of event.rewards) {
      const text = element('p', `${assets(item.assets)} · ${title(item.status)} · ${title(item.tier)}`, 'ev-meta');
      if (item.receipt) text.append(` · Receipt #${item.receipt}`);
      if (item.status === 'claimable') {
        text.append(' · Claim by ', timeNode(item.expires_at, event.timezone));
        box.append(text, action('Claim reward', {action: 'claim', event_id: event.id, reward_id: item.id}));
      } else box.append(text);
    }
    if (!event.rewards.length) box.append(element('p', 'Any earned rewards will appear here.', 'ev-meta'));
    box.append(rewardsPreview(event));
    return box;
  }

  function liveAction(text, run) {
    return button(text, async e => {
      if (pending) return;
      const control = e.currentTarget;
      control.disabled = true;
      pending = true;
      try { say(''); await run(); } catch (error) { say(error.message, true); }
      finally { pending = false; control.disabled = false; }
    });
  }

  function lobbyEntry(box) {
    const field = element('label', 'Lobby code');
    const input = element('input');
    input.autocomplete = 'off';
    input.maxLength = 24;
    field.append(input);
    const form = element('details');
    form.append(element('summary', 'Join existing lobby'), field, button('Open lobby', () => openLobby(input.value.trim())));
    box.append(form);
  }

  function competitionArena(fixture) {
    location.assign('/cricket?competition_fixture_id=' + fixture);
  }

  function detailsCard(name, target) {
    const detail = element('details', null, 'ev-panel');
    detail.append(element('summary', name));
    target.append(detail);
    return detail;
  }

  function renderCompetition(event) {
    heading.textContent = event.name;
    const intro = element('section', null, 'ev-panel');
    intro.append(button('All events', () => load()), element('p', `${event.format} · ${event.fixture_count} fixtures · AI ${label(event.difficulty)}`));
    for (const [key, caption] of [['enroll_until', 'Enrollment closes '], ['close', 'Event closes ']]) {
      const line = element('p', caption, 'ev-meta');
      line.append(timeNode(event[key], event.timezone));
      intro.append(line);
    }
    if (event.description) intro.append(element('p', event.description));
    if (event.banner) {
      const banner = element('img', null, 'ev-banner');
      banner.src = event.banner; banner.alt = '';
      intro.append(banner);
    }
    root.append(intro);

    if (!event.joined) {
      if (event.registration_open) {
        for (const mode of event.modes) {
          const choices = detailsCard(mode === 'solo' ? 'Join Solo' : 'Join Multi', root);
          for (const team of event.teams.filter(team => team.eligible)) {
            const card = detailsCard(team.name, choices);
            if (team.crest) {
              const crest = element('img');
              crest.src = team.crest; crest.alt = ''; crest.width = 42; crest.height = 42;
              card.append(crest);
            }
            card.append(element('p', [team.venue, team.ovr + ' OVR'].filter(Boolean).join(' · '), 'ev-meta'));
            for (const player of team.squad) card.append(element('p', `${player.name} · ${player.category} · ${player.rating}`, 'ev-meta'));
            card.append(liveAction('Confirm ' + team.name, async () => {
              await request('/competitions/' + event.id, {action: 'create', mode, team: team.key});
              await load(event.id);
            }));
          }
        }
        lobbyEntry(root);
      } else intro.append(element('p', 'Enrollment is closed.'));
      const objectives = detailsCard(`Tournament objectives · ${event.maximum} stars`, root);
      for (const obj of event.objectives) objectives.append(element('p', `${obj.title} ${obj.comparator || '≥'} ${obj.target} · ${obj.stars} ★`));
      renderRewardRoad(event, 0);
      return;
    }

    if (event.room.status === 'lobby') {
      mountLobby(event.room);
      return;
    }
    const panel = element('section', null, 'ev-panel');
    const team = event.teams.find(t => t.key === event.team);
    panel.append(element('h2', team.name),
      element('p', `${event.my_standing.points} points · NRR ${event.my_standing.nrr.toFixed(3)} · ${event.remaining} fixtures left`));
    if (event.form.length) panel.append(element('p', 'Recent form · ' + event.form.join(' '), 'ev-meta'));
    if (event.trophy) panel.append(element('p', '🏆 ' + event.trophy));
    const next = event.fixtures.find(f => f.personal && !f.result);
    if (next) renderFixture(panel, next, event);
    root.append(panel);
    const fixtures = detailsCard('Fixtures', root);
    for (const fixture of event.fixtures) renderFixture(fixtures, fixture, event);
    const board = detailsCard('Standings', root);
    for (const [index, row] of event.standings.entries()) {
      board.append(element('p', `${index + 1}. ${row.name} · ${row.points} pts · ${row.won}W ${row.lost}L · NRR ${row.nrr.toFixed(3)}`));
    }
    const squad = detailsCard('Squad', root);
    for (const player of team.squad) squad.append(element('p', `${player.name} · ${player.category} · ${player.rating}`));
    const stars = detailsCard(`Objectives · ${event.stars.claimed}/${event.maximum} stars claimed`, root);
    stars.open = true;
    stars.append(element('p', `${event.stars.earned} earned · ${event.stars.unclaimed} ready to claim`));
    const claim = (text, action, id = {}) => liveAction(text, async () => {
      await request('/competitions/' + event.id, {action, ...id});
      await load(event.id);
    });
    if (event.stars.unclaimed) stars.append(claim('Claim All Stars', 'claim_stars'));
    const states = {ready: 'Ready to claim', claimed: 'Claimed', expired: 'Expired', locked: 'Locked', in_progress: 'In progress'};
    for (const obj of event.stars.objectives) {
      stars.append(element('p', `${obj.title} · ${obj.measured}/${obj.target} · ${obj.stars} ★ · ${states[obj.status]}`));
      const progress = element('progress');
      progress.max = 1; progress.value = obj.progress; progress.setAttribute('aria-label', obj.title);
      stars.append(progress);
      if (obj.status === 'ready') stars.append(element('p', 'Claim by ' + date(obj.expires_at, event.timezone), 'ev-meta'),
        claim('Claim Star', 'claim_stars', {objective_id: obj.id}));
    }
    const road = renderRewardRoad(event, event.stars.claimed);
    if (event.tier) road.append(element('p', `Reward tier: ${title(event.tier)} · fixed at first eligibility`, 'ev-meta'));
    if (event.rewards.some(r => r.status === 'claimable')) road.append(claim('Claim All Rewards', 'claim_rewards'));
    for (const reward of event.rewards) {
      const line = element('p', `${assets(reward.assets)} · ${title(reward.status)}`);
      road.append(line);
      if (reward.status === 'claimable') road.append(element('p', 'Claim by ' + date(reward.expires_at, event.timezone), 'ev-meta'),
        claim('Claim Reward', 'claim_rewards', {reward_id: reward.id}));
    }
    const stats = detailsCard('Statistics', root);
    for (const p of event.statistics.batting.slice(0, 15)) stats.append(element('p', `${p.name} · ${p.runs} runs · HS ${p.high}`));
    for (const p of event.statistics.bowling.slice(0, 15)) stats.append(element('p', `${p.name} · ${p.wickets} wickets`));
    const table = detailsCard(`${event.room.kind === 'solo' ? 'Solo' : 'Multiplayer'} leaderboard`, root);
    for (const row of event.leaderboard) table.append(element('p', `${row.rank}. ${row.name} · ${row.stars} ★`));
  }

  function renderRewardRoad(event, claimed) {
    const road = detailsCard('Reward road', root);
    for (const row of event.milestones) road.append(element('p',
      `${row.stars} ★ · ${row.stars <= claimed ? 'Unlocked' : 'Locked'} · Free ${assets(row.free || {}) || '—'} · Diamond ${assets(row.diamond || {}) || '—'}`));
    return road;
  }

  function renderFixture(box, fixture, event) {
    const panel = element('article', null, 'ev-row');
    panel.append(element('h3', `Round ${fixture.round} · ${fixture.home || 'Qualifier'} v ${fixture.away || 'Qualifier'}`));
    panel.append(element('p', fixture.result?.winner ? fixture.result.winner + ' won' : title(fixture.status), 'ev-meta'));
    if (!fixture.result) panel.append(element('p',
      `Opens ${date(fixture.opens_at, event.timezone)} · Deadline ${date(fixture.deadline, event.timezone)}`, 'ev-meta'));
    if (fixture.personal && fixture.status === 'in_progress') {
      panel.append(button('Resume Match', () => competitionArena(fixture.id)));
    } else if (fixture.can_start) {
      const setup = detailsCard('Match setup', panel);
      setup.addEventListener('toggle', () => {
        if (setup.open && setup.childElementCount === 1) showCompetitionSetup(setup, fixture.id).catch(e => say(e.message, true));
      });
    } else if (fixture.personal && fixture.result?.kind === 'completed') {
      panel.append(button('View Scorecard', () => competitionArena(fixture.id)));
    }
    box.append(panel);
  }

  async function showCompetitionSetup(box, fixture, update = {}) {
    const {setup} = await request('/fixture/' + fixture, update);
    if (setup.status === 'in_progress') { competitionArena(fixture); return; }
    const menu = element('div');
    box.replaceChildren(element('summary', 'Match setup'), menu);
    menu.append(element('p', `${setup.team.name} v ${setup.opponent}`));
    const controls = element('div', null, 'ev-actions');
    const op = (text, action, value) => liveAction(text, () => showCompetitionSetup(box, fixture, {action, value, version: setup.version}));
    if (!setup.ready) controls.append(op('Ready', 'ready'));
    else if (!setup.all_ready) controls.append(element('p', 'Waiting for the other captain.'));
    else if (!setup.toss && setup.can_call) controls.append(op('Heads', 'coin', 'heads'), op('Tails', 'coin', 'tails'));
    else if (!setup.toss) controls.append(element('p', 'Waiting for the toss call.'));
    else if (!setup.toss.decision && setup.won_toss) controls.append(op('Bat First', 'elect', 'bat'), op('Bowl First', 'elect', 'bowl'));
    else if (!setup.toss.decision) controls.append(element('p', 'Waiting for the toss winner.'));
    else if (!setup.confirmed_xi) {
      menu.append(element('p', `Coin: ${setup.toss.coin} · Toss winner chose to ${setup.toss.decision}.`));
      controls.append(op(setup.fixed_xi ? 'Confirm XI' : 'Use suggested XI', 'xi', setup.default_xi));
      if (!setup.fixed_xi) {
        const squad = element('div', null, 'ev-xi-picks');
        const selected = [];
        const confirm = op('Confirm XI', 'xi', selected);
        confirm.hidden = true;
        for (const [i, card] of setup.team.squad.entries()) {
          const pick = button(`${i + 1}. ${card.name}`, () => {
            const index = selected.indexOf(card.roster_id);
            if (index >= 0) selected.splice(index, 1);
            else if (selected.length < 11) selected.push(card.roster_id);
            choices.textContent = selected.map(id => setup.team.squad.find(p => p.roster_id === id).name).join(' → ');
            pick.setAttribute('aria-pressed', String(selected.includes(card.roster_id)));
            confirm.hidden = selected.length !== 11;
          });
          pick.setAttribute('aria-pressed', 'false');
          squad.append(pick);
        }
        const choices = element('p', '');
        menu.append(squad, choices);
        controls.append(confirm);
      }
    } else if (setup.all_confirmed) controls.append(op('Start Match', 'start'));
    else controls.append(element('p', 'Waiting for the other Playing XI.'));
    controls.append(liveAction('Refresh', () => showCompetitionSetup(box, fixture)));
    menu.append(controls);
  }

  async function openLobby(invite) {
    try {
      const {lobby} = await request('/lobby', {invite});
      stop();
      activeEvent = lobby.event_id;
      root.replaceChildren(button('All events', () => load()));
      heading.textContent = lobby.name;
      say('');
      mountLobby(lobby, invite);
    } catch (error) { say(error.message, true); }
  }

  function mountLobby(initial, invite = null) {
    let lobby = initial;
    const panel = element('section', null, 'ev-panel');
    root.append(panel);
    const route = () => ({room_id: lobby.id, invite: invite || lobby.invite});
    async function refresh() {
      const response = await request('/lobby', route());
      lobby = response.lobby;
      draw();
    }
    function draw() {
      panel.replaceChildren(element('h2', 'Multiplayer lobby'));
      if (lobby.status !== 'lobby') {
        clearInterval(countdown);
        panel.append(element('p', title(lobby.status)));
        if (lobby.my_team) panel.append(button('Open tournament', () => load(lobby.event_id)));
        return;
      }
      panel.append(element('p', `${lobby.ai_count} AI managers if started now · ${duration(lobby.join_until)}`),
        timeNode(lobby.join_until, lobby.timezone));
      if (lobby.invite) {
        panel.append(element('p', 'Lobby code ' + lobby.invite));
        if (navigator.clipboard) panel.append(button('Copy code', () => navigator.clipboard.writeText(lobby.invite)));
        if (navigator.share) panel.append(button('Share lobby', () => navigator.share(lobby.invite_url ? {url: lobby.invite_url, title: lobby.name} : {text: 'Join my CCM26 Live Event with lobby code ' + lobby.invite})));
      }
      const op = (text, action, team, confirm = false) => liveAction(text, async () => {
        if (confirm && !window.confirm(confirm)) return;
        const result = await request('/lobby', {...route(), action, team, version: lobby.version,
                                                confirmed: Boolean(confirm)});
        lobby = result.lobby;
        if (lobby.status === 'running' && lobby.my_team) await load(lobby.event_id);
        else draw();
      });
      for (const team of lobby.teams) {
        const row = element('p', `${team.name} · ${team.owner || 'Open'}`);
        if (lobby.can_join && !team.owner) row.append(op('Claim seat', 'claim', team.key));
        panel.append(row);
      }
      if (lobby.can_join && lobby.my_team && !lobby.is_host) panel.append(op('Release my team', 'release'));
      if (lobby.is_host) {
        if (lobby.can_join && lobby.early_start) panel.append(op('Start Now', 'start', null, `Start with ${lobby.ai_count} AI managers?`));
        panel.append(op('Close lobby', 'cancel', null, 'Close registration and release all seats?'));
      }
    }
    draw();
    const epoch = screenEpoch;
    countdown = setInterval(() => {
      if (!pending && epoch === screenEpoch && lobby.status === 'lobby') refresh().catch(e => say(e.message, true));
    }, 6000);
  }

  function stop() {
    ++screenEpoch;
    clearInterval(countdown);
    countdown = null;
    activeEvent = null;
  }

  return {open: load, openLobby, stop};
})();
