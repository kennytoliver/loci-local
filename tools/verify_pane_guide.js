#!/usr/bin/env node
/*
 * 闸门：三处主从骨架（记忆 #detail / 会话 #s-guide / 技能 #sk-detail）在任何状态下，
 *       **都必须保持左右两栏 + 详情栏可见**，未选中时详情栏里要有引导卡。
 *
 * 背景：2026-09-24 用户拍板撤销「空态塌单列」。
 *   曾经的做法是 `.split.solo`（详情栏没内容就把两栏塌成单列、列表吃满全宽），
 *   用户明确否定：本项目所有主从页都是"左边列表 + 右边详情"，不存在单列形态；
 *   而且塌单列会让两栏宽度在"选没选中"之间从 1160px 跳到 383px，比空着更晃眼。
 *   现行为：详情栏常驻，未选中时渲染引导卡（`.split-side .guide`，半页文案见 paneGuide()）。
 *
 * 本闸门守的是**反方向**（旧闸门 verify_split_solo.js 守的是"必须塌"，已作废）：
 *   ① 空态   → 两栏、详情栏可见、宽度正常、引导卡显示
 *   ② 有内容 → 两栏、详情栏可见、宽度正常、引导卡收起
 *   ③ 回退   → 任何一处的 .split 都不允许再出现 solo 类；CSS 里不允许再有 .split.solo 规则
 *
 * 用法：node tools/verify_pane_guide.js [http://127.0.0.1:8787]
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

/* 1440 视口下内容区 ≈1160px（左侧导航 220 + 内边距）。
   两栏时左列 clamp(360px,33%,460px) → 383px，右栏吃掉剩下的 ≈757px。
   所以"详情栏正常"的门槛是 >700，而"列表有没有吃满"用 <600 判断。 */
const SIDE_MIN = 700;
const MAIN_MAX = 600;

