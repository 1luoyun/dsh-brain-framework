# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.0] - 2026-11-04

第一个公开版本。功能已经完整跑通，但 API 还会变 —— 所以是 `0.x`。

### Added

- **事件为唯一真相**的 SQLite 记忆库：事实 / 情节段 / 程序 / 向量全是派生视图，随时可重建
- **检索**：FTS5 全文 + IDF 加权覆盖率 + 标题分档排序；可选语义层（bge-small-zh）在词法覆盖率低时介入
- **遗忘三档**：衰减（降权）→ 休眠（不参与检索）→ 提炼（压缩正文）；事故与教训永久保护
- **派生视图**：`episode.py` 情节分段、`consolidate.py` 提炼事实与程序
- **决策留痕** `decide`：备选 / 选了谁 / 为什么；**作废留痕** `drop`：不物理删除，标记 superseded
- **自我模型**：按动作类型统计「我在哪类事上不可靠」
- **开工简报** `brief`：禁令 / 步骤 / 教训 / 同类事故 / 风险等级
- **隔离沙盘测试** `test_brain.py`：14 项断言，绝不碰真库
- **检索评测** `eval.py` + 示例题集（精确 / 模糊 / 否定三类）
- 健康体检 `health.py`（12 项）

### Changed

- 许可：MIT → **AGPL-3.0**（想拦住「套壳闭源」和「做成服务收费」）
- 模型路径配置化：`BRAIN_EMBED_MODEL`（默认走 HF 模型名，自动下载）
- 数据库路径配置化：`BRAIN_DB`（默认同目录 `brain.db`）
- 测试沙盘改为脚本同级 `.test-sand/`，不再假设目录结构

### Fixed

- `consolidate.py` 的数据库路径曾写死为绝对路径 —— 换机器或换库位置会静默连到旧库
- `test_brain.py` 的沙盘路径曾依赖 `memory/brain` 的上级结构，换目录会算成 `记录\记录`

[Unreleased]: https://github.com/1luoyun/brain-framework/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/1luoyun/brain-framework/releases/tag/v0.1.0
