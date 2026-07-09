/* ═══════════════════════════════════════════════════════════════
   AIWealth Trading Dashboard - Main Logic
   ═══════════════════════════════════════════════════════════════ */

let DATA = null;
let currentDate = '';
let tradingDates = [];
let equityChart = null;
let currentTimeRange = 'all';

/* ─── BOOTSTRAP ─── */
(function loadData() {
  // Support both: 1) data pre-loaded via <script> tag (file:// protocol)
  //               2) fetch from JSON file (http:// protocol)
  if (window.DASHBOARD_DATA) {
    DATA = window.DASHBOARD_DATA;
    tradingDates = Object.keys(DATA.daily_data).sort();
    currentDate = tradingDates[tradingDates.length - 1];
    init();
    return;
  }

  // Try fetch first (works with http server)
  fetch('dashboard_data.json')
    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(d => {
      DATA = d;
      tradingDates = Object.keys(DATA.daily_data).sort();
      currentDate = tradingDates[tradingDates.length - 1];
      init();
    })
    .catch(e => {
      // Fallback: try loading via dynamic script tag (for file:// protocol)
      var script = document.createElement('script');
      script.src = 'dashboard_data.js';
      script.onload = function() {
        if (window.DASHBOARD_DATA) {
          DATA = window.DASHBOARD_DATA;
          tradingDates = Object.keys(DATA.daily_data).sort();
          currentDate = tradingDates[tradingDates.length - 1];
          init();
        } else {
          document.getElementById('loadingOverlay').innerHTML =
            '<div class="loader" style="color:var(--red)">加载失败<br>请通过HTTP服务器打开，或确保 dashboard_data.js 存在</div>';
        }
      };
      script.onerror = function() {
        document.getElementById('loadingOverlay').innerHTML =
          '<div class="loader" style="color:var(--red)">加载失败: ' + e.message + '<br>提示: file://协议下请使用 python3 -m http.server 启动本地服务器</div>';
      };
      document.body.appendChild(script);
    });
})();

function init() {
  renderKPI();
  setupDateNav();
  renderDay(currentDate);
  renderEquityChart();
  setupTimeFilter();
  document.getElementById('loadingOverlay').classList.add('hide');
}

/* ─── KPI OVERVIEW ─── */
function renderKPI() {
  const s = DATA.summary;
  const items = [
    {
      label: '总资产 TOTAL',
      value: '¥' + Number(s.current_value).toLocaleString(),
      cls: 'gold',
      sub: '初始 ¥' + Number(s.initial_capital).toLocaleString()
    },
    {
      label: '累计收益率 RETURN',
      value: (s.total_return_pct >= 0 ? '+' : '') + s.total_return_pct.toFixed(2) + '%',
      cls: s.total_return_pct >= 0 ? 'positive' : 'negative',
      sub: '年化 ' + (s.cagr_pct || 0).toFixed(1) + '% · ×' + (s.current_value / s.initial_capital).toFixed(2) + '倍'
    },
    {
      label: '胜率 WIN RATE',
      value: s.win_rate.toFixed(1) + '%',
      cls: 'gold',
      sub: s.total_trades + ' 笔交易'
    },
    {
      label: '最大回撤 MAX DD',
      value: s.max_drawdown.toFixed(1) + '%',
      cls: 'negative',
      sub: 'Sharpe: ' + (s.sharpe_ratio || 0).toFixed(2)
    },
    {
      label: '今日收益',
      value: '--',
      cls: '',
      sub: '选择日期查看',
      id: 'kpiToday'
    }
  ];
  document.getElementById('kpiStrip').innerHTML = items.map(it => {
    const idAttr = it.id ? ' id="' + it.id + '"' : '';
    return '<div class="kpi"' + idAttr + '><div class="kpi-label">' + it.label + '</div>' +
      '<div class="kpi-value ' + it.cls + '">' + it.value + '</div>' +
      '<div class="kpi-sub">' + it.sub + '</div></div>';
  }).join('');
}

/* ─── DATE NAVIGATION ─── */
function setupDateNav() {
  const input = document.getElementById('dateInput');
  input.value = currentDate;
  input.addEventListener('change', () => {
    const val = input.value;
    if (DATA.daily_data[val]) {
      currentDate = val;
      renderDay(currentDate);
    } else {
      // 找最接近的交易日
      const nearest = tradingDates.reduce((prev, curr) =>
        Math.abs(new Date(curr) - new Date(val)) < Math.abs(new Date(prev) - new Date(val)) ? curr : prev
      );
      currentDate = nearest;
      input.value = nearest;
      renderDay(currentDate);
    }
  });

  document.getElementById('btnPrevDay').addEventListener('click', () => {
    const idx = tradingDates.indexOf(currentDate);
    if (idx > 0) {
      currentDate = tradingDates[idx - 1];
      input.value = currentDate;
      renderDay(currentDate);
    }
  });

  document.getElementById('btnNextDay').addEventListener('click', () => {
    const idx = tradingDates.indexOf(currentDate);
    if (idx < tradingDates.length - 1) {
      currentDate = tradingDates[idx + 1];
      input.value = currentDate;
      renderDay(currentDate);
    }
  });

  document.getElementById('btnLatest').addEventListener('click', () => {
    currentDate = tradingDates[tradingDates.length - 1];
    input.value = currentDate;
    renderDay(currentDate);
  });
}

