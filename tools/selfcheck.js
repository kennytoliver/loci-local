/* 功能自检：逐页在真实浏览器里跑一遍核心功能，输出 PASS/FAIL 清单。
   用法：node tools/selfcheck.js [base] */
const fs = require('fs');
let puppeteer;
try { puppeteer = require('puppeteer-core'); }
catch (e) { console.error('缺少 puppeteer-core，请设置 NODE_PATH。'); process.exit(2); }
const CHROME = [process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe'].filter(Boolean).find((p) => fs.existsSync(p));

const OUT = [];
let fails = 0;
function ck(name, cond, extra) {
  const good = !!cond;
  if (!good) fails++;
  console.log((good ? '  ✓ ' : '  ✗ ') + name + (extra ? ' — ' + extra : ''));
  OUT.push({ name, good, extra });
}

(async () => {
  const BASE = (process.argv[2] || 'http://127.0.0.1:8787').replace(/\/$/, '');
  const browser = await puppeteer.launch({ headless: true, executablePath: CHROME, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 1100 });
  const errs = [];
  page.on('pageerror', (e) => errs.push(e.message));
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  await page.goto(BASE + '/', { waitUntil: 'networkidle2' });
  await wait(1500);

  // ---------- 会话页 ----------
  console.log('\n[会话页]');
  await page.evaluate(() => window.show('session'));
  await wait(2000);
  const sess = await page.evaluate(() => ({
    rows: document.querySelectorAll('#s-list .mem').length,
    hasQ: !!document.getElementById('s-q'),
    hasBtn: !!document.querySelector('.fgroup button'),
    headFold: !!document.querySelector('#v-session .split-side .dhead[data-secfold]'),
    listFold: !!document.querySelector('#v-session .split-main .shead[data-secfold]'),
  }));
  ck('会话列表有数据', sess.rows > 0, sess.rows + ' 条');
  ck('搜索框 + 检索按钮都在同一组（.fgroup）', sess.hasQ && sess.hasBtn);
  ck('左栏「会话列表」可折叠', sess.listFold);
  ck('右栏「原文时间线」可折叠', sess.headFold);
  const ordS = await page.evaluate(() => {
    const s = document.querySelector('#v-session > .split');
    const p = document.querySelector('#v-session > .panel');
    return { sy: s ? Math.round(s.getBoundingClientRect().top) : -1,
             py: p ? Math.round(p.getBoundingClientRect().top) : -1,
             folded: !!document.querySelector('#v-session > .panel .gbody.hide') };
  });
  ck('会话页：已归档会话排在「归档新会话」之前',
     ordS.sy >= 0 && ordS.sy < ordS.py, 'split@' + ordS.sy + ' → panel@' + ordS.py);
  ck('「归档新会话」默认收起', ordS.folded);
  // 打开第一个会话
  await page.evaluate(() => {
    const el = document.querySelector('#s-list .mem[onclick*="openSession"]');
    if (el) el.click();
  });
  await wait(1800);
  const sv = await page.evaluate(() => ({
    bubs: document.querySelectorAll('#s-view .bub').length,
    folded: document.querySelectorAll('#s-view .bub.folded').length,
    btns: document.querySelectorAll('#s-view .foldbtn').length,
  }));
  ck('点开会话能看到原文（气泡）', sv.bubs > 0, sv.bubs + ' 个气泡');
  ck('长原文被折叠（有折叠按钮）', sv.btns > 0 && sv.folded === sv.btns, '折叠 ' + sv.folded + ' 条');

  // ---------- 记忆页 ----------
  console.log('\n[记忆页]');
  await page.evaluate(() => window.show('mem'));
  await wait(2200);
  const mem = await page.evaluate(() => ({
    cards: document.querySelectorAll('#list .mem').length,
    groups: document.querySelectorAll('#list .ghead').length,
    detail: !!document.querySelector('#detail .dhead'),
  }));
  ck('记忆列表有数据', mem.cards > 0, mem.cards + ' 条');
  ck('分组头（常驻/最近）可折叠', mem.groups >= 2, mem.groups + ' 个分组');
  ck('右栏记忆详情有渲染', mem.detail);

  // ---------- 本机内容页 ----------
  console.log('\n[本机内容页]');
  await page.evaluate(() => window.show('skill'));
  await wait(3200);
  const sk = await page.evaluate(() => {
    const heads = document.querySelectorAll('#sk-list .ghead');
    const bodies = document.querySelectorAll('#sk-list .gbody');
    const t = Array.from(document.querySelectorAll('#sk-list .ghead .gt')).map((e) => e.textContent);
    const gns = Array.from(document.querySelectorAll('#sk-list .grphead .gn')).map((e) => e.textContent);
    return {
      heads: heads.length, bodies: bodies.length, titles: t, gns,
      count: (document.getElementById('sk-count') || {}).textContent,
      srcnoteHidden: (() => { const s = document.querySelector('.srcnote'); return !s || getComputedStyle(s).display === 'none'; })(),
      firstCard: !!document.querySelector('#sk-list .mem'),
    };
  });
  ck('左栏分成 5 组', sk.heads === 5 && sk.bodies === 5, sk.titles.join('/'));
  ck('每组数量都写了', sk.gns.length === 5 && sk.gns.every((x) => /\d+\s*个/.test(x)), sk.gns.join(' | '));
  ck('标题栏长小字已去掉', !sk.count, JSON.stringify(sk.count));
  ck('「另有 N 个…」来源备注不显示', sk.srcnoteHidden);
  ck('左栏有内容条目', sk.firstCard);
  // 折叠第一组
  const fold = await page.evaluate(() => {
    const h = document.querySelector('#sk-list .ghead'); const b = h.nextElementSibling;
    const hh = () => Math.round(b.getBoundingClientRect().height);
    const before = hh(); h.click(); const after = hh(); const hid = b.classList.contains('hide'); h.click();
    return { before, after, back: hh(), hid };
  });
  ck('分组能整块收起且布局跟随', fold.hid && fold.after === 0 && fold.back === fold.before,
     fold.before + '→' + fold.after + '→' + fold.back + 'px');
  const grp2 = await page.evaluate(() => {
    const h = document.querySelector('#sk-list .ghead');
    if (!h) return null;
    const f = h.parentNode.querySelector('.grpfoot');
    const border = getComputedStyle(h).borderTopWidth;
    if (!f) return { border, t0: '(无按钮)' };
    const b = h.nextElementSibling;
    const t0 = f.textContent; f.click();
    const hid = b.classList.contains('hide'); const t1 = f.textContent; f.click();
    return { border, t0, t1, t2: f.textContent, hid };
  });
  const ordK = await page.evaluate(() => {
    const s = document.querySelector('#v-skill > .split');
    const p = document.querySelector('#v-skill > .panel');
    return { sy: s ? Math.round(s.getBoundingClientRect().top) : -1,
             py: p ? Math.round(p.getBoundingClientRect().top) : -1,
             folded: !!document.querySelector('#v-skill > .panel .gbody.hide') };
  });
  ck('本机内容：清算结果排在「来源探测」之前',
     ordK.sy >= 0 && ordK.sy < ordK.py, 'split@' + ordK.sy + ' → panel@' + ordK.py);
  ck('「本机来源探测」默认收起', ordK.folded);
  ck('分组是一个带边框的框', grp2 && grp2.border !== '0px', grp2 ? grp2.border : '无');
  ck('分组内容下方有「收起」按钮', grp2 && /收起/.test(grp2.t0 || ''), grp2 ? grp2.t0 : '无');
  ck('点下方按钮能收起且文字翻转',
     grp2 && grp2.hid && /展开/.test(grp2.t1 || '') && /收起/.test(grp2.t2 || ''),
     grp2 ? (grp2.t0 + ' → ' + grp2.t1 + ' → ' + grp2.t2) : '');
  // 点第一个技能看详情
  await page.evaluate(() => {
    const el = document.querySelector('#sk-list .mem[onclick*="pickSkill"]');
    if (el) el.click();
  });
  await wait(2000);
  const det = await page.evaluate(() => {
    const t = document.getElementById('sk-text');
    const cs = t ? getComputedStyle(t) : null;
    return {
      hasTitle: !!Array.from(document.querySelectorAll('#sk-detail .dsec-t')).find((e) => e.textContent.includes('正文内容')),
      len: t ? t.textContent.length : 0,
      border: cs ? cs.borderTopWidth : '0',
      h: t ? Math.round(t.getBoundingClientRect().height) : 0,
    };
  });
  ck('详情里有「正文内容」区块', det.hasTitle);
  ck('正文有实际内容', det.len > 0, det.len + ' 字');
  ck('正文有边框（像一块内容）', det.border !== '0px' && det.border !== '0', det.border);
  ck('正文没有被内层高度限制压扁', det.h > 60, det.h + 'px');

  // ---------- 采集页 ----------
  console.log('\n[采集页]');
  await page.evaluate(() => window.show('collect'));
  await wait(1200);
  await page.evaluate(() => { if (typeof doScan === 'function') doScan(); });
  await page.waitForSelector('#v-collect .stable', { timeout: 25000 }).catch(() => {});
  await wait(600);
  const col = await page.evaluate(() => {
    const st = document.querySelector('#v-collect .stable');
    if (!st) return null;
    const items = Array.from(st.querySelectorAll('.shead-row>span, .srow>span'));
    const noClip = items.filter((el) => {
      const o = getComputedStyle(el).overflow;
      return o !== 'hidden' && o !== 'clip';
    }).length;
    return { border: getComputedStyle(st).borderTopWidth,
             rows: st.querySelectorAll('.srow').length, noClip };
  });
  if (col) {
    ck('采集表有外框', col.border !== '0px', col.border);
    ck('采集表单元格都设了裁切（不会叠字）', col.noClip === 0, '未裁切 ' + col.noClip + ' 格');
    ck('采集表有数据行', col.rows > 0, col.rows + ' 行');
  } else {
    ck('采集表能渲染', false, '没有 .stable（可能扫描没跑完）');
  }

  // ---------- 其它页 ----------
  console.log('\n[其它页]');
  for (const [v, sel] of [['collect', '#v-collect'], ['clean', '#v-clean'], ['pack', '#v-pack'], ['handoff', '#v-handoff']]) {
    await page.evaluate((n) => window.show(n), v);
    await wait(1800);
    const okp = await page.evaluate((s) => {
      const el = document.querySelector(s);
      return el && !el.hidden;
    }, sel);
    ck(v + ' 页能打开', okp);
  }

  console.log('\n[总体]');
  ck('全程无 JS 报错', errs.length === 0, errs.slice(0, 3).join(' | '));
  console.log(`\n自检结果：${OUT.length - fails}/${OUT.length} 项通过${fails ? '，' + fails + ' 项失败' : ' ✅'}`);
  await browser.close();
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.error('自检脚本自身报错:', e.message); process.exit(2); });
