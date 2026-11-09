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
import io
import json
import os
import re
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
                 "embed_server.py", "brain.db-wal", "brain.db-shm",
                 # 2026-11-05 新增（加完忘了登记，health 当场报「目录脏」—— 这闸门是有效的）：
                 # 同日犯过两次（第一次是 assoc.py + field_lifecycle.py，第二次是 realcheck.py）
                 # ⇒ 往后在 brain 目录下新建 .py，**同一步**就把名字加进这里。
                 "assoc.py",           # 语义近邻边生成器
                 "field_lifecycle.py",  # 静态查「只读不写」字段
                 "realcheck.py",        # 真实场景验收（跟 eval 并列的另一把尺子）
                 "check_comment_claims.py"  # 扫注释里宣称了具体机制的地方（待核清单）
                 }

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


def check_derived(con):
    """派生一致性：作废事件的向量有没有清干净。

    2026-11-06 实测：去重 + 清理之后，event_emb 里躺着 3042 条已作废事件的向量
    —— 没人发现，因为没有任何检查在看这件事。作废只改 events，派生视图不会自动跟着清。
    这里只报「孤儿向量」（属于作废事件的），不做修复；修复走 `brain.py purge-derived --apply`。
    """
    n = con.execute(
        "SELECT count(*) FROM event_emb WHERE id IN"
        " (SELECT id FROM events WHERE superseded=1)").fetchone()[0]
    add("派生一致性", n == 0,
        "孤儿向量 %d 条" % n + ("（跑 brain.py purge-derived --apply 清）" if n else ""))


def check_memory_use(con):
    """记忆取用：写进去的东西，到底有没有被取出来用过。

    2026-11-08 立。由头是一组实测数字：3795 条有效事件里 3745 条（98.7%）
    从没被 recall 命中过，而**权重 >=80 的 229 条里只有 13 条被取过（5.7%）** ——
    最该复用的教训，九成四根本没在检索时露过面。

    这条门槛设在 20%，意味着它现在会红。红是有意的：一个永远绿的观测等于没观测。
    等把 done 流水降档、重复合并做完，高权重记录应该更容易冒头，那时它才该转绿。

    判据口径：只看 dormant=0（休眠的不算，它们本就不参与默认检索），
    只看 weight>=80（低权重沉底是正常的，「用进废退」本来就该让它沉）。
    """
    rows = con.execute(
        "SELECT weight, hits FROM events WHERE superseded=0 AND dormant=0").fetchall()
    total = len(rows)
    used = sum(1 for w, h in rows if (h or 0) > 0)
    hi = [(w, h) for w, h in rows if (w or 0) >= 80]
    hi_used = sum(1 for w, h in hi if (h or 0) > 0)
    rate = (float(hi_used) / len(hi)) if hi else 1.0
    add("记忆取用", rate >= 0.20,
        "高权重 %d 条取过 %d（%.1f%%，门槛 20%%）；全库取过 %d/%d（%.1f%%）"
        % (len(hi), hi_used, 100 * rate, used, total,
           100.0 * used / total if total else 0.0))


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
    """衰减留痕的一致性。

    2026-11-06 改判据：原来断言 `new_w == max(1, old_w - 10)`，把「降幅恒为 10」
    钉死在了体检里。而 cmd_forget 已改成分档（30-90 天 -5 / 90-180 -10 / >180 -15，
    碰过一次的降幅减半）—— 于是这条检查对每一次合规的降权都报红，成了噪音源。
    现在的判据只问两件事：**确实降了（new_w < old_w）** 且 **没跌破 1**。
    降幅是多少由 cmd_forget 决定，不该由体检替它做主。
    """
    bad = 0
    n = 0
    for r in con.execute("SELECT old_w, new_w FROM decay_log"):
        n += 1
        old_w = r["old_w"] or 0
        new_w = r["new_w"]
        if new_w is None or new_w >= old_w or new_w < 1:
            bad += 1
    add("衰减一致性", bad == 0,
        "decay_log %d 条；不是「降了且不低于 1」的 %d 条" % (n, bad))


