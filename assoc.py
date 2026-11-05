#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""assoc.py — 语义近邻边（links.rel='similar'）。

为什么单独一个脚本：算近邻要 numpy 和全库向量，那是 comfyui venv 才有的。
brain.py 要保持零依赖、要快，所以边在这里离线建好，它只负责走。

为什么必须定时跑（2026-11-05 补）：
    recall 的「联想」功能会顺着 similar 边走一跳（brain.py 里那段 UNION 查询），
    但**整个工作区没有任何代码生成这种边** —— 实测库里 7246 条 similar 边
    全部写于 2026-11-05 18:22:56 那一次（来源已不可考），之后新增的事件
    一条都没连上。没有定时重建，边只会越来越旧，联想功能会慢慢失效而又不报错。
    这跟「6 小时刷新链漏了建向量那一步」是同一类病：**派生视图有消费者、
    没生产者**。

规则：每条事件取最近的 K 条（sim >= MIN_SIM），双向各写一条。
增量：只处理「还没有任何 similar 边」的事件，所以常规运行很快。
用法（**必须用 comfyui venv 的 python 跑**，它才有 numpy）：
    <torch-venv>/Scripts/python.exe assoc.py --build
    ... assoc.py --stats
    ... assoc.py --rebuild      # 全量重算（边规则改了才用）
"""

import argparse
import os
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("BRAIN_DB") or os.path.join(HERE, "brain.db")
MIN_SIM = float(os.environ.get("BRAIN_ASSOC_MIN_SIM") or 0.78)
TOPK = int(os.environ.get("BRAIN_ASSOC_TOPK") or 4)
NOTE = "auto:assoc"


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def load_vectors(con):
    rows = list(con.execute(
        "SELECT m.id, m.v FROM event_emb m JOIN events e ON e.id = m.id"
        " WHERE e.superseded = 0 ORDER BY m.id"))
    if not rows:
        return [], None
    ids = [r["id"] for r in rows]
    mat = np.vstack([np.frombuffer(r["v"], dtype=np.float32) for r in rows])
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    return ids, mat


def cmd_build(a):
    con = connect()
    ids, mat = load_vectors(con)
    if not ids:
        print("没有向量，先跑 embed.py --build")
        return 0
    print("向量 %d 条" % len(ids))

    have = set()
    for r in con.execute("SELECT a, b FROM links WHERE rel='similar'"):
        have.add(r["a"])
        have.add(r["b"])
    if a.rebuild:
        todo = list(range(len(ids)))
        con.execute("DELETE FROM links WHERE rel='similar'")
    else:
        todo = [i for i, eid in enumerate(ids) if eid not in have]
    print("已有 similar 边的事件 %d 条；本次要连 %d 条" % (len(have), len(todo)))
    if not todo:
        print("没有要连的")
        con.close()
        return 0

    t0 = time.time()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    written = 0
    # 分批算，避免一次性 materialize 整个 n×n
    sub = mat[todo]
    sims = sub @ mat.T
    for row_i, pos in enumerate(todo):
        eid = ids[pos]
        sims[row_i, pos] = -1.0  # 不连自己
        order = np.argsort(-sims[row_i])[:TOPK]
        for j in order:
            s = float(sims[row_i, j])
            if s < MIN_SIM:
                break
            other = ids[int(j)]
            con.execute(
                "INSERT OR REPLACE INTO links (a,b,rel,note,created_at) VALUES (?,?,?,?,?)",
                (eid, other, "similar", "%.3f" % s, now))
            con.execute(
                "INSERT OR REPLACE INTO links (a,b,rel,note,created_at) VALUES (?,?,?,?,?)",
                (other, eid, "similar", "%.3f" % s, now))
            written += 2
        if (row_i + 1) % 500 == 0:
            con.commit()
            print("  已处理 %d/%d，写入 %d 条边" % (row_i + 1, len(todo), written))
    con.commit()
    print("完成：%d 条边，%.1fs" % (written, time.time() - t0))
    con.close()
    return 0


def cmd_stats(a):
    con = connect()
    for r in con.execute("SELECT rel, COUNT(*) n FROM links GROUP BY rel ORDER BY n DESC"):
        print("  %-10s %d" % (r[0], r[1]))
    n_ev = con.execute("SELECT COUNT(*) FROM events WHERE superseded=0").fetchone()[0]
    n_cov = con.execute(
        "SELECT COUNT(DISTINCT e.id) FROM events e WHERE e.superseded=0 AND EXISTS"
        " (SELECT 1 FROM links l WHERE l.rel='similar' AND (l.a=e.id OR l.b=e.id))").fetchone()[0]
    print("  覆盖：%d/%d 条事件有语义边（%.1f%%）" % (n_cov, n_ev, 100.0 * n_cov / max(1, n_ev)))
    con.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="补建（只处理还没有边的事件）")
    ap.add_argument("--rebuild", action="store_true", help="清空 similar 边后全量重算")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.rebuild:
        return cmd_build(a)
    if a.build:
        return cmd_build(a)
    return cmd_stats(a)


if __name__ == "__main__":
    sys.exit(main())
