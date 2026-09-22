#!/usr/bin/env node
/*
 * 诊断：技能页点开一个技能后，正文为什么显示不全。
 *
 * 只量事实，不给结论。输出：
 *   · #sk-detail（.split-side）的 position/overflow/height/scrollHeight
 *   · 内层 #sk-text（.handoff-out）的 max-height/overflow/scrollHeight/clientHeight
 *   · 页面能不能滚到底（documentElement.scrollHeight vs 视口）
 *   · 正文首尾各 60 字（确认 DOM 里到底有没有全部内容）
 *   · 顺带截图
 *
 * 用法：node tools/diag_skill_detail.js [base] [outdir]
 */
const fs = require('fs');
const path = require('path');

let puppeteer;
try { puppeteer = require('puppeteer-core'); }
catch (e) { console.error('[诊断] 缺少 puppeteer-core'); process.exit(2); }

const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  process.env.LOCALAPPDATA + '/Google/Chrome/Application/chrome.exe',
].find((p) => p && fs.existsSync(p));
if (!CHROME) { console.error('[诊断] 没找到 Chrome（可设 CHROME_PATH）'); process.exit(2); }

const BASE = process.argv[2] || 'http://127.0.0.1:8787';
const OUT = process.argv[3] || path.resolve(__dirname, '..', 'docs', '截图-改版-20260921');

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });

  const errs = [];
  page.on('pageerror', (e) => errs.push(String(e)));

  await page.goto(BASE, { waitUntil: 'networkidle2' });
  await page.waitForFunction(() => typeof window.show === 'function', { timeout: 15000 });
  await page.evaluate(() => window.show('skill'));
  await page.waitForFunction(
    () => document.querySelectorAll('#sk-list .mem').length > 0, { timeout: 25000 });
  await page.click('#sk-list .mem');
  await page.waitForFunction(
    () => { const e = document.querySelector('#sk-text'); return e && e.textContent.length > 200; },
    { timeout: 25000 });
  await new Promise((r) => setTimeout(r, 600));

  console.log('技能总数:', await page.evaluate(
    () => document.querySelectorAll('#sk-list .mem').length));

  const report = await page.evaluate(async () => {
    // 选一个正文最长的，最能暴露"显示不全"
    let best = 0;
    for (let i = 0; i < SKILLS.length; i++) {
      if ((SKILLS[i].size || 0) > (SKILLS[best].size || 0)) best = i;
    }
    await pickSkill(best);
    await new Promise((r) => setTimeout(r, 800));

    const side = document.getElementById('sk-detail');
    const text = document.getElementById('sk-text');
    const content = document.querySelector('.content');
    const cs = (el) => el ? getComputedStyle(el) : null;
    const box = (el) => {
      if (!el) return null;
      const c = cs(el);
      return {
        position: c.position, overflow: c.overflowY,
        maxHeight: c.maxHeight, height: c.height,
        clientHeight: el.clientHeight, scrollHeight: el.scrollHeight,
        top: Math.round(el.getBoundingClientRect().top),
        bottom: Math.round(el.getBoundingClientRect().bottom),
      };
    };
    const t = text ? text.textContent : '';

    // 关键：.content 才是滚动容器（body 是 100vh）
    const before = {
      contentScrollH: content.scrollHeight,
      contentClientH: content.clientHeight,
      scrollTop: content.scrollTop,
      sideBottomInView: side.getBoundingClientRect().bottom,
    };

    // 滚到底，再看右栏底部能不能进可视区
    content.scrollTop = content.scrollHeight;
    await new Promise((r) => setTimeout(r, 400));
    const after = {
      scrollTop: content.scrollTop,
      sideTop: Math.round(side.getBoundingClientRect().top),
      sideBottom: Math.round(side.getBoundingClientRect().bottom),
      textBottom: Math.round(text.getBoundingClientRect().bottom),
      contentBottom: Math.round(content.getBoundingClientRect().bottom),
      // 滚到底后，右栏底部是否超出 .content 可视区
      sideOverflows: side.getBoundingClientRect().bottom > content.getBoundingClientRect().bottom + 1,
      textOverflows: text.getBoundingClientRect().bottom > content.getBoundingClientRect().bottom + 1,
    };
    content.scrollTop = 0;

    return {
      picked: SKILLS[best].name,
      pickedSize: SKILLS[best].size,
      side: box(side),
      text: box(text),
      textLen: t.length,
      head: t.slice(0, 50).replace(/\n/g, '⏎'),
      tail: t.slice(-50).replace(/\n/g, '⏎'),
      before, after,
      innerScrollable: text ? (text.scrollHeight > text.clientHeight + 2) : null,
      // 内层要滚多少屏才看得完
      innerScreens: text ? +(text.scrollHeight / Math.max(1, text.clientHeight)).toFixed(1) : null,
    };
  });

  console.log('\n===== #sk-detail（.split-side）=====');
  console.log(JSON.stringify(report.side, null, 2));
  console.log('\n===== #sk-text（.handoff-out）=====');
  console.log(JSON.stringify(report.text, null, 2));
  console.log('\n===== 正文 =====');
  console.log('  选中:', report.picked, '(', report.pickedSize, '字节 )');
  console.log('  DOM 文字长度:', report.textLen, ' 内层可滚:', report.innerScrollable,
              ' 需滚', report.innerScreens, '屏');
  console.log('  开头:', report.head);
  console.log('  结尾:', report.tail);
  console.log('\n===== .content 滚动容器 =====');
  console.log('  滚动前:', JSON.stringify(report.before));
  console.log('  滚到底:', JSON.stringify(report.after));
  console.log('  ⚠ 右栏底部超出可视区:', report.after.sideOverflows);
  console.log('  ⚠ 正文底部超出可视区:', report.after.textOverflows);

  await page.screenshot({ path: path.join(OUT, 'diag-skill-detail.png') });
  console.log('\n截图:', path.join(OUT, 'diag-skill-detail.png'));
  console.log('页面报错:', errs.length ? errs : '无');

  await browser.close();
})();
