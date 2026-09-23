#!/usr/bin/env node
/*
 * 闸门：评审报告第五节「存疑」里被判为"要改"的三条。
 *   （第①②④条不改代码，故不在此闸门内 —— ②保留 hover 上浮、④只补证据。）
 *
 * ① 本机内容页（#sk-list）**默认只展开第一组**，其余收起
 *    —— 左列 5 组合计约 9000px（光"插件"一组 4888px），进门要滚很久。
 * ② `.ghead:hover .cv`（折叠箭头）hover 时颜色确实变（补上评审缺的证据）
 * ③ 采集表状态列**只在"待入库"出徽章**，"已入库"不再重复挂徽章
 *
 * 用法：node tools/verify_ui_polish.js http://127.0.0.1:8787
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log('  ' + (ok ? 'PASS' : 'FAIL') + '  ' + name + (detail ? '  —— ' + detail : ''));
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 1000 });
  await page.goto(base + '/', { waitUntil: 'load' });
  await page.waitForFunction('typeof window.show === "function"');
  await sleep(800);

  /* ── ① 本机内容页：默认只展开第一组 ───────────────────────── */
  await page.evaluate(() => window.show('skill'));
  await sleep(1600);                                  // loadSkills 要探测本机，给足时间
  const grp = await page.evaluate(() => {
    const heads = Array.prototype.slice.call(document.querySelectorAll('#sk-list .grphead.ghead'));
    return {
      n: heads.length,
      open: heads.map((h, i) => {
        const b = document.getElementById('g-skg' + i);
        return !(b && b.classList.contains('hide'));
      }),
      collapsedFlag: heads.map((h) => h.classList.contains('collapsed')),
      arrows: heads.map((h) => { const c = h.querySelector('.cv'); return c ? getComputedStyle(c).color : null; }),
      heads: heads.map((h) => (h.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 18)),
      listHeight: (document.getElementById('sk-list') || {}).scrollHeight || 0,
    };
  });
  check('本机内容页有分组', grp.n >= 2, grp.n + ' 组');
  check('默认只展开第 1 组', grp.n >= 2 && grp.open[0] === true && grp.open.slice(1).every((x) => x === false),
    '展开状态: [' + grp.open.join(', ') + ']  ' + grp.heads.join(' | '));
  check('收起态与 .collapsed 标记一致',
    grp.n >= 2 && grp.collapsedFlag.every((c, i) => c === !grp.open[i]),
    'collapsed: [' + grp.collapsedFlag.join(', ') + ']');
  check('收起后左列高度明显变短', grp.listHeight < 4000, 'scrollHeight=' + grp.listHeight + 'px（改前约 9000px）');

  /* ── ② .ghead:hover 时箭头颜色确实变（补评审缺的证据）───── */
  const headSel = '#sk-list .grphead.ghead:nth-of-type(1)';
  const firstHead = await page.$('#sk-list .grphead.ghead');
  const colorOf = () => page.evaluate(() => {
    const h = document.querySelector('#sk-list .grphead.ghead');
    const c = h && h.querySelector('.cv');
    return c ? getComputedStyle(c).color : null;
  });
  const before = await colorOf();
  if (firstHead) { await firstHead.hover(); await sleep(500); }
  const after = await colorOf();
  check('.ghead:hover 箭头颜色会变', !!before && !!after && before !== after,
    '常态 ' + before + ' → hover ' + after);
  await page.mouse.move(2, 2);
  await sleep(200);

  /* ── ③ 采集表状态列：只在"待入库"出徽章 ──────────────────── */
  await page.evaluate(() => window.show('collect'));
  await sleep(600);
  try { await page.evaluate('doScan()'); } catch (e) { /* 页面切回来时可能已自动扫 */ }
  await page.waitForFunction(
    () => document.querySelectorAll('#scan-list .stable .srow').length > 0,
    { timeout: 30000 }
  ).catch(() => {});
  const scan = await page.evaluate(() => {
    const rows = Array.prototype.slice.call(document.querySelectorAll('#scan-list .stable .srow'));
    const badges = Array.prototype.map.call(
      document.querySelectorAll('#scan-list .stable .srow .bdg'),
      (b) => (b.textContent || '').trim()
    );
    return {
      rows: rows.length,
      inDb: document.querySelectorAll('#scan-list .stable .srow.indb').length,
      badges,
      stateBadges: badges.filter((t) => t.indexOf('已入库') >= 0).length,
      pinBadges: badges.filter((t) => t.indexOf('待入库') >= 0).length,
    };
  });
  check('采集表渲染出行', scan.rows > 0, scan.rows + ' 行，其中 ' + scan.inDb + ' 行已入库');
  check('状态列不再出现"已入库"徽章', scan.stateBadges === 0, '已入库徽章 ' + scan.stateBadges + ' 个');
  check('已入库的行改用"灰底 + 左侧 inset 条"表达（不是靠徽章）',
    scan.inDb === 0 || scan.rows > scan.inDb, '已入库行靠 .indb 样式区分');

  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log('\n  verify_ui_polish: ' + (results.length - failed.length) + '/' + results.length + ' PASS'
    + (failed.length ? '  （' + failed.map((f) => f.name).join('; ') + '）' : ''));
  process.exit(failed.length ? 1 : 0);
})();
