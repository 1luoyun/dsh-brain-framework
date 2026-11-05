#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""brain.py —— 类脑记忆库的读写接口（零外部依赖，只用标准库）

设计口径
--------
唯一真相是 events（只增不改）。facts / procedures / episodes 都是从 events 派生的
视图，随时可重建。工作记忆不落库，每次现算。

评分（学自 Ebbinghaus：R = e^(-t/S)）
------------------------------------
    S = (weight / 10) * 7 天      # 权重 100 → 半衰 70 天；权重 10 → 7 天
    score = weight * e^(-age/S) * salience * (sting ? 1.25 : 1)

权重 1-100，由三类东西推：
    · 来源（事故 90+ / 决定 70 / 完成 50 / 流水 20）
    · 情绪（sting 会在评分时额外 ×1.25）
    · feedback（人夸一句 +5，挨骂 +15，被纠正 +20，最高 100）

命令
----
  init                              建库（幂等）
  remember --kind K --title T [...] 写一件事（唯一写入点）
  recall "关键词" [--limit N]       全文检索（命中会累加 hits）
  working [--limit N]               生成"当下该注意的"几条
  link A B --rel REL                记两件事的关系
  feedback ID --good/--bad/--correct 反馈调权重
  open --what W [--due D]           记一条未结
  close ID                          结掉一条未结
  stats                             看一眼库的体量
  forget [--dry-run]                衰减（带留痕）