(async () => {
  const b = await puppeteer.launch({ executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'] });

  const probe = (p) => p.evaluate(() => {
    const one = (sel) => {
      const sp = document.querySelector(sel);
      if (!sp) return null;
      const side = sp.querySelector('.split-side');
      const g = sp.querySelector('.split-side .guide');
      const shown = (e) => (e ? getComputedStyle(e).display !== 'none' : null);
      return {
        solo: sp.classList.contains('solo'),
        sideShown: shown(side),
        sideW: side ? +side.getBoundingClientRect().width.toFixed(1) : null,
        mainW: +sp.querySelector('.split-main').getBoundingClientRect().width.toFixed(1),
        hasGuide: !!g,
        guideShown: shown(g),
      };
    };
    return { session: one('#v-session .split'), mem: one('#v-mem .split'), skill: one('#v-skill .split') };
  });
  const line = (s) => s
    ? `solo=${s.solo} 列表宽=${s.mainW} 详情宽=${s.sideW} 详情显示=${s.sideShown} 引导卡=${s.guideShown}`
    : '取不到';

  for (const theme of ['light', 'dark']) {
    const p = await b.newPage();
    await p.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    await p.goto(`${base}/?theme=${theme}`, { waitUntil: 'networkidle2', timeout: 60000 });
    await sleep(1500);

    // ── ① 会话页：首屏就是空态（默认页 + 不自动选中）
    await p.evaluate(() => window.show('session'));
    await sleep(1400);
    let s = (await probe(p)).session;
    rec(`[${theme}] 会话页·空态 → 两栏 + 引导卡`,
      s && s.solo === false && s.sideShown === true && s.sideW > SIDE_MIN
        && s.mainW < MAIN_MAX && s.guideShown === true,
      s ? line(s) : '取不到');

    const sid = await p.evaluate(async () => {
      const list = await (await fetch('/api/session/list?limit=50')).json();
      if (!list || !list.length) return 0;
      await window.openSession(list[0].id);
      return list[0].id;
    });
    await sleep(1400);
    s = (await probe(p)).session;
    if (!sid) console.log(`  [${theme}] 会话页·库里没会话，②跳过`);
    else rec(`[${theme}] 会话页·选中 #${sid} → 两栏、引导卡收起`,
      s.solo === false && s.sideShown === true && s.mainW < MAIN_MAX && s.sideW > SIDE_MIN
        && s.guideShown === false, line(s));

    // ── ③ 技能页：空态（loadSkills 只写左列，右栏该保持骨架引导卡）
    await p.evaluate(() => window.show('skill'));
    await sleep(1800);
    let k = (await probe(p)).skill;
    rec(`[${theme}] 技能页·空态 → 两栏 + 详情栏可见 + 引导卡`,
      k && k.solo === false && k.sideShown === true && k.sideW > SIDE_MIN
        && k.mainW < MAIN_MAX && k.guideShown === true,
      k ? line(k) : '取不到');

    const sk = await p.evaluate(async () => {
      if (!(window.SKILLS || []).length) return '';
      await window.pickSkill(0);
      return window.SKILLS[0].name;
    });
    await sleep(1800);
    k = (await probe(p)).skill;
    if (!sk) console.log(`  [${theme}] 技能页·本机没技能，④跳过`);
    else rec(`[${theme}] 技能页·选中「${sk}」→ 两栏`,
      k.solo === false && k.sideShown === true && k.mainW < MAIN_MAX && k.sideW > SIDE_MIN, line(k));

    /* ── ⑤ 过渡/失败态也必须撑住：把第一条指到不存在的目录，走真实报错路径。
       历史坑：错误态只写 .dmain>.empty、没有 .dhead，被判成空栏后整栏消失，
       报错文案一个字符都看不见（实测详情栏宽 757px → 0）。 */
    const badRan = await p.evaluate(async () => {
      if (!(window.SKILLS || []).length) return false;
      const keep = window.SKILLS[0].path;
      window.SKILLS[0].path = 'C:/__no_such_skill_dir__';
      await window.pickSkill(0);
      window.SKILLS[0].path = keep;   // 立刻还原，不污染后续用例
      return true;
    });
    await sleep(1200);
    k = (await probe(p)).skill;
    if (!badRan) console.log(`  [${theme}] 技能页·本机没技能，⑤跳过`);
    else rec(`[${theme}] 技能页·报错时详情栏必须可见（文案不能被自己藏掉）`,
      k.solo === false && k.sideShown === true && k.sideW > SIDE_MIN, line(k));

    // ── ⑥ 占位函数 skHold() 的输出必须含 .dhead
    const holdOk = await p.evaluate(() => {
      if (typeof window.skHold !== 'function') return null;
      const d = document.createElement('div');
      d.innerHTML = window.skHold('t', 'm');
      return !!d.querySelector('.dhead');
    });
    if (holdOk === null) rec(`[${theme}] 技能页·占位函数 skHold 必须存在`, false, '未定义');
    else rec(`[${theme}] 技能页·占位函数 skHold 必须带 .dhead`, holdOk === true, holdOk ? '' : '缺 .dhead');

    // ── ⑦ 记忆页：无选中 → 引导卡（不再塌）
    await p.evaluate(() => window.show('mem'));
    await sleep(1500);
    await p.evaluate(() => window.renderDetail(0));   // 库里不会真有 id=0 → 走"没选中"分支
    await sleep(900);
    let m = (await probe(p)).mem;
    rec(`[${theme}] 记忆页·无选中 → 两栏 + 详情栏可见 + 引导卡`,
      m && m.solo === false && m.sideShown === true && m.sideW > SIDE_MIN
        && m.mainW < MAIN_MAX && m.guideShown === true,
      m ? line(m) : '取不到');

    // ── ⑧ 记忆页：默认加载（会自动选中第一条）→ 两栏
    await p.evaluate(() => window.loadList());
    await sleep(1500);
    m = (await probe(p)).mem;
    rec(`[${theme}] 记忆页·默认自动选中 → 两栏`,
      m && m.solo === false && m.sideShown === true && m.mainW < MAIN_MAX && m.sideW > SIDE_MIN,
      m ? line(m) : '取不到');

    // ── ⑨ 不变量：三处骨架任何时刻都不许出现 solo 类
    const all = await probe(p);
    const bad = Object.keys(all).filter((kk) => all[kk] && all[kk].solo);
    rec(`[${theme}] 三处骨架都不允许再出现 solo 类`,
      bad.length === 0, bad.length ? '仍带 solo：' + bad.join(',') : '会话/记忆/技能 均无 solo');

    // ── ⑩ 防回退：CSS 里不该再有 .split.solo 规则
    const soloRule = await p.evaluate(() => {
      let hit = null;
      for (const sheet of document.styleSheets) {
        let rows;
        try { rows = sheet.cssRules; } catch (e) { continue; }   // 跨源表读不了，跳过
        for (const r of rows) {
          if (r.selectorText && r.selectorText.indexOf('.split.solo') >= 0) hit = r.selectorText;
        }
      }
      return hit;
    });
    rec(`[${theme}] 撤销后 CSS 里不应再出现 .split.solo 规则`,
      soloRule === null, soloRule ? '仍有：' + soloRule : '');

    console.log('');
    await p.close();
  }
  await b.close();
  console.log(fail ? `\n❌ 失败 ${fail} 项` : '\n✅ 三处骨架都是两栏 + 引导卡，无 solo 残留');
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error('ERR', e.message); process.exit(1); });
