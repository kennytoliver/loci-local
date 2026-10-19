#!/usr/bin/env node
/*
 * 验「点列表项有没有选中底」——四个**有选中语义**的列表（2026-10-15 重写）。
 *
 * 为什么需要它（2026-09-23 用户先后报了两次同一个 bug）：
 *   ① 黑夜下只有记忆页点一条有蓝色底，会话页 / 本机内容页点下去只剩 hover 的淡灰。
 *   ② 质检页与清理页点下去也没有蓝底。
 *   光看代码判断不了"点完到底有没有底色"，也判断不了"鼠标移开后还在不在" —— 只能真点。
 *
 * ⚠️⚠️ 2026-10-15 重写（原标题结构全部作废）
 *   用户拍板：第一版时代的配色与设计全部作废，**以现在这版原型为准**。
 *   旧版测的是六处 `.mem` 行 + 记忆页 / 质检页 / 质检容器 —— 那些 上一版之后只剩
 *   **已删的隐藏 #v-mem 里还有残留**，于是闸门永远点不到东西（`rowEl` 为 null，
 *   报 "Cannot read properties of null (reading 'evaluate')"）。
 *   这一次改成按当前六页的真实行类重列，并做了三处修正：
 *     · 行类 `.mem` → 各页真实行类：`.cr` / `.hr` / `.sr` / `.sfr`
 *     · 选中类 `.sel` → `.on`（上一版 改了名，CSS 里现在全是 `.tr.on` / `.cr.on` / …）
 *     · 只跑 light（面板自 2026-09-24 起无条件亮色，dark 那一轮是噪音）
 *
 * ⚠️ 判据改成"**不硬编码颜色**"：
 *   旧版写死 `EXPECT = { light: 'rgb(219,234,254)' }`（--accent 解析值）。
 *   但 上一版之后各页选中底**本来就不同** —— 采集/交接卡/技能是 --lz-indigo-bg，
 *   库页（销毁候选）是珊瑚色 #fdf2f4。写死任何一个都会误伤另外三页。
 *   现在改成：点之前先记下该行底色，点完把鼠标移开再量 ——
 *   必须**带上 .on 类**且**底色与未选中时不同**，两者都满足才算过。
 *
 * 哪个页面该不该有选中态：会话页点行是"跳详情"，Agent 页点行是"接入/移除"，
 *   **这两页没有选中语义**，所以不在本闸门范围内（不是漏测）。
 *
 * 用法：node tools/verify_sel_feedback.js [http://127.0.0.1:8787]
 * 依赖：puppeteer-core（开发期；产品本身零依赖）
 */
let puppeteer;
try {
  puppeteer = require('puppeteer-core');
} catch (e) {
  console.error('[选中底] 缺少 puppeteer-core，请先 npm i puppeteer-core');
  process.exit(2);
}
const fs = require('fs');
const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
];
const CHROME = CHROME_CANDIDATES.filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) { console.error('[选中底] 没找到 Chrome，请用 CHROME_PATH 指定。'); process.exit(2); }
const url = (process.argv[2] || 'http://127.0.0.1:8787') + '/';

/* 四个有选中语义的列表：导航名 / 列表容器 / 行选择器 / 行类 / 中文名
   会话页与 Agent 页不在内 —— 点行是"跳详情 / 接入移除"，没有选中态。 */
