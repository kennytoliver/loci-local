#!/usr/bin/env node
/*
 * 主题自检：三关，全过才算没跑偏。
 *
 *   1) 令牌对账 —— 面板 :root 的 29×2 个令牌，逐项跟 Google 库
 *      （.trae-cn/design_libraries/Google/colors_and_type.css）比。
 *   2) 边框可见性 —— 算 --line / --line2 对卡片底的 WCAG 对比度。
 *      踩过的坑：严格照库对齐时，暗色 --border 被设成与卡片同色
 *      （#1d1d1c），对比度 1.00:1 —— **框是真的看不见**，用户直接反馈了。
 *   3) 暗色必须是蓝色系 —— 库的暗色强调色是红（#4a2026/#fc2c50），
 *      用户明确说"不希望用红色、颜色不协调"，所以这里钉死：暗色的
 *      强调色相必须落在 200°~260°（蓝），不许回到红。
 *
 * 依赖：puppeteer-core（复用系统 Chrome）。用法：
 *   node tools/audit_tokens.js [http://127.0.0.1:8787]
 */
const fs = require('fs');

let puppeteer;
try {
  puppeteer = require('puppeteer-core');
} catch (e) {
  console.error('[自检] 缺少 puppeteer-core，请先 npm i puppeteer-core');
  process.exit(2);
}
const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) { console.error('[自检] 没找到 Chrome，请用 CHROME_PATH 指定。'); process.exit(2); }

const BASE = (process.argv[2] || 'http://127.0.0.1:8787').replace(/\/$/, '');

// ── 抄自 .trae-cn/design_libraries/Google/colors_and_type.css ──────────────
const SPEC = {
  light: {
    '--background': '#ffffff', '--foreground': '#0e1115', '--card': '#ffffff',
    '--popover': '#f9f9fa', '--popover-foreground': '#0e1115',
    '--primary': '#4285f4', '--primary-foreground': '#ffffff',
    '--secondary': '#dbeafe', '--secondary-foreground': '#333942',
    '--muted': '#eff1f4', '--muted-foreground': '#7f8d9f',
    '--accent': '#dbeafe', '--accent-foreground': '#003e8f',
    '--destructive': '#ef4444',
    '--border': '#ebebeb', '--input': '#e2e3e4', '--ring': '#4285f4',
    '--chart-1': '#4285f4', '--chart-2': '#ea4335', '--chart-3': '#fbbc05',
    '--chart-4': '#0043ad', '--chart-5': '#34a853',
    '--sidebar': '#f0f6ff', '--sidebar-foreground': '#0e1115',
    '--sidebar-primary': '#4285f4', '--sidebar-accent': '#dbeafe',
    '--sidebar-accent-foreground': '#003e8f', '--sidebar-border': '#e7eaef',
    '--sidebar-ring': '#4285f4',
  },
  dark: {
    '--background': '#161616', '--foreground': '#eff1f4', '--card': '#1d1d1c',
    '--popover': '#2e2e2e', '--popover-foreground': '#e5e5e5',
    '--primary': '#fc2c50', '--primary-foreground': '#ffffff',
    '--secondary': '#383838', '--secondary-foreground': '#e5e5e5',
    '--muted': '#383838', '--muted-foreground': '#949494',
    '--accent': '#4a2026', '--accent-foreground': '#fc2c50',
    '--destructive': '#ef4444',
    '--border': '#1d1d1c', '--input': '#000000', '--ring': '#ffffff',
    '--chart-1': '#2dccd3', '--chart-2': '#f1204a', '--chart-3': '#edbbe8',
    '--chart-4': '#fbeb35', '--chart-5': '#baf6f0',
    '--sidebar': '#171717', '--sidebar-foreground': '#e5e5e5',
    '--sidebar-primary': '#0065fd', '--sidebar-accent': '#00266b',
    '--sidebar-accent-foreground': '#bfdbfe', '--sidebar-border': '#404040',
    '--sidebar-ring': '#00266b',
  },
};

// 允许的偏差，两类，都必须有明确理由：
//   CONFLICT —— 库与 Trae 的记忆稿冲突，以记忆稿为准（那是用户已认可的成品）
//   OVERRIDE —— 用户明确要求的覆盖
const CONFLICT = new Set(['dark:--primary']);       // 库红 / 记忆稿蓝 → 蓝
const OVERRIDE = new Set([
  'dark:--accent',                 // 库红 → 蓝（用户：夜间不要红色）
  'dark:--accent-foreground',      // 同上
  'dark:--border',                 // 库与卡片同色 → 调亮（用户：框看不见）
  'dark:--input',                  // 库纯黑 → 调亮（用户：框看不见）
  'light:--border',                // 库 #ebebeb 只有 1.19:1 → 调亮（用户：日间也要明显）
  'light:--input',                 // 同上
  'dark:--background',             // 库 #161616 对卡片只 1.073:1 → 压到 #0e0e0e
  'light:--background',            // 库 #ffffff 与卡片完全相同 → 压到 #eef0f4
]);                                // 后两条：用户「框跟背景融在一起，分不出」（见 ②b）

