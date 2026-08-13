/**
 * Task#322 前端bug复现/验证harness (jsdom, 不动线上server)
 * 用法: node run_harness.js <scenario> <durationSec>
 *   scenario:
 *     baseline    - 原样加载页面: 候选API注入1.5s慢响应(模拟真实网络), 行情价格每次+0.1%扰动
 *     posfail     - 首次/api/positions返回500(模拟init时接口抖动), 之后正常 → 复现bug2曲线冻结
 * 观测:
 *   1) 每50ms采样持仓表首个价格单元格textContent → 检测已填值后再次出现'--'的闪烁窗口
 *   2) 记录navChart每次setOption的series尾点值 → 检测收益曲线尾点是否随行情变动
 * 输出JSON时间线到 stdout 尾部 [HARNESS_RESULT] 行
 */
const { JSDOM, VirtualConsole } = require('jsdom');
const http = require('http');

// 用node:http代替undici全局fetch: python简易HTTP服务与undici keep-alive解析断言冲突(assert !paused)
function httpGet(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, { headers: { Connection: 'close' } }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const body = Buffer.concat(chunks).toString('utf8');
        resolve({
          ok: res.statusCode >= 200 && res.statusCode < 300,
          status: res.statusCode, statusText: res.statusMessage || '',
          text: async () => body,
          json: async () => JSON.parse(body),
        });
      });
    });
    req.on('error', reject);
    req.setTimeout(10000, () => { req.destroy(new Error('timeout')); });
  });
}

const scenario = process.argv[2] || 'baseline';
const durationSec = parseInt(process.argv[3] || '35', 10);
const BASE = 'http://localhost/';

let quoteTick = 0;      // 行情扰动计数
let posCallCount = 0;   // /api/positions调用计数

async function bridgedFetch(input, opts) {
  const url = new URL(String(input), BASE).href;
  const path = new URL(url).pathname + new URL(url).search;

  // scenario: posfail — 第1次 /api/positions 返回500 (模拟init时接口抖动)
  if (scenario === 'posfail' && new URL(url).pathname === '/api/positions') {
    posCallCount++;
    if (posCallCount === 1) {
      return { ok: false, status: 500, statusText: 'simulated init failure', json: async () => ({}) };
    }
  }

  const resp = await httpGet(url);

  // 候选API注入1.5s慢响应: 暴露"重建后等全部domJobs才回填"的闪烁窗口
  if (new URL(url).pathname === '/api/candidates/latest') {
    await new Promise(r => setTimeout(r, 1500));
  }

  // 行情扰动: 每次请求价格整体 ×(1 + tick*0.001), 模拟盘中价格变化 → 曲线尾点应随之变化
  if (new URL(url).pathname === '/api/realtime_quotes') {
    const body = await resp.json();
    quoteTick++;
    const k = 1 + quoteTick * 0.001;
    for (const code of Object.keys(body.data || {})) {
      const q = body.data[code];
      if (typeof q.price === 'number') q.price = +(q.price * k).toFixed(2);
    }
    return { ok: true, status: 200, statusText: 'OK', json: async () => body };
  }
  return resp;
}

(async () => {
  const htmlResp = await httpGet(BASE);
  let html = await htmlResp.text();
  // 剥离echarts真实库(jsdom无canvas), 由beforeParse注入记录型stub
  html = html.replace(/<script src="\/echarts\.min\.js"><\/script>/, '');

  const chartLog = [];   // {t, domId, tailReturn, nPoints, hasXAxis}
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('error', (...a) => errors.push({ t: Date.now(), type: 'console.error', msg: a.map(String).join(' ').slice(0, 300) }));
  vc.on('jsdomError', (e) => errors.push({ t: Date.now(), type: 'jsdomError', msg: String(e).slice(0, 300) }));

  const dom = new JSDOM(html, {
    url: BASE,
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(window) {
      // 盘中时钟mock: 把页面时间平移到今日10:30(isTradeSession=true), 否则盘后跑harness时
      // loadPositions/盯市链都不启动, 验不到bug1/bug2盘中行为。时间仍正常流动。
      const RealDate = Date;
      const target = new RealDate(); target.setHours(10, 30, 0, 0);
      const dow = target.getDay();
      if (dow === 0) target.setDate(target.getDate() + 1);      // 周日→周一
      else if (dow === 6) target.setDate(target.getDate() + 2); // 周六→周一
      const offset = target.getTime() - RealDate.now();
      class MockDate extends RealDate {
        constructor(...args) {
          if (args.length === 0) super(RealDate.now() + offset);
          else super(...args);
        }
        static now() { return RealDate.now() + offset; }
      }
      window.Date = MockDate;
      window.fetch = bridgedFetch;
      function LinearGradient() {}
      window.echarts = {
        graphic: { LinearGradient },
        init(domEl) {
          const id = domEl && domEl.id || '?';
          return {
            setOption(opt) {
              try {
                const s = opt && opt.series && opt.series[0];
                const data = s && s.data;
                chartLog.push({
                  t: Date.now(), domId: id,
                  nPoints: Array.isArray(data) ? data.length : null,
                  tail: Array.isArray(data) && data.length ? data[data.length - 1] : null,
                  hasXAxis: !!opt.xAxis,
                });
              } catch (e) { /* 记录失败不影响页面 */ }
            },
            resize() {}, dispose() {},
          };
        },
      };
    },
  });

  const doc = dom.window.document;

  // 50ms采样持仓表价格单元格: 检测闪烁('--'在已填值后再次出现)
  const samples = [];
  const t0 = Date.now();
  const sampler = setInterval(() => {
    const cells = doc.querySelectorAll('[data-code][data-field="price"]');
    const txts = Array.from(cells).slice(0, 5).map(c => c.textContent.trim());
    const prev = samples.length ? samples[samples.length - 1].v : null;
    const v = txts.join('|');
    if (v !== prev) samples.push({ ms: Date.now() - t0, v });
  }, 50);

  await new Promise(r => setTimeout(r, durationSec * 1000));
  clearInterval(sampler);

  // 分析1: 闪烁窗口 = 出现过真实价后, 又整体/局部回到'--'的时间段
  let filledOnce = false; const flickers = [];
  for (const s of samples) {
    const hasPrice = /\d/.test(s.v);
    const hasDash = s.v.split('|').some(x => x === '--');
    if (hasPrice && !hasDash) filledOnce = true;
    else if (filledOnce && hasDash) flickers.push(s);
  }
  // 分析2: navChart尾点变化次数(去重)
  const navTails = chartLog.filter(c => c.domId === 'navChart').map(c => JSON.stringify(c.tail));
  const uniqueTails = [...new Set(navTails)];

  console.log('[HARNESS_RESULT] ' + JSON.stringify({
    scenario, durationSec, quoteTick, posCallCount,
    flickerCount: flickers.length,
    flickerSamples: flickers.slice(0, 10),
    navSetOptionCalls: navTails.length,
    uniqueNavTails: uniqueTails,
    cellTimeline: samples.slice(0, 40),
    pageErrors: errors.slice(0, 10),
  }, null, 1));
  process.exit(0);
})().catch(e => { console.error('HARNESS FATAL', e); process.exit(1); });
