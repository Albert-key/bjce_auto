# -*- coding: utf-8 -*-
"""下载 Python 3.12.14 嵌入式发行包 (amd64), 多镜像自动回退"""
import os
import ssl
import sys
import urllib.error
import urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

VER = None
FNAME = None
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "downloads", "embed.zip")
os.makedirs(os.path.dirname(OUT), exist_ok=True)

# 3.12.14 无嵌入式发行版, 从高到低探测可用的 3.12.x
CANDIDATES = ["3.12.13", "3.12.12", "3.12.11", "3.12.10", "3.12.9",
              "3.12.8", "3.12.7", "3.12.6", "3.12.5", "3.12.4",
              "3.12.3", "3.12.2", "3.12.1", "3.12.0"]

MIRRORS = [
    "https://www.python.org/ftp/python",
    "https://mirrors.huaweicloud.com/python",
    "https://mirrors.aliyun.com/python",
]


def main():
    for ver in CANDIDATES:
        for base in MIRRORS:
            url = f"{base}/{ver}/python-{ver}-embed-amd64.zip"
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(
                    req, timeout=180, context=ctx).read()
                with open(OUT, "wb") as f:
                    f.write(data)
                print(f"OK: Python {ver} 嵌入式, {len(data) / 1e6:.1f} MB -> {OUT}")
                with open(os.path.join(os.path.dirname(OUT), "version.txt"),
                          "w") as f:
                    f.write(ver)
                return 0
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    break  # 该镜像无此版本, 试下一版本
                print(f"  {base} {ver}: {e}")
            except Exception as e:  # noqa: BLE001
                print(f"  {base} {ver}: {e}")
    print("未找到可用的 3.12.x 嵌入式发行版")
    return 1


if __name__ == "__main__":
    sys.exit(main())
