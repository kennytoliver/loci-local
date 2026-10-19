#!/usr/bin/env node
/*
 * 交互冒烟测试（只读，不改数据库）—— 2026-10-15 重写为测**采集页**。
 *
 * ⚠️⚠️ 为什么重写
 *   旧版标题写的是「记忆页交互冒烟测试」，主体是 `#list .mem` + `#memcard-*`
 *   + `#detail .dbody` + `#health .hcard` + `#gh-recent/#g-recent` 分组折叠 ——
 *   这些 上一版之后全没了（记忆页 / 质检页已从导航移除，只留在隐藏的 #v-mem 里）。
 *   结果它 35 项里有 34 项"通过"，但那是在**测已经删掉的页面** = 假绿。
 *   用户 2026-10-15 拍板：第一版时代的配色与设计全部作废，**以现在这版原型为准**。
 *
 *   为什么挑采集页当新主体：它是活页面里交互最丰富的 ——
 *   列表行（.cr）、选中态（.on）、hover、chip 筛选、勾选框、横向溢出都在，
 *   原来那些「三态底色 / 左侧强调条 / 不溢出」的判据原样适用，不用改语义。
 *
 * 守的是什么（这几条都是历史上真被用户报过的）：
 *   ① 页面无 JS 报错            （改版最容易踩的）
 *   ② 列表行渲染 + 来源标记     （内容真的出来了）
 *   ③ 点击 → 选中态已标记
 *   ④ 默认 / hover / 选中 三态底色互不相同   （分不清 = 看不见自己选了没）
 *   ⑤ 选中行有左侧强调条
 *   ⑥ 列表项不横向溢出          （长标题把列撑爆）
 *   ⑦ 亮色选中底是靛蓝一档
 *   ⑧ 六个页面都有标题区 + 22px 字号 + 不溢出
 *
 * ⚠️ 记忆页 / 质检页 / 记忆包页**不在范围内**（已删）；会话页点行是"跳详情"、
 *    Agent 页点行是"接入/移除"，都没有选中态，也不在范围内。
 *    「选中反馈」那部分由 verify_sel_feedback.js 在四个有选中语义的列表上盯着。
 *
 * 依赖：puppeteer-core（复用系统 Chrome）。用法：
 *   node tools/smoke_panel.js [http://127.0.0.1:8787]
 * 注意：不要在这里点「入库」「销毁」—— 那会写真实数据库。
 */
const fs = require('fs');

let puppeteer;
try {
  puppeteer = require('puppeteer-core');
} catch (e) {
  console.error('[冒烟] 缺少 puppeteer-core，请先 npm i puppeteer-core');
  process.exit(2);
}
const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) {
  console.error('[冒烟] 没找到 Chrome，请用 CHROME_PATH 指定。');
  process.exit(2);
}

