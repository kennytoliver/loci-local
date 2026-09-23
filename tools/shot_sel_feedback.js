#!/usr/bin/env node
/*
 * 出「选中底」对照截图：三个场景 × 暗/亮两个主题 × 改前(无 .sel) / 改后(有 .sel)。
 *   场景 ①  会话页左栏（#s-list）      —— 2026-09-23 第一次报的
 *   场景 ②  质检页（#audit-out）       —— 2026-09-23 第二次报的
 *   场景 ③  清理页·源文件（#sf-list）  —— 同上
 *
 * 说明：改前的状态不是靠改代码复现的 —— 页面加载后 JS 一视同仁地打标，
 *   所以这里在截「改前」时把那一行的 .sel 摘掉。除了这一个类，两个状态完全同源，可对照。
 *
 * 用法：node tools/shot_sel_feedback.js http://127.0.0.1:8799 [输出目录]
 * 依赖：puppeteer-core（开发期；产品本身零依赖）
 */
let puppeteer;
try { puppeteer = require('puppeteer-core'); } catch (e) { console.error('缺 puppeteer-core'); process.exit(2); }
const fs = require('fs');
const path = require('path');
const CHROME = [process.env.CHROME_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe']
  .filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) { console.error('没找到 Chrome'); process.exit(2); }

const argv = process.argv.slice(2);
const url = argv.find((a) => !a.startsWith('--')) || 'http://127.0.0.1:8799';
const outDir = argv.filter((a) => !a.startsWith('--'))[1] || 'docs/shots';
fs.mkdirSync(outDir, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* 场景：视图 / 列表容器 / 行选择器 / 出图前缀 / 渲染准备代码 */
const SCENES = [
  { v: 'session', box: '#s-list', row: '#s-list .mem', name: 'session' },
  { v: 'audit', box: '#audit-out', row: '#audit-out .mem', name: 'audit', setup: 'runAudit()' },
  { v: 'clean', box: '#sf-list', row: '#sf-list .mem', name: 'clean', setup: 'loadSourceFiles()' },
];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 1000, deviceScaleFactor: 2 });
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction('typeof window.show === "function"');

  const made = [];
  for (const sc of SCENES) {
    await page.evaluate((v) => window.show(v), sc.v);
    if (sc.setup) await page.evaluate((code) => eval(code), sc.setup);
    try {
      await page.waitForFunction((sel) => document.querySelectorAll(sel).length > 0,
        { timeout: 12000 }, sc.row);
    } catch (e) {
      console.log('跳过 ' + sc.name + '：列表没渲染出行');
      continue;
    }
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
      await sleep(250);
      /* 点第二行：第一行容易被 tab 焦点/工具条影响，第二行更有代表性 */
      const rows = await page.$$(sc.row);
      const row = rows[1] || rows[0];
      /* 滚进视口（质检正文很长、清理页的列表在第②块）—— 以**被点那一行**为准，
         否则取景从第一行开始算，点的那行在 340px 裁切线之外，图上只露一条蓝边（踩过） */
      await row.evaluate((e) => e.scrollIntoView({ block: 'start' }));
      await sleep(500);
      const box = await row.boundingBox();
      await page.mouse.click(box.x + Math.min(140, box.width / 2), box.y + box.height / 2);
      await sleep(500);
      await page.mouse.move(3, 3);
      await sleep(300);

      const clip = {
        x: Math.max(0, box.x - 14),
        y: Math.max(0, box.y - 46),
        width: Math.min(box.width + 28, 1200),
        height: Math.min(3 * box.height + 60, 380),
      };

      /* 改后：保留 .sel */
      let f = path.join(outDir, `sel-${sc.name}-${theme}-after.png`);
      await page.screenshot({ path: f, clip });
      made.push(f);

      /* 改前：摘掉 .sel（与改后同源、只差这一个类） */
      await page.evaluate((sel) => {
        document.querySelectorAll(sel + '.sel').forEach((x) => x.classList.remove('sel'));
      }, sc.row);
      await sleep(200);
      f = path.join(outDir, `sel-${sc.name}-${theme}-before.png`);
      await page.screenshot({ path: f, clip });
      made.push(f);

      await page.mouse.move(700, 700); await sleep(150);
    }
  }
  await browser.close();
  made.forEach((f) => console.log('已出图 ' + f));
})();
