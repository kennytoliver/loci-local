#!/usr/bin/env node
/*
 * 闸门：把面板里**四套折叠机制**钉住，防止改 UI 时被顺手删掉。
 *
 * 背景（2026-09-24）：用户问「之前我在每页功能中做过的折叠功能，改版后是否仍然保留？」
 *   —— 这个问题不该靠人肉复查代码回答，所以把它变成闸门。四套机制是：
 *     ① 页头「数据与说明」折叠  .pfold + .pmtgl + pmAll()/pmToggle()   （本次新增）
 *     ② 分组折叠               toggleGroup() + .ghead/.gbody.hide
 *     ③ 区域折叠               SEC_FOLD + secApply() + [data-secfold]
 *     ④ 长文折叠               foldAll()/foldOne() + .foldbtn（「展开全文」）
 *
 * 每套都验"存在 + 默认态 + 点了真的会动"三件事 —— 只验"类名还在"是假通过：
 * 元素在但没绑事件、点了没反应的情况，只看 DOM 是发现不了的。
 *
 * 用法：node tools/verify_folds.js [http://127.0.0.1:8787]
 * 依赖：puppeteer-core（开发期；产品本身零依赖）
 */
let puppeteer;
try { puppeteer = require('puppeteer-core'); } catch (e) { console.error('缺 puppeteer-core'); process.exit(2); }
const fs = require('fs');
const CHROME = [process.env.CHROME_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe']
  .filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) { console.error('没找到 Chrome'); process.exit(2); }

const base = process.argv.slice(2).find((a) => !a.startsWith('--')) || 'http://127.0.0.1:8787';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let fail = 0;
const rec = (n, ok, i) => { if (!ok) fail++; console.log((ok ? 'PASS  ' : 'FAIL  ') + n + (i ? '  ' + i : '')); };

const PAGES = ['mem', 'session', 'audit', 'clean', 'collect', 'agents', 'skill', 'pack', 'handoff'];

