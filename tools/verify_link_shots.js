// 会话↔记忆打通后的验证截图 + 三项断言（bug2 整页滚动 / bug3 取消还原 / 默认落点）
const puppeteer = require('puppeteer-core');
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const OUT = require('path').join(__dirname, '..', 'docs', '截图-改版-20260921');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rec = (name, ok, info) => console.log((ok ? 'PASS  ' : 'FAIL  ') + name + (info ? '  ' + info : ''));

(async () => {
  const b = await puppeteer.launch({ executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'] });
  for (const theme of ['light', 'dark']) {
    const p = await b.newPage();
    await p.setViewport({ width: 1560, height: 900, deviceScaleFactor: 1 });
    // 不带 #锚点 —— 验证默认落点
    await p.goto(`http://127.0.0.1:8787/?theme=${theme}`, { waitUntil: 'networkidle0' });
    await p.waitForFunction(() => document.querySelectorAll('#s-list .mem').length > 0, { timeout: 8000 });
    await sleep(500);

    // 断言：默认落点是「会话」不是「记忆」
    const land = await p.evaluate(() => ({
      sessionShown: !document.getElementById('v-session').hidden,
      memHidden: document.getElementById('v-mem').hidden,
      navOn: (document.querySelector('.nav.on') || {}).dataset?.v,
    }));
    rec(`[${theme}] 默认落点=会话`, land.sessionShown && land.memHidden && land.navOn === 'session', JSON.stringify(land));
    await p.screenshot({ path: `${OUT}/lk-1-land-${theme}.png` });

    // 打开 12 号会话（有 9 条产出记忆）
    // bug2 断言要点：面板的 body 是 overflow:hidden，真正滚动的是 div.content，
    // window.scrollY 永远是 0 —— 拿它判断"整页有没有被滚"是假通过（踩过）。
    const opened = await p.evaluate(async () => {
      const c = document.querySelector('.content');
      c.scrollTop = 600;                       // 先滚下去，模拟"用户正翻在下面"
      await new Promise((r) => setTimeout(r, 250));
      const before = c.scrollTop;
      await window.openSession(12);
      await new Promise((r) => setTimeout(r, 600));
      return { scrollBefore: before, scrollAfter: c.scrollTop,
        winY: window.scrollY,
        chips: document.querySelectorAll('#s-view .tchip').length,
        bubbles: document.querySelectorAll('#s-view .bub').length,
        title: document.getElementById('s-view-t').textContent,
        hint: document.getElementById('s-view-h').textContent };
    });
    rec(`[${theme}] bug2 点开会话不滚走内容区`, opened.scrollAfter === opened.scrollBefore,
      `内容区 前=${opened.scrollBefore} 后=${opened.scrollAfter}（window.scrollY=${opened.winY}，恒为 0 只做参照）`);
    rec(`[${theme}] 时间线出来了（气泡 + 产出记忆芯片）`,
      opened.bubbles >= 3 && opened.chips >= 3,
      `气泡=${opened.bubbles} 芯片=${opened.chips} 标题=${opened.title} | ${opened.hint}`);
    await p.screenshot({ path: `${OUT}/lk-2-detail-${theme}.png` });

    // 芯片点击 → 记忆页选中
    const jumped = await p.evaluate(async () => {
      const c = document.querySelector('#s-view .tchip');
      if (!c) return { err: 'no chip' };
      c.click();
      await new Promise((r) => setTimeout(r, 800));
      return { memShown: !document.getElementById('v-mem').hidden,
        sel: (document.querySelector('#list .mem.sel') || {}).id,
        jumpBtn: !!document.querySelector('#detail .mini') &&
          [...document.querySelectorAll('#detail .dsec-t')].map((e) => e.textContent.trim()).join('/') };
    });
    rec(`[${theme}] 芯片 → 记忆页并选中`, jumped.memShown && !!jumped.sel, JSON.stringify(jumped));
    await p.screenshot({ path: `${OUT}/lk-3-memdetail-${theme}.png` });

    // 先找一个**还能抽出候选**的会话（12 号已抽过，重复的会被自动跳过）
    const target = await p.evaluate(async () => {
      const list = await (await fetch('/api/session/list?limit=50')).json();
      for (const s of list) {
        const c = await (await fetch('/api/extract?sid=' + s.id)).json();
        if (c.length) return { sid: s.id, n: c.length };
      }
      return null;
    });
    if (!target) console.log('  (没有可抽的会话，跳过 bug3 用例)');

    // bug3：抽记忆 → 取消 → 时间线要回来
    if (target) {
      const cancel = await p.evaluate(async (sid) => {
        window.show('session');
        await window.openSession(sid);
        await new Promise((r) => setTimeout(r, 400));
        await window.extractSession(sid);
        await new Promise((r) => setTimeout(r, 1200));
        const cands = document.querySelectorAll('#s-list .ck').length;
        const mid = { cands, candSid: window.CAND_SID,
          bubblesMid: document.querySelectorAll('#s-view .bub').length };
        window.cancelExtract();
        await new Promise((r) => setTimeout(r, 1200));
        return Object.assign(mid, {
          bubblesAfter: document.querySelectorAll('#s-view .bub').length,
          chipsAfter: document.querySelectorAll('#s-view .tchip').length,
          rows: document.querySelectorAll('#s-list .mem').length });
      }, target.sid);
      rec(`[${theme}] bug3 取消抽取后时间线回来了`, cancel.bubblesAfter >= 3 && cancel.candSid > 0,
        `会话#${target.sid} 候选=${cancel.cands} CAND_SID=${cancel.candSid} 取消后气泡=${cancel.bubblesAfter} 芯片=${cancel.chipsAfter} 列表=${cancel.rows}`);
      await p.screenshot({ path: `${OUT}/lk-4-cancelled-${theme}.png` });
    }

    // 抽不到候选时，时间线**不许**被顶掉（以前会被一句"没抽到"占掉，回不来）
    const none = await p.evaluate(async () => {
      window.show('session');
      await window.openSession(12);
      await new Promise((r) => setTimeout(r, 400));
      const before = document.querySelectorAll('#s-view .bub').length;
      await window.extractSession(12);
      await new Promise((r) => setTimeout(r, 1000));
      return { before, after: document.querySelectorAll('#s-view .bub').length,
        notice: !!document.querySelector('#s-view .msg') };
    });
    rec(`[${theme}] 抽不到候选时时间线不丢`, none.after >= 3,
      `抽前=${none.before} 抽后=${none.after} 有提示=${none.notice}`);

    // 记忆列表：出处徽章 + 行高没被顶破
    const mem = await p.evaluate(async () => {
      window.show('mem');
      await window.loadList();
      await new Promise((r) => setTimeout(r, 600));
      const rows = [...document.querySelectorAll('#list .mem.lrow')];
      const withSrc = rows.filter((r) => r.querySelector('.bdg.src'));
      return { rows: rows.length, withSrc: withSrc.length,
        h: rows.length ? +rows[0].getBoundingClientRect().height.toFixed(2) : 0,
        overflow: rows.some((r) => r.scrollWidth > r.clientWidth + 1),
        srcText: withSrc.length ? withSrc[0].querySelector('.bdg.src').textContent : '',
        srcTip: withSrc.length ? withSrc[0].querySelector('.bdg.src').title : '' };
    });
    rec(`[${theme}] 记忆行有「有原话」徽章`, mem.withSrc > 0, `行=${mem.rows} 带出处=${mem.withSrc} 文案=${mem.srcText} 悬停=${mem.srcTip}`);
    rec(`[${theme}] 记忆行高未变、无横向溢出`, Math.abs(mem.h - 61.19) < 1.2 && !mem.overflow, `高=${mem.h} 溢出=${mem.overflow}`);
    await p.screenshot({ path: `${OUT}/lk-5-memlist-${theme}.png` });

    await p.close();
  }
  await b.close();
})().catch((e) => { console.error('ERR', e.message); process.exit(1); });