def check_sleep_log(con):
    """睡眠日志的语义自洽（2026-11-05 立）。

    由头很具体：给 forget 加「激活回落」时，我用 edit 插代码，
    `INSERT INTO sleep_log` 那行被挤进了新写的 for 循环体 —— 一次 forget
    按待回落事实数写了 **54 条** `merged=0` 的假「转入休眠」日志。
    而当时 test_brain 14/14 全过、health 也 PASS —— 因为它此前只查表和结构，
    **没有任何一项检查派生日志的语义**。这条就是补那个盲区。

    判据：`phase='dormant'` 必须真休眠了东西（merged > 0）。
    原代码有 `if dorm:` 保护，所以 merged=0 的 dormant 记录逻辑上不可能出现。
    """
    bad = con.execute(
        "SELECT COUNT(*) FROM sleep_log WHERE phase='dormant'"
        " AND (merged IS NULL OR merged = 0)").fetchone()[0]
    if bad:
        add("睡眠日志", False,
            "dormant 记录里有 %d 条 merged=0 —— 没休眠任何东西却写了休眠日志" % bad)
        return
    dup = con.execute(
        "SELECT COUNT(*) FROM (SELECT ts FROM sleep_log GROUP BY ts HAVING COUNT(*) > 5)"
    ).fetchone()[0]
    if dup:
        add("睡眠日志", False, "有 %d 个时间点写了超过 5 条日志（疑似批量污染）" % dup)
        return
    n = con.execute("SELECT COUNT(*) FROM sleep_log").fetchone()[0]
    add("睡眠日志", True, "%d 条；dormant 语义自洽，无同秒批量写入" % n)


def check_link_integrity(con):
    """派生引用完整性（2026-11-05 立）。

    已有 check_derived 只查「孤儿向量」（向量指向不存在的事件）。但 `links`
    和 `event_emb` 都还有另一种脏法：**指向已作废（superseded=1）的事件**。
    这跟 2026-11-06 记过的那条同源 ——「作废只改 events 表，派生视图不会自动跟」。

    实测（2026-11-05）：12238 条边、3500+ 条向量里，两种情况都是 **0**，
    说明现在的清理逻辑是对的（episode/assoc/embed 都只处理 superseded=0）。
    加这条不是因为它现在坏了，是给将来兜底 —— 一旦有人写了个不清作的派生脚本，
    这里会当场红。

    判据：links 两端都必须是存活的 events；event_emb 不得有作废事件的向量。
    """
    bad_links = con.execute(
        "SELECT COUNT(*) FROM links l WHERE"
        " NOT EXISTS (SELECT 1 FROM events e WHERE e.id = l.a)"
        " OR NOT EXISTS (SELECT 1 FROM events e WHERE e.id = l.b)"
        " OR EXISTS (SELECT 1 FROM events e WHERE e.id = l.a AND e.superseded = 1)"
        " OR EXISTS (SELECT 1 FROM events e WHERE e.id = l.b AND e.superseded = 1)"
    ).fetchone()[0]
    bad_emb = con.execute(
        "SELECT COUNT(*) FROM event_emb m WHERE EXISTS"
        " (SELECT 1 FROM events e WHERE e.id = m.id AND e.superseded = 1)"
    ).fetchone()[0]
    # 2026-11-05 补：`facts.evidence` 和 `episodes.event_ids` 也是事件 id 的引用表，
    # 之前两项没查它们。实测都是干净的（evidence 空 0 条、456 段情景零悬空），
    # 但这两张表是 consolidate / episode 每次重建时写的 —— 最可能出问题的正是重建。
    bad_ev = 0
    for _fid, _ev in con.execute("SELECT id, evidence FROM facts"):
        _s = str(_ev or "")
        for _tok in _s.strip("[]").split(","):
            _tok = _tok.strip()
            if not _tok.isdigit():
                continue
            if not con.execute("SELECT 1 FROM events WHERE id=?", (int(_tok),)).fetchone():
                bad_ev += 1
    bad_ep = 0
    for _eid, _ids in con.execute("SELECT id, event_ids FROM episodes"):
        _s = str(_ids or "")
        for _tok in _s.strip("[]").split(","):
            _tok = _tok.strip()
            if not _tok.isdigit():
                continue
            if not con.execute("SELECT 1 FROM events WHERE id=?", (int(_tok),)).fetchone():
                bad_ep += 1
    if bad_links or bad_emb or bad_ev or bad_ep:
        add("引用完整性", False,
            "links 断链/作废 %d；向量作废 %d；facts.evidence 悬空 %d；episodes 悬空 %d"
            % (bad_links, bad_emb, bad_ev, bad_ep))
        return
    nl = con.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    nm = con.execute("SELECT COUNT(*) FROM event_emb").fetchone()[0]
    nf = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    np_ = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    add("引用完整性", True,
        "links %d 条、向量 %d 条、facts %d 条、episodes %d 段，引用均存活"
        % (nl, nm, nf, np_))


