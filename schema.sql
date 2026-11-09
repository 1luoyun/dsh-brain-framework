-- brain.db —— 类脑记忆库（自建，零外部依赖）
-- 唯一真相：events。其余全部是从 events 派生的「视图」，可随时重建。
-- 2026-11-03 立，云蝶
--
-- 设计参考（学原理，不搬代码）：
--   · MemOS：权重由 feedback 驱动；衰减要留痕（trace.priority_decayed 是独立事件）
--   · HippoRAG：关联也是记忆（实体图 + 扩散），所以有 links 表
--   · Ebbinghaus：R = e^(-t/S)，S 随权重增大 —— 越重要的忘得越慢
--
-- ⚠ **已知的「空字段」（2026-11-06 查证，逐个有据）** —— 分两类，处置不同：
--   · **被取代的废字段**（零读零写，只留列不删因为迁移麻烦）：
--       `facts.hits`（63 条恒 0，职责已由 `activation` 承担）
--       `constraints.evidence`、`procedures.evidence`（全目录 grep 零匹配）
--       `open_items.event_id`（同上）
--   · **等不到输入的活字段**（有读、没写）：
--       `constraints.active` —— 7 处 `WHERE active=1` 在读它，**但没有任何代码把它置 0**。
--       也就是说「禁用一条约束」这个功能**不存在**。它是个待补的缺口，不是死字段。
--       `facts.contradicted_by` —— 等 `conflict` 命令的输出来喂；那功能是「只出候选人来定」，
--       所以它空着是正常的（当前确实没有该被推翻的配对）。
--   ⇒ **判据：一个空字段要么该通电（补写入方），要么该拔插头（标注废弃）。别让它不明不白地空着。**

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ═══════════════════════════════════════════════════════════
-- 1. 情景记忆的原子：一件事一行，只增不改
--    weight 1-100（2026-11-03 从 1-10 扩到 1-100，够细分）
--      90-100 事故 / 被纠正 / 改坏过东西
--      70-89  重要决定、可复用教训
--      40-69  日常完成、一般决定
--      1-39   流水账
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT    NOT NULL,
  kind       TEXT    NOT NULL,              -- decision/accident/correction/done/learn/promise/user
  actor      TEXT    NOT NULL DEFAULT 'me',
  title      TEXT    NOT NULL,
  body       TEXT,
  refs       TEXT,                          -- JSON 数组
  weight     INTEGER NOT NULL DEFAULT 50,   -- 1-100
  salience   REAL    NOT NULL DEFAULT 1.0,  -- 显著性倍数，feedback 会推它
  emotion    TEXT,                          -- sting / pride / neutral
  source     TEXT,
  decay_at   TEXT,
  superseded INTEGER NOT NULL DEFAULT 0,
  hits       INTEGER NOT NULL DEFAULT 0,    -- 被 recall 命中过几次（用进废退的证据）
  -- 2026-11-03 加：来源会话。没有它就做不到「brain 负责导航、原文层负责给证据」——
  -- 一条事件知道自己发生在哪次对话里，才能指回去取逐字原文。
  -- 取值来自 meta['current_session']（装配器 self.mjs 写入），猜错时回落按目录 mtime。
  session    TEXT,
  -- 要素指纹（模式分离用）：kind|英文整词|中文 2-gram。
  -- ⚠ 这一列最初是 ALTER 加进真库的，没同步进这个文件 —— 结果「用 schema.sql
  -- 新建的库」连 remember 都跑不起来（no column named fingerprint）。
  -- 2026-11-03 由 test_brain.py 抓到并补上。
  fingerprint TEXT,
  -- 2026-11-03 加：最后一次被 recall 命中的时间。
  -- 只有 hits 计数时，忘机机制只能知道「用过几次」、不知道「什么时候用过」——
  -- 长期看全库都会变成「用过」，遗忘再次失效。判据改用「180 天内用过」。
  -- ⚠ 与 fingerprint 一样，老库靠 brain.py 的 _migrate() 补列。
  last_hit_at TEXT,
  -- 2026-11-03 加：休眠。长期不用且权重已经很低 → 默认检索里不返回。
  -- 想不起来 ≠ 被删除：库还在，recall --include-dormant 能唤醒。
  dormant INTEGER NOT NULL DEFAULT 0,
  -- 2026-11-08 加：项目域。做不同的事用不同的数据，互不稀释。
  -- 起因很具体：查漏洞时要从 3794 条里捞，其中近一半是小说与烬灵 —— 工作检索总被别的项目吵到。
  -- 取值由 brain.py domain 按规则算，unknown 表示规则认不出、不参与任何域的默认检索。
  domain   TEXT
);

