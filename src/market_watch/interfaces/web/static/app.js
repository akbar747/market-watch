/* src/market_watch/interfaces/web/static/app.js
   看板前端：只负责画（服务端已降采样到 <=2000 点）。
   ECharts 是 vendored UMD（/static/vendor/echarts.min.js），禁止 CDN。 */
(function () {
  'use strict';

  var chart = null;

  function fmt(n) {
    return n === null || n === undefined ? '—' : String(n);
  }

  async function getJSON(url) {
    const resp = await fetch(url, { headers: { Accept: 'application/json' } });
    if (!resp.ok) throw new Error(url + ' -> HTTP ' + resp.status);
    return resp.json();
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = fmt(value);
  }

  async function loadSummary() {
    try {
      const s = await getJSON('/api/summary');
      setText('s-items', s.items_total);
      setText('s-cycle', s.cycle_ts);
      setText('s-alerts', s.alerts_24h);
      setText('s-ann', s.ann_24h);
      setText('s-partial', s.quality_partial);
      setText('s-db', s.db_size_mb + ' MB');
    } catch (e) {
      console.error('summary failed', e);
    }
  }

  async function loadQuality() {
    try {
      const q = await getJSON('/api/quality');
      setText('q-rejects', q.rejects_24h);
      setText('q-partial', q.partial_cycles);
      const ul = document.getElementById('q-rules');
      if (!ul) return;
      ul.innerHTML = '';
      (q.rules || []).forEach(function (r) {
        const li = document.createElement('li');
        li.innerHTML = '<span>' + r.rule + '</span><span>' + r.hits + '</span>';
        ul.appendChild(li);
      });
    } catch (e) {
      console.error('quality failed', e);
    }
  }

  async function loadHealth() {
    try {
      const h = await getJSON('/api/health');
      const ul = document.getElementById('h-limiter');
      if (ul) {
        ul.innerHTML = '';
        (h.limiter || []).forEach(function (l) {
          const li = document.createElement('li');
          li.innerHTML = '<span>' + l.host + '</span><span>' + l.current_rps.toFixed(2) +
            ' rps · tokens ' + l.tokens.toFixed(1) + ' · ' + l.consumed_total + ' req</span>';
          ul.appendChild(li);
        });
      }
      const ub = document.getElementById('h-breaker');
      if (ub) {
        ub.innerHTML = '';
        Object.keys(h.breaker || {}).forEach(function (host) {
          const li = document.createElement('li');
          li.innerHTML = '<span>breaker ' + host + '</span><span>' + h.breaker[host] + '</span>';
          ub.appendChild(li);
        });
      }
    } catch (e) {
      console.error('health failed', e);
    }
  }

  async function loadAlerts() {
    try {
      const rows = await getJSON('/api/alerts?limit=20');
      const ul = document.getElementById('a-list');
      if (!ul) return;
      ul.innerHTML = '';
      rows.forEach(function (r) {
        const li = document.createElement('li');
        li.innerHTML = '<span class="sev-' + r.severity + '">[' + r.severity + '] ' +
          r.rule + ' #' + r.item_id + '</span><span>' + (r.message || '') + '</span>';
        ul.appendChild(li);
      });
    } catch (e) {
      console.error('alerts failed', e);
    }
  }

  async function loadAnnouncements() {
    try {
      const rows = await getJSON('/api/announcements?limit=20');
      const ul = document.getElementById('n-list');
      if (!ul) return;
      ul.innerHTML = '';
      rows.forEach(function (r) {
        const li = document.createElement('li');
        const a = document.createElement('a');
        a.href = r.link; a.target = '_blank'; a.rel = 'noopener noreferrer';
        a.textContent = r.title || r.guid;
        const left = document.createElement('span');
        left.appendChild(a);
        const right = document.createElement('span');
        right.textContent = (r.published_at || '').slice(0, 16);
        li.appendChild(left); li.appendChild(right);
        ul.appendChild(li);
      });
    } catch (e) {
      console.error('announcements failed', e);
    }
  }

  async function loadSeries(itemId, table, sinceHours) {
    const url = '/api/series?item_id=' + encodeURIComponent(itemId) +
      '&table=' + encodeURIComponent(table) + '&since_hours=' + encodeURIComponent(sinceHours);
    const data = await getJSON(url);
    const el = document.getElementById('chart');
    if (!el || typeof echarts === 'undefined') return;
    if (!chart) chart = echarts.init(el);
    chart.setOption({
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis' },
      legend: { data: ['最低卖价', '最高买价'], textStyle: { color: '#8b98a5' } },
      grid: { left: 56, right: 20, top: 34, bottom: 40 },
      xAxis: { type: 'category', data: data.ts, axisLabel: { color: '#8b98a5' } },
      yAxis: { type: 'value', axisLabel: { color: '#8b98a5' }, scale: true },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
      series: [
        { name: '最低卖价', type: 'line', data: data.sell, showSymbol: false, lineStyle: { width: 1.4 } },
        { name: '最高买价', type: 'line', data: data.buy, showSymbol: false, lineStyle: { width: 1.4 } }
      ]
    }, true);
  }

  function bindSearch() {
    const input = document.getElementById('fq');
    const results = document.getElementById('fq-results');
    const itemInput = document.getElementById('f-item');
    if (!input || !results) return;
    var timer = null;
    input.addEventListener('input', function () {
      const q = input.value.trim();
      if (timer) clearTimeout(timer);
      if (q.length < 2) { results.textContent = ''; return; }
      timer = setTimeout(async function () {
        try {
          const rows = await getJSON('/api/search?q=' + encodeURIComponent(q) + '&limit=10');
          results.innerHTML = '';
          rows.forEach(function (r) {
            const a = document.createElement('a');
            a.textContent = '#' + r.item_id + ' ' + (r.name || '');
            a.addEventListener('click', function () {
              itemInput.value = r.item_id;
              results.textContent = '';
              input.value = r.name || '';
            });
            results.appendChild(a);
            results.appendChild(document.createTextNode('  '));
          });
        } catch (e) {
          console.error('search failed', e);
        }
      }, 250);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    loadSummary(); loadQuality(); loadHealth(); loadAlerts(); loadAnnouncements();
    bindSearch();
    const form = document.getElementById('f-series');
    if (form) {
      form.addEventListener('submit', function (ev) {
        ev.preventDefault();
        const itemId = document.getElementById('f-item').value || '24';
        const table = document.getElementById('f-table').value;
        const since = document.getElementById('f-since').value;
        loadSeries(itemId, table, since).catch(function (e) { console.error(e); });
      });
    }
    // 首屏默认画一次
    const itemId = (document.getElementById('f-item') || {}).value || '24';
    loadSeries(itemId, 'price_change', 24).catch(function () { /* 空库时静默 */ });
    window.addEventListener('resize', function () { if (chart) chart.resize(); });
    setInterval(function () { loadSummary(); loadHealth(); loadAlerts(); }, 30000);
  });
})();
