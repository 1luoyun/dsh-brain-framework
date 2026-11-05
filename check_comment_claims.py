"""扫 brain.py 里「注释宣称了具体机制」的地方，列出待核清单（2026-11-06 立）。

为什么需要它：今天抽查 6 个函数，发现 `_arousal` 的注释写着「判据用密度不用总数」，
而代码一直是纯总数判断 —— **注释和实现对不上，而它比「没有注释」更危险**：
下一个人（包括明天的我）看到那句话会以为密度已经在起作用，从而不会再想到去改它。

判据：注释里出现下面这些**可执行细节**时，必须去代码里核一遍：
  · 「用 A（不用 B）」「按 C 算」「阈值是 D」「系数 E」「权重 F」
  · 「>= N」「<= N」「N 天」「N 次」这类具体数字
笼统的注释（「这里做语义补充」）不容易骗人，**具体的才会**。

这个工具只出清单，不判对错 —— 判断要靠读代码 + 跑一次。

用法：
    python check_comment_claims.py            # 列清单
    python check_comment_claims.py --json     # 机器可读
"""

import argparse
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "brain.py")

# 可执行细节的线索词 —— **只留「说了但可能没做」的那种**。
# 第一版用了「按」「算」「权重」，结果 165 处，等于没区分度（这两个字在中文里太常见）。
# 收紧后只盯「宣称了一个机制」的句式，实测 30 处上下，人工能过一遍。
CLAIM_WORDS = (
    "不用", "用的是", "判据是", "判据用", "阈值是", "系数是",
    "上限是", "下限是", "改为", "改成", "换成", "按密度", "按权重",
)


def scan(path):
    """返回 [(函数名, 行号, 注释行)] —— 只收函数 docstring 或紧邻注释里的宣称。"""
    lines = io.open(path, encoding="utf-8", errors="replace").read().split("\n")
    out = []
    cur_fn = None
    in_doc = False
    for i, ln in enumerate(lines, 1):
        m = re.match(r"\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", ln)
        if m:
            cur_fn = m.group(1)
            in_doc = False
            continue
        stripped = ln.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            in_doc = not in_doc or stripped.count('"""') == 1
            continue
        is_comment = stripped.startswith("#")
        if not (in_doc or is_comment):
            continue
        if cur_fn is None:
            continue
        if any(w in stripped for w in CLAIM_WORDS):
            if len(stripped) >= 14:
                out.append((cur_fn, i, stripped[:100]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(TARGET):
        print("找不到 brain.py")
        return 1
    rows = scan(TARGET)

    if a.json:
        print(json.dumps({"count": len(rows), "claims": [
            {"fn": f, "line": l, "text": t} for f, l, t in rows]},
            ensure_ascii=False, indent=2))
        return 0

    print("注释里宣称了具体机制的地方 —— %d 处（**这些都要去代码里核一遍**）" % len(rows))
    print()
    for fn, ln, txt in rows:
        print("  L%-5d %-22s %s" % (ln, fn, txt))
    print()
    print("提醒：这个工具只出清单，不判对错。今天实测 6 个函数里 1 个对不上")
    print("      （_arousal 说「用密度不用总数」，实际是纯总数）。判断要靠读代码 + 跑一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
