#!/usr/bin/env node
/*
 * 量某个页面的真实布局尺寸，用来对齐设计稿比例（Trae 稿 → panel.py）。
 *
 * 为什么需要它：
 *   肉眼审 CSS 判断不了"比例对不对" —— 差 4px 看着就像，量出来才知道。
 *   上一轮记忆页就是靠它把列表项从 64.8px 收到 61.19px，与 Trae 稿逐项对齐。
 *
 * 依赖（开发期可选，产品本身仍是零依赖）：
 *   npm i puppeteer-core        # 复用系统 Chrome，不下载 Chromium
 *   本机 Chrome 路径：C:/Program Files/Google/Chrome/Application/chrome.exe
 *
 * 用法：
 *   node tools/measure.js http://127.0.0.1:8787/?theme=dark#mem
 *   node tools/measure.js "file:///C:/path/记忆.html" --row .memory-item --head .group-header
 *   node tools/measure.js <url> --row "#list .mem" --head "#list .ghead" --title .mtitle --meta .mmeta
 *
 * 提示：Windows 下若报 Cannot find module 'puppeteer-core'，设置
 *   NODE_PATH=C:/Users/<你>/.workbuddy/binaries/node/workspace/node_modules
 */
let puppeteer;
try {
  puppeteer = require('puppeteer-core');
} catch (e) {
  console.error('[测量工具] 缺少 puppeteer-core。请先执行：');
  console.error('    npm i puppeteer-core --no-audit --no-fund');
  console.error('  或在 Windows 下设置 NODE_PATH 指向已安装它的 node_modules。');
  process.exit(2);
}

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].filter(Boolean);
const fs = require('fs');
const CHROME = CHROME_CANDIDATES.find((p) => fs.existsSync(p));
if (!CHROME) {
  console.error('[测量工具] 没找到 Chrome，请用环境变量 CHROME_PATH 指定 chrome.exe 路径。');
  process.exit(2);
}

const argv = process.argv.slice(2);
const url = argv.find((a) => !a.startsWith('--'));
const flag = (name, dflt) => {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
};
if (!url) {
  console.error('用法: node tools/measure.js <url> [--row 选择器] [--head 选择器] [--title 选择器] [--meta 选择器] [--detail 选择器] [--theme dark|light]');
  process.exit(2);
}
const SEL = {
  row: flag('row', '#list .mem'),
  head: flag('head', '#list .ghead'),
  title: flag('title', null),
  meta: flag('meta', null),
  detail: flag('detail', '.split-side'),
};

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars', '--allow-file-access-from-files'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 900, deviceScaleFactor: 1 });
  await page.goto(url, { waitUntil: 'load' });
  try {
    await page.waitForFunction(
      (sel) => document.querySelectorAll(sel).length > 0, { timeout: 8000 }, SEL.row);
  } catch (e) {
    console.error(`[测量工具] 超时：找不到 ${SEL.row} 的任何元素。`);
    await browser.close();
    process.exit(1);
  }
  await new Promise((r) => setTimeout(r, 400));

  const out = await page.evaluate((SEL) => {
    const r = (el) => el.getBoundingClientRect();
    const cs = (el, p) => getComputedStyle(el)[p];
    const brief = (el) => {
      if (!el) return null;
      return {
        h: Math.round(r(el).height * 100) / 100,
        w: Math.round(r(el).width * 100) / 100,
        pad: cs(el, 'padding'),
        radius: cs(el, 'borderRadius'),
        fs: cs(el, 'fontSize'),
        border: cs(el, 'borderTopWidth'),
        bg: cs(el, 'backgroundColor'),
      };
    };
    const rows = Array.from(document.querySelectorAll(SEL.row));
    const heads = Array.from(document.querySelectorAll(SEL.head));
    const gaps = [];
    for (let i = 1; i < Math.min(rows.length, 6); i++) {
      gaps.push(Math.round((r(rows[i]).top - r(rows[i - 1]).bottom) * 100) / 100);
    }
    const headGaps = [];
    for (let i = 1; i < heads.length; i++) {
      headGaps.push(Math.round((r(heads[i]).top - r(heads[i - 1]).bottom) * 100) / 100);
    }
    return {
      rowCount: rows.length,
      row: brief(rows[0]),
      rowGaps: gaps,
      title: selInfo(rows[0], SEL.title),
      meta: selInfo(rows[0], SEL.meta),
      heads: heads.map((el) => ({ txt: (el.textContent || '').trim().slice(0, 14), ...brief(el) })),
      headGaps,
      detail: brief(document.querySelector(SEL.detail)),
    };
    function selInfo(row, sel) {
      if (!sel || !row) return null;
      const el = row.querySelector(sel) || document.querySelector(sel);
      return el ? { sel, ...brief(el) } : { sel, missing: true };
    }
  }, SEL);

  console.log(JSON.stringify(out, null, 2));
  await browser.close();
})().catch((e) => {
  console.error('ERR', e && e.message);
  process.exit(1);
});
