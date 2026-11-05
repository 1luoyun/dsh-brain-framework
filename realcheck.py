#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""realcheck.py — 真实场景验收（跟 eval.py 是两把不同的尺子）。

为什么需要它（2026-11-05 立）：
    eval.py 的 50 道题是**从库里挑事件反推题面** —— 答案已知，题围着答案造。
    真实提问反过来：先有问题、才去找答案，用的还是我自己的说法
    （「记忆库现在健康吗」，而不是「体检 PASS」）。两种是不同的任务。
    实测：eval 98.3% 的时候，10 个自然提问只有 1 条可用。

    所以这里放的是**我先有问题、且答案我知道在哪**的那批。
    它不替代 eval（eval 测排序与召回），它测「自然说法能不能够到」。

判据：任意一层（events / facts / procedures / constraints）命中期望 id 即算 pass。
    这是刻意的宽容 —— 真实使用里，我不管它在哪一层，能拿到就行。

用法：
    python realcheck.py           # 跑一遍，给命中率
    python realcheck.py --json
    python realcheck.py --show    # 连每题的返回一起看
"""
import argparse
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
BRAIN = os.path.join(HERE, "brain.py")

# 每条：问题、任意一层命中即可的期望 id、备注（为什么这么标）
CASES = [
    {"q": "我之前删错文件是怎么救回来的",
     "expect": [6, 7],
     "note": "fact 6（$I/$R 还原）或 constraints 7（走回收站）"},
    {"q": "改完配置怎么确认没改坏",
     "expect": [11],
     "note": "constraints 11：备份 → 解析校验 → 是否需重启"},
    {"q": "怎么判断一个插件装上没生效",
     "expect": [1, 2],
     "note": "fact 1（补丁层 disable）/ fact 2（inject pending 卡死）"},
    {"q": "编码问题怎么区分是哪一种",
     "expect": [126, 127, 128],
     "note": "fact 126/127/128：字形不存在 / 解码方向搞反 / bat 中文注释"},
    {"q": "哪些目录绝对不能碰",
     "expect": [5],
     "note": "constraints 5：~/.dsh、Programs\\DSH*、Windows"},
    {"q": "评测分数能信几分",
     "expect": [6572, 107],
     "note": "event 6572（逆向工程偏差）/ fact 107（评测与真实使用分开）"},
    {"q": "怎么让 agent 别陷入循环",
     "expect": [6542],
     "note": "event 6542：loop-guard 断路器"},
    # ── procedures 层（2026-11-05 补）──
    # 由头：procedures 九条的 success/fail 全是 0（proc-use 从没被调用过）。
    # 先要确认的是「内容有没有用」，再谈「有没有记成败」—— 实测这三条自然提问
    # 都召回到了对的步骤，所以内容没问题，缺的是使用反馈。
    {"q": "插件装上不显示怎么办",
     "expect": [1], "note": "procedures 1：插件缺文件时的修法"},
    {"q": "要递归删东西之前该做什么",
     "expect": [2], "note": "procedures 2：删东西前的三步预检"},
    {"q": "改配置文件前要注意什么",
     "expect": [3], "note": "procedures 3：改配置的安全姿势"},
    # ── 今天真实遇到过的问法（2026-11-05 补）──
    # 这两条不是我"觉得应该问"，是落云在会话里真的这么问过。
    # 补题的判据就是这个：**我只能用真遇到过的提问**，拍脑袋想出来的题
    # 会不自觉围着库里的标题造，那样测出来的分数没有意义。
    {"q": "思维链卡循环怎么办",
     "expect": [6542],
     "note": "event 6542：loop-guard 断路器（换一种说法问同一件事）"},
    {"q": "人格一致性怎么实现",
     "expect": [],
     "note": "答案在知识库 04-人格，库里确实没有 —— 不许硬凑"},
    # 类型意图的端到端（2026-11-05 补）。夹具只验「认不认得类型词」，
    # 这条验的是「认出来之后真的能输出」—— 上轮就是补池了却被 verdict 二次否决，
    # 输出 0 条。端到端才抓得住那种。
    {"q": "我改配置出过什么事故",
     "expect": [4535],
     "note": "类型意图 accident：词面撞不上，靠 kind 补池"},
    # 答案不在记忆库里（在 AGENTS.md / 知识库）。这几条用来验证
    # 「该说没有的时候，别硬凑一堆不相干的」—— 不计入命中率，只记录。
    {"q": "备份一般放哪里、留几份", "expect": [], "note": "答案在 AGENTS.md，库里没有"},
    {"q": "为什么不能给记忆加情绪召回", "expect": [], "note": "答案在知识库 04-人格，库里没有"},
]


def recall(q, limit=5):
    r = subprocess.run([sys.executable, BRAIN, "recall", q, "--limit", str(limit), "--no-count"],
                       capture_output=True, timeout=300, cwd=HERE)
    try:
        return json.loads(r.stdout.decode("utf-8", "replace"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()

    rows = []
    for c in CASES:
        d = recall(c["q"])
        if d is None:
            rows.append({"q": c["q"], "ok": False, "reason": "recall 失败", "note": c["note"]})
            continue
        # 各层的 id 是**独立编号**（procedures 5 和 constraints 5 是两个东西）。
        # 原来用裸 id 当键，结果「哪些目录绝对不能碰」命中的 constraints 5
        # 被 procedures 5（会话恢复）抢了层标记 —— 2026-11-05 首跑就撞上。
        got = {}
        for layer in ("hits", "facts", "procedures", "constraints"):
            for item in (d.get(layer) or []):
                got.setdefault((layer, item.get("id")), True)
        layers_of = [layer for layer in ("hits", "facts", "procedures", "constraints")]
        hits_found = [(e, layer) for e in c["expect"] for layer in layers_of
                      if (layer, e) in got]
        if c["expect"]:
            ok = bool(hits_found)
            reason = ("命中 %s" % ", ".join("%s#%s" % (layer, e) for e, layer in hits_found)
                      if ok else "未命中")
        else:
            ok = None
            reason = "答案不在库里，返回 %d 条（仅供观察）" % len(got)
        rows.append({"q": c["q"], "ok": ok, "reason": reason, "note": c["note"],
                     "top": [i.get("id") for i in (d.get("hits") or [])[:5]]})

    scored = [r for r in rows if r["ok"] is not None]
    passed = sum(1 for r in scored if r["ok"])

    if a.json:
        print(json.dumps({"ok": True, "scored": len(scored), "passed": passed,
                          "rate": round(passed / max(1, len(scored)), 3), "rows": rows},
                         ensure_ascii=False, indent=2))
        return 0

    print("真实场景验收 —— %d 题可判（另 %d 题答案不在库里，仅观察）"
          % (len(scored), len(rows) - len(scored)))
    for r in rows:
        mark = "OK " if r["ok"] else ("-- " if r["ok"] is None else "XX ")
        print("  [%s] %-22s %s" % (mark, r["q"], r["reason"]))
        if a.show and r.get("top"):
            print("        top5=%s" % r["top"])
    # 意图识别的**误触发**自检（2026-11-05 立）。
    # 由头：加时间意图那轮只验了「目标查询对不对」，没验「不该触发的会不会触发」，
    # 结果「最近的备份放在哪」被当成时间查询，返回的全是今天的事。
    # 写关键词规则时想的永远是命中的情形，漏的永远是不该命中的那些 ——
    # 所以把它固化成夹具，每次跑 realcheck 都过一遍。
    bad = check_intents()
    if bad:
        print()
        print("意图误触发/漏触发 %d 处：" % len(bad))
        for line in bad:
            print("  XX " + line)

    print()
    print("  命中 %d/%d（%.0f%%）" % (passed, len(scored), 100.0 * passed / max(1, len(scored))))
    print()
    print("  提醒：这把尺子跟 eval.py 测的不是一回事 —— eval 测「能不能找回那条记录」，")
    print("  这里测「自然说法能不能够到答案」。两个数字要一起看，别拿一个替代另一个。")
    return 0 if (passed == len(scored) and not bad) else 1


# 每个用例：查询、期望的 time_window、期望的 degree_intent 门限、期望的 kind。
# None 表示「不该触发」—— **第三列和第四列的 None 才是重点**，出问题都在那里。
INTENT_CASES = [
    ("最近的备份放在哪", None, None, None),      # 问东西在哪，不是问时间
    ("最后一个文件", None, None, None),          # 「最后」不是程度
    ("最好用的搜索方式", None, None, None),
    ("删除文件", None, None, None),              # 有「删除」但不是问事故
    ("怎么让 agent 别陷入循环", None, None, None),  # 「别」不是类型词
    ("最近做了什么", 3.0, None, None),
    ("今天都干了什么", 1.0, None, None),
    ("昨天改了什么配置", 2.0, None, None),
    ("我犯过最严重的错误", None, 90.0, None),
    ("最重要的教训", None, 90.0, "learn"),       # 同时点了程度和类型
    ("我学过哪些教训", None, None, "learn"),
    ("我做过哪些决定", None, None, "decision"),
    ("我改配置出过什么事故", None, None, "accident"),
    ("我哪里被纠正过", None, None, "correction"),
]


def check_intents():
    """验 brain 里那三个意图识别：该触发的触发、不该触发的别触发。"""
    sys.path.insert(0, HERE)
    try:
        import brain
    except Exception as e:
        return ["import brain 失败：%s" % type(e).__name__]
    bad = []
    for q, want_t, want_d, want_k in INTENT_CASES:
        got_t = brain._time_window(q)
        got_d = brain._degree_intent(q)
        got_k = brain._kind_intent(q)
        if got_t != want_t:
            bad.append("%s  time_window 期望 %s 实际 %s" % (q, want_t, got_t))
        if got_d != want_d:
            bad.append("%s  degree_intent 期望 %s 实际 %s" % (q, want_d, got_d))
        if got_k != want_k:
            bad.append("%s  kind_intent 期望 %s 实际 %s" % (q, want_k, got_k))
    return bad


if __name__ == "__main__":
    sys.exit(main())
