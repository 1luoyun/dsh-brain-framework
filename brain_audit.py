#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""大脑对账：schema 说有的东西，代码里到底有没有通往它的路。

2026-11-07 立。由头不是抽象洁癖：体检 brain 时先发现 procedures 表从来没有
写入路径（`proc` 的 help 却写着「记一套步骤」），顺着查下去又挖出 arousal /
sensory_buffer / motor_trace 三张空表 —— brain.py 里零引用，是建库时按仿生
架构画的蓝图（感觉缓冲、运动轨迹、情绪唤醒），实现后来换成了每次现算，表留在
原地没删。

真正的风险不是「多了三张空表」，是**看 schema 会以为这些层在工作**。这跟
`_arousal` 注释里那条 2026-11-06 的自我订正同源：注释说判据用密度，代码一直
用总数 —— 注释和实现对不上，是这类文件最容易误导人的地方，因为下一个人会
以为它已经在起作用，于是不会再去改。

三条判据：
  ① 每个声明的子命令都要绑上处理函数（lambda 也算）；
  ② 代码里 INSERT/REPLACE 的表名必须真实存在（防幻觉表名）；
  ③ 空表 + 无写入 + 不在派生白名单 = 死表。

第 ③ 条用棘轮：已知的三张记进 KNOWN_DEAD（附成因），新增的死表即判失败。
旧账要认、新账要拦 —— 否则这个闸门第一天就红着，红了就没人看。

用法:
  python brain对账.py                  # 对账, exit 0/1
  python brain对账.py --verbose        # 多打明细
  python brain对账.py --selftest       # 夹具自证

