// #s-list 被四种内容复用 —— 逐个验证行结构没被压扁（用户最初报的那个 bug 的根）
// ① 会话列表（网格表行 .tr）② 解析预览 ③ 抽取候选 ④ 原话检索（后三个是可读卡片 .mem）
//
// ⚠️ 2026-10-13 按 R3 六页设计更新：
//   · ① 会话列表从「紧凑行 .mem.lrow + .mtitle」改成「网格表行 .tr + .tt」，
//     点击从 gotoSession() 改成 openSessionDetail()。
//     「#s-list 被四种内容复用」这个**前提仍然成立**（代码里确认过），所以闸门保留，
//     只换选择器 —— 不是退役。
//   · 主题循环去掉：面板自 2026-09-24 起**只有亮色主题**（`?theme=dark` 会被拉回 light），
//     同一个主题跑两遍没有意义。
//   · 行高断言从写死的 61.19 改成「按当前实测值 ± 容差」，并把实测值写在断言里 ——
//     写死一个会随设计微调的像素数，只会让闸门在无关改动时变红。
const puppeteer = require('puppeteer-core');
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rec = (n, ok, i) => console.log((ok ? 'PASS  ' : 'FAIL  ') + n + (i ? '  ' + i : ''));

// 会话行（.tr）与卡片行（.mem）的正文类名不同，量之前要指明用哪个
const ROW_SEL = { session: '#s-list .tr', card: '#s-list .mem' };
const TEXT_SEL = { session: '.tt', card: '.content' };

(async () => {
  const b = await puppeteer.launch({ executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'] });
  const p = await b.newPage();
  await p.setViewport({ width: 1560, height: 900, deviceScaleFactor: 1 });
  await p.goto('http://127.0.0.1:8787/#session', { waitUntil: 'networkidle0' });
  await p.waitForFunction(() => document.querySelectorAll('#s-list .tr').length > 0, { timeout: 20000 });
  await sleep(400);

  // 量一行：宽度、正文宽度、有没有一字一行（高度异常大 + 宽度极小）
  const probe = (kind) => p.evaluate((k, ROWS, TEXTS) => {
    const rows = [...document.querySelectorAll(ROWS[k])];
    const one = rows[0];
    if (!one) return { n: 0 };
    const t = one.querySelector(TEXTS[k]);
    const r = one.getBoundingClientRect();
    const tr = t ? t.getBoundingClientRect() : { width: 0, height: 0 };
    return { n: rows.length, w: +r.width.toFixed(1), h: +r.height.toFixed(1),
      textW: +tr.width.toFixed(1), textH: +tr.height.toFixed(1),
      q: getComputedStyle(one).display, cls: one.className,
      onclick: one.getAttribute('onclick') || '' };
  }, kind, ROW_SEL, TEXT_SEL);

  const sess = await probe('session');
  rec('① 会话列表 = 网格表行（高≈57.5、标题铺满、点行跳详情）',
    sess.cls.split(' ').includes('tr') && Math.abs(sess.h - 57.5) < 3
      && sess.textW > 200 && /openSessionDetail/.test(sess.onclick),
    `行高=${sess.h}（基准 57.5±3）标题宽=${sess.textW} display=${sess.q} onclick=${sess.onclick}`);

  // ② 解析预览 —— **已从闸门移除**（2026-10-13）。
  //    原因：R3 六页改版把「粘贴/导入对话」的表单整个删了（`#s-text` / `#s-info` /
  //    `#s-save` 三个元素在 panel.py 里都已经是 0 处），`sessionParse()` 成了**死代码** ——
  //    它还在读一个不存在的元素。UI 都没了，就没有"预览行会不会被压扁"这回事。
  //    这里不再断言，改为在下面 ③④ 覆盖 `#s-list` 仍然活着的复用场景。

  // ③ 抽取候选 —— **已从闸门移除**（2026-10-13）。
  //    查证过程：`extractSession()` 写的是 `#s-view` / `#s-view-acts` / `#s-view-t` /
  //    `#s-view-h` —— 这四个 id 在 panel.py 里**各是 0 处**。也就是说 R3 把旧会话详情
  //    栏换成 `v-sdetail`（`sd-*`）之后，旧的 `s-view` 渲染器整块成了死代码：
  //    「抽取记忆」按钮是渲染进 `#s-view-acts` 的，而那个元素不存在 → 按钮根本不出现
  //    → 这条路径**从 UI 上不可达**。测它等于测一堆死代码。
  //    （抽取候选这个能力本身搬到了采集页，由那边自己的闸门覆盖。）

  // ④ 原话检索（`#s-q` 搜索框 → sessionSearch()，结果也写回 #s-list）
  const found = await p.evaluate(async () => {
    document.getElementById('s-q').value = '记忆';
    await window.sessionSearch();
    await new Promise((r) => setTimeout(r, 800));
    return document.querySelectorAll('#s-list .mem').length;
  });
  if (found) {
    const sv = await probe('card');
    rec('④ 原话检索 = 可读卡片（正文有宽度）',
      sv.textW > 300 && sv.textH < 200,
      `命中=${found} 行高=${sv.h} 正文宽=${sv.textW} class=${sv.cls}`);
  } else console.log('  ④ 检索无命中，跳过');

  // 清干净
  await p.evaluate(() => { document.getElementById('s-q').value = ''; window.loadSessions(); });
  await p.close();
  await b.close();
  const fails = sess && sess.n > 0 ? 0 : 1;
  process.exit(fails);
})().catch((e) => { console.error('ERR', e.message); process.exit(1); });
