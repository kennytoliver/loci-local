// #s-list 被四种内容复用 —— 逐个验证行结构没被压扁（用户最初报的那个 bug 的根）
// ① 会话列表（要紧凑行 .lrow）② 解析预览 ③ 抽取候选 ④ 原话检索（后三个要能读正文）
const puppeteer = require('puppeteer-core');
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rec = (n, ok, i) => console.log((ok ? 'PASS  ' : 'FAIL  ') + n + (i ? '  ' + i : ''));

(async () => {
  const b = await puppeteer.launch({ executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'] });
  for (const theme of ['light', 'dark']) {
    const p = await b.newPage();
    await p.setViewport({ width: 1560, height: 900, deviceScaleFactor: 1 });
    await p.goto(`http://127.0.0.1:8787/?theme=${theme}#session`, { waitUntil: 'networkidle0' });
    await p.waitForFunction(() => document.querySelectorAll('#s-list .mem').length > 0, { timeout: 8000 });
    await sleep(400);

    // 量一个行：宽度、标题文本宽度、有没有一字一行（高度异常大 + 宽度极小）
    const probe = () => p.evaluate(() => {
      const rows = [...document.querySelectorAll('#s-list .mem')];
      const one = rows[0];
      if (!one) return { n: 0 };
      const t = one.querySelector('.mtitle') || one.querySelector('.content');
      const r = one.getBoundingClientRect();
      const tr = t ? t.getBoundingClientRect() : { width: 0, height: 0 };
      return { n: rows.length, w: +r.width.toFixed(1), h: +r.height.toFixed(1),
        textW: +tr.width.toFixed(1), textH: +tr.height.toFixed(1),
        q: getComputedStyle(one).display, cls: one.className };
    });

    const sess = await probe();
    rec(`[${theme}] ① 会话列表 = 紧凑行（高≈61、标题铺满）`,
      sess.cls.includes('lrow') && Math.abs(sess.h - 61.19) < 2 && sess.textW > 200,
      `行高=${sess.h} 标题宽=${sess.textW} display=${sess.q}`);

    // ② 解析预览
    const parse = await p.evaluate(async () => {
      const ta = document.getElementById('s-text');
      ta.value = '我: 这批沃柑的保果方案要不要调整？\nAI: 建议先看落果率，超过 15% 再上赤霉素。\n我: 那成本大概多少？';
      await window.sessionParse();
      await new Promise((r) => setTimeout(r, 400));
      return true;
    });
    await sleep(300);
    const pv = await probe();
    rec(`[${theme}] ② 解析预览 = 可读卡片（正文有宽度、不是竖排）`,
      !pv.cls.includes('lrow') && pv.textW > 300 && pv.textH < 200,
      `行高=${pv.h} 正文宽=${pv.textW} 正文高=${pv.textH} class=${pv.cls}`);

    // ③ 抽取候选
    const cand = await p.evaluate(async () => {
      const list = await (await fetch('/api/session/list?limit=50')).json();
      for (const s of list) {
        const c = await (await fetch('/api/extract?sid=' + s.id)).json();
        if (c.length) { await window.extractSession(s.id); await new Promise((r) => setTimeout(r, 900)); return c.length; }
      }
      return 0;
    });
    if (cand) {
      const cv = await probe();
      rec(`[${theme}] ③ 抽取候选 = 可读卡片（带勾选框）`,
        cv.textW > 300 && cv.textH < 200 && await p.evaluate(() => !!document.querySelector('#s-list .ck')),
        `候选=${cand} 行高=${cv.h} 正文宽=${cv.textW} class=${cv.cls}`);
    } else console.log(`  [${theme}] ③ 无候选可测，跳过`);

    // ④ 原话检索
    const found = await p.evaluate(async () => {
      document.getElementById('s-q').value = '记忆';
      await window.sessionSearch();
      await new Promise((r) => setTimeout(r, 800));
      return document.querySelectorAll('#s-list .mem').length;
    });
    if (found) {
      const sv = await probe();
      rec(`[${theme}] ④ 原话检索 = 可读卡片（正文有宽度）+ 可点击跳转`,
        sv.textW > 300 && sv.textH < 200 &&
        await p.evaluate(() => /gotoSession/.test(document.querySelector('#s-list .mem').getAttribute('onclick') || '')),
        `命中=${found} 行高=${sv.h} 正文宽=${sv.textW} class=${sv.cls}`);
    } else console.log(`  [${theme}] ④ 检索无命中，跳过`);

    // 清干净
    await p.evaluate(() => { document.getElementById('s-q').value = ''; document.getElementById('s-text').value = ''; window.loadSessions(); });
    console.log('');
    await p.close();
  }
  await b.close();
})().catch((e) => { console.error('ERR', e.message); process.exit(1); });
