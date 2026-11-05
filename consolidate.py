#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""consolidate.py —— 巩固：把 4,540 条「发生过什么」提炼成「世界怎么运作」

人脑在慢波睡眠里做这件事：海马体把白天的情景重放给皮层，皮层抽出规律。
这一步不能靠写死规则，得能反复跑：每次跑都从 events 里重新提炼。

产出两张表：
    facts       —— 一句话事实（脱掉时间），带 evidence 指向支撑它的 event id
    procedures  —— 可复用的做法（下次遇到同类怎么办）

用法：
    python consolidate.py --report     # 只看能提炼出什么，不写库
    python consolidate.py --apply      # 写进 facts / procedures
    python consolidate.py --stats      # 看现有两层有多少条

设计口径：
    · 每条 fact 必须带 evidence（event id 列表），没有出处的宁可不写
    · confidence 1-100，来自支撑事件的数量与平均权重
    · 同一条 fact 已存在时只更新 confidence 与 evidence，不重复插
"""

import argparse
import os
import json
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 数据库位置：BRAIN_DB 环境变量优先，否则用本目录下的 brain.db。
# （早期版本写死了某个绝对路径，换机器或换库位置就静默连到旧库——比报错更坏：
#   它会"跑成功"，但改的不是你以为的库。）
DB = os.environ.get("BRAIN_DB") or os.path.join(HERE, "brain.db")


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


# ══════════════════════════════════════════════════════════
# 提炼规则表：每条规则 = (匹配条件, 事实陈述, 领域)
# 匹配条件用正则去 events.title + body 里找；命中 >= min_hits 才落库
# ══════════════════════════════════════════════════════════
RULES = [
    # ── DSH 插件 ───────────────────────────────────────────
    dict(
        name="插件停用只有一层管用",
        pattern=r"turn-rewind|从 bundles 移除.*无效|disabled: true.*拦得住",
        claim="停用会崩的 DSH 插件时，从 bundles 移除无效；只有补丁层 `- id: X / disabled: true` 拦得住（它按上次成功组合恢复启用集）",
        domain="dsh", min_hits=1,
    ),
    dict(
        name="pending 会卡死冷启动",
        pattern=r"pending|服务未挂载|永远 pending",
        claim="插件的 inject 服务若处在 pending，冷启动会卡死到底；热挂载能容忍 pending，冷启动不能——「接完没崩」不等于「重启能起」",
        domain="dsh", min_hits=1,
    ),
    dict(
        name="旧版本覆盖运行时新版",
        pattern=r"平铺.*盖住|盖住.*运行时|0\.1\.0-rc\.8.*0\.2\.0-rc\.2|版本.*覆盖",
        claim="profile 里被 pnpm 平铺的旧版本会盖住运行时自带的新版，导致服务未挂载、依赖它的插件永远 pending",
        domain="dsh", min_hits=1,
    ),
    dict(
        name="pnpm install 会清空 node_modules",
        pattern=r"pnpm install.*清空|清空了.*node_modules|Recreating",
        claim="对 app 自管的 profile 手工跑 `pnpm install` 会先清空 node_modules；新增/升级只能用 `pnpm add`（它同时更新 lockfile）",
        domain="dsh", min_hits=1,
    ),
    dict(
        name="startup 模式会拖死宿主",
        pattern=r"openAt.*startup|startup.*启动阶段",
        claim="session-query-sqlite 的 `openAt: startup` 会在启动阶段打开大库做 WAL 恢复，卡住则 sessionController 注册不上，宿主起不来；必须保持 first-search",
        domain="dsh", min_hits=1,
    ),

    # ── 删除与恢复 ─────────────────────────────────────────
    dict(
        name="回收站可以按命令还原",
        pattern=r"回收站|Recycle\.Bin|还原误删|取回.*原件",
        claim="删除走回收站后可以按命令还原：`$I` 存原始路径、`$R` 存内容，两者配对就能把文件放回原位",
        domain="fs", min_hits=2,
    ),
    dict(
        name="mv 语法错会整批失败",
        pattern=r"mv 语法|mv.*全失败|guard\.py mv.*--to",
        claim="`guard.py mv` 漏写 `--to` 时会把目标目录当成又一个源路径，一个文件都不会移动，而且报错只进 stderr",
        domain="fs", min_hits=1,
    ),
    dict(
        name="回退点可能在别的仓",
        pattern=r"独立 git 仓|在另一个仓|嵌套仓",
        claim="判「没有回退点」之前先确认文件归哪个仓——本工作区至少三个独立仓，外层仓只存指向内层的指针",
        domain="fs", min_hits=1,
    ),
    dict(
        name="配置会静默消失",
        pattern=r"静默消失|存在性闸门|configuration.*missing",
        claim="配置文件会无声消失（不知道是谁删的），所以关键配置要有存在性闸门定时检查",
        domain="fs", min_hits=1,
    ),

    # ── 编码与输出 ─────────────────────────────────────────
    dict(
        name="GBK 字形会当场崩",
        pattern=r"GBK|UnicodeEncodeError|无字形|⚠",
        claim="GBK 控制台里 print 到没有字形的字符（箭头、警示符号、emoji）会当场 UnicodeEncodeError；要进日志的提示行一律纯 ASCII",
        domain="encoding", min_hits=1,
    ),
    dict(
        name="读回方向会搞反",
        pattern=r"解码方向|按 UTF-8 解|GBK 写.*UTF-8 读|ufffd",
        claim="子进程输出的解码方向最易搞反：python 的 stdout 是 GBK、设过 OutputEncoding 的 PowerShell 是 UTF-8；搞反的产物是乱码或 U+FFFD",
        domain="encoding", min_hits=1,
    ),
    dict(
        name=".bat 不能写中文注释",
        pattern=r"\.bat.*中文|中文注释.*bat|OEM.*bat",
        claim="`.bat` 里写中文注释会让 cmd 按 OEM 码页把整行当命令执行，后续全部失效；注释必须纯 ASCII",
        domain="encoding", min_hits=1,
    ),

    # ── 判断与误判 ─────────────────────────────────────────
    dict(
        name="空转可能是等外部",
        pattern=r"空转|等外部|在等.*PR|等.*响应",
        claim="判「空转/没进展」前先问一句——可能是在等外部响应（PR 评审、对方回复），不是没干活",
        domain="judge", min_hits=1,
    ),
    dict(
        name="搜不到不等于没有",
        pattern=r"没搜到|没有搜到结果|反爬|搜索空手",
        claim="搜索空手时要分清两件事：「没查成」和「确实没有」。被反爬挡住是常态，不能混成一句「网上没有」",
        domain="judge", min_hits=1,
    ),
    dict(
        name="阈值假象",
        pattern=r"阈值假象|AUC.*假象|撤回旧结论",
        claim="指标好看不等于结论对：换阈值/换标签之前，先确认真实正例数不为零",
        domain="judge", min_hits=1,
    ),

    # ── 资源与运行 ─────────────────────────────────────────
    dict(
        name="堆 OOM 是宿主崩溃主因",
        pattern=r"堆 OOM|heap.*OOM|Last few GCs|NODE_OPTIONS",
        claim="DSH 宿主崩溃的常见根因是 JS 堆 OOM；症状在日志里是 `Last few GCs` 与 Mark-Compact 逼近上限，解法是抬 NODE_OPTIONS",
        domain="runtime", min_hits=1,
    ),
    # ── 2026-11-03 加：从「未被任何规则覆盖的 13 起事故」里挑出来的三条 ──
    dict(
        name="进程存在不等于单实例",
        pattern=r"双实例|多开.*实例|两个.*pid|相差 1 秒|单实例锁",
        claim="同名的守护/服务可能同时跑着两份（父进程都已退出，启动时间只差一两秒）；排障时先数进程再下结论。单实例锁 stale 是常态，自愈覆写，不要手改锁文件",
        domain="runtime", min_hits=2,
    ),
    dict(
        name="判据维度用错会整片误判",
        pattern=r"AppID 误判|口径误判|实际占用.*不等于|订正 D 盘",
        claim="判断占用/归属前先确认用的是哪个维度：AppID、文件名、显示名都可能与实际占用不一致，拿错维度会得出一整片误判（扫 C 盘那次）",
        domain="judge", min_hits=1,
    ),
    dict(
        name="删除前引用检查不能只看规则文件",
        pattern=r"删除前引用检查|引用检查|verify-config\.checks\.yml|真回退点",
        claim="删除/移动前先跑「删除前引用检查」：只看规则文件会漏 —— 被 verify-config.checks.yml 声明为回退点的备份，rules 查不到，但清单会当场报红",
        domain="fs", min_hits=1,
    ),
    # ── 2026-11-03 由 --uncovered 从 11 起未覆盖事故里挑出来的三条跨场景教训 ──
    # 挑选口径：能在别的场景复用；单章审稿、单次细节不算规律。
    dict(
        name="压缩后注入要查插件 source",
        pattern=r"producer-owned source kind|assertV4MessageSources|kind='plugin'",
        claim="压缩（compaction）之后报「producer-owned source kind」＝有插件在压缩后注入消息时把 source 写成 kind:'plugin'；会话格式 v4 写盘前会拒绝这种来源。判据：错误只在压缩后出现、新会话也复现 ⇒ 找「压缩后才写消息」的插件",
        domain="dsh", min_hits=1,
    ),
    dict(
        name="状态更新要放在排序之后",
        pattern=r"排序前.*(累加|取)|加到的不是|count-top",
        claim="「给前 N 条加计数」这类状态更新必须放在最终排序之后：在排序前取 ids，加上的是召回池的原始顺序，不是用户实际看到的那几条（hits 第一版就翻车在这）",
        domain="judge", min_hits=1,
    ),
    dict(
        name="旧口径不能当现值",
        pattern=r"字数口径|口径.*订正|旧数.*作废|同一个数字只能有一个口径",
        claim="报数字或据此写设计稿之前先确认口径版本：旧条款里的数（如「<1600 的 38 章」）可能是后来改过规则的旧值，不能当现值用",
        domain="judge", min_hits=1,
    ),
]


# ══════════════════════════════════════════════════════════
# 程序记忆：可复用的做法（从事故里长出来的步骤）
# ══════════════════════════════════════════════════════════
PROCS = [
    dict(
        name="插件缺文件时的修法",
        when_to_use="某个插件不显示 / 加载失败 / main 指向的文件不存在",
        steps=(
            "1) 读该包 package.json 的 `main`（不是 exports 里那个 ./package.json），确认入口文件在不在\n"
            "2) 缺文件就从 npmmirror 取同版本 tarball：https://registry.npmmirror.com/<name>/-/<name>-<ver>.tgz\n"
            "3) 解压后**只补缺失项**，绝不覆盖用户数据目录（如 dsh-meme 的 memes/）\n"
            "4) node --check 校验入口 + 扫零字节确认内容没被抹\n"
            "5) 重启宿主才生效"
        ),
        domain="dsh",
    ),
    dict(
        name="删东西前的三步预检",
        when_to_use="要递归删除 / 移动东西时（~/.dsh 是绝对禁区，见硬禁令；确需处理先问落云）",
        steps=(
            "1) 路径断言：guard.py assert-path <路径> --expect <标记>\n"
            "2) 引用检查：删除前引用检查.py <路径>（有引用即 exit 1；注意它对协议文本里的路径会假阳性）\n"
            "3) 内容可回退：确认 git 历史 / 备份里能按内容取回\n"
            "全过才走 guard.py rm（回收站，可还原）"
        ),
        domain="fs",
    ),
    dict(
        name="改配置的安全姿势",
        when_to_use="要改 cordis.patch.yml / package.json / 任何会加载的配置",
        steps=(
            "1) 先备份到 记录\\（带秒级时间戳）\n"
            "2) 只做 id 定位的 config 覆盖，不 insert 同名新行\n"
            "3) 改后立刻解析校验（YAML/JSON）\n"
            "4) 明确告知是否需要重启才生效\n"
            "5) 改完跑一次真实端到端，别只看「没报错」"
        ),
        domain="dsh",
    ),
    dict(
        name="会话从侧栏消失的恢复",
        when_to_use="升级/重装后打开只剩几个会话，旧会话看起来全没了",
        steps=(
            "1) 先看 ~/.dsh/sessions 目录下有没有 sessions.hold-* 之类的目录（升级常把旧格式会话整体挪走）\n"
            "2) 再看 workspace.json 的工作区 id 与成员表是否被换过（id 一变，侧栏就按新 id 过滤）\n"
            "3) 恢复 = 把会话目录**复制**回 ~/.dsh/sessions/<工作区slug>/\n"
            "4) 源目录 sessions.hold-* **原样保留**，当安全副本\n"
            "5) 复完核对目录数与文件大小，别只看「侧栏出现了」"
        ),
        domain="dsh",
    ),
    dict(
        name="装插件的选型口径",
        when_to_use="想装新插件时",
        steps=(
            "1) 只装「零/低 token 成本 且 解决真实痛点」的\n"
            "2) 每装一个先问：它每轮会往上下文里加多少？加的东西我真会读吗？\n"
            "3) 纯前端 / 纯配置的优先（不占模型预算）\n"
            "4) 会加成本但不解决痛点的，主动说明不装，别为了「看起来齐全」而装\n"
            "5) 装完必须重启验证冷启动能起（热挂载能容忍 pending，冷启动不能）"
        ),
        domain="dsh",
    ),
    dict(
        name="运行时损坏的辨识与修复",
        when_to_use="Python/Node 某个模块突然 import 失败、或行为诡异但不报错",
        steps=(
            "1) 先分清三种：文件缺失 / 文件被写零 / 版本覆盖\n"
            "2) 缺文件：对照同版本的完好副本（另一处安装、npm/tarball）逐个比对\n"
            "3) 被写零：看大小对不对、内容是不是全 NUL——2026-09-10 事故的签名\n"
            "4) 版本覆盖：profile 里平铺的旧版会盖住运行时新版，查 node_modules 里的实际版本\n"
            "5) 修完用「可导入模块数」这类硬指标验收，别只看「不报错了」"
        ),
        domain="runtime",
    ),
    dict(
        name="会话收口与交接",
        when_to_use="一个长会话要结束/要换会话接手",
        steps=(
            "1) 整理一次性件：探针/临时脚本/中间产物走回收站，别留在工作区\n"
            "2) 留的临时件要写索引：是什么 / 为什么留 / 何时可删\n"
            "3) 写交接文档（自包含）：已完成 / 当前状态数字口径 / 接着要办 / 今天新学到的坑\n"
            "4) 交接文档里的路径必须真实存在（有闸门查）\n"
            "5) 收口前跑一次总闸，别把红的留给下一个会话"
        ),
        domain="workflow",
    ),
    dict(
        name="插件体检的正确判据",
        when_to_use="怀疑某个插件坏了 / 要全量体检插件",
        steps=(
            "1) 别用「文件数/状态 Ok」当判据——Status Ok 不代表文件完好\n"
            "2) 按 package.json 的 `main` 逐个解析，主入口不存在就是坏了\n"
            "3) 顺序：先扫 21 个 bundle，再扫全部包（本次 352 个）\n"
            "4) 顺带扫零填充（长度对、内容全 NUL）——2026-09-10 事故的签名\n"
            "5) 修完必须重启验证，热挂载不代表冷启动能起"
        ),
        domain="dsh",
    ),
    dict(
        name="压缩后报 source kind 的排查",
        when_to_use="压缩（compaction）之后整轮报失败，错误提到 producer-owned source kind",
        steps=(
            "1) 看错误原文：会话格式 v4 写盘前 assertV4MessageSources 会拒绝 kind='plugin' 的消息来源\n"
            "2) 判据：错误只在压缩后出现 + 新会话也复现 ⇒ 一定有插件在 compaction/end 之后注入消息\n"
            "3) 查这些插件的 pre-step：谁在压缩后才写消息（dsh-batch-tool-calls 1.1.2 就栽在这）\n"
            "4) 把该插件的 source kind 改成自己的名字（如 'batch-tool-calls'），不要写 'plugin'\n"
            "5) 改完重启宿主，再走一次压缩验证"
        ),
        domain="dsh",
    ),
]


def gather(con):
    rows = con.execute(
        "SELECT id,ts,kind,title,substr(coalesce(body,''),1,400) AS body,weight"
        " FROM events WHERE superseded=0").fetchall()
    return [dict(r) for r in rows]


# 分桶时剔除的词：太泛、没有指向性（「修复」「文件」这种词能撞上任何事）。
GENERIC_WORDS = {"修复", "确认", "完成", "记录", "问题", "检查", "整理", "结果", "可以",
                 "需要", "现在", "已经", "一下", "这个", "那个", "实测", "验证", "发现",
                 "改成", "改为", "一次", "两遍", "一遍", "全部", "部分", "东西", "文件",
                 "目录", "写作", "交付", "小说", "章节"}
# 元词：它们本身就是「出事」的代名词，作为「领域」没有任何指向性 ——
# 实测「事故」这个词 100% 落在 accident 类事件上，但「事故领域出过事故」
# 跟没说一样。
META_WORDS = {"事故", "错误", "问题", "失败", "异常", "报错", "bug", "err"}


def freq_facts(con, min_size=10, max_size=80):
    """频次型事实：不解释「为什么」，只告诉「这领域有 N 条经验」。

    和 RULES 提炼的「规律型事实」互补：
      规律型 = 世界怎么运作（要人写才准）
      频次型 = 这件事反复发生（统计就能得出）
    """
    import collections as _c
    rows = con.execute("SELECT id,kind,title,refs,fingerprint FROM events WHERE superseded=0").fetchall()
    buckets = _c.defaultdict(list)
    for i, k, t, refs, fp in rows:
        r = refs or ""
        if "xingye-linger" in r or "重写稿" in (t or ""):
            continue
        if re.search(r"第[一二三四五六七八九十百零\d]+章|ch\d+", t or ""):
            continue
        for e in (fp or "").split("|")[1:]:
            if e and e not in GENERIC_WORDS:
                buckets[e].append((i, k, t, fp))
    out = []
    for elem, ms in buckets.items():
        n = len(ms)
        if not (min_size <= n <= max_size):
            continue
        if elem in META_WORDS:
            continue
        kinds = _c.Counter(m[1] for m in ms)
        acc = kinds.get("accident", 0)
        # 判据两条（2026-11-03 用真实数据定的，不是拍的）：
        #   ① 事故 >= 2 条 —— 1 条说明不了「这领域常出事」
        #   ② 事故率 >= 15% —— 20 条记录里 1 条事故，说「遇到先查历史」是废话
        # 实测：旧判据 acc>=1 留下 23 条，其中 18 条事故率不到 15%，
        # 全是一句「「全局」是本机的高频领域…」，信息量约等于零。
        if acc < 2 or acc / n < 0.15:
            continue
        acc_ms = sorted([m for m in ms if m[1] == "accident"], key=lambda m: m[0])
        tail = (acc_ms[-1][2] or "")[:40] if acc_ms else ""
        out.append({
            "claim": "「%s」出过 %d 次事故（共 %d 条记录）——最近一次：%s" % (elem, acc, n, tail),
            "domain": "pattern",
            "hits": n,
            "evidence": [m[0] for m in ms][:15],
            # 频次型封顶 70 —— 它只说"这事常发生"，不解释"为什么"，
            # 必须永远排在规律型（最高 95）后面
            "confidence": min(70, 40 + n + acc * 5),
        })
    out.sort(key=lambda x: -x["confidence"])
    return out


def pair_facts(con, min_support=2, max_df_rate=0.25):
    """组合型事实：哪两类事**总是一起**出事。

    认知科学依据：语义记忆是「多次情景的共同点被抽象出来」——单次经历只留
    情景，多次重复的共同点才沉淀成知识。单主题词（「删除」出过 N 次）只是
    词频；两个词共同出现在**同一起**事故里，才描出「这一类事」的形状。

    两条质量闸（2026-11-03 用真实数据定的）：
      ① 事故内出现率 >= 25% 的词当泛词剔除 —— 「崩溃」「根因」这种在事故里
         到处都是，跟谁配对都是废话；
      ② 词对里至少一个是英文标识符 —— 中文 2-gram 会切出「删的」「并修」
         这种跨词碎片，配上另一个碎片就是纯噪声（实测「删的+回收」）。
    机器只发现候选，人再决定要不要写成 RULES 里的规律。
    """
    import collections as _c
    import itertools
    rows = [dict(r) for r in con.execute(
        "SELECT id,ts,kind,title,fingerprint FROM events WHERE superseded=0"
        " AND kind IN ('accident','correction') ORDER BY ts")]
    toks_by_row = []
    df = _c.Counter()
    for r in rows:
        toks = sorted({t for t in (r["fingerprint"] or "").split("|")[1:]
                       if t and t not in GENERIC_WORDS and t not in META_WORDS})
        toks_by_row.append((r, toks))
        for t in toks:
            df[t] += 1
    n_doc = max(1, len(rows))
    common = {t for t, c in df.items() if c / n_doc >= max_df_rate}
    pair_events = _c.defaultdict(list)
    for r, toks in toks_by_row:
        kept = [t for t in toks if t not in common]
        for a, b in itertools.combinations(kept, 2):
            if not (re.search(r"[A-Za-z]", a) or re.search(r"[A-Za-z]", b)):
                continue
            pair_events[(a, b)].append(r)
    out = []
    for (a, b), evs in pair_events.items():
        n = len(evs)
        if n < min_support:
            continue
        last = evs[-1]
        out.append({
            "claim": "「%s」和「%s」常一起出事（%d 次）——最近一次：%s" % (
                a, b, n, (last["title"] or "")[:40]),
            "domain": "pattern",
            "hits": n,
            "evidence": [e["id"] for e in evs][:15],
            # 组合型也封顶 70：它是「事实统计」，不是「原因解释」
            "confidence": min(70, 45 + n * 5),
        })
    out.sort(key=lambda x: (-x["confidence"], x["claim"]))
    return out[:20]


def user_topic_facts(con, min_support=10, top=8):
    """落云关心什么：从 user 事件指纹里找高频主题。

    「关系记忆」不能只有「他说过什么」这一层原始记录，还得有一层抽象：
    1807 条发言里反复出现的方向。brief 回答「该注意什么」时用得上。

    claim 里**不写次数** —— 写了次数每次重算就会变，旧事实留在库里清不掉。
    次数放 hits 字段。
    """
    import collections as _c
    # 这些出现在指纹里也不说明「他关心」：虚词、会话/子代理 ID 片段。
    # 2026-11-03 实测：不筛的话会提炼出「反复提到「session-xxxx」」
    # 「反复提到「不是」」这种垃圾。
    extra_stop = {"不是", "没有", "这个", "那个", "可以", "什么", "现在", "就是",
                  "还是", "但是", "因为", "所以", "其实", "然后", "觉得", "一个",
                  "两个", "一下", "时候", "东西", "问题"}
    id_pat = re.compile(r"[0-9a-f]{6,}-|session-|^[0-9]+$")

    def _junk(t):
        return (t in GENERIC_WORDS or t in META_WORDS or t in extra_stop
                or bool(id_pat.search(t)))

    cnt = _c.Counter()
    ids = _c.defaultdict(list)
    for r in con.execute("SELECT id,fingerprint FROM events WHERE superseded=0"
                         " AND kind='user'"):
        for t in (r["fingerprint"] or "").split("|")[1:]:
            if t and not _junk(t):
                cnt[t] += 1
                if len(ids[t]) < 8:
                    ids[t].append(r["id"])
    out = []
    for t, n in cnt.most_common(top):
        # 门槛提到 30：低于这个数只能说「提过」，谈不上「关心」。
        # 2026-11-03 实测：门槛 10 会提炼出「落云反复提到「出来」」这种。
        if n < max(min_support, 30):
            continue
        out.append({
            "claim": "落云反复提到「%s」—— 这是他关心的方向之一" % t,
            "domain": "relation",
            "hits": n,
            "evidence": ids[t],
            "confidence": min(70, 40 + n),
        })
    return out


# 自我模型用的动作分类。和 brain.py 的 ACTIONS 同源思路：
# 与其从词频里猜「我常犯什么错」（实测会冒出「就行」「出来」这种垃圾），
# 不如按**动作类型**分桶 —— 事故出在哪个动作上，是可统计、可解释的。
SELF_ACTIONS = (
    ("删东西", ["删", "删除", "rmtree", "回收站", "清理"]),
    ("改配置", ["配置", "cordis", "patch", "yml", "settings", "openAt", "profile"]),
    ("装插件", ["插件", "pnpm", "install", "bundle", "依赖", "模块"]),
    ("改代码", ["脚本", "重构", "代码", "函数", "逻辑", "排序"]),
    ("跑闸门", ["闸门", "体检", "巡检", "自检", "healthcheck"]),
    ("写作", ["正文", "章节", "改稿", "写作", "初稿"]),
    ("记忆检索", ["recall", "记忆", "检索", "brain", "评测", "向量"]),
    ("备份迁移", ["备份", "迁移", "归档", "快照", "恢复"]),
    ("编码输出", ["编码", "GBK", "UTF", "乱码", "打印", "输出"]),
)


def self_model_facts(con):
    """从经历里回看「我」：哪类事上容易出事、哪类做得多。

    这是「自我模型」的第一版：不是世界怎么运作（那是 facts 的 dsh/fs/encoding），
    而是**我**在哪类动作上不可靠。人对自己也是这么认识的 ——
    「我在收尾阶段容易漏东西」「我不擅长估时间」。

    只用 title 做分类（body 里有大量引用别人的话，会把别人的事故算到我头上）。
    """
    rows = [dict(r) for r in con.execute(
        "SELECT id,kind,title FROM events WHERE superseded=0")]
    out = []
    for name, keys in SELF_ACTIONS:
        rel = [r for r in rows if any(k in (r["title"] or "") for k in keys)]
        if len(rel) < 5:          # 样本太少谈不上「我常在这出事」
            continue
        acc = [r for r in rel if r["kind"] == "accident"]
        corr = [r for r in rel if r["kind"] == "correction"]
        if not acc and not corr:
            continue
        rate = 100.0 * len(acc) / len(rel)
        out.append({
            "claim": "在「%s」这类事上我出过 %d 次事故（%d 条相关经历里占 %.0f%%）"
                     " —— 这类事开工前先把预检跑完" % (name, len(acc), len(rel), rate),
            "domain": "self",
            "hits": len(acc),
            "evidence": [r["id"] for r in acc][:10],
            "confidence": min(85, 50 + len(acc) * 5),
        })
    out.sort(key=lambda f: -f["confidence"])
    return out


def consolidate(con, apply=False):
    ev = gather(con)
    report = {"events": len(ev), "facts": [], "procedures": len(PROCS)}

    for rule in RULES:
        rx = re.compile(rule["pattern"], re.I)
        hits = [e for e in ev if rx.search((e["title"] or "") + " " + (e["body"] or ""))]
        if len(hits) < rule.get("min_hits", 1):
            continue
        ev_ids = [h["id"] for h in hits]
        avg_w = sum(h["weight"] for h in hits) / len(hits)
        # 置信度：命中数 + 平均权重，封顶 95
        conf = min(95, int(40 + 12 * min(len(hits), 4) + avg_w * 0.15))
        report["facts"].append({
            "rule": rule["name"], "claim": rule["claim"], "domain": rule["domain"],
            "hits": len(hits), "evidence": ev_ids[:12], "confidence": conf,
        })

    report["facts"] = (report["facts"] + freq_facts(con) + pair_facts(con)
                       + user_topic_facts(con) + self_model_facts(con))

    if apply:
        n_f = n_p = 0
        for f in report["facts"]:
            row = con.execute("SELECT id FROM facts WHERE claim=?", (f["claim"],)).fetchone()
            if row:
                con.execute("UPDATE facts SET confidence=?, evidence=?, last_checked=? WHERE id=?",
                            (f["confidence"], json.dumps(f["evidence"]), now(), row["id"]))
            else:
                con.execute(
                    "INSERT INTO facts (claim,evidence,confidence,last_checked,created_at,domain)"
                    " VALUES (?,?,?,?,?,?)",
                    (f["claim"], json.dumps(f["evidence"]), f["confidence"], now(), now(), f["domain"]))
                n_f += 1
        # 派生视图要「重建」，不是「只增」：旧判据产出的 pattern 事实，
        # 如果这次不再产出，就该消失 —— 否则库里会永远躺着过期的废话
        # （2026-11-03 实测：旧判据留下的 23 条「高频领域」事实全是噪音）。
        # 派生视图要「重建」，不是「只增」：旧判据产出的事实，如果这次不再
        # 产出，就该消失 —— 否则库里会永远躺着过期的废话
        # （2026-11-03 实测：旧判据留下的 23 条「高频领域」事实全是噪音）。
        # relation 域同理：过滤规则收紧后，旧的关系事实也必须一起清掉，
        # 否则「落云反复提到「session-5e8a…」」这种会永远留在库里。
        n_del = 0
        for dom in ("pattern", "relation", "self"):
            keep = {f["claim"] for f in report["facts"] if f["domain"] == dom}
            for row in con.execute(
                    "SELECT id, claim FROM facts WHERE domain=?", (dom,)).fetchall():
                if row["claim"] not in keep:
                    con.execute("DELETE FROM facts WHERE id=?", (row["id"],))
                    n_del += 1
        for p in PROCS:
            row = con.execute("SELECT id FROM procedures WHERE name=?", (p["name"],)).fetchone()
            if row:
                con.execute("UPDATE procedures SET when_to_use=?, steps=? WHERE id=?",
                            (p["when_to_use"], p["steps"], row["id"]))
            else:
                con.execute(
                    "INSERT INTO procedures (name,when_to_use,steps,created_at,domain)"
                    " VALUES (?,?,?,?,?)",
                    (p["name"], p["when_to_use"], p["steps"], now(), p["domain"]))
                n_p += 1
        con.execute("INSERT INTO sleep_log (ts,phase,events_in,facts_out,proc_out,merged,note)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (now(), "slow-wave", len(ev), n_f, n_p, 0,
                     "consolidate.py apply"))
        con.commit()
        report["inserted"] = {"facts": n_f, "procedures": n_p}
        report["deleted"] = {"stale_pattern_facts": n_del}
    return report


def main():
    # 与 brainstorm.py 一致：落盘一律 UTF-8（Windows 下 print 重定向默认 GBK）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--uncovered", action="store_true",
                    help="列出没有被任何一条事实引用的事故（找知识缺口）")
    a = ap.parse_args()

    con = connect()
    if a.stats:
        out = {
            "events": con.execute("SELECT count(*) FROM events").fetchone()[0],
            "facts": con.execute("SELECT count(*) FROM facts").fetchone()[0],
            "procedures": con.execute("SELECT count(*) FROM procedures").fetchone()[0],
            "sleep_log": con.execute("SELECT count(*) FROM sleep_log").fetchone()[0],
            "by_domain_facts": {r[0]: r[1] for r in con.execute(
                "SELECT domain,count(*) FROM facts GROUP BY domain")},
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if a.uncovered:
        # 知识缺口自检：哪些事故没有被任何一条事实引用。
        # 做法是反查 facts.evidence 里出现过的 event id，再从事故里减掉。
        # 2026-11-03 靠它发现 13 起没被覆盖，其中 3 起提炼成了新规则。
        ref = set()
        for r in con.execute("SELECT evidence FROM facts"):
            try:
                ref |= set(int(i) for i in json.loads(r[0] or "[]"))
            except Exception:
                pass
        rows = [dict(r) for r in con.execute(
            "SELECT id,ts,weight,title FROM events WHERE superseded=0 AND kind='accident'"
            " ORDER BY ts")]
        miss = [r for r in rows if r["id"] not in ref]
        print(json.dumps({
            "ok": True,
            "accidents": len(rows),
            "covered": len(rows) - len(miss),
            "uncovered": len(miss),
            "items": miss,
            "hint": "从这些事故里挑可复用的教训，加进 RULES；只挑跨场景能用的，"
                    "别把单次事故的细节塞成规则",
        }, ensure_ascii=False, indent=2))
        con.close()
        return 0

    rep = consolidate(con, apply=a.apply)
    con.close()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