-- idx_events_domain 不在这里建：老库的 events 表已存在，CREATE TABLE IF NOT EXISTS
-- 会整段跳过，此时 domain 列还没补上，建索引必然报 no such column。
-- 所以它挪到 brain.py 的 _migrate() 里，在 ALTER 之后建。2026-11-08 实测踩到。

CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind   ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_weight ON events(weight DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  title, body, content='events', content_rowid='id', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO events_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

-- ═══════════════════════════════════════════════════════════
-- 2. 关联（学自 HippoRAG：记忆之间的关系本身也是记忆）
--    rel: caused_by / contradicts / same_as / follows / fixes
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS links (
  a     INTEGER NOT NULL REFERENCES events(id),
  b     INTEGER NOT NULL REFERENCES events(id),
  rel   TEXT    NOT NULL,
  note  TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (a, b, rel)
);
CREATE INDEX IF NOT EXISTS idx_links_b ON links(b);

-- ═══════════════════════════════════════════════════════════
-- 3. 衰减留痕（学自 MemOS：衰减必须是事件，不能静默改分）
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS decay_log (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  event_id INTEGER NOT NULL REFERENCES events(id),
  old_w    INTEGER NOT NULL,
  new_w    INTEGER NOT NULL,
  reason   TEXT
);

-- ═══════════════════════════════════════════════════════════
-- 4. 未结：承诺与待办（工作记忆必带）
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS open_items (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  what       TEXT    NOT NULL,
  created_at TEXT    NOT NULL,
  due        TEXT,
  status     TEXT    NOT NULL DEFAULT 'open',
  note       TEXT,
  event_id   INTEGER REFERENCES events(id)
);

-- ═══════════════════════════════════════════════════════════
-- 5. 语义记忆：脱去时间的事实（派生）
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS facts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  claim           TEXT    NOT NULL,
  evidence        TEXT,
  confidence      INTEGER NOT NULL DEFAULT 50,
  last_checked    TEXT,
  contradicted_by TEXT,
  created_at      TEXT    NOT NULL,
  -- 下面两列同样是 ALTER 加进真库后没同步进来的（2026-11-03 由 test_brain.py 抓到）
  domain          TEXT,                        -- dsh / fs / encoding / judge / runtime / pattern
  hits            INTEGER NOT NULL DEFAULT 0,   -- ⚠ **废字段**（2026-11-06 查证）：全库 63 条恒为 0，
                                                 -- 代码里**零读零写**（field_lifecycle 静态扫描 + 全目录 grep 双重确认）。
                                                 -- 它的职责已由下面的 `activation` 承担 ——
                                                 -- 只留列不删，是因为删列要迁移；**别以为它在工作**。
                                                 -- 若将来真要清理，先确认没有外部脚本按列名读它。
  -- 2026-11-05 加：记忆成熟期（对标 arXiv 2605.08538 的 engram maturation）。
  -- 依据 Kitamura 的发现：记忆痕迹形成即刻，但**先是沉默的**，之后才变得可检索。
  -- 所以新事实不该一落地就跟沉淀多年的老规则抢同一份注意力。
  -- 规则：新事实从 0.45 起，每次被 recall 带进前列 +0.08、上限 1.0；
  --      迁移时已有的 facts 全给 1.0（它们已经沉淀过，不该被新规则压）。
  activation      REAL    NOT NULL DEFAULT 1.0,
  -- 2026-11-05 加：最后一次被 recall 真正带出来的时间。
  -- 只有「+0.08 成长」没有「回落」的话，跑久了所有事实都满值、区分度归零；
  -- forget 按这一列判断「超过 30 天没被取用」就往下走（下限 0.3）。
  last_used_at    TEXT
);

-- ═══════════════════════════════════════════════════════════
-- 6. 程序记忆：怎么做（派生）
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS procedures (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL,
  when_to_use   TEXT,
  steps         TEXT,
  evidence      TEXT,
  success_count INTEGER NOT NULL DEFAULT 0,
  fail_count    INTEGER NOT NULL DEFAULT 0,
  -- 2026-11-03 加：小脑的功能不是"记住步骤"，是"通过失败定位序列里薄弱的那一步"。
  -- JSON：{"2": 3} 表示第 2 步失手过 3 次。
  weak_steps    TEXT,
  created_at    TEXT    NOT NULL,
  domain        TEXT                       -- 同样是 ALTER 加进来、没同步的那一列
);

