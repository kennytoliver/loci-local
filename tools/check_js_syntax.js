#!/usr/bin/env node
/*
 * 闸门：面板内嵌的 JS 必须语法正确。
 *
 * 为什么要单独一盏灯：面板的 JS 全写在一个 **Python 三引号字符串**里
 * （panel.py 里的 HTML 模板）。少一个括号不会被 Python 发现（字符串而已），
 * 只会在浏览器里整块 script 解析失败 → 页面上每个按钮都点不动。
 * 而其它浏览器闸门对这种故障的表现是"等 window.show 超时"，
 * 报出来的话跟真正的原因（第 1234 行少个右括号）差了十万八千里。
 *
 * 踩过的坑：别拿 panel.py 的**源文件**来抽 <script> ——
 * Python 字符串里的 `\\s` 在源码里是双反斜杠、在响应里才是单反斜杠，
 * 直接读源码会把自检页那条正则误判成语法错误（假红）。
 * 所以这里走 HTTP 读**服务端真正吐出来的 HTML**。
 *
 * 用法：node tools/check_js_syntax.js [base]
 */
const http = require('http');

const BASE = process.argv[2] || 'http://127.0.0.1:8787';

function get(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let buf = '';
      res.setEncoding('utf8');
      res.on('data', (d) => { buf += d; });
      res.on('end', () => resolve(buf));
    }).on('error', reject);
  });
}

(async () => {
  let html;
  try { html = await get(BASE + '/'); }
  catch (e) {
    console.error('  ✗ 取不到面板页面（面板没起？）：' + e.message);
    process.exit(1);
  }
  const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  if (!blocks.length) { console.error('  ✗ 页面里一个 <script> 都没有'); process.exit(1); }

  let fail = 0;
  blocks.forEach((js, i) => {
    try {
      new Function(js);        // 只解析、不执行
      console.log('  ✓ script[' + i + '] 语法正确（' + js.length + ' 字符）');
    } catch (e) {
      fail++;
      const lines = js.split('\n');
      console.log('  ✗ script[' + i + '] 语法错误：' + e.message);
      // 尽量把出错的源码行打出来 —— 否则只知道"错了"、不知道错在哪
      const m = /line (\d+)/.exec(e.stack || '');
      if (m) {
        const n = parseInt(m[1], 10) - 2;
        if (lines[n]) console.log('      ' + (n + 1) + ' | ' + lines[n].trim().slice(0, 160));
      }
    }
  });
  console.log('  通过 ' + (blocks.length - fail) + ' 块，失败 ' + fail + ' 块');
  process.exit(fail > 0 ? 1 : 0);
})().catch((e) => { console.error('[闸门] 异常：' + e.message); process.exit(1); });