/* ─── RENDER A SINGLE DAY ─── */
function renderDay(date) {
  const day = DATA.daily_data[date];
  if (!day) {
    document.getElementById('positionsGrid').innerHTML = '<div class="no-data">该日期无交易数据</div>';
    document.getElementById('signalsList').innerHTML = '<div class="no-data">无信号</div>';
    document.getElementById('commentary').innerHTML = '<p>暂无点评</p>';
    return;
  }

  document.getElementById('posDate').textContent = date;

  // Update today's KPI
  const todayKpi = document.getElementById('kpiToday');
  if (todayKpi) {
    const val = day.daily_return_pct;
    const cls = val >= 0 ? 'positive' : 'negative';
    todayKpi.innerHTML =
      '<div class="kpi-label">今日收益 TODAY</div>' +
      '<div class="kpi-value ' + cls + '">' + (val >= 0 ? '+' : '') + val.toFixed(2) + '%</div>' +
      '<div class="kpi-sub">组合净值 ¥' + Number(day.portfolio_value).toLocaleString() + '</div>';
  }

  renderPositions(day.positions || []);
  renderSignals(day.signals || day.trades_today || []);
  renderCommentary(day.commentary || '暂无点评');
}

/* ─── POSITIONS ─── */
function renderPositions(positions) {
  if (!positions || !Array.isArray(positions) || positions.length === 0) {
    document.getElementById('positionsGrid').innerHTML =
      '<div class="no-data">当日无持仓明细（查看交易记录请点击具体交易日期）</div>';
    return;
  }
  // Ensure 5 slots
  const slots = [];
  for (let i = 1; i <= 5; i++) {
    const pos = positions.find(p => p.slot === i);
    slots.push(pos || { slot: i, status: 'empty' });
  }

  document.getElementById('positionsGrid').innerHTML = slots.map(pos => {
    if (pos.status === 'empty') {
      return '<div class="pos-card empty">' +
        '<div class="pos-slot">仓位 ' + pos.slot + '</div>' +
        '<div class="pos-status">⚪</div>' +
        '<div class="pos-empty-text">空仓 · 等待信号</div>' +
        '</div>';
    }

    const retCls = pos.return_pct >= 0 ? 'pos' : 'neg';
    const retSign = pos.return_pct >= 0 ? '+' : '';
    const statusEmoji = pos.status === 'holding' ? '🟢' : (pos.status === 'selling' ? '🔴' : '⚪');
    const cardCls = pos.status === 'holding' ? 'holding' : (pos.status === 'selling' ? 'selling' : '');

    return '<div class="pos-card ' + cardCls + '">' +
      '<div class="pos-slot">仓位 ' + pos.slot + '</div>' +
      '<div class="pos-status">' + statusEmoji + '</div>' +
      '<div class="pos-stock">' + esc(pos.name) + '</div>' +
      '<div class="pos-code">' + pos.code + '</div>' +
      '<div class="pos-strategy">' + esc(pos.strategy) + '</div>' +
      '<div class="pos-info">' +
        '<div><span class="label">买入:</span> ' + pos.buy_date + ' ¥' + pos.buy_price.toFixed(2) + '</div>' +
        '<div><span class="label">当前:</span> ¥' + pos.current_price.toFixed(2) + '</div>' +
        '<div><span class="label">持仓:</span> ' + pos.holding_days + '天</div>' +
      '</div>' +
      '<div class="pos-return ' + retCls + '">' + retSign + pos.return_pct.toFixed(2) + '%</div>' +
      '</div>';
  }).join('');
}

/* ─── SIGNALS ─── */
function renderSignals(signals) {
  if (signals.length === 0) {
    document.getElementById('signalsList').innerHTML = '<div class="no-data">今日无买卖信号</div>';
    return;
  }

  document.getElementById('signalsList').innerHTML = signals.map(sig => {
    const isBuy = sig.type === 'buy';
    const typeCls = isBuy ? 'buy' : 'sell';
    const typeText = isBuy ? '买入' : '卖出';
    const icon = isBuy ? '🟢' : '🔴';

    return '<div class="signal-item ' + typeCls + '">' +
      '<div class="signal-icon">' + icon + '</div>' +
      '<div class="signal-content">' +
        '<div class="signal-header">' +
          '<span class="signal-type ' + typeCls + '">' + typeText + '</span>' +
          '<span class="signal-stock">' + esc(sig.name) + ' (' + sig.code + ')</span>' +
          '<span class="signal-price">¥' + sig.price.toFixed(2) + '</span>' +
          '<span class="signal-strategy">' + esc(sig.strategy) + '</span>' +
        '</div>' +
        '<div class="signal-reason">' + esc(sig.reason) + '</div>' +
      '</div>' +
    '</div>';
  }).join('');
}

