#!/usr/bin/env node
/*
 * 八个页面 × 明暗双主题 = 16 张基线截图（规范第八节第 3 步）。
 *
 * ⚠ 切页陷阱：面板没有 hashchange 监听，改 location.hash 切不动页面，
 *   会得到 8 张一模一样的图。必须在上文里直接调 window.show(id)。
 * （规范 397 行专门记了这个坑。）
 *
 * 用法：node tools/snapshot_pages.js [http://127.0.0.1:8787] [输出目录]
 *   默认输出到 docs/截图-改版-20260921/
 */
const fs = require('fs');
const path = require('path');

let puppeteer;
try {
  puppeteer = require('puppeteer-core');
} catch (e) {
  console.error('[截图] 缺少 puppeteer-core');
  process.exit(2);
}
const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) { console.error('[截图] 没找到 Chrome'); process.exit(2); }

const BASE = (process.argv[2] || 'http://127.0.0.1:8787').replace(/\/$/, '');
const ROOT = path.resolve(__dirname, '..');
const OUT = process.argv[3] || path.join(ROOT, 'docs', '截图-改版-20260921');

const PAGES = [
  ['session', '会话'], ['mem', '记忆'], ['audit', '质检'], ['clean', '清理'],
  ['collect', '采集'], ['agents', 'Agent'], ['pack', '记忆包'], ['handoff', '交接卡'],
];

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });

  const hashes = new Set();
  for (const theme of ['dark', 'light']) {
    const page = await browser.newPage();
    await page.setViewport({ width: 1560, height: 900 });
    await page.goto(`${BASE}/?theme=${theme}`, { waitUntil: 'networkidle0' });
    await new Promise((r) => setTimeout(r, 400));

    for (const [id] of PAGES) {
      // 直接调 show()：hash 方式切不动（见文件头注释）
      await page.evaluate((v) => { window.show(v); }, id);
      await new Promise((r) => setTimeout(r, 550));
      const file = path.join(OUT, `${id}-${theme}.png`);
      await page.screenshot({ path: file });

      // 自检：每张图必须字节不同，否则就是「切页没生效」的假通过
      const buf = fs.readFileSync(file);
      const crypto = require('crypto');
      const h = crypto.createHash('md5').update(buf).digest('hex').slice(0, 8);
      if (hashes.has(h)) console.log(`  ⚠ ${id}-${theme} 与已有图片内容相同（切页可能没生效）`);
      hashes.add(h);
      const active = await page.evaluate(() => {
        const on = document.querySelector('.nav.on');
        return on ? on.dataset.v : '?';
      });
      console.log(`  ${(id + '-' + theme).padEnd(18)} ${(buf.length / 1024).toFixed(0).padStart(4)}KB  md5=${h}  当前页=${active}`);
    }
    await page.close();
  }

  await browser.close();
  console.log(`\n共 ${PAGES.length * 2} 张，输出到 ${OUT}`);
  console.log(`不同内容 ${hashes.size} 种${hashes.size === PAGES.length * 2 ? '（全部唯一 ✓）' : '（有重复，检查切页）'}`);
})().catch((e) => { console.error('ERR', e && e.message); process.exit(1); });
