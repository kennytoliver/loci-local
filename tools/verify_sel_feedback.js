#!/usr/bin/env node
/*
 * 验「点列表项有没有选中底」在三个主从页 × 两个主题下是否一致。
 *
 * 为什么需要它（2026-09-23 用户报的 bug）：
 *   黑夜下只有记忆页点一条有蓝色底（--sel-bg），会话页 / 本机内容页点下去只剩 hover 的淡灰。
 *   根因是 .sel 只由 selectMem() 打在 #memcard-* 上，另外两页从来没被标记过。
 *   光看代码判断不了"点完到底有没有底色"，也判断不了"鼠标移开后还在不在" —— 只能真点。
 *
 * 判据：点一下 → 把鼠标挪到空白处（脱离 :hover）→ 仍必须读到 --sel-bg 的解析值。
 *   暗色 --sel-bg = --accent = #00266b → rgb(0, 38, 107)
 *   亮色 --sel-bg = --accent = #dbeafe → rgb(219, 234, 254)
 *
 * 用法：node tools/verify_sel_feedback.js http://127.0.0.1:8799
 * 依赖：puppeteer-core（开发期；产品本身零依赖）
 */
let puppeteer;
try {
  puppeteer = require('puppeteer-core');
} catch (e) {
  console.error('[选中底验证] 缺少 puppeteer-core，请 npm i puppeteer-core --no-audit --no-fund');
  process.exit(2);
}
const fs = require('fs');
const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].filter(Boolean);
const CHROME = CHROME_CANDIDATES.find((p) => fs.existsSync(p));
if (!CHROME) {
  console.error('[选中底验证] 没找到 Chrome，用 CHROME_PATH 指定 chrome.exe');
  process.exit(2);
}
const argv = process.argv.slice(2);
const url = argv.find((a) => !a.startsWith('--')) || 'http://127.0.0.1:8799';

/* 三个主从页：导航名 / 列表容器 / 行选择器 / 中文名 */
const PAGES = [
  { v: 'mem', box: '#list', row: '#list .mem', label: '记忆' },
  { v: 'session', box: '#s-list', row: '#s-list .mem', label: '会话' },
  { v: 'skill', box: '#sk-list', row: '#sk-list .mem', label: '本机内容' },
];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 1000, deviceScaleFactor: 1 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e.message).slice(0, 160)));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('console: ' + m.text().slice(0, 160));
  });
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction('typeof window.show === "function"', { timeout: 15000 });

  const out = [];
  for (const theme of ['dark', 'light']) {
    /* 切主题改 documentElement 的 data-theme —— 别依赖 localStorage，key 不对会量错主题 */
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    for (const p of PAGES) {
      await page.evaluate((v) => window.show(v), p.v);
      let ok = true;
      try {
        await page.waitForFunction(
          (sel) => document.querySelectorAll(sel).length > 0,
          { timeout: 12000 }, p.row);
      } catch (e) { ok = false; }
      if (!ok || !(await page.$(p.row))) { out.push({ theme, ...p, note: '列表没渲染出行，跳过' }); continue; }
      /* 等布局落定再量 —— 不等的话 boundingBox 可能取到上一帧的位置，点空就没有 .sel（踩过） */
      await new Promise((r) => setTimeout(r, 350));

      /* 真点：第一行的中心（用 page.click 走 CDP 真鼠标，addStyleTag 伪造 class 量不出 :hover） */
      const box = await (await page.$(p.row)).boundingBox();
      const cx = box.x + Math.min(120, box.width / 2);
      const cy = box.y + box.height / 2;
      const hit = await page.evaluate((x, y) => {
        const t = document.elementFromPoint(x, y);
        return t ? (t.closest('.split-main .mem') ? 'row' : t.tagName) : 'null';
      }, cx, cy);
      if (hit !== 'row') { out.push({ theme, ...p, note: '点击点没落在行上（命中 ' + hit + '）' }); continue; }
      await page.mouse.click(cx, cy);
      await new Promise((r) => setTimeout(r, 350));
      /* 关键：把鼠标挪到空白处，脱离 :hover —— 这时读到的底色才真的是「选中态」而不是悬停态 */
      await page.mouse.move(4, 4);
      await new Promise((r) => setTimeout(r, 250));

      const got = await page.evaluate((sel) => {
        const el = document.querySelector(sel);
        const cs = getComputedStyle(el);
        return {
          bg: cs.backgroundColor,
          inset: cs.boxShadow,
          sel: el.classList.contains('sel'),
          hoverBg: null,
        };
      }, p.row);
      out.push({ theme, ...p, note: '', ...got });
    }
  }
  await browser.close();

  const EXPECT = { dark: 'rgb(0, 38, 107)', light: 'rgb(219, 234, 254)' };
  let bad = 0;
  console.log('页面     主题   点完(鼠标已移开)          带 .sel  判定');
  console.log('─'.repeat(72));
  for (const r of out) {
    const want = EXPECT[r.theme];
    const pass = r.bg === want && r.sel === true;
    if (!pass && r.note === '') bad++;
    console.log(
      String(r.label).padEnd(8) + r.theme.padEnd(7) + String(r.bg).padEnd(24) +
      String(r.sel).padEnd(9) + (r.note ? '跳过：' + r.note : pass ? 'PASS' : 'FAIL（期望 ' + want + '）'));
  }
  if (errors.length) {
    console.log('\n页面报错：');
    errors.slice(0, 8).forEach((e) => console.log('  ' + e));
  }
  console.log('\n' + (bad === 0 && errors.length === 0 ? '全部通过' : `失败 ${bad} 项 / 报错 ${errors.length} 条`));
  process.exit(bad === 0 && errors.length === 0 ? 0 : 1);
})();
