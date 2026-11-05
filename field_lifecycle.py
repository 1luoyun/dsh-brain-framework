#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""field_lifecycle.py — 查「只读不写」的字段（有消费者、没生产者）。

2026-11-05 立。由头很实在：一天里连查出三个同一类病 ——
  · 建向量那一步不在 6 小时刷新链里（新记忆永远没有向量，语义层召不回）
  · links.rel='similar' 有 7246 条，但生成器是跑完就删的一次性脚本
  · salience 的 schema 注释写着「feedback 会推它」，实际从没被写过，
    于是 score() 里那个乘数恒等于 1.0

这三个都不是「写错了」，是**建好了没接上**。而 health.py 查的是数据自洽
（表齐不齐、引用有没有效、值合不合理），**查不出「这个字段从来没被写过」**。
所以补这一件。

做法：读 schema.sql 拿全部列名 → 扫本目录 *.py 里的 SQL 字面量 →
统计每列出现在「写位置」（UPDATE ... SET col= / INSERT INTO t (...)）与否，
报出从来没出现在写位置的列。

**这是粗筛，不是精查**：
  · SQL 是拼出来的，正则只看得到字面量，动态拼的列名会漏；
  · 「只读」不一定是错 —— contradicted_by 是防御性字段，留着是对的；
    真正要警惕的是**注释宣称会写、实际不写**的那种（salience 就是）。
  · 所以它只出清单，判定交给人。

用法：
    python field_lifecycle.py            # 出报告
    python field_lifecycle.py --json     # 机读
