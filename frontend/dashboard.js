/**
 * AIWealth 回测策略 Dashboard
 * 纯前端实现，加载JSON数据，ECharts渲染图表
 * 数据源：组合档案(frontend/data静态) + 在产5策略solo档案(server /data/solo/只读路由)
 */

(function() {
  'use strict';

  // ===== 配置 =====
  // 在产5策略solo档案经 server.py /data/solo/ 白名单路由读取 logs/backtest/solo/，
  // 每周日全量刷新/参数升级重测后自动更新，无需同步。
  const STRATEGIES = {
    combined_5slot_new: {
      file: './data/combined_5slot_new_trades.json',
      label: '5策略联合'
    },
    firstboard_low_open_dip_v2: {
      file: '/data/solo/firstboard_low_open_dip_v2_trades.json',
      label: '首板低吸'
    },
    amplitude_reversal: {
      file: '/data/solo/amplitude_reversal_trades.json',
      label: '巨振反转'
    },
    gem_star_late_seal: {
      file: '/data/solo/gem_star_late_seal_trades.json',
      label: '创科晚封'
    },
    big_yang_low_open_v2: {
      file: '/data/solo/big_yang_low_open_v2_trades.json',
      label: '大阳低吸'
    },
    two_board_pullback_dip_h1c: {
      file: '/data/solo/two_board_pullback_dip_h1c_trades.json',
      label: '双板回调低吸'
    }
  };

  let currentStrategy = 'combined_5slot_new';

  // [Task#209] 策略中文正名(server.py STRATEGY_CN镜像, 渲染层转换, 数据内部英文代号不动)
  const STRATEGY_CN = {
    firstboard_low_open_dip_v2: '首板低吸',
    amplitude_reversal: '巨振反转',
    gem_star_late_seal: '创科晚封',
    big_yang_low_open_v2: '大阳低吸',
    two_board_pullback_dip_h1c: '双板回调低吸',
    early_surge_chase_0940: '早盘冲板追击',
    limitup_early_seal: '涨停早封',
    gem_star_limitup_low_open: '创科涨停低开'
  };
  let dataCache = {};
  let equityChartInstance = null;
  let monthlyChartInstance = null;
  let currentSort = { col: 'id', asc: true };
  let filteredTrades = [];
  let resizeHandlersBound = false;

  // ===== 初始化 =====
  async function init() {
    try {
      setupTabs();
      await loadStrategy(currentStrategy);
    } catch (e) {
      showError('初始化失败: ' + e.message);
    }
  }

  function showError(msg) {
    var el = document.getElementById('loadingMsg');
    if (el) {
      el.style.display = 'flex';
      el.innerHTML = '<div class="error-msg"><strong>Dashboard Error</strong><br>' + msg + '</div>';
    }
  }

  function setupTabs() {
    // HTML内tab为历史硬编码（含退役策略），此处按STRATEGIES动态重建
    const tabBar = document.querySelector('.tabs');
    if (tabBar) {
      tabBar.innerHTML = Object.keys(STRATEGIES).map(k =>
        `<button class="tab-btn${k === currentStrategy ? ' active' : ''}" data-strategy="${k}">${STRATEGIES[k].label}</button>`
      ).join('');
    }
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentStrategy = btn.dataset.strategy;
        await loadStrategy(currentStrategy);
      });
    });
  }

  // ===== 数据适配层 =====
  // 档案实际结构: { mode, summary|combined_summary, strategies, daily_nav{date:nav}, trades[] }
  // trades字段: code/strategy_name/buy_date/buy_price/sell_date/sell_price/hold_hours/profit_pct/reason
  // 此处适配为渲染层所需 { summary, trades, equity_curve, monthly_returns }，不改档案。
  function adaptData(raw, label) {
    const s = raw.summary || raw.combined_summary || {};
    const initialCapital = s.initial_capital || raw.initial_capital || 1000000;
    const slotCount = raw.n_slots || (raw.strategies ? raw.strategies.length : 1);

    const summary = {
      strategy_name: label,
      period: (s.start_date || '?') + ' ~ ' + (s.end_date || '?'),
      cagr_pct: s.cagr_pct != null ? s.cagr_pct : 0,
      final_capital: s.final_nav != null ? s.final_nav : initialCapital,
      initial_capital: initialCapital,
      win_rate_pct: s.win_rate_pct != null ? s.win_rate_pct : 0,
      avg_return_pct: s.avg_profit_pct != null ? s.avg_profit_pct : 0,
      max_drawdown_pct: s.max_drawdown_pct != null ? s.max_drawdown_pct : 0,
      total_trades: s.n_trades != null ? s.n_trades : (raw.trades || []).length,
      fee_rate: '—',
      slot: slotCount,
      compliance: raw.mode || ''
    };

    const trades = (raw.trades || []).map((t, i) => ({
      id: i + 1,
      code: t.code || '',
      name: t.name || '—',
      strategy: t.strategy_name || t.strategy || '',
      buy_date: t.buy_date || '',
      buy_price: t.buy_price != null ? t.buy_price : 0,
      sell_date: t.sell_date || '',
      sell_price: t.sell_price != null ? t.sell_price : 0,
      sell_reason: t.reason || t.sell_reason || '',
      return_pct: t.profit_pct != null ? t.profit_pct : (t.return_pct != null ? t.return_pct : 0),
      hold_hours: t.hold_hours != null ? t.hold_hours : 0,
      capital_after: t.capital_after != null ? t.capital_after : null
    }));

    // daily_nav dict → equity_curve[{date, capital}]
    const nav = raw.daily_nav || {};
    const navDates = Object.keys(nav).sort();
    const equity_curve = navDates.map(d => ({ date: d, capital: nav[d] }));

    // daily_nav → 月度收益（月末nav环比）
    const monthly_returns = [];
    let prevMonthNav = initialCapital;
    let curMonth = null;
    let lastNav = initialCapital;
    navDates.forEach(d => {
      const m = d.substring(0, 7);
      if (curMonth !== null && m !== curMonth) {
        monthly_returns.push({ month: curMonth, return_pct: (lastNav / prevMonthNav - 1) * 100 });
        prevMonthNav = lastNav;
      }
      curMonth = m;
      lastNav = nav[d];
    });
    if (curMonth !== null) {
      monthly_returns.push({ month: curMonth, return_pct: (lastNav / prevMonthNav - 1) * 100 });
    }

    return { summary, trades, equity_curve, monthly_returns };
  }

  async function loadStrategy(key) {
    const loadingMsg = document.getElementById('loadingMsg');
    const mainContent = document.getElementById('mainContent');

    if (dataCache[key]) {
      renderAll(dataCache[key]);
      return;
    }

    loadingMsg.style.display = 'flex';
    if (mainContent) mainContent.style.display = 'none';

    try {
      const resp = await fetch(STRATEGIES[key].file);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const raw = await resp.json();
      const data = adaptData(raw, STRATEGIES[key].label);
      dataCache[key] = data;
      renderAll(data);
    } catch (e) {
      loadingMsg.innerHTML = `
        <div class="error-msg">
          <strong>数据加载失败</strong><br>
          错误: ${e.message}<br><br>
          如果使用 file:// 协议打开，请改用本地HTTP服务器：<br>
          <code style="background:#222;padding:4px 8px;border-radius:4px;">
            cd /home/AIWealth/frontend && python3 -m http.server 8080
          </code><br>
          然后访问 <code>http://localhost:8080/dashboard.html</code>
        </div>`;
    }
  }

  // ===== 渲染入口 =====
  function renderAll(data) {
    document.getElementById('loadingMsg').style.display = 'none';
    var mainContent = document.getElementById('mainContent');
    mainContent.style.display = 'block';

    try {
      renderKPI(data.summary);
      renderAnnualStats(data.trades);
      renderTradeTable(data.trades);
    } catch (e) {
      showError('渲染数据失败: ' + e.message);
      return;
    }

    // Defer chart initialization to next frame to ensure layout is computed
    if (typeof echarts === 'undefined') {
      console.warn('ECharts not loaded, skipping charts');
      return;
    }
    requestAnimationFrame(function() {
      try {
        var eqDom = document.getElementById('equityChart');
        var moDom = document.getElementById('monthlyChart');
        // Ensure containers have dimensions before ECharts init
        if (eqDom && eqDom.clientWidth > 0) {
          renderEquityCurve(data.equity_curve, data.summary);
        }
        if (moDom && moDom.clientWidth > 0) {
          renderMonthlyReturns(data.monthly_returns);
        }
      } catch (e) {
        console.error('Chart render error:', e);
        // Charts failed but table/KPI still work
      }
    });
  }

  // ===== KPI 卡片 =====
  function renderKPI(summary) {
    const grid = document.getElementById('kpiGrid');
    const fmt = (n) => n >= 10000000 ? (n / 100000000).toFixed(2) + '亿' :
                       n >= 10000 ? (n / 10000).toFixed(0) + '万' : n.toFixed(0);

    grid.innerHTML = `
      <div class="kpi-card">
        <div class="label">策略名称</div>
        <div class="value blue" style="font-size:16px">${summary.strategy_name}</div>
        <div class="sub">${summary.period}</div>
      </div>
      <div class="kpi-card">
        <div class="label">年化收益 CAGR</div>
        <div class="value green">${summary.cagr_pct.toFixed(2)}%</div>
        <div class="sub">初始100万 → ${fmt(summary.final_capital)}</div>
      </div>
      <div class="kpi-card">
        <div class="label">胜率</div>
        <div class="value ${summary.win_rate_pct >= 50 ? 'green' : 'red'}">${summary.win_rate_pct.toFixed(2)}%</div>
        <div class="sub">笔均收益 ${summary.avg_return_pct.toFixed(2)}%</div>
      </div>
      <div class="kpi-card">
        <div class="label">最大回撤</div>
        <div class="value red">${summary.max_drawdown_pct.toFixed(2)}%</div>
        <div class="sub">费率 ${summary.fee_rate}</div>
      </div>
      <div class="kpi-card">
        <div class="label">总交易笔数</div>
        <div class="value gold">${summary.total_trades}</div>
        <div class="sub">slot=${summary.slot} · ${summary.compliance}</div>
      </div>
      <div class="kpi-card">
        <div class="label">最终资金</div>
        <div class="value green">${fmt(summary.final_capital)}</div>
        <div class="sub">倍数 ${(summary.final_capital / summary.initial_capital).toFixed(1)}x</div>
      </div>
    `;
  }

  // ===== 资金曲线 =====
  function renderEquityCurve(equityCurve, summary) {
    const dom = document.getElementById('equityChart');
    if (equityChartInstance) equityChartInstance.dispose();
    equityChartInstance = echarts.init(dom);

    const dates = equityCurve.map(d => d.date);
    const values = equityCurve.map(d => d.capital);

    const option = {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(20,22,30,0.95)',
        borderColor: '#333',
        textStyle: { color: '#e8e8e8', fontSize: 12 },
        formatter: function(params) {
          const p = params[0];
          const val = p.value;
          const fmtVal = val >= 100000000 ? (val/100000000).toFixed(4) + '亿' :
                         val >= 10000 ? (val/10000).toFixed(2) + '万' : val.toFixed(2);
          const ret = ((val / summary.initial_capital - 1) * 100).toFixed(2);
          return `${p.axisValue}<br/>资金: ¥${fmtVal}<br/>总收益: ${ret}%`;
        }
      },
      grid: { left: 80, right: 30, top: 30, bottom: 50 },
      xAxis: {
        type: 'category',
        data: dates,
        axisLine: { lineStyle: { color: '#333' } },
        axisLabel: { color: '#8a8f9e', fontSize: 11 },
        splitLine: { show: false }
      },
      yAxis: {
        type: 'value',
        axisLine: { show: false },
        axisLabel: {
          color: '#8a8f9e', fontSize: 11,
          formatter: v => v >= 100000000 ? (v/100000000).toFixed(1)+'亿' :
                         v >= 10000 ? (v/10000).toFixed(0)+'万' : v
        },
        splitLine: { lineStyle: { color: '#1e2130' } }
      },
      series: [{
        type: 'line',
        data: values,
        smooth: true,
        symbol: 'none',
        lineStyle: { color: '#448aff', width: 2 },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(68,138,255,0.3)' },
            { offset: 1, color: 'rgba(68,138,255,0.02)' }
          ])
        }
      }],
      dataZoom: [{
        type: 'inside', start: 0, end: 100
      }, {
        type: 'slider', start: 0, end: 100, height: 20, bottom: 5,
        borderColor: '#333', fillerColor: 'rgba(68,138,255,0.2)',
        textStyle: { color: '#8a8f9e' }
      }]
    };

    equityChartInstance.setOption(option);
    if (!resizeHandlersBound) {
      resizeHandlersBound = true;
      window.addEventListener('resize', function() {
        if (equityChartInstance) equityChartInstance.resize();
        if (monthlyChartInstance) monthlyChartInstance.resize();
      });
    }
  }

  // ===== 月度收益柱状图 =====
  function renderMonthlyReturns(monthlyReturns) {
    const dom = document.getElementById('monthlyChart');
    if (monthlyChartInstance) monthlyChartInstance.dispose();
    monthlyChartInstance = echarts.init(dom);

    const months = monthlyReturns.map(d => d.month);
    const values = monthlyReturns.map(d => d.return_pct);
    const colors = values.map(v => v >= 0 ? '#00c853' : '#ff1744');

    const option = {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(20,22,30,0.95)',
        borderColor: '#333',
        textStyle: { color: '#e8e8e8', fontSize: 12 },
        formatter: params => `${params[0].axisValue}<br/>收益率: ${params[0].value.toFixed(2)}%`
      },
      grid: { left: 60, right: 20, top: 20, bottom: 50 },
      xAxis: {
        type: 'category',
        data: months,
        axisLine: { lineStyle: { color: '#333' } },
        axisLabel: { color: '#8a8f9e', fontSize: 10, rotate: 45 },
        splitLine: { show: false }
      },
      yAxis: {
        type: 'value',
        axisLine: { show: false },
        axisLabel: { color: '#8a8f9e', fontSize: 11, formatter: '{value}%' },
        splitLine: { lineStyle: { color: '#1e2130' } }
      },
      series: [{
        type: 'bar',
        data: values.map((v, i) => ({ value: v, itemStyle: { color: colors[i] } })),
        barMaxWidth: 16
      }],
      dataZoom: [{
        type: 'inside', start: 0, end: 100
      }]
    };

    monthlyChartInstance.setOption(option);
  }

  // ===== 年度分组统计 =====
  function renderAnnualStats(trades) {
    const yearMap = {};
    trades.forEach(t => {
      const year = t.buy_date.substring(0, 4);
      if (!yearMap[year]) yearMap[year] = { trades: [], wins: 0, totalReturn: 0 };
      yearMap[year].trades.push(t);
      if (t.return_pct > 0) yearMap[year].wins++;
      yearMap[year].totalReturn += t.return_pct;
    });

    const grid = document.getElementById('annualGrid');
    grid.innerHTML = Object.keys(yearMap).sort().map(year => {
      const d = yearMap[year];
      const cnt = d.trades.length;
      const wr = (d.wins / cnt * 100).toFixed(1);
      const avgR = (d.totalReturn / cnt).toFixed(2);
      const totalR = d.totalReturn.toFixed(1);
      return `
        <div class="annual-card">
          <div class="year">${year}年</div>
          <div class="stats">
            交易笔数: <strong>${cnt}</strong><br>
            胜率: <strong>${wr}%</strong><br>
            笔均收益: <strong>${avgR}%</strong><br>
            累计收益: <strong>${totalR}%</strong>
          </div>
        </div>`;
    }).join('');
  }

  // ===== 交易明细表格 =====
  function renderTradeTable(trades) {
    filteredTrades = [...trades];

    // 填充策略过滤下拉(value保持英文代号, 展示用中文正名)
    const strategies = [...new Set(trades.map(t => t.strategy))];
    const filterSel = document.getElementById('filterStrategy');
    filterSel.innerHTML = '<option value="all">全部策略</option>' +
      strategies.map(s => `<option value="${s}">${STRATEGY_CN[s] || s}</option>`).join('');

    // 绑定事件
    document.getElementById('searchInput').oninput = () => applyFilters(trades);
    document.getElementById('filterResult').onchange = () => applyFilters(trades);
    document.getElementById('filterStrategy').onchange = () => applyFilters(trades);

    // 表头排序
    document.querySelectorAll('#tradeTable thead th').forEach(th => {
      th.onclick = () => {
        const col = th.dataset.col;
        if (currentSort.col === col) {
          currentSort.asc = !currentSort.asc;
        } else {
          currentSort.col = col;
          currentSort.asc = true;
        }
        document.querySelectorAll('#tradeTable thead th').forEach(h => {
          h.classList.remove('sorted', 'asc');
        });
        th.classList.add('sorted');
        if (currentSort.asc) th.classList.add('asc');
        renderTableBody();
      };
    });

    applyFilters(trades);
  }

  function applyFilters(trades) {
    const search = document.getElementById('searchInput').value.toLowerCase();
    const resultFilter = document.getElementById('filterResult').value;
    const strategyFilter = document.getElementById('filterStrategy').value;

    filteredTrades = trades.filter(t => {
      if (search && !t.code.toLowerCase().includes(search) && !t.name.toLowerCase().includes(search)) return false;
      if (resultFilter === 'win' && t.return_pct <= 0) return false;
      if (resultFilter === 'loss' && t.return_pct >= 0) return false;
      if (strategyFilter !== 'all' && t.strategy !== strategyFilter) return false;
      return true;
    });

    renderTableBody();
  }

  function renderTableBody() {
    // Sort
    const col = currentSort.col;
    const asc = currentSort.asc;
    filteredTrades.sort((a, b) => {
      let va = a[col], vb = b[col];
      if (va == null) va = -Infinity;
      if (vb == null) vb = -Infinity;
      if (typeof va === 'string') {
        return asc ? va.localeCompare(vb) : vb.localeCompare(va);
      }
      return asc ? va - vb : vb - va;
    });

    document.getElementById('tradeCount').textContent = `显示 ${filteredTrades.length} 笔`;

    const tbody = document.getElementById('tradeBody');
    const fmtCap = v => v == null ? '—' :
                        v >= 10000000 ? (v/10000000).toFixed(2)+'千万' :
                        v >= 10000 ? (v/10000).toFixed(1)+'万' : v.toFixed(0);

    tbody.innerHTML = filteredTrades.map(t => {
      const retClass = t.return_pct > 0 ? 'td-green' : t.return_pct < 0 ? 'td-red' : '';
      return `<tr>
        <td>${t.id}</td>
        <td>${t.code}</td>
        <td>${t.name}</td>
        <td>${STRATEGY_CN[t.strategy] || t.strategy}</td>
        <td>${t.buy_date}</td>
        <td>${t.buy_price.toFixed(2)}</td>
        <td>${t.sell_date}</td>
        <td>${t.sell_price.toFixed(2)}</td>
        <td>${t.sell_reason}</td>
        <td class="${retClass}">${t.return_pct > 0 ? '+' : ''}${t.return_pct.toFixed(2)}%</td>
        <td>${t.hold_hours}h</td>
        <td>${fmtCap(t.capital_after)}</td>
      </tr>`;
    }).join('');
  }

  // ===== 启动 =====
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    // DOMContentLoaded already fired
    init();
  }
})();
