#!/usr/bin/env node
/*
 * 出「选中底」对照截图：会话页左栏，暗/亮两个主题 × 改前(无 .sel) / 改后(有 .sel)。
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

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 1000, deviceScaleFactor: 2 });
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction('typeof window.show === "function"');
  await page.evaluate(() => window.show('session'));
  await page.waitForFunction(() => document.querySelectorAll('#s-list .mem').length > 0, { timeout: 12000 });
  await sleep(400);

  const made = [];
  for (const theme of ['dark', 'light']) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    await sleep(250);
    const box = await (await page.$('#s-list .mem')).boundingBox();
    /* 点第二行：第一行容易被 tab 焦点/工具条影响，第二行更有代表性 */
    const row = (await page.$$('#s-list .mem'))[1] || (await page.$('#s-list .mem'));
    const rb = await row.boundingBox();
    await page.mouse.click(rb.x + 140, rb.y + rb.height / 2);
    await sleep(500);
    await page.mouse.move(3, 3);
    await sleep(300);

    const clip = {
      x: Math.max(0, box.x - 14),
      y: Math.max(0, box.y - 42),
      width: box.width + 28,
      height: Math.min(4 * box.height + 56, 340),
    };

    /* 改后：保留 .sel */
    let f = path.join(outDir, `sel-session-${theme}-after.png`);
    await page.screenshot({ path: f, clip });
    made.push(f);

    /* 改前：摘掉 .sel（与改后同源、只差这一个类） */
    await page.evaluate(() => {
      document.querySelectorAll('#s-list .mem.sel').forEach((x) => x.classList.remove('sel'));
    });
    await sleep(200);
    f = path.join(outDir, `sel-session-${theme}-before.png`);
    await page.screenshot({ path: f, clip });
    made.push(f);

    /* 复位，下一主题重新点 */
    await page.mouse.move(700, 700); await sleep(150);
  }
  await browser.close();
  made.forEach((f) => console.log('已出图 ' + f));
})();
