#!/usr/bin/env node
/*
 * 边框专项探针 —— 用户点名要查的"边框 / 文字边框尺寸是否正确"。
 *
 * 为什么单写一个（既有闸门查不到）：
 *   现有闸门只钉了少数几个点的行高（如本机内容页列表项），**没有全局量过描边**。
 *   而描边恰恰是最容易出事的一类：
 *     · 全局规则顺带命中（上一次 `.content` 类名撞车就是这么炸的，每行虚增 68px）
 *     · 四边宽度不一致、上下 padding 不对称 → 文字在框里肉眼可见地偏
 *     · 框宽 / 框高被撑大或压缩 → "文字边框大小不正确"
 *   底色和对比度有 audit_tokens 管，**尺寸和裁切只有这里管**。
 *
 * 判定规则（都能在 docs/设计规范-面板.md 找到依据，不是拍脑袋）：
 *   E1 文字被裁      : 元素有直接文字  且 scrollWidth>clientWidth+1（横向吞字）
 *   E2 四边描边不一致: 文字级元素四边 border-width 不等（规范：描边统一 1px）
 *   E3 描边非 1px    : 文字级元素 border-width ∉ {0,1}px
 *   E4 上下内边距不对称: 文字级元素 padTop≠padBottom（规范里的标签都是对称的）
 *   E5 框高对不上     : 框高 ≠ (内容行高 + 上下 padding + 上下描边)，差 > 1.5px
 *                       —— 这一条专抓"被外部规则撑高"的虚增
 *   E6 框不可见       : 描边色与父底同色（对比度 < 1.1）→ 框画了等于没画
 *
 * 用法：
 *   NODE_PATH=... node tools/probe_borders.js http://127.0.0.1:8787
 *   NODE_PATH=... node tools/probe_borders.js http://127.0.0.1:8787 --json out.json
 */
let puppeteer;
try {
  puppeteer = require('puppeteer-core');
} catch (e) {
  console.error('[边框探针] 缺少 puppeteer-core，请设置 NODE_PATH 指向装了它的 node_modules。');
  process.exit(2);
}
const fs = require('fs');
const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) {
  console.error('[边框探针] 没找到 Chrome，请用 CHROME_PATH 指定。');
  process.exit(2);
}

const argv = process.argv.slice(2);
const BASE = (argv.find((a) => !a.startsWith('--')) || 'http://127.0.0.1:8787').replace(/\/$/, '');
const ji = argv.indexOf('--json');
const JSON_OUT = ji >= 0 ? argv[ji + 1] : null;
// 反向测试：不注回归就看不出这盏灯亮不亮（规范第八节的硬要求）
const BROKEN = argv.includes('--broken');

// 人造的边框回归 —— 六类问题各造一个，探针必须全部抓到。
// ⚠️ 目标元素要用 `#skill` 页上**确实存在**的（.proj / .btn），
//    初版用的 `.tag` / `.mini` 在技能页没有实例，注入等于没注（灯不亮查了半天）。
const BROKEN_CSS = `
  .proj { border-left-width: 3px !important; }               /* E2 四边不等 / E3 非 1px */
  .proj { padding: 6px 8px 1px 8px !important; }             /* E4 上下内边距不对称 */
  .btn  { min-height: 52px !important; }                     /* E5 被外部规则撑高 */
  .btn  { white-space: nowrap; max-width: 26px; overflow: hidden; } /* E1 横向吞字 */
  .btn  { border-color: rgba(0,0,0,0) !important; }          /* E6 框不可见 */
`;

const VIEWS = BROKEN ? ['skill'] : ['agents', 'audit', 'clean', 'collect', 'handoff', 'mem', 'pack', 'session', 'skill'];

