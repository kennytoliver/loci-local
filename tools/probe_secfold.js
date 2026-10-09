/* 区域级折叠探针：验证"整块收起"真的让布局收缩 + 状态能记住。
   用法：node tools/probe_secfold.js [base] */
const fs = require('fs');
let puppeteer;
try { puppeteer = require('puppeteer-core'); }
catch (e) { console.error('缺少 puppeteer-core，请设置 NODE_PATH。'); process.exit(2); }
const CHROME = [process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe'].filter(Boolean).find((p) => fs.existsSync(p));

(async () => {
  const BASE = (process.argv[2] || 'http://127.0.0.1:8787').replace(/\/$/, '');
  const browser = await puppeteer.launch({ headless: true, executablePath: CHROME, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const errs = [];
  page.on('pageerror', (e) => errs.push(e.message));

  await page.goto(BASE + '/#session', { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 1600));

  const probe = async (headSel, bodySel, label) => {
    const r = await page.evaluate((hs, bs) => {
      const h = document.querySelector(hs), b = document.querySelector(bs);
      if (!h || !b) return { err: `找不到 ${hs} / ${bs}` };
      if (!h.dataset.secfold) return { err: `${hs} 没有 data-secfold（未初始化）` };
      const hh = () => Math.round(b.getBoundingClientRect().height);
      const before = hh();
      h.click();
      const afterHide = hh(), hasHide = b.classList.contains('hide');
      const one = document.querySelector(hs + ' .sec-1');
      const oneTxt = one ? one.textContent.slice(0, 24) : '(无)';
      const arrow = h.classList.contains('sec-collapsed') ? '▶' : '▼';
      h.click();
      const afterShow = hh();
      return { before, afterHide, afterShow, hasHide, oneTxt, arrow };
    }, headSel, bodySel);
    if (r.err) { console.log(`[${label}] ${r.err}`); return r; }
    console.log(`[${label}] 高度 ${r.before} → 收起后 ${r.afterHide} → 展开后 ${r.afterShow}` +
                ` | hide=${r.hasHide} | 收起摘要="${r.oneTxt}"`);
    return r;
  };

  const a = await probe('#v-session .split-main .shead', '#v-session .split-main .sbody', '会话·左列表');
  const b = await probe('#v-session .split-side .dhead', '#v-session .split-side .dmain', '会话·右栏原文');

  // 持久化：收起右栏 → 刷新 → 应仍是收起
  await page.evaluate(() => document.querySelector('#v-session .split-side .dhead').click());
  await new Promise((r) => setTimeout(r, 400));
  const before = await page.evaluate(() => document.querySelector('#v-session .split-side .dmain').classList.contains('hide'));
  await page.reload({ waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 1600));
  const after = await page.evaluate(() => {
    const el = document.querySelector('#v-session .split-side .dmain');
    return el ? el.classList.contains('hide') : null;
  });
  console.log(`持久化: 刷新前 hide=${before} → 刷新后 hide=${after}（期望都是 true）`);
  await page.evaluate(() => { try { localStorage.removeItem('hp.sec.sess-view'); } catch (e) {} });

  const ok = !a.err && !b.err && a.hasHide && a.afterHide < a.before && a.afterShow > a.afterHide
    && b.hasHide && b.afterHide < b.before && b.afterShow > b.afterHide && after === true;
  console.log('结论:', ok ? 'PASS 区域折叠生效、布局跟随、状态能记住' : 'FAIL');
  console.log('页面错误:', errs.length ? errs : '(无)');
  await browser.close();
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error('探针自身报错:', e.message); process.exit(2); });
