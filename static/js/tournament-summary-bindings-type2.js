(() => {
  const data = JSON.parse(document.getElementById('cmu-match-data').textContent);
  const svg = document.querySelector('svg.graphic');
  const ns = 'http://www.w3.org/2000/svg';
  const node = key => svg.querySelector(`[data-bind="${key}"]`);
  const put = (key, value) => {const el = node(key); el.textContent = value; return el;};
  const fit = (el, width, minimum=16) => {
    let size = parseFloat(getComputedStyle(el).fontSize);
    while (el.getComputedTextLength() > width && size > minimum) el.style.fontSize = `${--size}px`;
    while (el.getComputedTextLength() > width && el.textContent.length > 4) el.textContent = el.textContent.slice(0,-2)+'…';
  };
  const make = (tag, attrs, text) => {
    const el = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([k,v]) => el.setAttribute(k,v));
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const gradient = (id, colours) => {
    document.getElementById(id).querySelectorAll('stop').forEach((stop,i,stops) => {
      stop.setAttribute('stop-color',colours[Math.min(colours.length-1,Math.round(i*(colours.length-1)/(stops.length-1||1)))]);
    });
  };
  put('header', `${data.tournament} • ${data.match}`);
  put('stadium',data.stadium.toUpperCase());put('pitch',data.pitch);
  fit(node('header'),520,17);fit(node('stadium'),345,13);fit(node('pitch'),275,13);
  svg.querySelector('#impact-highlight')?.remove();
  data.innings.forEach((innings,index) => {
    const key = index ? 'teamB' : 'teamA', p = innings.palette;
    const group = svg.querySelector(`g[clip-path="url(#${index?'botClip':'topClip'})"]`);
    gradient(index?'seaHead':'wineHead',[p['header-top'],p['header-mid'],p['header-bottom']]);
    gradient(index?'seaScore':'burgScore',[p['header-top'],p['header-mid'],p['header-bottom']]);
    gradient(index?'sea':'wine',[p.body,p.body,p.body]);
    const rim=svg.querySelector(`:scope > polygon[points^="125,${index?527:199}"]`);
    rim.style.filter=`drop-shadow(0 0 4px ${p.light})`;
    group.querySelector('path[fill="#fff0ad"],path[fill="#a3fff0"]').setAttribute('fill',p['header-mid']);
    group.querySelectorAll('path[stroke]').forEach(el=>el.setAttribute('stroke',p.light));
    const lines = innings.team.split(/\s+/);
    let split = 1;
    for(let i=1;i<lines.length;i++) if(Math.abs(lines.slice(0,i).join(' ').length-lines.slice(i).join(' ').length)<Math.abs(lines.slice(0,split).join(' ').length-lines.slice(split).join(' ').length)) split=i;
    put(`${key}.nameTop`, lines.length===1 ? innings.team : lines.slice(0,split).join(' '));
    put(`${key}.nameBottom`, lines.length===1 ? '' : lines.slice(split).join(' '));
    if(lines.length===1) node(`${key}.nameTop`).setAttribute('y',index?597:267);
    ['nameTop','nameBottom'].forEach(part=>{const el=node(`${key}.${part}`);el.style.fill=p['on-team'];fit(el,390,20);});
    put(`${key}.overs`,`${innings.overs} ${data.overs_label}`).style.fill=p['on-team'];
    put(`${key}.score`,innings.score).style.fill=p['on-team'];
    fit(node(`${key}.overs`),255,20);fit(node(`${key}.score`),300,35);
    const image = svg.querySelector(`image[x="${index?124:126}"][y="${index?529:202}"]`);
    if(innings.logo) {image.setAttribute('href',innings.logo);image.setAttribute('preserveAspectRatio','xMidYMid meet');}
    else {image.replaceWith(make('text',{x:194,y:index?606:279,'text-anchor':'middle',fill:p['on-team'],'font-size':48,'font-weight':900},innings.initials));}
    ['batting','bowling'].forEach(kind => {
      innings[kind==='batting'?'batters':'bowlers'].forEach((row,i) => {
        const prefix=`${key}.${kind}.${i}`, name=put(`${prefix}.0`,row.name);
        put(`${prefix}.1`,kind==='bowling'?row.value.replace('/','-'):row.value).style.fill=p.number;
        put(`${prefix}.2`,row.extra);
        name.style.fill='#fff';name.classList.remove('bright');
        fit(node(`${prefix}.1`),115,19);fit(node(`${prefix}.2`),90,17);
        const nameX=Number(name.getAttribute('x'));
        const valueStart=node(`${prefix}.1`).getBBox().x;
        const badgeX=Math.min(nameX+238,valueStart-168);
        fit(name,row.impact?badgeX-nameX-12:valueStart-nameX-20,17);
        if(row.impact||row.potm) {
          const y=Number(name.getAttribute('y')), x=kind==='batting'?150:834;
          const highlight=make('g',{'data-player-highlight':row.impact?'impact':'potm'});
          highlight.style.filter=`drop-shadow(0 0 3px ${row.impact?'#47ff34':'#e7b948'})`;
          highlight.append(make('rect',{x,y:y-31,width:kind==='batting'?623:565,height:39,rx:14,fill:row.impact?'#073d23':'#43320a','fill-opacity':.92,stroke:row.impact?'#64ff47':'#ffe17f','stroke-width':2}));
          if(row.impact) {
            highlight.append(make('rect',{x:badgeX,y:y-27,width:150,height:29,rx:8,fill:'#103a20',stroke:'#bcff61'}));
            highlight.append(make('text',{x:badgeX+75,y:y-7,'text-anchor':'middle',fill:'#f1ffc4','font-size':15,'font-weight':900},'ϟ IMPACT PLAYER'));
          }
          if(row.potm) highlight.append(make('text',{x:Number(name.getAttribute('x'))-27,y,fill:'#ffe17f','font-size':27},'♛'));
          name.parentNode.insertBefore(highlight,name.parentNode.firstChild);
        }
      });
    });
  });
  const winnerSide=data.innings.find(innings=>innings.team===data.winner)||data.innings[0];
  gradient('result',[winnerSide.palette.body,winnerSide.palette.body]);
  put('winner',[data.winner,data.margin.replace(/^WIN BY/,'WON BY')].filter(Boolean).join(' '));fit(node('winner'),1120,25);
  put('potm',data.potm);fit(node('potm'),310,19);
  const metrics=Object.fromEntries(data.metrics.map(([value,label])=>[label,value]));
  put('potmBat',metrics.BATTING==='—'?'':metrics.BATTING.replace('(',' ('));
  put('potmBowl',metrics.BOWLING==='—'?'':metrics.BOWLING.replace('/','-')+(metrics.OVERS&&metrics.OVERS!=='—'?` (${metrics.OVERS})`:''));
  const figures=node('potmBat').parentNode;
  figures.querySelector('tspan[fill="#fff"]').textContent=metrics.BATTING!=='—'&&metrics.BOWLING!=='—'?'  &  ':'';
  fit(figures,350,17);
  if(!node('potmBat').textContent&&!node('potmBowl').textContent) node('potmBat').textContent='—';
  if(window.CMU_SUMMARY_EDITABLE) svg.querySelectorAll('[data-bind]').forEach(el=>{
    el.style.cursor='text';
    el.addEventListener('dblclick',()=>{const value=window.prompt('Edit preview text',el.textContent);if(value!==null)el.textContent=value;});
  });
  document.title=`${data.tournament} — ${data.match} — Spacial Type 2`;
  svg.setAttribute('aria-label',`${data.tournament} ${data.match} summary, Spacial Type 2`);
})();
