#!/usr/bin/env node
/*
 * 闸门：顶栏「每页显示什么」必须严格等于该页应有的集合 —— 切页不能串。
 *
 * 为什么要有它（2026-10-15）
 *   上一版 原型是「一页一个顶栏」（proto/*.html 的 .topact）。实现上不是每页一个 DOM，
 *   而是**一条共用顶栏 + 按页切 hidden**。这种做法的唯一风险就是「串页」：
 *   切走之后上一页的按钮没关掉，残留在顶栏上。
 *   靠"记得每个分支都写对"是不可靠的 —— 每加一页就多一个漏的机会。
 *   所以：结构上用「先全关、再开一个」（见 applyTopbar），行为上用这道闸门钉。
 *
 * 判定是**全等**，不是"包含"：
 *   把顶栏里所有可切换的元素列出来，看当前**可见**的那些，
 *   跟下面 EXPECT 里该页应有的集合一一对应 —— 多一个、少一个都算红。
 *   然后**乱序来回切 3 轮**，每一跳都比一遍（残留一定会被抓到）。
 *
 * 用法：
 *   node tools/verify_topbar.js [base]
 *   node tools/verify_topbar.js --broken     # 反向测试：故意留一个容器不关，必须变红
 */
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = [process.env.CHROME_PATH,
                'C:/Program Files/Google/Chrome/Application/chrome.exe',
                'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe']
  .filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) { console.error('[顶栏闸门] 没找到 Chrome，请用 CHROME_PATH 指定。'); process.exit(2); }

const argv = process.argv.slice(2);
const BROKEN = argv.includes('--broken');
const BASE = (argv.find((a) => !a.startsWith('--')) || 'http://127.0.0.1:8787').replace(/\/$/, '');

/* 顶栏里**可切换**的元素 */
const TOGGLEABLE = ['sp-search', 'sp-scanbtn', 'tb-agents', 'tb-collect', 'tb-handoff', 'tb-clean', 'sd-bar'];

/* 每一页应有的可见集合 —— 照 上一版 原型 proto/*.html 的 .topact，**与 panel.py 的 TBAR 独立**。
   两边分别写死是有意的：如果只比对 TBAR 自己，表写错了闸门也照样绿（恒真断言）。 */
const EXPECT = {
  session: { search: true,  scan: true,  nodes: [],           detail: false },
  skill:   { search: true,  scan: true,  nodes: [],           detail: false },
  collect: { search: true,  scan: false, nodes: [],           detail: false },
  handoff: { search: true,  scan: false, nodes: [],           detail: false },
  clean:   { search: true,  scan: false, nodes: ['tb-clean'], detail: false },
  agents:  { search: true,  scan: false, nodes: ['tb-agents'],detail: false },
  sdetail: { search: false, scan: false, nodes: [],           detail: true  },
};

const results = [];
const rec = (name, ok, info) => {
  results.push({ name, ok });
  console.log('  ' + (ok ? 'PASS' : 'FAIL') + '  ' + name + (info ? '  ' + info : ''));
};

