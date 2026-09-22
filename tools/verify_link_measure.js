// 量「会话↔记忆」接缝处新组件的真实尺寸：有没有被压扁 / 溢出 / 文字被裁
const puppeteer = require('puppeteer-core');
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rec = (n, ok, i) => console.log((ok ? 'PASS  ' : 'FAIL  ') + n + (i ? '  ' + i : ''));

(async () => {
  const b = await puppeteer.launch({ executablePath: CHROME, headless: 'new',
    args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars'] });
  for (const theme of ['light', 'dark']) {
    const p = await b.newPage();
    await p.setViewport({ width: 1560, height: 900, deviceScaleFactor: 1 });
    await p.goto(`http://127.0.0.1:8787/?theme=${theme}#session`, { waitUntil: 'networkidle0' });
    await p.waitForFunction(() => document.querySelectorAll('#s-list .mem').length > 0, { timeout: 8000 });
    await p.evaluate(() => window.openSession(12));
    await sleep(800);

    const m = await p.evaluate(() => {
      const num = (v) => +v.toFixed(2);
      const r = (el) => el.getBoundingClientRect();
      const bubs = [...document.querySelectorAll('#s-view .bub')];
      const chips = [...document.querySelectorAll('#s-view .tchip')];
      const tmem = [...document.querySelectorAll('#s-view .tmem')];
      const side = document.querySelector('#v-session .split-side');
      const view = document.getElementById('s-view');
      const cs = (el) => getComputedStyle(el);
      // 气泡里的文字有没有被裁（scrollHeight > clientHeight 且 overflow hidden）
      const clipped = bubs.filter((x) => cs(x).overflow === 'hidden' && x.scrollHeight > x.clientHeight + 1);
      return {
        bubN: bubs.length,
        bubW: bubs.map((x) => num(r(x).width)),
        bubH: bubs.map((x) => num(r(x).height)),
        chipN: chips.length,
        chipW: chips.map((x) => num(r(x).width)),
        chipH: chips.map((x) => num(r(x).height)),
        chipTt: chips.map((x) => num(r(x.querySelector('.tchip-t')).width)),
        chipFont: chips.length ? cs(chips[0]).fontSize : '',
        chipRadius: chips.length ? cs(chips[0]).borderRadius : '',
        tmemN: tmem.length,
        tmemH: tmem.map((x) => num(r(x).height)),
        sideW: num(r(side).width), sideScrollW: side.scrollWidth,
        viewW: num(r(view).width), viewScrollW: view.scrollWidth,
        sideOverflow: side.scrollWidth > side.clientWidth + 1,
        clipped: clipped.length,
        sideMaxH: cs(side).maxHeight, sideOverflowY: cs(side).overflowY,
      };
    });
    rec(`[${theme}] 时间线气泡宽度受控（≤80% 右栏）`, m.bubW.every((w) => w <= m.sideW * 0.85), `最宽=${Math.max(...m.bubW)} 右栏=${m.sideW}`);
    rec(`[${theme}] 气泡未被裁字`, m.clipped === 0, `被裁=${m.clipped} 气泡数=${m.bubN}`);
    rec(`[${theme}] 记忆芯片有正常尺寸（不是被压成一根）`,
      m.chipH.every((h) => h >= 18 && h <= 30) && m.chipW.every((w) => w >= 60),
      `高=${m.chipH[0]} 宽=${m.chipW.slice(0, 4).join(',')} 字号=${m.chipFont} 圆角=${m.chipRadius}`);
    rec(`[${theme}] 芯片内部文字有宽度（没被压没）`, m.chipTt.every((w) => w > 12), `文字宽=${m.chipTt.slice(0, 4).join(',')}`);
    rec(`[${theme}] 记忆行分组正常`, m.tmemN >= 3, `分组行=${m.tmemN} 高=${m.tmemH[0]}`);
    rec(`[${theme}] 右栏无横向溢出`, !m.sideOverflow, `客户宽=${m.sideW} 内容宽=${m.sideScrollW}`);
    console.log(`  · 右栏滚动设置：maxHeight=${m.sideMaxH} overflowY=${m.sideOverflowY}`);

    // 记忆页：出处徽章有没有把行挤坏
    const mm = await p.evaluate(async () => {
      window.show('mem');
      await window.loadList();
      await new Promise((r) => setTimeout(r, 600));
      const rows = [...document.querySelectorAll('#list .mem.lrow')];
      const src = rows.filter((r) => r.querySelector('.bdg.src'));
      const h = rows.map((r) => +r.getBoundingClientRect().height.toFixed(2));
      const tagEl = rows.find((r) => r.querySelector('.mtags'));
      return { n: rows.length, srcN: src.length,
        hmin: Math.min(...h), hmax: Math.max(...h),
        srcW: src.length ? +src[0].querySelector('.bdg.src').getBoundingClientRect().width.toFixed(2) : 0,
        srcH: src.length ? +src[0].querySelector('.bdg.src').getBoundingClientRect().height.toFixed(2) : 0,
        tagW: tagEl ? +tagEl.querySelector('.mtags').getBoundingClientRect().width.toFixed(2) : 0,
        tagClipped: tagEl ? tagEl.querySelector('.mtags').scrollWidth > tagEl.querySelector('.mtags').clientWidth + 1 : null };
    });
    rec(`[${theme}] 记忆行高统一（61.19 基准，带出处也不变）`,
      Math.abs(mm.hmax - mm.hmin) < 0.6 && Math.abs(mm.hmax - 61.19) < 1.2,
      `最小=${mm.hmin} 最大=${mm.hmax} 行=${mm.n} 带出处=${mm.srcN}`);
    rec(`[${theme}] 出处徽章尺寸正常、标签区还有宽度`,
      mm.srcH >= 15 && mm.srcH <= 20 && mm.srcW >= 40,
      `徽章=${mm.srcW}×${mm.srcH} 剩余标签宽=${mm.tagW}`);

    // 反方向：从记忆卡点「跳到这段原话」——必须真的滚到那一轮，且落在视口里
    const jump = await p.evaluate(async () => {
      window.show('mem');
      await window.loadList();
      await new Promise((r) => setTimeout(r, 500));
      await window.gotoSession(12, 15);
      await new Promise((r) => setTimeout(r, 1800));
      const t = document.getElementById('tn-12-15');
      if (!t) return { err: 'no anchor' };
      const c = document.querySelector('.content');
      const r = t.getBoundingClientRect();
      return { top: +r.top.toFixed(1), bottom: +r.bottom.toFixed(1),
        vh: window.innerHeight, contentTop: c.scrollTop, contentClientH: c.clientHeight,
        inView: r.top < window.innerHeight && r.bottom > 0,
        atTop: r.top > -4 && r.top < 120 };
    });
    rec(`[${theme}] 「跳到这段原话」滚到第 15 轮且开头可见`, jump.inView && jump.atTop,
      `元素 top=${jump.top} 内容区 scrollTop=${jump.contentTop} 视口=${jump.vh}`);
    console.log('');
    await p.close();
  }
  await b.close();
})().catch((e) => { console.error('ERR', e.message); process.exit(1); });