// 边框可见性阈值。2026-09-21 用户反馈过三轮：
//   第一次「夜间框不够明显」→ 发现暗色 --line 对卡片只有 1.00:1（库把它设成卡片色）
//   第二次「日间模式的也要强调一点」→ 亮色库值 #ebebeb 只有 1.19:1，也偏弱
//   第三次「框框起来了但跟背景底色融合，区分不出来」→ 前两次都只修了**线**，没修**面**：
//          亮色 --bg 与 --card 都是 #ffffff（面差 1.000:1）、暗色 1.073:1，
//          于是"框"只是浮在同一平面上的一圈线，框里框外一个色。
// 所以标准定成"一眼能看清"，而不是"库的原始值"。暗色阈值更高：
// 低亮度区人眼对明度差的分辨更差，同对比度看着更糊。
const MIN_BORDER = { light: 1.35, dark: 1.40 };
const MIN_CONTROL = { light: 1.50, dark: 1.85 };
// 面差：卡片底 vs 页面底。比线差可以低（大面积色块人眼敏感），但 1.00 就是"没有层次"。
const MIN_FACE = { light: 1.06, dark: 1.10 };

const norm = (s) => {
  s = (s || '').trim();
  let m = s.match(/^#([0-9a-f]{6})$/i);
  if (m) return '#' + m[1].toLowerCase();
  m = s.match(/^#([0-9a-f]{3})$/i);
  if (m) return '#' + m[1].toLowerCase().split('').map((c) => c + c).join('');
  const r = s.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
  if (r) return '#' + [1, 2, 3].map((i) => (+r[i]).toString(16).padStart(2, '0')).join('');
  return s;
};
const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const lum = (h) => {
  const c = rgb(h).map((v) => {
    const x = v / 255;
    return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m);
  return (x + 0.05) / (y + 0.05);
};
const hue = (h) => {
  const [r, g, b] = rgb(h).map((v) => v / 255);
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  if (!d) return -1;
  let x = mx === r ? ((g - b) / d) % 6 : (mx === g ? (b - r) / d + 2 : (r - g) / d + 4);
  return Math.round(((x * 60) + 360) % 360);
};

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });
  let fails = 0, warns = 0;

  for (const theme of ['dark', 'light']) {
    const page = await browser.newPage();
    await page.setViewport({ width: 1560, height: 900 });
    await page.goto(`${BASE}/?theme=${theme}#mem`, { waitUntil: 'networkidle0' });
    await new Promise((r) => setTimeout(r, 400));
    const tok = await page.evaluate((keys) => {
      const cs = getComputedStyle(document.documentElement);
      const o = {};
      keys.forEach((k) => { o[k] = cs.getPropertyValue(k); });
      return o;
    }, [...Object.keys(SPEC[theme]), '--line', '--line2', '--bg', '--d0']);

    console.log(`===== ${theme} =====`);

    // ① 令牌对账
    let diffs = 0;
    for (const [k, want] of Object.entries(SPEC[theme])) {
      const got = norm(tok[k]);
      if (got === norm(want)) continue;
      const tag = CONFLICT.has(theme + ':' + k) ? 'CONFLICT'
        : OVERRIDE.has(theme + ':' + k) ? 'OVERRIDE' : 'DIFF';
      if (tag === 'OVERRIDE') warns++;
      if (tag === 'DIFF') { diffs++; fails++; }
      console.log(`   ${tag.padEnd(9)} ${k.padEnd(30)} 库=${norm(want)} 面板=${got}`);
    }
    console.log(`   ① 令牌对账：${Object.keys(SPEC[theme]).length} 个，硬偏差 ${diffs} 个`);

    // ② 边框可见性（--line 对 --card）
    const card = tok['--card'], line = tok['--line'], line2 = tok['--line2'];
    const r1 = ratio(line, card), r2 = ratio(line2, card);
    const ok1 = r1 >= MIN_BORDER[theme], ok2 = r2 >= MIN_CONTROL[theme];
    if (!ok1 || !ok2) fails++;
    console.log(`   ② 边框可见性：常规 --line ${r1.toFixed(2)}:1（需 ≥${MIN_BORDER[theme]}）`
      + `${ok1 ? ' ✓' : ' ✗'} ｜ 控件 --line2 ${r2.toFixed(2)}:1（需 ≥${MIN_CONTROL[theme]}）`
      + `${ok2 ? ' ✓' : ' ✗'}`);

    // ②b 面差（--card 对 --bg）—— 只有"线"没有"面"，框就是浮着的一圈线
    //     （用户第三次反馈的正是这个：线可见，但框里框外一个色）
    const bg = tok['--bg'] || tok['--d0'];
    const r3 = ratio(card, bg);
    const ok3 = r3 >= MIN_FACE[theme];
    if (!ok3) fails++;
    console.log(`   ②b 面差（卡片底 vs 页面底）${r3.toFixed(3)}:1（需 ≥${MIN_FACE[theme]}）`
      + `${ok3 ? ' ✓' : ' ✗'}`
      + (r3 < 1.01 ? '  ← 完全相同：框画出来了，但框里框外一个色' : ''));

    // ③ 暗色强调色必须是蓝
    if (theme === 'dark') {
      const blue = ['--accent', '--accent-foreground', '--primary', '--sidebar-primary'];
      const bad = blue.filter((k) => {
        const h = hue(norm(tok[k]));
        return h < 200 || h > 260;
      });
      if (bad.length) fails++;
      console.log(`   ③ 暗色强调色为蓝系（色相 200°~260°）：`
        + blue.map((k) => `${k}=${hue(norm(tok[k]))}°`).join(' ')
        + (bad.length ? `  ✗ 不是蓝：${bad.join(',')}` : '  ✓'));
    }
    await page.close();
  }

  await browser.close();
  console.log(`\n自检结束：失败 ${fails} 项，已知覆盖 ${warns} 项`);
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.error('ERR', e && e.message); process.exit(1); });
