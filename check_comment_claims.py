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

# Windows 控制台是 GBK，⚠ 这类字形没有 → print 当场 UnicodeEncodeError，
# 而崩的时候清单已经算完、还没打出来。2026-11-07 实测就在第 99 行崩了。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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


def has_number(text):
    """注释里有没有具体数字（阈值、比例、条数、实测值）。

    2026-11-06 实测出的规律：**带数字的注释几乎不会腐烂** ——
      · 「阈值 0.35，因为 1 号题的 0.354 差 0.1 会掉档」→ 可验证，基本不会错
      · 「判据用密度不用总数」→ 看起来具体、实际没数字，**最容易错**
    今天核了 8 处，唯一不一致的 `_arousal` 恰好是唯一一条宣称了机制却没带数字的。
    所以这个函数用来**给清单排优先级**：没数字的排前面。
    """
    return bool(re.search(r"[0-9]", text))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(TARGET):
        print("找不到 brain.py")
        return 1
    rows = scan(TARGET)
    # 没数字的排前面 —— 那是今天实测里唯一出过不一致的那类
    rows = sorted(rows, key=lambda x: (has_number(x[2]), x[1]))

    if a.json:
        print(json.dumps({"count": len(rows), "claims": [
            {"fn": f, "line": l, "text": t, "has_number": has_number(t)}
            for f, l, t in rows]}, ensure_ascii=False, indent=2))
        return 0

    risky = [r for r in rows if not has_number(r[2])]
    print("注释里宣称了具体机制的地方 —— %d 处（**这些都要去代码里核一遍**）" % len(rows))
    print()
    if risky:
        print("⚠ 优先核这 %d 处：**宣称了机制但没带数字** —— 今天唯一出问题的那类" % len(risky))
        for fn, ln, txt in risky:
            print("  L%-5d %-22s %s" % (ln, fn, txt))
        print()
    rest = [r for r in rows if has_number(r[2])]
    if rest:
        print("其余 %d 处（带数字，一般写的时候被证据约束过，风险低）：" % len(rest))
        for fn, ln, txt in rest:
            print("  L%-5d %-22s %s" % (ln, fn, txt))
    print()
    print("提醒：这个工具只出清单，不判对错。已核 8 处、1 处不一致（_arousal 说「用密度不用总数」，")
    print("      实际是纯总数）。判断要靠读代码 + 跑一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
