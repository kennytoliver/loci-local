#!/usr/bin/env node
/*
 * 闸门：三处主从骨架的「空态塌单列」必须双向都对。
 *
 * 背景（用户报的问题 1）：会话页是打开面板的默认页，而且不自动选中第一条，
 *   详情栏空着却照旧占着 2/3 宽的格子 —— 1440px 下右边约 760px 全是空背景，
 *   只有一句灰字，看着像"页面没加载完"。记忆页 / 技能页是同款骨架、同款毛病。
 *
 * 修法：`.split.solo`（两栏塌成单列、`.split-side` 不显示），由 JS 的 splitSolo()
 *   在"详情栏有没有内容"之间切换。**三处共用**，别再各写一份。
 *
 * 本闸门量的是**两个方向都不能坏**：
 *   ① 空态   → solo 在、详情栏不显示、左列表吃满内容区
 *   ② 有内容 → solo 没了、详情栏可见且宽度正常、左列表回到窄列
 * 只测一个方向是不够的 —— 只会塌不会恢复，比原来更糟。
 *
 * 用法：node tools/verify_split_solo.js [http://127.0.0.1:8787]
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

/* 1440 视口下内容区 ≈1160px（左侧导航 220 + 内边距）——
   所以"吃满"是 >1100，不是 >1200（这个错我犯过，别再当成代码 bug 查）。 */
const FULL_MIN = 1100;

(async () => {
  const b = await puppeteer.launch({ executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'] });

  const probe = (p) => p.evaluate(() => {
    const one = (sel) => {
      const sp = document.querySelector(sel);
      if (!sp) return null;
      const side = sp.querySelector('.split-side');
      return {
        solo: sp.classList.contains('solo'),
        sideShown: side ? getComputedStyle(side).display !== 'none' : null,
        sideW: side ? +side.getBoundingClientRect().width.toFixed(1) : null,
        mainW: +sp.querySelector('.split-main').getBoundingClientRect().width.toFixed(1),
      };
    };
    return { session: one('#v-session .split'), mem: one('#v-mem .split'), skill: one('#v-skill .split') };
  });
  const line = (s) => `solo=${s.solo} 列表宽=${s.mainW} 详情宽=${s.sideW} 详情显示=${s.sideShown}`;

  for (const theme of ['light', 'dark']) {
    const p = await b.newPage();
    await p.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    await p.goto(`${base}/?theme=${theme}`, { waitUntil: 'networkidle2', timeout: 60000 });
    await sleep(1500);

    // ── 会话页：首屏必须是空态（默认页 + 不自动选中）
    await p.evaluate(() => window.show('session'));
    await sleep(1400);
    let s = (await probe(p)).session;
    rec(`[${theme}] 会话页·空态 → 塌单列、详情栏不显示`,
      s && s.solo === true && s.sideShown === false && s.mainW > FULL_MIN, s ? line(s) : '取不到');

    const sid = await p.evaluate(async () => {
      const list = await (await fetch('/api/session/list?limit=50')).json();
      if (!list || !list.length) return 0;
      await window.openSession(list[0].id);
      return list[0].id;
    });
    await sleep(1400);
    s = (await probe(p)).session;
    if (!sid) console.log(`  [${theme}] 会话页·库里没会话，②跳过`);
    else rec(`[${theme}] 会话页·选中 #${sid} → 恢复两栏`,
      s.solo === false && s.sideShown === true && s.mainW < 600 && s.sideW > 700, line(s));

    // ── 技能页
    await p.evaluate(() => window.show('skill'));
    await sleep(1800);
    let k = (await probe(p)).skill;
    rec(`[${theme}] 技能页·空态 → 塌单列`,
      k && k.solo === true && k.sideShown === false && k.mainW > FULL_MIN, k ? line(k) : '取不到');

    const sk = await p.evaluate(async () => {
      if (!(window.SKILLS || []).length) return '';
      await window.pickSkill(0);
      return window.SKILLS[0].name;
    });
    await sleep(1800);
    k = (await probe(p)).skill;
    if (!sk) console.log(`  [${theme}] 技能页·本机没技能，④跳过`);
    else rec(`[${theme}] 技能页·选中「${sk}」→ 恢复两栏`,
      k.solo === false && k.sideShown === true && k.mainW < 600 && k.sideW > 700, line(k));

    // ── 记忆页：无选中 → 塌；默认加载（会自动选中第一条）→ 恢复
    await p.evaluate(() => window.show('mem'));
    await sleep(1500);
    await p.evaluate(() => window.renderDetail(0));
    await sleep(900);
    let m = (await probe(p)).mem;
    rec(`[${theme}] 记忆页·无选中 → 塌单列`,
      m && m.solo === true && m.sideShown === false && m.mainW > FULL_MIN, m ? line(m) : '取不到');

    await p.evaluate(() => window.loadList());
    await sleep(1500);
    m = (await probe(p)).mem;
    rec(`[${theme}] 记忆页·默认自动选中 → 恢复两栏`,
      m && m.solo === false && m.sideShown === true && m.mainW < 600 && m.sideW > 700, m ? line(m) : '取不到');

    console.log('');
    await p.close();
  }
  await b.close();
  console.log(fail ? `\n❌ 失败 ${fail} 项` : '\n✅ 三处骨架双向都对');
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error('ERR', e.message); process.exit(1); });
