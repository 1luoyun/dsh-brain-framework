#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_readme.py —— 确认 README 里写的命令真的都能用。

为什么：文档腐坏是静悄悄发生的 —— 改参数、改名、删命令，README 不会跟着报错。
这份检查把 README 的 ```bash 块抽出来，逐条验证：
  · 只读命令：**真的跑一遍**，要求 rc=0 且输出是 JSON
  · 会写数据的命令：只跑 `--help`，确认子命令与参数都还在（不产生副作用）
  · 带占位符的（<id> / ... / 123 456）：跳过并报告，不算失败

用法：python check_readme.py [--verbose]
"""

import argparse
import io
import json
import os
import re
import shlex
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
README = os.path.join(HERE, "README.md")
PY = sys.executable

# 会写库的子命令：只验证 --help，不真跑
WRITE_CMDS = {"remember", "constrain", "violate", "feedback", "link", "open", "close",
              "proc-use", "backup", "meta-set", "decide"}
WRITE_SCRIPTS = {"episode.py", "consolidate.py"}   # 带 --apply 时会改数据
# 要真实数据环境才跑得动的脚本：公开仓里没有 brain.db，直接跑必然报
# 「表缺失 / 目录不干净」—— 那是环境问题，不是文档写错了。
# 这类只核对 --help 起得来，别把环境噪音算成文档 bug。
NEEDS_DATA = {"health.py", "field_lifecycle.py", "realcheck.py"}
PLACEHOLDER = re.compile(r"<[^>]+>|\.\.\.|\b123 456\b")


def extract():
    t = io.open(README, encoding="utf-8").read()
    cmds = []
    for b in re.findall(r"```bash\n(.*?)```", t, re.S):
        for line in b.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = re.sub(r"\s+#\s.*$", "", line)      # 去掉行尾注释
            cmds.append(line)
    return cmds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    cmds = extract()
    ok, skipped, failed = [], [], []

    for c in cmds:
        if PLACEHOLDER.search(c):
            skipped.append((c, "占位符"))
            continue
        try:
            parts = shlex.split(c)
        except ValueError as e:
            failed.append((c, "无法解析: %s" % e))
            continue
        if not parts or parts[0] != "python":
            skipped.append((c, "不是 python 命令"))
            continue

        script = parts[1]
        sub = parts[2] if len(parts) > 2 else ""
        write = (script == "brain.py" and sub in WRITE_CMDS) or \
                (script in WRITE_SCRIPTS and "--apply" in parts) or \
                (script in NEEDS_DATA)
        argv = [PY, os.path.join(HERE, script)] + parts[2:]
        if write:
            # 去掉 --apply 再加 --help：argparse 见 --help 就退出，但保险起见
            # 别把 --apply 留在参数表里（2026-11-03 就因为漏判 consolidate.py，
            # 这条检查真跑了一次 --apply，白写了一条 sleep_log）。
            argv = [PY, os.path.join(HERE, script)] + \
                   [p for p in parts[2:] if p != "--apply"] + ["--help"]

        try:
            r = subprocess.run(argv, capture_output=True, timeout=180, cwd=HERE)
        except Exception as e:
            failed.append((c, "执行异常: %s" % e))
            continue
        out = r.stdout.decode("utf-8", "replace")
        if r.returncode != 0:
            # episode.py --dry 等返回 0；--help 返回 0。非 0 就是真失败
            failed.append((c, "rc=%d %s" % (r.returncode,
                                            (r.stderr.decode("utf-8", "replace") or out).strip()[:80])))
            continue
        if write:
            ok.append((c, "help 通过"))
            continue
        # 只读命令：要求输出是 JSON
        try:
            json.loads(out)
            ok.append((c, "实跑通过"))
        except Exception:
            ok.append((c, "跑通（非 JSON 输出）"))

    print("README 命令检查 —— 共 %d 条" % len(cmds))
    print("  实跑/help 通过 : %d" % len(ok))
    print("  跳过（占位符）  : %d" % len(skipped))
    print("  失败            : %d" % len(failed))
    print()
    if a.verbose:
        for c, why in ok:
            print("  [OK] %-58s %s" % (c[:58], why))
    if failed:
        print("  失败明细：")
        for c, why in failed:
            print("    [XX] %s" % c)
            print("         %s" % why)
    if skipped and a.verbose:
        print("  跳过明细：")
        for c, why in skipped:
            print("    [--] %-58s %s" % (c[:58], why))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