-- ═══════════════════════════════════════════════════════════
-- 7. 情景：按会话/任务聚段（派生）
--    2026-11-03 扩字段：原来只有 summary + event_ids，塞不下「那段是什么性质、
--    多重、带什么情绪」—— 而没有情绪标记的情景，回看时挑不出来。
--    生成器：episode.py（事件分割理论，见该文件头）
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS episodes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session     TEXT,                       -- 来源会话（可空；导入的没有）
  started_at  TEXT    NOT NULL,
  ended_at    TEXT    NOT NULL,
  summary     TEXT    NOT NULL,           -- 一句话：那段时间在干什么
  kind_main   TEXT,                       -- 主导性质
  event_count INTEGER NOT NULL DEFAULT 0,
  weight      INTEGER NOT NULL DEFAULT 50,-- 段内最高权重
  emotion     TEXT,                       -- 段内主导情绪（sting 的段值得回看）
  event_ids   TEXT,                       -- JSON 数组
  built_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_started ON episodes(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_weight  ON episodes(weight DESC);

-- ═══════════════════════════════════════════════════════════
-- 8. 元信息
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
INSERT OR IGNORE INTO meta(k, v) VALUES ('schema_version', '2');
INSERT OR IGNORE INTO meta(k, v) VALUES ('created_at', datetime('now', 'localtime'));

-- ═══════════════════════════════════════════════════════════
-- 9. 禁令（2026-11-03 加）
--    对应「碰过火就不碰」—— 不是知识，是条件反射。
--    severity: hard(绝对禁区，每轮必带) / soft(默认遵守，可评估后破例)
--    violated: 违反过几次；每违反一次 +1，权重跟着涨
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS constraints (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  what          TEXT    NOT NULL,        -- 不许/必须做什么（一句）
  why           TEXT,                    -- 为什么（来自哪次事故）
  severity      TEXT    NOT NULL DEFAULT 'soft',
  scope         TEXT,                    -- 适用范围：全部 / 改配置 / 删文件 / 对外
  violated      INTEGER NOT NULL DEFAULT 0,
  last_violated TEXT,
  evidence      TEXT,                    -- 引用 event id
  active        INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_constraints_sev ON constraints(severity, active);

-- 提炼留痕（2026-11-05 补）：每压一条记一行，事后能查
CREATE TABLE IF NOT EXISTS distill_log (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT    NOT NULL,
  event_id  INTEGER NOT NULL,
  old_chars INTEGER,
  new_chars INTEGER
);
CREATE INDEX IF NOT EXISTS idx_distill_ev ON distill_log(event_id);

-- 睡眠 / 衰减汇总（2026-11-07 补）：distill 与 forget 各写一行，记「这一轮动了多少」。
-- 由头很具体：这张表 code 一直在用（brain.py 有两处 INSERT），但 schema.sql 从来没建过 ——
-- 用 schema.sql 新建的库跑到 distill --apply 或 forget --apply，当场
-- `no such table: sleep_log`。跟 2026-11-03 那次 fingerprint 漏进 schema.sql 是同一个病：
-- 「改代码」和「改 schema.sql」必须同批做，否则老库看不出问题、新库直接跑不起来。
CREATE TABLE IF NOT EXISTS sleep_log (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT    NOT NULL,
  phase     TEXT,
  events_in INTEGER,
  facts_out INTEGER,
  proc_out  INTEGER,
  merged    INTEGER,
  note      TEXT
);

-- 下面三张是**预留表**：health.py 的 RESERVED_TABLES 要求它们存在，但代码还没接上
-- （brain_audit.py 会把它们列为「空表 + 无写入」并记进已知清单，附成因）。
-- 2026-11-07 补：schema.sql 一直缺这三张的建表语句，用 schema.sql 新建的库体检会报
-- 「预留缺」，而拿老库看是正常的 —— 又是「改代码 / 改库」与「改 schema.sql」不同批的老毛病。
CREATE TABLE IF NOT EXISTS arousal (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  level    REAL,           -- 0-1 警觉度
  load     REAL,           -- 0-1 负载
  err_rate REAL,
  note     TEXT
);

CREATE TABLE IF NOT EXISTS sensory_buffer (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL,
  channel TEXT,            -- vision / audio / touch / text
  raw     TEXT,
  ttl_sec INTEGER DEFAULT 3
);

CREATE TABLE IF NOT EXISTS motor_trace (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  action   TEXT,           -- 动作名
  target   TEXT,
  seq_no   INTEGER,        -- 时序
  ok       INTEGER,
  err_ms   INTEGER         -- 时序误差（将来有身体时用得上）
);