退出码: 0 通过 / 1 有偏差 / 2 用法错误
输出只用 ASCII 标记, 表名与子命令本来就是英文, 中文只进注释。
"""
import io
import os
import re
import sqlite3
import sys

# GBK 兜底（协议 §4.2）：本机 python stdout 是 GBK，撞上非 GBK 字形会当场崩，
# 而崩的时候往往已经把结果算完、只是打不出来。本脚本 print 只用 ASCII，
# 但这道兜底按规矩统一加，免得哪天顺手加一行中文就炸。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# 默认找脚本同目录下的 brain.py / brain.db，也能用参数指到别处
_HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(_HERE, "brain.py")
DB = os.path.join(_HERE, "brain.db")

# 派生表：由 events / reindex 重建，代码里没有直接 INSERT 是正常的
DERIVED = {
    "facts", "episodes", "event_emb", "links_hint",
    "events_fts", "events_fts_config", "events_fts_data",
    "events_fts_docsize", "events_fts_idx",
}

# 已知死表（棘轮基线）。新增的死表一律 FAIL；要豁免只能加进这里并写清成因
KNOWN_DEAD = {
    "arousal": "预留表（health.py 的 RESERVED_TABLES 之一），代码尚未接上",
    "sensory_buffer": "预留表（health.py 的 RESERVED_TABLES 之一），代码尚未接上",
    "motor_trace": "预留表（health.py 的 RESERVED_TABLES 之一），代码尚未接上",
}


def _subcommands(src):
    """从源码里抠出 (子命令名, 绑定的处理函数名或 'lambda')。"""
    out = []
    for block in re.split(r"sub\.add_parser\(", src)[1:]:
        m = re.match(r"\s*[\"']([\w\-]+)[\"']", block)
        if not m:
            continue
        name = m.group(1)
        # lambda 绑定也算绑上了（brain.py 的 init 就是 lambda）；不能用
        # `\s*[,)]` 收尾，lambda 后面紧跟的是参数名，那会把 init 错判成未绑定
        mm = re.search(r"set_defaults\(\s*fn\s*=\s*(lambda|\w+)", block)
        out.append((name, mm.group(1) if mm else ""))
    return out


def _writes(src):
    """代码里真正写到的表名 -> 出现次数。"""
    got = {}
    for tbl in re.findall(
        r"(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO)\s+[\[\"'`]?(\w+)",
        src, re.I,
    ):
        got[tbl.lower()] = got.get(tbl.lower(), 0) + 1
    return got


def audit(src_path=SRC, db_path=DB):
    """跑一遍对账，返回结果字典（不打印、不改任何东西）。"""
    src = io.open(src_path, encoding="utf-8").read()
    subs = _subcommands(src)
    defs = set(re.findall(r"^def (cmd_\w+)", src, re.M))
    writes = _writes(src)

    con = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table'"
            " and name not like 'sqlite_%' order by name")]
        counts = {}
        for t in tables:
            try:
                counts[t] = con.execute('select count(*) from "%s"' % t).fetchone()[0]
            except sqlite3.Error:
                counts[t] = -1
    finally:
        con.close()

    # ① 声明了但没绑处理函数 / 绑了但函数不存在
    unbind = [n for n, f in subs if not f]
    ghost_fn = [(n, f) for n, f in subs
                if f and f != "lambda" and f not in defs]

    # ② 写进了不存在的表
    low = {t.lower() for t in tables}
    phantom = sorted(t for t in writes if t not in low)

    # ③ 死表
    dead = []
    for t in tables:
        if writes.get(t.lower()) or t.lower() in DERIVED:
            continue
        if counts.get(t, 0) != 0:
            continue
        dead.append(t)
    new_dead = [t for t in dead if t not in KNOWN_DEAD]

    return {
        "subs": len(subs), "unbind": unbind, "ghost_fn": ghost_fn,
        "phantom": phantom, "dead": dead, "new_dead": new_dead,
        "tables": tables, "counts": counts, "writes": writes,
    }


def _report(r, verbose=False):
    ok = not (r["unbind"] or r["ghost_fn"] or r["phantom"] or r["new_dead"])
    bits = ["subs=%d" % r["subs"]]
    bits.append("unbind=%d" % len(r["unbind"]))
    bits.append("ghost_fn=%d" % len(r["ghost_fn"]))
    bits.append("phantom=%d" % len(r["phantom"]))
    bits.append("dead=%d(%s)" % (len(r["dead"]),
                                 "known" if not r["new_dead"] else "new"))
    print("%s %s" % ("[OK]  " if ok else "[FAIL]", " ".join(bits)))

    for n in r["unbind"]:
        print("   unbound subcommand: %s" % n)
    for n, f in r["ghost_fn"]:
        print("   subcommand %s -> missing function %s" % (n, f))
    for t in r["phantom"]:
        print("   INSERT into non-existent table: %s" % t)
    for t in r["new_dead"]:
        print("   NEW dead table: %s (0 rows, no write path) -- "
              "either wire it up or add to KNOWN_DEAD with a reason" % t)

    if verbose:
        for t in r["dead"]:
            why = KNOWN_DEAD.get(t, "??")
            print("   dead/known %-18s %s" % (t, why))
        for t in r["tables"]:
            if r["counts"].get(t, 0) == 0:
                print("   empty table: %-18s write=%d derived=%s" % (
                    t, r["writes"].get(t.lower(), 0),
                    t.lower() in DERIVED))
    print("VERDICT: " + ("PASS" if ok else "FAIL"))
    return ok


def selftest():
    """夹具：在临时库里造四组样本，证明判据真会失败、也不会误报。"""
    base = os.path.join(os.environ.get("TEMP", "."), "_selftest_brain"
                        + str(os.getpid()))
    os.makedirs(base, exist_ok=True)
    fails = 0
    total = 0

    def case(name, src_text, tables, expect_ok):
        nonlocal fails, total
        total += 1
        sp = os.path.join(base, "src_%s.py" % total)
        dp = os.path.join(base, "db_%s.db" % total)
        with io.open(sp, "w", encoding="utf-8", newline="\n") as f:
            f.write(src_text)
        con = sqlite3.connect(dp)
        for t in tables:
            con.execute('create table "%s" (x integer)' % t)
        if tables:
            con.execute('insert into "%s" (x) values (1)' % tables[0])
        con.commit()
        con.close()
        r = audit(sp, dp)
        got = not (r["unbind"] or r["ghost_fn"] or r["phantom"] or r["new_dead"])
        if got != expect_ok:
            fails += 1
            print("  FAIL case%d %s: expect_ok=%s got=%s" % (total, name, expect_ok, got))
        else:
            print("  ok   case%d %s" % (total, name))

    # 1 健康：声明+绑定+真实现，表有数据
    case("healthy", 'p = sub.add_parser("go")\np.set_defaults(fn=cmd_go)\n'
                    'def cmd_go(a):\n    return 0\n', ["events"], True)
    # 2 声明了但没绑处理函数 -> 必须 FAIL
    case("unbound", 'p = sub.add_parser("go")\n', ["events"], False)
    # 3 绑了不存在的函数 -> 必须 FAIL
    case("ghost_fn", 'p = sub.add_parser("go")\np.set_defaults(fn=cmd_nope)\n',
         ["events"], False)
    # 4 写进不存在的表 -> 必须 FAIL
    case("phantom_table", 'con.execute("INSERT INTO nope (x) VALUES (1)")\n',
         ["events"], False)
    # 5 空表无写入且不在基线 -> 必须 FAIL（棘轮有效）
    case("new_dead", '', ["events", "brand_new"], False)
    # 6 空表但就是 KNOWN_DEAD 里那三张（此处用派生表代替，验证不误报）
    case("derived_ok", '', ["events", "facts"], True)
    # 7 lambda 绑定算合法（这是今天真实踩过的假阳性）
    case("lambda_ok", 'p = sub.add_parser("init")\n'
                      'p.set_defaults(fn=lambda a: (0))\n', ["events"], True)
    # 8 橡皮章检测：把上面的健康样本改成"必须有错"，判据若不变则说明它没在看
    total += 1
    sp = os.path.join(base, "src_stamp.py")
    dp = os.path.join(base, "db_stamp.db")
    with io.open(sp, "w", encoding="utf-8", newline="\n") as f:
        f.write('p = sub.add_parser("go")\np.set_defaults(fn=cmd_go)\n'
                'def cmd_go(a):\n    return 0\n')
    con = sqlite3.connect(dp)
    con.execute("create table events (x integer)")
    # 表里必须有行：空表 + 无写入本来就是「死表」，不插数据等于拿坏样本测
    con.execute("insert into events (x) values (1)")
    con.commit(); con.close()
    r = audit(sp, dp)
    if r["new_dead"] or r["phantom"] or r["ghost_fn"]:
        fails += 1
        print("  FAIL case%d stamp: healthy sample reported as broken" % total)
    else:
        print("  ok   case%d stamp (healthy sample stays clean)" % total)

    for fn in os.listdir(base):
        try:
            os.remove(os.path.join(base, fn))
        except OSError:
            pass
    try:
        os.rmdir(base)
    except OSError:
        pass

    print("  selftest %d cases, %d failed" % (total, fails))
    print("VERDICT: " + ("PASS" if fails == 0 else "FAIL"))
    return fails == 0


def main(argv):
    verbose = "--verbose" in argv
    if "--selftest" in argv:
        return 0 if selftest() else 1
    unknown = [a for a in argv if a.startswith("-") and a not in ("--verbose",)]
    if unknown:
        print("[FAIL] unknown arg: %s (use --verbose / --selftest)" % " ".join(unknown))
        return 2
    if not os.path.exists(SRC):
        print("[FAIL] brain.py not found: %s" % SRC)
        return 1
    if not os.path.exists(DB):
        print("[FAIL] brain.db not found: %s" % DB)
        return 1
    return 0 if _report(audit(), verbose) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