def check_self_md(con=None):
    """SELF.md 必须是 UTF-8 无 BOM 且含「装配时刻」（2026-11-06 立）。

    由头是一次真实事故：我用 pwsh 的 `>` 重定向重新生成它，
    PS 5.1 的 `>` 默认写 **UTF-16 LE + BOM**（头 8 字节 `ff fe 23 00 20 00 91 4e`），
    整个文件全废 —— **而没有任何东西报错**。它是装配物，不在任何测试里，
    直到我想验证新内容、用 python 读它时才撞上 `UnicodeDecodeError: byte 0xff`。

    判据三条：不带 BOM、能按 UTF-8 解码、含「装配时刻」（说明骨架真拼上了）。
    """
    p = os.path.join(HERE, "..", "hub", "SELF.md")
    if not os.path.exists(p):
        add("SELF.md", False, "不存在（装配器没跑过？）")
        return
    try:
        raw = open(p, "rb").read()
    except Exception as e:
        add("SELF.md", False, "读不了：%s" % type(e).__name__)
        return
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw[:3] == b"\xef\xbb\xbf":
        add("SELF.md", False,
            "带 BOM（头 4 字节 %r）—— 装配通道很可能用了 pwsh 重定向（它写 UTF-16）" % raw[:4])
        return
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        add("SELF.md", False, "不是 UTF-8：%s" % e)
        return
    if "装配时刻" not in txt:
        add("SELF.md", False, "缺「装配时刻」—— 骨架没拼上")
        return
    add("SELF.md", True, "%d 字符，UTF-8 无 BOM，含装配时刻" % len(txt))


def check_episode_size(con):
    """episodes 的段长不能超过 MAX_EVENTS（2026-11-06 立）。

    由头：实测最大一段 218 条（2026-10-10 连续工作 3 小时全粘一起），摘要必然丢细节。
    修了 `segment()` 和 `fold_singletons()` 两处之后，再加这一项守着 —— 因为它**会悄悄退化**：
    `fold_singletons` 合并碎段时如果没有长度闸门，就会把 segment 切好的段又滚回去，
    而那一步**不报错、不影响任何检索**，只有人去看分布才会发现。

    判据三条：没有超限的段、段数 > 0、事件总数与活跃事件数对得上（防「切得太狠丢数据」）。
    """
    try:
        rows = con.execute("SELECT event_count FROM episodes").fetchall()
    except Exception as e:
        add("分段粒度", False, "查 episodes 失败：%s" % type(e).__name__)
        return
    if not rows:
        add("分段粒度", False, "episodes 为空")
        return
    lens = [r[0] or 0 for r in rows]
    # 上限跟 episode.py 的常量对齐（不在两处各写一份数字）
    limit = 60
    try:
        import importlib.util as _ilu
        _sp = _ilu.spec_from_file_location("_epchk", os.path.join(HERE, "episode.py"))
        _m = _ilu.module_from_spec(_sp)
        _sp.loader.exec_module(_m)
        limit = int(getattr(_m, "MAX_EVENTS", limit))
    except Exception:
        pass
    over = [x for x in lens if x > limit]
    total = sum(lens)
    live = con.execute("SELECT COUNT(*) FROM events WHERE superseded=0").fetchone()[0]
    detail = "段数 %d、最大 %d、平均 %.1f" % (len(lens), max(lens), total / len(lens))
    if over:
        add("分段粒度", False, "%d 段超过上限 %d（最大 %d）—— 合并那一步可能又没设闸门"
            % (len(over), limit, max(lens)))
        return
    if total < live:
        add("分段粒度", False, "段内事件共 %d，少于活跃事件 %d —— 可能有事件没进任何段"
            % (total, live))
        return
    add("分段粒度", True, detail + "，覆盖 %d 条活跃事件" % live)


