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
  'C:/Users/user/AppData/Local/Google/Chrome/Application/chrome.exe',
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

  for (const theme of ['dark', 'light']) {
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);

    // ── 技能页 ──
    await page.evaluate(() => window.show('skill'));
    await page.waitForFunction(
      () => document.querySelectorAll('#sk-list .mem').length > 0, { timeout: 25000 });
    await page.click('#sk-list .mem');
    await page.waitForFunction(
      () => { const e = document.querySelector('#sk-text');
              return e && e.textContent.length > 200; }, { timeout: 25000 });
    await new Promise((r) => setTimeout(r, 500));

    const sk = await page.evaluate(() => ({
      count: document.querySelectorAll('#sk-list .mem').length,
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
      () => !!document.querySelector('#v-session .pagehead'), { timeout: 15000 });
    await page.evaluate(() => document.querySelector('[onclick="runAutoScan()"]').click());
    await page.waitForFunction(
      () => { const e = document.querySelector('#scan-msg');
              return e && e.style.display !== 'none' && /来源：/.test(e.textContent); },
      { timeout: 60000 });
    await new Promise((r) => setTimeout(r, 400));

    const conv = await page.evaluate(() => {
      const page = document.getElementById('v-session');
      // 会话页正文区里第一个 .panel —— 应该是「归档新会话」
      const firstPanel = page.querySelector('.panel');
      return {
        text: document.querySelector('#scan-msg').textContent,
        // 用户要求：会话页不要再有「本机对话来源」模块
        hasSrcCard: !!document.getElementById('src-panel') ||
                    !!document.getElementById('src-list'),
        firstPanelTitle: firstPanel
          ? (firstPanel.querySelector('.listhead .t') || {}).textContent || '' : '',
        // 核对顺序：页头 → 归档表单 → 已归档会话
        order: Array.from(page.querySelectorAll('.pagehead .ptitle, .panel .listhead .t, .listhead .t'))
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

  for (const theme of ['dark', 'light']) {
    const sk = report['技能页-' + theme];
    console.log(`\n[${theme}] 技能页：${sk.count} 个技能 ｜ ${sk.headline}`);
    for (const s of sk.sources) console.log(`   ${s.on ? '●' : '○'} ${s.name.padEnd(12)} ${s.why}`);

    const cv = report['会话页-' + theme];
    console.log(`[${theme}] 会话页：`);
    console.log(`   顺序：${(cv.order || []).join(' → ')}`);
    console.log(`   第一块卡片：${cv.firstPanelTitle}`);
    console.log(`   扫描文案首行："${cv.text.split('\n')[0]}"`);
    const badList = report['文案是旧的硬编码名单-' + theme];
    console.log(`   文案含旧硬编码名单？${badList ? '是 ← 有问题' : '否 ✓'}`);
    console.log(`   还有「本机对话来源」模块？${cv.hasSrcCard ? '有 ← 用户明确不要' : '没有 ✓'}`);
    if (badList) failed++;
    if (cv.hasSrcCard) failed++;
    if (cv.firstPanelTitle.indexOf('归档新会话') < 0) {
      console.log('   ⚠ 第一块卡片不是「归档新会话」← 排版被改回去了');
      failed++;
    }
  }
  if (errs.length) { console.log('\n⚠ 页面报错:', errs); failed++; }
  console.log('\n截图：');
  for (const s of shots) console.log('  ' + path.basename(s) + '  ' + fs.statSync(s).size + 'B');
  console.log(failed ? `\n✗ 有 ${failed} 项不达标` : '\n✓ 全部达标');
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error('失败：' + e.message); process.exit(1); });
