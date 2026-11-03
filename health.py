#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""health.py — 类脑记忆库的全链路体检（一条命令跑完）。

为什么要它：前面十几轮里，每次验收都是临时写一段脚本去查（覆盖率、锁定期、
分段质量……），查完就丢。同一种检查写过三遍以上，就该成命令。

体检项（全部只读，不修数据）：
  1. 表齐全        —— 9 张主体表 + 4 张预留表是否都在
  2. schema 版本   —— meta.schema_version
  3. 事件完整性    —— ts 格式、fingerprint 覆盖率
  4. 情景覆盖      —— episodes.event_ids 的并集是否盖住全部非作废事件
  5. 事实证据有效  —— facts.evidence 里引用的 event id 是否都存在
  6. 衰减一致性    —— decay_log 每条是否满足 new_w = max(1, old_w - 10)
  7. 工作记忆可跑  —— working 能否正常返回 4 条
  8. 元信息        —— current_session / episodes_built_at 等关键键
  9. 目录干净      —— 是否只有源码与数据库
 10. 备份新鲜度    —— 最近一份快照是否在 48 小时内
 11. 库体积        —— 主库 / WAL 大小
 12. 纠错记录      —— 最近一条 correction 是否在 14 天内（人格塑造的信号）

用法：
    python health.py            # 人读
    python health.py --json     # 机读
退出码：0 全过；1 有 FAIL。
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "brain.db")

MAIN_TABLES = ("events", "links", "decay_log", "constraints", "facts",
               "procedures", "episodes", "open_items", "meta")
RESERVED_TABLES = ("sensory_buffer", "motor_trace", "arousal", "sleep_log")
ALLOWED_FILES = {".gitignore", "README.md", "health.py", "eval.py", "eval_cases.json",
                 "eval_synth.py", "eval_vs.py", "test_brain.py", "embed.py", "brain.db",
                 "brain.py", "cluster.py", "consolidate.py", "episode.py",
                 "import_tasklog.py", "schema.sql", "脑区映射.md", "check_readme.py",
                 "embed_server.py", "brain.db-wal", "brain.db-shm"}

RESULTS = []


def add(name, ok, detail):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})


def connect():
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    return con


def check_tables(con):
    have = {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    miss = [t for t in MAIN_TABLES if t not in have]
    miss_r = [t for t in RESERVED_TABLES if t not in have]
    add("表齐全", not miss and not miss_r,
        "主体缺 %s；预留缺 %s" % (miss or "无", miss_r or "无"))


def check_schema(con):
    row = con.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    v = row["v"] if row else None
    add("schema 版本", v is not None, "schema_version=%s" % v)


def check_events(con):
    total = con.execute("SELECT count(*) FROM events WHERE superseded=0").fetchone()[0]
    bad_ts = con.execute(
        "SELECT count(*) FROM events WHERE length(ts) <> 19").fetchone()[0]
    no_fp = con.execute(
        "SELECT count(*) FROM events WHERE superseded=0"
        " AND (fingerprint IS NULL OR fingerprint='')").fetchone()[0]
    ok = total > 0 and bad_ts == 0 and no_fp == 0
    add("事件完整性", ok,
        "非作废 %d 条；ts 格式异常 %d；无指纹 %d" % (total, bad_ts, no_fp))


def check_episodes(con):
    n = con.execute("SELECT count(*) FROM episodes").fetchone()[0]
    ev = {r[0] for r in con.execute("SELECT id FROM events WHERE superseded=0")}
    covered = set()
    broken = 0
    for r in con.execute("SELECT event_ids FROM episodes"):
        try:
            covered |= set(int(i) for i in json.loads(r[0] or "[]"))
        except Exception:
            broken += 1
    miss = ev - covered
    row = con.execute("SELECT v FROM meta WHERE k='episodes_built_at'").fetchone()
    built = row["v"] if row else None

    # 派生视图落后于事件是**正常状态** —— episodes 每 6h 才重建一次，中间新记的事件
    # 自然没被覆盖。只有当漏掉的是「重建之前就已经存在的」事件，才算真问题。
    # 不区分这两者的体检会天天报 FAIL，然后被人忽略。
    stale = 0
    if built:
        for i in miss:
            r = con.execute("SELECT ts FROM events WHERE id=?", (i,)).fetchone()
            if r and r["ts"] and r["ts"] > built:
                stale += 1
    real = len(miss) - stale
    ok = n > 0 and real == 0 and broken == 0
    add("情景覆盖", ok,
        "episodes %d 段；未覆盖 %d（其中 %d 条是 %s 之后新增的，等下次刷新）；"
        "真缺 %d；JSON 损坏 %d" % (n, len(miss), stale, built or "-", real, broken))


def check_evidence(con):
    ids = {r[0] for r in con.execute("SELECT id FROM events")}
    bad = 0
    refs = 0
    for r in con.execute("SELECT claim, evidence FROM facts"):
        try:
            arr = json.loads(r["evidence"] or "[]")
        except Exception:
            bad += 1
            continue
        for i in arr:
            refs += 1
            try:
                if int(i) not in ids:
                    bad += 1
            except Exception:
                bad += 1
    add("事实证据有效", bad == 0, "引用 %d 个 id；无效 %d 个" % (refs, bad))


def check_decay(con):
    bad = 0
    n = 0
    for r in con.execute("SELECT old_w, new_w FROM decay_log"):
        n += 1
        expect = max(1, (r["old_w"] or 0) - 10)
        if r["new_w"] != expect:
            bad += 1
    add("衰减一致性", bad == 0,
        "decay_log %d 条；不符合 max(1,old-10) 的 %d 条" % (n, bad))


def check_working():
    try:
        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "brain.py"), "working"],
            capture_output=True, timeout=120, cwd=HERE)
        d = json.loads(out.stdout.decode("utf-8", "replace"))
        n = len(d.get("attention", []))
        ok = out.returncode == 0 and n > 0
        add("工作记忆可跑", ok, "返回 %d 条（rc=%d）" % (n, out.returncode))
    except Exception as e:
        add("工作记忆可跑", False, "调用失败：%s" % e)