(async () => {
  const b = await puppeteer.launch({ executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'] });

  for (const theme of ['light', 'dark']) {
    const p = await b.newPage();
    await p.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    await p.goto(`${base}/?theme=${theme}`, { waitUntil: 'networkidle2', timeout: 60000 });
    await sleep(1500);

    /* ── ① 页头「数据与说明」：9 页都要有开合按钮，且默认收起 ── */
    const heads = await p.evaluate((ids) => ids.map((v) => {
      const sec = document.getElementById('v-' + v);
      const tgl = sec && sec.querySelector('.pagehead .pmtgl');
      const box = document.getElementById('pm-' + v);
      return { v,
        tgl: !!tgl,
        aria: tgl ? tgl.getAttribute('aria-expanded') : null,
        h: box ? Math.round(box.getBoundingClientRect().height) : -1,
        n: box ? box.querySelectorAll('.pfin > *').length : 0 };
    }), PAGES);
    const noTgl = heads.filter((h) => !h.tgl).map((h) => h.v);
    const notShut = heads.filter((h) => h.h !== 0 || h.aria !== 'false').map((h) => h.v + '(h=' + h.h + ',aria=' + h.aria + ')');
    rec(`[${theme}] ① 页头折叠：9 页都有开合按钮`, noTgl.length === 0,
      noTgl.length ? '缺：' + noTgl.join(',') : heads.map((h) => h.v + ':' + h.n).join(' '));
    rec(`[${theme}] ① 页头折叠：9 页默认全部收起`, notShut.length === 0,
      notShut.length ? '未收起：' + notShut.join(',') : '折叠区高度均为 0');

    /* ── ② 页头折叠：点开真的会展开，再点真的会收起 ── */
    await p.evaluate(() => window.show('mem'));
    await sleep(900);
    const cycle = await p.evaluate(async () => {
      const tgl = document.querySelector('#v-mem > .pagehead .pmtgl');
      const box = document.getElementById('pm-mem');
      const h = () => Math.round(box.getBoundingClientRect().height);
      const a = h(), aria0 = tgl.getAttribute('aria-expanded');
      tgl.click(); await new Promise((r) => setTimeout(r, 450));
      const bb = h(), aria1 = tgl.getAttribute('aria-expanded');
      tgl.click(); await new Promise((r) => setTimeout(r, 450));
      return { a, bb, c: h(), aria0, aria1, aria2: tgl.getAttribute('aria-expanded') };
    });
    rec(`[${theme}] ② 页头折叠：收起 ${cycle.a}px → 展开 ${cycle.bb}px → 再收起 ${cycle.c}px`,
      cycle.a === 0 && cycle.bb > 20 && cycle.c === 0 && cycle.aria0 === 'false'
        && cycle.aria1 === 'true' && cycle.aria2 === 'false',
      `aria ${cycle.aria0}→${cycle.aria1}→${cycle.aria2}`);

    /* ── ③ 分组折叠（会话页「归档新会话」）：默认收起，点了会开 ── */
    await p.evaluate(() => window.show('session'));
    await sleep(1300);
    const g1 = await p.evaluate(async () => {
      const body = document.getElementById('g-sessarc'), head = document.getElementById('gh-sessarc');
      if (!body || !head) return { err: '缺 gh-sessarc' };
      const shut0 = body.classList.contains('hide');
      const h0 = Math.round(body.getBoundingClientRect().height);
      head.click(); await new Promise((r) => setTimeout(r, 350));
      const h1 = Math.round(body.getBoundingClientRect().height);
      const shut1 = body.classList.contains('hide');
      head.click(); await new Promise((r) => setTimeout(r, 350));
      return { shut0, h0, h1, shut1, shut2: body.classList.contains('hide'), foot: !!document.querySelector('#v-session .grpfoot') };
    });
    rec(`[${theme}] ③ 分组折叠「归档新会话」：默认收起 → 点开 ${g1.h1}px → 再收起`,
      !g1.err && g1.shut0 === true && g1.h0 === 0 && g1.h1 > 20 && g1.shut2 === true,
      g1.err || `收起态高=${g1.h0} 展开态高=${g1.h1}`);

    /* ── ④ 分组折叠（本机内容页「本机来源探测」）：默认收起 ── */
    await p.evaluate(() => window.show('skill'));
    await sleep(2400);
    const g2 = await p.evaluate(async () => {
      const body = document.getElementById('g-skillsrc'), head = document.getElementById('gh-skillsrc');
      if (!body || !head) return { err: '缺 gh-skillsrc' };
      const shut0 = body.classList.contains('hide');
      const h0 = Math.round(body.getBoundingClientRect().height);
      head.click(); await new Promise((r) => setTimeout(r, 350));
      const h1 = Math.round(body.getBoundingClientRect().height);
      head.click(); await new Promise((r) => setTimeout(r, 350));
      return { shut0, h0, h1, shut2: body.classList.contains('hide') };
    });
    rec(`[${theme}] ④ 分组折叠「本机来源探测」：默认收起 → 点开 ${g2.h1}px → 再收起`,
      !g2.err && g2.shut0 === true && g2.h0 === 0 && g2.h1 > 20 && g2.shut2 === true,
      g2.err || `收起态高=${g2.h0} 展开态高=${g2.h1}`);

    /* ── ⑤ 分组折叠（本机内容页左栏技能分组）：只展开第一组 ── */
    const g3 = await p.evaluate(() => {
      const heads = Array.from(document.querySelectorAll('#sk-list .grphead.ghead'));
      const open = heads.filter((h) => {
        const k = h.dataset.k || h.getAttribute('onclick') || h.id;
        return !h.classList.contains('collapsed');
      });
      return { n: heads.length, open: open.length, foot: document.querySelectorAll('#sk-list .grpfoot').length };
    });
    rec(`[${theme}] ⑤ 左栏技能分组折叠：${g3.n} 组，只展开第一组`,
      g3.n >= 2 && g3.open === 1, `展开 ${g3.open} 组，组脚按钮 ${g3.foot} 个`);

    /* ── ⑥ 区域折叠（SEC_FOLD）：三处骨架 6 个头都在，且点了真的折 ── */
    const sec = await p.evaluate(async () => {
      const want = ['#v-mem .split-main .shead', '#v-mem .split-side .dhead',
                    '#v-session .split-main .shead', '#v-session .split-side .dhead',
                    '#v-skill .split-main .shead', '#v-skill .split-side .dhead'];
      const miss = want.filter((s) => !document.querySelector(s + '[data-secfold]'));
      const noRole = want.filter((s) => { const e = document.querySelector(s); return e && !e.hasAttribute('role'); });
      /* 会话页列表头：点一下必须给 .sbody 加上 .hide */
      const head = document.querySelector('#v-session .split-main .shead');
      const body = document.querySelector('#v-session .split-main .sbody');
      const before = body.classList.contains('hide');
      head.click(); await new Promise((r) => setTimeout(r, 250));
      const mid = body.classList.contains('hide');
      head.click(); await new Promise((r) => setTimeout(r, 250));
      return { miss, noRole, before, mid, after: body.classList.contains('hide') };
    });
    rec(`[${theme}] ⑥ 区域折叠：三处骨架 6 个可折头齐全`,
      sec.miss.length === 0, sec.miss.length ? '缺：' + sec.miss.join(' ') : '记忆/会话/技能 × 列表/详情');
    rec(`[${theme}] ⑥ 区域折叠：键盘可达 + 点击真的折/展开`,
      sec.noRole.length === 0 && sec.before === false && sec.mid === true && sec.after === false,
      `role 缺 ${sec.noRole.length} 处；.hide ${sec.before}→${sec.mid}→${sec.after}`);

    /* ── ⑦ 长文折叠（foldAll）：注一段超长正文，必须出现「展开全文」并可切换 ── */
    /* ⚠️ 必须先切回会话页：第 ⑤ 步把页面切到「本机内容」了，而 #v-session 是 display:none，
       在隐藏页里量高度永远是 0 —— 曾因此误报「长文折叠失效」（是闸门自己的锅，不是产品）。 */
    await p.evaluate(() => window.show('session'));
    await sleep(1300);
    const f1 = await p.evaluate(async () => {
      const host = document.querySelector('#v-session .split-side .dmain');
      if (!host) return { err: '取不到 #v-session .dmain' };
      if (typeof window.foldAll !== 'function') return { err: 'foldAll 未定义' };
      const d = document.createElement('div');
      d.className = 'dbody';
      d.textContent = '长文折叠机制自检 '.repeat(400);
      host.appendChild(d);
      window.foldAll();
      const btn = d.nextElementSibling;
      if (!btn || !btn.classList.contains('foldbtn')) { d.remove(); return { err: '没插出 .foldbtn' }; }
      const h0 = Math.round(d.getBoundingClientRect().height);
      const t0 = btn.textContent;
      btn.click();
      const h1 = Math.round(d.getBoundingClientRect().height);
      const t1 = btn.textContent;
      btn.remove(); d.remove();
      return { h0, h1, t0, t1 };
    });
    rec(`[${theme}] ⑦ 长文折叠：「${f1.t0 || '?'}」→「${f1.t1 || '?'}」 ${f1.h0}px → ${f1.h1}px`,
      !f1.err && f1.t0 === '展开全文' && f1.t1 === '收起全文' && f1.h1 > f1.h0 + 50,
      f1.err || '');

    /* ── ⑧ 清理页 4 个 Panel 都能折（wrapPanels：把 .panel 正文包进 .gbody，点标题栏收起） ── */
    await p.evaluate(() => window.show('clean'));
    await sleep(1800);
    const c1 = await p.evaluate(async () => {
      const keys = ['cl-a', 'cl-b', 'cl-c', 'cl-d'];
      const miss = keys.filter((k) => !document.getElementById('gh-' + k)
        || !document.getElementById('g-' + k));
      const h = (k) => Math.round(document.getElementById('g-' + k).getBoundingClientRect().height);
      const opened = keys.map(h);
      const head = document.getElementById('gh-cl-a');
      head.click(); await new Promise((r) => setTimeout(r, 350));
      const shut = h('cl-a');
      const cls = document.getElementById('gh-cl-a').classList.contains('collapsed');
      const aria = document.getElementById('gh-cl-a').getAttribute('aria-expanded');
      head.click(); await new Promise((r) => setTimeout(r, 350));
      return { miss, opened, shut, cls, aria, back: h('cl-a') };
    });
    rec(`[${theme}] ⑧ 清理页 4 个 Panel 都可折（cl-a~cl-d 标题栏）`,
      c1.miss.length === 0 && c1.opened.every((x) => x > 20)
        && c1.shut === 0 && c1.cls === true && c1.aria === 'false' && c1.back > 20,
      c1.miss.length ? '缺 head/body：' + c1.miss.join(',')
        : `展开高 ${c1.opened.join('/')} → 点 cl-a 收起 ${c1.shut} → 复原 ${c1.back}`);

    /* ── ⑨ 防回退：CSS 里必须仍然存在这几条规则（删掉就等于功能没了） ── */
    const css = await p.evaluate(() => {
      const txt = [];
      for (const sheet of document.styleSheets) {
        let rows; try { rows = sheet.cssRules; } catch (e) { continue; }
        for (const r of rows) if (r.selectorText) txt.push(r.selectorText);
      }
      const all = txt.join(' | ');
      return {
        pfold: all.indexOf('.pfold') >= 0,
        gbody: all.indexOf('.gbody.hide') >= 0,
        secbody: all.indexOf('.sbody.hide') >= 0 && all.indexOf('.dmain.hide') >= 0,
        foldbody: all.indexOf('.foldbody.folded') >= 0,
      };
    });
    rec(`[${theme}] ⑨ 防回退：四套折叠的 CSS 规则都还在`,
      css.pfold && css.gbody && css.secbody && css.foldbody, JSON.stringify(css));

    console.log('');
    await p.close();
  }
  await b.close();
  console.log(fail ? `\n❌ 失败 ${fail} 项` : '\n✅ 四套折叠机制（页头 / 分组 / 区域 / 长文）都在且可用');
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error('ERR', e.message); process.exit(1); });