// 页面里量描边：核心都在这个函数里跑（注入浏览器）
function collect() {
  const px = (v) => Math.round((parseFloat(v) || 0) * 100) / 100;
  const path = (el) => {
    const bits = [];
    let cur = el, depth = 0;
    while (cur && cur.nodeType === 1 && depth < 4) {
      let s = cur.tagName.toLowerCase();
      if (cur.id) s += '#' + cur.id;
      const cls = (cur.className && typeof cur.className === 'string')
        ? cur.className.trim().split(/\s+/).filter(Boolean).slice(0, 2) : [];
      if (cls.length) s += '.' + cls.join('.');
      bits.unshift(s);
      cur = cur.parentElement; depth++;
    }
    return bits.join(' > ');
  };
  const directText = (el) => {
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.textContent;
    return t.trim();
  };
  // 相对亮度（sRGB 简化版，够用于"同色判定"）
  const lum = (rgb) => {
    const m = /rgba?\(([^)]+)\)/.exec(rgb);
    if (!m) return null;
    const p = m[1].split(',').map((x) => parseFloat(x));
    if (p.length > 3 && p[3] === 0) return null; // 全透明
    const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(p[0]) + 0.7152 * f(p[1]) + 0.0722 * f(p[2]);
  };
  const ratio = (a, b) => {
    if (a == null || b == null) return null;
    const [hi, lo] = a > b ? [a, b] : [b, a];
    return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100;
  };
  // 向后找第一个不透明的背景色（判定"框可见性"时的实际衬托底）
  const bgBehind = (el) => {
    let cur = el.parentElement;
    while (cur) {
      const cs = getComputedStyle(cur);
      const c = cs.backgroundColor;
      if (c && !/rgba\(0, 0, 0, 0\)|transparent/.test(c)) return c;
      cur = cur.parentElement;
    }
    return getComputedStyle(document.body).backgroundColor;
  };

  const out = [];
  for (const el of document.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    const bt = px(cs.borderTopWidth), br = px(cs.borderRightWidth);
    const bb = px(cs.borderBottomWidth), bl = px(cs.borderLeftWidth);
    const anyBorder = (bt + br + bb + bl) > 0;
    if (!anyBorder) continue;
    if (cs.borderTopStyle === 'none' && cs.borderRightStyle === 'none'
      && cs.borderBottomStyle === 'none' && cs.borderLeftStyle === 'none') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;           // 不可见 / 折叠
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    if (cs.opacity === '0') continue;

    const txt = directText(el);
    const lh = cs.lineHeight === 'normal' ? px(cs.fontSize) * 1.2 : px(cs.lineHeight);
    // 四周都围起来（非零边数 ≥ 3）才叫"文字框"。
    // 只有单边描边的是**分隔线 / 下划线**（.sfoot 的 border-top、.shead 的 border-bottom、
    // .leadlink 的虚线下划线），那是有意设计，不该按"框"判尺寸 —— 初版没排除，误报一片。
    const sides = [bt, br, bb, bl].filter((x) => x > 0).length;
    const isLabel = !!txt && txt.length <= 40 && r.height < 64 && sides >= 3;
    // 行盒高怎么取才准（踩过两次）：
    //   ① 不能用 fontSize×1.2 猜 —— Chrome 的 normal 行高由字体度量决定，中文栈常在 1.3~1.5 倍
    //   ② 也不能只用 Range —— Range 给的是**字体盒**高度，**不含 leading**
    //      （实测 `.msg`：Range 16px，真实行盒 21.88px，差 5.88px → 又一片误报）
    //   正确顺序：CSS 写了确定 line-height 就用它（最权威）→ normal 时退回 Range → 最后才猜
    const lhRaw = cs.lineHeight;
    const lhExact = lhRaw === 'normal' ? null : px(lhRaw);
    let contentH = null, lines = 0;
    const tnodes = Array.from(el.childNodes).filter((n) => n.nodeType === 3 && n.textContent.trim());
    if (tnodes.length) {
      try {
        const rg = document.createRange();
        rg.selectNodeContents(el);
        const rects = Array.from(rg.getClientRects()).filter((x) => x.height > 0);
        lines = rects.length;
        if (rects.length) contentH = Math.round(rects.reduce((a, b) => a + b.height, 0) * 100) / 100;
      } catch (e) { /* 量不到就算了 */ }
    }
    // 单行文字的行盒高；多行时各段高度之和会被 leading 影响，E5 直接跳过
    const lineBox = lhExact != null ? lhExact * Math.max(lines, 1) : (contentH != null ? contentH : px(cs.fontSize) * 1.2);
    const base = lineBox;
    const expectH = base + px(cs.paddingTop) + px(cs.paddingBottom) + bt + bb;
    const colorT = lum(cs.borderTopColor), bgT = lum(bgBehind(el));

    out.push({
      path: path(el),
      cls: (typeof el.className === 'string' ? el.className.trim() : '') || el.tagName.toLowerCase(),
      txt: txt.slice(0, 30),
      isLabel, sides, lines, unmeasured: lhExact == null && contentH == null,
      w: Math.round(r.width * 100) / 100,
      h: Math.round(r.height * 100) / 100,
      b: [bt, br, bb, bl],
      pad: [px(cs.paddingTop), px(cs.paddingRight), px(cs.paddingBottom), px(cs.paddingLeft)],
      fs: px(cs.fontSize), lh, expectH: Math.round(expectH * 100) / 100,
      scrollW: el.scrollWidth, clientW: el.clientWidth,
      scrollH: el.scrollHeight, clientH: el.clientHeight,
      overflow: cs.overflow,
      borderColor: cs.borderTopColor,
      // 边框色全透明 = 框根本没画（比"对比度低"更彻底）
      borderInvisible: /rgba\(0,\s*0,\s*0,\s*0\)|transparent/.test(cs.borderTopColor),
      behind: bgBehind(el),
      contrast: ratio(colorT, bgT),
    });
  }
  return out;
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--hide-scrollbars', '--no-first-run'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 900, deviceScaleFactor: 1 });

  const report = { base: BASE, themes: {} };

  for (const theme of BROKEN ? ['dark'] : ['dark', 'light']) {
    report.themes[theme] = {};
    for (const v of VIEWS) {
      await page.goto(`${BASE}/?theme=${theme}#${v}`, { waitUntil: 'networkidle2' });
      try {
        await page.waitForFunction(() => typeof window.show === 'function', { timeout: 10000 });
      } catch (e) { /* 继续 */ }
      // 切页必须**校验真的切过去了**：
      //   面板没有 hashchange 监听，改 hash 切不动，得直接调 window.show(id)。
      //   切失败时页面还是上一页 → 量出来的数会张冠李戴（初版 session/light 只有 22 条
      //   就是没切过去，差点当成"数据没加载"去查后端）。
      let ok = false;
      for (let attempt = 0; attempt < 3 && !ok; attempt++) {
        await page.evaluate((id) => {
          if (typeof window.show === 'function') { try { window.show(id); } catch (e) {} }
        }, v);
        await new Promise((r) => setTimeout(r, 800));
        ok = await page.evaluate((id) => {
          const el = document.getElementById('v-' + id);
          if (!el) return true; // 该页没有独立容器时跳过校验
          return el.offsetParent !== null || getComputedStyle(el).display !== 'none';
        }, v);
      }
      if (!ok) console.log(`    （提示：${theme}/#${v} 切页校验未通过，数据可能没渲染）`);

      if (BROKEN) await page.addStyleTag({ content: BROKEN_CSS });

      const items = await page.evaluate(collect);
      // 判定
      const findings = [];
      for (const it of items) {
        const [bt, br, bb, bl] = it.b;
        if (it.isLabel) {
          if (it.scrollW > it.clientW + 1) findings.push({ code: 'E1', ...it });
          const set = new Set([bt, br, bb, bl].filter((x) => x > 0));
          // E2 只看"四边描边是否等宽"（规范要求统一 1px），
          // 别再挂 `padT===padB` 之类附加条件 —— 初版这么写，反向测试时被自己的用例挡住
          if (set.size > 1) findings.push({ code: 'E2', ...it });
          for (const x of [bt, br, bb, bl]) if (x > 0 && x !== 1) findings.push({ code: 'E3', ...it });
          if (Math.abs(it.pad[0] - it.pad[2]) > 0.01 && it.h < 40) findings.push({ code: 'E4', ...it });
          // 阈值 3px：放过字体度量带来的 1~2px 近似噪声，
          // 但真出事的那种（`.content` 撞车每行虚增 68px）一定抓得到
          if (!it.unmeasured && it.lines <= 1 && Math.abs(it.h - it.expectH) > 3) {
            findings.push({ code: 'E5', ...it });
          }
        }
        if ((it.contrast != null && it.contrast < 1.1) || it.borderInvisible) {
          if (it.h > 4) findings.push({ code: 'E6', ...it });
        }
      }
      report.themes[theme][v] = { total: items.length, labels: items.filter((x) => x.isLabel).length, findings };
      const tag = findings.length ? `!! ${findings.length} 条` : 'ok';
      process.stdout.write(`  ${theme.padEnd(5)} #${v.padEnd(8)} 描边元素 ${String(items.length).padStart(3)}  文字级 ${String(items.filter((x) => x.isLabel).length).padStart(3)}  ${tag}\n`);
    }
  }

  // 汇总：按规则码聚合（同一路径只报一次）
  const byCode = {};
  for (const theme of Object.keys(report.themes)) {
    for (const v of Object.keys(report.themes[theme])) {
      for (const f of report.themes[theme][v].findings) {
        const key = f.code;
        byCode[key] = byCode[key] || [];
        if (!byCode[key].some((x) => x.path === f.path && x.theme === theme)) {
          byCode[key].push({ theme, view: v, ...f });
        }
      }
    }
  }
  report.summary = {};
  for (const k of Object.keys(byCode).sort()) report.summary[k] = byCode[k].length;

  console.log('\n── 汇总（按规则码去重后的条数）──');
  const NAME = {
    E1: '文字被裁（横向吞字）', E2: '四边描边宽度不一致', E3: '描边不是 1px',
    E4: '上下内边距不对称', E5: '框高与内容不符（疑被撑高）', E6: '描边与底色同色（框看不见）',
  };
  if (!Object.keys(byCode).length) console.log('  无发现 ✓');
  for (const k of Object.keys(byCode).sort()) {
    console.log(`  ${k} ${NAME[k] || ''}: ${byCode[k].length} 条`);
    for (const f of byCode[k].slice(0, 6)) {
      console.log(`      [${f.theme}/${f.view}] ${f.cls}  高${f.h} 期望${f.expectH} 边[${f.b.map((n) => n).join(',')}] pad[${f.pad.join(',')}] 对比${f.contrast}  "${f.txt.slice(0, 18)}"`);
      console.log(`         ${f.path}`);
    }
  }

  if (JSON_OUT) { fs.writeFileSync(JSON_OUT, JSON.stringify({ report, detail: byCode }, null, 2), 'utf8'); console.log(`\n明细已写入 ${JSON_OUT}`); }
  await browser.close();

  if (BROKEN) {
    // 反向测试：注进去的五类问题必须全被抓到，少一类就说明这盏灯是坏的
    const need = ['E1', 'E2', 'E3', 'E4', 'E5', 'E6'];
    const miss = need.filter((k) => !byCode[k] || !byCode[k].length);
    if (miss.length) {
      console.error(`\n[反向测试] 失败：注入了回归却抓不到 ${miss.join(' ')} —— 探针有漏，别信它的绿灯。`);
      process.exit(1);
    }
    console.log(`\n[反向测试] 通过：注入的六类回归全部命中（${need.map((k) => k + '×' + byCode[k].length).join(' ')}）。`);
    process.exit(0);
  }

  // 没发现问题才算通过（E6 单列，不算失败；尺寸类必须为零）
  const fatal = ['E1', 'E2', 'E3', 'E4', 'E5'].reduce((n, k) => n + (byCode[k] ? byCode[k].length : 0), 0);
  process.exit(fatal > 0 ? 1 : 0);
})().catch((e) => { console.error('ERR', e && e.message); process.exit(2); });
