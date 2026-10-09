#!/usr/bin/env node
/*
 * 生成「改前 / 改后」同画面对比图。
 *
 * 为什么不用旧截图：docs/ 是 gitignore 的，旧图已被覆盖，拿不到。
 * 更干净的做法是**在同一份 DOM 上只反转令牌**再截一次 —— 内容和布局完全一致，
 * 唯一的差异就是我们要展示的那件事（页面底 vs 卡片底的面差）。
 *
 * 用法：node tools/compare_layers.js [http://127.0.0.1:8787] [输出.png]
 */
const fs = require('fs');
const path = require('path');

let puppeteer;
try {
  puppeteer = require('puppeteer-core');
} catch (e) {
  console.error('[对比] 缺少 puppeteer-core');
  process.exit(2);
}
const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) { console.error('[对比] 没找到 Chrome'); process.exit(2); }

const BASE = (process.argv[2] || 'http://127.0.0.1:8787').replace(/\/$/, '');
const OUT = process.argv[3] || path.resolve(__dirname, '..', 'docs', '截图-改版-20260921', '对比-面差.png');

// 改动前的页面底（库原值 / 本次改动前）
const BEFORE = {
  dark: '--d0:#161616;',
  light: '--d0:#ffffff;',
};

(async () => {
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });

  const buf = { before: {}, after: {} };
  for (const theme of ['light', 'dark']) {
    const page = await browser.newPage();
    await page.setViewport({ width: 1120, height: 620 });
    await page.goto(`${BASE}/?theme=${theme}`, { waitUntil: 'networkidle0' });
    await page.evaluate(() => { window.show('session'); });
    await new Promise((r) => setTimeout(r, 600));

    buf.after[theme] = await page.screenshot({ encoding: 'base64' });

    // 只反转页面底色这一条令牌，其余全不动
    await page.addStyleTag({ content: `:root[data-theme="${theme}"], :root { ${BEFORE[theme]} }` });
    await new Promise((r) => setTimeout(r, 350));
    buf.before[theme] = await page.screenshot({ encoding: 'base64' });

    await page.close();
  }
  await browser.close();

  // 用一张 HTML 把四张图并排放出来，再让 Chrome 截这张 HTML
  const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{margin:0;background:#111;font:13px/1.5 "Segoe UI",system-ui,sans-serif;color:#eee;padding:20px}
    h1{font-size:16px;margin:0 0 4px}
    .sub{color:#9aa;font-size:12px;margin-bottom:16px}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
    .cell{background:#1c1c1c;border:1px solid #333;border-radius:10px;padding:10px}
    .cap{font-size:12px;margin-bottom:8px;display:flex;justify-content:space-between}
    .cap b{font-weight:600}
    .bad{color:#ff8a8a}.good{color:#8ce99a}
    img{width:100%;display:block;border-radius:6px}
    .note{margin-top:16px;color:#9aa;font-size:12px;line-height:1.8}
    code{background:#262626;padding:1px 5px;border-radius:4px}
  </style></head><body>
  <h1>卡片边框 / 背景分层 —— 改前 vs 改后</h1>
  <div class="sub">同一份页面、同一时刻，只反转「页面底色」这一条令牌后重新截图。唯一变量就是面差。</div>
  <div class="grid">
    <div class="cell"><div class="cap"><b>改前 · 亮色</b><span class="bad">页面底 #ffffff，卡片 #ffffff → 面差 1.000:1</span></div><img src="data:image/png;base64,${buf.before.light}"></div>
    <div class="cell"><div class="cap"><b>改后 · 亮色</b><span class="good">页面底 #eef0f4，卡片 #ffffff → 面差 1.141:1</span></div><img src="data:image/png;base64,${buf.after.light}"></div>
    <div class="cell"><div class="cap"><b>改前 · 暗色</b><span class="bad">页面底 #161616，卡片 #1d1d1c → 面差 1.073:1</span></div><img src="data:image/png;base64,${buf.before.dark}"></div>
    <div class="cell"><div class="cap"><b>改后 · 暗色</b><span class="good">页面底 #0e0e0e，卡片 #1d1d1c → 面差 1.144:1</span></div><img src="data:image/png;base64,${buf.after.dark}"></div>
  </div>
  <div class="note">
    线差（描边 vs 卡片）本来就有，两边都是 1.37 / 1.44，所以「框看得见」。<br>
    缺的是 <b>面差</b> —— 卡片底和页面底同色时，框只是浮在同一个平面上的一圈线，框里框外分不开。<br>
    现在卡片（<code>--card</code>）保持最亮/最浅，页面（<code>--bg</code>）退一层，描边（<code>--line</code>）留着勾轮廓，三层各不相同。
  </div>
  </body></html>`;

  const page = await browser0(html, OUT);
  console.log('对比图已生成：' + OUT);

  async function browser0(htmlStr, outPath) {
    const b = await puppeteer.launch({
      executablePath: CHROME, headless: 'new',
      args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
    });
    const p = await b.newPage();
    await p.setViewport({ width: 1180, height: 800, deviceScaleFactor: 1 });
    await p.setContent(htmlStr, { waitUntil: 'networkidle0' });
    const h = await p.evaluate(() => document.body.scrollHeight);
    await p.setViewport({ width: 1180, height: Math.min(h + 40, 3000) });
    await new Promise((r) => setTimeout(r, 400));
    await p.screenshot({ path: outPath, fullPage: true });
    await b.close();
    return p;
  }
})().catch((e) => { console.error('ERR', e && e.message); process.exit(1); });
