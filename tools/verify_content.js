#!/usr/bin/env node
/*
 * 闸门：本机内容页（技能 / 配置文件 / MCP / 插件 / 历史版本）必须
 * ①五类都在、②密钥一个都不许漏。
 *
 * 钉的是这几个东西，以及几个**真被修掉的 bug**：
 *   ① 面板上每个"打开文件夹"按钮都是死的 —— 前端走 POST /api/open-folder，
 *      后端只有 GET 分支，而那个分支还引用了 do_GET 里不存在的 `body`
 *      （Python 一执行就 NameError）。实测过：POST 一直回 {"error":"not found"}。
 *   ② 敏感文件（~/.zcode/v2/credentials.json）里有明文 access_token 和 api-key，
 *      面板必须有"只报键名、不读值"的能力。
 *   ③ 备份同样要脱敏 —— models.json.bak-* 里是**明文 API Key**。
 *      给了"看历史版本"却不给脱敏，等于开了个看原文的后门。
 *   ④ 不记路径的插件（ZCode / Trae 只存启用开关）不许渲染"打开所在文件夹"，
 *      否则又造出一对点不动的死按钮（就是 bug ① 同一类毛病）。
 *
 * 反向测试（必须做，否则又是一盏不亮的灯）：
 *   node tools/verify_content.js --broken
 *   模拟回归：
 *     (a) 把关**真凭据原文 + 真 apiKey 原文**塞进详情区（假装"脱敏被拿掉了"）
 *         → 两条泄漏断言都必须判红；
 *     (b) 清空 CFGS / PLUGINS / BACKUPS（假装"采集器被删了"）→ 类别断言判红。
 *   两种都靠"真值比对"而不是"看有没有遮挡符"，所以这盏灯是亮的。
 *
 * 用法：node tools/verify_content.js [base] [--broken]
 */
const fs = require('fs');
const os = require('os');

let puppeteer;
try { puppeteer = require('puppeteer-core'); }
catch (e) { console.error('[闸门] 缺少 puppeteer-core'); process.exit(2); }

const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  process.env.LOCALAPPDATA + '/Google/Chrome/Application/chrome.exe',
].find((p) => p && fs.existsSync(p));
if (!CHROME) { console.error('[闸门] 没找到 Chrome（可设 CHROME_PATH）'); process.exit(2); }

const BASE = process.argv[2] || 'http://127.0.0.1:8787';
const BROKEN = process.argv.includes('--broken');
// 家目录别只认 USERPROFILE：Git Bash 里它经常没导出去，
// 那样真值读不到 → 泄漏比对无从比对（闸门会判红并说明"比对无意义"，
// 而不是假装绿灯 —— 这是故意的）。
const HOME = os.homedir() || process.env.USERPROFILE || process.env.HOME || '';
const CRED = (HOME + '/.zcode/v2/credentials.json').replace(/\\/g, '/');

let pass = 0, fail = 0;
const ok = (name, cond, extra) => {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (extra ? ' — ' + extra : '')); }
};

// 明文泄漏探针：取真凭据值的 8 个字符，去 DOM 里找。
// 真值只在本地读、只用于比对，从不打印、不写进仓库。
// 踩过两个坑：
//   ① 字符集写窄了（漏 +/=）→ 匹配 0 个，比对等于没做；
//   ② ZCode 的凭据值是 `enc:v1:<密文>` 这种形式，**所有值前 7 位都一样**，
//      取"前 8 位"做样本会让 6 个值退化成同一个样本 → 改取中段（跳过统一前缀）。
function secretPrefixes(raw) {
  const vals = [];
  for (const m of String(raw).matchAll(/:\s*"([^"]{16,})"/g)) vals.push(m[1].slice(8, 16));
  return [...new Set(vals)].filter((v) => v.length === 8);
}
let CRED_RAW = '';
try { CRED_RAW = fs.readFileSync(CRED, 'utf8'); }
catch (e) { console.log('  （读不到真值样本 ' + CRED + '：' + e.code + '）'); }

