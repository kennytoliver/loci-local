/* 截取用户报的几处问题现场，便于定位。（纯诊断用）
   用法：node tools/shot_bugshots.js [base] */
const fs = require('fs');
const path = require('path');
let puppeteer;
try { puppeteer = require('puppeteer-core'); }
catch (e) { console.error('缺少 puppeteer-core，请设置 NODE_PATH。'); process.exit(2); }
const CHROME = [process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe'].filter(Boolean).find((p) => fs.existsSync(p));

const OUT = path.join(require('os').tmpdir(), 'hp_shots') + '/';

(async () => {
  const BASE = (process.argv[2] || 'http://127.0.0.1:8787').replace(/\/$/, '');
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({ headless: true, executablePath: CHROME, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 1100 });

  const jobs = [
    ['session', '#v-session', 2200],
    ['clean', '#v-clean', 2000],
    ['collect', '#v-collect', 2600],
    ['skill', '#v-skill', 3200],
  ];
  await page.goto(BASE + '/', { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 1500));
  for (const [name, sel, wait] of jobs) {
    await page.evaluate((n) => { window.show(n); }, name);
    await new Promise((r) => setTimeout(r, wait));
    const el = await page.$(sel);
    if (!el) { console.log(name, '找不到', sel); continue; }
    const box = await el.boundingBox();
    if (!box || !box.height) { console.log(name, '元素不可见（隐藏中）'); continue; }
    const h = Math.min(box.height, 2400);
    await page.screenshot({ path: OUT + name + '.png', clip: { x: box.x, y: box.y, width: box.width, height: h } });
    console.log(`${name}: ${Math.round(box.width)}x${Math.round(box.height)} → 截 ${Math.round(h)}`);
  }
  await browser.close();
  console.log('输出目录:', OUT);
  process.exit(0);
})().catch((e) => { console.error('截图失败:', e.message); process.exit(2); });
