"""停止本地服务：结束监听配置端口(默认 8765)的进程。纯标准库。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import HOST, PORT

import subprocess


def main():
    port = PORT
    print("正在停止端口 %d 上的服务..." % port)
    killed = False
    try:
        raw = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"], stderr=subprocess.DEVNULL
        )
        # 中文 Windows 的 netstat 输出为 GBK，用容错解码，避免 utf-8 报错
        out = raw.decode("utf-8", errors="replace")
        if "\ufffd" in out:
            out = raw.decode("gbk", errors="replace")
        for line in out.splitlines():
            if (":%d" % port) in line and "LISTENING" in line.upper():
                pid = line.strip().split()[-1]
                subprocess.run(["taskkill", "/PID", pid, "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print("已结束进程 PID", pid)
                killed = True
    except Exception as e:
        print("通过 netstat 查找失败：", e)
    if not killed:
        print("未发现运行中的服务（或端口未被占用）。")


if __name__ == "__main__":
    main()
