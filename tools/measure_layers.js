#!/usr/bin/env node
/*
 * 层次可见性测量：把「面差」和「线差」分成两件事分别量。
 *
 * 为什么单独写这个：
 *   audit_tokens.js 第②关只量了「描边 vs 卡片底」（--line / --card），
 *   从来没有量过「卡片底 vs 页面底」（--card / --bg）。
 *   亮色下这两个值都是 #ffffff —— 面差 1.00:1（完全相同），
 *   而 --line 对卡片 1.37:1 是"合格"的，所以旧闸门一路绿。
 *   用户看到的却是"框画出来了，但框里框外一个色，分不开"。
 *
 * 所以本脚本量三个对比度，缺一不可：
 *   ① 卡片面 vs 页面面   （--card / --bg）      —— 面差，分层靠它
 *   ② 描边   vs 卡片面   （--line / --card）    —— 线差，勾轮廓靠它
 *   ③ 控件描边 vs 卡片面 （--line2 / --card）   —— 输入框轮廓
 * 再用 getComputedStyle 复核真实 DOM 元素的底色，防止令牌对但元素没引用对。
 *
 * 用法：node tools/measure_layers.js [http://127.0.0.1:8787]
 *   Windows 需 NODE_PATH 指向装了 puppeteer-core 的 node_modules。
 */
const fs = require('fs');

let puppeteer;
try {
  puppeteer = require('puppeteer-core');
} catch (e) {
  console.error('[测量] 缺少 puppeteer-core');
  process.exit(2);
}
const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) { console.error('[测量] 没找到 Chrome'); process.exit(2); }

const BASE = (process.argv[2] || 'http://127.0.0.1:8787').replace(/\/$/, '');

