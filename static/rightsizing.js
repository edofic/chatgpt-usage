/* Account-wide plan comparisons. Settings and annotations stay in this browser. */
(() => {
  'use strict';
  const root = document.getElementById('rightsizing');
  if (!root) return;
  root.innerHTML = `
    <div class="rs-heading"><div><div class="kicker">Subscription fit · all devices</div>
    <h2>Compare plans</h2></div><button type="button" id="rs-close">Close</button></div>
    <p class="rs-subtitle">Would another plan cover your work, when you want to do it? Replay your recorded allowance usage with different weekly and five-hour limits.</p>
    <div class="rs-controls"><label>History<select id="rs-period"><option value="7">Last 7 days</option><option value="30" selected>Last 30 days</option><option value="60">Last 60 days</option><option value="90">Last 90 days</option><option value="0">All history</option></select></label><label>Keep in reserve<select id="rs-reserve"><option value="0">0% · use it all</option><option value="10" selected>10% headroom</option><option value="20">20% headroom</option><option value="30">30% headroom</option></select></label></div>
    <details class="rs-settings"><summary>Allowance assumptions</summary>
      <div class="rs-fields">
        <label>Your recorded plan, × Plus<input id="rs-current" type="number" min="0.1" max="1000" step="any" value="5" required></label>
        <label>Plus five-hour block, % of week<input id="rs-five" type="number" min="1" max="100" step="any" value="15" required></label>
        <label>First Pro allowance, × Plus<input id="rs-pro0" type="number" min="0.1" max="1000" step="any" value="5" required></label>
        <label>Second Pro allowance, × Plus<input id="rs-pro1" type="number" min="0.1" max="1000" step="any" value="10" required></label>
        <label>Third Pro allowance, × Plus<input id="rs-pro2" type="number" min="0.1" max="1000" step="any" value="25" required></label>
      </div>
      <p class="rs-note">Editable comparison profiles, not detected billing tiers. Starting calibration: your plan is 5× Plus, and a Plus five-hour block is 15% of its week. Pro profiles assume no five-hour cap. Verify your allowances before relying on a recommendation. Use a period with a consistent recorded plan.</p>
      <p class="rs-note" id="rs-calibration"></p>
    </details>
    <div id="rs-feedback" class="rs-pending" role="status">Loading comparison…</div>
    <div id="rs-results" hidden>
      <div id="rs-verdict" class="rs-verdict"></div>
      <div id="rs-plans" class="rs-plans" aria-label="Choose a plan to inspect"></div>
      <div class="rs-detail"><div class="rs-stat"><h3>Weekly capacity</h3><b id="rs-week-value"></b><p id="rs-week-detail" class="rs-note"></p></div><div class="rs-stat"><h3>Five-hour interruptions</h3><b id="rs-session-value"></b><p id="rs-session-detail" class="rs-note"></p></div></div>
      <p id="rs-schedule" class="rs-note"></p>
      <div class="rs-weeks"><h3>Complete seven-day spans</h3><div id="rs-chart"></div><p class="rs-note">Overlapping 168-hour spans. Weekly fit checks every observed increase; the chart shows daily spans, the latest span, and the busiest. Dashed line: budget after reserve.</p></div>
      <details class="rs-sessions"><summary>Sessions that would encounter the five-hour cap</summary><div id="rs-sessions"></div></details>
      <details class="rs-exclusions"><summary>Exclude exceptional usage <span id="rs-exclusion-count"></span></summary>
        <p class="rs-note">Review the busiest days below, or mark a precise range. Exclusions affect this comparison only. No activity is excluded automatically, and model names cannot be inferred from quota readings.</p>
        <div id="rs-candidates" class="rs-candidates"></div>
        <form id="rs-exclusion-form"><div class="rs-fields"><label>From<input id="rs-from" type="datetime-local" required></label><label>Until<input id="rs-until" type="datetime-local" required></label><label class="rs-wide">Reason<input id="rs-reason" maxlength="160" value="Burn before announced reset" required></label></div><button type="submit">Exclude this range</button></form>
        <div id="rs-exclusion-error" class="rs-error" role="alert"></div>
        <div id="rs-exclusion-list" class="rs-exclusion-list"></div>
        <button id="rs-clear" type="button">Restore all usage</button>
        <p class="rs-note">Ranges use your browser’s time zone. An increase is excluded when its collection timestamp falls inside the range. The original observation period stays the same, including excluded days.</p>
      </details>
      <details class="rs-method"><summary>Evidence and limitations</summary><p id="rs-evidence"></p><p id="rs-quality"></p><p>This comparison uses account-wide quota readings, including other devices. It assumes the allowance ratios stayed constant throughout the selected history. It cannot identify models, extra-credit usage after an exhausted meter, or activity between unobserved resets.</p><p>Activity is placed at collection timestamps. Five-hour windows start on the first observed increase after the previous block ends; actual first-request times can be earlier. Whole-point readings can be coarse relative to a small block, so session and wait estimates are approximate.</p><p>The replay starts with fresh allowances and keeps the original activity times. Both limits share the same admitted work. Denied work is not queued. The reserve reduces the usable weekly budget. Historical early resets are ignored; the first existing usage balance is excluded. Downward corrections within a cycle do not create new consumption.</p><p>Settings and exclusions are saved only in this browser, separately for each account and quota bucket. <a href="https://learn.chatgpt.com/docs/pricing" target="_blank" rel="noopener noreferrer">Check current OpenAI plan guidance ↗</a></p></details>
    </div>`;

  const $ = id => document.getElementById(`rs-${id}`);
  const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const date = at => new Date(at * 1000).toLocaleString([], {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
  const dayLabel = at => new Date(at * 1000).toLocaleDateString([], {month:'short', day:'numeric'});
  const amount = value => Number(value).toLocaleString([], {maximumFractionDigits:1});
  const wait = seconds => seconds >= 86400 ? `${Math.floor(seconds/86400)}d ${Math.round(seconds%86400/3600)}h` : seconds < 3600 ? `${Math.round(seconds / 60)}m` : `${amount(seconds / 3600)}h`;
  let report = null, scope = null, config = null, selected = 'plus', sequence = 0, timer, storageAvailable = true;
  const statuses = {fits:'Fits observed usage',too_small:'Weekly budget too small',interruptions:'Fits week · session pauses',insufficient:'More history needed'};

  function readControls() {
    return {...config, current_multiple:Number($('current').value), five_hour_percent:Number($('five').value),
      pro_multiples:[0,1,2].map(i => Number($(`pro${i}`).value)),
      reserve_percent:Number($('reserve').value), period_days:Number($('period').value),
      timezone:Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'};
  }
  function setControls() {
    $('current').value = config.current_multiple;
    $('five').value = config.five_hour_percent;
    [0,1,2].forEach(i => $(`pro${i}`).value = config.pro_multiples[i]);
    $('period').value = config.period_days;
    $('reserve').value = config.reserve_percent;
  }
  function save() {
    if (!scope) return;
    try { localStorage.setItem(`chatgpt-usage-rightsizing-${scope}`, JSON.stringify({config,selected})); }
    catch (_) { storageAvailable = false; }
  }
  function plot(plan) {
    if (!report.weeks.length) { $('chart').textContent = 'At least seven days of history are needed for a complete weekly comparison.'; return; }
    const width=640, height=190, left=40, right=8, top=16, bottom=30;
    const values=report.weeks.map(w => w.amount/plan.weekly_capacity*100);
    const budget=100-report.settings.reserve_percent;
    const maximum=Math.max(110,...values)*1.1, chartHeight=height-top-bottom;
    const y=v => top+chartHeight*(1-v/maximum), slot=(width-left-right)/values.length;
    let svg=`<svg class="rs-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Seven-day demand as a percentage of ${escape(plan.name)} weekly allowance"><line x1="${left}" x2="${width-right}" y1="${y(budget)}" y2="${y(budget)}" stroke="var(--muted)" stroke-dasharray="4 4"/><text x="0" y="${y(budget)+4}" font-size="10" fill="var(--muted)">${budget}%</text>`;
    report.weeks.forEach((w,i) => { svg+=`<rect x="${left+i*slot+1}" y="${y(values[i])}" width="${Math.max(1,slot-2)}" height="${chartHeight*(values[i]/maximum)}" fill="${values[i]>budget?'var(--warning)':'var(--accent)'}"><title>${escape(dayLabel(w.start))}–${escape(dayLabel(w.end))}: ${amount(values[i])}% of weekly allowance</title></rect>`; });
    svg+=`<text x="${left}" y="${height-5}" fill="var(--muted)" font-size="11">${escape(dayLabel(report.weeks[0].start))}</text><text x="${width-right}" y="${height-5}" text-anchor="end" fill="var(--muted)" font-size="11">${escape(dayLabel(report.weeks.at(-1).end))}</text></svg>`;
    $('chart').innerHTML=svg;
  }
  function render() {
    const history=report.history;
    $('results').hidden=!history;
    if (!history) { $('feedback').textContent='Collect weekly quota readings to compare subscription plans.'; return; }
    const plan=report.plans.find(p => p.id===selected) || report.plans[0];
    selected=plan.id;
    const suggestion=report.plans.find(p => p.id===report.suggested_plan);
    const verdict=$('verdict');
    let title, detail;
    if (!history.total_points || !history.complete_week_spans) {
      title='More observed usage is needed'; detail='Keep collecting for at least seven days with recorded activity before using a plan recommendation.';
    } else if (suggestion) {
      title=`${suggestion.name} is the smallest tested allowance that fits`;
      detail=`It covers every tested seven-day span with ${report.settings.reserve_percent}% weekly reserve and no estimated five-hour overrun. This is conditional on your allowance assumptions.`;
    } else {
      title='None of these profiles fits the observed workload'; detail='Inspect the weekly demand, adjust your allowance assumptions, or mark exceptional activity to compare a recurring workload.';
    }
    if (history.stale || history.gap_count) detail+=' Some readings are stale or have gaps; treat this as provisional.';
    verdict.classList.toggle('warn',!suggestion || history.stale || history.gap_count>0);
    verdict.replaceChildren(); const strong=document.createElement('strong'); strong.textContent=title;
    const sub=document.createElement('span'); sub.textContent=detail; verdict.append(strong,sub);
    $('plans').replaceChildren();
    for (const p of report.plans) {
      const button=document.createElement('button'); button.type='button'; button.className='rs-plan'; button.setAttribute('aria-pressed',p.id===selected);
      button.innerHTML=`<strong>${escape(p.name)}</strong><span class="rs-fit ${p.status==='fits'?'':'warn'}">${statuses[p.status]}</span><small>${p.average_week_percent===null?'No activity':amount(p.average_week_percent)+'% / week on average'}</small><small>${p.five_hour_capacity===null?'No five-hour cap':amount(p.five_hour_capacity)+' recorded plan points / 5h'}</small>`;
      button.addEventListener('click',()=>{selected=p.id;save();render();}); $('plans').append(button);
    }
    $('week-value').textContent=plan.peak_week_percent===null?'Need 7 days':`${amount(plan.peak_week_percent)}%`;
    $('week-detail').textContent=plan.peak_week_percent===null?'The average alone cannot establish weekly fit.':`Busiest complete seven-day span. Your usable weekly budget is ${100-report.settings.reserve_percent}% after reserve. Average demand: ${amount(plan.average_week_percent)}% per week.`;
    $('session-value').textContent=plan.five_hour_capacity===null?'No cap':`${plan.sessions.over_limit} / ${plan.sessions.windows}`;
    $('session-detail').textContent=plan.five_hour_capacity===null?'This Pro profile has no five-hour limit. The weekly budget still applies.':'Estimated active five-hour blocks above the cap, considered independently of weekly exhaustion.';
    $('schedule').textContent=plan.replay.covered_percent===null?'No retained activity to replay.':`Keeping your original activity times: approximately ${amount(plan.replay.covered_percent)}% of consumption fits both budgets. ${amount(plan.replay.blocked_points)} recorded plan points would need additional allowance or rescheduling${plan.replay.max_wait_seconds?', with a longest estimated wait of '+wait(plan.replay.max_wait_seconds):''}. Denied work is not carried forward.`;
    $('calibration').textContent=`One Plus week = ${amount(100/report.settings.current_multiple)} points of your recorded plan. One Plus five-hour block ≈ ${amount(report.settings.five_hour_percent/report.settings.current_multiple)} points.`;
    $('sessions').replaceChildren();
    if (!plan.sessions.examples.length) $('sessions').textContent=plan.five_hour_capacity===null?'No five-hour cap for this profile.':'No estimated five-hour overruns in the retained history.';
    else {
      const ol=document.createElement('ol');
      for (const session of plan.sessions.examples) { const li=document.createElement('li');li.textContent=`${date(session.start)}: ${amount(session.multiple)}× a block’s allowance. First excess observed ${date(session.first_blocked_at)}; about ${wait(session.wait_seconds)} until that block ends.`;ol.append(li); } $('sessions').append(ol);
    }
    $('evidence').textContent=`${date(history.start)}–${date(history.end)} · ${amount(history.days)} observed days · ${history.samples.toLocaleString()} readings. ${amount(history.original_points)} recorded plan points before exclusions; ${amount(history.excluded_points)} removed; ${amount(history.total_points)} retained. Early reset boundaries in the source history: ${history.resets}.`;
    $('quality').textContent=`${history.gap_count} collection gaps longer than 30 minutes${history.gap_count?' (largest '+wait(history.largest_gap_seconds)+')':''}. ${history.corrections} downward correction readings. ${history.stale?'The latest reading is over an hour old.':'The latest reading is recent.'} No model attribution is used.`;
    $('exclusion-count').textContent=`(${config.exclusions.length})`;
    $('candidates').replaceChildren();
    for (const day of [...report.days].filter(d => d.amount>0).sort((a,b)=>b.amount-a.amount).slice(0,5)) {
      const row=document.createElement('div');row.className='rs-row';
      const span=document.createElement('span');span.textContent=`${dayLabel(day.start)} · ${amount(day.amount)} recorded plan points`;
      const small=document.createElement('small');small.textContent=`${amount((day.amount-day.excluded)/plan.weekly_capacity*100)}% of ${plan.name} weekly allowance retained`;span.append(small);
      const button=document.createElement('button');button.type='button';button.textContent='Exclude day';button.disabled=day.excluded>=day.amount;
      button.setAttribute('aria-label',`Exclude all usage on ${day.date}`);
      button.addEventListener('click',()=>addExclusion(day.start,day.end,`Exceptional day · ${day.date}`));row.append(span,button);$('candidates').append(row);
    }
    $('exclusion-list').replaceChildren();
    config.exclusions.forEach((x,i)=>{const row=document.createElement('div');row.className='rs-row';const span=document.createElement('span');span.textContent=x.label;const small=document.createElement('small');small.textContent=`${date(x.start)}–${date(x.end)}`;span.append(small);const button=document.createElement('button');button.type='button';button.textContent='Remove';button.setAttribute('aria-label',`Remove exclusion ${x.label}`);button.onclick=()=>{config.exclusions.splice(i,1);update();};row.append(span,button);$('exclusion-list').append(row);});
    $('clear').disabled=!config.exclusions.length;
    plot(plan);
    $('feedback').textContent=`${history.stale?'Latest reading is stale. ':''}${config.exclusions.length?'Exclusions applied. ':''}${storageAvailable?'Assumptions and exclusions saved in this browser.':'Browser storage unavailable; changes last for this visit.'}`;
  }
  async function request(method, body) {
    const response=await fetch('/api/rightsizing',{method,cache:'no-store',...(body?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
    const data=await response.json(); if (!response.ok) throw new Error(data.error || 'Comparison unavailable'); return data;
  }
  async function update() {
    clearTimeout(timer); const mine=++sequence;
    const inputs=[...root.querySelectorAll('.rs-settings input')];
    if (inputs.some(input=>!input.checkValidity())) { $('feedback').textContent='Enter valid allowance values to update the comparison.';return; }
    config=readControls();$('feedback').textContent='Comparing recorded usage…';
    try {
      const next=await request('POST',config); if (mine!==sequence) return;
      if (next.history && next.history.scope!==scope) { await initialize();return; }
      report=next;config=next.settings;save();render();
    } catch (error) { if (mine===sequence) $('feedback').textContent=error.message; }
  }
  function addExclusion(start,end,label) {
    if (config.exclusions.length>=200) { $('exclusion-error').textContent='Remove an exclusion before adding another (limit 200).';return; }
    config.exclusions.push({start,end,label});$('exclusion-error').textContent='';update();
  }
  $('exclusion-form').addEventListener('submit',event=>{
    event.preventDefault();const start=new Date($('from').value).getTime()/1000,end=new Date($('until').value).getTime()/1000;
    if (!Number.isFinite(start)||!Number.isFinite(end)||end<=start) { $('exclusion-error').textContent='Choose an end time after the start.';return; }
    addExclusion(start,end,$('reason').value.trim() || 'Exceptional usage');
  });
  $('clear').onclick=()=>{config.exclusions=[];update();};
  ['period','reserve','current','five','pro0','pro1','pro2'].forEach(id=>$(id).addEventListener('change',()=>{clearTimeout(timer);timer=setTimeout(update,150);}));
  async function initialize() {
    const mine=++sequence;
    try {
      const initial=await request('GET');if (mine!==sequence) return;
      scope=initial.history?.scope || null;config=initial.settings;report=initial;
      try { const saved=scope && JSON.parse(localStorage.getItem(`chatgpt-usage-rightsizing-${scope}`));if(saved?.config && typeof saved.config==='object') {config={...config,...saved.config};selected=saved.selected || 'plus';} }
      catch (_) { storageAvailable=false; }
      setControls();
      const next=await request('POST',readControls());if(mine!==sequence) return;
      report=next;config=next.settings;render();
    } catch (error) { if (mine===sequence) { $('feedback').textContent=`Could not load comparison: ${error.message}`;const retry=document.createElement('button');retry.textContent='Retry';retry.onclick=initialize;$('feedback').append(' ',retry); } }
  }
  const link=document.getElementById('compare-plans');
  let started=false;
  function show(open, focus=false) {
    root.hidden=!open;
    link?.setAttribute('aria-expanded',String(open));
    if (open) {
      if (!started) { started=true;initialize(); }
      else if (config&&scope) update();
      if (focus) { root.scrollIntoView({block:'start'});root.focus({preventScroll:true}); }
    } else {
      clearTimeout(timer);
      if (focus) link?.focus();
    }
  }
  link?.addEventListener('click',event=>{
    event.preventDefault();
    history.replaceState(null,'',`${location.pathname}${location.search}#rightsizing`);
    show(true,true);
  });
  $('close').addEventListener('click',()=>{
    if(location.hash==='#rightsizing') history.replaceState(null,'',`${location.pathname}${location.search}`);
    show(false,true);
  });
  window.addEventListener('hashchange',()=>show(location.hash==='#rightsizing'));
  window.addEventListener('usage:updated',()=>{if(!root.hidden&&config&&scope){clearTimeout(timer);timer=setTimeout(update,300);}});
  show(location.hash==='#rightsizing');
})();