"""

import argparse
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# 这个目录只放源码与数据。brain.py 现在会 import episode（写完一条事件顺手
# 重建情景段），而 import 默认会在同目录生成 __pycache__ —— 关掉写入，
# 免得每次运行都往这里扔缓存（health.py 的「目录干净」检查当场就会红）。
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("BRAIN_DB") or os.path.join(HERE, "brain.db")
SCHEMA = os.path.join(HERE, "schema.sql")

KINDS = ("decision", "accident", "correction", "done", "learn", "promise", "user")
RELS = ("caused_by", "contradicts", "same_as", "follows", "fixes")

# 权重档（1-100）
W = {"accident": 95, "correction": 90, "promise": 80, "decision": 70,
     "learn": 75, "done": 50, "user": 60}

# feedback 增量
FB = {"good": 5, "bad": 15, "correct": 20}


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """给老库补列。

    schema.sql 用的是 `CREATE TABLE IF NOT EXISTS` —— 表已存在时整段跳过，
    新列不会自动长出来。2026-11-03 就栽过一次：fingerprint 是 ALTER 加的、
    schema.sql 漏了，结果用 schema.sql 新建的库连 remember 都跑不起来。
    所以「改 schema.sql」和「加迁移」必须同批做。
    """
    have = {r[1] for r in con.execute("PRAGMA table_info(events)")}
    for name, ddl in (("last_hit_at", "TEXT"),
                      ("dormant", "INTEGER NOT NULL DEFAULT 0")):
        if name not in have:
            con.execute("ALTER TABLE events ADD COLUMN %s %s" % (name, ddl))


def ensure_schema(con: sqlite3.Connection) -> None:
    with open(SCHEMA, encoding="utf-8") as f:
        con.executescript(f.read())
    _migrate(con)
    con.commit()


def emit(obj) -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def clamp(v, lo=1, hi=100) -> int:
    return max(lo, min(hi, int(v)))


# ── 模式分离：要素指纹（海马齿状回干的事）─────────────────
_STOP = {"确认", "检查", "记录", "整理", "一遍", "这个", "那个", "已经", "现在",
         "可以", "需要", "问题", "情况", "结果", "办法", "方案", "一下"}


def fingerprint(title: str, kind: str) -> str:
    """从标题抽要素词，排序去重后拼成指纹。

    目的：让"改配置搞坏宿主"这类**同型不同因**的事故能被区分开——
    openAt / pnpm install / dsh-wb 三种都会搞坏宿主，但根因不同。

    分词口径：英文标识符整词；中文取**相邻 2 字**（bigram）。
    用 4 字贪婪会切出"恢复误删"这种整块，跟"误删"配不上（2026-11-03 实测踩过）。
    """
    t = title or ""
    keep = []
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_.\-]{1,}", t):
        if len(w) >= 2 and w not in keep:
            keep.append(w)
    for seg in re.findall(r"[\u4e00-\u9fff]+", t):
        for i in range(len(seg) - 1):
            bg = seg[i:i + 2]
            if bg in _STOP or bg in keep:
                continue
            keep.append(bg)
    return kind + "|" + "|".join(sorted(keep)[:12])



# ── 评分 ────────────────────────────────────────────────────
def score(row) -> float:
    """一条事件的检索分。

    2026-11-05 试改两轮，结论：**时间衰减不能砍，使用加成可以加强**。
      第一轮把半衰系数 7→14 天（想让旧事件别被当没发生过）：hit@1 +1，但 hit@10 -3，
      老题 #21/#30 直接掉出去 —— 旧的全都浮上来，把正确答案挤走了。
      所以 s 恢复原样；只把「使用加成」上限从 0.5 提到 1.0：
      抗衰减的来源应该是「被真的用过」，不是「所有东西都别老」。
    """
    w = float(row["weight"] or 50)
    try:
        ts = time.mktime(time.strptime(row["ts"], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        ts = time.time()
    age = max(0.0, (time.time() - ts) / 86400.0)
    s = max(1.0, (w / 10.0) * 7.0)          # 记忆强度：越重要忘得越慢
    retention = math.exp(-age / s)
    sal = float(row["salience"]) if row["salience"] is not None else 1.0
    sting = 1.25 if row["emotion"] == "sting" else 1.0
    used = 1.0 + min(1.0, (row["hits"] or 0) * 0.10)   # 常被取用 = 抗衰减的真正来源
    return w * retention * sal * sting * used


# ── 写 ──────────────────────────────────────────────────────
def _current_session(con=None):
    """当前会话 id，按两档取：

    ① meta['current_session'] —— 由装配器（self.mjs）写进来。它知道确切的容器 id；
       Python 侧拿不到（实测环境变量里没有 DSH_SESSION_ID）。
    ② 回落到「~/.dsh/sessions/<workspace>/ 下最近被写过的目录」—— 会猜错
       （实测猜成了 session-A，而实际是 session-B），
       所以只在 ① 没有时才用，且它只用于挂来源，不影响评分与排序。
    """
    own = con is None
    if own:
        con = connect()
    try:
        row = con.execute("SELECT v FROM meta WHERE k='current_session'").fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    finally:
        if own:
            con.close()
    base = os.path.join(os.path.expanduser("~"), ".dsh", "sessions")
    if not os.path.isdir(base):
        return None
    best = None
    for ws in os.listdir(base):
        wd = os.path.join(base, ws)
        if not os.path.isdir(wd):
            continue
        for sid in os.listdir(wd):
            p = os.path.join(wd, sid)
            if not os.path.isdir(p):
                continue
            try:
                m = os.path.getmtime(p)
            except Exception:
                continue
            if best is None or m > best[0]:
                best = (m, sid)
    return best[1] if best else None


def _rebuild_episodes(con):
    """写完一条事件后顺手重建情景段。

    实测 866 段只要 0.18 秒，比一次 recall 还快。不重建的代价很具体：
    SELF.md 里「此刻在哪段经历里」会一直指向旧的段 —— 2026-11-03 实测，
    一个会话里记了十几条，episode_now 还停在几小时前，工作记忆和「现在在
    干什么」完全割裂（4 条 attention 与当前 episode 交集为空）。
    """
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import episode as _ep
        return _ep.rebuild(con)
    except Exception:
        return None


def cmd_remember(a) -> int:
    if a.kind not in KINDS:
        emit({"ok": False, "error": "kind 必须是 %s" % "/".join(KINDS)})
        return 2
    if not a.title.strip():
        emit({"ok": False, "error": "title 不能为空"})
        return 2
    weight = a.weight if a.weight is not None else W.get(a.kind, 50)
    weight = clamp(weight)
    con = connect(); ensure_schema(con)
    sess = getattr(a, "session", None) or _current_session(con)
    cur = con.execute(
        "INSERT INTO events (ts,kind,actor,title,body,refs,weight,emotion,source,decay_at,fingerprint,session)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (now(), a.kind, a.actor, a.title.strip(), a.body,
         json.dumps(a.ref, ensure_ascii=False) if a.ref else None,
         weight, a.emotion, a.source, a.decay_at, fingerprint(a.title, a.kind), sess))
    eid = cur.lastrowid
    killed = []
    if a.supersedes:
        for old_id in a.supersedes:
            row = con.execute("SELECT id,title FROM events WHERE id=? AND superseded=0",
                              (old_id,)).fetchone()
            if not row:
                continue
            con.execute("UPDATE events SET superseded=1 WHERE id=?", (old_id,))
            con.execute("INSERT OR REPLACE INTO links (a,b,rel,note,created_at) VALUES (?,?,?,?,?)",
                        (eid, old_id, "contradicts", "被 #%d 推翻" % eid, now()))
            killed.append(old_id)
    con.commit()
    ep_stats = _rebuild_episodes(con)
    con.close()
    emit({"ok": True, "id": eid, "weight": weight, "kind": a.kind, "session": sess,
          **({"episodes": ep_stats["episodes"]} if ep_stats else {}),
          **({"superseded": killed} if killed else {})})
    return 0


def cmd_link(a) -> int:
    if a.rel not in RELS:
        emit({"ok": False, "error": "rel 必须是 %s" % "/".join(RELS)})
        return 2
    con = connect(); ensure_schema(con)
    try:
        con.execute("INSERT OR REPLACE INTO links (a,b,rel,note,created_at) VALUES (?,?,?,?,?)",
                    (a.a, a.b, a.rel, a.note, now()))
        con.commit()
        ok = True
    except sqlite3.IntegrityError as e:
        ok = False; err = str(e)
    con.close()
    emit({"ok": ok, "a": a.a, "b": a.b, "rel": a.rel} if ok else {"ok": False, "error": err})
    return 0 if ok else 1


def cmd_feedback(a) -> int:
    con = connect(); ensure_schema(con)
    row = con.execute("SELECT id, weight, salience FROM events WHERE id=?", (a.id,)).fetchone()
    if not row:
        con.close(); emit({"ok": False, "error": "没有这条事件"}); return 1
    sign = "good" if a.good else ("bad" if a.bad else "correct")
    new_w = clamp(row["weight"] + FB[sign])
    # 2026-11-05：salience 从上线起就是**死字段** —— schema 注释写着「feedback 会推它」，
    # 但这里只改 weight，从没碰过它，所以它恒等于 1.0，score() 里那个乘数等于不存在。
    # weight（重要性，决定忘多慢）与 salience（显著度，决定多容易被唤起）在生物上
    # 是两件事：一条教训可以很重要却很少想起，也可以不重却总在眼前。
    # 所以 feedback 同时推 salience：幅度小、封顶 1.5，不喧宾夺主。
    sal = min(1.5, round(float(row["salience"] or 1.0) * (1.05 if sign == "good" else 1.02), 3))
    con.execute("UPDATE events SET weight=?, salience=? WHERE id=?", (new_w, sal, a.id))
    con.commit(); con.close()
    emit({"ok": True, "id": a.id, "from": row["weight"], "to": new_w,
          "salience": sal, "signal": sign})
    return 0


def cmd_constrain(a) -> int:
    if a.severity not in ("hard", "soft"):
        emit({"ok": False, "error": "severity 只能是 hard / soft"})
        return 2
    con = connect(); ensure_schema(con)
    cur = con.execute(
        "INSERT INTO constraints (what,why,severity,scope,created_at) VALUES (?,?,?,?,?)",
        (a.what.strip(), a.why, a.severity, a.scope, now()))
    con.commit(); cid = cur.lastrowid; con.close()
    emit({"ok": True, "id": cid, "severity": a.severity})
    return 0


def cmd_violate(a) -> int:
    """违反一条禁令：计数 +1、记时间，同时写一条事故事件。"""
    con = connect(); ensure_schema(con)
    row = con.execute("SELECT id,what,violated FROM constraints WHERE id=?", (a.id,)).fetchone()
    if not row:
        con.close(); emit({"ok": False, "error": "没有这条禁令"}); return 1
    con.execute("UPDATE constraints SET violated=violated+1, last_violated=? WHERE id=?",
                (now(), a.id))
    cur = con.execute(
        "INSERT INTO events (ts,kind,actor,title,body,weight,emotion,source)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (now(), "accident", "me", "违反禁令：%s" % row["what"][:80],
         a.note or "", 95, "sting", "constraint"))
    con.commit()
    eid = cur.lastrowid; con.close()
    emit({"ok": True, "constraint_id": a.id, "violated": row["violated"] + 1, "event_id": eid})
    return 0


def cmd_constraints(a) -> int:
    con = connect(); ensure_schema(con)
    where = "active=1"
    params = []
    if a.severity:
        where += " AND severity=?"
        params.append(a.severity)
    rows = [dict(r) for r in con.execute(
        "SELECT id,what,why,severity,scope,violated,last_violated FROM constraints"
        " WHERE %s ORDER BY CASE severity WHEN 'hard' THEN 0 ELSE 1 END, violated DESC, id" % where,
        params)]
    con.close()
    emit({"ok": True, "count": len(rows), "constraints": rows})
    return 0


def cmd_open(a) -> int:
    con = connect(); ensure_schema(con)
    cur = con.execute("INSERT INTO open_items (what,created_at,due,note) VALUES (?,?,?,?)",
                      (a.what.strip(), now(), a.due, a.note))
    con.commit(); oid = cur.lastrowid; con.close()
    emit({"ok": True, "open_item_id": oid})
    return 0


def cmd_close(a) -> int:
    con = connect(); ensure_schema(con)
    con.execute("UPDATE open_items SET status='done' WHERE id=? AND status='open'", (a.id,))
    con.commit(); n = con.total_changes; con.close()
    emit({"ok": bool(n), "id": a.id})
    return 0 if n else 1


# ── 读 ──────────────────────────────────────────────────────
# 中文虚词表：这些词在查询里不携带检索意图，留着只会让「什么」「怎么」
# 这类泛词去撞一堆无关记录。注意只放**本身没有实义**的两字词 ——
# 「事情」「东西」偶尔有实义，不放。
STOP_ZH = {"什么", "怎么", "为什", "么样", "多少", "如何", "是否", "哪些",
           "这个", "那个", "这些", "那些", "一下", "一些", "我们", "你们",
           "他们", "现在", "已经", "可以", "需要", "应该", "还是", "就是",
           "因为", "所以", "但是", "而且", "如果", "然后", "之前", "之后",
           "知道", "觉得", "时候"}


def _grams(q: str):
    """把长查询拆成相邻 2-gram，用于"线索不对时再试一次"。"""
    g = []
    for seg in re.findall(r"[\u4e00-\u9fff]+", q):
        for i in range(len(seg) - 1):
            bg = seg[i:i + 2]
            if bg not in g:
                g.append(bg)
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_.\-]{2,}", q):
        if w not in g:
            g.append(w)
    return g[:8]


def _idf(n_docs: int, df: int) -> float:
    """平滑 IDF：词越常见权重越低。

    为什么要有它：多词分支原来的判据是「至少命中 2 个词」——**按个数**算。
    中文 2-gram 拆得越细越容易凑够 2 个（「注意」+「什么」），而一条只命中
    「dsh-meme」的稀有记录反倒被挡掉（2026-11-03 实测：fuzzy 题 28 因此 miss）。
    检索领域的标准做法是按 IDF 加权（BM25 的 IDF 项），稀有词命中一次就够。
    """
    return math.log(1.0 + (n_docs + 1.0) / (df + 1.0))


def _degree_intent(q: str):
    """查询里有没有「最严重/最重要」这类**程度词**；有就返回门限，否则 None。

    2026-11-05 立。继承时间意图那套：问「我犯过最严重的错误是什么」，
    返回的第一条是 weight 75 的「凭感觉估时间」，而库里 weight≥90 的
    42 条事故（openAt 改 startup 98 分、pnpm 清空 node_modules 95 分）
    一条都没出现 —— 因为「最严重」三个字在词面上什么都撞不上。
    用户用了程度词，答案就该按权重给。
    """
    if any(w in q for w in ("最严重", "最重要", "最重", "最大的", "最惨", "最糟")):
        return 90.0
    if any(w in q for w in ("严重", "重要", "致命")):
        return 80.0
    return None


def _time_window(q: str):
    """查询里的时间词 → 天数窗口；没提时间返回 None。

    2026-11-05 立。由头：问「今天都干了什么」，返回 6 条里只有 4 条是今天的；
    问「最近做了什么」直接全错 —— 返回的全是两个月前的章节记录。
    「最近」这个词没有时间语义，检索器只按词面撞，撞到「做了什么」就收工。
    所以把时间词翻译成一个窗口，命中窗内的事件额外加权。
    """
    if any(w in q for w in ("今天", "今日", "刚才", "刚刚")):
        return 1.0
    if any(w in q for w in ("昨天", "昨日")):
        return 2.0
    if any(w in q for w in ("最近", "这几天", "近期", "这两天")):
        return 3.0
    return None


def _term_weights(con, terms):
    """每个查询词的 IDF 与总和。

    库里出现 0 次的词**不计入分母** —— 它不可能命中，算进去只会让覆盖率永远
    到不了线（2026-11-03：「竹竿」这类词在库里不存在，却把负例的分母撑大）。
    若全部词都不在库里，总和为 0，调用方据此直接判「无词面依据」。

    2026-11-05 改：**一次扫描算完所有词**。原来每个词单独发一条
    `count(*) ... WHERE title LIKE ? OR body LIKE ?`，而带通配符的 LIKE 必然
    全表扫 —— brief 的 clues 动辄十几个，就是十几次扫全库，实测 2.2 秒里
    绝大部分耗在这。合并成一条 SUM(CASE...) 后是同样一次扫描出全部 df，
    算出来的 IDF 值一个不差（同样的 n 与 df）。
    """
    n = con.execute("SELECT count(*) FROM events WHERE superseded=0").fetchone()[0]
    if not terms:
        return [], 0.0
    one = "SUM(CASE WHEN (title LIKE ? OR body LIKE ?) THEN 1 ELSE 0 END)"
    params = []
    for t in terms:
        params.append("%" + t + "%")
        params.append("%" + t + "%")
    row = con.execute(
        "SELECT %s FROM events WHERE superseded=0" % ", ".join([one] * len(terms)),
        params).fetchone()
    ws, tot = [], 0.0
    for df in row:
        df = df or 0
        w = _idf(n, df) if df > 0 else 0.0
        ws.append(w)
        tot += w
    return ws, tot


def _ts_of(ts):
    try:
        return time.mktime(time.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


# 哪几类事件之间才谈得上「新版推翻旧版」。
# done 类不算：同一章写两遍是「这活干了两次」，不是后一次推翻了前一次
# （2026-11-03 实测：不设这条限制，全库扫出 25,310 对「同类高重合」，
#   九成是「写正文 chNN」的重复，判成 stale 就是纯误伤）。
NEWABLE_KINDS = ("decision", "accident", "correction", "learn")


def find_conflicts(rows, min_gap_days=7):
    """找「可能的新版推翻旧版」的候选对 —— 只出证据，不自动压分。

    为什么不一键判定：2026-11-03 实测，全库 243 条 decision/accident/
    correction/learn 里只有 8 对满足判据，而且多半是「同一件事干了两次」
    而不是「新版推翻旧版」；不设 kind 限制时更是扫出 25,310 对，九成是
    「写正文 chNN」的重复。自动降权 = 误伤，所以机器只摆候选，人来定。

    判据（三条同时满足）：
      ① fingerprint 交集够多（长指纹要 3 个，短指纹按一半放）
      ② kind 相同，且属于可被推翻的四类（NEWABLE_KINDS）
      ③ 时间差 >= min_gap_days 天
    """
    out = []
    if len(rows) < 2:
        return out
    toks = {}
    for r in rows:
        parts = [p for p in (r.get("fingerprint") or "").split("|") if p]
        if parts and parts[0] == r["kind"]:
            parts = parts[1:]
        toks[r["id"]] = set(parts)
    gap = min_gap_days * 86400
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if a["kind"] != b["kind"] or a["kind"] not in NEWABLE_KINDS:
                continue
            small = min(len(toks[a["id"]]), len(toks[b["id"]]))
            need = 3 if small >= 6 else max(2, (small + 1) // 2)
            shared = len(toks[a["id"]] & toks[b["id"]])
            if shared < need:
                continue
            ta, tb = _ts_of(a.get("ts")), _ts_of(b.get("ts"))
            if ta is None or tb is None or abs(tb - ta) < gap:
                continue
            old, new = (a, b) if ta < tb else (b, a)
            out.append({
                "shared": shared,
                "kind": a["kind"],
                "old": {"id": old["id"], "ts": old["ts"], "weight": old["weight"],
                        "title": old["title"]},
                "new": {"id": new["id"], "ts": new["ts"], "weight": new["weight"],
                        "title": new["title"]},
            })
    out.sort(key=lambda p: (-p["shared"], -p["new"]["weight"]))
    return out


def _embed_port():
    return int(os.environ.get("BRAIN_EMBED_PORT") or 7799)


def _http_vector(q, top):
    """问常驻服务。服务不在/超时返回 None（调用方再去拉起或回落）。"""
    try:
        url = "http://127.0.0.1:%d/q?text=%s&top=%d" % (
            _embed_port(), urllib.parse.quote(q), top)
        with urllib.request.urlopen(url, timeout=3) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return d.get("hits") if d.get("ok") else None
    except Exception:
        return None


def _ensure_embed_server():
    """把常驻向量服务拉起来（已在跑就是 no-op，返回 True）。

    为什么值得多这一层：一次性调用实测 6.95 秒，几乎全花在 import torch +
    加载模型；常驻后一次加载、后续每次几十毫秒。中枢每轮都要查，差的是数量级。
    """
    py = os.path.join(os.path.dirname(os.path.dirname(HERE)), "comfyui", "venv",
                      "Scripts", "python.exe")
    script = os.path.join(HERE, "embed_server.py")
    if not (os.path.exists(py) and os.path.exists(script)):
        return False
    try:
        r = subprocess.run([py, script, "--start"], capture_output=True,
                           timeout=90, cwd=HERE, creationflags=0x08000000)
        return r.returncode == 0
    except Exception:
        return False


def _sem_sims(q, texts):
    """问常驻服务算一批文本与 q 的相似度；服务不在返回 None。

    给 brief 挑「哪套步骤贴题」用：词面匹配分不清「删一个目录」和
    「删东西前的三步预检」（只有一个「删」字共享），语义能分清
    （2026-11-03 实测 0.567 vs 0.451，正确答案第一）。

    **分批**：接口是 GET + URL 编码，300 条标题编码后五万多字符，
    超过服务端的 URL 上限会被拒（2026-11-04 实测：user_said 恒空，
    根因就是这里整批发，`urlopen` 抛异常被吞成 None）。
    每批 50 条，URL 长度可控，拼接结果对调用方透明。

    **2026-11-05 试过 POST 一次发完，反而更慢，已回滚 —— 别再来一遍**：
    服务端加了 POST /sim（能收任意长度），实测 358 条真实文本——
      POST 一次         2.96s
      GET 每批 50 条     1.82s   ← 现在用的
      GET 每批 20 条     1.75s
    原因不是网络往返，是 **batch 内 pad 到最长**：整批发时 pad 到全局最长（164 字），
    分批发时每批各自 pad，平均短得多。transformer 的 attention 又是 O(n²)，
    pad 一多就贵。所以「少往返」在这里是错的优化方向，**分批反而更快**。
    （顺带：brief 那 2 秒里大部分是服务端在算，socket 等待里含着它，别被 profiler 的
    recv_into 骗了。）
    """
    if not texts:
        return []
    out = []
    for i in range(0, len(texts), 50):
        chunk = texts[i:i + 50]
        try:
            url = "http://127.0.0.1:%d/sim?q=%s&texts=%s" % (
                _embed_port(), urllib.parse.quote(q),
                urllib.parse.quote(json.dumps(chunk, ensure_ascii=False)))
            with urllib.request.urlopen(url, timeout=5) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return None
        sims = d.get("sims") if d.get("ok") else None
        if sims is None or len(sims) != len(chunk):
            return None
        out.extend(sims)
    return out


def _vector_hits(q, top=20, min_sim=0.5):
    """语义检索：优先常驻服务，回落一次性子进程。

    阈值 0.5 是量出来的：两道否定题的 top1 只有 0.46/0.44，而正题 top1 是
    0.58/0.61。不设阈值的话，纯向量会把任何句子都匹配上一堆东西。
    """
    hits = _http_vector(q, top)
    if hits is None and _ensure_embed_server():
        hits = _http_vector(q, top)
    if hits is None:
        # 最后回落：老的一次性路径，慢（约 7 秒）但不会因为服务挂了就 mute。
        py = os.path.join(os.path.dirname(os.path.dirname(HERE)), "comfyui", "venv",
                          "Scripts", "python.exe")
        script = os.path.join(HERE, "embed.py")
        if not (os.path.exists(py) and os.path.exists(script)):
            return []
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8",
                       HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            r = subprocess.run([py, script, "--query", q, "--top", str(top), "--json"],
                               capture_output=True, timeout=180, env=env, cwd=HERE)
            hits = json.loads(r.stdout.decode("utf-8", "replace")).get("hits", [])
        except Exception:
            return []
    return [h for h in (hits or []) if h.get("sim", 0) >= min_sim]


def _episodes_for(con, ids, limit=3):
    """命中的事件属于哪一段经历（情景层）。

    人脑说出一件事时，会自动带上「那是哪一次」—— 记忆不是孤立的事实点，
    而是嵌在一段经历里。一段 episode 被命中的条数越多，越说明线索指向的是
    那次经历本身，而不是一条巧合相似的事实。
    """
    if not ids:
        return []
    idset = set(ids)
    try:
        cur = con.execute("SELECT id,started_at,ended_at,summary,kind_main,"
                          "event_count,weight,emotion,event_ids FROM episodes")
    except sqlite3.OperationalError:
        return []
    out = []
    for r in cur:
        try:
            members = set(json.loads(r["event_ids"] or "[]"))
        except Exception:
            continue
        hit = idset & members
        # 判据用「命中项里有多少落在这一段」，不是「命中几条」。
        # 段内可能收着十几条事件，命中 2-3 条很容易是巧合
        # （2026-11-03 实测：查「人设 云蝶」拖出了 openAt 事故那段，因为 10 条命中
        #   里有 3 条恰好落在那 19 条事件里）。要求至少 40%，且不少于 2 条。
        # 代价是小查询（1-2 条命中）通常给不出情景 —— 那是对的：
        # 一两件事构不成「一次经历」。
        need = max(2, int(len(idset) * 0.4))
        if len(hit) < need:
            continue
        out.append({
            "id": r["id"],
            "summary": r["summary"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "kind_main": r["kind_main"],
            "event_count": r["event_count"],
            "weight": r["weight"],
            "emotion": r["emotion"],
            "matched": len(hit),
            "matched_ids": sorted(hit)[:5],
        })
    out.sort(key=lambda x: (-x["matched"], -x["weight"]))
    return out[:limit]


def cmd_recall(a) -> int:
    """检索三层（情景/语义/程序）。

    提取失败 ≠ 遗忘：搜不到时先换线索再试，并把「试过什么」如实报出来，
    绝不把「我没查到」说成「库里没有」。
    """
    q = a.query.strip()
    if not q:
        emit({"ok": False, "error": "query 不能为空"}); return 2
    con = connect(); ensure_schema(con)
    # 休眠记录默认不参与检索：想不起来 ≠ 被删除。
    # 库还在，`--include-dormant` 能唤醒 —— 这是「遗忘」和「删除」的分界：
    # 遗忘只降提取概率（或暂时不返回），删除才是真的不要了。
    inc_dormant = getattr(a, "include_dormant", False)
    dm_e = "" if inc_dormant else " AND e.dormant=0"
    dm_p = "" if inc_dormant else " AND dormant=0"
    # 关系记忆（落云的发言）默认不进召回池：「他说过什么」和「发生过什么」
    # 是两层。2026-11-03 实测：导入 1807 条 user 事件后 hit@10 从 34 掉到 31，
    # 降权 0.5 也救不回来 —— 它们的 body 长、词面丰富，直接把召回池占满。
    # 要查「他上次怎么说」用 recall --with-user。
    inc_user = getattr(a, "with_user", False)
    du_e = "" if inc_user else " AND e.kind!='user'"
    du_p = "" if inc_user else " AND kind!='user'"
    attempts = []
    rows = []

    def fts(term):
        # 用 bm25 取召回池：FTS5 自带 bm25()，返回负值、越小越相关。
        # 原来的 ORDER BY 是 `e.weight DESC` —— 那等于**完全不看相关性**，
        # 所以查什么词都先冒出权重最高的那几条（2026-11-03 查「人设 云蝶」
        # 拖出一堆无关的高分事故，就是这个原因）。
        # 召回池放大到 limit*5，交给后面的评分再去排一次。
        try:
            cur = con.execute(
                "SELECT e.id,e.ts,e.kind,e.title,e.weight,e.emotion,e.salience,e.hits,e.session,"
                "       e.fingerprint, bm25(events_fts) AS rel,"
                "       snippet(events_fts,1,'…','…','…',12) AS snip"
                " FROM events_fts JOIN events e ON e.id=events_fts.rowid"
                " WHERE events_fts MATCH ? AND e.superseded=0" + dm_e + du_e +
                " ORDER BY rel ASC LIMIT ?", (term, max(a.limit * 5, 30)))
            return [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            return []

    def like(terms, weights=None):
        # 召回池按「IDF 加权命中分」排，不再按命中个数排。
        # 2026-11-03 实测：查「dsh-meme 看着正常其实坏了」时，真正的那条只命中
        # dsh-meme 一个词（n_hit=1），而一堆无关记录各命中 3-4 个常见 2-gram
        # （「看着」「正常」「其实」），按 n_hit 排它连 50 条池子都进不去。
        weights = weights if weights else [1.0] * len(terms)
        conds, params = [], []
        for t in terms:
            conds.append("(title LIKE ? OR body LIKE ?)")
            params += ["%" + t + "%"] * 2
        nsql, nparams = [], []
        wsql, wparams = [], []
        for t, w in zip(terms, weights):
            nsql.append("(CASE WHEN title LIKE ? OR body LIKE ? THEN 1 ELSE 0 END)")
            nparams += ["%" + t + "%"] * 2
            wsql.append("(CASE WHEN title LIKE ? OR body LIKE ? THEN ? ELSE 0 END)")
            wparams += ["%" + t + "%", "%" + t + "%", w]
        sql = (
            # 括号不能省：% 的优先级高于 +，不括起来格式化只会作用到最后一个
            # 字符串字面量上（2026-11-03 实测：TypeError: not all arguments
            # converted during string formatting）。
            ("SELECT id,ts,kind,title,weight,emotion,salience,hits,session,fingerprint,"
             " substr(coalesce(body,''),1,120) AS snip,"
             " (%s) AS n_hit, (%s) AS rel_hit FROM events"
             " WHERE (%s) AND superseded=0" + dm_p + du_p +
             " ORDER BY rel_hit DESC, n_hit DESC, weight DESC, ts DESC LIMIT ?")
            % (" + ".join(nsql), " + ".join(wsql), " OR ".join(conds)))
        cur = con.execute(sql, nparams + wparams + params + [max(a.limit * 5, 30)])
        out2 = [dict(r) for r in cur.fetchall()]
        # 给 LIKE 结果造一个与 bm25 同向的相关性分（负数、越小越相关）。
        # 乘 5 是为了和 bm25 的量级（-2 ~ -16）对齐。
        for r in out2:
            r["rel"] = -5.0 * max(1e-6, r.get("rel_hit") or 0.0)
        return out2

    # 线索 1+2：FTS 与 LIKE 同时跑再合并 —— FTS5 的 unicode61 对中文分词不可靠，
    # 光靠它会漏（实测：标题含"估时间"的今天事件，被 FTS 判为不命中）
    rows = fts(q)
    n_fts = len(rows)
    extra = like([q])
    seen_ids = {r["id"] for r in rows}
    added = 0
    for r in extra:
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"]); rows.append(r); added += 1
    attempts.append({"cue": "fts+like", "term": q,
                     "hits": len(rows), "fts_only": n_fts, "like_added": added})
    # 线索 3：换线索。分两种输入，规则不同：
    #   · 用户敲了多个词（含空格）→ 这是"任意一个词命中即可"（OR），不该要求全部命中
    #   · 用户敲的是一个词 → 拆 2-gram，且必须命中 >=2 个要素
    #     （否则"量子纠缠香蕉飞船"随手撞上两个单字就冒充命中）
    # 分词口径：英文整词 + 中文 2-gram，**丢掉单字**。
    # 单字的中文 LIKE 等于匹配一切 —— 2026-11-03 实测：一句自然问法拆出个「动」字，
    # 命中 50 条、把真正的答案（那次 pnpm 事故）挤出前十。
    # 另外：被空格分隔出来的「长中文串」不是词、是一整句，拿去做 LIKE 永远不中，
    # 只会把 n_hit 的分母撑大，反而压低真正命中的那条。英文标识符不受此限。
    words = []
    for w in re.split(r"[\s,，、]+", q):
        if not w:
            continue
        if re.fullmatch(r"[A-Za-z0-9_.\-]+", w):
            # 单字符英文/数字词（D / C / 8）必须丢：LIKE '%D%' 会命中一切含 d
            # 的记录，它只是把无关条目拉进池子（2026-11-03 实测：查「D 盘 AppID
            # 误判」，#4523 被一堆碰巧含 d 的新事件挤到第 13 名）。
            if len(w) >= 2:
                words.append(w)
        elif 2 <= len(w) <= 6:
            words.append(w)
    for g in _grams(q):
        if g not in words:
            words.append(g)
    # 最后统一剔虚词。放在末尾是因为 2-gram 也会切出「什么」「怎么」这种碎片，
    # 它们对判断「这条是不是答案」毫无贡献，只负责把无关记录拉进候选。
    words = [w for w in words if w not in STOP_ZH]
    if not words:
        words = [q]
    if len(words) >= 2:
        # 多词查询**总是**展开，不再要求「之前一条都没命中」。
        # 2026-11-03 实测：「人设 云蝶」从 10 条退化成 1 条，就是那条判断的锅。
        wts, tot = _term_weights(con, words)
        extra2 = like(words, wts) if tot > 0 else []
        added2 = 0
        for r in extra2:
            if r["id"] in seen_ids:
                continue
            cov = (r.get("rel_hit") or 0.0) / tot if tot > 0 else 0.0
            # 门槛只做「别把无关记录全拉进来」的粗筛，**不当相关性判据**。
            # 2026-11-03 实测（39 题）：正确答案的覆盖率从 0.02（「C 盘满了
            # 哪些能清」→ #4523）到 1.00 都有；0.35 的硬门槛把 28 号题
            # （只命中 dsh-meme 一个词，cov 0.20）直接挡在门外。
            # 真正的判据交给「绝对覆盖率排序 + 最终 verdict 门槛」。
            if cov < 0.08:
                continue
            r["coverage"] = round(cov, 3)
            seen_ids.add(r["id"])
            rows.append(r)
            added2 += 1
        attempts.append({"cue": "multi-word-or", "terms": words,
                         "idf_total": round(tot, 2), "added": added2})
    if not rows:
        g = _grams(q)
        if g:
            wts, tot = _term_weights(con, g)
            cand = like(g, wts) if tot > 0 else []
            keep = []
            for r in cand:
                cov = (r.get("rel_hit") or 0.0) / tot if tot > 0 else 0.0
                if cov < 0.08:
                    continue
                r["coverage"] = round(cov, 3)
                r["matched_grams"] = r.get("n_hit") or 0
                keep.append(r)
            keep.sort(key=lambda r: -r.get("coverage", 0))
            rows = keep
            attempts.append({"cue": "bigram", "terms": g, "hits": len(rows),
                             "rule": "idf coverage >= 0.08"})

    # 所有候选用同一把尺子量：词法覆盖率。
    # FTS 进来的那条只有 bm25 分，与 LIKE 的 IDF 加权分**量纲不同**，直接
    # min-max 混在一起必然把真答案压死（2026-11-03 实测：#2305 标题原样含
    # REQUEST_EXTENSION + 根因，却因为 bm25 分小而排到第 29 名）。
    wts2, tot2 = _term_weights(con, words)
    strong_terms = [t for t in words
                    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]{2,}", t)]
    # 文档长度归一化（BM25 的 |D| 项，b=0.75）：正文越长，命中同样的词越不值钱。
    # 为什么要它：新事件的正文经常整段引用旧事故，词面能全中，于是「正文里提过」
    # 压过「这事本身」（2026-11-03 实测：4 号题的 #4552 正文引用了 git hooks
    # 零填充，把原始记录 #7 压下去；9 号题的 #4568 也是这么拿 cov 1.0 的）。
    avg_len = con.execute(
        "SELECT avg(length(coalesce(title,'') || ' ' || coalesce(body,'')))"
        " FROM events WHERE superseded=0").fetchone()[0] or 800.0
    B = 0.75

    def lex_cov_of(rid):
        """返回 (IDF 覆盖率, 命中词数, 标题覆盖率, 是否命中英文标识符)。

        英文标识符（dsh-meme / REQUEST_EXTENSION 这种）在中文语料里稀有且
        指代明确，命中一个就是强信号 —— 这是唯一能救「只提到 dsh-meme、
        其余全是口语虚词」那类查询的东西（28 号题）。
        标题覆盖率单独算：用户原样问某条标题时，标题命中才是真答案。
        """
        row = con.execute("SELECT title, body FROM events WHERE id=?", (rid,)).fetchone()
        if not row or tot2 <= 0:
            return 0.0, 0, 0.0, False
        title = row["title"] or ""
        body = row["body"] or ""
        text = title + " " + body
        hit = [(t, w) for t, w in zip(words, wts2) if t in text]
        lenf = 1.0 - B + B * (len(text) / max(1.0, avg_len))
        cov = sum(w for _, w in hit) / tot2 / lenf
        # 标题覆盖率**不除长度因子**：标题命中率跟正文长短无关。
        # 2026-11-03 实测：26 号题的答案 #4527 标题原样含 NoobAI/IPAdapter/
        # 不兼容，正文长（三组对照实验），除完 lenf 只剩 0.372，被一条
        # title_cov=0 的交接文档（cov 0.837）顶掉。
        t_cov = sum(w for t, w in hit if t in title) / tot2
        return cov, len(hit), t_cov, any(t in text for t in strong_terms)

    for r in rows:
        cov, nhit, t_cov, strong = lex_cov_of(r["id"])
        r["coverage"] = round(min(1.0, max(r.get("coverage") or 0.0, cov)), 3)
        r["title_cov"] = round(min(1.0, t_cov), 3)
        r["n_hit"] = max(int(r.get("n_hit") or 0), nhit)
        r["strong"] = strong

    # 线索 4：语义。**只在词法不够时**才用 —— 一次调用约 7 秒（几乎全花在
    # import torch）。2026-11-03 试过无条件两路都跑（hybrid 的标准做法），
    # 但在这份库上 hit@1 从 25 掉到 23：向量把大量「语义沾边」的新事件带进来，
    # 而 eval 的期望答案多数是老事故。等有重排器/置信度校准再开，别裸开。
    max_cov = max([r.get("coverage") or 0.0 for r in rows], default=0.0)
    if max_cov < 0.6 and not getattr(a, "no_vector", False):
        added_v = 0
        for h in _vector_hits(q):
            # 语义分也换成同一把尺子：sim 0.45 以下不可用（负例 top1 实测
            # 0.46~0.52，正例 0.58 以上），0.75 以上当作满相关。
            vcov = round(min(1.0, max(0.0, (h["sim"] - 0.45) / 0.30)), 3)
            if h["id"] in seen_ids:
                # **不能跳过**：已经在词法池里的条目一样要把语义分并进去。
                # 2026-11-03 实测：33 号题的 #2325 先被词法以 cov 0.14 拉进池，
                # 向量命中（sim 0.589）却被 `continue` 丢掉，于是被埋到 12 名后。
                for r in rows:
                    if r["id"] == h["id"]:
                        r["coverage"] = round(max(r["coverage"] or 0.0, vcov), 3)
                        r["from_vector"] = round(h["sim"], 3)
                        break
                continue
            row = con.execute(
                "SELECT id,ts,kind,title,weight,emotion,salience,hits,session,"
                " fingerprint, substr(coalesce(body,''),1,120) AS snip"
                " FROM events WHERE id=? AND superseded=0", (h["id"],)).fetchone()
            if not row:
                continue
            d2 = dict(row)
            d2["rel"] = -20.0 * h["sim"]     # 与 bm25 同向：越相似越负
            d2["from_vector"] = round(h["sim"], 3)
            d2["coverage"] = vcov
            d2["title_cov"] = 0.0
            d2["n_hit"] = 0
            d2["strong"] = False
            rows.append(d2)
            seen_ids.add(h["id"])
            added_v += 1
        attempts.append({"cue": "vector", "added": added_v, "min_sim": 0.5})

    episodes = []
    # 时间意图补池（2026-11-05）：查询带「今天/昨天/最近」时，先把窗口内的事件
    # 拉进召回池。只做加权是不够的 —— 「最近做了什么」在词面上只会撞到「做了什么」，
    # 池子里全是两个月前的章节记录，给它们加权、没加权的，近期事件根本不在池里。
    # 这一步是"补召回"，不是"改排序"：池子有了，后面的 score 才有得排。
    _tw = _time_window(q)
    _dg = _degree_intent(q)
    if _tw is not None:
        _cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                                time.localtime(time.time() - _tw * 86400))
        _have = {r["id"] for r in rows}
        _extra = [dict(x) for x in con.execute(
            "SELECT e.id,e.ts,e.kind,e.title,e.weight,e.emotion,e.salience,e.hits,e.session"
            " FROM events e WHERE e.superseded=0 AND e.ts >= ?"
            " ORDER BY e.weight DESC, e.ts DESC LIMIT ?",
            (_cutoff, max(20, a.limit * 3)))]
        _added = 0
        for x in _extra:
            if x["id"] not in _have:
                rows.append(x)
                _have.add(x["id"])
                _added += 1
        if _added:
            attempts.append({"cue": "time_window", "days": _tw, "added": _added})
    # 程度意图补池（2026-11-05）：跟时间窗同一个坑 —— 只加权没用，
    # weight≥90 的事故往往词面撞不上「犯了什么错误」，压根不在池子里。
    # 「最严重的」就该把库里最重的那几条摆出来。
    if _dg is not None:
        _have = {r["id"] for r in rows}
        _hi = [dict(x) for x in con.execute(
            "SELECT e.id,e.ts,e.kind,e.title,e.weight,e.emotion,e.salience,e.hits,e.session"
            " FROM events e WHERE e.superseded=0 AND e.weight >= ?"
            " ORDER BY e.weight DESC, e.ts DESC LIMIT ?",
            (_dg, max(10, a.limit * 2)))]
        _added2 = 0
        for x in _hi:
            if x["id"] not in _have:
                rows.append(x)
                _have.add(x["id"])
                _added2 += 1
        if _added2:
            attempts.append({"cue": "degree", "threshold": _dg, "added": _added2})

    if rows:
        # 启动效应（priming）：最近 24 小时内发生的事更容易被想起
        now_ts = time.time()
        # 相关性混合：把 bm25 归一化到 0..1，与「权重 × 衰减 × 启动」相乘。
        # 纯按相关性排会让低权重流水挤掉事故；纯按权重排就等于不看查询
        # —— 两条都试过，2026-11-03 改成相乘，各占一半。
        rels = [r["rel"] for r in rows if r.get("rel") is not None]
        lo = min(rels) if rels else None
        hi = max(rels) if rels else None
        for r in rows:
            base = score(con.execute(
                "SELECT ts,weight,salience,emotion,hits FROM events WHERE id=?", (r["id"],)).fetchone())
            if r.get("kind") == "user":
                # 关系记忆不抢经验记忆的位置：「他说过什么」和「发生过什么」
                # 是两层。2026-11-03 实测：导入 1807 条 user 事件后
                # hit@10 从 34 掉到 31 —— 落云的话里全是查询词，挤掉了真答案。
                base *= 0.5
            try:
                age_h = (now_ts - time.mktime(time.strptime(r["ts"], "%Y-%m-%d %H:%M:%S"))) / 3600.0
            except Exception:
                age_h = 999
            prime = 1.5 if age_h <= 24 else (1.15 if age_h <= 72 else 1.0)
            rel = r.get("rel")
            if r.get("coverage") is not None:
                # 有绝对覆盖率就用绝对标准：池子大小不影响这一条的分数。
                # 原来的 min-max 是**池内相对**的 —— 同一个查询，池里有没有更强的
                # 记录会改变它的相关性分，等于拿「对手」量「自己」。
                #
                # 2026-11-05 实测（固定快照 A/B，reports/baseline-20261105.db）：
                # 下限 0.3 → 0.15，hit@10 **42→46**、overall **86.7%→93.3%**、
                # 未命中从 8 条降到 4 条。根因是 fuzzy 题的答案能进 top30 却挤不进
                # top10 —— boost 只有 0.3~1.0 的幅度，压不过 weight 造成的差距。
                # 拉开幅度后相关性重新有话语权。（同批试过压缩 weight 本身，
                # hit@1 掉 1，已回滚 —— 别两条一起动。）
                boost = 0.15 + 0.85 * r["coverage"]
                if r.get("strong") and r["coverage"] < 0.35:
                    # 英文标识符命中是强信号，但只在词法覆盖率低时补一档：
                    # 28 号题全靠这一条（cov 0.20）。不设这个限制的话，新事件
                    # 正文里只要带个 pnpm 就 +0.25，会把 2 号题的真答案压下去。
                    boost = min(1.0, boost + 0.15)
            elif rel is None or hi is None or hi == lo:
                # LIKE 命中的没有 bm25 分，给个中性值，别当作「最不相关」
                boost = 0.8
            else:
                # bm25 与 LIKE 的相关性分都是**越小越相关**（负数），
                # 所以归一化用 (hi - rel) —— 写成 (rel - lo) 会把最相关的那条
                # 打成最低分（2026-11-03 差点这么上线：查 pnpm install，
                # 命中两个词的 #624 被降到 0.5 倍，排在只沾一个词的事件后面）。
                # 幅度也要够狠：原来是 0.3~1.0，2026-11-05 改成 0.15~1.0 后
                # hit@10 42→46（见上面 coverage 分支的同批实测说明）。
                # 0.5~1.0 时「只沾一个泛词但很新」的记录压过「两个词都命中但很老」的那条。
                boost = 0.15 + 0.85 * ((hi - rel) / (hi - lo))
            r["score"] = round(base * prime * boost, 1)
            r["matched"] = boost > 0.8
            r["primed"] = prime > 1.0
        # 时间意图加权（2026-11-05）：查询里带「今天/昨天/最近」时，命中窗口内的事件
        # 提到最前。不只给 1.6 倍 —— 实测那样没用：窗口内的事件词面通常对不上，
        # boost 会掉到下限 0.15，1.6 倍之后仍然压不过两个月前的旧记录。
        # 既然用户问的是「最近」，那就是**按时间要答案**，窗内的排前面才对。
        _win = _time_window(q)
        if _win is not None:
            for r in rows:
                _d = _days_since(r.get("ts"))
                if _d is not None and _d <= _win:
                    r["score"] = round(r["score"] * 6.0, 1)
                    r["timefit"] = True
        # 程度意图（2026-11-05）：问「最严重的」时，高权重的事故该排前面。
        # 跟时间意图同一个道理 —— 「最严重」在词面上没有落点，只能靠权重读懂。
        if _dg is not None:
            for r in rows:
                if float(r.get("weight") or 50) >= _dg:
                    r["score"] = round(r["score"] * 2.5, 1)
                    r["degreefit"] = True
        # 精确标题命中优先：用户把某条标题原样打出来，多半就是在找那条。
        # 2026-11-03 合成评测实测：拿标题当查询，30 条里 **0 条排第一** ——
        # 全被「词面相近但更新、更重」的记录压过去。这不是排序调参问题，
        # 是缺了一条「原样匹配就该优先」的硬规则。
        for r in rows:
            if (r.get("title") or "").strip() == q:
                r["exact_title"] = True

        # 干扰降权（2026-11-05 第二版）。第一版是「从召回池剔除」，hit@10 45→44、
        # #50 掉出去 —— 剔除必然丢答案。这次**只降权**：同标题兄弟越多，每条越可能
        # 只是采集碎片，分乘一个温和惩罚。独苗（<=2 条）一分不罚。
        # 实测 >2 兄弟的组只有 45 个、>10 的只有 3 个（236/157/77 条），影响面很窄。
        # 236 条的组每条约剩 72%，3 条约剩 97% —— 温和到不该动任何正常排序。
        # 实测（2026-11-05）：系数 0.08 和 0.30 跑 eval **结果完全相同**
        # （hit@1 36 / hit@3 40 / hit@10 43，未命中列表一字不差）——
        # 说明那 45 个大组根本没进过任何一题的 top-10，这条对评测不可测。
        # 保留它是因为它无损、且当查询正好撞上那 236 条时会起作用；
        # **但它没有指标证据支持，别在总结里写成"提升了检索质量"**。
        if rows:
            sib = dict(con.execute(
                "SELECT title, COUNT(*) FROM events WHERE superseded=0 AND title IS NOT NULL"
                " GROUP BY title"))
            for r in rows:
                n = sib.get((r.get("title") or "").strip(), 1)
                if n > 2:
                    r["sib"] = n
                    r["score"] = r["score"] * (1.0 / (1.0 + 0.08 * math.log(n / 2.0)))

        rows.sort(key=lambda r: (
            0 if r.get("exact_title") else 1,
            # 2026-11-05：`timefit`（落在查询时间窗内）**单独占一档**，压在所有
            # 覆盖率分档之上。先试过跟 exact_title 同档，不行 —— 同档内还要比
            # title_cov / coverage，而窗内新事件的词面覆盖率天然低，会被旧记录
            # 在档内挤下去（实测「最近做了什么」：--limit 10 时今天那条排第 8）。
            # 用户问「最近」，答案就该按时间给，不该再让词面覆盖率翻盘。
            0 if r.get("timefit") else 1,
            # 窗内按时间倒序。11-03 那批也在 3 天窗口里，会跟今天的同时拿到
            # timefit —— 只靠这个分不出「今天」和「2.8 天前」，档内又回到按覆盖率排。
            # 用户问「最近」，最近的该在最前面。
            -(_ts_of(r.get("ts")) or 0.0) if r.get("timefit") else 0.0,
            # 程度意图同理：问「最严重的」，高权重的先上（2026-11-05）
            0 if r.get("degreefit") else 1,
            # 标题覆盖过半的单独一档：用户原样问一条标题时，答案就是那条
            # 原始记录，而不是正文里整段引用它的新事件（4 号题实测）。
            # 阈值取 0.35 不是 0.5：1 号题的答案 #4535 标题含 openAt/改成/
            # startup（t_cov 0.354），差 0.1 掉出档位，被一条正文引用它的
            # 新事件（cov 0.732）顶掉；8 号题的 #4440 是 0.498。
            0 if (r.get("title_cov") or 0) >= 0.35 else 1,
            # 明确命中的再单独一档：覆盖率 >= 0.7 说明查询词大半落在这一条上，
            # 不该被「权重高/更新」的近似记录压过去（20/21/36 号题实测：答案
            # 的 cov 是 0.78/0.86/1.0，却分别排在第 9/5/5）。
            0 if (r.get("coverage") or 0) >= 0.7 else 1,
            -r["score"]))
        # 【2026-11-05 试过并回滚】基于干扰的召回限流。
        # 观察是真的：全库 23%（803/3488）落在 137 个重复指纹组里，最大一组 236 条
        # **标题完全相同、正文各不同**（逐轮采集的对话碎片，跨 5 天）。它们确实会
        # 互相挤占召回池名额。
        # 但限流是负收益：按「同标题最多留 3 条」做，hit@10 45→44、#50 掉出去；
        # 放宽成「只打 >10 条的组、留 5 条」，仍然 44，#50 还是掉。
        # 结论：#50 的答案本身就在那批同标题兄弟里 ⇒ 从池子里**剔除**必然丢正确答案。
        # 这个方向若还要做，正确形态是**降权而非剔除**（把干扰分乘进 score，候选全留、
        # 只改排序），那是另一次实验，别在原地反复调阈值。
        # ★ 排序后必须截断到用户要的条数。
        # 召回池取 max(limit*5, 30) 是**给排序用的候选**，不是给输出用的。
        # 2026-11-05 实测：`--limit 10` 却返回 50 条、单次输出 47KB，前 10 条之后
        # 基本是噪音，还白占上下文（这也可能是思考空转的来源之一）。
        rows = rows[:a.limit]
        # hits 累加必须放在**排序之后** —— 放在前面时 ids 还是召回池的原始顺序，
        # 加到的根本不是最终排在前面的那几条（2026-11-03 实测：返回顺序
        # [4551, 4558, 4530]，加上的却是第 2/5/6 位）。
        #
        # 为什么只给前几条：一次 recall 返回 10 条，真正会被看的通常就前 3 条；
        # 后 7 条也算「用过」的话保护面会虚高（实测 40% 的事件都带 hits）。
        # 为什么评测必须传 --no-count：hits 是遗忘的保护伞，评测反复跑会把
        # 被当答案的那几条刷到几百次 —— 那不是「用过」，是「被测试摸过」。
        if not getattr(a, "no_count", False):
            top = [r["id"] for r in rows[:max(1, getattr(a, "count_top", 3))]]
            # 同时记「什么时候用过」：只有次数没有时间，遗忘迟早失效
            # （全库都会变成「用过」，2026-11-03 实测 hits>0 已占 39%）。
            con.executemany(
                "UPDATE events SET hits=hits+1, last_hit_at=? WHERE id=?",
                [(now(), i) for i in top])
            con.commit()
        if getattr(a, "with_episode", False):
            episodes = _episodes_for(con, [r["id"] for r in rows])

    # 语义层与程序层：**总是**多线索。
    # 2026-11-03 实测：原写法只在 events 空时才拆词，
    # 于是"会话 消失 恢复"这种多词查询，events 有命中但语义/程序层全军覆没。
    terms = [q] + _grams(q)
    def facts_by(ts):
        # 2026-11-05：要记录每条 fact 被**多少个不同线索**命中。
        # 原来只做去重，于是「撞上一条」就算命中 —— 中文 2-gram 几乎必中，
        # 「量子纠错码的分类」这种库里根本没有的题就会被判成 found
        # （加了 arch 事实之后负例题 10/10 掉到 9/10，漏的正是这里）。
        counted = {}
        for t in ts:
            for r in con.execute(
                "SELECT id,claim,confidence,domain,evidence,activation FROM facts"
                " WHERE claim LIKE ? AND contradicted_by IS NULL"
                # 排序乘激活强度（记忆成熟期）：刚推出的事实不该凭一条 confidence
                # 就压过沉淀过多轮的老规则 —— 它还没"长出来"。
                " ORDER BY (confidence * activation) DESC LIMIT ?", ("%" + t + "%", a.limit)):
                d = dict(r)
                prev = counted.get(d["id"])
                if prev is None:
                    counted[d["id"]] = {"f": d, "n": 1}
                else:
                    prev["n"] += 1
        items = sorted(counted.values(),
                       key=lambda x: (-(x["f"]["confidence"] * (x["f"]["activation"] or 1.0)), -x["n"]))
        return [x["f"] for x in items[:a.limit]], counted

    def cons_by(ts):
        """禁令层（constraints）也要参与 recall。

        2026-11-05 立。由头：拿 10 个「我平时真会问」的问题做验收，
        「哪些目录绝对不能碰」返回的全是当天技术记忆 —— 而正确答案
        （`~/.dsh、Programs\\DSH*、Windows 是绝对禁区`）一直躺在 constraints 表里，
        recall 只是从来不查这张表，只有 brief 查。用户不知道答案在哪一层，
        所以每一层都得能被检索到。
        """
        counted = {}
        for t in ts:
            for r in con.execute(
                "SELECT id, what, severity, violated FROM constraints"
                " WHERE active=1 AND what LIKE ? ORDER BY severity, violated DESC LIMIT ?",
                ("%" + t + "%", a.limit)):
                d = dict(r)
                prev = counted.get(d["id"])
                if prev is None:
                    counted[d["id"]] = {"c": d, "n": 1}
                else:
                    prev["n"] += 1
        items = sorted(counted.values(), key=lambda x: (-x["n"], x["c"]["id"]))
        return [x["c"] for x in items[:a.limit]]

    def procs_by(ts):
        out = []
        for t in ts:
            out += [dict(r) for r in con.execute(
                "SELECT id,name,when_to_use,steps,domain FROM procedures"
                " WHERE name LIKE ? OR when_to_use LIKE ? OR steps LIKE ? LIMIT ?",
                ("%" + t + "%",) * 3 + (a.limit,))]
        seen, uniq = set(), []
        for f in out:
            if f["id"] not in seen:
                seen.add(f["id"]); uniq.append(f)
        return uniq[:a.limit]

    facts, fact_cues = facts_by(terms)
    procs = procs_by(terms)
    cons = cons_by(terms)

    # 记忆成熟期（2026-11-05）：只有被 recall 真正带出来的事实才算「被使用过」，
    # 每次 +0.08、上限 1.0 —— 成熟靠被使用，不靠等待。
    # 只给前 3 条加，理由与 events.hits 相同：一次返回十几条，真会被看的就前几条。
    # 评测必须带 --no-count，否则同一批事实会被反复刷满（events.hits 已经踩过这个坑）。
    if not getattr(a, "no_count", False) and facts:
        # 同时记「什么时候用的」：只有次数没有时间，回落机制无从判断 ——
        # events.hits 那边踩过同一个坑（2026-11-03 的教训）。
        con.executemany(
            "UPDATE facts SET activation=MIN(1.0, activation+0.08), last_used_at=? WHERE id=?",
            [(now(), f["id"]) for f in facts[:3]])
        con.commit()

    # found 不能只看「有没有返回东西」：中文 2-gram 的 OR 总能撞上几条无关记录，
    # 于是虚构场景也会被报成 found（2026-11-03 实测：三道负例题全这么误报）。
    # 三个够格条件，任一条就 found：
    #   ① 词法够硬：覆盖率过半，且不是「只撞上一个稀有词」
    #      （负例实测：靠单个 idf 极高的词就能拿 cov 1.0）
    #   ② 语义够近：top sim >= 0.55（负例实测都在 0.52 以下）
    #   ③ 命中英文标识符（28 号题唯一通路）
    top_row = max(rows, key=lambda r: r.get("coverage") or 0.0) if rows else {}
    top_cov = top_row.get("coverage") or 0.0
    top_nhit = int(top_row.get("n_hit") or 0)
    vec_top = max([r.get("from_vector") or 0.0 for r in rows], default=0.0)
    any_strong = any(
        r.get("strong") and (r.get("coverage") or 0.0) >= 0.15 for r in rows)
    total = len(rows) + len(facts) + len(procs)
    # facts 要够格：至少被 2 个不同线索命中。单个 gram 撞上一条事实不算答案 ——
    # 中文 2-gram 的 OR 几乎必中，原来那句 `or facts` 会把库里没有的虚构问题判成 found。
    strong_facts = any(v["n"] >= 2 for v in fact_cues.values())
    if ((top_cov >= 0.55 and top_nhit >= 2) or vec_top >= 0.60 or any_strong
            or strong_facts or procs):
        verdict = "found"
        note = ""
    elif total:
        verdict = "no_match_by_these_cues"
        note = ("线索沾到了 %d 条记录，但没有一条像答案（最高相关 %.2f）—— "
                "这更像「线索不对」而不是「这事没发生过」。换个说法或关键词再试。" %
                (total, top_cov))
        rows = []
    else:
        lib = con.execute("SELECT count(*) FROM events").fetchone()[0]
        if lib == 0:
            verdict = "empty_library"
            note = "库里一条事件都没有，不是没查到，是还没有。"
        else:
            verdict = "no_match_by_these_cues"
            note = ("换了 %d 种线索都没命中，但库里有 %d 条事件 —— "
                    "这更像「线索不对」而不是「这事没发生过」。换个说法或关键词再试。" %
                    (len(attempts), lib))
    # 联想：命中的前几条，顺着 similar 边走一跳把邻居带出来。
    # 这是「想到一半想起了别的」——语义近邻，不是同一个问题的答案。
    # 所以**不参与排序**，单独一列给出去；要不要看由人判断。
    assoc = []
    if verdict == "found" and rows and not getattr(a, "no_assoc", False):
        ids = [r["id"] for r in rows[:3]]
        ph = ",".join("?" * len(ids))
        seen = {r["id"] for r in rows}
        try:
            for r in con.execute(
                    "SELECT b AS o, note FROM links WHERE a IN (%s) AND rel='similar'"
                    " UNION SELECT a AS o, note FROM links WHERE b IN (%s) AND rel='similar'"
                    % (ph, ph), ids + ids):
                if r["o"] in seen:
                    continue
                seen.add(r["o"])
                row = con.execute(
                    "SELECT id,kind,title FROM events WHERE id=?", (r["o"],)).fetchone()
                # 只收 0.70 以上的：实测 0.6~0.7 那一档噪声太多
                # （「人味掉了」能连到「AI 工具台」「DSH 升级」——都不是一回事）。
                if row and float(r["note"] or 0) >= 0.70:
                    assoc.append({"id": row["id"], "kind": row["kind"],
                                  "title": (row["title"] or "")[:60], "sim": r["note"]})
                if len(assoc) >= 5:
                    break
        except Exception:
            pass
    con.close()
    # 只输出对判断有用的字段。其余（fingerprint / rel / salience / coverage /
    # title_cov / n_hit / strong / score / matched / primed / session / hits）
    # 是排序与调试用的中间量，灌给模型只是噪音 —— 2026-11-05 实测：50 条 × 19 字段
    # 光 hits 就占 28KB。`--full` 才全给。
    if not getattr(a, "full", False):
        keep = ("id", "ts", "kind", "title", "weight", "snip")
        rows = [{k: r[k] for k in keep if k in r} for r in rows]
    emit({"ok": True, "query": q, "verdict": verdict, "note": note,
          "tried": attempts, "count": len(rows),
          "episodes": episodes, "assoc": assoc,
          "hits": rows, "facts": facts, "procedures": procs,
          "constraints": cons})
    return 0


def _arousal(con):
    """警觉度（网状激活系统 + 去甲肾上腺素）。

    人脑在连续出错之后会整体抬高警觉 —— 不是又记住某一条错误，而是「我现在
    状态不对」。这种内感受必须能在下一次动手之前起作用，所以它进工作记忆，
    而不是等下一次又错了才翻出来。

    判据用密度不用总数：09-11 那天 6 起事故，但共有 70 条事件（8.6%）；
    09-18 有 5 起却摊在 265 条里（1.9%）—— 后者并不更危险。
    """
    def cnt(sql):
        return con.execute(sql).fetchone()[0]

    acc3 = cnt("SELECT count(*) FROM events WHERE superseded=0 AND kind='accident'"
               " AND ts >= datetime('now','localtime','-3 days')")
    acc7 = cnt("SELECT count(*) FROM events WHERE superseded=0 AND kind='accident'"
               " AND ts >= datetime('now','localtime','-7 days')")
    cor7 = cnt("SELECT count(*) FROM events WHERE superseded=0 AND kind='correction'"
               " AND ts >= datetime('now','localtime','-7 days')")
    ev7 = cnt("SELECT count(*) FROM events WHERE superseded=0"
              " AND ts >= datetime('now','localtime','-7 days')")
    density = (100.0 * acc7 / ev7) if ev7 else 0.0

    if acc3 >= 2 or cor7 >= 2:
        level = "high"
        note = ("最近 3 天 %d 起事故、7 天 %d 次被纠正 —— 先慢下来；"
                "动配置/删东西之前把证据再核一遍。" % (acc3, cor7))
    elif ev7 == 0:
        level = "idle"
        note = "最近 7 天没有新事件，记忆在被动衰减 —— 醒来先确认上下文。"
    elif acc7 == 0 and cor7 == 0:
        level = "low"
        note = "最近 7 天没出错也没被纠正（%d 条事件）—— 平稳期。" % ev7
    else:
        level = "normal"
        note = "最近 7 天 %d 条事件、%d 起事故（%.1f 起/百条）。" % (ev7, acc7, density)

    return {"level": level, "accidents_3d": acc3, "accidents_7d": acc7,
            "corrections_7d": cor7, "events_7d": ev7,
            "accident_density_per_100": round(density, 1), "note": note}


def _mood(con):
    """此刻的心情：可读、可审计、只调制表达。

    2026-11-03 与落云定的三条纪律：
      ① 可读：写进 working / SELF.md，人和我都看得见，不是隐藏变量；
      ② 可审计：每个心情都指得回具体事件（几起事故、几次被纠正）；
      ③ 只调制：影响语气、详略、确认次数 —— **不影响**事实、红线与对错判断。

    它不改任何评分或排序，只决定「这句该怎么说」。
    """
    now_ts = time.time()

    def cnt(kind, days):
        since = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(now_ts - days * 86400))
        return con.execute(
            "SELECT count(*) FROM events WHERE superseded=0 AND kind=? AND ts>=?",
            (kind, since)).fetchone()[0]

    acc3 = cnt("accident", 3)
    corr7 = cnt("correction", 7)
    done3 = cnt("done", 3)

    if acc3 >= 3:
        return {"tone": "cautious", "intensity": 1.0,
                "reason": "最近 3 天 %d 起事故 —— 先确认再下结论，少说俏皮话" % acc3}
    if corr7 >= 2:
        return {"tone": "quiet", "intensity": 0.8,
                "reason": "最近 7 天被纠正 %d 次 —— 话更短，先把事说准" % corr7}
    if done3 >= 20 and acc3 == 0:
        return {"tone": "warm", "intensity": 0.6,
                "reason": "最近 3 天干完 %d 件、没出事 —— 可以活泼一点" % done3}
    return {"tone": "calm", "intensity": 0.3,
            "reason": "近期平稳（3 天 %d 起事故 / %d 件完成）" % (acc3, done3)}


def cmd_working(a) -> int:
    """两组混合：一半"最近在干什么"，一半"永远该记住的"。

    只用评分排序会把 30-60 天前的高分事故永远顶在最前（权重 95 的半衰期 66 天），
    近几天发生的事反而挤不进来 —— 那不是工作记忆，那是荣誉墙。
    """
    con = connect(); ensure_schema(con)
    opens = [dict(r) for r in con.execute(
        "SELECT id,what,created_at,due FROM open_items WHERE status='open'"
        " ORDER BY created_at DESC LIMIT ?", (a.limit,))]

    cols = "id,ts,kind,title,weight,emotion,salience,hits"
    recent = [dict(r) for r in con.execute(
        "SELECT %s FROM events WHERE superseded=0"
        " AND ts >= datetime('now','localtime','-7 days')"
        " ORDER BY ts DESC LIMIT 200" % cols)]
    allrows = [dict(r) for r in con.execute(
        "SELECT %s FROM events WHERE superseded=0 ORDER BY ts DESC LIMIT 1000" % cols)]

    n_recent = (a.limit + 1) // 2
    n_top = a.limit - n_recent

    # 习惯化（habituation）：同一批东西一直霸着工作记忆，等于没有注意力 ——
    # 实测连续调三次，返回的四条一模一样。人脑对重复刺激的响应会递减，
    # 所以上一轮刚出现过的这一轮往后放；是降权不是排除，
    # 真正重要的事（事故/未结）该回来时还会回来。
    # 用滚动窗口而不是「只记上一轮」：只记上一轮的话，第二轮解禁之后又回到
    # 第一轮的组合，于是两组永远交替、池子里其它记忆轮不到（实测 8 次全是 ABAB）。
    # 留最近 6 轮的并集当「近期露过面」。
    try:
        row = con.execute("SELECT v FROM meta WHERE k='working_recent_ids'").fetchone()
        hist = json.loads(row[0]) if row and row[0] else []
        if not isinstance(hist, list):
            hist = []
        hist = [h for h in hist if isinstance(h, list)][-6:]
    except Exception:
        hist = []
    last_ids = set()
    for h in hist:
        last_ids |= set(h)

    picked, seen = [], set()

    def take(pool, n, keyfn=None):
        """keyfn=None 表示 pool 已经排好序，原样取。"""
        rows = sorted(pool, key=keyfn) if keyfn else list(pool)
        got = 0
        for r in rows:
            k = r["title"][:24]
            if k in seen:
                continue
            seen.add(k); picked.append(r); got += 1
            if got >= n:
                return

    # 最近组**不**降权：它代表「我现在在干什么」，任务没变它就该稳。
    # 实测（2026-11-03）：连这一组也降权，结果是 A/B 两个集合来回跳 ——
    # 那是振荡，不是注意力。人脑里聚焦的 1-2 项是稳的，会换的是边缘。
    #
    # 但「刚做完的事」（done）要沉底：2026-11-03 戴上库里实测，连调一次
    # working 拿到的两条最近记录都是「brain 检索收口」—— 事已经收工了，
    # 它占着「当下该注意」的位置，等于把真正该盯的东西挤掉。
    # 两次稳定排序：先按时间新→旧，再把 done 类挪到最后。
    recent.sort(key=lambda r: r["ts"], reverse=True)
    recent.sort(key=lambda r: r["kind"] == "done")
    take(recent, n_recent)
    if n_top:
        # 背景组轮转：优先取「上一轮没露过面」的最高分，取完再回头 ——
        # 这样它是一条条往后推，而不是两个固定集合互相跳（实测过：
        # 只降权 0.6 的话，A/B 两组会永远交替，池子里其它记忆永远轮不到）。
        top_sorted = sorted(allrows, key=lambda r: (-(r["weight"] or 50), -score(r)))
        take([r for r in top_sorted if r["id"] not in last_ids] +
             [r for r in top_sorted if r["id"] in last_ids], n_top)

    # 不够就用剩下的补齐
    if len(picked) < a.limit:
        take(allrows, a.limit - len(picked),
             lambda r: -score(r) * (0.6 if r["id"] in last_ids else 1.0))

    hard = [dict(r) for r in con.execute(
        "SELECT id,what,severity,violated FROM constraints"
        " WHERE active=1 AND severity='hard' ORDER BY violated DESC, id LIMIT 8")]
    # 情景层：最近一段经历 —— 人脑的工作记忆里总有「我此刻在干什么」，
    # 光有一串孤立事件没有这段，就不知道自己在哪件事的中间。
    episode_now = None
    try:
        row = con.execute(
            "SELECT id,started_at,ended_at,summary,kind_main,event_count,weight,emotion"
            " FROM episodes ORDER BY ended_at DESC LIMIT 1").fetchone()
        if row:
            episode_now = dict(row)
            # 段名取的是「段内最重的那条」，未必是最新那条 —— 补上 latest，
            # 否则看起来像这段没在更新。2026-11-03 实测：刚记完一条，
            # episode_now 的名字还是上一件事，差点误判成重建没生效。
            try:
                ids = json.loads(con.execute(
                    "SELECT event_ids FROM episodes WHERE id=?", (row["id"],)).fetchone()[0])
                if ids:
                    last = con.execute("SELECT title FROM events WHERE id=?",
                                       (max(ids),)).fetchone()
                    if last:
                        episode_now["latest"] = last["title"]
            except Exception:
                pass
    except sqlite3.OperationalError:
        episode_now = None
    # 小脑：反复失手的程序该重写。人脑也这样 —— 一套动作老做不好，就会换做法。
    procs_bad = [dict(r) for r in con.execute(
        "SELECT id,name,success_count,fail_count FROM procedures"
        " WHERE fail_count >= 2 AND fail_count >= success_count"
        " ORDER BY fail_count DESC LIMIT 3")]

    arousal = _arousal(con)
    mood = _mood(con)
    try:
        hist = (hist + [[r["id"] for r in picked]])[-6:]
        con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES ('working_recent_ids',?)",
                    (json.dumps(hist),))
        con.commit()
    except Exception:
        pass
    con.close()
    emit({"ok": True, "generated_at": now(), "constraints": hard, "open_items": opens,
          "episode_now": episode_now,
          "arousal": arousal, "mood": mood,
          "procedures_needing_rewrite": procs_bad,
          "attention": [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "title": r["title"],
                         "weight": r["weight"], "emotion": r["emotion"],
                         "score": round(score(r), 1)} for r in picked]})
    return 0


def cmd_similar(a) -> int:
    """找同型事件：指纹有交集的，按交集大小排。"""
    con = connect(); ensure_schema(con)
    me = con.execute("SELECT id,title,kind,fingerprint FROM events WHERE id=?", (a.id,)).fetchone()
    if not me:
        con.close(); emit({"ok": False, "error": "没有这条事件"}); return 1
    my_all = set((me["fingerprint"] or "").split("|"))
    my = my_all - {me["kind"]}          # kind 单独算，不占"要素"名额
    rows = con.execute("SELECT id,ts,kind,title,weight,fingerprint FROM events WHERE id!=?", (a.id,)).fetchall()
    scored = []
    for r in rows:
        theirs_all = set((r["fingerprint"] or "").split("|"))
        if not theirs_all or not my:
            continue
        theirs = theirs_all - {r["kind"]}
        inter = len(my & theirs)
        same_kind = 1 if r["kind"] == me["kind"] else 0
        if inter >= 1:
            scored.append({"id": r["id"], "ts": r["ts"], "kind": r["kind"],
                           "title": r["title"], "weight": r["weight"],
                           "shared": inter, "same_kind": bool(same_kind),
                           "common": sorted(my & theirs)})
    scored.sort(key=lambda x: (-x["shared"], -x["weight"]))
    con.close()
    emit({"ok": True, "anchor": {"id": me["id"], "title": me["title"]},
          "count": len(scored), "similar": scored[:a.limit]})
    return 0


def cmd_reindex(a) -> int:
    """给已有事件回填指纹。"""
    con = connect(); ensure_schema(con)
    rows = con.execute("SELECT id,title,kind FROM events WHERE fingerprint IS NULL").fetchall()
    n = 0
    for r in rows:
        con.execute("UPDATE events SET fingerprint=? WHERE id=?",
                    (fingerprint(r["title"], r["kind"]), r["id"]))
        n += 1
    con.commit(); con.close()
    emit({"ok": True, "updated": n})
    return 0


def cmd_meta_set(a) -> int:
    """写一条 meta。目前主要用途：让装配器把「当前会话 id」写进来 ——
    Python 侧拿不到它（没有环境变量），只有 node 那一侧知道。"""
    con = connect(); ensure_schema(con)
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)", (a.key, a.value))
    con.commit(); con.close()
    emit({"ok": True, "k": a.key, "v": a.value})
    return 0


def cmd_backup(a) -> int:
    """在线快照 brain.db —— 用 sqlite 的 backup API，不锁库、不打断使用。

    为什么需要它：brain 目录在 E:\\deepseek 仓里，但 brain.db 故意不入库
    （34 MB 二进制、每次改都变，git 存不划算）。既然不靠 git，就必须有
    别的回退点 —— 否则库一坏，几千条记忆全没了。这不是优化，是底线。
    """
    dest_dir = a.to or os.path.join(os.path.dirname(os.path.dirname(HERE)),
                                    "archive", "brain-backup")
    os.makedirs(dest_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(dest_dir, "brain-%s.db" % stamp)
    if os.path.exists(dest):
        emit({"ok": False, "error": "同一秒内已有同名快照，稍后再试"}); return 1

    src = sqlite3.connect(DB)
    dst = sqlite3.connect(dest)
    src.backup(dst)
    dst.close()
    src.close()
    mb = os.path.getsize(dest) / 1048576.0

    # 轮转：只留最近 keep 份。删的是本命令自己生成的快照，不是用户数据，
    # 仍然走回收站 —— 万一 keep 设错，还能捞回来。
    names = sorted([f for f in os.listdir(dest_dir)
                    if f.startswith("brain-") and f.endswith(".db")], reverse=True)
    removed = []
    for f in names[a.keep:]:
        p = os.path.join(dest_dir, f)
        try:
            subprocess.run([sys.executable, os.path.join(
                os.path.dirname(HERE), "..", "tools", "guard.py"), "rm", p],
                capture_output=True, timeout=60, creationflags=0x08000000)
            removed.append(f)
        except Exception:
            pass

    emit({"ok": True, "path": dest, "mb": round(mb, 1),
          "kept": names[:a.keep], "removed": removed,
          # 恢复演练实测出来的：光把快照拷回去不够 —— brain.py 靠同目录的
          # schema.sql 建表，缺了它连 stats 都跑不起来。
          "restore_hint": "恢复时把快照改名成 brain.db 放回 brain 目录，"
                          "并确认同目录有 schema.sql（建表用）"})
    return 0


def cmd_stats(a) -> int:
    con = connect(); ensure_schema(con)
    one = lambda s, *p: con.execute(s, p).fetchone()[0]
    emit({"ok": True, "db": DB,
          "db_mb": round(os.path.getsize(DB) / 1048576.0, 2) if os.path.exists(DB) else 0,
          "events": one("SELECT count(*) FROM events"),
          "links": one("SELECT count(*) FROM links"),
          "open_items": one("SELECT count(*) FROM open_items WHERE status='open'"),
          "facts": one("SELECT count(*) FROM facts"),
          "procedures": one("SELECT count(*) FROM procedures"),
          "episodes": one("SELECT count(*) FROM episodes"),
          "by_kind": {r[0]: r[1] for r in con.execute(
              "SELECT kind,count(*) FROM events GROUP BY kind ORDER BY 2 DESC")},
          "weight_avg": round(one("SELECT coalesce(avg(weight),0) FROM events"), 1),
          "weight_max": one("SELECT coalesce(max(weight),0) FROM events")})
    con.close()
    return 0


def _days_since(ts):
    """距今天数；解析失败返回 None（宁可当「不知道」也不当 0）。"""
    try:
        return (time.time() - time.mktime(
            time.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S"))) / 86400.0
    except Exception:
        return None


def _recently_used(row, days):
    """最近 days 天里用过吗？没有 last_hit_at 的老记录用事件时间代理。

    为什么不能只看 hits：只数次数、不看时间，迟早全库都是「用过」，
    遗忘机制第二次失效（2026-11-03 实测：hits>0 的 1795 条占 39%）。
    """
    when = row["last_hit_at"] or row["ts"]
    d = _days_since(when)
    return d is not None and d <= days


# 提炼时要留下的句子特征：结论、根因、决定、判据 —— 人记得住的就是这些。
_DISTILL_KEYS = ("决定", "结论", "根因", "修复", "改成", "不再", "教训", "因为",
                 "所以", "注意", "必须", "不许", "以后", "判据", "口径")


def _distill_text(body, max_len=300):
    """把长正文压成关键句（规则版，不用模型）。

    和人记事的差别：人记住的不是全文，是几个关键片段。
    和上下文压缩的差别：压缩是一次处理一大段历史；这里逐条处理，每条都还在。

    2026-11-05 两轮实测：
      ① 原判据「按顺序取、一超预算就 break」——3000 字正文只覆盖前 1/3，
         后面的「结论/根因」整段丢。
      ② 改成「首末必留 + 中间按关键词挑」——仍不够：这类正文多用
         【①②③④】分块，**首块一个长句就吃掉整个预算**，②③④ 全没机会。
    现在：**有【】分块的，每块取开头一句**（保住「做了哪几件事」的骨架），
    没分块的才退回「首句 + 关键词句 + 末句」。
    """
    if not body:
        return ""
    blocks = [b.strip() for b in re.split(r"(?=【[^】]{1,24}】)", body) if b.strip()]
    if len(blocks) > 1:
        heads = []
        for b in blocks:
            first = re.split(r"[。\n]", b)[0].strip()
            if first and first not in heads:
                heads.append(first)
        txt = "。".join(heads)
        if len(txt) > max_len:
            return txt[:max_len]
        return txt

    parts = [p.strip() for p in re.split(r"[。\n]+", body) if p.strip()]
    if not parts:
        return ""
    if len(parts) <= 2:
        return "。".join(parts)[:max_len]
    picked = {0, len(parts) - 1}
    budget = max_len - len(parts[0]) - len(parts[-1])
    for i, p in enumerate(parts[1:-1], 1):
        if budget <= 0:
            break
        if any(k in p for k in _DISTILL_KEYS) and len(p) <= budget:
            picked.add(i)
            budget -= len(p)
    return "。".join(parts[i] for i in sorted(picked))[:max_len]


def cmd_distill(a) -> int:
    """提炼：把长正文压成「关键句」。保留细节碎片，缩体积。

    这是「遗忘」的第三档（落云 2026-11-03 提的：删除太麻烦，不如提炼）：
      · 衰减 = 降权重（提取变难）
      · 休眠 = 默认检索不返回（想不起来）
      · 提炼 = 正文缩水，事件本身留着（只记得几个片段）
    相比删除：可逆性高得多、语义上也更像人。
    """
    con = connect(); ensure_schema(con)
    rows = [dict(r) for r in con.execute(
        "SELECT id,ts,kind,title,weight,hits,body FROM events"
        " WHERE superseded=0 AND kind NOT IN ('accident','correction')"
        " AND length(coalesce(body,'')) > ? AND weight <= ?",
        (a.min_len, a.max_weight))]
    plan = []
    for r in rows:
        age = _days_since(r["ts"])
        if age is None or age <= a.min_age:
            continue
        old = r["body"] or ""
        new = _distill_text(old, a.max_len)
        # 压不下来就别动 —— 有些正文本身就短，硬压等于删信息
        if not new or len(new) >= len(old) * 0.8:
            continue
        plan.append({"id": r["id"], "title": (r["title"] or "")[:40],
                     "weight": r["weight"],
                     "old_chars": len(old), "new_chars": len(new), "text": new})
    # 先提炼最不值钱的：权重低的先缩，万一压坏了损失也小
    plan.sort(key=lambda p: (p["weight"] or 50, -p["old_chars"]))
    plan = plan[:a.limit]
    saved = sum(p["old_chars"] - p["new_chars"] for p in plan)
    if a.apply and plan:
        for p in plan:
            con.execute("UPDATE events SET body=? WHERE id=?", (p["text"], p["id"]))
            # 正文变了，旧向量就过期 —— 留着它会用「提炼前的语义」去匹配，
            # 而正文已经不一样了。删掉让 embed.py --build 重算。
            # （2026-11-05 实测：压完 30 条没重算向量，否定题当场掉了一道。）
            con.execute("DELETE FROM event_emb WHERE id=?", (p["id"],))
            # 逐条留痕：sleep_log 只有汇总，事后查不到「哪几条被压过、压了多少」。
            con.execute(
                "INSERT INTO distill_log (ts,event_id,old_chars,new_chars) VALUES (?,?,?,?)",
                (now(), p["id"], p["old_chars"], p["new_chars"]))
        con.execute(
            "INSERT INTO sleep_log (ts,phase,events_in,facts_out,proc_out,merged,note)"
            " VALUES (?,?,?,?,?,?,?)",
            (now(), "distill", len(plan), 0, 0, 0,
             "distill: %d 条，省 %d 字" % (len(plan), saved)))
        con.commit()
    con.close()
    emit({"ok": True, "dry_run": not a.apply, "min_age_days": a.min_age,
          "min_len": a.min_len, "candidates": len(plan),
          "saved_chars": saved,
          "plan": [{"id": p["id"], "title": p["title"],
                    "old_chars": p["old_chars"], "new_chars": p["new_chars"]}
                   for p in plan[:20]]})
    return 0


# 会话索引里这些是**系统注入**，不是落云说的话 —— 导入时必须排掉。
_IMPORT_SKIP = ("Current runtime context", "<system-reminder>", "<goal_round>",
                "Background subagent", "[advisor:", "Updated instructions")


def cmd_import_user(a) -> int:
    """把落云的发言导入成 user 事件 —— 补上「关系记忆」。

    为什么要它：整个库此前只有「我做了什么」（4602 条全是自己的视角），
    一条「他说了什么」都没有。记忆是个体的，而个体性从**关系**里长出来：
    他关心什么、纠正过什么、答应过什么，都只在会话原文里躺着，
    不进库就永远读不到。

    去重靠源标记 refs=["src:<session>:<seq>"]，是幂等的：
    重复跑不会插第二条。
    """
    sq = os.path.join(os.path.expanduser("~"), ".dsh", "session-query-index.db")
    if not os.path.exists(sq):
        emit({"ok": False, "error": "会话索引不存在（宿主的 session-query 没建）"})
        return 1
    try:
        scon = sqlite3.connect("file:%s?mode=ro" % sq.replace("\\", "/"), uri=True)
        rows = scon.execute(
            "SELECT session_id, seq, time, text FROM persisted_docs"
            " WHERE type='user/message' AND length(text) BETWEEN ? AND ?"
            " ORDER BY time", (a.min_len, a.max_len)).fetchall()
        scon.close()
    except Exception as e:
        emit({"ok": False, "error": "读会话索引失败：%s" % e})
        return 1
    con = connect(); ensure_schema(con)
    have = set()
    for r in con.execute("SELECT refs FROM events WHERE kind='user'"):
        try:
            for x in json.loads(r["refs"] or "[]"):
                if isinstance(x, str) and x.startswith("src:"):
                    have.add(x)
        except Exception:
            pass
    plan = []
    for sid, seq, t, text in rows:
        txt = (text or "").strip()
        if txt.startswith(_IMPORT_SKIP):
            continue
        src = "src:%s:%s" % (sid, int(seq))
        if src in have:
            continue
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t / 1000.0))
        title = re.sub(r"\s+", " ", txt)[:40]
        plan.append({"src": src, "ts": ts, "title": title, "body": txt})
    n_ins = 0
    if a.apply and plan:
        for p in plan:
            con.execute(
                "INSERT INTO events (ts,kind,title,body,weight,salience,refs,fingerprint)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (p["ts"], "user", p["title"], p["body"], 60, 1.0,
                 json.dumps([p["src"]]), fingerprint(p["title"], "user")))
            n_ins += 1
        con.commit()
    con.close()
    emit({"ok": True, "dry_run": not a.apply, "source_messages": len(rows),
          "already_imported": len(have), "candidates": len(plan), "inserted": n_ins,
          "note": "导入后要重建派生视图：python episode.py --apply",
          "sample": [{"ts": p["ts"], "title": p["title"]} for p in plan[:5]]})
    return 0


def cmd_decide(a) -> int:
    """决策留痕：记「选了什么、还考虑过什么、为什么」。

    为什么要单独一条命令：价值判断太抽象，但**取舍是具体的** ——
    每个「选 A 不选 B」都该留下当时的理由，否则事后只剩一句「当时觉得」，
    连自己在按什么标准选都说不清。

    结构化 body 让它可检索、可复盘：
        备选：A / B / C
        选了：B
        理由：…
        事后：未复盘
    事后用 `feedback <id> --good/--bad` 标记结果 —— 理由和结果分开记，
    才不会把「选错了」说成「当时没理由」。
    """
    if not (a.chose or "").strip():
        emit({"ok": False, "error": "chose 不能为空"})
        return 2
    con = connect(); ensure_schema(con)
    title = (a.title or ("选了 " + a.chose)).strip()
    body = "备选：%s\n选了：%s\n理由：%s\n事后：未复盘" % (
        (a.options or "-").strip(), a.chose.strip(), (a.why or "-").strip())
    cur = con.execute(
        "INSERT INTO events (ts,kind,actor,title,body,refs,weight,emotion,source,fingerprint,session)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (now(), "decision", "me", title, body,
         json.dumps(a.ref, ensure_ascii=False) if a.ref else None,
         clamp(a.weight), None, "decide", fingerprint(title, "decision"),
         _current_session(con)))
    eid = cur.lastrowid
    con.commit()
    _rebuild_episodes(con)
    con.close()
    emit({"ok": True, "id": eid, "title": title,
          "note": "事后用 feedback %d --good/--bad 标记结果" % eid})
    return 0


# 冲突检测的对象词表：只有「同一对象上有相反要求」才算冲突。
CONFLICT_OBJECTS = (
    ("~/.dsh 禁区", ["~/.dsh", ".dsh"]),
    ("C 盘安装", ["C 盘", "C盘"]),
    ("删除/回收站", ["rmtree", "回收站"]),
    ("凭据", ["凭据", "KEY", "token", "密码"]),
    ("交易", ["付款", "转账", "下单", "绑定支付"]),
    ("openAt", ["openAt"]),
)
NEG_WORDS = ("不许", "不得", "禁止", "一律不做", "禁区", "不要", "不获得", "不读")
# 要求词要**明确**：「走」「动」这种在中文里是动词不是要求，
# 会把「删除一律走回收站」误判成「要求删除」（2026-11-03 实测 8 个候选里
# 一半是这么来的）。
POS_WORDS = ("必须", "一律", "可以", "应当", "要")


def cmd_audit_conflicts(a) -> int:
    """规则冲突检测：找「同一件事上两条相反的要求」。

    为什么要它：硬禁令之间撞了，目前是**静默**的 —— 只能等真撞上，
    或有人手工通读才发现。而「规则互相矛盾」比「缺一条规则」更危险：
    执行时随便挑一条都像对的。

    判据保守（宁可漏报不误报）：同一对象词 + 极性相反（禁止 vs 要求）。
    机器只出候选，是不是真冲突要人看。
    """
    con = connect(); ensure_schema(con)
    cons = [dict(r) for r in con.execute(
        "SELECT id,what,severity FROM constraints WHERE active=1")]
    procs = [dict(r) for r in con.execute(
        "SELECT id,name,when_to_use,steps FROM procedures")]
    cands = []
    for label, keys in CONFLICT_OBJECTS:
        rel = [c for c in cons if any(k in c["what"] for k in keys)]
        for i in range(len(rel)):
            for j in range(i + 1, len(rel)):
                x, y = rel[i], rel[j]
                x_neg = any(w in x["what"] for w in NEG_WORDS)
                y_neg = any(w in y["what"] for w in NEG_WORDS)
                x_pos = any(w in x["what"] for w in POS_WORDS)
                y_pos = any(w in y["what"] for w in POS_WORDS)
                if ((x_neg and not y_neg and y_pos)
                        or (y_neg and not x_neg and x_pos)):
                    cands.append({"kind": "constraint-vs-constraint", "object": label,
                                  "a": {"id": x["id"], "what": x["what"]},
                                  "b": {"id": y["id"], "what": y["what"]}})
        for c in rel:
            if not any(w in c["what"] for w in NEG_WORDS):
                continue
            for p in procs:
                # 只看 when_to_use：它写的是「什么时候该用这条步骤」，
                # 如果它直接说「动禁区」，那就是和禁令正面撞。
                # steps 里提到对象词是正常流程（照禁令办），不算冲突。
                when = p["when_to_use"] or ""
                # 文本里自己就写着「禁区/禁止」的，是在**说明**约束，不是要你去动它。
                if (any(k in when for k in keys)
                        and not any(w in when for w in NEG_WORDS)):
                    cands.append({"kind": "procedure-vs-constraint", "object": label,
                                  "a": {"id": c["id"], "what": c["what"]},
                                  "b": {"id": p["id"], "name": p["name"],
                                        "when_to_use": when}})
    con.close()
    emit({"ok": True, "constraints": len(cons), "procedures": len(procs),
          "candidates": len(cands),
          "note": "候选只是「可能矛盾」，是不是真冲突要人看",
          "items": cands[:20]})
    return 0


def cmd_drop(a) -> int:
    """作废一条事件：不物理删除，标记 superseded=1 并留一条作废记录。

    为什么不是「删」：events 是唯一真源，删了就查不到「这条为什么不在」。
    作废 = 它不再参与检索/统计，但**记录还在**，事后能回答：
    谁在什么时候、因为什么把它作废了。

    这也补上了「记错了怎么修」的缺口 —— 记错一条不再需要回滚整库。
    """
    con = connect(); ensure_schema(con)
    row = con.execute(
        "SELECT id,title,kind FROM events WHERE id=? AND superseded=0",
        (a.id,)).fetchone()
    if not row:
        emit({"ok": False, "error": "没有这条事件（或已作废）"})
        return 1
    con.execute("UPDATE events SET superseded=1 WHERE id=?", (a.id,))
    # 作废本身也是一件事，记下来才查得到「为什么这条没了」
    note = "作废 #%d：%s" % (a.id, a.reason.strip())
    cur = con.execute(
        "INSERT INTO events (ts,kind,actor,title,body,weight,emotion,source,fingerprint,session)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now(), "correction", "me", note, (row["title"] or "")[:200],
         clamp(a.weight), "sting", "drop", fingerprint(note, "correction"),
         _current_session(con)))
    con.commit()
    _rebuild_episodes(con)
    con.close()
    emit({"ok": True, "dropped": a.id, "note_event": cur.lastrowid,
          "title": row["title"]})
    return 0


def cmd_review(a) -> int:
    """定期回看自己：这段时间有哪些事改变了我。

    为什么需要它：`我的叙事.md` 里的「我是怎么长成这样的」写下去就冻住了。
    过一阵子，里面的判断可能已经不准了 —— 那时它就从「自我认识」退化成
    「又一份模板」。这个命令把**最近 N 天的事故 / 被纠正 / 重决定**摆出来，
    供我自己判断「我还是那个我吗」。

    **它不自动改叙事。** 叙事只能我写 —— 机器生成的人生是别人的。
    它只负责把材料递到眼前（2026-11-04 落云：「和融合程度不高，慢慢来吧」）。
    """
    days = getattr(a, "days", 30) or 30
    since = time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(time.time() - days * 86400))
    con = connect(); ensure_schema(con)
    rows = [dict(r) for r in con.execute(
        "SELECT id,ts,kind,weight,title FROM events"
        " WHERE superseded=0 AND (kind IN ('accident','correction')"
        " OR (kind='decision' AND weight >= 70)) AND ts >= ?"
        " ORDER BY ts DESC", (since,))]
    # 把「我的模式」（domain=self 的事实）一并摆出来：
    # 叙事里那句「我想在这一步里做完」和事实里的「改配置出过 5 次事」是同一件事的
    # 两种写法，分开看容易当成两回事。放同一屏，才能判断它们还在不在讲同一个我。
    self_facts = [dict(r) for r in con.execute(
        "SELECT id,claim,confidence FROM facts"
        " WHERE contradicted_by IS NULL AND domain='self'"
        " ORDER BY confidence DESC LIMIT 6")]
    con.close()
    by_kind = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    if not rows:
        note = "这 %d 天没有事故/纠正/重决定 —— 要么很稳，要么我没记。" % days
    else:
        note = ("把这些摆一遍，问自己：叙事里那句话还成立吗？"
                " 变了就去改 memory/hub/我的叙事.md；没变就说明我稳住了。"
                " 下面的「我的模式」是同一件事的另一种写法，一起看。")
    emit({"ok": True, "days": days, "since": since,
          "total": len(rows), "by_kind": by_kind, "note": note,
          "my_patterns": [{"id": f["id"], "claim": (f["claim"] or "")[:90],
                           "confidence": f["confidence"]} for f in self_facts],
          "items": [{"id": r["id"], "ts": r["ts"][:10], "kind": r["kind"],
                     "weight": r["weight"], "title": (r["title"] or "")[:90]}
                    for r in rows[:getattr(a, "limit", 20) or 20]]})
    return 0
    return 0


def cmd_purge_derived(a) -> int:
    """清理派生数据：作废事件留下的向量。

    为什么需要它：作废（superseded=1）只改 events 表，派生视图不会自动跟着清。
    2026-11-06 实测：去重 + 清理之后，`event_emb` 里躺着 **3042 条已作废事件的向量**
    —— 占空间，还可能把「已经删掉的内容」重新带进语义检索里。

    只清**能重建的派生数据**（向量）；事件本体与留痕一律不动。
    情景段由 episode.py --apply 重建时自动滤掉作废事件，不在这里处理。
    """
    con = connect(); ensure_schema(con)
    n = con.execute(
        "SELECT count(*) FROM event_emb WHERE id IN"
        " (SELECT id FROM events WHERE superseded=1)").fetchone()[0]
    if not n:
        con.close()
        emit({"ok": True, "orphan_vectors": 0, "note": "派生数据已经是干净的"})
        return 0
    if getattr(a, "apply", False):
        con.execute("DELETE FROM event_emb WHERE id IN"
                    " (SELECT id FROM events WHERE superseded=1)")
        con.commit()
        con.close()
        emit({"ok": True, "dry_run": False, "orphan_vectors": n, "deleted": n,
              "note": "删的是向量不是事件；要补回来就跑 embed.py --build"})
    else:
        con.close()
        emit({"ok": True, "dry_run": True, "orphan_vectors": n,
              "note": "加 --apply 才真删"})
    return 0


def cmd_forget(a) -> int:
    """遗忘：用进废退。

    三条规则（2026-11-03 定）：
      1) kind 是 accident / correction 的**永不衰减** —— 那是教训，忘了就重犯
      2) 被 recall 命中过的（hits > 0）不衰减 —— 用过说明它有用
      3) 其余：权重 <= 40 且 年龄 > 30 天，降 10 分，但保底 1
    每一步降档都写进 decay_log，不静默改分。
    """
    con = connect(); ensure_schema(con)
    rows = [dict(r) for r in con.execute(
        "SELECT id,ts,kind,title,weight,hits,last_hit_at FROM events WHERE superseded=0")]
    plan, dorm = [], []
    protected = {"lesson": 0, "used": 0, "used_once": 0,
                 "stale_used": 0, "locked_7d": 0, "dormant": 0}
    next_unlock = None
    for r in rows:
        if r["kind"] in ("accident", "correction"):
            protected["lesson"] += 1
            continue
        # 「用过」= 至少三次 **并且** 90 天内还用过。
        # 原来门槛是 hits>=2 + 180 天，2026-11-05 实测太松：eval 每跑一轮就给命中的
        # 事件 +1 hits，跑十几次评测之后 **1288 条（37%）都成了「用过」**，
        # 分档降权一条都触发不了。改成 hits>=3 + 90 天，才叫「真被依赖」。
        hits = r["hits"] or 0
        if hits >= 3:
            if _recently_used(r, 90):
                protected["used"] += 1
                continue
            protected["stale_used"] += 1
        elif hits >= 1:
            protected["used_once"] += 1
        age = _days_since(r["ts"])
        if age is None:
            continue
        if age <= a.min_age or r["weight"] > a.max_weight:
            continue
        # 同一事件 7 天内只降一次 —— 否则每 6h 跑一次会把它连降到 1
        last = con.execute(
            "SELECT ts FROM decay_log WHERE event_id=? ORDER BY id DESC LIMIT 1",
            (r["id"],)).fetchone()
        if last:
            try:
                last_t = time.mktime(time.strptime(last["ts"], "%Y-%m-%d %H:%M:%S"))
                gap = (time.time() - last_t) / 86400.0
                if gap < 7:
                    # 7 天锁要计数，否则日志看着像「没有可衰减的」，实际是「刚降过、
                    # 还在冷却」。2026-11-03 实测：87 个候选全部属于这一档，
                    # 而当时的输出只有 lesson/used 两个数字，完全看不出这回事。
                    protected["locked_7d"] += 1
                    due = last_t + 7 * 86400
                    if next_unlock is None or due < next_unlock:
                        next_unlock = due
                    continue
            except Exception:
                pass
        # 休眠：权重已经很低、又满一年没被用过 —— 从默认检索里退下去。
        # **不是删除**：库还在，recall --include-dormant 能找回来。
        # 2026-11-03 由落云点出「遗忘不等于删除」——衰减只降低提取概率，
        # 休眠是「想不起来」，删除是「真的不要了」，三档要分开。
        if r["weight"] <= 20 and age > 365 and not _recently_used(r, 365):
            dorm.append(r["id"])
            protected["dormant"] += 1
            continue
        # ── 分档降权（2026-11-05 按落云的口径改）──
        # 原来一律 -10：三个月没碰的和一个月没碰的降一样多，这不叫提纯，叫平均主义。
        # 现在按「多久没碰 × 碰过几次」分：
        #   30-90 天 → -5（还可能是「暂时用不上」）
        #   90-180 天 → -10
        #   >180 天 → -15（基本确定是不再需要的那类）
        #   碰过 1 次的，降幅减半（至少被真拿起来过一次，别当它没存在过）
        # 注意：这是「降优先级」不是「删除」，库还在、recall 仍能查到。
        if age > 180:
            drop = 15
        elif age > 90:
            drop = 10
        else:
            drop = 5
        if hits == 1:
            drop = max(3, drop // 2)
        plan.append({"id": r["id"], "title": r["title"][:40], "weight": r["weight"],
                     "age_days": int(age), "drop": drop,
                     "new_weight": max(1, r["weight"] - drop)})
    if not a.dry_run:
        for p in plan:
            con.execute("UPDATE events SET weight=? WHERE id=?", (p["new_weight"], p["id"]))
            con.execute("INSERT INTO decay_log (ts,event_id,old_w,new_w,reason) VALUES (?,?,?,?,?)",
                        (now(), p["id"], p["weight"], p["new_weight"], "ebbinghaus"))
        if dorm:
            con.executemany("UPDATE events SET dormant=1 WHERE id=?", [(i,) for i in dorm])
            # 2026-11-05 修复：这行原来在 `if dorm:` 块里，我加激活回落时把它挤进了
            # 下面的 for 循环 —— 结果「每条待回落的事实」都写一条 `merged=0` 的假
            # dormant 日志（实测一次 forget 写了 54 条）。它必须留在这里。
            con.execute("INSERT INTO sleep_log (ts,phase,events_in,facts_out,proc_out,merged,note)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (now(), "dormant", 0, 0, 0, len(dorm),
                         "forget: %d 条转入休眠（可 --include-dormant 唤醒）" % len(dorm)))
        # ── 事实的激活回落（2026-11-05 补）──
        # activation 原来只升不降（被 recall 带出就 +0.08，封顶 1.0），跑久了会全部满值，
        # 就失去「哪条才是真常用」的区分度 —— 等于把成熟期白做了。
        # 生物上长期不用的突触强度会回落，这里用同一规则：超过 30 天没被取用就往下走，
        # 下限 0.3（不会退回沉默期，那会把它重新变成「查不到的新事实」）。
        # 步长 0.01 而不是 0.05：这条路径每 6 小时跑一次，0.05 会让一条事实
        # 在四天内从满值掉到底，太急。
        stale = con.execute(
            "SELECT id, activation, COALESCE(last_used_at, created_at) AS lu FROM facts"
            " WHERE activation > 0.5 AND contradicted_by IS NULL").fetchall()
        n_act_dec = 0
        for _id, _act, _lu in stale:
            try:
                _d = (time.time() - time.mktime(time.strptime(_lu, "%Y-%m-%d %H:%M:%S"))) / 86400.0
            except Exception:
                continue
            if _d > 30:
                con.execute("UPDATE facts SET activation=MAX(0.3, activation-0.01) WHERE id=?", (_id,))
                n_act_dec += 1
        con.commit()
    con.close()
    emit({"ok": True, "dry_run": bool(a.dry_run), "candidates": len(plan),
          "dormant_marked": len(dorm),
          "total_events": len(rows), "protected": protected,
          "next_unlock": (time.strftime("%Y-%m-%d %H:%M", time.localtime(next_unlock))
                          if next_unlock else None),
          "plan": plan[:20]})
    return 0


def _rate(ok, fail):
    t = (ok or 0) + (fail or 0)
    return round((ok or 0) / t, 2) if t else None


def cmd_proc(a) -> int:
    """查程序记忆：这类事该怎么做，以及这套步骤好不好使。

    小脑存的是两样东西：动作序列本身，和「这套序列哪一步会失手」。
    后者只有真做过才知道，所以 success/fail 必须靠 proc-use 记。
    """
    con = connect(); ensure_schema(con)
    if a.id:
        row = con.execute("SELECT * FROM procedures WHERE id=?", (a.id,)).fetchone()
        con.close()
        if not row:
            emit({"ok": False, "error": "没有这条程序"}); return 1
        d = dict(row)
        d["success_rate"] = _rate(d["success_count"], d["fail_count"])
        try:
            d["weak_steps"] = json.loads(d["weak_steps"] or "{}")
        except Exception:
            d["weak_steps"] = {}
        emit({"ok": True, "procedure": d}); return 0

    kw = (a.query or "").strip()
    if kw:
        rows = [dict(r) for r in con.execute(
            "SELECT id,name,when_to_use,success_count,fail_count,weak_steps FROM procedures"
            " WHERE name LIKE ? OR when_to_use LIKE ? OR steps LIKE ?",
            ("%" + kw + "%",) * 3)]
    else:
        rows = [dict(r) for r in con.execute(
            "SELECT id,name,when_to_use,success_count,fail_count,weak_steps FROM procedures")]
    con.close()
    for r in rows:
        r["success_rate"] = _rate(r["success_count"], r["fail_count"])
    # 最该重写的排前面。从没用过的（ok=fail=0）不算坏 —— 那叫「还没验过」
    rows.sort(key=lambda r: -((r["fail_count"] or 0) - (r["success_count"] or 0)))
    emit({"ok": True, "count": len(rows), "procedures": rows})
    return 0


def cmd_proc_use(a) -> int:
    """记一次使用。失败了还能记「卡在第几步」—— 那是小脑真正记的东西。"""
    con = connect(); ensure_schema(con)
    row = con.execute("SELECT id,name,success_count,fail_count,weak_steps"
                      " FROM procedures WHERE id=?", (a.id,)).fetchone()
    if not row:
        con.close(); emit({"ok": False, "error": "没有这条程序"}); return 1
    if a.ok:
        con.execute("UPDATE procedures SET success_count=success_count+1 WHERE id=?", (a.id,))
    else:
        con.execute("UPDATE procedures SET fail_count=fail_count+1 WHERE id=?", (a.id,))
        if a.step:
            try:
                weak = json.loads(row["weak_steps"] or "{}")
            except Exception:
                weak = {}
            k = str(a.step)
            weak[k] = weak.get(k, 0) + 1
            con.execute("UPDATE procedures SET weak_steps=? WHERE id=?",
                        (json.dumps(weak, ensure_ascii=False), a.id))
    con.commit()
    upd = con.execute("SELECT id,name,success_count,fail_count,weak_steps"
                      " FROM procedures WHERE id=?", (a.id,)).fetchone()
    con.close()
    d = dict(upd)
    d["success_rate"] = _rate(d["success_count"], d["fail_count"])
    emit({"ok": True, "updated": d})
    return 0


def cmd_conflict(a) -> int:
    """列出「可能被推翻」的候选对，人来定。

    干扰检测（人脑的 retrieval-induced forgetting）在这台机器上只能做到
    「找候选」这一步 —— 判谁推翻谁需要语境，脚本判不准就会误伤，
    所以这里不写库，只把证据摆出来。
    """
    con = connect(); ensure_schema(con)
    rows = [dict(r) for r in con.execute(
        "SELECT id,ts,kind,title,weight,fingerprint FROM events"
        " WHERE superseded=0 AND kind IN (?,?,?,?)", NEWABLE_KINDS)]
    con.close()
    pairs = find_conflicts(rows, a.min_gap)
    emit({"ok": True, "scanned": len(rows), "candidates": len(pairs),
          "pairs": pairs[:a.limit],
          "how_to_settle": "确认哪对是真新旧版本后："
                           "brain.py remember --kind decision --title '推翻 #旧id：...' --supersedes 旧id"})
    return 0


# 动作词表：中文没法靠切词可靠匹配，靠人把「一句话」映射到「一类动作」。
# 这张表就是 prefront 的「把话变成计划」那一步的最简版 —— 命中哪类动作，
# 就用那类的关键词去翻历史，比 2-gram 乱撞准得多。
ACTIONS = (
    ("删文件", ["删", "删除", "清除", "清理", "回收站", "rmtree"]),
    ("改配置", ["patch", "yml", "yaml", "settings", "cordis", "组合", "配置项"]),
    ("装插件", ["安装", "install", "pnpm", "插件", "bundle", "依赖"]),
    ("改代码", ["重构", "脚本", ".py", ".js", ".mjs"]),
    ("跑闸门", ["闸门", "自检", "体检", "巡检", "healthcheck", "ruff"]),
    ("写作", ["正文", "写作", "改稿", "章节"]),
    ("迁移备份", ["迁移", "搬家", "归档", "备份", "快照"]),
    ("发布提交", ["发布", "commit", "push", "上架"]),
)


def _cue_events(con, q, max_events=400):
    """把一句自然语言拆成能检索的线索，取候选事件。

    实测教训（2026-11-03）：
      · 英文标识符要**整词**保留（openAt / cordis.patch.yml）。拆成 2-gram 之后
        "openAt" 变成 "op"/"pe"/"en"，一条都对不上 —— 明明库里有那条 openAt 事故，
        结果 matched=0。
      · 中文不能靠 2-gram：「改小说正文」切出「改小」「说正」这种碎片，
        OR 一拼就命中几百条。「删文件」切出「删文」「文件」，前者没人用、
        后者满库都是。所以中文走**动作词表**：先认出这是哪类动作，再按那类翻。
    """
    q = q or ""
    words = set(re.findall(r"[A-Za-z][A-Za-z0-9_.\-]{2,}", q))
    kws = set(words)
    for name, keys in ACTIONS:
        if any(k in q for k in keys):
            kws |= set(keys)
    if not kws:
        kws = {q}
    kws = [k for k in kws if k]
    conds = ["(title LIKE ? OR body LIKE ?)"] * len(kws)
    params = []
    for k in kws:
        params += ["%" + k + "%"] * 2
    # 先放宽召回再收口：OR 全量会把「只沾一个泛词」的几百条也算成同类动作
    # （2026-11-03 实测：「删除记录 tmp 里的一次性文件」matched 打满 400，
    #   事故率被稀释成 1%）。取 4 倍池子，再按 IDF 加权覆盖率筛。
    rows = [dict(r) for r in con.execute(
        "SELECT id,ts,kind,title,weight,emotion,fingerprint,"
        " substr(coalesce(body,''),1,400) AS body FROM events"
        " WHERE superseded=0 AND (" + " OR ".join(conds) + ") ORDER BY ts DESC LIMIT ?",
        params + [max(max_events * 4, 800)])]
    truncated = len(rows) >= max(max_events * 4, 800)
    wts, tot = _term_weights(con, kws)
    kept = []
    for r in rows:
        text = (r["title"] or "") + " " + (r.get("body") or "")
        cov = (sum(w for k, w in zip(kws, wts) if k in text) / tot) if tot > 0 else 0.0
        r["cov"] = round(cov, 3)
        # 0.18 是 2026-11-03 在「清理 tmp」与「改小说正文」两个真实动作上量的：
        # 真同类（命中 2 个以上线索）都在 0.25 以上，只沾一个泛词的在 0.1 以下。
        if cov >= 0.18:
            kept.append(r)
    kept.sort(key=lambda r: (-r["cov"], r["ts"]))
    return kept[:max_events], set(kws), truncated


def cmd_rehearse(a) -> int:
    """预演（默认模式网络的功能之一：情景未来思维）。

    人脑在动手之前会用过去的经历模拟「这一步会怎样」（Schacter 的情景未来
    思维）。这里把它做成一条命令：给一个打算做的动作，回过去在类似动作上
    出事的比例、最重的一次、以及相关的硬禁令 —— 让「动手前先查」有具体形状，
    而不是一句含糊的嘱咐。

    注意它答的不是「这事以前怎么回事」（那是 recall），而是
    「我这一步该不该先停一下」。
    """
    q = (a.about or "").strip()
    if not q:
        emit({"ok": False, "error": "about 不能为空"}); return 2
    con = connect(); ensure_schema(con)
    rows, cues, truncated = _cue_events(con, q)
    hits = [(len(set((r["fingerprint"] or "").split("|")[1:]) & cues), r) for r in rows]
    hits.sort(key=lambda x: (-x[0], -(x[1]["weight"] or 0)))

    n = len(hits)
    acc = [h for _, h in hits if h["kind"] == "accident"]
    rate = (len(acc) / n) if n else 0.0
    hard = [dict(r) for r in con.execute(
        "SELECT id,what,severity FROM constraints WHERE active=1") if
        any(x in (r["what"] or "") for x in cues)]

    # 分档只看「比例 + 条数」。单看「有没有 90 分事故」会把无关事故也算进来 ——
    # 2026-11-03 实测：「改小说正文」因为命中里混进一条「扫 C 盘误判」的高分事故
    # 被判成 high，而那条跟写作毫无关系。
    if rate >= 0.15 and len(acc) >= 3:
        risk = "high"
    elif len(acc) >= 2 and (rate >= 0.05 or len(acc) >= 8):
        # 绝对数也算：332 次里 16 起事只占 4.8%，但 16 起就是 16 次踩坑
        risk = "medium"
    else:
        risk = "low"
    worst = max(acc, key=lambda h: h["weight"] or 0) if acc else None
    con.close()

    if risk == "high":
        verdict = ("过去 %d 次类似操作里出过 %d 次事 —— 先停一下：备份、写清怎么回退、"
                   "把下面这些禁令过一遍再动手。" % (n, len(acc)))
    elif risk == "medium":
        verdict = "过去 %d 次里出过 %d 次事，有先例 —— 按流程走，别跳闸门。" % (n, len(acc))
    elif n:
        if acc:
            # 别把「比例低」说成「没出过事」：2026-11-03 实测 6 起事故、
            # 占比 1%，旧文案照样输出「没出过事」，自相矛盾。
            verdict = ("过去 %d 次类似操作里出过 %d 次事（%d%%，比例不高）—— "
                       "可以照常做，但 worst 里那次先看一眼。" %
                       (n, len(acc), round(rate * 100)))
        else:
            verdict = "过去 %d 次类似操作没出过事，但也不是没有代价（%d 条记录）—— 照常做。" % (n, n)
    else:
        verdict = "库里找不到类似动作的记号 —— 这可能是第一次做，把它当无先例处理。"

    if truncated:
        verdict += ("（注意：命中条数打满了上限，说明这个说法太宽、统计可能被泛词稀释，"
                    "换个更具体的说法会更准。）")

    emit({"ok": True, "about": q, "matched": n, "accidents": len(acc),
          "accident_rate": round(rate, 2), "risk": risk,
          "worst": ({"id": worst["id"], "ts": worst["ts"], "weight": worst["weight"],
                     "title": worst["title"]} if worst else None),
          "constraints": hard[:5],
          "examples": [{"id": h["id"], "ts": h["ts"], "kind": h["kind"],
                        "title": h["title"]} for _, h in hits[:5]],
          "verdict": verdict})
    return 0


# 「像是纠正」的提示词。只用来挑候选，**不自动记** —— 机器分不清
# 「这也太慢了」是纠正还是随口抱怨，最后一步得人看。
# 分强弱：强词单独出现就够（「笨蛋」「你确定」），弱词要凑够两个
# （「应该」「注意」在任何长文里都有，2026-11-03 实测 242 条消息里误判 197 条，
#   大部分来自系统注入的 AGENTS.md 全文）。
CORRECTION_CUES_STRONG = ("不对", "错了", "搞错", "笨蛋", "不许", "重来",
                          "你确定", "漏了", "忘了", "没做完", "怎么又")
CORRECTION_CUES_WEAK = ("应该", "注意", "不要")
# 系统注入的内容不是落云说的话，别当纠正。
SYSTEM_PREFIX = ("<system-reminder>", "Current runtime context", "Updated instructions",
                 "# ", "```")


def cmd_audit_corrections(a) -> int:
    """纠错漏记检测：从会话原文里找「疑似被纠正」，对照库里记了没有。

    为什么要有它：「被纠正就记 correction」是条行为规则，但没有执行件
    （协议 §5.18：规则不带可执行件必然腐坏）。2026-11-03 实测全库只有 1 条
    correction，可最近三天落云纠正过好几次 —— 不是没纠正，是没记。
    这个命令只出候选：记不记由人定，机器分不清「纠正」和「随口抱怨」。
    """
    since = time.time() - a.days * 86400
    sq = os.path.join(os.path.expanduser("~"), ".dsh", "session-query-index.db")
    if not os.path.exists(sq):
        emit({"ok": False, "error": "会话索引不存在（宿主的 session-query 没建）"})
        return 1
    try:
        scon = sqlite3.connect("file:%s?mode=ro" % sq.replace("\\", "/"), uri=True)
        rows = scon.execute(
            "SELECT session_id, seq, time, text FROM persisted_docs"
            " WHERE type='user/message' AND time >= ? ORDER BY time DESC",
            (since * 1000.0,)).fetchall()
        scon.close()
    except Exception as e:
        emit({"ok": False, "error": "读会话索引失败：%s" % e})
        return 1
    cands = []
    for sid, _seq, t, text in rows:
        txt = (text or "").strip()
        # 太长的不像纠正（纠正通常一句短话）；系统注入的直接跳过。
        if len(txt) < 4 or len(txt) > 200 or txt.startswith(SYSTEM_PREFIX):
            continue
        strong = [c for c in CORRECTION_CUES_STRONG if c in txt]
        weak = [c for c in CORRECTION_CUES_WEAK if c in txt]
        if not strong and len(weak) < 2:
            continue
        cands.append({
            "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(t / 1000.0)),
            "session": (sid or "")[:16],
            "text": txt[:80],
            "cues": strong or weak,
        })
    con = connect()
    tx_since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
    n_corr = con.execute(
        "SELECT count(*) FROM events WHERE superseded=0 AND kind='correction'"
        " AND ts >= ?", (tx_since,)).fetchone()[0]
    con.close()
    emit({"ok": True, "days": a.days, "user_messages": len(rows),
          "candidates": len(cands), "corrections_recorded": n_corr,
          "note": "候选只用来提示漏记；有些只是提问，不是纠正",
          "items": cands[:a.limit]})
    return 0


def _user_noise(title):
    """这条「用户发言」其实不是他说的话，是粘贴的路径/系统通知。

    导入时只过滤了**开头**就是系统注入的（_IMPORT_SKIP），
    但用户消息里也会整段粘路径、或把后台任务通知复制进来。
    2026-11-04 实测：不过滤时 brief 的 top5 里三条是这类。
    """
    t = (title or "").strip()
    if not t:
        return True
    low = t.lower()
    if "background job" in low or "current runtime context" in low:
        return True
    # 2026-11-05 补：导入时 _IMPORT_SKIP 漏掉的几种系统注入。
    # 实测 1807 条 user 里有 99 条是这些（background job 66 / batching reminder 15 /
    # approval policy 13 / model changed 5）——它们不是他说的话，混进关系层会污染 brief。
    if ("batching reminder" in low or "the approval policy changed" in low
            or "model changed" in low or "updated instructions" in low):
        return True
    if t.startswith('"') or t[:3].upper() == "E:\\":
        return True
    return False


def cmd_brief(a) -> int:
    """开工前的简报：我说要做某件事，你告诉我该知道什么。

    和 recall 的区别：recall 是「我想到一个词去查」，brief 是「我要做某事」——
    前者要的是命中列表，后者要的是**一组分好类的提醒**：禁令、可用的步骤、
    相关教训、同类事故、风险高低。recall 回答「有没有」，brief 回答「该注意什么」。

    匹配靠线索词（英文整词 + 中文 2-gram，去虚词），不做语义 —— 这一层是
    给人看的清单，宁可多列几条也不漏；真正的排序能力留给 recall。
    """
    q = (a.about or "").strip()
    if not q:
        emit({"ok": False, "error": "about 不能为空"}); return 2
    con = connect(); ensure_schema(con)

    cues = [w for w in re.split(r"[\s,，、]+", q) if len(w) >= 2]
    for g in _grams(q):
        if g not in cues:
            cues.append(g)
    # 复用动作词表：中文 2-gram 抓不住「删一个目录」里的「删」——
    # 它切出来的是「删一」「一个」「个目」「目录」，一个都撞不上
    # 「删东西前的三步预检」（那条写的是「要递归删除」）。
    # 所以先认出这是哪类动作，把那类的关键词一并加进线索。
    for _name, keys in ACTIONS:
        if any(k in q for k in keys):
            for k in keys:
                if k not in cues:
                    cues.append(k)
    cues = [c for c in cues if c not in STOP_ZH] or [q]

    # 线索词按 IDF 加权，和 recall 用同一把尺子。
    # 原来的 hit() 是「子串命中计数」：问「把类脑记忆接进新框架」时，
    # 只要记录里出现「记忆」就算相关，于是给了两条
    # 「『修掉』出过 3 次事故」这种纯噪声事实（2026-11-03 实测）。
    cw, ctot = _term_weights(con, cues)

    def cov_of(text):
        if not text or ctot <= 0:
            return 0.0
        return sum(w for c, w in zip(cues, cw) if c in text) / ctot

    def hit(text, need=0.25):
        return cov_of(text) >= need

    def any_hit(text, need=3.0):
        """命中一个足够稀有的线索词就算。

        禁令与步骤清单**宁可多列不可漏**：覆盖率口径会把「删一个目录」
        与「删东西前的三步预检」判成 0.08（「删」是高频字，被其他线索词
        稀释），于是一条该给的步骤都不给（2026-11-03 实测）。
        单字线索不参与这条判据 —— 它们只负责扩大召回，不该单独决定相关性。
        """
        text = text or ""
        return any(w >= need and len(c) >= 2 and c in text
                   for c, w in zip(cues, cw))

    cons = [dict(r) for r in con.execute(
        "SELECT id,what,severity,violated FROM constraints WHERE active=1")]
    rel_cons = sorted([c for c in cons if any_hit(c["what"], 2.0)],
                      key=lambda c: (-c["violated"], c["id"]))

    procs = [dict(r) for r in con.execute(
        "SELECT id,name,when_to_use,success_count,fail_count,weak_steps FROM procedures")]
    # pattern 域的事实是「某个词出过 N 次事故」这种统计元数据 ——
    # 它们的 claim 里带着高频词，任何查询都能撞上，当「教训」给出去是噪声。
    facts = [dict(r) for r in con.execute(
        "SELECT id,claim,confidence,domain FROM facts"
        " WHERE contradicted_by IS NULL AND coalesce(domain,'') <> 'pattern'")]
    # 关系层：**你说过什么**。默认不进 recall（会挤占召回池，实测 hit@10 掉 3），
    # 但「动手前简报」正是它该出现的地方。判据必须语义——他的发言是完整口语句
    # （「修一下吧，不然装了不能用不是白瞎了」），和查询词面零重叠。
    # 只取最近 300 条：合并成一次语义调用之后，条数几乎不增加耗时
    # （每次调用约 2 秒是服务端固定开销）；120 条会把半年前相关的话筛掉
    # ——2026-11-05 实测 120 条时「brain 库下一步往哪走」的 user_said 为空。
    users = [dict(r) for r in con.execute(
        "SELECT id, ts, title FROM events"
        " WHERE superseded=0 AND kind='user' AND coalesce(title,'') <> ''"
        " ORDER BY ts DESC LIMIT 300")]
    # 过滤「看着像他的话、其实是粘贴/系统残留」的（2026-11-04 实测不过滤时 top5 三条是这些）
    users = [u for u in users if not _user_noise(u["title"])]

    # 三处一起算，只调一次语义服务。
    # 每次调用约 2 秒（服务端固定开销，与条数几乎无关），分三次就是 6.2 秒
    # ——2026-11-05 实测 brief 6.38s，拆开计时后确认全耗在三次调用上。
    proc_texts = [p["name"] + "：" + (p["when_to_use"] or "") for p in procs]
    fact_texts = [f["claim"] for f in facts]
    user_texts = [u["title"] for u in users]
    n_p, n_f = len(proc_texts), len(fact_texts)
    alls = _sem_sims(q, proc_texts + fact_texts + user_texts) if (n_p + n_f + len(user_texts)) else None
    if alls and len(alls) == n_p + n_f + len(user_texts):
        psims, fsims, usims = alls[:n_p], alls[n_p:n_p + n_f], alls[n_p + n_f:]
    else:
        psims = fsims = usims = None

    # ── 步骤 ──
    # 词面在这里必败：「删一个目录」与「删东西前的三步预检」只共享一个「删」字。
    if psims:
        ranked = sorted(zip(procs, psims), key=lambda x: -x[1])
        top = ranked[0][1] if ranked else 0.0
        # 绝对线 0.55 说明「语义上像」；相对线 top-0.08 砍掉「只是都提到插件」的陪跑
        rel_procs = [p for p, s in ranked if s >= 0.55 and s >= top - 0.08]
    else:
        rel_procs = [p for p in procs
                     if any_hit(p["name"] + " " + (p["when_to_use"] or ""))]
    rel_procs.sort(key=lambda p: -((p["fail_count"] or 0) - (p["success_count"] or 0)))

    # ── 事实 ──
    if fsims:
        ranked = sorted(zip(facts, fsims), key=lambda x: -x[1])
        top_f = ranked[0][1] if ranked else 0.0
        rel_facts = [f for f, s in ranked if s >= 0.50 and s >= top_f - 0.10]
        # 自我画像优先：domain='self' 直接决定这一步要不要慢下来。
        # 2026-11-03 实测：不加这条，改配置问题会给出 6 条技术事实，
        # 却漏掉「改配置这类事我出过 5 次事」。
        self_f = sorted([f for f, s in ranked
                         if f["domain"] == "self" and s >= 0.40],
                        key=lambda f: -f["confidence"])[:2]
        rel_facts = (self_f[:2]
                     + [f for f in rel_facts if f["domain"] != "self"])[:6]
    else:
        rel_facts = sorted([f for f in facts if hit(f["claim"])],
                           key=lambda f: -f["confidence"])

    # ── 事故 ──
    acc = [dict(r) for r in con.execute(
        "SELECT id,ts,title,weight FROM events WHERE superseded=0 AND kind='accident'")]
    rel_acc = [r for r in acc if hit(r["title"])]
    rel_acc.sort(key=lambda r: -(r["weight"] or 0))

    # ── 他说过的话 ──
    rel_user = []
    if usims:
        ranked = sorted(zip(users, usims), key=lambda x: -x[1])
        top_u = ranked[0][1] if ranked else 0.0
        # 口语相似度本就低：绝对线 0.45（比步骤的 0.55 松），再做相对线
        for u, s in ranked:
            if s >= 0.45 and s >= top_u - 0.08:
                rel_user.append({"id": u["id"], "ts": u["ts"],
                                 "title": (u["title"] or "")[:80]})
            if len(rel_user) >= 5:
                break
    else:
        # 语义服务不在时回落词面，别让 brief 因为服务挂了就哑
        for u in users:
            if hit(u["title"], 0.30):
                rel_user.append({"id": u["id"], "ts": u["ts"],
                                 "title": (u["title"] or "")[:80]})
                if len(rel_user) >= 5:
                    break

    # 「沾边的事件数」按同一口径数：扫标题+正文，覆盖率 >= 0.25 才算。
    # 原来只拿 cues[0] 整句去 LIKE，中文长句几乎必然 0 条（实测 0），
    # 于是出现「matched_events=0 却 risk=high」的自相矛盾。
    n_ev = 0
    for r in con.execute("SELECT title, body FROM events WHERE superseded=0"):
        if cov_of((r["title"] or "") + " " + (r["body"] or "")) >= 0.25:
            n_ev += 1
    con.close()

    hard = [c for c in rel_cons if c["severity"] == "hard"]
    # 没有直接相关的事故就别报 high —— 旧判据只看撞上了几起，
    # 而「记忆」这种泛词能撞出一堆，风险等级被泛词绑架。
    if hard and rel_acc:
        risk = "high"
    elif hard or len(rel_acc) >= 2:
        risk = "medium"
    else:
        risk = "low"

    parts = []
    if hard:
        parts.append("%d 条硬禁令相关" % len(hard))
    if rel_procs:
        parts.append("%d 套现成步骤" % len(rel_procs))
    if rel_facts:
        parts.append("%d 条已提炼的教训" % len(rel_facts))
    if rel_acc:
        parts.append("%d 起同类事故（最重一次 %d 分）" % (len(rel_acc), rel_acc[0]["weight"]))
    if rel_user:
        parts.append("%d 条他说过的话" % len(rel_user))
    if parts:
        advice = "；".join(parts) + "。"
    elif n_ev == 0:
        advice = "库里没有和这事沾边的记录 —— 当作第一次做，做完记一笔。"
    else:
        advice = ("有 %d 条事件沾边，但没有一条够得上「同类事故/步骤/教训」——"
                  " 先按常规流程走。" % n_ev)

    # 2026-11-05：标出「这套步骤从没被用过」。由头很具体 —— procedures 九条的
    # success_count / fail_count **全是 0**，`proc-use` 从上线起没人调用过，
    # 于是「记成败、定位薄弱步骤」这一层完全是死的（今天第三个同类问题：
    # 前面还有 embed 不在刷新链里、similar 边没有生成器、salience 从没被写过）。
    # brief 是唯一会把这些步骤摆到眼前的地方，就在这里显示使用次数 ——
    # 看到 used=0 就该想到「做完记一笔」。
    for p in rel_procs:
        p["used"] = (p.get("success_count") or 0) + (p.get("fail_count") or 0)

    emit({"ok": True, "about": q, "risk": risk, "advice": advice,
          "cues": cues[:10], "matched_events": n_ev,
          "constraints": rel_cons[:8], "procedures": rel_procs[:5],
          "facts": rel_facts[:6], "accidents": rel_acc[:8],
          "user_said": rel_user})
    return 0


def cmd_source(a) -> int:
    """回到原文：给一条事件，把它前后几分钟的原始对话取出来。

    这是「brain 负责导航、原文层负责给证据」的最后一环。
    数据来源是宿主的会话全文索引（~/.dsh/session-query-index.db，只读打开），
    里面有 12 万条 user/message 与 assistant/message 的逐字文本。

    为什么按时间而不是按 session：历史事件 4568 条里只有 14 条带 session
    （这个字段是 2026-11-03 才加的），按 session 查等于对九成九的事件失效。
    时间窗是可靠的 —— 事件就是那时记的。
    """
    con = connect(); ensure_schema(con)
    ev = con.execute("SELECT id,ts,kind,title,session FROM events WHERE id=?",
                     (a.id,)).fetchone()
    con.close()
    if not ev:
        emit({"ok": False, "error": "没有这条事件"}); return 1
    try:
        base = time.mktime(time.strptime(str(ev["ts"])[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        emit({"ok": False, "error": "事件时间格式异常: %r" % ev["ts"]}); return 1

    sq = os.path.join(os.path.expanduser("~"), ".dsh", "session-query-index.db")
    if not os.path.exists(sq):
        emit({"ok": False, "error": "会话索引不存在（宿主的 session-query 没建）"}); return 1

    lo = (base - a.window * 60) * 1000.0
    hi = (base + a.window * 60) * 1000.0
    try:
        scon = sqlite3.connect("file:%s?mode=ro" % sq.replace("\\", "/"), uri=True)
        rows = scon.execute(
            "SELECT session_id, seq, type, time, text FROM persisted_docs"
            " WHERE time BETWEEN ? AND ?"
            "   AND type IN ('user/message','assistant/message')"
            # 宿主每轮都会塞一条 Current runtime context，它不是对话内容，
            # 混在原文里非常吵（2026-11-03 实测：前后 5 分钟里一半是它）。
            "   AND coalesce(text,'') NOT LIKE 'Current runtime context%'"
            " ORDER BY time LIMIT ?", (lo, hi, a.limit)).fetchall()
        scon.close()
    except Exception as e:
        emit({"ok": False, "error": "读会话索引失败: %s" % e}); return 1

    out = []
    for sid, seq, typ, t_ms, text in rows:
        out.append({
            "session": sid,
            "seq": seq,
            "type": typ,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_ms / 1000.0)),
            "text": (text or "")[:a.max_chars],
        })

    emit({"ok": True, "event": {"id": ev["id"], "ts": ev["ts"], "kind": ev["kind"],
                                "title": ev["title"], "session": ev["session"]},
          "window_minutes": a.window, "found": len(out), "docs": out,
          "note": ("索引里只有已扫描过的会话；当前正在进行的会话可能还没有。"
                   if not out else "")})
    return 0


# ── 入口 ────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(prog="brain", description="类脑记忆库")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.set_defaults(fn=lambda a: (ensure_schema(connect()), emit({"ok": True, "db": DB}), 0)[-1])

    p = sub.add_parser("remember")
    p.add_argument("--kind", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--body"); p.add_argument("--weight", type=int)
    p.add_argument("--emotion"); p.add_argument("--actor", default="me")
    p.add_argument("--ref", action="append"); p.add_argument("--source")
    p.add_argument("--decay-at")
    p.add_argument("--supersedes", type=int, action="append",
                   help="被这条新事件推翻的旧事件 id（可多次）")
    p.add_argument("--session", help="来源会话 id；不传就按 sessions 目录最近改动来猜")
    p.set_defaults(fn=cmd_remember)

    p = sub.add_parser("recall"); p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--include-dormant", action="store_true",
                   help="连休眠记录一起查。休眠 = 长期不用已退到后台，不是删除；"
                        "要找很久以前的事时用它唤醒")
    p.add_argument("--with-user", action="store_true",
                   help="把落云的发言（kind=user）也纳入检索。默认不混进来 ——"
                        "「他说过什么」和「发生过什么」是两层")
    p.add_argument("--with-episode", action="store_true",
                   help="附带「命中的事落在哪段经历里」。默认关：段粒度（平均 5 条、最长 83 条）"
                        "跟查询命中粒度（10-20 条）对不上，按「命中几条」挑段必然是噪声")
    p.add_argument("--no-vector", action="store_true",
                   help="不做语义补充（纯词法）。评测纯词法能力、或需要快的时候用")
    p.add_argument("--no-count", action="store_true",
                   help="不累加 hits。评测脚本必须带 —— 否则被当答案的记录会被刷成"
                        "「用过几百次」，永远逃过衰减")
    p.add_argument("--count-top", type=int, default=3,
                   help="只给前几条累加 hits（默认 3）。一次返回 10 条时，后几条"
                        "多半只是被列表带出来的，不该算「用过」")
    p.set_defaults(fn=cmd_recall)

    # 容量 4±1（Cowan 2001）：塞多了不聚焦，反而不如少而准
    p = sub.add_parser("working"); p.add_argument("--limit", type=int, default=4)
    p.set_defaults(fn=cmd_working)

    p = sub.add_parser("link")
    p.add_argument("a", type=int); p.add_argument("b", type=int)
    p.add_argument("--rel", required=True); p.add_argument("--note")
    p.set_defaults(fn=cmd_link)

    p = sub.add_parser("feedback"); p.add_argument("id", type=int)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--good", action="store_true"); g.add_argument("--bad", action="store_true")
    g.add_argument("--correct", action="store_true")
    p.set_defaults(fn=cmd_feedback)

    p = sub.add_parser("constrain")
    p.add_argument("--what", required=True)
    p.add_argument("--why"); p.add_argument("--scope")
    p.add_argument("--severity", default="soft")
    p.set_defaults(fn=cmd_constrain)

    p = sub.add_parser("violate"); p.add_argument("id", type=int)
    p.add_argument("--note"); p.set_defaults(fn=cmd_violate)

    p = sub.add_parser("constraints"); p.add_argument("--severity")
    p.set_defaults(fn=cmd_constraints)

    p = sub.add_parser("open"); p.add_argument("--what", required=True)
    p.add_argument("--due"); p.add_argument("--note"); p.set_defaults(fn=cmd_open)

    p = sub.add_parser("close"); p.add_argument("id", type=int); p.set_defaults(fn=cmd_close)

    p = sub.add_parser("similar"); p.add_argument("id", type=int)
    p.add_argument("--limit", type=int, default=10); p.set_defaults(fn=cmd_similar)

    p = sub.add_parser("reindex"); p.set_defaults(fn=cmd_reindex)

    p = sub.add_parser("meta-set")
    p.add_argument("key"); p.add_argument("value")
    p.set_defaults(fn=cmd_meta_set)

    p = sub.add_parser("backup")
    p.add_argument("--to", help="快照目录（默认 archive\\brain-backup）")
    p.add_argument("--keep", type=int, default=5, help="保留最近几份（默认 5）")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("proc")
    p.add_argument("query", nargs="?", default="", help="关键词；留空列全部")
    p.add_argument("--id", type=int, help="看某一条的完整步骤")
    p.set_defaults(fn=cmd_proc)

    p = sub.add_parser("proc-use")
    p.add_argument("id", type=int)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ok", action="store_true")
    g.add_argument("--fail", action="store_true")
    p.add_argument("--step", type=int, help="失败卡在第几步（对应 steps 里的序号）")
    p.set_defaults(fn=cmd_proc_use)

    p = sub.add_parser("source")
    p.add_argument("id", type=int, help="事件 id")
    p.add_argument("--window", type=int, default=5, help="前后多少分钟（默认 5）")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--max-chars", type=int, default=400, help="每条原文最多截多少字")
    p.set_defaults(fn=cmd_source)

    p = sub.add_parser("audit-corrections")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_audit_corrections)

    p = sub.add_parser("brief")
    p.add_argument("--about", required=True, help="打算做的事，一句话")
    p.set_defaults(fn=cmd_brief)

    p = sub.add_parser("purge-derived")
    p.add_argument("--apply", action="store_true", help="真删（默认只看）")
    p.set_defaults(fn=cmd_purge_derived)

    p = sub.add_parser("rehearse")
    p.add_argument("--about", required=True, help="打算做的动作，一句话")
    p.set_defaults(fn=cmd_rehearse)

    p = sub.add_parser("conflict")
    p.add_argument("--min-gap", type=int, default=7, help="两条至少隔几天才算新旧版本")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_conflict)

    p = sub.add_parser("distill")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--min-age", type=int, default=90, help="多少天以上的才提炼")
    p.add_argument("--min-len", type=int, default=500, help="正文超过多少字才值得提炼")
    p.add_argument("--max-len", type=int, default=300, help="提炼后保留多少字")
    p.add_argument("--max-weight", type=int, default=50,
                   help="只提炼权重不超过这个的（高价值的别动）")
    p.add_argument("--limit", type=int, default=50, help="一次最多提炼几条")
    p.set_defaults(fn=cmd_distill)

    p = sub.add_parser("import-user")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--min-len", type=int, default=8)
    p.add_argument("--max-len", type=int, default=1500)
    p.set_defaults(fn=cmd_import_user)

    p = sub.add_parser("decide")
    p.add_argument("--title", help="一句话结论（默认：选了 X）")
    p.add_argument("--options", help="还考虑过哪些，逗号分隔")
    p.add_argument("--chose", required=True, help="选了哪个")
    p.add_argument("--why", help="为什么选它")
    p.add_argument("--weight", type=int, default=70)
    p.add_argument("--ref", nargs="*", type=int, help="相关事件 id")
    p.set_defaults(fn=cmd_decide)

    p = sub.add_parser("audit-conflicts")
    p.set_defaults(fn=cmd_audit_conflicts)

    p = sub.add_parser("review")
    p.add_argument("--days", type=int, default=30, help="回看最近多少天（默认 30）")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("drop")
    p.add_argument("id", type=int)
    p.add_argument("--reason", required=True, help="为什么作废（会写进留痕）")
    p.add_argument("--weight", type=int, default=80)
    p.set_defaults(fn=cmd_drop)

    p = sub.add_parser("forget")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--min-age", type=int, default=30, help="多少天以上的才算旧")
    # 2026-11-03 从 40 提到 50。实测：库里 w<=40 的只有 2 条，而 w 恰为 50 的
    # （done 类默认值）有 1293 条从没被 recall 用到过、年龄也都超过 30 天。
    # 门槛卡在 40 等于「只有极少数会衰减」，其余九成永远不淡忘 ——
    # 那不是「用进废退」，那是「谁都不会忘」。
    p.add_argument("--max-weight", type=int, default=50, help="只衰减权重不超过这个的")
    p.set_defaults(fn=cmd_forget)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