// ── 颜色工具 ───────────────────────────────────────────────────────────────
const norm = (s) => {
  s = (s || '').trim();
  let m = s.match(/^#([0-9a-f]{6})$/i);
  if (m) return '#' + m[1].toLowerCase();
  m = s.match(/^#([0-9a-f]{3})$/i);
  if (m) return '#' + m[1].toLowerCase().split('').map((c) => c + c).join('');
  const r = s.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?/);
  if (r) {
    // 半透明色：按白色打底合成，跟浏览器视觉一致（本项目 chrome 就是不透明的浅色）
    let [rr, gg, bb] = [+r[1], +r[2], +r[3]];
    const a = r[4] === undefined ? 1 : +r[4];
    if (a < 1) {
      rr = Math.round(rr * a + 255 * (1 - a));
      gg = Math.round(gg * a + 255 * (1 - a));
      bb = Math.round(bb * a + 255 * (1 - a));
    }
    return '#' + [rr, gg, bb].map((v) => v.toString(16).padStart(2, '0')).join('');
  }
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

// 阈值：面差比线差可以低（大面积色块人眼更敏感），但 1.00 就是「没有」。
const MIN = {
  face: { light: 1.06, dark: 1.10 },   // 卡片面 vs 页面面
  line: { light: 1.35, dark: 1.40 },   // 与 audit_tokens.js 对齐
  ctrl: { light: 1.50, dark: 1.85 },
};

const PAGES = [
  ['session', '会话'], ['mem', '记忆'], ['audit', '质检'], ['clean', '清理'],
  ['collect', '采集'], ['agents', 'Agent'], ['pack', '记忆包'], ['handoff', '交接卡'],
];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });
  let fails = 0;

  for (const theme of ['dark', 'light']) {
    const page = await browser.newPage();
    await page.setViewport({ width: 1560, height: 900 });
    await page.goto(`${BASE}/?theme=${theme}#mem`, { waitUntil: 'networkidle0' });
    await new Promise((r) => setTimeout(r, 400));

    const tok = await page.evaluate(() => {
      const cs = getComputedStyle(document.documentElement);
      const g = (k) => cs.getPropertyValue(k).trim();
      return {
        bg: g('--bg'), card: g('--card'), line: g('--line'), line2: g('--line2'),
        d0: g('--d0'), d1: g('--d1'), chrome: g('--chrome'), muted: g('--muted'),
      };
    });

    // 真实 DOM 复核：每页的内容区底 + 卡片底/描边
    // 注意 .content 与部分容器是 rgba(0,0,0,0)（透明，实际透出 body），
    // 直接读 backgroundColor 会把"透明"当成白色算错，所以必须向上找有效背景。
    const dom = await page.evaluate((pages) => {
      const alphaOf = (c) => {
        const m = (c || '').match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?/);
        return m ? (m[4] === undefined ? 1 : +m[4]) : 0;
      };
      const effBg = (el) => {
        let n = el, hops = 0;
        while (n && hops < 12) {
          const c = getComputedStyle(n).backgroundColor;
          if (alphaOf(c) > 0.001) {
            return { color: c, from: n === el ? 'self'
              : (n.tagName.toLowerCase() + (n.className ? '.' + String(n.className).split(' ')[0] : '')) };
          }
          n = n.parentElement; hops++;
        }
        return { color: 'rgb(255,255,255)', from: 'fallback' };
      };
      const out = {
        content: effBg(document.querySelector('.content') || document.body),
        pages: {},
      };
      // 卡片类名不止 .panel：质检页的仪表是 .health，主从页是 .split-main/.split-side。
      // 只认 .panel 会漏掉质检页（它的 #health/#audit-out 内容全靠 JS 填，静态 HTML 里没有 .panel）。
      const CARD_SEL = '.panel, .health, .split-main, .split-side';
      for (const [id] of pages) {
        const sec = document.getElementById('v-' + id);
        if (!sec) continue;
        const panel = sec.querySelector(CARD_SEL);
        const card = panel || sec;
        const c = getComputedStyle(card);
        const eb = effBg(card);
        out.pages[id] = {
          cls: panel ? panel.className.split(' ').slice(0, 2).join('.') : '(无卡片容器)',
          bg: eb.color.split(',').slice(0, 3).join(',') + ')',
          bgFrom: eb.from,
          bd: c.borderTopColor,
          bw: c.borderTopWidth,
        };
      }
      return out;
    }, PAGES);

    console.log(`\n===== ${theme} =====`);
    console.log(`  令牌： --bg=${tok.bg} --card=${tok.card} --line=${tok.line} --line2=${tok.line2}`);
    console.log(`         --chrome=${tok.chrome}  --muted=${tok.muted}`);

    const contentBg = norm(dom.content.color);
    const bg = norm(tok.bg), card = norm(tok.card);
    const R = {
      face: ratio(card, bg),
      line: ratio(norm(tok.line), card),
      ctrl: ratio(norm(tok.line2), card),
    };
    const mark = (v, t) => (v >= t ? '✓' : '✗');
    const faceOk = R.face >= MIN.face[theme];
    const lineOk = R.line >= MIN.line[theme];
    const ctrlOk = R.ctrl >= MIN.ctrl[theme];
    if (!faceOk || !lineOk || !ctrlOk) fails++;
    console.log(`  ① 面差（卡片底 vs 页面底）  ${R.face.toFixed(3)}:1  需 ≥${MIN.face[theme]}  ${mark(R.face, MIN.face[theme])}`
      + (R.face < 1.01 ? '   ← 完全相同，框里框外一个色' : ''));
    console.log(`  ② 线差（常规描边 vs 卡片）  ${R.line.toFixed(2)}:1  需 ≥${MIN.line[theme]}  ${mark(R.line, MIN.line[theme])}`);
    console.log(`  ③ 线差（控件描边 vs 卡片）  ${R.ctrl.toFixed(2)}:1  需 ≥${MIN.ctrl[theme]}  ${mark(R.ctrl, MIN.ctrl[theme])}`);

    console.log(`  DOM 复核：内容区底=${contentBg}（来自 ${dom.content.from}）  --bg=${bg}`
      + (contentBg === bg ? ' ✓ 一致' : ' ✗ 不一致！'));
    const rows = Object.entries(dom.pages);
    let sameAsPage = 0;
    for (const [id, v] of rows) {
      const r = ratio(norm(v.bg), contentBg);
      if (r < 1.01) sameAsPage++;
      console.log(`    ${id.padEnd(8)} ${(v.cls || '').padEnd(16)} 底=${norm(v.bg)} (对内容区 ${r.toFixed(3)}:1) 描边=${norm(v.bd)} ${v.bw}`);
    }
    if (sameAsPage) {
      console.log(`    ⚠ 有 ${sameAsPage} 个页面的卡片底与内容区底相同 —— 面差为 0`);
      fails++;
    }
    await page.close();
  }

  await browser.close();
  console.log(`\n测量结束：${fails ? '有 ' + fails + ' 个主题不达标' : '两个主题三层都达标'}`);
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.error('ERR', e && e.message); process.exit(1); });
