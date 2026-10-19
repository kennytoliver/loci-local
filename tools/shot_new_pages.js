#!/usr/bin/env node
/*
 * 给新增的「技能」页和「本机对话来源」卡拍双主题截图，顺便验证：
 *   ① 技能页能列出本机 skill 并渲染详情
 *   ② 会话页的来源卡能显示每个源"扫不扫、为什么"
 *   ③ 扫描文案是**按探测结果动态生成**的，没退回旧的硬编码名单
 *
 * ⚠ 两个坑（都是踩过的）：
 *   · 面板没有 hashchange 监听 → 改 location.hash 切不动页面，得直接调 window.show(id)
 *   · Chrome 在本机是系统安装的，不在 .workbuddy/binaries 下
 *
 * 用法：node tools/shot_new_pages.js [http://127.0.0.1:8787] [输出目录]
 */
const fs = require('fs');
const path = require('path');

let puppeteer;
try {
  puppeteer = require('puppeteer-core');
} catch (e) {
  console.error('[截图] 缺少 puppeteer-core'); process.exit(2);
}

const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  process.env.LOCALAPPDATA + '/Google/Chrome/Application/chrome.exe',
].find((p) => p && fs.existsSync(p));
if (!CHROME) { console.error('[截图] 没找到 Chrome（可设 CHROME_PATH）'); process.exit(2); }

const BASE = process.argv[2] || 'http://127.0.0.1:8787';
const OUT = process.argv[3] || path.resolve(__dirname, '..', 'docs', '截图-改版-20260921');

