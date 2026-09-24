#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「退出服务」两条路径的真实实测（在临时端口 + 临时库上跑，绝不动 8787 和真库）。

为什么需要它：
    「能关掉」这件事最容易假通过 —— 接口回了 ok:true 不代表进程真的退了，
    也可能是响应还没发完就把连接掐了（浏览器看到 ERR_CONNECTION_RESET）。
    所以这里三条都必须过：
      ① 接口返回 {ok:true} 且响应体完整读完（没被掐断）
      ② 进程真的消失（poll() 有退出码 + 端口释放）
      ③ 退出后数据库完好（还能打开、条数不变）

   第二条路径是闲置自动退出：页面关掉后靠心跳停摆触发，是"用完即弃"的设计核心。
   watchdog 每 20 秒巡检一次，所以这里至少等 25 秒。

用法：python tools/verify_shutdown.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

RESULTS = []


def rec(name, ok, info=""):
    RESULTS.append((name, ok, info))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("  " + info if info else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_ready(port, timeout=20):
    """轮询 /api/ping，返回 True 表示服务已就绪。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def port_open(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def db_count(db):
    import sqlite3
    c = sqlite3.connect(db, timeout=5)
    try:
        return c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        c.close()


def spawn(port, idle, db):
    env = dict(os.environ)
    env["LOCI_DB"] = db
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.Popen(
        [PY, "-u", os.path.join(ROOT, "panel.py"), "--port", str(port),
         "--idle-exit", str(idle)],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def post_json(port, path, payload=None, timeout=10):
    """返回 (状态码, 解析后的 JSON or None, 原始文本)。连接被掐会抛异常。"""
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path), data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
        try:
            return r.status, json.loads(raw), raw
        except ValueError:
            return r.status, None, raw


def main():
    tmp = tempfile.mkdtemp(prefix="hippo-shutdown-")
    real_db = os.path.join(ROOT, "loci.db")
    db = os.path.join(tmp, "loci.db")
    shutil.copy2(real_db, db)
    before = db_count(db)
    print("临时库：%s（%d 条记忆）\n" % (db, before))

    proc = None
    try:
        # ── 路径一：点「退出服务」按钮 → POST /api/shutdown ──────────────────
        print("── 路径一：手动退出（/api/shutdown）")
        port = free_port()
        proc = spawn(port, 0, db)   # 0 = 常驻，排除自动退出干扰
        ok_ready = wait_ready(port)
        rec("临时实例启动并就绪", ok_ready, "port=%d pid=%d" % (port, proc.pid))
        if not ok_ready:
            out = proc.stdout.read(4000).decode("utf-8", "replace") if proc.stdout else ""
            print("      启动输出：", out[-800:])
            raise SystemExit(2)

        rec("退出前端口在监听", port_open(port))

        try:
            code, body, raw = post_json(port, "/api/shutdown")
            rec("POST /api/shutdown 返回 200", code == 200, "code=%d" % code)
            rec("响应体完整可解析（没被掐断）", isinstance(body, dict),
                "raw=%r" % raw[:80])
            rec("响应声明 ok:true", bool(body and body.get("ok")), str(body))
        except urllib.error.URLError as e:
            rec("POST /api/shutdown 返回 200", False, "连接异常：%s" % e)
            rec("响应体完整可解析（没被掐断）", False, "连接被重置")
            rec("响应声明 ok:true", False, "拿不到响应")

        # 进程真的退出？代码里是延迟 0.4s 再 os._exit(0)
        gone = False
        t0 = time.time()
        while time.time() - t0 < 6:
            if proc.poll() is not None:
                gone = True
                break
            time.sleep(0.2)
        rec("进程真的退出（不是只回了 ok）", gone,
            "退出码=%s，耗时 %.1fs" % (proc.returncode, time.time() - t0))

        # 端口释放
        rel = True
        t0 = time.time()
        while time.time() - t0 < 4:
            if not port_open(port):
                break
            time.sleep(0.2)
        else:
            rel = False
        rec("端口已释放（没有僵尸监听）", rel, "port=%d" % port)

        # ── 路径二：闲置自动退出 ────────────────────────────────────────────
        print("\n── 路径二：闲置自动退出（--idle-exit 3，watchdog 每 20s 巡检）")
        port2 = free_port()
        proc2 = spawn(port2, 3, db)
        ok2 = wait_ready(port2)
        rec("临时实例启动并就绪", ok2, "port=%d" % port2)
        if ok2:
            t0 = time.time()
            auto_gone = False
            while time.time() - t0 < 40:
                if proc2.poll() is not None:
                    auto_gone = True
                    break
                time.sleep(1)
            rec("闲置超时后自动退出", auto_gone,
                "耗时 %.1fs" % (time.time() - t0))
            out2 = ""
            try:
                out2 = proc2.stdout.read(4000).decode("utf-8", "replace")
            except Exception:
                pass
            rec("退出前有提示文案（不是静默消失）", "闲置" in out2 or "自动退出" in out2,
                repr(out2.strip().splitlines()[-1][:60]) if out2.strip() else "无输出")
            if proc2.poll() is None:
                proc2.kill()

        # ── 数据完好 ───────────────────────────────────────────────────────
        print("\n── 退出后数据完好性")
        after = db_count(db)
        rec("数据库仍可读且条数不变", after == before, "%d → %d" % (before, after))

    finally:
        for p in (proc,):
            try:
                if p and p.poll() is None:
                    p.kill()
            except Exception:
                pass
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    bad = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + "═" * 52)
    print("通过 %d 项，失败 %d 项" % (len(RESULTS) - len(bad), len(bad)))
    if bad:
        print("红：" + "、".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
