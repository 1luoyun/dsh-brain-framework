#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""embed.py — 给事件算语义向量（bge-small-zh），存进 brain.db 的 event_emb 表。

为什么单独一个脚本：算向量要 torch + transformers，那是 comfyui venv 才有的
（系统 Python 没有）。brain.py 要零依赖、要快，不能背这个包袱 ——
所以向量在这里离线算好，brain.py 只负责查。

用法（**必须用装了 torch 的那个解释器跑**，不是跑 brain.py 的那个）：
    <装了 torch 的 python> embed.py --build     # 建/补索引
    ... embed.py --stats                        # 看索引状态
    ... embed.py --query "一句话" --top 10       # 试查

例：本机用的是一个独立 venv（`<venv>/Scripts/python.exe`），因为它装了 CUDA 版 torch，
而系统 Python 没有 —— brain.py 本身仍然零依赖，只有本脚本要这个环境。

模型：`BRAIN_EMBED_MODEL` 可覆盖（本地目录或 HF 模型名）；默认 `BAAI/bge-small-zh-v1.5`
（92 MB，512 维，CPU 约 780 条/秒），transformers 首次运行会自动下载。
"""

import argparse
import json
import os
import sqlite3
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
import torch  # noqa: E402
from transformers import AutoModel, AutoTokenizer  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("BRAIN_DB") or os.path.join(HERE, "brain.db")
# 2026-11-03 开源准备：原来写死本机路径 `<workspace>\models\...`，换机器直接崩。
# 现在默认给 HF 模型名（自动下载），需要离线/自备模型时用环境变量指本地目录。
MODEL = os.environ.get("BRAIN_EMBED_MODEL") or "BAAI/bge-small-zh-v1.5"
BATCH = 64


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def ensure_table(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS event_emb ("
        "  id     INTEGER PRIMARY KEY REFERENCES events(id),"
        "  dim    INTEGER NOT NULL,"
        "  v      BLOB    NOT NULL,"
        "  model  TEXT,"
        "  built_at TEXT)")
    con.commit()


class Encoder:
    def __init__(self):
        t0 = time.time()
        self.tok = AutoTokenizer.from_pretrained(MODEL)
        self.model = AutoModel.from_pretrained(MODEL)
        self.model.eval()
        self.load_s = time.time() - t0

    def encode(self, texts):
        enc = self.tok(texts, padding=True, truncation=True, max_length=512,
                       return_tensors="pt")
        with torch.no_grad():
            out = self.model(**enc)
        emb = out.last_hidden_state[:, 0]          # bge 用 CLS pooling
        emb = torch.nn.functional.normalize(emb, dim=-1)
        return emb.numpy().astype("float32")


def pack(v):
    return struct.pack("<%df" % len(v), *v.tolist())


def unpack(blob):
    n = len(blob) // 4
    return np.array(struct.unpack("<%df" % n, blob), dtype="float32")


def text_of(row):
    return " ".join(x for x in (row["title"], row["body"]) if x)[:1000]


def cmd_build(a):
    con = connect()
    ensure_table(con)
    rows = list(con.execute(
        "SELECT id, title, body FROM events WHERE superseded=0 ORDER BY id"))
    have = {r[0] for r in con.execute("SELECT id FROM event_emb")}
    todo = [r for r in rows if r["id"] not in have]
    print("事件 %d 条，已有向量 %d，待算 %d" % (len(rows), len(have), len(todo)))
    if not todo:
        print("没有要算的")
        return 0

    enc = Encoder()
    print("模型加载 %.1fs" % enc.load_s)
    t0 = time.time()
    done = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        vecs = enc.encode([text_of(r) for r in chunk])
        for r, v in zip(chunk, vecs):
            con.execute(
                "INSERT OR REPLACE INTO event_emb(id,dim,v,model,built_at) VALUES (?,?,?,?,?)",
                (r["id"], len(v), pack(v), "bge-small-zh-v1.5",
                 time.strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        done += len(chunk)
        if done % (BATCH * 10) == 0:
            print("  已算 %d / %d" % (done, len(todo)))
    dt = time.time() - t0
    print("完成 %d 条，%.1fs（%.0f 条/秒）" % (done, dt, done / max(dt, 1e-6)))
    return 0


def cmd_stats(a):
    con = connect()
    ensure_table(con)
    n = con.execute("SELECT count(*) FROM event_emb").fetchone()[0]
    tot = con.execute("SELECT count(*) FROM events WHERE superseded=0").fetchone()[0]
    dim = con.execute("SELECT dim FROM event_emb LIMIT 1").fetchone()
    size = con.execute("SELECT sum(length(v)) FROM event_emb").fetchone()[0] or 0
    print(json.dumps({"events": tot, "embedded": n, "missing": tot - n,
                      "dim": dim[0] if dim else None,
                      "size_mb": round(size / 1048576.0, 1)}, ensure_ascii=False))
    return 0


def cmd_query(a):
    con = connect()
    t0 = time.time()
    enc = Encoder()
    qv = enc.encode([a.query])[0]
    # 一次读全部 BLOB 再 frombuffer —— 逐条 struct.unpack 慢得离谱
    # （实测 4560 条要 5 秒以上，这样改完是毫秒级）
    ids, blobs = [], []
    for r in con.execute("SELECT id, v FROM event_emb ORDER BY id"):
        ids.append(r["id"])
        blobs.append(r["v"])
    if not blobs:
        print(json.dumps({"ok": False, "error": "还没有向量索引，先 --build"}))
        return 1
    dim = len(blobs[0]) // 4
    m = np.frombuffer(b"".join(blobs), dtype="float32").reshape(-1, dim)
    sims = m @ qv
    order = np.argsort(-sims)[:a.top]
    hit = [{"id": ids[k], "sim": round(float(sims[k]), 4)} for k in order]
    dt = time.time() - t0

    if a.json:
        print(json.dumps({"ok": True, "query": a.query, "indexed": len(ids),
                          "elapsed_s": round(dt, 2), "hits": hit}, ensure_ascii=False))
        return 0

    print("query=%r  %d 条已索引  耗时 %.2fs" % (a.query, len(ids), dt))
    print()
    for h in hit:
        row = con.execute("SELECT ts,kind,weight,title FROM events WHERE id=?",
                          (h["id"],)).fetchone()
        if not row:
            continue
        print("  %.3f  #%-5d %s w=%-3d %s" % (
            h["sim"], h["id"], row["ts"][:10], row["weight"], (row["title"] or "")[:56]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--query")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.build:
        return cmd_build(a)
    if a.stats:
        return cmd_stats(a)
    if a.query:
        return cmd_query(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
