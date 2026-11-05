#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eval.py — 检索评测：把「检索好不好」变成一个能重复跑的数字。

为什么需要它：前面十几轮改过 recall 三次（多线索兜底、bm25、相关性混合），
每次都是「挑几个查询看看效果」——挑的都是我熟悉的例子，等于自测自己出的题。
要谈「比现有架构好」，得先有一条不看个例的基线。

指标：
  hit@1 / hit@3 / hit@10   —— 期望事件落在前 1/3/10 条里的题数占比
  neg_pass                 —— negative 题（库里没有）正确返回 no_match 的比例
  overall                  —— 两类题合起来的准确率

用法：
    python eval.py              # 跑全部，给汇总
    python eval.py --verbose    # 另外列出每题命中详情
    python eval.py --json       # 机读
"""

import argparse
import atexit
import json
import os
import sqlite3
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
# 题集可换：BRAIN_EVAL_CASES 环境变量 > 同目录 eval_cases.json > examples 里的示例。
# 2026-11-03 开源准备：真实题集含本机私事（openAt/pnpm/小说章节），不能进仓库；
# 仓库只带一份通用示例，各人按自己的库造题才测得准。
CASES = os.environ.get("BRAIN_EVAL_CASES") or os.path.join(HERE, "eval_cases.json")
if not os.path.exists(CASES):
    CASES = os.path.join(HERE, "examples", "eval_cases.example.json")
BRAIN = os.path.join(HERE, "brain.py")


_SNAP = None


def _cleanup_snapshot():
    """跑完把快照删掉。删不掉不影响结果，但会留在目录里（health 会报）。"""
    global _SNAP
    if _SNAP and os.path.exists(_SNAP):
        try:
            os.remove(_SNAP)
        except OSError:
            pass


atexit.register(_cleanup_snapshot)


def _make_snapshot():
    """把 brain.db 复制成评测专用快照，返回路径；失败返回 None。

    2026-11-05 立：**评测必须跑在库快照上**。
    实测：同一份代码、中间只夹了几条 remember，hit@10 就从 45 变成 44 ——
    库本身在变，前后数字根本不可比。复制一次约 0.2 秒，换来的是可重复。
    用 sqlite 的 backup API：不锁库、不打断正在跑的写入。
    """
    global _SNAP
    src = os.environ.get("BRAIN_DB") or os.path.join(HERE, "brain.db")
    if not os.path.exists(src):
        return None
    dst = os.path.join(HERE, ".eval-snapshot.db")
    try:
        s = sqlite3.connect(src)
        d = sqlite3.connect(dst)
        with d:
            s.backup(d)
        d.close()
        s.close()
    except Exception:
        return None
    _SNAP = dst
    return dst


def recall(q, limit=10):
    r = subprocess.run([sys.executable, BRAIN, "recall", q, "--limit", str(limit), "--no-count"],
                       capture_output=True, timeout=300, cwd=HERE)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.decode("utf-8", "replace"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--live", action="store_true",
                    help="直接在真库上跑。默认先复制快照 —— 库在变，不复制就不可比")
    a = ap.parse_args()

    if not a.live:
        snap = _make_snapshot()
        if snap:
            os.environ["BRAIN_DB"] = snap

    data = json.load(open(CASES, encoding="utf-8"))
    cases = data["cases"]

    rows = []
    for c in cases:
        d = recall(c["q"], a.limit)
        ids = [h["id"] for h in (d or {}).get("hits", [])]
        verdict = (d or {}).get("verdict", "error")
        exp = c.get("expect") or []
        rank = None
        for i, eid in enumerate(ids, 1):
            # 只在 limit 之内才算命中 —— 之前漏了这个约束，
            # 排在 16 名的也会被标成 OK，和 hit@10 的统计自相矛盾。
            if eid in exp and i <= a.limit:
                rank = i
                break
        # 期望也可能落在事实层或程序层。问「该注意什么」这种问题，
        # 正确答案本来就该是一条事实或一套步骤，而不是某条历史事件 ——
        # 只认事件 id 会把「答对了」判成「没答」。
        fact_ids = [f["id"] for f in (d or {}).get("facts", [])]
        proc_ids = [p["id"] for p in (d or {}).get("procedures", [])]
        aux = bool(set(fact_ids) & set(c.get("expect_facts") or [])) or \
              bool(set(proc_ids) & set(c.get("expect_procs") or []))
        if aux and rank is None:
            rank = 1
        # negative 题：判定标准是「库如实回答这里没有」。
        # 2026-11-05：原来只认 no_match_by_these_cues，于是空库（empty_library）
        # 时负例题全判错 —— 可「库里一条都没有」恰恰是最诚实的「没有」。
        # 开源仓首次跑示例题集时暴露的（0/2），本机库非空所以一直没显形。
        neg_ok = (verdict in ("no_match_by_these_cues", "empty_library")) if c["type"] == "negative" else None
        rows.append({
            "id": c["id"], "type": c["type"], "q": c["q"], "expect": exp,
            "verdict": verdict, "top": ids[:5], "rank": rank, "neg_ok": neg_ok,
            "via": "fact/proc" if aux and not any(e in ids[:a.limit] for e in exp) else "event",
        })

    pos = [r for r in rows if r["type"] != "negative"]
    neg = [r for r in rows if r["type"] == "negative"]
    hit1 = sum(1 for r in pos if r["rank"] == 1)
    hit3 = sum(1 for r in pos if r["rank"] and r["rank"] <= 3)
    hit10 = sum(1 for r in pos if r["rank"] and r["rank"] <= a.limit)
    neg_ok = sum(1 for r in neg if r["neg_ok"])
    total = len(rows)
    good = hit10 + neg_ok

    out = {
        "ok": True,
        "cases": total,
        "positive": len(pos),
        "negative": len(neg),
        "hit@1": hit1,
        "hit@3": hit3,
        "hit@10": hit10,
        "miss": len(pos) - hit10,
        "neg_pass": neg_ok,
        "overall": round(good / total, 3) if total else 0,
        "rows": rows if a.verbose else None,
    }

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print("检索评测 —— %d 题（正向 %d / 否定 %d）" % (total, len(pos), len(neg)))
        print("  hit@1  : %d/%d" % (hit1, len(pos)))
        print("  hit@3  : %d/%d" % (hit3, len(pos)))
        print("  hit@10 : %d/%d" % (hit10, len(pos)))
        print("  neg_pass: %d/%d" % (neg_ok, len(neg)))
        print("  overall : %.1f%%" % (100.0 * good / total))
        if good == total:
            print()
            print("  注意：全过。这通常说明题集已经被调透了 ——"
                  " 扩题比继续调参更有价值。")
        print()
        if a.verbose:
            for r in rows:
                mark = "OK " if (r["neg_ok"] if r["type"] == "negative" else r["rank"]) else "XX "
                rank = ("#" + str(r["rank"])) if r["rank"] else ("no_match" if r["neg_ok"] else "miss")
                print("  [%s] %-2d %-6s %-38s -> %s" % (mark, r["id"], r["type"], r["q"][:38], rank))
                if not r["neg_ok"] if r["type"] == "negative" else not r["rank"]:
                    print("        top5=%s" % r["top"])
        else:
            bad = [r for r in rows if not (r["neg_ok"] if r["type"] == "negative" else r["rank"])]
            for r in bad:
                print("  未命中 [%-2d %s] %s" % (r["id"], r["type"], r["q"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
