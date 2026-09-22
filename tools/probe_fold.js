/* 长文折叠探针：验证会话原文/记忆详情是否被折叠、展开按钮是否真能用。
   用法：node tools/probe_fold.js [base] */
const fs = require('fs');
let puppeteer;
try { puppeteer = require('puppeteer-core'); }
catch (e) { console.error('缺少 puppeteer-core，请设置 NODE_PATH。'); process.exit(2); }
const CHROME = [process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe'].filter(Boolean).find((p) => fs.existsSync(p));

(async () => {
  const BASE = (process.argv[2] || 'http://127.0.0.1:8787').replace(/\/$/, '');
  const browser = await puppeteer.launch({ headless: true, executablePath: CHROME, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const errs = [];
  page.on('pageerror', (e) => errs.push(e.message));

  await page.goto(BASE + '/#session', { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 1200));

  // 打开第一个会话（原文时间线在右栏）
  const sid = await page.evaluate(() => {
    const el = document.querySelector('#s-list .mem[onclick*="openSession"]');
    if (!el) return null;
    const m = /openSession\((\d+)\)/.exec(el.getAttribute('onclick'));
    return m ? +m[1] : null;
  });
  console.log('打开会话 id:', sid);
  if (sid) { await page.evaluate((id) => openSession(id), sid); await new Promise((r) => setTimeout(r, 1600)); }

  const stat = await page.evaluate(() => {
    const bus = Array.from(document.querySelectorAll('#s-view .bub'));
    const lim = (typeof window.FOLD_CHARS === 'number') ? window.FOLD_CHARS : 320;
    return {
      total: bus.length,
      lim,
      long: bus.filter((b) => b.textContent.trim().length > lim).length,
      short: bus.filter((b) => b.textContent.trim().length <= lim).length,
      folded: document.querySelectorAll('#s-view .bub.folded').length,
      btns: document.querySelectorAll('#s-view .foldbtn').length,
    };
  });
  console.log(`气泡 共${stat.total} | 阈值>${stat.lim}字 长${stat.long} 短${stat.short} | 已折叠${stat.folded} | 按钮${stat.btns}`);

  // 点第一个按钮：高度应变大、文字变「收起」；再点回来
  const r = await page.evaluate(() => {
    const btn = document.querySelector('#s-view .foldbtn');
    if (!btn) return { err: '没有折叠按钮（可能这条会话都不够长）' };
    const body = btn.previousElementSibling;
    const h = () => Math.round(body.getBoundingClientRect().height);
    const h0 = h(), t0 = btn.textContent;
    btn.click();
    const h1 = h(), t1 = btn.textContent;
    btn.click();
    const h2 = h(), t2 = btn.textContent;
    return { h0, h1, h2, t0, t1, t2 };
  });
  if (r.err) console.log('展开测试:', r.err);
  else console.log(`展开测试: 高度 ${r.h0} → ${r.h1} → ${r.h2} | 按钮「${r.t0}」→「${r.t1}」→「${r.t2}」`);

  const ok = !r.err && r.h1 > r.h0 && r.h2 === r.h0 && r.t1.includes('收起') && r.t0.includes('展开');
  console.log('结论:', ok ? 'PASS 折叠/展开正常' : 'FAIL');
  console.log('页面错误:', errs.length ? errs : '(无)');
  await browser.close();
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error('探针自身报错:', e.message); process.exit(2); });
