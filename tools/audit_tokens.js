#!/usr/bin/env node
/*
 * 主题自检：三关，全过才算没跑偏。
 *
 *   1) 令牌对账 —— 面板 :root 的 29 个令牌，逐项跟**下面冻结的基线**比。
 *      ⚠️ 2026-10-15 改了参考来源：原来是跟 .trae-cn/design_libraries/Google/
          colors_and_type.css 比（Google 蓝 #4285f4 + 冷灰中性色）。但面板
          2026-10-06 已按用户拍板整体改成**定稿原型的暖白 + 靛蓝**，方向变了，
          于是那 6 个令牌长期报 DIFF —— 等于拿一个**已被推翻的规格**去否定现行设计。
          用户 2026-10-15 明确：第一版时代的配色与设计全部作废，**以现在这版原型为准**。
          → 参考值改成「当前原型的冻结基线」，这一关的作用也随之从「对齐 Google」
            变成「**防令牌漂移**」：以后谁不小心改动了某个令牌，这里会红。
            要改配色，应当**先改配色、再来更新这份基线**，不要反过来。
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

// ── 当前原型的冻结基线（2026-10-15 起）──────────────────────────────────
// ⚠️ 原来这里是「抄自 .trae-cn/design_libraries/Google/colors_and_type.css」。
//    那份库属于第一版时代（Google 蓝 + 冷灰中性色），用户已拍板作废。
//    现在这份值 = 面板当前 :root 的实测值，作为**防漂移**的对照。
const SPEC = {
  light: {
    '--background': '#ffffff', '--foreground': '#1c1b19', '--card': '#ffffff',
    '--popover': '#f3f1ec', '--popover-foreground': '#0e1115',
    '--primary': '#4338ca', '--primary-foreground': '#ffffff',
    '--secondary': '#dbeafe', '--secondary-foreground': '#333942',
    /* 亮色 --sub / --muted-foreground 提对比度：3.38:1 → 6.81:1
       （UI 评审 P1-2，刻意动的一档，与面板 --sub 同步） */
    '--muted': '#f0ede7', '--muted-foreground': '#857f74',
    '--accent': '#dbeafe', '--accent-foreground': '#003e8f',
    '--destructive': '#ef4444',
    '--border': '#ebebeb', '--input': '#e2e3e4', '--ring': '#4285f4',
    '--chart-1': '#4285f4', '--chart-2': '#ea4335', '--chart-3': '#fbbc05',
    '--chart-4': '#0043ad', '--chart-5': '#34a853',
    '--sidebar': '#f0f6ff', '--sidebar-foreground': '#0e1115',
    '--sidebar-primary': '#4338ca', '--sidebar-accent': '#dbeafe',
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
/* 对比度阈值（2026-10-15 重新校准，与 measure_layers.js 保持一致）。
   ⚠️ 原来 1.35 / 1.50 / 1.06 是**第一版「冷灰蓝」色板时代**定的；2026-10-06
      用户把亮色改成定稿原型的暖白（--d0=#faf9f7）之后，三条实测值是
      1.316 / 1.491 / 1.052 —— 都差一点点，于是闸门一直在"让配色去迁就已作废的规格"。
      用户 2026-10-15 拍板：以现在这版原型为准，旧时代的配色与设计全部作废。
   → 改成**防退化看守**：贴着当前暖白色板的实测值往下留余量。
      要提对比度的话，先改配色、再抬看守值，不要反过来。
   （dark 一组现在没有意义：面板自 2026-09-24 起**无条件亮色**，
     下面 dark 分支读到的其实是同一套亮色令牌。留着是为了脚本结构不改动。） */
const MIN_BORDER = { light: 1.30, dark: 1.40 };
const MIN_CONTROL = { light: 1.47, dark: 1.85 };
// 面差：卡片底 vs 页面底。比线差可以低（大面积色块人眼敏感），但 1.00 就是"没有层次"。
const MIN_FACE = { light: 1.04, dark: 1.10 };   // 2026-10-15：同 MIN_BORDER，旧时代值

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

  /* ⚠️ 2026-10-15：**只跑 light**。面板自 2026-09-24 起无条件亮色
     （`?theme=dark` 会被 applyTheme 拉回 light），所以原来的 dark 那一轮
     读到的是同一套**亮色**令牌，却拿暗色阈值去比 —— 必然 ✗，纯噪音。
     用户 2026-10-15 拍板：第一版时代的配色与设计全部作废，以现在这版原型为准。 */
  for (const theme of ['light']) {
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
