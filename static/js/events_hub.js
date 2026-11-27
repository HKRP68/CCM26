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
      if (eventId) renderDetail(data.event);
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
      return;
    }
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

    async function show(call = null) {
      content.replaceChildren(element('p', 'Loading match setup…', 'ev-meta'));
      try {
        const {setup} = await request('/action', {action: 'setup', ...identifiers, call});
        const level = {easy: 'Easy', normal: 'Normal', hard: 'Hard', hard_plus: 'Hard++'}[setup.difficulty];
        const info = [setup.format, setup.pitch + ' pitch', setup.venue, setup.weather, level].filter(Boolean);
        const deadline = element('p', 'Start by ', 'ev-meta');
        deadline.append(timeNode(setup.cutoff, event.timezone));
        content.replaceChildren(element('p', `${setup.team} v ${setup.opponent}`),
          element('p', info.join(' · '), 'ev-meta'), deadline);
        for (const goal of setup.objectives) {
          content.append(element('p', goal.key === 'win' ? goal.title : `${goal.title} · ${goal.threshold}`, 'ev-meta'));
        }
        if (!setup.toss) {
          content.append(button('Heads', () => show('heads')), button('Tails', () => show('tails')));
          return;
        }
        const toss = setup.toss;
        const label = toss.human_won ? 'You won the toss.' : `${setup.opponent} elected to ${toss.decision} first.`;
        content.append(element('p', `${toss.coin.toUpperCase()} · ${label}`));
        const options = setup.squad.map(p => ({
          value: p.roster_id, text: `${p.name} · ${p.version || 'Base'} · ${p.category} · ${p.rating}`,
        }));
        const picks = [];
        const captain = select(options.filter(p => setup.default_xi.includes(p.value)), 'Captain');
        if (!setup.fixed_xi) {
          const field = element('fieldset');
          field.append(element('legend', 'Playing XI in batting order'));
          const updateCaptain = () => {
            const selected = picks.map(p => Number(p.control.value));
            const previous = captain.control.value;
            captain.control.replaceChildren();
            for (const id of new Set(selected)) {
              const player = options.find(p => p.value === id);
              if (!player) continue;
              const option = element('option', player.text);
              option.value = id;
              captain.control.append(option);
            }
            if ([...captain.control.options].some(option => option.value === previous)) captain.control.value = previous;
          };
          for (let position = 0; position < 11; position++) {
            const pick = select(options, `Position ${position + 1}`);
            pick.control.value = setup.default_xi[position];
            pick.control.onchange = updateCaptain;
            picks.push(pick);
            field.append(pick.field);
          }
          field.append(button('Quick XI', () => {
            picks.forEach((pick, position) => { pick.control.value = setup.default_xi[position]; });
            updateCaptain();
          }));
          content.append(field);
        }
        content.append(captain.field);
        for (const decision of toss.human_won ? ['bat', 'bowl'] : [toss.decision]) {
          const text = toss.human_won ? `Start · ${decision === 'bat' ? 'Bat' : 'Bowl'} first` : 'Start match';
          content.append(button(text, e => {
            const xi = picks.length ? picks.map(p => Number(p.control.value)) : setup.default_xi;
            if (new Set(xi).size !== 11) return say('Choose 11 distinct players to start.', true);
            const action = identifiers.game_id ? 'start' : 'attempt';
            return mutate(e.currentTarget, {...identifiers, action, xi, decision, captain_id: Number(captain.control.value)});
          }));
        }
      } catch (error) {
        content.replaceChildren(element('p', error.message, 'ev-error'), button('Retry setup', () => show()));
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

  function stop() {
    ++screenEpoch;
    clearInterval(countdown);
    countdown = null;
    activeEvent = null;
  }

  return {open: load, stop};
})();
