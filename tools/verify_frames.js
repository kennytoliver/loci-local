#!/usr/bin/env node
/*
 * 闸门：所有 `.framed` 的外框必须**不被父容器裁掉**。
 *
 * 为什么需要它（评审报告第五节存疑①，2026-09-23 被证实是真 bug）：
 *   `.framed::after` 画在元素**外面** 2px（left/right/top:-2px）。落在 `.split-main`
 *   里的栏头，父容器带 overflow:hidden → 外扩的 2px 被裁：
 *     · `.shead`   左/上/右三面全裁，只剩一条底边；而它自己本来就有 border-bottom，
 *                  于是那条底边是"重描一遍"，纯噪音
 *     · `.grphead` 左/右被裁，只剩上下两条线；而它与 .gbody 本是完整边框盒 →
 *                  上下各多一条 2px 外的平行线，看着像"双线"
 *   一共 8 个键被改回 0。这个闸门就是防止以后有人再点错、或页面结构一变又出现裁切。
 *
 * ⚠️ 关键认知：`.framed` **正常打开也会打上**（`frameTitles()` 每次加载都跑），
 *   `?frames=1` 只是"可视化 + 可编辑"那层。所以本脚本**不加参数**跑，量的就是真实状态。
 *
 * 用法：node tools/verify_frames.js http://127.0.0.1:8787
 * 依赖：puppeteer-core（开发期；产品本身零依赖）
 */
let puppeteer;
try { puppeteer = require('puppeteer-core'); } catch (e) { console.error('缺 puppeteer-core'); process.exit(2); }
const fs = require('fs');
const CHROME = [process.env.CHROME_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe']
  .filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) { console.error('没找到 Chrome'); process.exit(2); }

const argv = process.argv.slice(2);
const base = argv.find((a) => !a.startsWith('--')) || 'http://127.0.0.1:8787';
const shotDir = argv.filter((a) => !a.startsWith('--'))[1];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const VIEWS = ['session', 'mem', 'audit', 'clean', 'collect', 'agents', 'skill', 'pack', 'handoff'];

const PROBE = () => {
  const out = [];
  const sec = document.querySelector("section[id^='v-']:not([hidden])") || document.body;
  const SEL = '.pagehead,.listhead,.shead,.dhead,.grphead';
  const peers = Array.prototype.slice.call(sec.querySelectorAll(SEL));
  Array.prototype.slice.call(document.querySelectorAll('.framed')).forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;                 // 未渲染（隐藏视图）
    const cls = String(el.className || '').replace(/\b(framed|want-frame|no-frame)\b/g, '').trim().split(/\s+/)[0];
    const key = sec.id.replace(/^v-/, '') + '/' + cls + '[' + peers.indexOf(el) + ']';

    let anc = el.parentElement, clip = null;
    while (anc) {
      const cs = getComputedStyle(anc);
      if ([cs.overflow, cs.overflowX, cs.overflowY].some((x) => x && x !== 'visible')) { clip = anc; break; }
      anc = anc.parentElement;
    }
    let clipped = { left: false, top: false, right: false }, info = null;
    if (clip) {
      const cr = clip.getBoundingClientRect(), cs = getComputedStyle(clip);
      const bl = parseFloat(cs.borderLeftWidth) || 0, br = parseFloat(cs.borderRightWidth) || 0,
            bt = parseFloat(cs.borderTopWidth) || 0;
      const boxL = cr.left + bl, boxR = cr.right - br, boxT = cr.top + bt;
      clipped.left = r.left - 2 < boxL - 0.5;
      clipped.right = r.right + 2 > boxR + 0.5;
      clipped.top = r.top - 2 < boxT - 0.5;
      info = { el: (clip.className || clip.tagName).toString().trim().split(/\s+/)[0], ov: cs.overflow };
    }
    out.push({ key, text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 20), clipped, info, any: clipped.left || clipped.top || clipped.right });
  });
  return out;
};

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 1000, deviceScaleFactor: 2 });
  await page.goto(base + '/', { waitUntil: 'load' });
  await page.waitForFunction('typeof window.show === "function"');
  await sleep(900);

  let total = 0, bad = [];
  for (const v of VIEWS) {
    await page.evaluate((x) => window.show(x), v);
    await sleep(900);
    let rows = [];
    try { rows = await page.evaluate(PROBE); } catch (e) { rows = []; }
    total += rows.length;
    rows.filter((r) => r.any).forEach((r) => bad.push({ view: v, ...r }));
  }

  bad.forEach((r) => console.log(
    '  FAIL [' + r.key + '] "' + r.text + '"  被裁: ' +
    ['left', 'top', 'right'].filter((k) => r.clipped[k]).join('+') +
    '  父容器 .' + (r.info ? r.info.el + ' (overflow:' + r.info.ov + ')' : '?')
  ));
  if (bad.length) {
    console.log('\n  → 把上面这些键在 panel.py 的 FRAME_SELECTION 里改成 0（栏头类的框本来就画不全）');
  }

  if (shotDir) {
    fs.mkdirSync(shotDir, { recursive: true });
    for (const v of ['mem', 'skill']) {
      await page.evaluate((x) => window.show(x), v);
      await sleep(800);
      await page.screenshot({ path: shotDir + '/framed-' + v + '.png' });
    }
  }

  await browser.close();
  /* 防空跑：视图没渲染出来时 PROBE 会返回 0 个元素，那样"0 个被裁"就是假通过。
     至少要看到 10 个框才算量到了东西（当前基线 13 个）。 */
  const MIN_EXPECTED = 10;
  const enough = total >= MIN_EXPECTED;
  if (!enough) console.log('  FAIL 只量到 ' + total + ' 个框（少于 ' + MIN_EXPECTED + '）—— 可能视图没渲染出来，等于没测');
  const pass = bad.length === 0 && enough;
  console.log('\n  verify_frames: ' + (total - bad.length) + '/' + total + ' 个框完好  ' + (pass ? 'PASS' : 'FAIL'));
  process.exit(pass ? 0 : 1);
})();