// 第二组真值样本：models.json 的备份里是**明文 apiKey**（长度 35、以 sk- 开头）。
// 取中段 8 字符（跳过所有 key 共有的 sk- 前缀）。同样只用于比对，不打印。
const WORKBUDDY = (HOME + '/.workbuddy').replace(/\\/g, '/');
let MODELS_BAK = '';
try {
  MODELS_BAK = (fs.readdirSync(WORKBUDDY) || [])
    .filter((n) => n.indexOf('models.json.bak-') === 0).sort().pop() || '';
  if (MODELS_BAK) MODELS_BAK = WORKBUDDY + '/' + MODELS_BAK;
} catch (e) { MODELS_BAK = ''; }
let MODELS_RAW = '';
try { if (MODELS_BAK) MODELS_RAW = fs.readFileSync(MODELS_BAK, 'utf8'); }
catch (e) { console.log('  （读不到真值样本 ' + MODELS_BAK + '：' + e.code + '）'); }
function apiKeySamples(raw) {
  const out = [];
  for (const m of String(raw).matchAll(/"apiKey"\s*:\s*"([^"]{12,})"/g)) out.push(m[1].slice(5, 13));
  return [...new Set(out)].filter((v) => v.length === 8);
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });

  const errs = [];
  page.on('pageerror', (e) => errs.push(String(e)));

  await page.goto(BASE, { waitUntil: 'networkidle2' });
  await page.waitForFunction(() => typeof window.show === 'function', { timeout: 15000 });
  await page.evaluate(() => window.show('skill'));
  await page.waitForFunction(
    () => typeof CFGS !== 'undefined' && typeof MCPS !== 'undefined' &&
          document.querySelectorAll('#sk-list .mem').length > 0, { timeout: 25000 });

  if (BROKEN) {
    await page.evaluate((txt) => {
      // (a) 假装脱敏被拿掉：把真凭据原文 + 真 apiKey 原文一起塞进详情区
      document.getElementById('sk-detail').innerHTML =
        '<div class="dhead"></div>' +
        '<div class="dmain"><div class="handoff-out" id="sk-text" style="display:block"></div></div>' +
        '<div id="cfg-note" class="hint">已脱敏</div>';
      document.getElementById('sk-text').textContent = txt;
      // (b) 假装采集器被删了
      CFGS.length = 0;
      PLUGINS.length = 0;
      BACKUPS.length = 0;
      // (c) 假装"页面级 .content 的 padding 又漏回列表项里了"
      const s = document.createElement('style');
      s.textContent =
        '#sk-list .mem[onclick*="pickPlugin"] .content' +
        '{padding:20px 20px 48px!important;overflow-y:auto!important}';
      document.head.appendChild(s);
    }, CRED_RAW + '\n' + MODELS_RAW);
  }

  const shape = await page.evaluate(() => ({
    cfgs: CFGS.length, mcps: MCPS.length, skills: SKILLS.length, csrc: CSRC.length,
    plugins: PLUGINS.length, backups: BACKUPS.length,
    oldver: PLUGINS.reduce((n, p) => n + (p.old_count || 0), 0),
    withver: PLUGINS.filter((p) => p.version_count > 0).length,
    secrets: CFGS.filter((c) => c.secret).length,
    groups: [...document.querySelectorAll('#sk-list .grphead .gt')].map((e) => e.textContent),
    count: (document.getElementById('sk-count') || {}).textContent || '',
  }));

  console.log('  实测：技能 ' + shape.skills + ' ｜ 配置文件 ' + shape.cfgs + '（敏感 ' +
              shape.secrets + '）｜ MCP ' + shape.mcps + ' ｜ 插件 ' + shape.plugins +
              '（' + shape.withver + ' 个有本地版本，旧版本合计 ' + shape.oldver + '）｜ 备份 ' +
              shape.backups + ' ｜ 来源行 ' + shape.csrc);
  console.log('  左列分组：' + JSON.stringify(shape.groups));

  ok('左列分五段：技能 / 配置文件 / MCP / 插件 / 历史版本',
     shape.groups.join('|') === '技能|配置文件|MCP|插件|历史版本', JSON.stringify(shape.groups));
  ok('配到了配置文件（至少 1 个）', shape.cfgs > 0, '只有 ' + shape.cfgs + ' 个');
  ok('配到了 MCP（至少 1 条）', shape.mcps > 0, '只有 ' + shape.mcps + ' 条');
  ok('配到了已装插件（至少 1 个）', shape.plugins > 0, '只有 ' + shape.plugins + ' 个');
  ok('配到了配置备份（至少 1 份）', shape.backups > 0, '只有 ' + shape.backups + ' 份');
  ok('来源表把没内容的产品也报出来了（≥4 行）', shape.csrc >= 4, '只有 ' + shape.csrc + ' 行');
  ok('抬头计数五类都写了',
     /配置文件/.test(shape.count) && /MCP/.test(shape.count) &&
     /插件/.test(shape.count) && /本地历史版本/.test(shape.count),
     '实际：' + shape.count);
  ok('技能排在列表最前（技能是唯一能"传过去"的类别）',
     (shape.groups[0] || '') === '技能', shape.groups[0]);

  // ── 打开敏感文件，验"只报键名、不读值" ──────────────────────────────
  const idx = await page.evaluate(() => CFGS.findIndex((c) => c.secret));
  ok('本机存在被判定为敏感的文件（credentials 之类）', idx >= 0);
  if (idx >= 0) {
    await page.evaluate((i) => pickCfg(i), idx);
    await page.waitForFunction(
      () => document.querySelector('#sk-detail .dhead'), { timeout: 15000 });
    await new Promise((r) => setTimeout(r, 500));
    const sec = await page.evaluate(() => ({
      text: (document.getElementById('sk-text') || {}).textContent || '',
      hasDhead: !!document.querySelector('#sk-detail .dhead'),
      hasDmain: !!document.querySelector('#sk-detail .dmain'),
      note: (document.getElementById('cfg-note') || {}).textContent || '',
    }));
    ok('敏感文件详情用了会话页骨架（.dhead/.dmain）', sec.hasDhead && sec.hasDmain);
    ok('敏感文件明确说了"不读值"', /不读|隐去/.test(sec.note), '提示是：' + sec.note);
    ok('敏感文件仍告诉用户有哪些键名（不是一片空白）',
       /·/.test(sec.text), '正文：' + sec.text.slice(0, 80));
  }

  // ── 明文泄漏比对：**两种模式都跑** ─────────────────────────────────
  const domText = await page.evaluate(
    () => (document.getElementById('sk-text') || {}).textContent || '');
  const prefixes = secretPrefixes(CRED_RAW);
  const hits = prefixes.map((v, i) => (domText.includes(v) ? i : -1)).filter((i) => i >= 0);
  ok('详情正文里没有明文凭据片段（拿真值中段比对，样本 ' + prefixes.length + ' 个）',
     hits.length === 0 && prefixes.length > 0,
     prefixes.length === 0
       ? '读不到真值，比对无意义'
       // 只报命中的**序号**，绝不回显命中的片段本身 ——
       // 闸门日志会进终端、进记忆、进对话，真值不能跟着走
       : ('漏了 ' + hits.length + ' 个片段（样本序号 ' + hits.join(',') + '）'));

  // ── 普通配置文件：正文应在，提示里说明已脱敏，且**不许有内层滚动条** ──
  const ni = await page.evaluate(() => CFGS.findIndex((c) => !c.secret));
  if (ni >= 0) {
    await page.evaluate((i) => pickCfg(i), ni);
    await new Promise((r) => setTimeout(r, 700));
    const plain = await page.evaluate(() => {
      const t = document.getElementById('sk-text');
      const ct = getComputedStyle(t);
      return {
        text: t.textContent || '',
        note: (document.getElementById('cfg-note') || {}).textContent || '',
        maxHeight: ct.maxHeight,
        overflowY: ct.overflowY,
        innerScrollable: t.scrollHeight > t.clientHeight + 2,
      };
    });
    ok('普通配置文件能读到正文', plain.text.length > 20, '只有 ' + plain.text.length + ' 字');
    ok('普通配置文件也走脱敏（提示里有"脱敏"/"隐去"）',
       /脱敏|隐去/.test(plain.note), '提示是：' + plain.note);
    // 这条防的是用户报过的那个严重 bug：详情套了内层 max-height + overflow:auto，
    // 正文被压进一个小窗，外层还有一层滚动条 → 永远只看得见一小条
    ok('配置文件详情没有内层高度限制', plain.maxHeight === 'none',
       'maxHeight=' + plain.maxHeight);
    ok('配置文件详情没有内层滚动条', plain.innerScrollable === false &&
       plain.overflowY !== 'auto' && plain.overflowY !== 'scroll',
       'overflowY=' + plain.overflowY + ' 内层可滚=' + plain.innerScrollable);
  } else {
    ok('普通配置文件能读到正文', false, '配置采集器里一个非敏感文件都没有');
    ok('普通配置文件也走脱敏', false, '同上');
    ok('配置文件详情没有内层高度限制', false, '没有非敏感文件可测');
    ok('配置文件详情没有内层滚动条', false, '同上');
  }

  // ── MCP 详情 ───────────────────────────────────────────────────────
  if (shape.mcps > 0) {
    await page.evaluate(() => pickMcp(0));
    await new Promise((r) => setTimeout(r, 300));
    const m = await page.evaluate(() => ({
      text: (document.getElementById('sk-text') || {}).textContent || '',
      hasDhead: !!document.querySelector('#sk-detail .dhead'),
    }));
    ok('MCP 详情能渲染且用会话页骨架', m.hasDhead && m.text.length > 20,
       '正文 ' + m.text.length + ' 字');
    ok('MCP 详情只列环境变量名、不列值（env 里经常就是 API Key）',
       /环境变量/.test(m.text) && /只列名字|不读/.test(m.text), m.text.slice(0, 120));
  }

  // ── 插件：本地版本历史（竞品放云端的那块，我们做成就地清算） ────────
  const rowCounts = await page.evaluate(() => ({
    plug: document.querySelectorAll('#sk-list .mem[onclick*="pickPlugin"]').length,
    back: document.querySelectorAll('#sk-list .mem[onclick*="pickBackupRow"]').length,
  }));
  ok('左列把每个插件都列出来了', rowCounts.plug === shape.plugins,
     '列表 ' + rowCounts.plug + ' 行 / 数据 ' + shape.plugins + ' 个');
  ok('左列把每份配置备份都列出来了', rowCounts.back === shape.backups,
     '列表 ' + rowCounts.back + ' 行 / 数据 ' + shape.backups + ' 份');
  // 一个东西在左列出现两次，用户会以为"这是两份不同的东西" ——
  // 所以历史版本段只放配置备份，插件版本挂在插件自己的详情里。
  ok('插件不在「历史版本」里重复出现（版本挂在插件详情里看）',
     rowCounts.plug === shape.plugins, '插件行 ' + rowCounts.plug + ' 行');

  const pi = await page.evaluate(() => PLUGINS.findIndex((p) => p.version_count > 0));
  ok('本机有插件带着本地版本目录可看', pi >= 0, '一个都没有');
  if (pi >= 0) {
    await page.evaluate((i) => pickPlugin(i), pi);
    await page.waitForFunction(
      () => !!document.querySelector('#sk-detail .dhead'), { timeout: 15000 });
    await new Promise((r) => setTimeout(r, 500));
    const pv = await page.evaluate(() => {
      const t = document.getElementById('sk-text');
      const ct = getComputedStyle(t);
      return {
        text: t.textContent || '',
        hasDhead: !!document.querySelector('#sk-detail .dhead'),
        hasDmain: !!document.querySelector('#sk-detail .dmain'),
        maxHeight: ct.maxHeight, overflowY: ct.overflowY,
        innerScrollable: t.scrollHeight > t.clientHeight + 2,
      };
    });
    ok('插件详情用会话页骨架（.dhead/.dmain）', pv.hasDhead && pv.hasDmain);
    ok('插件详情列出了本地版本', /本地版本/.test(pv.text), pv.text.slice(0, 120));
    ok('插件详情标出了"当前"版本（能分清哪个在用、哪些是旧的）',
       /当前/.test(pv.text), pv.text.slice(0, 200));
    ok('插件详情没有内层高度限制', pv.maxHeight === 'none', 'maxHeight=' + pv.maxHeight);
    ok('插件详情没有内层滚动条', pv.innerScrollable === false &&
       pv.overflowY !== 'auto' && pv.overflowY !== 'scroll', 'overflowY=' + pv.overflowY);
  }

  // 不记路径的插件（ZCode / Trae 的黑名单式清单）不许画出点不动的按钮 ——
  // 这就是 bug ① 的同一类毛病，换个地方长出来。
  const np = await page.evaluate(() => PLUGINS.findIndex((p) => !p.path));
  if (np >= 0) {
    await page.evaluate((i) => pickPlugin(i), np);
    await new Promise((r) => setTimeout(r, 500));
    const npv = await page.evaluate(() => ({
      acts: [...document.querySelectorAll('#sk-detail .dacts button')].map((b) => b.textContent),
      actsHtml: (document.querySelector('#sk-detail .dacts') || {}).innerHTML || '',
      text: (document.getElementById('sk-text') || {}).textContent || '',
    }));
    ok('不记路径的插件不渲染"打开所在文件夹"（别造死按钮）',
       !npv.acts.some((t) => /打开所在文件夹/.test(t)), JSON.stringify(npv.acts));
    ok('不记路径的插件说明了为什么没有版本（不是一片空白）',
       npv.text.length > 40, '只有 ' + npv.text.length + ' 字');
  }

  // ── 历史版本：配置备份**必须走同一套脱敏** ─────────────────────────
  const mi = await page.evaluate(() => BACKUPS.findIndex((b) => /^models\.json\.bak-/.test(b.name)));
  ok('本机存在 models.json 的备份（里面的 apiKey 是明文）', mi >= 0,
     '备份有 ' + shape.backups + ' 份，没有 models.json 的');
  if (mi >= 0) {
    await page.evaluate((i) => pickBackupRow(i), mi);
    await page.waitForFunction(
      () => !!document.querySelector('#sk-detail .dhead'), { timeout: 15000 });
    await new Promise((r) => setTimeout(r, 600));
    const bd = await page.evaluate(() => {
      const t = document.getElementById('sk-text');
      const ct = getComputedStyle(t);
      return {
        text: t.textContent || '',
        note: (document.getElementById('cfg-note') || {}).textContent || '',
        hasDhead: !!document.querySelector('#sk-detail .dhead'),
        hasDmain: !!document.querySelector('#sk-detail .dmain'),
        maxHeight: ct.maxHeight, overflowY: ct.overflowY,
        innerScrollable: t.scrollHeight > t.clientHeight + 2,
      };
    });
    ok('备份详情用会话页骨架（.dhead/.dmain）', bd.hasDhead && bd.hasDmain);
    ok('备份详情说了"和当前配置同一套脱敏"', /脱敏|隐去/.test(bd.note), '提示是：' + bd.note);
    ok('备份详情把 apiKey 隐去了（看得出字段名、看不出值）',
       /"apiKey"\s*:\s*"[^"]*已隐去/.test(bd.text), bd.text.slice(0, 160));
    ok('备份详情没有内层高度限制', bd.maxHeight === 'none', 'maxHeight=' + bd.maxHeight);
    ok('备份详情没有内层滚动条', bd.innerScrollable === false &&
       bd.overflowY !== 'auto' && bd.overflowY !== 'scroll', 'overflowY=' + bd.overflowY);
  }

  // 备份里的明文密钥泄漏比对 —— 和凭据那条同理（真值中段比对）。
  // 反向测试时把真 apiKey 原文塞回详情区，这条断言必须判红；
  // 上一节里 #sk-text 已经被 MCP 详情覆盖过，所以比对**读当下的 DOM**、
  // 反向测试要**再注一次**，否则这条断言永远绿（一盏不亮的灯）。
  if (BROKEN) {
    await page.evaluate((txt) => {
      document.getElementById('sk-detail').innerHTML =
        '<div class="dhead"></div>' +
        '<div class="dmain"><div class="handoff-out" id="sk-text" style="display:block"></div></div>' +
        '<div id="cfg-note" class="hint">已脱敏</div>';
      document.getElementById('sk-text').textContent = txt;
    }, MODELS_RAW);
  }
  const ksamples = apiKeySamples(MODELS_RAW);
  const liveBak = await page.evaluate(
    () => (document.getElementById('sk-text') || {}).textContent || '');
  const khits = ksamples.map((v, i) => (liveBak.includes(v) ? i : -1)).filter((i) => i >= 0);
  ok('备份正文里没有明文密钥片段（拿真值中段比对，样本 ' + ksamples.length + ' 个）',
     khits.length === 0 && ksamples.length > 0,
     ksamples.length === 0
       ? '读不到真值，比对无意义'
       : ('漏了 ' + khits.length + ' 个片段（样本序号 ' + khits.join(',') + '）'));

  // ── 列表项不许被"页面级 .content 规则"顺带撑高 ──────────────────────
  // 真 bug：页面正文容器也叫 `.content`（flex:1 + padding:20px 20px 48px），
  // 这条全局规则**顺带命中**列表项内部的 `.content`，把每一项虚增 68px。
  // 实测证据：插件行内容 2 行 = 49px，实高却是 117px，卡片下面空一大截。
  // 两边单独看都正常，所以只能量出来。--broken 会把这条规则注回去，必须判红。
  const leak = await page.evaluate(() => {
    // 反向测试注的是 `!important`，所以要读**计算值**（getComputedStyle），
    // 不是读样式表文本 —— 读文本的话这条断言永远绿。
    // 钉**插件行**而不是"第一行"：第一行是技能行，技能卡本来就有描述、本来就高，
    // 拿它当基线量不出这个 bug（实测技能行 206px，插件行才是 108px 的基准）。
    const c = document.querySelector('#sk-list .mem[onclick*="pickPlugin"] .content');
    if (!c) return null;
    const cs = getComputedStyle(c);
    return {
      pad: cs.padding, overflowY: cs.overflowY,
      inner: c.scrollHeight > c.clientHeight + 2,
      h: +c.getBoundingClientRect().height.toFixed(1),
      rowH: +c.parentElement.getBoundingClientRect().height.toFixed(1),
    };
  });
  ok('本机内容列表项没被页面级 .content 规则撑高（padding 应为 0）',
     !!leak && leak.pad === '0px',
     leak ? 'padding=' + leak.pad + ' 行高=' + leak.rowH : '没找到列表项');
  ok('本机内容列表项内部没有滚动条',
     !!leak && leak.inner === false && leak.overflowY !== 'auto' && leak.overflowY !== 'scroll',
     leak ? 'overflowY=' + leak.overflowY + ' 内层可滚=' + leak.inner : '没找到列表项');
  // 本机内容列表项行高回到正常量级（<130px，修前 176）
  ok('本机内容列表项行高回到正常量级（<130px，修前 176）',
     !!leak && leak.rowH < 130, leak ? '行高=' + leak.rowH : '没找到列表项');

  // 标签行（.top）不许被长版本号挤到换行。
  // WorkBuddy 的版本串是 `5.5.6-wb.38337834.g5f969292.h7826dc9400fd`（44 字符），
  // 全量塞进列表会让 top 从单行变两行、卡片又高回去 —— 列表只放主版本号，
  // 完整值进 title/详情（规范第 5 条）。
  const topRow = await page.evaluate(() => {
    const r = document.querySelector('#sk-list .mem[onclick*="pickPlugin"]');
    if (!r) return null;
    const t = r.querySelector('.top');
    const s = r.querySelector('.score');
    return {
      topH: +t.getBoundingClientRect().height.toFixed(1),
      label: (s || {}).textContent || '',
      title: (s || {}).title || '',
    };
  });
  ok('插件行的标签行没被长版本号挤到换行（top 应为单行 ≤30px）',
     !!topRow && topRow.topH <= 30, topRow ? 'top 高=' + topRow.topH + 'px' : '没找到插件行');
  ok('列表里显示的是主版本号、完整版本号在悬停提示里',
     !!topRow && /^v?\d+(\.\d+){0,2}$/.test(topRow.label.trim()) &&
     /完整版本/.test(topRow.title),
     topRow ? ('列表=' + topRow.label + ' / title=' + topRow.title) : '没找到插件行');

  // 排序：有旧版本历史的要排最前。不按 agent 字母序 ——
  // 那样 ZCode/Trae 那 7 个"只记了启用开关"的零信息条目会霸占最前，
  // 把真正有旧版本可看的插件挤出屏幕（它们才是这一页的重点）。
  const ord = await page.evaluate(() => ({
    first: PLUGINS[0] ? { name: PLUGINS[0].name, old: PLUGINS[0].old_count } : null,
    maxOld: Math.max.apply(null, PLUGINS.map((p) => p.old_count || 0)),
    desc: PLUGINS.every((p, i) => i === 0 || (PLUGINS[i - 1].old_count || 0) >= (p.old_count || 0)),
  }));
  ok('插件按"本地旧版本多的在前"排序（别让零信息条目刷屏）',
     ord.desc && (!ord.maxOld || (ord.first && ord.first.old === ord.maxOld)),
     '最前=' + JSON.stringify(ord.first) + ' 全表降序=' + ord.desc);

  ok('页面无 JS 报错', errs.length === 0, errs.join(' | '));

  await browser.close();

  console.log('');
  console.log('  通过 ' + pass + ' 项，失败 ' + fail + ' 项' + (BROKEN ? '（反向测试）' : ''));
  if (BROKEN && fail === 0) {
    console.log('  ✗ 反向测试失败：把真凭据塞进 DOM / 清空配置清单，闸门竟然还是绿的');
    process.exit(1);
  }
  if (!BROKEN && fail > 0) process.exit(1);
  if (BROKEN) console.log('  ✓ 反向测试通过：回归确实被判红');
  process.exit(0);
})().catch((e) => { console.error('[闸门] 异常：' + e.message); process.exit(1); });