const BASE = (process.argv[2] || 'http://127.0.0.1:8787').replace(/\/$/, '');
const results = [];
const rec = (name, pass, info = '') =>
  results.push(`${pass ? 'PASS' : 'FAIL'}  ${name}${info ? '  ' + info : ''}`);

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });

  /* ⚠️ 2026-10-15：只跑 light。面板自 2026-09-24 起无条件亮色
     （?theme=dark 会被 applyTheme 拉回 light），dark 那一轮读到的是同一套亮色令牌。 */
  const selBgByTheme = {};
  for (const theme of ['light']) {
    const page = await browser.newPage();
    const errs = [];
    page.on('pageerror', (e) => errs.push(String(e.message || e)));
    page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
    await page.setViewport({ width: 1560, height: 900, deviceScaleFactor: 1 });
    await page.goto(`${BASE}/?theme=${theme}`, { waitUntil: 'networkidle0' });
    await page.evaluate(() => window.show('collect'));
    /* ⚠️ 等**可选的行**：已入库的行带 .lock，ckToggle() 对它们直接 return（点了没反应）。
       实测 50 行里 29 行是 lock，所以必须 :not(.lock)，否则整段测试全扑空。 */
    await page.waitForFunction(
      () => document.querySelectorAll('#scan-list .cr:not(.lock)').length > 0,
      { timeout: 20000 });
    await new Promise((r) => setTimeout(r, 400));

    rec(`[${theme}] 无页面 JS 报错`, errs.length === 0, errs.slice(0, 2).join(' | '));

    /* ② 行渲染 + 来源标记（.src）：内容真的出来了，且来源有文字 */
    const rows = await page.$$eval('#scan-list .cr', (rs) => rs.map((r) => ({
      lock: r.classList.contains('lock'),
      src: (r.querySelector('.src') || {}).textContent || '',
    })));
    const live = rows.filter((r) => !r.lock);
    rec(`[${theme}] 列表行已渲染（含未入库的）`,
      rows.length > 0 && live.length > 0,
      `共 ${rows.length} 行 / 可选 ${live.length} 行`);
    const noSrc = rows.filter((r) => !r.src.trim()).length;
    rec(`[${theme}] 每行都有来源标记`, noSrc === 0, `缺来源 ${noSrc} 行`);

    /* ③ 真点第一行可选行 → 选中态
       ⚠️ 必须先滚进视口：未入库的行排在已入库（.lock）后面，第一个可选的
          很可能在首屏之外 —— 不滚的话 boundingBox 的 y 落在视口外，
          elementFromPoint 直接返回 null（踩过：报「点击点没落在行上（命中 null）」）。 */
    await page.$eval('#scan-list .cr:not(.lock)', (e) => e.scrollIntoView({ block: 'center' }));
    await new Promise((r) => setTimeout(r, 300));
    const box = await page.$eval('#scan-list .cr:not(.lock)', (e) => {
      const r = e.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height };
    });
    const cx = box.x + Math.min(160, box.w / 2);
    const cy = box.y + box.h / 2;
    const hit = await page.evaluate((x, y) => {
      const t = document.elementFromPoint(x, y);
      if (!t) return 'null';
      if (t.closest('button,a')) return 'btn';
      return t.closest('.cr') ? 'row' : t.tagName;
    }, cx, cy);
    if (hit !== 'row') {
      rec(`[${theme}] 点击点落在行上`, false, '命中 ' + hit);
    } else {
      await page.mouse.click(cx, cy);
      await new Promise((r) => setTimeout(r, 400));
      const st = await page.evaluate(() => {
        const r = document.querySelector('#scan-list .cr:not(.lock)');
        const cs = getComputedStyle(r);
        const cb = r.querySelector('.cbx');
        const ccs = cb ? getComputedStyle(cb) : null;
        return { on: r.classList.contains('on'), bg: cs.backgroundColor, sh: cs.boxShadow,
                 cbxFilled: !!(ccs && ccs.backgroundColor && ccs.backgroundColor !== 'rgba(0, 0, 0, 0)'),
                 cbxBg: ccs ? ccs.backgroundColor : '无勾选框' };
      });
      rec(`[${theme}] 点击 → 选中态已标记（.on）`, st.on === true);
      rec(`[${theme}] 选中底色不是透明`, !!st.bg && st.bg !== 'rgba(0, 0, 0, 0)', st.bg);
      /* ⚠️ 2026-10-15 改：原来断言「选中行有左侧强调条（inset box-shadow）」——
         那是**旧记忆页**的做法。上一版 的选中反馈换成了「行底染色 + 勾选框被填充」
         （panel.py: `#v-collect .cr.on .cbx{background:var(--lz-indigo)}`），
         实测 inset 是 none。所以改成断言当前设计真正的可见反馈：勾选框填充。
         （"选中态到底看不看得出来"这件事，下面第七项 + verify_sel_feedback 也在守。） */
      rec(`[${theme}] 选中行的勾选框被填充（看得见选了没）`, st.cbxFilled === true, st.cbxBg);
      selBgByTheme[theme] = st.bg;
    }

    /* ④ 三态底色互不相同：默认 / hover / 选中
       hover 走 CDP 真鼠标 —— addStyleTag 伪造 class 量不出 :hover */
    const states = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('#scan-list .cr:not(.lock)'));
      const sel = document.querySelector('#scan-list .cr.on') || rows[0];
      const other = rows.find((e) => e !== sel) || rows[0];
      return { restBg: getComputedStyle(other).backgroundColor, onBg: getComputedStyle(sel).backgroundColor };
    });
    if (states.restBg && states.onBg) {
      const oBox = await page.$eval('#scan-list .cr:not(.lock):not(.on)', (e) => {
        const r = e.getBoundingClientRect();
        return { x: r.x, y: r.y, w: r.width, h: r.height };
      }).catch(() => null);
      if (oBox) {
        await page.mouse.move(oBox.x + Math.min(160, oBox.w / 2), oBox.y + oBox.h / 2);
        await new Promise((r) => setTimeout(r, 300));
        const hoverBg = await page.evaluate(() => {
          const h = document.querySelector('#scan-list .cr:hover');
          return h ? getComputedStyle(h).backgroundColor : null;
        });
        rec(`[${theme}] 默认 / hover / 选中 三态底色互不相同`,
          new Set([states.restBg, hoverBg, states.onBg]).size === 3,
          `${states.restBg} ｜ ${hoverBg} ｜ ${states.onBg}`);
      }
    }

    /* ⑥ 列表项不横向溢出（长标题把列撑爆） */
    const over = await page.evaluate(() => {
      const lw = document.querySelector('#scan-list').getBoundingClientRect().width;
      const bad = Array.from(document.querySelectorAll('#scan-list .cr'))
        .filter((e) => e.getBoundingClientRect().width > lw + 1).length;
      return { bad, lw: Math.round(lw) };
    });
    rec(`[${theme}] 列表项未横向溢出`, over.bad === 0, `列宽=${over.lw}`);

    await page.close();
  }

  /* ⑦ 亮色选中底必须是靛蓝那一档（var(--lz-indigo-bg) 的解析值）。
     ⚠️ 原来这里断言「选中底色明暗不同」—— 面板已无条件亮色，
        那一轮量到的"暗色"其实是亮色令牌，同一个值比大小必然 FAIL。 */
  rec('亮色选中底是靛蓝一档（选中态看得出来）',
    selBgByTheme.light === 'rgb(238, 240, 253)',
    `亮=${selBgByTheme.light}`);

  /* ⑧ 六个页面都有标题区 + 22px 字号 + 不横向溢出
     ⚠️ 2026-10-15 重列：原清单是八页时代（mem / audit / pack 三个页面 上一版 已删），
        选择器也从 `.pagehead .ptitle` + 20px 换成 上一版 统一的 `.phead .ttl h2` + 22px。
     ⚠️ 必须用 show(k) 切页 —— 面板靠 history.replaceState 改 hash，没有 hashchange
        监听，改 location.hash 切不动（会"假通过"：隐藏元素里也有标题）。 */
  const badHeads = await (async () => {
    const p = await browser.newPage();
    await p.goto(`${BASE}/`, { waitUntil: 'networkidle0' });
    await p.waitForFunction('typeof window.show === "function"', { timeout: 15000 });
    const want = { session: '会话', collect: '采集', handoff: '交接卡', clean: '库',
      agents: 'Agent', skill: '技能' };
    const bad = await p.evaluate(async (w) => {
      const out = [];
      for (const k of Object.keys(w)) {
        window.show(k);
        await new Promise((r) => setTimeout(r, 300));
        const sec = document.getElementById('v-' + k);
        const pt = sec.querySelector('.phead .ttl h2');
        const size = pt ? getComputedStyle(pt).fontSize : null;
        const n = [...sec.querySelectorAll('*')]
          .filter((e) => e.getBoundingClientRect().right > document.documentElement.clientWidth + 1).length;
        if (sec.hidden) out.push(k + '(没切过去)');
        else if (!pt || pt.textContent.trim() !== w[k]) out.push(k + '(标题=' + (pt ? pt.textContent.trim() : '无') + ')');
        else if (size !== '22px') out.push(k + '(字号=' + size + ')');
        else if (n !== 0) out.push(k + '(溢出 ' + n + ')');
      }
      return out;
    }, want);
    await p.close();
    return bad;
  })();
  rec('六个页面都有 22px 标题区且不溢出', badHeads.length === 0,
    badHeads.length ? badHeads.join(' / ') : '6/6 通过');

  await browser.close();
  console.log(results.join('\n'));
  const fails = results.filter((r) => r.startsWith('FAIL'));
  console.log(`\n合计 ${results.length} 项，失败 ${fails.length}`);
  process.exit(fails.length ? 1 : 0);
})();