退出码：0 正常；1 发现「注释说会写但从不写」的可疑项（人工确认后再改）。
"""

import argparse
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "schema.sql")


def parse_columns():
    """{table: [col, ...]}，从 CREATE TABLE 块里取。"""
    text = open(SCHEMA, encoding="utf-8").read()
    out = {}
    # 建表语句可能单行（`CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);`），
    # 也可能多行 —— 原来只匹配 `\n);` 结尾的多行式，单行表就被吃掉、body 串到下一张表，
    # 于是 meta 表会配上一堆 constraints 的列（2026-11-05 首跑就是满屏误报）。
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*;", text, re.S):
        table, body = m.group(1), m.group(2)
        cols = []
        for line in body.splitlines():
            s = line.strip()
            if not s or s.startswith("--") or s.upper().startswith(("PRIMARY", "FOREIGN", "UNIQUE", "CHECK")):
                continue
            first = s.split()[0]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", first):
                continue
            if first.lower() in ("insert", "create", "select", "update", "delete"):
                continue  # 解析串行了，别把 SQL 关键字当列名
            cols.append(first)
        out[table] = cols
    return out


def collect_sql():
    """把本目录所有 py 文件里的 SQL 字面量拼起来（保留换行，便于跨行正则）。"""
    chunks = []
    for p in glob.glob(os.path.join(HERE, "*.py")):
        try:
            chunks.append(open(p, encoding="utf-8").read())
        except Exception:
            pass
    return "\n".join(chunks)


def written_columns(sql, table):
    """这列有没有出现在写位置。返回命中次数。"""
    hits = 0
    # INSERT INTO <table> (a,b,c)
    for m in re.finditer(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+%s\s*\(([^)]*)\)" % table, sql, re.I | re.S):
        hits += len(m.group(1).split(","))
    # UPDATE <table> SET a=?, b=c  (到 WHERE 之前)
    for m in re.finditer(r"UPDATE\s+%s\s+SET\s+(.*?)(?:WHERE|$)" % table, sql, re.I | re.S):
        hits += len(re.findall(r"(\w+)\s*=", m.group(1)))
    return hits


def read_count(sql, col):
    return len(re.findall(r"\b%s\b" % re.escape(col), sql))


def check_orphan_options(src):
    """`getattr(a, "...")` 读的选项，命令行里必须真的注册过（2026-11-05 立）。

    由头：一天里连抓两个 —— `--full` 和 `--no-assoc` 都在代码里被读
    （`if not getattr(a, "full", False)` 这种），但 `add_argument` 里从来没有它们。
    `getattr` 带默认值 ⇒ 不报错、静默走默认分支 ⇒ **代码看着是好的，
    这个能力实际不存在**，而且没有任何测试会变红。

    判据：正则各扫一遍 `getattr(a, "name")` 和 `add_argument(...)`，做差集。

    **2026-11-06 修**：只扫 `add_argument("--name")` 会**误报**用 `dest=` 改过名字的选项。
    实例：`p.add_argument("--all", dest="show_all", ...)` 只认 `--all`，
    而代码里读的是 `getattr(a, "show_all")` —— 差集里就多出一个不存在的 orphan。
    **参数本身是好的，是检查写窄了。** 所以现在三种写法都算注册：
    `--name`（横线转下划线）、`dest="name"`、以及 `add_argument("name")`（位置参数）。
    """
    used = set(re.findall(r'getattr\(a,\s*"([a-z_]+)"', src))
    reg = {m.group(1).replace("-", "_")
           for m in re.finditer(r'add_argument\(\s*"--([a-z0-9-]+)"', src)}
    # dest 显式改名
    reg |= set(re.findall(r'dest\s*=\s*"([a-z_]+)"', src))
    # 位置参数（没有 -- 前缀）
    reg |= {m.group(1).replace("-", "_")
            for m in re.finditer(r'add_argument\(\s*"([a-zA-Z0-9_-]+)"', src)}
    return sorted(used - reg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    cols = parse_columns()
    sql = collect_sql()

    report = []
    for table, columns in cols.items():
        w = written_columns(sql, table)
        for c in columns:
            if c.lower() == "id":
                continue  # 主键自增，本来就不需要显式写（首跑没跳过，八张表的 id 全被误报）
            r = read_count(sql, c)
            if r == 0:
                continue
            # 单看某个列是否在写位置出现过，用同一套写块再判一次
            written = False
            for m in re.finditer(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+%s\s*\(([^)]*)\)" % table, sql, re.I | re.S):
                if re.search(r"\b%s\b" % re.escape(c), m.group(1)):
                    written = True
                    break
            if not written:
                for m in re.finditer(r"UPDATE\s+%s\s+SET\s+(.*?)(?:WHERE|$)" % table, sql, re.I | re.S):
                    if re.search(r"\b%s\s*=" % re.escape(c), m.group(1)):
                        written = True
                        break
            if not written:
                report.append({"table": table, "column": c, "reads": r})

    # 选项注册检查（2026-11-05 补）
    brain_src = ""
    _bp = os.path.join(HERE, "brain.py")
    if os.path.exists(_bp):
        brain_src = open(_bp, encoding="utf-8", errors="replace").read()
    orphan = check_orphan_options(brain_src) if brain_src else []

    if a.json:
        print(json.dumps({"ok": True, "read_only": report, "orphan_options": orphan},
                         ensure_ascii=False, indent=2))
    else:
        print("只读不写的列（粗筛，需人工判定）：")
        if not report:
            print("  无")
        for r in sorted(report, key=lambda x: (-x["reads"], x["table"])):
            print("  %-14s %-18s 出现 %d 次，但从未出现在 INSERT/UPDATE 的写位置"
                  % (r["table"], r["column"], r["reads"]))
        print()
        print("``getattr(a, ...)` 读了但没注册的选项：")
        if not orphan:
            print("  无（每个被读取的选项都能从命令行传进来）")
        for n in orphan:
            print("  --%s  代码里读它，add_argument 里没有 —— 这个开关实际用不了" % n)
        print()
        print("提醒：只读不一定是错（防御性字段就该只读）；真正要查的是"
              "「注释宣称会写、实际不写」的那种。")
    return 1 if orphan else 0


if __name__ == "__main__":
    sys.exit(main())