const vis = (el) => {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0 &&
         getComputedStyle(el).display !== 'none' && getComputedStyle(el).visibility !== 'hidden';
};

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 1000 });
  const errs = [];
  page.on('pageerror', (e) => errs.push(String(e.message || e)));
  await page.goto(BASE + '/', { waitUntil: 'networkidle2' });
  await page.waitForFunction(() => typeof window.show === 'function', { timeout: 15000 });
  await new Promise((r) => setTimeout(r, 1200));

  /* 取当前可见的顶栏元素集合（只看 TOGGLEABLE 那几项） */
  const snap = () => page.evaluate((ids) => {
    const v = (el) => {
      if (!el) return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 &&
             getComputedStyle(el).display !== 'none' && getComputedStyle(el).visibility !== 'hidden';
    };
    const out = { visible: [], buttons: {} };
    ids.forEach((id) => {
      const el = document.getElementById(id);
      if (v(el)) {
        out.visible.push(id);
        out.buttons[id] = [...el.querySelectorAll('button')].filter(v).map((b) => b.textContent.trim());
      }
    });
    return out;
  }, TOGGLEABLE);

  const expectVisible = (spec) => {
    const a = [];
    if (spec.search) a.push('sp-search');
    if (spec.scan) a.push('sp-scanbtn');
    spec.nodes.forEach((n) => a.push(n));
    if (spec.detail) a.push('sd-bar');
    return a.sort();
  };

  /* ── ① 逐页：可见集合必须全等 ── */
  const pages = Object.keys(EXPECT);
  for (const v of pages) {
    if (v === 'sdetail') await page.evaluate(() => {
      /* ⚠️ 全局名是 SESS_ALL（不是 SESS）—— 我第一版写错过，
         结果 openSessionDetail 根本没被调用，于是闸门报"详情页顶栏不对"，
         实际是闸门自己没进去那页。写闸门时先确认全局名。 */
      const s = window.SESS_ALL && SESS_ALL[0];
      if (s) return openSessionDetail(s.id);
      return null;
    });
    else await page.evaluate((x) => show(x), v);
    await new Promise((r) => setTimeout(r, v === 'sdetail' ? 2000 : 1500));
    if (BROKEN && v === 'agents') {   // 反向测试：故意把别的容器也打开
      await page.evaluate(() => { const e = document.getElementById('tb-clean'); if (e) e.hidden = false; });
      await new Promise((r) => setTimeout(r, 200));
    }
    const got = (await snap()).visible.sort();
    const want = expectVisible(EXPECT[v]);
    rec(`[${v}] 顶栏可见集合全等  得到 ${JSON.stringify(got)}`,
      JSON.stringify(got) === JSON.stringify(want),
      `期望 ${JSON.stringify(want)}`);
  }

  /* ── ② 乱序来回切 3 轮：每一跳都比一遍（这一条专抓"残留"） ── */
  const order = ['agents', 'clean', 'session', 'handoff', 'skill', 'collect', 'agents', 'session', 'clean', 'collect',
                 'handoff', 'agents', 'skill', 'session', 'collect', 'clean', 'agents', 'session', 'handoff', 'skill'];
  let bad = 0, firstBad = '';
  for (let i = 0; i < order.length * 3; i++) {
    const v = order[i % order.length];
    await page.evaluate((x) => show(x), v);
    await new Promise((r) => setTimeout(r, 260));
    const got = (await snap()).visible.sort();
    const want = expectVisible(EXPECT[v]);
    if (JSON.stringify(got) !== JSON.stringify(want)) {
      bad++;
      if (!firstBad) firstBad = `第 ${i + 1} 跳到 ${v}：得到 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`;
    }
  }
  rec(`乱序切页 ${order.length * 3} 跳，每一跳都不串`, bad === 0, bad ? `${bad} 跳出错；首错 ${firstBad}` : '');

  /* ── ③ 各容器的按钮内容不能串（例如 Agent 的容器里不该出现"打开备份目录"） ── */
  const cont = {};
  for (const v of ['agents', 'clean']) {
    await page.evaluate((x) => show(x), v);
    await new Promise((r) => setTimeout(r, 900));
    cont[v] = (await snap()).buttons;
  }
  let mix = [];
  if (cont.agents && cont.agents['tb-agents']) {
    const t = cont.agents['tb-agents'].join('|');
    if (!/自动检测/.test(t) || !/手动添加/.test(t)) mix.push('tb-agents 内容不对：' + t);
    if (/备份目录/.test(t)) mix.push('tb-agents 里混进了库页的按钮：' + t);
  } else mix.push('Agent 页的 tb-agents 不可见或为空');
  if (cont.clean && cont.clean['tb-clean']) {
    const t = cont.clean['tb-clean'].join('|');
    if (!/打开备份目录/.test(t)) mix.push('tb-clean 内容不对：' + t);
    if (/自动检测|手动添加/.test(t)) mix.push('tb-clean 里混进了 Agent 页的按钮：' + t);
  } else mix.push('库页的 tb-clean 不可见或为空');
  rec('容器里的按钮内容不互相混入', mix.length === 0, mix.join('；'));

  rec('切页过程无 JS 报错', errs.length === 0, errs.slice(0, 2).join(' | '));

  await browser.close();
  const fails = results.filter((r) => !r.ok);
  console.log('\n  verify_topbar: ' + (results.length - fails.length) + '/' + results.length + ' PASS'
    + (fails.length ? '  （' + fails.map((f) => f.name).join('; ') + '）' : '')); 
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error('ERR', e && e.message); process.exit(1); });
