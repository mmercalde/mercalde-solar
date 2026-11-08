/* History section of the Pi5 dashboard.
 *
 * Loaded on demand when the History section is opened, after uPlot. Talks only
 * to /history/api/* (proxied by the Pi5 to the KAMRUI's history_api.py) and to
 * /config for the generator thresholds. Every time is shown in America/Tijuana
 * on a 12-hour clock, whatever zone the viewer's device is in.
 *
 * Day view: small multiples sharing one time axis (voltage, current, SOC,
 * power, solar by array), cursor and zoom synced across them. Generator runs
 * are shaded bands, sunrise/sunset dashed verticals, thresholds dashed
 * horizontals on the voltage panel. A second date overlays that day, dashed.
 * Energy view: daily bars for the last 30 days or a chosen month.
 */
(function () {
  'use strict';
  var TZ = 'America/Tijuana';
  var root = document.getElementById('histRoot');
  if (!root || !window.uPlot) return;

  /* Colors: validated on the dashboard's dark panel (#111823) with the dataviz
     validator. Lines on one panel never exceed three hues; the battery series
     on the power panel is a filled area, its secondary encoding. */
  var C = {
    solar: '#c98500', load: '#3987e5', gen: '#d55181', batt: '#199e70',
    mppt80: '#d95926', south: '#3987e5', west: '#199e70',
    mep: '#9085e9', kub: '#d55181',
    v: '#199e70', a: '#3987e5', soc: '#c98500', usable: '#199e70',
    axis: '#8fa0b3', grid: 'rgba(255,255,255,0.07)', txt: '#e8eef5', dim: '#8fa0b3'
  };
  var GEN_NAME = { mep: 'MEP-803A', kubota: 'Kubota' };
  var GEN_COLOR = { mep: C.mep, kubota: C.kub };

  /* ---------- time helpers ---------- */
  var fmtClock = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hour: 'numeric', minute: '2-digit', hour12: true });
  var fmtYmd = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' });
  var fmtLong = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
  var fmtShort = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', weekday: 'short', month: 'short', day: 'numeric' });
  function clock(ts) { return ts == null ? '--' : fmtClock.format(ts * 1000).replace(' AM', ' am').replace(' PM', ' pm'); }
  function today() { return fmtYmd.format(new Date()); }
  function addDays(ymd, n) {
    var d = new Date(ymd + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }
  function label(ymd) { return fmtLong.format(new Date(ymd + 'T12:00:00Z')); }
  function shortLabel(ymd) { return fmtShort.format(new Date(ymd + 'T12:00:00Z')); }
  function tzDate(ts) { return uPlot.tzDate(new Date(ts * 1e3), TZ); }
  function num(v, d) {
    if (v == null || isNaN(v)) return '--';
    if (Math.abs(v) < 0.5 * Math.pow(10, -(d || 0))) v = 0;   /* no "-0" */
    return Number(v).toLocaleString('en-US', { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
  }
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; }); }

  /* ---------- markup ---------- */
  root.innerHTML =
    "<div class='h-tabs' role='tablist'>" +
    "<button class='h-tab on' data-tab='day' role='tab'>Day</button>" +
    "<button class='h-tab' data-tab='energy' role='tab'>Energy</button></div>" +
    "<div class='h-view' id='hDayView'>" +
    "<div class='h-bar'>" +
    "<button class='btn s sm' id='hPrev' aria-label='Previous day'>&#9664;</button>" +
    "<input type='date' id='hDate'>" +
    "<button class='btn s sm' id='hNext' aria-label='Next day'>&#9654;</button>" +
    "<button class='btn s sm' id='hToday'>Today</button>" +
    "<span class='h-sep'></span>" +
    "<label class='h-cmp'>Compare <input type='date' id='hCmp'></label>" +
    "<button class='btn s sm' id='hCmpClear' hidden>&times;</button>" +
    "<span class='h-sep'></span>" +
    "<button class='btn s sm' id='hZoomIn' aria-label='Zoom in'>+</button>" +
    "<button class='btn s sm' id='hZoomOut' aria-label='Zoom out'>&minus;</button>" +
    "<button class='btn s sm' id='hPanL' aria-label='Pan left'>&#8592;</button>" +
    "<button class='btn s sm' id='hPanR' aria-label='Pan right'>&#8594;</button>" +
    "<button class='btn s sm' id='hReset'>Full day</button>" +
    "<span class='h-sep'></span>" +
    "<button class='btn s sm' id='hCsv'>CSV</button>" +
    "<button class='btn s sm' id='hPng'>PNG</button>" +
    "</div>" +
    "<div class='h-status' id='hStatus'></div>" +
    "<div class='h-sum' id='hSum'></div>" +
    "<div id='hPanels'></div>" +
    "<div class='h-foot' id='hNotes'></div>" +
    "</div>" +
    "<div class='h-view' id='hEnergyView' hidden>" +
    "<div class='h-bar'>" +
    "<button class='btn s sm' id='hLast30'>Last 30 days</button>" +
    "<span class='h-sep'></span>" +
    "<button class='btn s sm' id='hMPrev' aria-label='Previous month'>&#9664;</button>" +
    "<input type='month' id='hMonth'>" +
    "<button class='btn s sm' id='hMNext' aria-label='Next month'>&#9654;</button>" +
    "<span class='h-sep'></span>" +
    "<button class='btn s sm' id='hECsv'>CSV</button>" +
    "</div>" +
    "<div class='h-status' id='hEStatus'></div>" +
    "<div class='h-sum' id='hESum'></div>" +
    "<div id='hEPanels'></div>" +
    "<div class='h-tablewrap'><table class='h-table' id='hETable'></table></div>" +
    "<div class='h-foot'>Generator kWh before 2026-08-28 comes from the gateway's hourly history; after, from its lifetime counter read hourly, so a day runs 11:37 pm to 11:37 pm. Run hours are from the agent's run records, which start 2026-08-28.</div>" +
    "</div>";
  function $(id) { return document.getElementById(id); }
  /* The panel's content width: clientWidth includes its padding. */
  function innerW(el) {
    var cs = getComputedStyle(el);
    return Math.max(200, el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight));
  }

  /* ---------- state ---------- */
  var S = {
    date: today(), cmp: null, A: null, B: null, cfg: null,
    charts: [], view: null, timer: null, seq: 0,
    energy: null, eRange: null, eCharts: []
  };
  $('hDate').max = today();
  $('hCmp').max = today();
  $('hMonth').max = today().slice(0, 7);

  /* ---------- fetching ---------- */
  function getJSON(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (b) { return { status: r.status, body: b }; });
    }, function () { return { status: 0, body: { online: false } }; });
  }
  function unreachable(res) { return res.status === 0 || res.status === 503 || (res.body && res.body.online === false); }
  var DOWN = 'History is kept on the KAMRUI and it is not answering right now. The live values above are unaffected.';

  function loadThresholds() {
    return getJSON('/config').then(function (res) {
      var c = res.body && res.body.config;
      if (c && c.mep803a && c.kubota) S.cfg = c;
    });
  }

  /* ---------- the day view ---------- */
  function setStatus(id, html, kind) {
    var el = $(id); el.innerHTML = html || ''; el.className = 'h-status' + (kind ? ' ' + kind : '');
  }

  function loadDay(keepView) {
    var seq = ++S.seq;
    $('hDate').value = S.date;
    $('hNext').disabled = S.date >= today();
    setStatus('hStatus', 'Loading ' + esc(label(S.date)) + '&hellip;');
    var reqs = [getJSON('/history/api/day?date=' + S.date), getJSON('/history/api/usable?date=' + S.date + '&step=1')];
    if (S.cmp) reqs.push(getJSON('/history/api/day?date=' + S.cmp), getJSON('/history/api/usable?date=' + S.cmp + '&step=1'));
    Promise.all(reqs).then(function (res) {
      if (seq !== S.seq) return;
      if (unreachable(res[0])) { setStatus('hStatus', DOWN, 'bad'); clearPanels(); return; }
      if (res[0].status !== 200) { setStatus('hStatus', esc(res[0].body.error || 'Request failed'), 'bad'); clearPanels(); return; }
      S.A = withUsable(res[0].body, res[1]);
      S.B = (res[2] && res[2].status === 200) ? withUsable(res[2].body, res[3]) : null;
      if (!keepView) S.view = null;
      render();
      scheduleRefresh();
    });
  }

  /* The battery work's usable % (agent usable.py, GET /battery?date=&step=1),
     put on our minute axis. Taken as given, never recomputed here. */
  function withUsable(D, res) {
    var pts = (res && res.status === 200 && res.body.day && res.body.day.points) || [];
    var byMin = {};
    pts.forEach(function (p) { if (p.usable_pct != null) byMin[p.ts - p.ts % 60] = p.usable_pct; });
    D.series.usable = D.series.t.map(function (t) { return t in byMin ? byMin[t] : null; });
    D.usableAnchors = (res && res.status === 200 && res.body.day && res.body.day.anchors) || [];
    D.usableOk = pts.length > 0;
    var u = D.series.usable.filter(function (v) { return v != null; });
    if (u.length) { D.summary.min_usable = Math.min.apply(null, u); D.summary.max_usable = Math.max.apply(null, u); }
    return D;
  }

  function scheduleRefresh() {
    clearTimeout(S.timer);
    if (S.date === today()) S.timer = setTimeout(function () {
      if (document.hidden || !root.offsetParent) { scheduleRefresh(); return; }
      loadDay(true);
    }, 60000);
  }

  function clearPanels() {
    S.charts.forEach(function (c) { c.u.destroy(); });
    S.charts = [];
    $('hPanels').innerHTML = ''; $('hSum').innerHTML = ''; $('hNotes').innerHTML = '';
  }

  /* B is drawn on A's clock: each B time moved by the gap between the two
     local midnights, so 2:00 pm lies over 2:00 pm. */
  function shifted(B, A) { var off = A.start - B.start; return B.series.t.map(function (t) { return t + off; }); }

  function joinData(keysA, keysB) {
    var A = S.A, tA = A.series.t;
    var tableA = [tA].concat(keysA.map(function (k) { return pick(A, k); }));
    if (!S.B || !keysB.length || !S.B.series.t.length) return tableA;
    var tB = shifted(S.B, A);
    var tableB = [tB].concat(keysB.map(function (k) { return pick(S.B, k); }));
    var modes = function (n) { var m = [0]; for (var i = 0; i < n; i++) m.push(1); return m; };
    /* 1 = keep each table's own nulls as gaps; join's alignment holes stay
       undefined and are drawn through. */
    return uPlot.join([tableA, tableB], [modes(keysA.length), modes(keysB.length)]);
  }
  function pick(D, k) {
    var s = D.series;
    if (k === 'batt_kw') return s.batt_w.map(function (w) { return w == null ? null : w; });
    return s[k] || s.t.map(function () { return null; });
  }

  function panelDefs() {
    var hourly = S.A.resolution === 'hour';
    var bl = S.B ? ' (' + shortLabel(S.B.date) + ')' : '';
    function W(v) { return v == null ? '--' : num(v) + ' W'; }
    return [
      { id: 'v', title: 'Battery voltage', unit: 'V', h: 230, thresholds: true,
        keys: ['v'], series: [{ label: 'Voltage', stroke: C.v, fmt: function (v) { return v == null ? '--' : num(v, 2) + ' V'; } }],
        cmp: ['v'] },
      { id: 'a', title: 'Battery current', unit: 'A', h: 160, zero: true,
        keys: ['a'], series: [{ label: 'Current (+ charge)', stroke: C.a, fmt: function (v) { return v == null ? '--' : num(v, 1) + ' A'; } }],
        cmp: ['a'] },
      { id: 'soc', title: 'State of charge', unit: '%', h: 170, hide: hourly, anchors: true,
        keys: ['usable', 'soc'], series: [
          { label: 'Usable (0 % = 52.0 V)', stroke: C.usable, width: 2, fmt: function (v) { return v == null ? '--' : num(v, 1) + ' %'; } },
          { label: "Monitor's SOC", stroke: C.soc, fmt: function (v) { return v == null ? '--' : num(v) + ' %'; } }],
        cmp: ['usable', 'soc'] },
      { id: 'pw', title: 'Power', unit: 'W', h: 230, zero: true,
        keys: ['solar', 'load', 'gen_w', 'batt_kw'],
        series: [
          { label: 'Solar', stroke: C.solar, fmt: W },
          { label: 'House load', stroke: C.load, fmt: W },
          { label: hourly ? 'Generator' : 'Generator (est.)', stroke: C.gen, fmt: W },
          { label: 'Battery (+ charge / − discharge)', stroke: C.batt, fill: 'rgba(25,158,112,0.22)', width: 1, fmt: W }
        ],
        cmp: ['solar', 'load'] },
      { id: 'arr', title: 'Solar by array', unit: 'W', h: 180, hide: hourly,
        keys: ['mppt80', 'south', 'west'],
        series: [
          { label: 'MPPT 80 · ground + terrace', stroke: C.mppt80, fmt: W },
          { label: 'MPPT 150 · gen building (south)', stroke: C.south, fmt: W },
          { label: 'MPPT 150 · house (west)', stroke: C.west, fmt: W }
        ],
        cmp: [] }
    ].map(function (p) { p.bl = bl; return p; });
  }

  function render() {
    clearPanels();
    var A = S.A;
    $('hSum').innerHTML = summaryHtml();
    var notes = (A.notes || []).slice();
    if (S.cfg) notes.push('Threshold lines are the current settings, not what was set on that day.');
    $('hNotes').textContent = notes.join(' ');
    if (A.resolution === 'none') {
      setStatus('hStatus', 'Nothing was recorded on ' + esc(label(A.date)) + '.', 'warn');
      return;
    }
    var s = A.resolution === 'hour' ? 'Hourly averages' : 'One point a minute';
    setStatus('hStatus', esc(label(A.date)) + ' &middot; ' + s +
      (S.B ? ' &middot; dashed: ' + esc(label(S.B.date)) + (S.B.resolution === 'none' ? ' (nothing recorded)' : '') : '') +
      ' &middot; drag across a chart to zoom, double-click for the full day');
    var defs = panelDefs();
    defs.forEach(function (p) { if (!p.hide) S.charts.push(makePanel(p)); });
    if (S.view) applyView(S.view.min, S.view.max);
  }

  function summaryHtml() {
    var A = S.A, m = A.summary || {}, out = [];
    if (m.min_v != null) out.push(chip('Voltage', num(m.min_v, 2) + '–' + num(m.max_v, 2) + ' V'));
    if (m.min_usable != null) out.push(chip('Usable', num(m.min_usable) + '–' + num(m.max_usable) + ' %'));
    if (m.min_soc != null) out.push(chip('Monitor SOC', num(m.min_soc) + '–' + num(m.max_soc) + ' %'));
    if (m.peak_solar_w != null) out.push(chip('Peak solar', num(m.peak_solar_w) + ' W at ' + clock(m.peak_solar_at)));
    if (A.sun) out.push(chip('Sun', clock(A.sun.sunrise) + ' – ' + clock(A.sun.sunset)));
    var runs = (A.runs || []).map(function (r) {
      var why = r.kind ? ' &middot; ' + esc(r.kind) : '';
      return "<span class='h-run' style='--c:" + GEN_COLOR[r.gen] + "'><i></i>" + GEN_NAME[r.gen] + ' ' +
        clock(r.start) + ' – ' + (r.open ? 'now' : clock(r.stop)) + ' &middot; ' + r.minutes + ' min' + why + '</span>';
    });
    out.push(runs.length ? runs.join('') : chip('Generators', 'no runs'));
    return out.join('');
  }
  function chip(k, v) { return "<span class='h-chip'><b>" + k + '</b> ' + v + '</span>'; }

  /* ---------- one uPlot panel ---------- */
  var syncing = false;
  function makePanel(p) {
    var wrap = document.createElement('div');
    wrap.className = 'h-panel';
    wrap.innerHTML = "<div class='h-ttl'>" + esc(p.title) + '</div>';
    $('hPanels').appendChild(wrap);
    var hourly = S.A.resolution === 'hour';
    var cmpKeys = (S.B && S.B.resolution !== 'none') ? p.cmp : [];
    var data = joinData(p.keys, cmpKeys);
    var series = [{ value: function (u, v) { return v == null ? '--' : clock(v); } }];
    p.series.forEach(function (s) {
      series.push({ label: s.label, stroke: s.stroke, fill: s.fill, width: s.width || 1.5, scale: p.unit,
        spanGaps: false, points: { show: hourly, size: 5 }, value: function (u, v) { return s.fmt(v); } });
    });
    cmpKeys.forEach(function (k) {
      var s = p.series[p.keys.indexOf(k)];
      series.push({ label: s.label + p.bl, stroke: s.stroke, width: 1.25, dash: [5, 4], alpha: 0.85, scale: p.unit,
        spanGaps: false, points: { show: S.B.resolution === 'hour', size: 4 }, value: function (u, v) { return s.fmt(v); } });
    });
    var scales = { x: { time: true, auto: false, min: S.A.start, max: S.A.end } };
    scales[p.unit] = { auto: true, range: function (u, mn, mx) { return yRange(p, mn, mx); } };
    var opts = {
      width: innerW(wrap), height: p.h,
      tzDate: tzDate,
      series: series, scales: scales,
      axes: [
        { stroke: C.axis, grid: { stroke: C.grid, width: 1 }, ticks: { stroke: C.grid } },
        { scale: p.unit, stroke: C.axis, grid: { stroke: C.grid, width: 1 }, ticks: { stroke: C.grid },
          size: 56, values: function (u, vals) { return vals.map(function (v) { return axisVal(p.unit, v); }); } }
      ],
      cursor: {
        sync: { key: 'histday', setSeries: false },
        drag: { x: true, y: false, setScale: true },
        bind: { dblclick: function () { return function () { resetView(); return null; }; } }
      },
      legend: { live: true },
      hooks: {
        drawClear: [function (u) { drawBands(u); }],
        draw: [function (u) { drawMarkers(u, p); }],
        setScale: [function (u, key) {
          if (key !== 'x' || syncing) return;
          var mn = u.scales.x.min, mx = u.scales.x.max;
          S.view = (mn <= S.A.start && mx >= S.A.end) ? null : { min: mn, max: mx };
          syncing = true;
          S.charts.forEach(function (c) { if (c.u !== u) c.u.setScale('x', { min: mn, max: mx }); });
          syncing = false;
        }]
      }
    };
    var u = new uPlot(opts, data, wrap);
    wireTouch(u);
    return { u: u, p: p, wrap: wrap };
  }

  function yRange(p, mn, mx) {
    if (mn == null || mx == null) return p.unit === '%' ? [0, 100] : [0, 1];
    if (p.unit === '%') return [Math.max(0, Math.floor(mn / 10) * 10 - 10), Math.max(100, Math.ceil(mx / 10) * 10)];
    if (p.thresholds && S.cfg) {
      [S.cfg.mep803a, S.cfg.kubota].forEach(function (g) {
        mn = Math.min(mn, g.startVoltage); mx = Math.max(mx, g.stopVoltage);
      });
    }
    if (p.unit === 'V') { var pad = Math.max(0.2, (mx - mn) * 0.08); return [Math.floor((mn - pad) * 2) / 2, Math.ceil((mx + pad) * 2) / 2]; }
    if (p.zero) { mn = Math.min(0, mn); mx = Math.max(0, mx); }
    var r = uPlot.rangeNum(mn, mx, 0.1, true);
    return r;
  }
  function axisVal(unit, v) {
    if (unit === 'W') return Math.abs(v) >= 1000 ? num(v / 1000, Math.abs(v) % 1000 ? 1 : 0) + 'k' : num(v);
    if (unit === 'V') return num(v, 1);
    return num(v);
  }

  /* Generator runs as bands and the night as a faint wash, under the lines. */
  function drawBands(u) {
    var ctx = u.ctx, b = u.bbox, A = S.A;
    ctx.save();
    ctx.beginPath(); ctx.rect(b.left, b.top, b.width, b.height); ctx.clip();
    if (A.sun) {
      ctx.fillStyle = 'rgba(120,140,190,0.06)';
      var x0 = u.valToPos(A.start, 'x', true), sr = u.valToPos(A.sun.sunrise, 'x', true),
        ss = u.valToPos(A.sun.sunset, 'x', true), x1 = u.valToPos(A.end, 'x', true);
      ctx.fillRect(x0, b.top, sr - x0, b.height);
      ctx.fillRect(ss, b.top, x1 - ss, b.height);
    }
    (A.runs || []).forEach(function (r) {
      var a = u.valToPos(r.start, 'x', true), z = u.valToPos(r.stop, 'x', true);
      ctx.fillStyle = hexA(GEN_COLOR[r.gen], 0.18);
      ctx.fillRect(a, b.top, Math.max(2, z - a), b.height);
      ctx.fillStyle = hexA(GEN_COLOR[r.gen], 0.9);
      ctx.fillRect(a, b.top, Math.max(2, z - a), 3 * devicePixelRatio);
    });
    ctx.restore();
  }

  function drawMarkers(u, p) {
    var ctx = u.ctx, b = u.bbox, A = S.A, dpr = devicePixelRatio;
    ctx.save();
    ctx.beginPath(); ctx.rect(b.left, b.top, b.width, b.height); ctx.clip();
    ctx.font = (11 * dpr) + 'px system-ui,sans-serif';
    ctx.lineWidth = dpr;
    if (A.sun) {
      ctx.strokeStyle = 'rgba(201,133,0,0.6)'; ctx.setLineDash([3 * dpr, 4 * dpr]);
      [['sunrise', A.sun.sunrise], ['sunset', A.sun.sunset]].forEach(function (s) {
        var x = u.valToPos(s[1], 'x', true);
        ctx.beginPath(); ctx.moveTo(x, b.top); ctx.lineTo(x, b.top + b.height); ctx.stroke();
        if (p.id === 'v') { ctx.fillStyle = C.dim; ctx.fillText((s[0] === 'sunrise' ? '☀ ' : '☾ ') + clock(s[1]), x + 4 * dpr, b.top + b.height - 6 * dpr); }
      });
    }
    if (p.thresholds && S.cfg) {
      var raw = [
        ['MEP', 'start', S.cfg.mep803a.startVoltage, C.mep], ['MEP', 'stop', S.cfg.mep803a.stopVoltage, C.mep],
        ['Kubota', 'start', S.cfg.kubota.startVoltage, C.kub], ['Kubota', 'stop', S.cfg.kubota.stopVoltage, C.kub]
      ];
      /* One line and one label where both generators share a value. */
      var lines = [];
      raw.forEach(function (r) {
        var same = lines.filter(function (l) { return l[1] === r[2] && l[3] === r[1]; })[0];
        if (same) { same[0] = 'MEP + Kubota ' + r[1]; same[2] = C.txt; }
        else lines.push([r[0] + ' ' + r[1], r[2], r[3], r[1]]);
      });
      var used = [];
      lines.forEach(function (l) {
        var y = u.valToPos(l[1], 'V', true);
        ctx.strokeStyle = hexA(l[2], 0.8); ctx.setLineDash([6 * dpr, 4 * dpr]);
        ctx.beginPath(); ctx.moveTo(b.left, y); ctx.lineTo(b.left + b.width, y); ctx.stroke();
        var ty = y - 4 * dpr;
        used.forEach(function (o) { if (Math.abs(o - ty) < 12 * dpr) ty = o - 12 * dpr; });
        used.push(ty);
        ctx.fillStyle = C.txt; ctx.textAlign = 'right';
        ctx.fillText(l[0] + ' ' + num(l[1], 1) + ' V', b.left + b.width - 6 * dpr, ty);
        ctx.textAlign = 'left';
      });
    }
    if (p.anchors) {
      /* Where usable.py re-set its counter: a full pack (top) or 52.0 V (floor). */
      ctx.setLineDash([2 * dpr, 3 * dpr]); ctx.strokeStyle = 'rgba(232,238,245,0.5)';
      (A.usableAnchors || []).forEach(function (an) {
        var x = u.valToPos(an.ts, 'x', true);
        ctx.beginPath(); ctx.moveTo(x, b.top); ctx.lineTo(x, b.top + b.height); ctx.stroke();
        ctx.fillStyle = C.txt;
        ctx.fillText(an.kind === 'top' ? 'full (100 %)' : '52.0 V (0 %)', x + 4 * dpr, b.top + 12 * dpr);
      });
    }
    if (p.zero) {
      var y0 = u.valToPos(0, p.unit, true);
      ctx.setLineDash([]); ctx.strokeStyle = 'rgba(255,255,255,0.25)';
      ctx.beginPath(); ctx.moveTo(b.left, y0); ctx.lineTo(b.left + b.width, y0); ctx.stroke();
    }
    ctx.restore();
  }
  function hexA(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return 'rgba(' + (n >> 16) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  /* ---------- zoom and pan ---------- */
  function applyView(mn, mx) {
    var A = S.A, span = A.end - A.start;
    var w = Math.max(600, Math.min(span, mx - mn));
    if (mn < A.start) { mn = A.start; }
    if (mn + w > A.end) { mn = A.end - w; }
    mx = mn + w;
    S.view = (mn <= A.start && mx >= A.end) ? null : { min: mn, max: mx };
    syncing = true;
    S.charts.forEach(function (c) { c.u.setScale('x', { min: mn, max: mx }); });
    syncing = false;
  }
  function curView() { return S.view || { min: S.A.start, max: S.A.end }; }
  function resetView() { if (S.A) applyView(S.A.start, S.A.end); }
  function zoom(f, center) {
    if (!S.A) return;
    var v = curView(), c = center == null ? (v.min + v.max) / 2 : center, w = (v.max - v.min) * f;
    applyView(c - w * (c - v.min) / (v.max - v.min), c - w * (c - v.min) / (v.max - v.min) + w);
  }
  function pan(f) { if (!S.A) return; var v = curView(), d = (v.max - v.min) * f; applyView(v.min + d, v.max + d); }

  /* Wheel zooms around the cursor. Two fingers pinch-zoom and pan; one finger
     is left to scroll the page, which on a phone is what one finger is for. */
  function wireTouch(u) {
    var over = u.over, t0 = null;
    over.addEventListener('wheel', function (e) {
      /* A plain wheel scrolls the page. Ctrl or Alt + wheel zooms; a
         trackpad pinch arrives as Ctrl + wheel. */
      if (!e.ctrlKey && !e.altKey) return;
      e.preventDefault();
      var x = u.posToVal(e.offsetX, 'x');
      zoom(e.deltaY < 0 ? 0.8 : 1.25, x);
    }, { passive: false });
    over.addEventListener('touchstart', function (e) {
      if (e.touches.length !== 2) { t0 = null; return; }
      var v = curView();
      t0 = { d: dist(e), c: mid(e), min: v.min, max: v.max };
    }, { passive: true });
    over.addEventListener('touchmove', function (e) {
      if (!t0 || e.touches.length !== 2) return;
      e.preventDefault();
      var r = over.getBoundingClientRect(), w0 = t0.max - t0.min;
      var f = t0.d / Math.max(10, dist(e));
      var anchor = t0.min + w0 * (t0.c - r.left) / r.width;
      var w = w0 * f;
      var mn = anchor - w * (mid(e) - r.left) / r.width;
      applyView(mn, mn + w);
    }, { passive: false });
    over.addEventListener('touchend', function () { t0 = null; }, { passive: true });
  }
  function dist(e) { var a = e.touches[0], b = e.touches[1]; return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY); }
  function mid(e) { return (e.touches[0].clientX + e.touches[1].clientX) / 2; }

  /* ---------- export ---------- */
  function download(name, blob) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  function csvCell(v) { return v == null ? '' : (typeof v === 'string' && /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : String(v)); }
  function dayCsv(D) {
    var cols = [['battery_v', 'v'], ['battery_a', 'a'], ['battery_w', 'batt_w'], ['usable_pct', 'usable'], ['monitor_soc_pct', 'soc'],
      ['solar_w', 'solar'], ['mppt80_w', 'mppt80'], ['south_w', 'south'], ['west_w', 'west'],
      ['load_w', 'load'], ['generator_w_est', 'gen_w'], ['mep_running', 'mep'], ['kubota_running', 'kub']];
    if (D.resolution === 'hour') cols.splice(1, 0, ['battery_v_min', 'v_min'], ['battery_v_max', 'v_max']);
    var lines = [['date', 'time', 'unix'].concat(cols.map(function (c) { return c[0]; })).join(',')];
    D.series.t.forEach(function (t, i) {
      lines.push([D.date, clock(t), t].concat(cols.map(function (c) { var s = D.series[c[1]]; return csvCell(s ? s[i] : null); })).join(','));
    });
    return lines.join('\n') + '\n';
  }
  function exportCsv() {
    if (!S.A) return;
    var txt = dayCsv(S.A);
    if (S.B) txt += '\n' + dayCsv(S.B);
    download('solar_' + S.A.date + (S.B ? '_vs_' + S.B.date : '') + '.csv', new Blob([txt], { type: 'text/csv' }));
  }
  function exportPng() {
    if (!S.charts.length) return;
    var dpr = devicePixelRatio, pad = 12 * dpr, head = 22 * dpr, w = 0, h = pad + 26 * dpr;
    S.charts.forEach(function (c) { w = Math.max(w, c.u.ctx.canvas.width); h += head + c.u.ctx.canvas.height + pad; });
    var cv = document.createElement('canvas'); cv.width = w + 2 * pad; cv.height = h;
    var ctx = cv.getContext('2d');
    ctx.fillStyle = '#0b0f14'; ctx.fillRect(0, 0, cv.width, cv.height);
    ctx.fillStyle = C.txt; ctx.font = 'bold ' + (15 * dpr) + 'px system-ui,sans-serif';
    ctx.fillText('Mercalde Solar · ' + label(S.A.date) + (S.B ? ' (dashed: ' + label(S.B.date) + ')' : ''), pad, pad + 14 * dpr);
    var y = pad + 26 * dpr;
    S.charts.forEach(function (c) {
      ctx.fillStyle = C.dim; ctx.font = (12 * dpr) + 'px system-ui,sans-serif';
      ctx.fillText(c.p.title + '  —  ' + c.p.series.map(function (s) { return s.label; }).join(', '), pad, y + 15 * dpr);
      y += head;
      ctx.drawImage(c.u.ctx.canvas, pad, y);
      y += c.u.ctx.canvas.height + pad;
    });
    cv.toBlob(function (b) { download('solar_' + S.A.date + '.png', b); });
  }

  /* ---------- energy view ---------- */
  function loadEnergy(from, to, title) {
    S.eRange = { from: from, to: to, title: title };
    setStatus('hEStatus', 'Loading&hellip;');
    getJSON('/history/api/energy?from=' + from + '&to=' + to).then(function (res) {
      if (unreachable(res)) { setStatus('hEStatus', DOWN, 'bad'); clearEnergy(); return; }
      if (res.status !== 200) { setStatus('hEStatus', esc(res.body.error || 'Request failed'), 'bad'); clearEnergy(); return; }
      S.energy = res.body;
      renderEnergy();
    });
  }
  function clearEnergy() {
    S.eCharts.forEach(function (u) { u.destroy(); }); S.eCharts = [];
    $('hEPanels').innerHTML = ''; $('hETable').innerHTML = ''; $('hESum').innerHTML = '';
  }
  function last30() { var t = today(); $('hMonth').value = ''; loadEnergy(addDays(t, -29), t, 'Last 30 days'); }
  function loadMonth(ym) {
    var first = ym + '-01', d = new Date(first + 'T12:00:00Z');
    d.setUTCMonth(d.getUTCMonth() + 1); d.setUTCDate(0);
    var last = d.toISOString().slice(0, 10);
    if (last > today()) last = today();
    $('hMonth').value = ym;
    loadEnergy(first, last, fmtMonth(ym));
  }
  function fmtMonth(ym) { return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'long', year: 'numeric' }).format(new Date(ym + '-15T12:00:00Z')); }
  function shiftMonth(n) {
    var ym = $('hMonth').value || today().slice(0, 7);
    var d = new Date(ym + '-15T12:00:00Z'); d.setUTCMonth(d.getUTCMonth() + n);
    var nym = d.toISOString().slice(0, 7);
    if (nym > today().slice(0, 7)) return;
    loadMonth(nym);
  }

  function groupedBars(gi, gn) {
    return function (u, sidx, i0, i1) {
      var xs = u.data[0], ys = u.data[sidx], s = u.series[sidx], p = new Path2D(), dpr = devicePixelRatio;
      var dx = Math.abs(u.valToPos(86400, 'x', true) - u.valToPos(0, 'x', true));
      var gw = dx * 0.78, bw = gw / gn, gap = 2 * dpr;
      var y0 = u.valToPos(0, s.scale, true);
      for (var i = i0; i <= i1; i++) {
        if (ys[i] == null) continue;
        var x = u.valToPos(xs[i], 'x', true) - gw / 2 + gi * bw;
        var y = u.valToPos(ys[i], s.scale, true);
        var top = Math.min(y, y0), ht = Math.max(1, Math.abs(y0 - y));
        if (p.roundRect) p.roundRect(x + gap / 2, top, Math.max(1, bw - gap), ht, ys[i] >= 0 ? [4 * dpr, 4 * dpr, 0, 0] : [0, 0, 4 * dpr, 4 * dpr]);
        else p.rect(x + gap / 2, top, Math.max(1, bw - gap), ht);
      }
      return { stroke: p, fill: p };
    };
  }

  function energyChart(title, unit, h, rows, defs) {
    var wrap = document.createElement('div');
    wrap.className = 'h-panel';
    wrap.innerHTML = "<div class='h-ttl'>" + esc(title) + '</div>';
    $('hEPanels').appendChild(wrap);
    var xs = rows.map(function (r) { return Date.parse(r.day + 'T12:00:00Z') / 1000; });
    var data = [xs].concat(defs.map(function (d) { return rows.map(function (r) { return d.get(r); }); }));
    var series = [{ label: 'Day', value: function (u, v) { return v == null ? '--' : shortLabel(new Date(v * 1000).toISOString().slice(0, 10)); } }];
    defs.forEach(function (d, i) {
      series.push({ label: d.label, stroke: d.color, fill: hexA(d.color, 0.85), width: 1, scale: unit,
        paths: groupedBars(d.group == null ? i : d.group, d.groups || defs.length), points: { show: false },
        value: function (u, v) { return v == null ? '--' : num(v, unit === 'h' ? 1 : 1) + ' ' + unit; } });
    });
    var scales = { x: { time: true, auto: false, min: xs[0] - 43200, max: xs[xs.length - 1] + 43200 } };
    scales[unit] = { auto: true, range: function (u, mn, mx) { return uPlot.rangeNum(Math.min(0, mn || 0), Math.max(0, mx || 1), 0.1, true); } };
    var u = new uPlot({
      width: innerW(wrap), height: h, series: series, scales: scales,
      tzDate: function (ts) { return uPlot.tzDate(new Date(ts * 1e3), 'UTC'); },
      axes: [
        { stroke: C.axis, grid: { show: false }, ticks: { stroke: C.grid } },
        { scale: unit, stroke: C.axis, grid: { stroke: C.grid, width: 1 }, ticks: { stroke: C.grid }, size: 50 }
      ],
      cursor: { drag: { x: false, y: false }, points: { show: false } },
      legend: { live: true },
      hooks: { draw: [function (u) {
        var ctx = u.ctx, b = u.bbox, y0 = u.valToPos(0, unit, true);
        ctx.save(); ctx.strokeStyle = 'rgba(255,255,255,0.25)'; ctx.lineWidth = devicePixelRatio;
        ctx.beginPath(); ctx.moveTo(b.left, y0); ctx.lineTo(b.left + b.width, y0); ctx.stroke(); ctx.restore();
      }] }
    }, data, wrap);
    S.eCharts.push(u);
  }

  function renderEnergy() {
    clearEnergy();
    var rows = S.energy.days;
    var tot = { s: 0, l: 0, g: 0, bi: 0, bo: 0, mh: 0, kh: 0 };
    rows.forEach(function (r) {
      tot.s += r.solar_kwh || 0; tot.l += r.load_kwh || 0; tot.g += r.gen_kwh || 0;
      tot.bi += r.batt_in_kwh || 0; tot.bo += r.batt_out_kwh || 0; tot.mh += r.mep_hours || 0; tot.kh += r.kub_hours || 0;
    });
    setStatus('hEStatus', esc(S.eRange.title) + ' &middot; ' + esc(shortLabel(S.energy.from)) + ' – ' + esc(shortLabel(S.energy.to)));
    $('hESum').innerHTML = chip('Solar', num(tot.s, 1) + ' kWh') + chip('Load', num(tot.l, 1) + ' kWh') +
      chip('Generator', num(tot.g, 1) + ' kWh') + chip('MEP', num(tot.mh, 1) + ' h') + chip('Kubota', num(tot.kh, 1) + ' h');
    energyChart('Daily energy', 'kWh', 230, rows, [
      { label: 'Solar', color: C.solar, get: function (r) { return r.solar_kwh; } },
      { label: 'House load', color: C.load, get: function (r) { return r.load_kwh; } },
      { label: 'Generator', color: C.gen, get: function (r) { return r.gen_kwh; } }
    ]);
    energyChart('Battery in (+) and out (−)', 'kWh', 190, rows, [
      { label: 'Into battery', color: C.batt, group: 0, groups: 1, get: function (r) { return r.batt_in_kwh; } },
      { label: 'Out of battery', color: C.batt, group: 0, groups: 1, get: function (r) { return r.batt_out_kwh == null ? null : -r.batt_out_kwh; } }
    ]);
    energyChart('Generator run hours', 'h', 170, rows, [
      { label: 'MEP-803A', color: C.mep, get: function (r) { return r.mep_hours; } },
      { label: 'Kubota', color: C.kub, get: function (r) { return r.kub_hours; } }
    ]);
    var head = '<tr><th>Day</th><th>Solar kWh</th><th>Load kWh</th><th>Gen kWh</th><th>Batt in</th><th>Batt out</th><th>MEP h</th><th>Kubota h</th><th>Volts</th></tr>';
    var body = rows.slice().reverse().map(function (r) {
      return "<tr data-day='" + r.day + "'><td><a href='#' class='h-daylink'>" + esc(shortLabel(r.day)) + '</a>' + (r.partial ? ' *' : '') + '</td><td>' + num(r.solar_kwh, 1) + '</td><td>' + num(r.load_kwh, 1) +
        '</td><td>' + num(r.gen_kwh, 1) + '</td><td>' + num(r.batt_in_kwh, 1) + '</td><td>' + num(r.batt_out_kwh, 1) +
        '</td><td>' + num(r.mep_hours, 1) + '</td><td>' + num(r.kub_hours, 1) + '</td><td>' +
        (r.min_v == null ? '--' : num(r.min_v, 1) + '–' + num(r.max_v, 1)) + '</td></tr>';
    }).join('');
    var foot = '<tr class="tot"><td>Total</td><td>' + num(tot.s, 1) + '</td><td>' + num(tot.l, 1) + '</td><td>' + num(tot.g, 1) +
      '</td><td>' + num(tot.bi, 1) + '</td><td>' + num(tot.bo, 1) + '</td><td>' + num(tot.mh, 1) + '</td><td>' + num(tot.kh, 1) + '</td><td></td></tr>';
    $('hETable').innerHTML = '<thead>' + head + '</thead><tbody>' + body + foot + '</tbody>';
  }
  function exportEnergyCsv() {
    if (!S.energy) return;
    var keys = ['day', 'solar_kwh', 'load_kwh', 'gen_kwh', 'gen_source', 'batt_in_kwh', 'batt_out_kwh', 'mep_hours', 'kub_hours', 'min_v', 'max_v', 'partial'];
    var lines = [keys.join(',')].concat(S.energy.days.map(function (r) { return keys.map(function (k) { return csvCell(r[k]); }).join(','); }));
    download('solar_energy_' + S.energy.from + '_' + S.energy.to + '.csv', new Blob([lines.join('\n') + '\n'], { type: 'text/csv' }));
  }

  /* ---------- wiring ---------- */
  function go(d) { if (!d || d > today()) return; S.date = d; loadDay(false); }
  $('hPrev').onclick = function () { go(addDays(S.date, -1)); };
  $('hNext').onclick = function () { go(addDays(S.date, 1)); };
  $('hToday').onclick = function () { go(today()); };
  $('hDate').onchange = function () { go(this.value); };
  $('hCmp').onchange = function () { S.cmp = this.value || null; $('hCmpClear').hidden = !S.cmp; loadDay(true); };
  $('hCmpClear').onclick = function () { S.cmp = null; $('hCmp').value = ''; this.hidden = true; loadDay(true); };
  $('hZoomIn').onclick = function () { zoom(0.5); };
  $('hZoomOut').onclick = function () { zoom(2); };
  $('hPanL').onclick = function () { pan(-0.4); };
  $('hPanR').onclick = function () { pan(0.4); };
  $('hReset').onclick = resetView;
  $('hCsv').onclick = exportCsv;
  $('hPng').onclick = exportPng;
  $('hLast30').onclick = last30;
  $('hMonth').onchange = function () { if (this.value) loadMonth(this.value); };
  $('hMPrev').onclick = function () { shiftMonth(-1); };
  $('hMNext').onclick = function () { shiftMonth(1); };
  $('hECsv').onclick = exportEnergyCsv;
  $('hETable').onclick = function (e) {
    var a = e.target.closest('.h-daylink'); if (!a) return;
    e.preventDefault(); showTab('day'); go(a.closest('tr').dataset.day);
  };
  function showTab(name) {
    Array.prototype.forEach.call(root.querySelectorAll('.h-tab'), function (b) { b.classList.toggle('on', b.dataset.tab === name); });
    $('hDayView').hidden = name !== 'day';
    $('hEnergyView').hidden = name !== 'energy';
    if (name === 'energy' && !S.energy) last30();
    resize();
  }
  Array.prototype.forEach.call(root.querySelectorAll('.h-tab'), function (b) { b.onclick = function () { showTab(b.dataset.tab); }; });

  function resize() {
    S.charts.forEach(function (c) { var w = innerW(c.wrap); if (w !== c.u.width) c.u.setSize({ width: w, height: c.p.h }); });
    S.eCharts.forEach(function (u) { var w = innerW(u.root.parentNode); if (w !== u.width) u.setSize({ width: w, height: u.height }); });
  }
  var rt = null;
  window.addEventListener('resize', function () { clearTimeout(rt); rt = setTimeout(resize, 150); });
  var sec = document.getElementById('histSec');
  if (sec) sec.addEventListener('toggle', function () { if (sec.open) setTimeout(resize, 0); });

  window.SolarHistory = { reload: function () { loadDay(true); } };
  /* Deep links: #history=YYYY-MM-DD[&cmp=YYYY-MM-DD], #history-energy. */
  (function () {
    var h = location.hash.slice(1), m = /(?:^|&)history=(\d{4}-\d{2}-\d{2})/.exec(h), c = /(?:^|&)cmp=(\d{4}-\d{2}-\d{2})/.exec(h);
    if (m && m[1] <= today()) S.date = m[1];
    if (c && c[1] <= today()) { S.cmp = c[1]; $('hCmp').value = c[1]; $('hCmpClear').hidden = false; }
  })();
  loadThresholds().then(function () { loadDay(false); }, function () { loadDay(false); });
  if (location.hash === '#history-energy') showTab('energy');
})();