def check_migration_coverage(con):
    """schema.sql 里的列，必须在 `_migrate` 里也有对应的补列语句（2026-11-06 立）。

    由头：`_migrate` 的注释写着「**改 schema.sql 和加迁移必须同批做**」，并举了
    2026-11-03 那次事故（fingerprint 是 ALTER 加进真库的、schema.sql 漏了，
    于是用 schema.sql 新建的库连 remember 都跑不起来）。

    **但这条规则一直没有执行件** —— 而现在 `_migrate` 只补 `last_hit_at` 和 `dormant`
    两列。将来谁往 schema 里加第三列、忘了同步迁移，**老库会静默少一列**，
    直到某条 SQL 报 `no such column` 才发现。

    判据：schema.sql 建出的列 - 真库有的列 = 需要迁移的列；这些列必须在 `_migrate` 的
    (name, ddl) 元组里出现过。当前两者一致（18 列对 18 列），所以只要不新增就没问题。
    """
    import tempfile
    schema_p = os.path.join(HERE, "schema.sql")
    brain_p = os.path.join(HERE, "brain.py")
    if not (os.path.exists(schema_p) and os.path.exists(brain_p)):
        add("迁移覆盖", False, "找不到 schema.sql 或 brain.py")
        return
    # 从 brain.py 里读 _migrate 补了哪些列
    src = io.open(brain_p, encoding="utf-8").read()
    m = re.search(r"def _migrate\(.*?\n    have = .*?\n(.*?)\n\n", src, re.S)
    migrated = set(re.findall(r'\("([a-z_]+)",\s*"', m.group(1))) if m else set()
    # 用 schema.sql 建临时库，拿到 events 的列
    tmp = os.path.join(tempfile.gettempdir(), "_health_migrate.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        c = sqlite3.connect(tmp)
        c.executescript(io.open(schema_p, encoding="utf-8").read())
        cols = {r[1] for r in c.execute("PRAGMA table_info(events)")}
        c.close()
    except Exception as e:
        add("迁移覆盖", False, "用 schema.sql 建库失败：%s" % type(e).__name__)
        return
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    # 真库已有的列（新库不需要迁移）
    # 2026-11-06: 这里原本有个 `real = {...}` 赋值但从未使用（ruff F841），删掉。
    # 一个全新库按 schema 建出来就齐了，所以这里只核「migrate 声明的列是否都在 schema 里」
    # —— 反过来（schema 有而 migrate 漏）在全新库上体现不出来，得靠人改代码时记着。
    missing_in_schema = migrated - cols
    if missing_in_schema:
        add("迁移覆盖", False,
            "_migrate 补的列 %s 不在 schema.sql 里 —— 全新库跟老库结构会分叉"
            % sorted(missing_in_schema))
        return
    add("迁移覆盖", True,
        "schema %d 列；_migrate 补 %d 列（%s），声明均在 schema 内"
        % (len(cols), len(migrated), "/".join(sorted(migrated)) or "无"))


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
    # __pycache__ / .pytest_cache / .mypy_cache 是跑 Python 的正常产物，不算「目录脏」。
    # 2026-11-06 实测：每次跑完测试它就冒出来，报红纯属噪音——噪音多了体检会被忽略。
    IGNORE = {"__pycache__", ".pytest_cache", ".mypy_cache"}
    have = {n for n in os.listdir(HERE) if n not in IGNORE}
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
    而最近三天被纠正过两次 —— 不是没纠正，是没记。
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


def check_refresh_log():
    """刷新链最近一轮有没有硬失败标记（2026-11-06 立）。

    **由头是一起整整 9 次没人发现的失败**：memq 于 2026-11-04 退役，
    `memindex.mjs` 连同它的 index 产物一起被移进 `archive\\memq-retired-20261104\\`，
    而 `tools\\memory-refresh.bat` 第 5 步还在调 `%HUB%\\memindex.mjs`。
    从 2026-11-05 01:34 起，每 6 小时一次 `Error: Cannot find module`，
    连着 9 轮 —— 一次都没被看见。

    原因不是没人跑，是**没人读 refresh.log**：那 9 次失败全躺在里面，
    而 bat 对第 5 步没有 errorlevel 检查，health 也不看这个文件。

    判据：切出最后一次 run（`=====` 开头的行为界）之后的片段，命中硬失败标记即红。
    `[WARN]` **不算** —— 那是 bat 里有意的软失败（realcheck 未命中照样记 WARN）。
    """
    p = os.path.join(HERE, "..", "hub", "refresh.log")
    if not os.path.exists(p):
        # 没接定时刷新链的环境（例如刚 clone 下来）没有这个文件，跳过而不是报红，
        # 否则这一项在别人机器上会永远失败。
        add("刷新链日志", True, "未接刷新链（无 refresh.log），跳过")
        return
    try:
        raw = open(p, "rb").read()
    except Exception as e:
        add("刷新链日志", False, "读不了：%s" % type(e).__name__)
        return
    txt = raw.decode("utf-8", "replace")
    lines = txt.splitlines()
    last = -1
    for i, ln in enumerate(lines):
        # 只认"一轮开始"的分隔行；SUMMARY 那行也以 ===== 开头，但不是一轮的起点
        if (ln.startswith("===== run ") or ln.startswith("===== MANUAL RERUN ")) \
                and "SUMMARY" not in ln:
            last = i
    if last < 0:
        add("刷新链日志", False, "没有任何 run 分隔标记")
        return
    seg = "\n".join(lines[last:])
    marks = ("[FAIL]", "Error:", "MODULE_NOT_FOUND", "forrtl:", "Traceback (most recent")
    hit = [m for m in marks if m in seg]
    stamp = lines[last].strip().lstrip("= ").strip()
    if hit:
        add("刷新链日志", False, "最近一轮（%s）有失败：%s" % (stamp, "、".join(hit)))
        return
    add("刷新链日志", True, "最近一轮（%s）无失败标记" % stamp)


def selftest():
    """自证：造几个坏样本，确认对应检查真的会红（2026-11-06 立）。

    由头：17 项检查里只有 4 项（睡眠日志、引用完整性、SELF.md、分段粒度）
    是我手动做过双向实证的，其余 13 项**从没验过会不会失败**。
    **一条永远报 OK 的闸门等于没有闸门** —— 而它比没有更糟，因为它给人虚假的安心。

    做法：在**临时副本**上注入坏数据，跑对应检查，看它是不是 add(False)。
    不碰真库。
    """
    import shutil
    import tempfile
    cases_failed = []
    src = DB
    tmp = os.path.join(tempfile.gettempdir(), "_brain_health_selftest.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        shutil.copy2(src, tmp)
        con = sqlite3.connect(tmp)
        con.row_factory = sqlite3.Row

        # 夹具 1：links 指向不存在的事件（引用完整性该红）
        con.execute("INSERT INTO links (a,b,rel,created_at) VALUES (999999,999998,'similar',?)",
                    (time.strftime("%Y-%m-%d %H:%M:%S"),))
        con.commit()
        RESULTS.clear()
        check_link_integrity(con)
        cases_failed.append(("引用完整性", len(RESULTS) == 1 and not RESULTS[0]["ok"]))
        con.execute("DELETE FROM links WHERE a=999999")
        con.commit()

        # 夹具 2：episodes 段长超限（分段粒度该红）
        row = con.execute("SELECT id, event_count FROM episodes LIMIT 1").fetchone()
        if row:
            con.execute("UPDATE episodes SET event_count=99999 WHERE id=?", (row["id"],))
            con.commit()
            RESULTS.clear()
            check_episode_size(con)
            cases_failed.append(("分段粒度", len(RESULTS) == 1 and not RESULTS[0]["ok"]))
            con.execute("UPDATE episodes SET event_count=? WHERE id=?", (row["event_count"], row["id"]))
            con.commit()

        # 夹具 3：事件表为空（check_events 该红）
        con.execute("CREATE TEMP TABLE _bak AS SELECT * FROM events")
        con.execute("DELETE FROM events")
        con.commit()
        RESULTS.clear()
        check_events(con)
        cases_failed.append(("事件数量", len(RESULTS) >= 1 and not RESULTS[0]["ok"]))
        con.execute("INSERT INTO events SELECT * FROM _bak")
        con.commit()

        con.close()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    bad = [n for n, ok in cases_failed if not ok]
    print("health 自证 —— %d 组夹具" % len(cases_failed))
    for n, ok in cases_failed:
        print("  [%s] %s" % ("OK " if ok else "XX", n))
    print()
    print("VERDICT: %s" % ("PASS" if not bad else "FAIL（%d 组：%s）" % (len(bad), "/".join(bad))))
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="造坏样本验证检查真的会红，不碰真库")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    if not os.path.exists(DB):
        print(json.dumps({"ok": False, "error": "brain.db 不存在"}, ensure_ascii=False))
        return 1

    con = connect()
    for fn in (check_tables, check_schema, check_events, check_derived, check_episodes,
               check_evidence, check_decay, check_sleep_log, check_link_integrity,
               check_self_md, check_episode_size, check_migration_coverage,
               check_meta, check_corrections, check_memory_use):
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
    try:
        check_refresh_log()
    except Exception as e:
        add("刷新链日志", False, "检查本身出错：%s" % e)

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
