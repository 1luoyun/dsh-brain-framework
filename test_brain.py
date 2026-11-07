#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_brain.py —— 回归测试：在隔离的空库里跑一遍核心链路，断言行为没变。

为什么要有它：这 32 轮里我至少改坏过三次 ——
  ① 情景段在并发下丢覆盖（6 个 remember 只覆盖 5 条）
  ② hits 累加写在了排序之前，加到的不是最终前 3
  ③ 评测把 hits 刷到几百，让记录永远逃过衰减
三次都是「改完跑一下、看数字对不上」才发现的。eval.py 能测检索质量，
但测不到「写入 / 衰减 / 分段 / 备份」这些底层行为。这个脚本补那一块。

做法：建一个隔离沙盘（临时目录里放 brain.py + episode.py + schema.sql），
在空库上跑一遍完整链路，逐项断言。**绝不碰真库。**

用法：python test_brain.py        全部通过返回 0
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
# 沙盘放在脚本同级（`.test-sand`，已进 .gitignore）—— **不依赖上级目录结构**。
# 原先用 `dirname(dirname(HERE))/记录/tmp`，2026-11-03 裸机测试当场暴露：
# 把代码拷到别的目录后沙盘落成了 `记录\记录\tmp\_brain_test`（双「记录」）。
# 换机器/换目录是常态，测试环境不该假设自己住在 memory/brain。
SAND = os.environ.get("BRAIN_TEST_SAND") or os.path.join(HERE, ".test-sand")
PY = sys.executable

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %-30s %s" % ("OK" if cond else "XX", name, detail))


def run(args, timeout=180):
    r = subprocess.run([PY, os.path.join(SAND, "brain.py")] + args,
                       capture_output=True, timeout=timeout, cwd=SAND)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def js(args):
    rc, out, err = run(args)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def setup():
    if os.path.isdir(SAND):
        shutil.rmtree(SAND)
    os.makedirs(SAND)
    for f in ("brain.py", "episode.py", "schema.sql"):
        shutil.copy2(os.path.join(HERE, f), os.path.join(SAND, f))
    con = sqlite3.connect(os.path.join(SAND, "brain.db"))
    with open(os.path.join(SAND, "schema.sql"), encoding="utf-8") as fh:
        con.executescript(fh.read())
    con.commit()
    con.close()


