(() => {
  const data = JSON.parse(document.getElementById('cmu-match-data').textContent);
  const setText = (selector, value, root = document) => {
    const node = root.querySelector(selector);
    node.textContent = value;
    return node;
  };
  const fitText = (node, width, minimum = 14) => {
    const style = getComputedStyle(node);
    let size = parseFloat(style.fontSize);
    const context = document.createElement('canvas').getContext('2d');
    const measure = () => {
      context.font = `${style.fontStyle} ${style.fontWeight} ${size}px ${style.fontFamily}`;
      return context.measureText(node.textContent).width + (parseFloat(style.letterSpacing) || 0) * node.textContent.length;
    };
    node.style.whiteSpace = 'nowrap';
    while (measure() > width && size > minimum) node.style.fontSize = `${--size}px`;
    while (measure() > width && node.textContent.length > 4) {
      node.textContent = node.textContent.slice(0, -2) + '…';
    }
  };
  const palette = (node, colours) => {
    node.dataset.bound = 'true';
    Object.entries(colours).forEach(([key, value]) => node.style.setProperty(`--${key}`, value));
  };
  const rows = (container, entries) => {
    container.replaceChildren();
    entries.forEach(entry => {
      const row = document.createElement('div');
      row.className = `stat${entry.potm ? ' potm' : ''}${entry.impact ? ' impact' : ''}`;
      const tick = document.createElement('i'); tick.className = 'tick'; row.append(tick);
      const name = document.createElement('span'); name.className = 'pname'; name.textContent = entry.name; row.append(name);
      if (entry.impact) {
        const badge = document.createElement('span'); badge.className = 'impact-mark'; badge.textContent = 'ϟ IMPACT'; row.append(badge);
      }
      const value = document.createElement('span'); value.className = 'runs'; value.textContent = entry.value; row.append(value);
      const extra = document.createElement('span'); extra.className = 'balls'; extra.textContent = entry.extra; row.append(extra);
      [name, value, extra].forEach(node => node.contentEditable = window.CMU_SUMMARY_EDITABLE ? 'true' : 'false');
      container.append(row);
      const reserved = value.offsetWidth + extra.offsetWidth + 48 + (entry.impact ? 126 : 0);
      fitText(name, row.clientWidth - reserved, 16);
    });
  };
  document.title = `${data.tournament} — ${data.match} Summary`;
  setText('.season-ribbon > span', data.tournament);
  setText('.brand-right', data.brand);
  setText('.meta .match', data.match);
  setText('.meta .stadium [contenteditable]', data.stadium.toUpperCase());
  setText('.meta .pitch strong', data.pitch);
  document.querySelectorAll('.innings').forEach((section, index) => {
    const innings = data.innings[index];
    palette(section, innings.palette);
    section.setAttribute('aria-label', `${innings.team} innings`);
    setText('.score-title', innings.team, section);
    const overs = section.querySelector('.overs');
    overs.firstChild.textContent = data.overs_label + ' ';
    setText('.overs span', innings.overs, section);
    setText('.score-chunk span', innings.score, section);
    rows(section.querySelector('.score-half.left'), innings.batters);
    rows(section.querySelector('.score-half.right'), innings.bowlers);
    fitText(section.querySelector('.score-title'), 560, 19);
    fitText(section.querySelector('.score-chunk span'), 205, 28);
    const crest = document.querySelector(index === 0 ? '.crest.left' : '.crest.right');
    palette(crest, innings.palette);
    if (innings.logo) {
      const image = crest.querySelector('img'); image.src = innings.logo; image.alt = `${innings.team} emblem`;
    } else {
      crest.replaceChildren();
      const initials = document.createElement('span'); initials.className = 'crest-initials'; initials.textContent = innings.initials; crest.append(initials);
    }
  });
  fitText(setText('.winner h2', data.winner), 690, 24);
  fitText(setText('.winner h3', data.margin), 720, 22);
  fitText(setText('.potm-name', data.potm), 320, 18);
  const figures = setText('.potm-figures', data.figures);
  if (/^\d+\*?\(\d+\)$/.test(data.figures)) {
    const [runs, balls] = data.figures.split('(');
    figures.textContent = runs + ' ';
    const small = document.createElement('small'); small.textContent = '(' + balls; figures.append(small);
  }
  fitText(figures, 260, 18);
  fitText(document.querySelector('.season-ribbon > span'), 310, 14);
  fitText(document.querySelector('.brand-right'), 260, 14);
  fitText(document.querySelector('.meta .stadium [contenteditable]'), 290, 13);
  fitText(document.querySelector('.meta .match'), 164, 15);
  fitText(document.querySelector('.meta .pitch strong'), 110, 15);
  if (!window.CMU_SUMMARY_EDITABLE) document.querySelectorAll('[contenteditable]').forEach(node => node.contentEditable = 'false');
  document.getElementById('viewport').setAttribute('aria-label', `${data.tournament} match summary, 16 by 9 aspect ratio`);
})();
