#!/usr/bin/env node
/*
 * 给「本机内容」页这一轮新增的两块（插件 / 历史版本）拍双主题基线截图。
 *
 * 三张一组 × 明暗 = 6 张：
 *   ① 左列滚到「插件」分组 —— 看得到插件行 + 旧版本徽标 + 下面的「历史版本」段
 *   ② 插件详情 —— 本地版本列表、"当前"标记
 *   ③ 配置备份详情 —— 脱敏后的正文（models.json.bak，真值是明文 API Key）
 *
 * ⚠ 踩过的坑（照抄现有截图脚本的做法）：
 *   · 面板没有 hashchange 监听 → 改 location.hash 切不动页面，得直接调 window.show()
 *   · 主题用 documentElement 的 data-theme，且**不能**同时依赖 localStorage ——
 *     上一轮我按错的 key 写 localStorage，截出来的图跟以为的主题不一样
 *   · 拍完要校验 6 张 md5 互不相同，否则可能"切页/切主题根本没生效"，
 *     看着有图其实是同一张（"图有 16 张"不等于"16 个状态都拍到了"）
 *
 * 用法：node tools/shot_content_plugins.js [http://127.0.0.1:8787] [输出目录]
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

let puppeteer;
try { puppeteer = require('puppeteer-core'); }
catch (e) { console.error('[截图] 缺少 puppeteer-core'); process.exit(2); }

const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  process.env.LOCALAPPDATA + '/Google/Chrome/Application/chrome.exe',
].find((p) => p && fs.existsSync(p));
if (!CHROME) { console.error('[截图] 没找到 Chrome（可设 CHROME_PATH）'); process.exit(2); }

const BASE = process.argv[2] || 'http://127.0.0.1:8787';
const OUT = process.argv[3] || path.resolve(__dirname, '..', 'docs', 'shots');

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

  await page.goto(BASE, { waitUntil: 'networkidle2' });
  await page.waitForFunction(() => typeof window.show === 'function', { timeout: 15000 });

  const shots = [];
  const info = {};

  for (const theme of ['dark', 'light']) {
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
    await page.evaluate(() => window.show('skill'));
    await page.waitForFunction(
      () => typeof PLUGINS !== 'undefined' && PLUGINS.length > 0 &&
            document.querySelectorAll('#sk-list .mem').length > 0, { timeout: 25000 });
    await new Promise((r) => setTimeout(r, 400));

    // ① 左列滚到「插件」分组
    await page.evaluate(() => {
      const h = [...document.querySelectorAll('#sk-list .grphead')]
        .find((e) => e.querySelector('.gt').textContent === '插件');
      if (h) h.scrollIntoView({ block: 'start' });
    });
    await new Promise((r) => setTimeout(r, 400));
    let f = path.join(OUT, `content-plugins-${theme}.png`);
    await page.screenshot({ path: f, fullPage: true });
    shots.push(f);
    info['插件分组-' + theme] = await page.evaluate(() => {
      const heads = [...document.querySelectorAll('#sk-list .grphead')]
        .map((e) => e.querySelector('.gt').textContent);
      const rows = [...document.querySelectorAll('#sk-list .mem[onclick*="pickPlugin"]')];
      return {
        groups: heads,
        plugins: rows.length,
        withOldBadge: rows.filter((r) => /旧版本/.test(r.textContent)).length,
        backups: document.querySelectorAll('#sk-list .mem[onclick*="pickBackupRow"]').length,
        headline: (document.getElementById('sk-count') || {}).textContent || '',
      };
    });

    // ② 插件详情：**优先挑有旧版本的** —— 只有 1 个版本的插件看不出
    //    "当前 / 旧" 的差别，那样这张基线图就白拍了（拍的是功能的空壳）。
    const pi = await page.evaluate(() => {
      const i = PLUGINS.findIndex((p) => p.old_count > 0);
      return i >= 0 ? i : PLUGINS.findIndex((p) => p.version_count > 0);
    });
    if (pi >= 0) {
      await page.evaluate((i) => pickPlugin(i), pi);
      await page.waitForFunction(
        () => /本地版本/.test((document.getElementById('sk-text') || {}).textContent || ''),
        { timeout: 15000 });
      await new Promise((r) => setTimeout(r, 400));
      f = path.join(OUT, `content-plugin-detail-${theme}.png`);
      await page.screenshot({ path: f, fullPage: true });
      shots.push(f);
      const d = await page.evaluate(() => ({
        name: (document.querySelector('#sk-detail .dhtop h3') || {}).textContent || '',
        text: document.getElementById('sk-text').textContent,
      }));
      info['插件详情-' + theme] = {
        name: d.name,
        versions: (d.text.match(/^\s*[▶ ]\s*\d+\.\d+/gm) || []).length,
        current: /←\s*当前/.test(d.text),
        oldCount: (await page.evaluate((i) => PLUGINS[i].old_count, pi)),
        head: d.text.slice(0, 120),
      };
      if (!(info['插件详情-' + theme].versions > 1 && info['插件详情-' + theme].current)) {
        console.log('  ✗ 插件详情截图没拍到多个版本 + 当前标记');
        process.exitCode = 1;
      }
    }

    // ③ 备份详情：models.json.bak（真值是明文 API Key）
    const mi = await page.evaluate(
      () => BACKUPS.findIndex((b) => /^models\.json\.bak-/.test(b.name)));
    if (mi >= 0) {
      await page.evaluate((i) => pickBackupRow(i), mi);
      await page.waitForFunction(
        () => /已隐去|读不出/.test((document.getElementById('sk-text') || {}).textContent || ''),
        { timeout: 15000 });
      await new Promise((r) => setTimeout(r, 400));
      f = path.join(OUT, `content-backup-detail-${theme}.png`);
      await page.screenshot({ path: f, fullPage: true });
      shots.push(f);
      info['备份详情-' + theme] = await page.evaluate(() => ({
        name: (document.querySelector('#sk-detail .dhtop h3') || {}).textContent || '',
        masked: /已隐去/.test(document.getElementById('sk-text').textContent),
        note: (document.getElementById('cfg-note') || {}).textContent || '',
      }));
    }
  }

  await browser.close();

  // 6 张图必须互不相同 —— 否则"切主题/切状态"没生效，看着有图其实拍的是同一张
  const md5 = shots.map((f) => crypto.createHash('md5').update(fs.readFileSync(f)).digest('hex'));
  const uniq = new Set(md5).size;

  console.log('  ' + JSON.stringify(info, null, 2).replace(/\n/g, '\n  '));
  console.log('');
  console.log('  共 ' + shots.length + ' 张，md5 去重后 ' + uniq + ' 张');
  shots.forEach((f, i) => console.log('    ' + path.basename(f) + '  ' + md5[i].slice(0, 8)));

  let fail = 0;
  if (errs.length) { fail++; console.log('  ✗ 页面有 JS 报错：' + errs.join(' | ')); }
  else console.log('  ✓ 页面无 JS 报错');
  if (uniq !== shots.length) { fail++; console.log('  ✗ 有重复截图：切主题/切状态没生效'); }
  else console.log('  ✓ ' + shots.length + ' 张截图互不相同');
  const bad = Object.keys(info).filter((k) => /插件分组/.test(k) &&
    info[k].plugins === 0);
  if (bad.length) { fail++; console.log('  ✗ 插件分组是空的：' + bad.join('、')); }
  else console.log('  ✓ 插件分组有内容');
  if (fail) process.exit(1);
})().catch((e) => { console.error('[截图] 异常：' + e.message); process.exit(1); });