def check_meta(con):
    keys = {r["k"]: r["v"] for r in con.execute("SELECT k,v FROM meta")}
    need = ("schema_version", "episodes_built_at", "current_session")
    miss = [k for k in need if not keys.get(k)]
    add("元信息", not miss, "缺 %s；current_session=%s" % (
        miss or "无", (keys.get("current_session") or "-")[:32]))


def check_dir():
    have = set(os.listdir(HERE))
    extra = sorted(n for n in have if n not in ALLOWED_FILES)
    add("目录干净", not extra, "多余条目：%s" % (extra or "无"))


def check_backup():
    """备份新鲜度。这是「坏了能回退」唯一的证据 —— 没有它，
    前面九项全 PASS 也只是「现在跑得好」，不代表救得回来。

    2026-11-03 实测过一次恢复：把快照拷到沙盘、配好 schema.sql，
    stats / recall / working / brief 四条命令都正常返回。
    顺带发现 brain.py 靠 schema.sql 建表，光拷 brain.db 不够。
    """
    d = os.path.join(os.path.dirname(os.path.dirname(HERE)), "archive", "brain-backup")
    if not os.path.isdir(d):
        add("备份新鲜度", False, "没有快照目录（archive\\brain-backup）—— 库坏了没法还原")
        return
    snaps = sorted(f for f in os.listdir(d) if f.startswith("brain-") and f.endswith(".db"))
    if not snaps:
        add("备份新鲜度", False, "快照目录是空的")
        return
    newest = os.path.join(d, snaps[-1])
    age_h = (time.time() - os.path.getmtime(newest)) / 3600.0
    ok = age_h <= 48          # 6h 一跑，48h 还不到就是断了两天
    add("备份新鲜度", ok, "最新 %s（%.1f 小时前，共 %d 份%s）" % (
        snaps[-1], age_h, len(snaps), "" if ok else "，已超 48 小时"))


def check_corrections(con):
    """纠错记录的新鲜度：人格塑造靠「被纠正就记」，断了没人知道。

    只判「连续 14 天一条 correction 都没有」—— 不是每天必须有，但两周都没有，
    说明写入纪律已经断了。2026-11-03 实测：全库只有 1 条 correction，
    而最近三天落云纠正过两次 —— 不是没纠正，是没记。
    """
    row = con.execute(
        "SELECT max(ts) FROM events WHERE superseded=0 AND kind='correction'").fetchone()
    last = row[0] if row else None
    if not last:
        add("纠错记录", False, "库里一条 correction 都没有")
        return
    try:
        age_days = (time.time() - time.mktime(
            time.strptime(last[:19], "%Y-%m-%d %H:%M:%S"))) / 86400.0
    except Exception:
        add("纠错记录", False, "最近一条 correction 的时间格式异常: %r" % last)
        return
    ok = age_days <= 14
    add("纠错记录", ok, "最近一条 correction %s（%.1f 天前）%s" % (
        last, age_days, "" if ok else "—— 两周没记过纠正，写入纪律断了"))


def check_size():
    if not os.path.exists(DB):
        add("库体积", False, "brain.db 不存在")
        return
    mb = os.path.getsize(DB) / 1048576.0
    wal = DB + "-wal"
    wmb = (os.path.getsize(wal) / 1048576.0) if os.path.exists(wal) else 0.0
    add("库体积", True, "brain.db %.1f MB；WAL %.1f MB" % (mb, wmb))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(DB):
        print(json.dumps({"ok": False, "error": "brain.db 不存在"}, ensure_ascii=False))
        return 1

    con = connect()
    for fn in (check_tables, check_schema, check_events, check_episodes,
               check_evidence, check_decay, check_meta, check_corrections):
        try:
            fn(con)
        except Exception as e:
            add(fn.__name__, False, "检查本身出错：%s" % e)
    con.close()
    try:
        check_working()
    except Exception as e:
        add("工作记忆可跑", False, str(e))
    check_dir()
    check_backup()
    check_size()

    fails = [r for r in RESULTS if not r["ok"]]
    verdict = "PASS" if not fails else "FAIL"

    if a.json:
        print(json.dumps({"verdict": verdict, "checks": RESULTS}, ensure_ascii=False, indent=2))
    else:
        print("brain 体检 —— %d 项" % len(RESULTS))
        for r in RESULTS:
            print("  [%s] %-12s %s" % ("OK " if r["ok"] else "XX", r["check"], r["detail"]))
        print()
        print("VERDICT: %s（失败 %d 项）" % (verdict, len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
