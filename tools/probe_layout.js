/* 布局 / 边框诊断：量各页两栏宽度与关键元素边框，定位"排列不准、边框没处理好"。
   用法：node tools/probe_layout.js [base] */
const fs = require('fs');
let puppeteer;
try { puppeteer = require('puppeteer-core'); }
catch (e) { console.error('缺少 puppeteer-core，请设置 NODE_PATH。'); process.exit(2); }
const CHROME = [process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe'].filter(Boolean).find((p) => fs.existsSync(p));

const JOBS = [
  ['session', ['.listhead', '.fgroup', '.split', '.split-main', '.split-side']],
  ['mem', ['.listhead', '.split', '.split-main', '.split-side', '.shead']],
  ['skill', ['.split', '.split-main', '.split-side', '.shead', '#sk-list .mem', '#sk-list .mem .content']],
  ['collect', ['.listhead', '.panel']],
  ['clean', ['.listhead', '.panel']],
];

(async () => {
  const BASE = (process.argv[2] || 'http://127.0.0.1:8787').replace(/\/$/, '');
  const browser = await puppeteer.launch({ headless: true, executablePath: CHROME, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 1100 });
  await page.goto(BASE + '/', { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 1200));

  for (const [view, sels] of JOBS) {
    await page.evaluate((n) => window.show(n), view);
    await new Promise((r) => setTimeout(r, 2600));
    console.log('==== ' + view + ' ====');
    for (const sel of sels) {
      const r = await page.evaluate((v, s) => {
        const el = document.querySelector('#v-' + v + ' ' + s);
        if (!el) return null;
        const b = el.getBoundingClientRect(), cs = getComputedStyle(el);
        return { w: Math.round(b.width), h: Math.round(b.height), x: Math.round(b.x),
          bt: cs.borderTopWidth, bb: cs.borderBottomWidth, bl: cs.borderLeftWidth,
          bs: cs.borderTopStyle, bc: cs.borderTopColor, bg: cs.backgroundColor };
      }, view, sel);
      if (!r) { console.log('  ' + sel + ': (无这个元素)'); continue; }
      console.log('  ' + sel.padEnd(28) +
        ` ${r.w}x${r.h} x=${r.x} border=${r.bt}/${r.bb}/${r.bl} ${r.bs} ${r.bc}`);
    }
  }
  await browser.close();
  process.exit(0);
})().catch((e) => { console.error('诊断失败:', e.message); process.exit(2); });