def main():
    setup()
    print("沙盘: %s" % SAND)

    print("\n--- 写入 ---")
    a = js(["remember", "--kind", "accident", "--title", "测试事故：把配置改坏了",
            "--weight", "95", "--emotion", "sting", "--source", "test"])
    ok("remember 返回 id", a and a.get("id"), "id=%s" % (a or {}).get("id"))
    b = js(["remember", "--kind", "done", "--title", "测试完成：某个日常任务",
            "--source", "test"])
    ok("第二条写入", b and b.get("id"))

    print("\n--- 检索 ---")
    r = js(["recall", "把配置改坏了", "--limit", "5", "--no-vector"])
    ok("recall 能命中", r and r["verdict"] == "found" and r["count"] >= 1,
       "verdict=%s count=%s" % ((r or {}).get("verdict"), (r or {}).get("count")))
    ok("命中第一条就是它", r and r["hits"] and r["hits"][0]["title"].startswith("测试事故"),
       (r["hits"][0]["title"][:24] if r and r["hits"] else "-"))

    r2 = js(["recall", "紫色独角兽月球饺子馆", "--limit", "5", "--no-vector"])
    ok("否定题返回 no_match", r2 and r2["verdict"] == "no_match_by_these_cues",
       "verdict=%s" % (r2 or {}).get("verdict"))

    print("\n--- 未结项层（open_items 也要能被 recall 查到）---")
    # 2026-11-05 立。由头：五个数据层（events/facts/procedures/constraints/open_items）
    # 前四个都进了 recall，只有 open_items 漏了 —— 问「我说过要做什么还没做」返回 no_match，
    # 而答案就躺在表里。这条断言就是防它再被漏掉。
    o1 = js(["open", "--what", "测试用未结项：验证 recall 能查到它"])
    ok("open 写入", o1 and o1.get("open_item_id"), "id=%s" % (o1 or {}).get("open_item_id"))
    r3 = js(["recall", "我说过要做什么还没做", "--limit", "5", "--no-vector"])
    ok("recall 能查到未结项",
       r3 and (r3.get("open_items") or []), "n=%d" % len((r3 or {}).get("open_items") or []))
    ok("有未结项时 verdict 是 found", r3 and r3.get("verdict") == "found",
       "verdict=%s" % (r3 or {}).get("verdict"))
    r4 = js(["recall", "把配置改坏了", "--limit", "5", "--no-vector"])
    ok("不带待办词时不塞未结项", r4 and not (r4.get("open_items") or []),
       "n=%d" % len((r4 or {}).get("open_items") or []))

    print("\n--- 工作记忆（working）---")
    # 2026-11-05 补。由头：今天改过 working 的背景组排序（weight 主排 → score 主排），
    # 但它是唯一**没有任何测试覆盖**的改动点 —— 不在 eval、不在 realcheck、也不在
    # test_brain 的断言里，只能靠人工看输出。补上最基础的几条。
    w = js(["working"])
    ok("working 能跑", w and w.get("ok"), str((w or {}).get("ok")))
    ok("working 有 attention", w and (w.get("attention") or []),
       "n=%d" % len((w or {}).get("attention") or []))
    ok("working 有 episode_now 字段", w and "episode_now" in (w or {}))
    ok("working 带未结项", w and isinstance(w.get("open_items"), list),
       "n=%d" % len((w or {}).get("open_items") or []))
    # 刚写的那条 open item 应该在其中（沙盘里只有 1 条）
    _ids = [o.get("id") for o in ((w or {}).get("open_items") or [])]
    ok("未结项 id 正确", _ids == [1], str(_ids))

    print("\n--- 禁令的停用/恢复（unconstrain）---")
    # 2026-11-06 补。由头：`constraints.active` 原来 7 处读、零处写 ——
    # 「禁用一条约束」这功能根本不存在。补了 unconstrain 之后必须锁住它，
    # 否则下次谁改坏了 `constraints` 的 where 条件，这里不会出声。
    c1 = js(["constrain", "--what", "沙盘测试约束", "--severity", "soft"])
    cid = (c1 or {}).get("id")
    ok("constrain 建约束", cid, "id=%s" % cid)
    cs = js(["constraints"])
    ok("默认只列 active", cs and cs.get("count") == 1, "count=%s" % (cs or {}).get("count"))
    u1 = js(["unconstrain", str(cid), "--disable"])
    ok("unconstrain --disable 生效", u1 and u1.get("active") == 0,
       "active=%s" % (u1 or {}).get("active"))
    cs2 = js(["constraints"])
    ok("停用后默认视图看不到", cs2 and cs2.get("count") == 0, "count=%s" % (cs2 or {}).get("count"))
    cs3 = js(["constraints", "--all"])
    ok("--all 能看到停用的", cs3 and cs3.get("count") == 1 and cs3.get("active") == 0,
       "count=%s active=%s" % ((cs3 or {}).get("count"), (cs3 or {}).get("active")))
    u2 = js(["unconstrain", str(cid)])
    ok("unconstrain 能恢复", u2 and u2.get("active") == 1, "active=%s" % (u2 or {}).get("active"))

    print("\n--- 情景分段 ---")
    con = sqlite3.connect(os.path.join(SAND, "brain.db"))
    n_ep = con.execute("SELECT count(*) FROM episodes").fetchone()[0]
    covered = set()
    for (ids,) in con.execute("SELECT event_ids FROM episodes"):
        covered |= set(json.loads(ids or "[]"))
    ev = {r[0] for r in con.execute("SELECT id FROM events WHERE superseded=0")}
    con.close()
    ok("episodes 非空", n_ep > 0, "%d 段" % n_ep)
    ok("覆盖全部事件", ev <= covered, "%d/%d" % (len(ev & covered), len(ev)))

    # 2026-11-06 补：MAX_EVENTS 条数闸门（纯函数测，不依赖沙盘数据量）。
    # 由头：原来最大一段 218 条（连续工作 3 小时全粘一起），摘要必然丢细节；
    # 而 `fold_singletons` 合并碎段时**没有长度上限**，会把 segment 切好的段又滚回去
    # —— 所以这层必须同时验 segment 和 fold 两处。
    import importlib.util as _ilu2
    _sp = _ilu2.spec_from_file_location("_epmod", os.path.join(HERE, "episode.py"))
    _ep = _ilu2.module_from_spec(_sp)
    _sp.loader.exec_module(_ep)
    _fake = []
    for _k in range(100):
        _fake.append({"id": _k + 1, "ts": "2026-11-06 10:%02d:00" % (_k % 60),
                      "kind": "done", "title": "写正文 ch%d" % _k,
                      "fingerprint": "done|写文|正文|ch%d" % _k, "weight": 50,
                      "emotion": None})
    _segs = _ep.segment(_fake, set())
    ok("segment 每段不超过 MAX_EVENTS",
       all(len(s["rows"]) <= _ep.MAX_EVENTS for s in _segs),
       "段数=%d max=%d" % (len(_segs), max(len(s["rows"]) for s in _segs)))
    _folded = _ep.fold_singletons(_segs, None)
    ok("fold 之后仍不超过 MAX_EVENTS",
       all(len(s["rows"]) <= _ep.MAX_EVENTS for s in _folded),
       "段数=%d max=%d" % (len(_folded), max(len(s["rows"]) for s in _folded)))
    ok("100 条假事件全部保留（不丢数据）",
       sum(len(s["rows"]) for s in _folded) == 100,
       str(sum(len(s["rows"]) for s in _folded)))

    print("\n--- 衰减 ---")
    # 把两条事件的时间改成 60 天前，再跑 forget
    con = sqlite3.connect(os.path.join(SAND, "brain.db"))
    con.execute("UPDATE events SET ts=datetime('now','localtime','-60 days')")
    con.commit()
    con.close()
    # 2026-11-07：forget 的默认改成了 dry-run（治 6759 —— 当时以为它是只出计划，
    # 实际默认真跑，一次降了 684 条权重）。要验「真降了」必须显式 --apply，
    # 否则这里查到的是原值，断言就成了在测 dry-run 的默认值本身。
    f = js(["forget", "--apply"])
    ok("forget 有候选", f and f["candidates"] >= 1, "candidates=%s" % (f or {}).get("candidates"))
    con = sqlite3.connect(os.path.join(SAND, "brain.db"))
    w = con.execute("SELECT weight FROM events WHERE kind='done'").fetchone()
    # 2026-11-06：降幅从「一律 -10」改成分档（30-90 天 -5 / 90-180 -10 / >180 -15，
    # 碰过一次的减半）。断言随之改成「必须真的降了、且没降到 1 以下」，
    # 不再限定具体降幅——限定它等于把判据钉死在某个版本上。
    bad = con.execute(
        "SELECT count(*) FROM decay_log WHERE new_w >= old_w OR new_w < 1").fetchone()[0]
    con.close()
    ok("完成类被降权", w and w[0] < 50, "w=%s" % (w[0] if w else "-"))
    ok("decay_log 一致", bad == 0, "不合规 %d" % bad)

    print("\n--- 保护规则 ---")
    con = sqlite3.connect(os.path.join(SAND, "brain.db"))
    w_acc = con.execute("SELECT weight FROM events WHERE kind='accident'").fetchone()[0]
    con.close()
    ok("事故永不衰减", w_acc == 95, "w=%d" % w_acc)

    print("\n--- 备份 ---")
    # 必须指定 --to 到沙盘内：不指定的话它会按默认路径落到
    # 记录\archive\brain-backup\ —— 那是**真库**的备份目录，测试不许往里塞东西。
    bk_dir = os.path.join(SAND, "_bk")
    bk = js(["backup", "--to", bk_dir])
    p = (bk or {}).get("path")
    ok("backup 生成文件", p and os.path.exists(p), os.path.basename(p) if p else "-")
    ok("备份落在沙盘内", p and os.path.abspath(p).startswith(os.path.abspath(SAND)),
       os.path.dirname(p) if p else "-")
    if p:
        c = sqlite3.connect(p)
        ok("快照可打开", c.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        c.close()

    print("\n--- _pick_by_sem（纯函数，brief/recall 三层共用）---")
    # 2026-11-05 立。它把「语义挑 + 词面并」收成一个判据，三处共用；
    # 重构时最容易静默改坏的就是这种「到处在用、但没单测」的小函数。
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("_brainmod", os.path.join(HERE, "brain.py"))
    _bm = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_bm)
    _items = [{"id": 1, "t": "改配置先备份"}, {"id": 2, "t": "删除走回收站"},
              {"id": 3, "t": "完全无关的东西"}]
    _r = _bm._pick_by_sem(_items, [0.90, 0.85, 0.30], "t", min_sim=0.50, top_gap=0.10)
    ok("语义挑出高分两条", [x["id"] for x in _r] == [1, 2], str([x["id"] for x in _r]))
    _r2 = _bm._pick_by_sem(_items, [0.90, 0.85, 0.30], "t", min_sim=0.50, top_gap=0.03)
    ok("top_gap 收窄后只留最高分", [x["id"] for x in _r2] == [1],
       str([x["id"] for x in _r2]))
    _r3 = _bm._pick_by_sem(_items, [], "t", hit_fn=lambda s: "回收站" in s)
    ok("无语义时走词面并集", [x["id"] for x in _r3] == [2], str([x["id"] for x in _r3]))
    _r4 = _bm._pick_by_sem(_items, [0.90, 0.85, 0.30], "t", min_sim=0.50, top_gap=0.10,
                           hit_fn=lambda s: "无关" in s)
    ok("词面命中与语义并集不重复", [x["id"] for x in _r4] == [1, 2, 3],
       str([x["id"] for x in _r4]))

    print()
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项: %s" % FAIL)
    # 跑完清掉沙盘（它只有几十 KB，但留在源码目录里会让人误以为是数据）
    try:
        shutil.rmtree(SAND)
    except Exception:
        print("（沙盘没删掉，可手动删: %s）" % SAND)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