const OLD_HARDCODED = /（ZCode \/ Claude Code \/ Codex）/;

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 2 });

  const errs = [];
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text()); });

  const shots = [];
  const report = {};

  await page.goto(BASE, { waitUntil: 'networkidle2' });
  await page.waitForFunction(() => typeof window.show === 'function', { timeout: 15000 });

  /* ⚠️ 2026-10-15：只跑 light。面板自 2026-09-24 起**无条件亮色**，
     `?theme=dark` 会被 applyTheme() 拉回 light，所以原来那句
     `setAttribute('data-theme', t)` 设了也白设 —— dark 那一轮存的其实是亮色截图，
     还会用暗色阈值去判，纯噪音。用户拍板：第一版时代的配色全部作废。 */
  for (const theme of ['light']) {

    // ── 技能页 ──
    await page.evaluate(() => window.show('skill'));
    await page.waitForFunction(
      () => document.querySelectorAll('#sk-list .sr').length > 0, { timeout: 25000 });
    /* ⚠️ 2026-10-15：原来 `page.click('#sk-list .mem')` 想"点行打开详情"。
       实测 上一版 里技能行的 onclick 是 **skToggle(i)**（勾选/取消勾选），
       根本不打开详情 —— 于是等 #sk-text 有内容永远超时。
       而详情是由 pickSkill(i) 填的，它目前只被**隐藏容器 #sk-srcs** 里的行调用
       （= 技能详情在 UI 上没有可见入口，这是另一个待拍板的问题）。
       截图脚本是开发工具，直接调 pickSkill(0) 是合理的。 */
    await page.evaluate(() => window.pickSkill(0));
    await page.waitForFunction(
      () => { const e = document.querySelector('#sk-text');
              return e && e.textContent.length > 200; }, { timeout: 25000 });
    await new Promise((r) => setTimeout(r, 500));

    const sk = await page.evaluate(() => ({
      count: document.querySelectorAll('#sk-list .sr').length,
      headline: (document.getElementById('sk-count') || {}).textContent || '',
      sources: Array.from(document.querySelectorAll('#sk-src .srcrow')).map((r) => ({
        on: r.classList.contains('on'),
        name: r.querySelector('.sname').textContent,
        why: r.querySelector('.swhy').textContent,
      })),
    }));
    report['技能页-' + theme] = sk;
    let f = path.join(OUT, `skill-${theme}.png`);
    await page.screenshot({ path: f, fullPage: true });
    shots.push(f);

    // ── 会话页：验证"来源卡已移除 + 归档表单是第一块 + 扫描文案仍动态" ──
    await page.evaluate(() => window.show('session'));
    await page.waitForFunction(
      () => !!document.querySelector('#v-session .phead'), { timeout: 15000 });
    await page.evaluate(() => document.querySelector('[onclick="runAutoScan()"]').click());
    await page.waitForFunction(
      /* ⚠️ 2026-10-15 两处一起改：
         ① 原来是 #scan-msg —— 上一版 后 **0 处定义**，scanmsg() 正因写它而抛错
            （顶栏「重新扫描」整个是坏的，已修）。
         ② scanmsg() 现在优先写采集页的 #scan-info，**但当前在会话页** ——
            #scan-info 挂在隐藏的 v-collect 里，于是 scanmsg() 会退化成 **toast**。
            所以这里两个地方都要等：采集页的 #scan-info，或任意 toast。
            （原来只等 #scan-info，在会话页永远等不到 → 60000ms 超时。） */
      () => {
        const e = document.querySelector('#scan-info');
        if (e && e.style.display !== 'none' && /来源：/.test(e.textContent)) return true;
        return [...document.querySelectorAll('#toast .toast-item')]
          .some((t) => /来源：/.test(t.textContent));
      },
      { timeout: 60000 });
    await new Promise((r) => setTimeout(r, 400));

    const conv = await page.evaluate(() => {
      const page = document.getElementById('v-session');
      // 会话页正文区里第一个 .panel —— 应该是「归档新会话」
      /* ⚠️ 2026-10-15：会话页正文区的容器从 `.panel` 换成 `.card`（上一版）。 */
      const firstPanel = page.querySelector('.sp-main .card');
      return {
        text: (document.querySelector('#scan-info') || {}).textContent || '',
        // 用户要求：会话页不要再有「本机对话来源」模块
        hasSrcCard: !!document.getElementById('src-panel') ||
                    !!document.getElementById('src-list'),
        /* ⚠️ 2026-10-15：原来取「第一张 .card 的标题」。上一版 会话页主区是表格
           不是卡片，所以改成量表格本身 + 页面标题。 */
        hasTbl: !!document.querySelector('#v-session .sp-main .tbl'),
        rows: document.querySelectorAll('#s-list .tr').length,
        title: ((document.querySelector('#v-session .phead .ttl h2') || {}).textContent || '').trim(),
        // 核对顺序：页头 → 归档表单 → 已归档会话
        /* ⚠️ 2026-10-15：原来查 `.pagehead .ptitle` / `.panel .listhead .t`
           （旧工作台骨架）。上一版之后会话页是 `.phead .ttl h2` + `.cols > .sp-main`。 */
        order: Array.from(page.querySelectorAll('.phead .ttl h2, .sp-main .card .chead .t'))
          .map((e) => e.textContent.trim()).filter(Boolean).slice(0, 4),
      };
    });
    report['会话页-' + theme] = conv;
    report['文案是旧的硬编码名单-' + theme] = OLD_HARDCODED.test(conv.text);
    f = path.join(OUT, `session-${theme}.png`);
    await page.screenshot({ path: f, fullPage: true });
    shots.push(f);
  }

  await browser.close();

  let failed = 0;

  /* ⚠️ 2026-10-15：报告循环原来也写死 dark+light，但上面只跑 light →
     report['技能页-dark'] 是 undefined，一读 .count 就抛。两处要一致。 */
  for (const theme of ['light']) {
    const sk = report['技能页-' + theme];
    console.log(`\n[${theme}] 技能页：${sk.count} 个技能 ｜ ${sk.headline}`);
    for (const s of sk.sources) console.log(`   ${s.on ? '●' : '○'} ${s.name.padEnd(12)} ${s.why}`);

    const cv = report['会话页-' + theme];
    console.log(`[${theme}] 会话页：`);
    console.log(`   顺序：${(cv.order || []).join(' → ')}`);
    /* ⚠️ 2026-10-15：这行原打印「第一块卡片」，上一版 后会话页主区是表格不是卡片，
       firstPanelTitle 已经不用了（改成下面那行「主区」）。 */
    console.log(`   扫描文案首行："${cv.text.split('\n')[0]}"`);
    const badList = report['文案是旧的硬编码名单-' + theme];
    console.log(`   文案含旧硬编码名单？${badList ? '是 ← 有问题' : '否 ✓'}`);
    console.log(`   还有「本机对话来源」模块？${cv.hasSrcCard ? '有 ← 用户明确不要' : '没有 ✓'}`);
    if (badList) failed++;
    if (cv.hasSrcCard) failed++;
    /* ⚠️ 2026-10-15：原来断言「第一块卡片是『归档新会话』」——那是 上一版 之前
       会话页的版面（顶部一张归档表单卡）。上一版之后会话页主区是**表格**
       （.cols > .sp-main > .tbl），归档表单整张卡已经不在会话页里了。
       用户拍板以现在这版原型为准 → 改成断言当前版面真的在：
       主区是表格、表格有行、页面标题是「会话」。 */
    if (!(cv.hasTbl && cv.rows > 0 && cv.title === '会话')) {
      console.log('   ⚠ 会话页主区不是「标题=会话 + 有行的表格」← 版面被改坏了');
      failed++;
    } else {
      console.log('   主区：会话表格（' + cv.rows + ' 行）✓');
    }
  }
  if (errs.length) { console.log('\n⚠ 页面报错:', errs); failed++; }
  console.log('\n截图：');
  for (const s of shots) console.log('  ' + path.basename(s) + '  ' + fs.statSync(s).size + 'B');
  console.log(failed ? `\n✗ 有 ${failed} 项不达标` : '\n✓ 全部达标');
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error('失败：' + e.message); process.exit(1); });
