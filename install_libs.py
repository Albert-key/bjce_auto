# -*- coding: utf-8 -*-
"""手动安装 playwright: 下载 wheel 并解压到 libs/ (绕开 pip 的临时目录搬移)"""
import json
import os
import re
import ssl
import sys
import urllib.request
import zipfile

MIRROR = "https://pypi.tuna.tsinghua.edu.cn"
HERE = os.path.dirname(os.path.abspath(__file__))
WHEELS = os.path.join(HERE, "wheels")
LIBS = os.path.join(HERE, "libs")
os.makedirs(WHEELS, exist_ok=True)
os.makedirs(LIBS, exist_ok=True)

UA = {"User-Agent": "pip/25.0 (python 3.12; Windows)"}
MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn",
    "https://mirrors.aliyun.com",
    "https://pypi.org",
]
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=120, context=CTX).read()


def simple_index(pkg: str) -> tuple:
    """按镜像顺序尝试, 返回 (html, 该镜像的包页面 URL)"""
    last_err = None
    for base in MIRRORS:
        url = f"{base}/simple/{pkg}/"
        try:
            return fetch(url).decode("utf-8", "replace"), base
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  镜像 {base} 失败: {e}")
    raise RuntimeError(f"所有镜像均失败: {last_err}")


def pick_wheel(pkg: str, pattern: str) -> tuple:
    """返回 (下载链接, 文件名) — 选匹配 pattern 且版本最高的 wheel"""
    html, base = simple_index(pkg)
    links = re.findall(r'href="([^"]+)"[^>]*>([^<]+\.whl)</a>', html)
    best = None
    for href, fname in links:
        if re.search(pattern, fname):
            ver = fname.split("-")[1]
            key = [int(x) if x.isdigit() else 0 for x in re.split(r"[.]", ver)]
            if best is None or key > best[0]:
                best = (key, href, fname)
    if not best:
        raise RuntimeError(f"未找到匹配 {pattern} 的 {pkg} wheel")
    href = best[1]
    # 相对链接转绝对
    if href.startswith("/"):
        for m in MIRRORS:
            if m in base or base == m:
                href = m + href
                break
        else:
            href = MIRRORS[0] + href
    elif not href.startswith("http"):
        href = f"{base}/simple/{pkg}/{href}"
    return href, best[2]


def main():
    plans = [
        ("playwright", r"-py3-none-win_amd64\.whl$"),
        ("greenlet", r"-cp312-cp312-win_amd64\.whl$"),
        ("pyee", r"-py3-none-any\.whl$"),
    ]
    for pkg, pat in plans:
        href, fname = pick_wheel(pkg, pat)
        path = os.path.join(WHEELS, fname)
        if not os.path.exists(path):
            print(f"下载 {fname} ...")
            data = fetch(href)
            with open(path, "wb") as f:
                f.write(data)
            print(f"  {len(data) / 1e6:.1f} MB")
        print(f"解压 {fname} -> libs/")
        with zipfile.ZipFile(path) as z:
            z.extractall(LIBS)
    print("完成")


if __name__ == "__main__":
    sys.exit(main())
