#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验「浏览器提前断开」不再把 traceback 打到 stderr。

为什么需要它（2026-09-23 用户报的日志噪声）：
  浏览器**刷新 / 切走标签 / 关页面**时，正好赶上服务端在写响应，Windows 会抛
  ConnectionAbortedError [WinError 10053]。这不是故障，但没人接的话 socketserver
  会把整个 traceback 打满 stderr：
      panel.py do_GET → _json → _send
  功能不受影响，可日志脏到"看着像面板崩了"。

怎么验（不能只看代码，必须真造一次断连）：
  1. 起一个 panel 子进程，env 里设 LOCI_LOG_CONN=1 —— 这样一旦走到"吞掉"那条路，
     它会打一行「客户端提前断开（第 N 次，已忽略）」，等于告诉我们**确实触发了**；
  2. 用原始 socket 发一个请求，然后带 SO_LINGER=0 立刻 close —— 对端发 RST，
     服务端写响应时必然失败（这是 10053 的成因）；
  3. 断言：stderr 里**有**那行「客户端提前断开」（证明路走到了），
     且**没有** Traceback / ConnectionAbortedError（证明吞住了）；
  4. 再断言正常请求仍 200、页面长度正常 —— 别为了吞异常把真响应也搞坏。

用法：python tools/verify_conn_drop.py
"""
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PANEL = os.path.join(ROOT, "panel.py")

PROBE_TIMES = 6          # 断连探针次数
SKIP = os.environ.get("LOCI_TEST_SKIP_DB")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_up(port, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
                if r.status == 200:
                    return time.time() - t0
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("面板没起来")


def abort_probe(port):
    """发一个请求后立刻 RST 断开 —— 制造 WinError 10053 的现场。"""
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.connect(("127.0.0.1", port))
        s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
    finally:
        s.close()          # SO_LINGER=0 → 直接发 RST，不等正常四次挥手


def main():
    port = free_port()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["LOCI_LOG_CONN"] = "1"     # 关键：让"吞掉"那条路留下证据
    env.setdefault("LOCI_DB", os.path.join(ROOT, "loci.db"))

    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", PANEL, "--port", str(port), "--idle-exit", "0"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    fails = []
    try:
        took = wait_up(port)
        print(f"面板已起来（{took:.1f}s，端口 {port}）")

        for _ in range(PROBE_TIMES):
            abort_probe(port)
            time.sleep(0.35)
        time.sleep(1.5)                     # 等服务端把异常处理完

        # 正常请求必须仍然好使（吞异常别把真响应也吞了）
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=8) as r:
                page = r.read()
                ok_status, ok_len = r.status, len(page)
        except Exception as e:
            ok_status, ok_len = None, 0
            fails.append(f"正常请求失败：{e}")

        # /api 也要正常（走的就是 _json → _send 这条路）
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=8) as r:
                api_status = r.status
        except Exception as e:
            api_status = None
            fails.append(f"/api/ping 失败：{e}")
    finally:
        proc.terminate()
        try:
            _out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            _out, err = proc.communicate()
    stderr = (err or b"").decode("utf-8", "replace")

    swallowed = "客户端提前断开" in stderr
    has_tb = "Traceback" in stderr
    has_10053 = "ConnectionAbortedError" in stderr or "10053" in stderr
    other_tb = [ln for ln in stderr.splitlines() if "Traceback" in ln or "Error" in ln][:4]

    print()
    print("─" * 64)
    print(f"  断连探针 {PROBE_TIMES} 次")
    print(f"  走到「吞掉」那条路的证据   {'有 ✓' if swallowed else '没有 ✗（说明探针没触发，测试不算数）'}")
    print(f"  stderr 里的 Traceback      {'有 ✗' if has_tb else '无 ✓'}")
    print(f"  stderr 里的 WinError 10053 {'有 ✗' if has_10053 else '无 ✓'}")
    print(f"  正常页面  /                {ok_status} · {ok_len} 字节")
    print(f"  正常接口  /api/ping        {api_status}")
    print("─" * 64)

    if not swallowed:
        fails.append("探针没能触发连接中断 —— 这条闸门没验到东西，别当通过")
    if has_tb or has_10053:
        fails.append("stderr 里仍有 traceback：")
        fails.extend("    " + ln for ln in other_tb)
    if ok_status != 200 or ok_len < 50000:
        fails.append(f"正常页面响应异常：status={ok_status} len={ok_len}")
    if api_status != 200:
        fails.append(f"/api/ping 响应异常：{api_status}")

    if fails:
        print("\n失败：")
        for f in fails:
            print("  " + f)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
