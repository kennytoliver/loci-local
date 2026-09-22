/* 会话页搜索功能专项探针 —— 复现用户反馈"搜索没反应"。
   用法：node tools/probe_session_search.js [base]   默认 http://127.0.0.1:8787 */
const fs = require('fs');
let puppeteer;
try { puppeteer = require('puppeteer-core'); }
catch (e) { console.error('缺少 puppeteer-core，请设置 NODE_PATH 指向装了它的 node_modules。'); process.exit(2); }
const CHROME = [process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe'].filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) { console.error('没找到 Chrome，请用 CHROME_PATH 指定。'); process.exit(2); }

(async () => {
  const BASE = (process.argv[2] || 'http://127.0.0.1:8787').replace(/\/$/, '');
  const browser = await puppeteer.launch({ headless: true, executablePath: CHROME, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });

  const errs = [];
  page.on('console', (m) => { if (m.type() === 'error') errs.push('console.error: ' + m.text()); });
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));

  await page.goto(BASE + '/#session', { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 1200));

  const listLen = () => page.$eval('#s-list', (el) => el.innerHTML.length).catch(() => -1);
  const listHead = () => page.$eval('#s-list', (el) => el.innerText.slice(0, 90)).catch(() => '');
  console.log('#s-q 输入框存在:', !!(await page.$('#s-q')), '| 初始 #s-list 长度:', await listLen());

  // 一次"设值 + 真回车"，等响应回来再判断
  async function tryEnter(label, value) {
    await page.$eval('#s-q', (el, v) => { el.value = v; }, value);
    await page.focus('#s-q');
    const before = await listLen();
    const waiter = page.waitForResponse((r) => r.url().includes('/api/session/search'), { timeout: 20000 }).catch(() => null);
    await page.keyboard.press('Enter');
    const resp = await waiter;
    let n = -1;
    if (resp) { try { n = JSON.parse(await resp.text()).length; } catch (e) {} }
    await new Promise((r) => setTimeout(r, 500));
    console.log(`[${label}] value=${JSON.stringify(value)} | 请求=${resp ? 'HTTP ' + resp.status() : '无'} | 返回条数=${n}` +
                ` | 长度 ${before}->${await listLen()} | 头=${JSON.stringify(await listHead())}`);
  }

  await tryEnter('英文', 'hippocampus');
  await tryEnter('中文', '海马体');

  // 直接调用函数（对照）
  const direct = await page.evaluate(async () => {
    try {
      document.getElementById('s-q').value = '海马体';
      await window.sessionSearch();
      return 'ok, #s-list 长度=' + document.getElementById('s-list').innerHTML.length;
    } catch (e) { return 'ERR: ' + (e && e.message ? e.message : String(e)); }
  });
  console.log('直接调 sessionSearch():', direct);

  console.log('页面错误:', errs.length ? errs : '(无)');
  await browser.close();
  process.exit(0);
})().catch((e) => { console.error('探针自身报错:', e.message); process.exit(2); });
