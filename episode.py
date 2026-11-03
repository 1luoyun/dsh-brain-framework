#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""episode.py — 情景记忆：把零散事件切成「一次经历」。

人脑依据（学原理，不搬代码）：
  · 事件分割理论（Zacks & Swallow 2007）：连续经验在「预测误差骤增」处被切成
    离散事件；边界处工作记忆内容几乎全部换掉。
  · 重复抑制（repetition suppression）：同一动作重复发生时响应递减 —— 所以
    「写正文 ch62」连记 15 次，在记忆里是一个动作做了 15 遍，不是 15 段经历。
  · 海马情景绑定：一个 episode 自带时间范围、情绪标记与显著度。

判据：fingerprint 的主题连续性。与最近 K 条的主题集有交集 → 同一段；
      无交集 → 新段（等价于「预测误差骤增」）。
时间闸门：同一段内相邻事件间隔不超过 MAX_GAP 分钟（跨天必断）。

用法：
  python episode.py --dry          # 只看分割结果与统计，不写库
  python episode.py --apply        # 重建 episodes 表
  python episode.py --show [N]     # 看已入库的情景
"""

import argparse
import collections
import datetime
import json
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "brain.db")

K_RECENT = 3        # 主题记忆保留最近几条
OVERLAP_MIN = 2     # 长标题要求的最小共享元素数（短标题自适应降到 1）
MAX_GAP = 360       # 同一段内相邻事件最长间隔（分钟），6 小时
STOP_RATIO = 0.10   # 出现在 >10% 事件里的元素不带区分度，不算主题（实测"写文/正文/章节"各占 71%）
MIN_MERGE = 2       # 少于这个条数的段，尝试并入相邻段（单调流水不该独占一段）


def raw_tokens(fingerprint, kind):
    """fingerprint = kind|tok|tok|...  ->  全部元素（不含 kind）"""
    if not fingerprint:
        return set()
    parts = [p for p in fingerprint.split("|") if p]
    if parts and parts[0] == kind:
        parts = parts[1:]
    return set(parts)


def build_stop(con):
    """哪些元素是泛词。

    人脑记"那次"靠的是区分度，不是出现频率：一条线索里若有"写文/正文/章节"
    这种 71% 的事都带有的词，它对"是哪一次"毫无贡献，反而会把不相干的事
    全粘成一段（实测：10-10 那天 1217 条事件被粘成一个 episode）。
    """
    df = collections.Counter()
    n = 0
    for kind, fp in con.execute("SELECT kind, fingerprint FROM events WHERE superseded = 0"):
        n += 1
        for t in raw_tokens(fp, kind):
            df[t] += 1
    if n == 0:
        return set(), df, n
    return {t for t, c in df.items() if c / n > STOP_RATIO}, df, n


def tokens(fingerprint, kind, stop):
    return raw_tokens(fingerprint, kind) - stop


def parse_ts(ts):
    try:
        return datetime.datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def segment(rows, stop):
    """把按时间排好序的行切成段。

    每段是一个 dict：
      theme  该段的主题并集（只保留最近 K_RECENT 条的贡献）
      recent 最近 K_RECENT 条的主题集合列表
      rows   事件行
    """
    segs = []
    cur = None
    prev_ts = None

    for r in rows:
        ts = parse_ts(r["ts"])
        if ts is None:
            continue
        tok = tokens(r["fingerprint"], r["kind"], stop)

        gap_ok = prev_ts is not None and (ts - prev_ts).total_seconds() / 60.0 <= MAX_GAP
        same_day = prev_ts is not None and prev_ts.date() == ts.date()
        theme = set()
        for s in (cur["recent"] if cur else []):
            theme |= s
        # 交集门槛：短标题元素少（"自检"这种），共享 1 个就够了；
        # 长标题元素多，共享 1 个多半是「修复」「文件」这种泛词，要求 ≥2。
        inter = tok & theme
        need = 1 if len(tok) <= 2 else OVERLAP_MIN
        overlap = len(inter) >= need

        if cur is None or not (gap_ok and same_day and overlap):
            cur = {"recent": [], "rows": []}
            segs.append(cur)

        cur["rows"].append(r)
        cur["recent"].append(tok)
        if len(cur["recent"]) > K_RECENT:
            cur["recent"].pop(0)
        prev_ts = ts

    return segs


def fold_singletons(segs, rows_by_id):
    """把只有 1 条的碎片并入相邻段（同一小时内优先，否则不动）。

    「写正文 ch62」这样的单条流水，在记忆里属于隔壁那段，不该单独成段。
    """
    if len(segs) < 2:
        return segs
    out = []
    for i, s in enumerate(segs):
        if len(s["rows"]) >= MIN_MERGE or not out:
            out.append(s)
            continue
        prev = out[-1]
        a = parse_ts(prev["rows"][-1]["ts"])
        b = parse_ts(s["rows"][0]["ts"])
        if a and b and (b - a).total_seconds() / 60.0 <= 60:
            prev["rows"].extend(s["rows"])
            prev["recent"] = (prev["recent"] + s["recent"])[-K_RECENT:]
        else:
            out.append(s)
    return out


def summarize(seg):
    rows = seg["rows"]
    kinds = collections.Counter(r["kind"] for r in rows)
    titles = collections.Counter(r["title"] for r in rows)
    top_title, top_n = titles.most_common(1)[0]
    heaviest = max(rows, key=lambda r: (r["weight"] or 0, r["id"]))
    weight = max(r["weight"] or 0 for r in rows)
    emotions = [r["emotion"] for r in rows if r["emotion"]]
    emotion = collections.Counter(emotions).most_common(1)[0][0] if emotions else None

    # 标题：重复的写「动作 ×次数」，否则用最重的那条
    if top_n >= 3 and len(titles) <= max(2, len(rows) // 4):
        head = "%s ×%d" % (top_title, top_n)
    else:
        head = heaviest["title"]

    kind_main = kinds.most_common(1)[0][0]
    span = ""
    a, b = parse_ts(rows[0]["ts"]), parse_ts(rows[-1]["ts"])
    if a and b:
        if a.date() == b.date():
            span = "%s %s-%s" % (a.strftime("%m-%d"), a.strftime("%H:%M"), b.strftime("%H:%M"))
        else:
            span = "%s~%s" % (a.strftime("%m-%d %H:%M"), b.strftime("%m-%d %H:%M"))

    return {
        "started_at": rows[0]["ts"],
        "ended_at": rows[-1]["ts"],
        "summary": head,
        "kind_main": kind_main,
        "event_count": len(rows),
        "weight": weight,
        "emotion": emotion,
        "span": span,
        "titles": [t for t, _ in titles.most_common(4)],
    }


def load_events(con, since=None):
    sql = ("SELECT id, ts, kind, title, body, weight, emotion, fingerprint "
           "FROM events WHERE superseded = 0")
    args = ()
    if since:
        sql += " AND ts >= ?"
        args = (since,)
    sql += " ORDER BY ts, id"
    return list(con.execute(sql, args))


def rebuild(con=None):
    """重建 episodes。实测 866 段只要 0.18 秒 —— 比一次 recall 还快，
    所以别的命令可以随手调它（brain.py remember 就调了）。

    不重建的代价：SELF.md 里「此刻在哪段经历里」会一直指向旧的段。
    2026-11-03 实测：一个会话里记了十几条，episode_now 还停在几小时前那段，
    工作记忆和「现在在干什么」完全割裂。
    """
    own = con is None
    if own:
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
    # 重建是 DELETE + 全量 INSERT。并发跑会互相踩：2026-11-03 实测，
    # 同时起 6 个 remember，最后只覆盖到 5 条 —— 后一个进程读的快照里
    # 还没有前一个刚写的那条，于是把它覆盖掉了。
    # BEGIN IMMEDIATE 先拿写锁再读，后来的会排队等（比丢数据好）。
    try:
        con.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        pass          # 已经在事务里（比如调用方刚 commit 完紧接着进来）
    stop, _df, _n = build_stop(con)
    rows = load_events(con)
    segs = fold_singletons(segment(rows, stop), None)
    eps = [summarize(s) for s in segs]
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con.execute("DELETE FROM episodes")
    # 关系本身也是记忆：同一段经历里的相邻事件天然是「先后发生」。
    # 之前 links 表一直是空的（4568 条事件、0 条关系）—— 等于只有点没有边，
    # 「互相关联的完整结构」缺的就是这一层。
    # 只重建 rel='follows' 且是自动生成的那一批；人手动 link 的其它关系不动。
    con.execute("DELETE FROM links WHERE rel='follows' AND note='auto:episode'")
    n = 0
    n_link = 0
    for seg, e in zip(segs, eps):
        ids = [r["id"] for r in seg["rows"]]
        con.execute(
            "INSERT INTO episodes(session, started_at, ended_at, summary, kind_main, "
            "event_count, weight, emotion, event_ids, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (None, e["started_at"], e["ended_at"], e["summary"], e["kind_main"],
             e["event_count"], e["weight"], e["emotion"], json.dumps(ids), now))
        n += 1
        for x, y in zip(ids, ids[1:]):
            con.execute(
                "INSERT OR IGNORE INTO links(a,b,rel,note,created_at) VALUES (?,?,?,?,?)",
                (x, y, "follows", "auto:episode", now))
            n_link += 1
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('episodes_built_at', ?)", (now,))
    con.commit()
    if own:
        con.close()
    return {"events": len(rows), "episodes": len(eps), "written": n, "links": n_link}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--show", nargs="?", const=8, type=int)
    ap.add_argument("--since")
    a = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    if a.show:
        n = a.show
        total = con.execute("SELECT count(*) FROM episodes").fetchone()[0]
        print(json.dumps({"episodes_total": total, "sample": [
            {k: r[k] for k in r.keys()} for r in
            con.execute("SELECT * FROM episodes ORDER BY started_at DESC LIMIT ?", (n,))
        ]}, ensure_ascii=False, indent=2))
        return 0

    if a.apply:
        con.close()
        print(json.dumps(dict(rebuild(), ok=True), ensure_ascii=False, indent=2))
        return 0

    stop, df, n_ev = build_stop(con)
    rows = load_events(con, a.since)
    segs = fold_singletons(segment(rows, stop), None)
    eps = [summarize(s) for s in segs]

    sizes = collections.Counter()
    for e in eps:
        n = e["event_count"]
        key = "1" if n == 1 else ("2-4" if n <= 4 else ("5-19" if n <= 19 else "20+"))
        sizes[key] += 1

    out = {
        "ok": True,
        "events": len(rows),
        "episodes": len(eps),
        "stop_words": sorted(stop),
        "size_buckets": dict(sizes),
        "avg_events_per_episode": round(len(rows) / max(1, len(eps)), 2),
        "dry": True,
        "sample": eps[:8],
    }

    con.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
