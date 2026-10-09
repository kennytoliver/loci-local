#!/usr/bin/env node
/*
 * 闸门：技能页详情必须"看得到全文 + 不套内层滚动条"。
 *
 * 钉的是 2026-09-21 用户报的**严重 bug**：
 *   "skill 这边的话，点击技能之后，它的正文出现了 bug，非常非常严重的 bug，
 *    条件没显示完全"
 *
 * 根因（实测出来的，不是猜）：
 *   详情区是手搓 div + `<div class="handoff-out" style="max-height:46vh;overflow:auto">`。
 *   fbs-bookwriter 的 SKILL.md 有 8010px 高、要滚 19.3 屏，全被压进 414px 的小窗；
 *   外面 `.content` 还有一层滚动条 → 两层滚动，正文永远只能看到一小条。
 *
 * 修法：照会话页骨架来 —— `.dhead` + `.dmain > .handoff-out`，**不设内层高度限制**。
 *
 * 反向测试（必须做，否则又是一盏不亮的灯）：
 *   把下面 MAX_MAXHEIGHT 改成 'none' 之外的任何值，或断言 .split-side 必须 sticky 包裹，
 *   都应该红。做法：跑 'node tools/verify_skill_detail.js --broken'，
 *   它会在页面里**注入旧样式**（max-height:46vh + overflow:auto）再断言，必须红。
 *
 * 用法：node tools/verify_skill_detail.js [base] [--broken]
 */
const fs = require('fs');

let puppeteer;
try { puppeteer = require('puppeteer-core'); }
catch (e) { console.error('[闸门] 缺少 puppeteer-core'); process.exit(2); }

const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  process.env.LOCALAPPDATA + '/Google/Chrome/Application/chrome.exe',
].find((p) => p && fs.existsSync(p));
if (!CHROME) { console.error('[闸门] 没找到 Chrome（可设 CHROME_PATH）'); process.exit(2); }

const BASE = process.argv[2] || 'http://127.0.0.1:8787';
const BROKEN = process.argv.includes('--broken');

let pass = 0, fail = 0;
const ok = (name, cond, extra) => {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (extra ? ' — ' + extra : '')); }
};

(async () => {
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

  // 挑正文最大的那个 skill —— 短文档盖不住这个 bug
  await page.evaluate(() => {
    let best = 0;
    for (let i = 0; i < SKILLS.length; i++) {
      if ((SKILLS[i].size || 0) > (SKILLS[best].size || 0)) best = i;
    }
    window.__pickIdx = best;
  });
  await page.evaluate(() => pickSkill(window.__pickIdx));
  await page.waitForFunction(
    () => { const e = document.querySelector('#sk-text'); return e && e.textContent.length > 500; },
    { timeout: 25000 });
  await new Promise((r) => setTimeout(r, 500));

  if (BROKEN) {
    // 反向测试：注入旧样式，闸门必须变红
    await page.evaluate(() => {
      const t = document.getElementById('sk-text');
      t.style.maxHeight = '46vh';
      t.style.overflow = 'auto';
    });
    await new Promise((r) => setTimeout(r, 300));
  }

  const m = await page.evaluate(() => {
    const side = document.getElementById('sk-detail');
    const text = document.getElementById('sk-text');
    const content = document.querySelector('.content');
    const ct = getComputedStyle(text);
    const textLen = text.textContent.length;

    // 滚到底，看正文末尾能不能进可视区
    content.scrollTop = content.scrollHeight;
    const sawTail = text.getBoundingClientRect().bottom <=
                    content.getBoundingClientRect().bottom + 2;
    content.scrollTop = 0;

    return {
      // 结构：必须是会话页那套
      hasDhead: !!side.querySelector('.dhead'),
      hasDmain: !!side.querySelector('.dmain'),
      // 正文：不许有内层高度限制 / 内层滚动条
      maxHeight: ct.maxHeight,
      overflowY: ct.overflowY,
      innerScrollable: text.scrollHeight > text.clientHeight + 2,
      innerRatio: +(text.scrollHeight / Math.max(1, text.clientHeight)).toFixed(1),
      // 文字确实在 DOM 里（不是被前端截了）
      textLen,
      // 滚到底能看到末行
      sawTail,
      // 有没有两层滚动
      contentScrollable: content.scrollHeight > content.clientHeight + 2,
    };
  });

  console.log('  实测：maxHeight=' + m.maxHeight + ' overflowY=' + m.overflowY +
              ' 内层可滚=' + m.innerScrollable + '（需滚 ' + m.innerRatio + ' 屏）' +
              ' 文字 ' + m.textLen + ' 字');

  ok('详情区用了 .dhead（会话页骨架）', m.hasDhead);
  ok('详情区用了 .dmain（会话页骨架）', m.hasDmain);
  ok('正文没有内层高度限制', m.maxHeight === 'none', 'maxHeight=' + m.maxHeight);
  ok('正文没有内层滚动条', m.innerScrollable === false,
     '要滚 ' + m.innerRatio + ' 屏');
  ok('正文没有内层 overflow:auto', m.overflowY !== 'auto' && m.overflowY !== 'scroll',
     'overflowY=' + m.overflowY);
  ok('正文完整进了 DOM（>5000 字）', m.textLen > 5000, '只有 ' + m.textLen + ' 字');
  ok('滚到底能看到正文末行', m.sawTail);

  ok('页面无 JS 报错', errs.length === 0, errs.join(' | '));

  await browser.close();

  console.log('');
  console.log('  通过 ' + pass + ' 项，失败 ' + fail + ' 项' + (BROKEN ? '（反向测试）' : ''));
  if (BROKEN && fail === 0) {
    console.log('  ✗ 反向测试失败：注入旧样式后闸门竟然还是绿的 —— 这盏灯是坏的');
    process.exit(1);
  }
  if (!BROKEN && fail > 0) process.exit(1);
  if (BROKEN) console.log('  ✓ 反向测试通过：旧样式确实被判红');
  process.exit(0);
})().catch((e) => { console.error('[闸门] 异常：' + e.message); process.exit(1); });
