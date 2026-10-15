# -*- coding: utf-8 -*-
"""构建便携版: 嵌入式 Python + 依赖库 + 脚本, 可在任意 Win x64 机器运行"""
import os
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "build" / "北京干部学院自动刷课"
DL = HERE / "downloads" / "embed.zip"


def main():
    # 嵌入式 Python 不存在时自动下载
    if not DL.exists():
        print("未找到嵌入式 Python, 自动下载 ...")
        sys.path.insert(0, str(HERE))
        import download_python
        if download_python.main() != 0:
            raise SystemExit("嵌入式 Python 下载失败, 无法构建")

    if ROOT.exists():
        shutil.rmtree(ROOT)
    (ROOT).mkdir(parents=True)

    # 1. 解压嵌入式 Python -> runtime/
    runtime = ROOT / "runtime"
    with zipfile.ZipFile(DL) as z:
        z.extractall(runtime)
    print("runtime:", sorted(p.name for p in runtime.iterdir()))

    # 2. 配置 ._pth: 加入 ../libs
    pth = runtime / "python312._pth"
    pth.write_text(
        "python312.zip\n.\n..\\libs\n",
        encoding="ascii")

    # 3. 复制 libs (排除 __pycache__)
    libs_src = HERE / "libs"
    libs_dst = ROOT / "libs"
    shutil.copytree(
        libs_src, libs_dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    n = sum(1 for _ in libs_dst.rglob("*"))
    print(f"libs: {n} 项")

    # 4. 复制主脚本
    shutil.copy2(HERE / "auto_play.py", ROOT / "auto_play.py")

    # 5. 启动器
    bat = ROOT / "启动刷课.bat"
    bat.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "cd /d \"%~dp0\"\r\n"
        "set PYTHONUTF8=1\r\n"
        "set PYTHONIOENCODING=utf-8\r\n"
        "title Beijing Cadre Academy Auto Player\r\n"
        "echo Starting... A browser window will open. Please login there.\r\n"
        "runtime\\python.exe auto_play.py\r\n"
        "echo.\r\n"
        "echo Program exited. Press any key to close.\r\n"
        "pause >nul\r\n",
        encoding="ascii")

    # 6. 使用说明
    (ROOT / "使用说明.txt").write_text(USAGE, encoding="utf-8-sig")

    total = sum(f.stat().st_size for f in ROOT.rglob("*") if f.is_file())
    print(f"构建完成: {ROOT}  共 {total / 1e6:.1f} MB")


USAGE = """北京干部网络学院自动刷学时 - 便携版使用说明
====================================================

【程序功能】
  自动播放北京干部网络学院(bjce.bjdj.gov.cn)课程视频:
  登录后自动开始播放; 视频暂停自动续播; 防止"30分钟未操作"自动暂停;
  播完一章自动切下一章; 播完一课自动切下一课;
  课程播放完毕后进度达到 80% 即视为完成, 不重播;
  直到 必修>=10 学时 且 选修>=40 学时(共50学时)自动停止。

【运行要求】
  - Windows 10/11 x64
  - 已安装 Chrome 或 Edge 浏览器(Windows 一般自带 Edge)
  - 能访问互联网(bjce.bjdj.gov.cn)
  - 无需安装 Python(已内置)

【使用步骤】
  1. 将本文件夹解压到 电脑桌面 或 文档 等可写位置
     (不要放到需要管理员权限的目录如 C:\\Program Files)
  2. 双击「启动刷课.bat」
  3. 首次运行: 在弹出的浏览器窗口中登录平台(账号密码或扫码)
     ——登录一次即可, 之后登录态自动保留
  4. 登录后全自动运行, 浏览器窗口可最小化, 但不要关闭
  5. 达到 50 学时后脚本自动停止; 中途想停止: 直接关闭命令行窗口

【注意事项】
  - 全程约需 30~40 小时播放时间, 请在系统电源设置中
    将"睡眠"设为"从不", 并保持电脑通电联网
  - 运行期间请勿在其他电脑/浏览器/手机上登录该平台
    (平台限制单会话, 多处登录会互相失效; 若失效, 脚本会
    自动回到首页等待重新登录, 登录后自动继续)
  - 播放过程完全按 1 倍速进行, 学时由平台服务端按实际
    观看时长累计
  - 如遇杀毒软件拦截, 请添加信任/白名单(程序仅打开浏览器
    播放课程, 不修改系统)

【文件说明】
  启动刷课.bat     启动入口
  auto_play.py     主程序
  runtime\\         内置 Python 3.12 运行环境
  libs\\            依赖库(playwright 等)
  profile\\         浏览器数据(保存登录态, 首次运行自动生成)
  progress.log     运行日志(可随时打开查看播放进度)
  state.json       当前学时快照
  errors\\          异常截图(自动生成)

【恢复初始状态】
  删除 profile 文件夹后重新启动 = 全新状态(需重新登录)。
"""


if __name__ == "__main__":
    main()