const PAGES = [
  /* ⚠️ 采集页要 :not(.lock) —— 已入库的行带 .lock，ckSelectable() 会把它过滤掉，
     点它 ckToggle() 直接 return，什么都不发生（实测：50 行里 29 行是 lock）。 */
  { v: 'collect', box: '#scan-list', row: '#scan-list .cr:not(.lock)', cls: 'cr', label: '采集' },
  { v: 'handoff', box: '#hf-list', row: '#hf-list .hr', cls: 'hr', label: '交接卡' },
  { v: 'skill', box: '#sk-list', row: '#sk-list .sr', cls: 'sr', label: '技能' },
  { v: 'clean', box: '#sf-list', row: '#sf-list .sfr', cls: 'sfr', label: '库·源文件' },
];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1560, height: 1000, deviceScaleFactor: 1 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e.message).slice(0, 160)));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('console: ' + m.text().slice(0, 160));
  });
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction('typeof window.show === "function"', { timeout: 15000 });

  const out = [];
  for (const p of PAGES) {
    await page.evaluate((v) => window.show(v), p.v);
    await new Promise((r) => setTimeout(r, 2600));   // 等这一页的列表渲染出来

    let ok = true;
    try {
      await page.waitForFunction(
        (sel) => document.querySelectorAll(sel).length > 0,
        { timeout: 12000 }, p.row);
    } catch (e) { ok = false; }
    const rowEl = ok ? await page.$(p.row) : null;
    if (!rowEl) { out.push({ ...p, note: '列表没渲染出行，跳过', pass: null }); continue; }

    /* 等布局落定再量 —— 不等的话 boundingBox 可能取到上一帧的位置，点空就没有 .on（踩过） */
    await new Promise((r) => setTimeout(r, 350));
    /* 先滚进视口，否则 elementFromPoint 直接返回 null */
    await rowEl.evaluate((e) => e.scrollIntoView({ block: 'center' }));
    await new Promise((r) => setTimeout(r, 250));

    /* 点之前先记下"未选中"的底色，作为对照 —— 不写死具体颜色 */
    const before = await rowEl.evaluate((e) => getComputedStyle(e).backgroundColor);

    const box = await rowEl.boundingBox();
    if (!box) { out.push({ ...p, note: '行不可见（拿不到 boundingBox）', pass: null }); continue; }
    const cx = box.x + Math.min(120, box.width / 2);
    const cy = box.y + box.height / 2;

    /* 点之前确认这个点真的落在行上（不是按钮、也不是别的浮层） */
    const hit = await page.evaluate((x, y, cls) => {
      const t = document.elementFromPoint(x, y);
      if (!t) return 'null';
      if (t.closest('button,a')) return 'btn:' + t.tagName;
      return t.closest('.' + cls) ? 'row' : t.tagName;
    }, cx, cy, p.cls);
    if (hit !== 'row') { out.push({ ...p, note: '点击点没落在行上（命中 ' + hit + '）', pass: null }); continue; }

    /* 真点：走 CDP 真鼠标 —— addStyleTag 伪造 class 量不出 :hover 与真实事件 */
    await page.mouse.click(cx, cy);
    await new Promise((r) => setTimeout(r, 350));
    /* 关键：把鼠标挪到空白处脱离 :hover，这时读到的底色才真的是「选中态」 */
    await page.mouse.move(4, 4);
    await new Promise((r) => setTimeout(r, 250));

    /* ⚠️ 必须**重新查这一行**：点击会触发重渲染，旧的 rowEl 句柄指向的节点
       已经脱离 DOM —— 对它求 getComputedStyle 会得到空字符串（实测踩过）。
       所以按"第几行"重新定位，而不是复用旁边那个元素句柄。 */
    const after = await page.evaluate((sel, i, cls) => {
      const rows = document.querySelectorAll(sel);
      const e = rows[i];                       /* i=0：我们点的就是第一行 */
      if (!e) return { bg: '(行没了)', on: false, cls };
      const cs = getComputedStyle(e);
      return { bg: cs.backgroundColor, on: e.classList.contains('on'), cls };
    }, p.row, 0, p.cls);

    const pass = after.on === true && after.bg !== before;
    out.push({ ...p, note: '', before, after, pass });
  }
  await browser.close();

  let bad = 0;
  console.log('列表        点前底色                  点后底色                  带 .on  判定');
  console.log('─'.repeat(88));
  for (const r of out) {
    if (r.pass === null) {
      console.log(String(r.label).padEnd(11) + '—'.padEnd(25) + '—'.padEnd(25) + '—'.padEnd(8) + '跳过：' + r.note);
      continue;
    }
    if (!r.pass) bad++;
    console.log(
      String(r.label).padEnd(11) +
      String(r.before).padEnd(25) +
      String(r.after.bg).padEnd(25) +
      String(r.after.on).padEnd(8) +
      (r.pass ? 'PASS' : 'FAIL（要 .on + 底色有变化）'));
  }
  if (errors.length) {
    console.log('\n页面报错：');
    errors.slice(0, 8).forEach((e) => console.log('  ' + e));
  }
  console.log('\n' + (bad === 0 && errors.length === 0 ? '全部通过' : `失败 ${bad} 项 / 报错 ${errors.length} 条`));
  process.exit(bad === 0 && errors.length === 0 ? 0 : 1);
})();
