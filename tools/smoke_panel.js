#!/usr/bin/env node
/*
 * 记忆页交互冒烟测试（只读，不改数据库）。
 *
 * 为什么需要它：
 *   改版会重写卡片 HTML（cardHtml），id 没问题不代表交互没问题。
 *   这个脚本在真实浏览器里跑一遍关键路径：选中→详情、分组折叠、
 *   类型圆点、横向不溢出、页面无 JS 报错，明暗两主题各测一次。
 *
 * 依赖：npm i puppeteer-core（复用系统 Chrome）；用法：
 *   node tools/smoke_panel.js [http://127.0.0.1:8787]
 *   Windows 若报找不到模块，设置 NODE_PATH 指向装了 puppeteer-core 的 node_modules。
 *
 * 注意：不要在这里点「设为常驻」「删除」—— 那会写真实数据库。
 */
const fs = require('fs');

let puppeteer;
try {
  puppeteer = require('puppeteer-core');
} catch (e) {
  console.error('[冒烟] 缺少 puppeteer-core，请先 npm i puppeteer-core --no-audit --no-fund');
  process.exit(2);
}
const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) {
  console.error('[冒烟] 没找到 Chrome，请用 CHROME_PATH 指定。');
  process.exit(2);
}

const BASE = (process.argv[2] || 'http://127.0.0.1:8787').replace(/\/$/, '');
const results = [];
const rec = (name, pass, info = '') =>
  results.push(`${pass ? 'PASS' : 'FAIL'}  ${name}${info ? '  ' + info : ''}`);

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'],
  });

  const selBgByTheme = {};
  for (const theme of ['dark', 'light']) {
    const page = await browser.newPage();
    const errs = [];
    page.on('pageerror', (e) => errs.push(String(e.message || e)));
    page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
    await page.setViewport({ width: 1560, height: 900, deviceScaleFactor: 1 });
    await page.goto(`${BASE}/?theme=${theme}#mem`, { waitUntil: 'networkidle0' });
    await page.waitForFunction(() => document.querySelectorAll('#list .mem').length > 0, { timeout: 8000 });
    await new Promise((r) => setTimeout(r, 300));

    rec(`[${theme}] 无页面 JS 报错`, errs.length === 0, errs.slice(0, 2).join(' | '));

    /* 类型标记已上色 —— 2026-09-24 改版后，标记从「8px 圆点 .mdot + 内联 background」
       变成「30px 图标块 .mico + .mico.<类型> 类吃 --t-*-bg 令牌」。
       断言跟着换，但**同时把原来那条弱的加强**：老版本只要求"颜色种数 ≥1"，
       全是一个颜色也能过；现在要求"同类同色、异类异色"，才真的守住"类型可区分"。 */
    const marks = await page.$$eval('#list .mem', (rows) => rows.map((r) => {
      const t = r.querySelector('.mmeta .tb');
      const m = r.querySelector('.mico');
      return { type: t ? t.textContent.trim() : '', bg: m ? getComputedStyle(m).backgroundColor : '' };
    }));
    const byType = {};
    let uncolored = 0;
    marks.forEach((x) => {
      if (!x.type || !x.bg || x.bg === 'rgba(0, 0, 0, 0)') { uncolored++; return; }
      (byType[x.type] = byType[x.type] || new Set()).add(x.bg);
    });
    const types = Object.keys(byType);
    const typeColors = new Set(types.map((t) => [...byType[t]][0]));
    rec(`[${theme}] 类型图标块已按类型上色（同类同色 / 异类异色）`,
      marks.length > 0 && uncolored === 0 && types.length > 0
        && types.every((t) => byType[t].size === 1) && typeColors.size === types.length,
      `${marks.length} 行 / ${types.length} 种类型 / ${typeColors.size} 种颜色${uncolored ? ' / 未上色 ' + uncolored : ''}`);

    const id = await page.$eval('#list .mem', (e) => e.id.replace('memcard-', ''));
    await page.click(`#memcard-${id}`);
    await new Promise((r) => setTimeout(r, 500));
    const detail = await page.evaluate(() => {
      const b = document.querySelector('#detail .dbody');
      return {
        len: b ? b.textContent.trim().length : 0,
        selected: !!document.querySelector('#list .mem.sel'),
      };
    });
    rec(`[${theme}] 点击列表项 → 右侧详情填充`, detail.len > 5, `正文长度=${detail.len}`);
    rec(`[${theme}] 选中态已标记`, detail.selected);

    // 交互三态必须互不相同：默认(透明) / hover(中性灰) / 选中(模式强调色)
    const states = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('#list .mem'));
      const sel = document.querySelector('#list .mem.sel') || rows[0];
      const other = rows.find((e) => e !== sel) || rows[0];
      return {
        selBg: getComputedStyle(sel).backgroundColor,
        selShadow: getComputedStyle(sel).boxShadow,
        selTitle: getComputedStyle(sel.querySelector('.mtitle')).color,
        restBg: getComputedStyle(other).backgroundColor,
        otherId: other.id,
      };
    });
    selBgByTheme[theme] = states.selBg;
    await page.hover('#' + states.otherId);
    await new Promise((r) => setTimeout(r, 250));
    const hoverBg = await page.evaluate(
      (i) => getComputedStyle(document.getElementById(i)).backgroundColor, states.otherId);
    rec(`[${theme}] 默认 / hover / 选中 三态底色互不相同`,
      new Set([states.restBg, hoverBg, states.selBg]).size === 3,
      `${states.restBg} ｜ ${hoverBg} ｜ ${states.selBg}`);
    rec(`[${theme}] 选中行有左侧强调条`, /inset/.test(states.selShadow),
      states.selShadow.slice(0, 48));

    // 批7：页面标题 / 列表项结构（名字 + 时间 · 平台 · 常驻|最近 · 标签）/ 详情分区
    const head = await page.evaluate(() => {
      const t = document.querySelector('#page-title');
      const row = document.querySelector('#list .mem');
      const meta = row ? row.querySelector('.mmeta') : null;
      const stateB = meta ? meta.querySelector('.bdg.pin, .bdg.state') : null;
      const tags = meta ? meta.querySelector('.mtags') : null;
      return {
        title: t ? t.textContent.trim() : null,
        titleSize: t ? getComputedStyle(t).fontSize : null,
        meta: meta ? meta.textContent.replace(/\s+/g, ' ').trim() : null,
        machine: meta ? /#[0-9]/.test(meta.textContent) || /\bP[1-4]\b/.test(meta.textContent) : true,
        badges: meta ? [...meta.querySelectorAll('.bdg')].map((b) => b.textContent.trim()) : [],
        state: stateB ? stateB.textContent.trim() : null,
        tags: tags ? tags.textContent.trim() : '',
        tooltip: row ? (row.getAttribute('title') || '') : '',
      };
    });
    rec(`[${theme}] 数值条下方有页面标题「记忆」`,
      head.title === '记忆' && head.titleSize === '20px',
      `标题=${head.title} 字号=${head.titleSize}`);
    rec(`[${theme}] 列表 meta 不再出现 #编号 / P重要度 机器码`, !head.machine, head.meta);
    rec(`[${theme}] meta = 平台徽章 + 常驻/最近 状态徽章`,
      head.badges.length >= 1 && !!head.state,
      `徽章=${head.badges.join('/')} 状态=${head.state}`);
    rec(`[${theme}] 机器码挪到行 title（鼠标悬停可见）`,
      /#\d+/.test(head.tooltip), head.tooltip.slice(0, 32));
    const dsecs = await page.evaluate(() =>
      [...document.querySelectorAll('#detail .dsec-t')].map((e) => e.textContent.trim()));
    rec(`[${theme}] 详情已分区（内容 / 标签 / 归属信息）`,
      dsecs.includes('标签') && dsecs.includes('归属信息'), dsecs.join(' · '));

    // 防 class 撞名：曾经 `.pri` 同时是「主按钮」和「输出强调小字」，
    // `.pri{font-size:11px}` 把主按钮压成了 11px。主按钮必须与普通按钮同字号。
    const btnFonts = await page.evaluate(() => {
      const fs = (e) => (e ? getComputedStyle(e).fontSize : null);
      const plain = [...document.querySelectorAll('.btn')]
        .find((x) => !x.classList.contains('pri'));
      return { pri: fs(document.querySelector('.btn.pri')), plain: fs(plain) };
    });
    rec(`[${theme}] 主按钮字号 = 普通按钮字号（防 class 撞名）`,
      !!btnFonts.pri && btnFonts.pri === btnFonts.plain,
      `主=${btnFonts.pri} 普通=${btnFonts.plain}`);

    // 八个页面都必须有标题区（.pagehead + 20px/600 的 .ptitle），切换时不报错、不横向溢出。
    // ⚠️ 必须用 show(k) 切页 —— 面板靠 history.replaceState 改 hash，没有 hashchange 监听，
    // 改 location.hash 是切不动的（会"假通过"：隐藏元素里也有标题、也没有溢出）。
    const pageHeads = await page.evaluate(async () => {
      const want = { mem: '记忆', session: '会话', audit: '记忆质检', clean: '清理',
        collect: '采集中心', agents: 'Agent', pack: '记忆包', handoff: '项目交接卡' };
      const bad = [];
      for (const k of Object.keys(want)) {
        window.show(k);
        await new Promise((r) => setTimeout(r, 280));
        const sec = document.getElementById('v-' + k);
        const pt = sec.querySelector('.pagehead .ptitle');
        const size = pt ? getComputedStyle(pt).fontSize : null;
        const over = [...sec.querySelectorAll('*')]
          .filter((e) => e.getBoundingClientRect().right > document.documentElement.clientWidth + 1).length;
        if (sec.hidden) bad.push(k + '(没切过去)');
        else if (!pt || pt.textContent.trim() !== want[k]) bad.push(k + '(标题=' + (pt ? pt.textContent.trim() : '无') + ')');
        else if (size !== '20px') bad.push(k + '(字号=' + size + ')');
        else if (over !== 0) bad.push(k + '(溢出 ' + over + ')');
      }
      window.show('mem');
      // 回到记忆页后列表要重新加载，等它回来，否则后面的折叠测试会扑空
      for (let i = 0; i < 40 && !document.querySelector('#list .mem'); i++) {
        await new Promise((r) => setTimeout(r, 100));
      }
      return bad;
    });
    rec(`[${theme}] 八个页面都有 20px 标题区且不溢出`,
      pageHeads.length === 0, pageHeads.length ? pageHeads.join(' / ') : '8/8 通过');

    // 质检仪表（规范 Chart 组件）：环形进度 + 四色扣分条 + 计数 chips
    // 仪表是异步画的，且是「两次接口串行」：show('audit') → runAudit() 等 /api/audit
    // → renderHealth() 再等 /api/health，两个都回来才画出环。
    // ⚠️ 2026-09-21 踩坑：原来等 80×100ms(=8s)，而真实耗时约 6.4s（两个接口各 3.2s），
    //    余量不足 1s → 约 1/3 概率假失败（html=0B，看着像"仪表没渲染"）。
    //    真根因在 hippocampus.py 的 O(n^2) 配对（每对重复 tokenize）已修（3.2s→0.3s）。
    //    这里同时把预算放宽到 20s，并回报实测等待时长 + 设一条宽松上限，
    //    这样以后万一又慢了会明确失败，而不是偶发假失败。
    const gauge = await page.evaluate(async () => {
      const WAIT_MS = 20000;
      const t0 = Date.now();
      window.show('audit');
      while (Date.now() - t0 < WAIT_MS && !document.querySelector('#health .hring')) {
        await new Promise((r) => setTimeout(r, 50));
      }
      const waited = Date.now() - t0;
      const bars = [...document.querySelectorAll('#health .hbar i')]
        .map((e) => getComputedStyle(e).backgroundColor);
      const empty = (document.querySelector('#health') || {}).innerHTML || '';
      const o = {
        ring: !!document.querySelector('#health .hring'),
        barColors: new Set(bars).size, bars: bars.length,
        chips: document.querySelectorAll('#health .hchip').length,
        grade: (document.querySelector('#health .hgrade') || {}).textContent || '',
        htmlLen: empty.length,
        waited: waited,
      };
      window.show('mem');
      for (let i = 0; i < 40 && !document.querySelector('#list .mem'); i++) {
        await new Promise((r) => setTimeout(r, 100));
      }
      return o;
    });
    rec(`[${theme}] 质检仪表 = 环形进度 + 多色扣分条`,
      gauge.ring && gauge.bars >= 4 && gauge.barColors >= 4 && gauge.chips >= 1,
      `环=${gauge.ring} 条=${gauge.bars}/色${gauge.barColors} chips=${gauge.chips} `
      + `等级=${gauge.grade.slice(0, 6)} html=${gauge.htmlLen}B 等待=${gauge.waited}ms`);
    // 性能回归哨兵：正常约 0.6s（两个接口各 0.3s）。给 5s 上限 —— 余量约 8 倍不会假失败，
    // 但哪天又退回 6.4s（两个接口各 3.2s）这里会明确报出来，而不是偶发超时。
    rec(`[${theme}] 质检页渲染耗时在预算内（<5s）`,
      gauge.waited < 5000, `实测等待 ${gauge.waited}ms`);

    await page.click('#gh-recent');
    await new Promise((r) => setTimeout(r, 250));
    const hidden = await page.$eval('#g-recent', (e) => e.classList.contains('hide'));
    await page.click('#gh-recent');
    await new Promise((r) => setTimeout(r, 250));
    const shown = await page.$eval('#g-recent', (e) => !e.classList.contains('hide'));
    rec(`[${theme}] 分组折叠/展开正常`, hidden && shown);

    const overflow = await page.evaluate(() => {
      const lw = document.querySelector('#list').getBoundingClientRect().width;
      const bad = Array.from(document.querySelectorAll('#list .mem'))
        .filter((e) => e.getBoundingClientRect().width > lw + 1).length;
      return { bad, lw: Math.round(lw) };
    });
    rec(`[${theme}] 列表项未横向溢出`, overflow.bad === 0, `列宽=${overflow.lw}`);

    await page.close();
  }

  // 选中色必须随模式变化（Google 规范：亮色走蓝 #dbeafe、暗色走红 #4a2026）
  rec('选中底色明暗不同（模式化强调色）',
    !!selBgByTheme.dark && !!selBgByTheme.light && selBgByTheme.dark !== selBgByTheme.light,
    `暗=${selBgByTheme.dark} ｜ 亮=${selBgByTheme.light}`);

  await browser.close();
  console.log(results.join('\n'));
  const fails = results.filter((r) => r.startsWith('FAIL'));
  console.log(`\n合计 ${results.length} 项，失败 ${fails.length}`);
  process.exit(fails.length ? 1 : 0);
})().catch((e) => {
  console.error('ERR', e && e.message);
  process.exit(1);
});