/* ─── COMMENTARY ─── */
function renderCommentary(text) {
  const paragraphs = text.split('\n').filter(l => l.trim());
  document.getElementById('commentary').innerHTML =
    paragraphs.map(p => '<p>' + esc(p) + '</p>').join('');
}

/* ─── EQUITY CHART (ECharts) ─── */
function renderEquityChart() {
  const chartDom = document.getElementById('equityChart');
  equityChart = echarts.init(chartDom, null, { renderer: 'canvas' });

  updateEquityChart();

  // Responsive resize
  window.addEventListener('resize', () => {
    if (equityChart) equityChart.resize();
  });
}

function updateEquityChart() {
  const dates = tradingDates.slice();
  let filteredDates = dates;

  if (currentTimeRange !== 'all') {
    const lastDate = new Date(dates[dates.length - 1]);
    let startDate;
    if (currentTimeRange === '1m') {
      startDate = new Date(lastDate);
      startDate.setMonth(startDate.getMonth() - 1);
    } else if (currentTimeRange === '3m') {
      startDate = new Date(lastDate);
      startDate.setMonth(startDate.getMonth() - 3);
    } else if (currentTimeRange === '6m') {
      startDate = new Date(lastDate);
      startDate.setMonth(startDate.getMonth() - 6);
    }
    filteredDates = dates.filter(d => new Date(d) >= startDate);
  }

  const returns = filteredDates.map(d => DATA.daily_data[d].cumulative_return_pct);
  const benchmark = filteredDates.map(d => DATA.daily_data[d].benchmark_return_pct || 0);

  const option = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#141d2b',
      borderColor: '#1a2436',
      textStyle: { color: '#e8edf5', fontFamily: 'JetBrains Mono', fontSize: 11 },
      formatter: function(params) {
        let html = '<div style="font-weight:600;margin-bottom:4px">' + params[0].axisValue + '</div>';
        params.forEach(p => {
          const color = p.seriesIndex === 0 ? '#d4a534' : '#38b6ff';
          html += '<div style="color:' + color + '">' + p.seriesName + ': ' +
            (p.value >= 0 ? '+' : '') + p.value.toFixed(2) + '%</div>';
        });
        return html;
      }
    },
    legend: {
      data: ['策略收益', '沪深300'],
      textStyle: { color: '#7a8ba5', fontFamily: 'JetBrains Mono', fontSize: 11 },
      top: 0,
      right: 10
    },
    grid: { left: 60, right: 20, top: 40, bottom: 30 },
    xAxis: {
      type: 'category',
      data: filteredDates,
      axisLine: { lineStyle: { color: '#1a2436' } },
      axisLabel: { color: '#4a5a72', fontFamily: 'JetBrains Mono', fontSize: 10 },
      splitLine: { show: false }
    },
    yAxis: {
      type: 'value',
      axisLine: { lineStyle: { color: '#1a2436' } },
      axisLabel: {
        color: '#4a5a72',
        fontFamily: 'JetBrains Mono',
        fontSize: 10,
        formatter: '{value}%'
      },
      splitLine: { lineStyle: { color: '#111927', type: 'dashed' } }
    },
    series: [
      {
        name: '策略收益',
        type: 'line',
        data: returns,
        smooth: true,
        symbol: 'none',
        lineStyle: { color: '#d4a534', width: 2 },
        areaStyle: {
          color: {
            type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(212,165,52,0.25)' },
              { offset: 1, color: 'rgba(212,165,52,0)' }
            ]
          }
        }
      },
      {
        name: '沪深300',
        type: 'line',
        data: benchmark,
        smooth: true,
        symbol: 'none',
        lineStyle: { color: '#38b6ff', width: 1.5, type: 'dashed' },
        areaStyle: null
      }
    ]
  };

  equityChart.setOption(option, true);

  // Mark current date
  equityChart.on('click', function(params) {
    if (params.componentType === 'series') {
      const clickedDate = filteredDates[params.dataIndex];
      if (clickedDate && DATA.daily_data[clickedDate]) {
        currentDate = clickedDate;
        document.getElementById('dateInput').value = currentDate;
        renderDay(currentDate);
      }
    }
  });
}

function setupTimeFilter() {
  document.querySelectorAll('#timeFilter .time-pill').forEach(pill => {
    pill.addEventListener('click', () => {
      document.querySelectorAll('#timeFilter .time-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      currentTimeRange = pill.dataset.range;
      updateEquityChart();
    });
  });
}

/* ─── UTILS ─── */
function esc(s) {
  if (!s) return '';
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
