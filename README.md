# OpenCode Storage Audit

[English](#english) | [中文](#中文)

---

## English

A self-contained [OpenCode](https://opencode.ai) skill that audits and safely cleans OpenCode's local storage: the SQLite database, its WAL, the persistent event log, `session_diff` files, and oversized `tool-output`.

The audit is **read-only** and never prints conversation content. Cleanup is destructive by design, requires explicit flags, takes a verified backup first, and refuses to run while OpenCode is alive.

### Requirements

- Python 3.8+ (standard library only — no third-party packages)
- Linux or WSL (running-process detection reads `/proc`; on other platforms stop OpenCode manually before cleanup)

### Install

Copy the directory into your OpenCode skills folder:

```bash
git clone https://github.com/GoldenStain/opencode-storage-audit.git
cp -r opencode-storage-audit ~/.config/opencode/skills/
```

Or symlink it:

```bash
ln -s "$PWD/opencode-storage-audit" ~/.config/opencode/skills/opencode-storage-audit
```

### Usage

```bash
python3 scripts/opencode_storage.py audit   [--top N] [--oversized-mib N]
python3 scripts/opencode_storage.py cleanup [--clear-events] [--delete-orphan-diffs] [--dry-run]
```

Shared options: `--data-dir PATH`, `--json`, `--no-color`.

Data directory resolution: `--data-dir` → `$OPENCODE_DATA_DIR` → `$XDG_DATA_HOME/opencode` → `~/.local/share/opencode`.

#### audit (read-only)

Reports:

- Total OpenCode data size, with per-entry sizes under the data directory.
- Physical SQLite size, WAL size, freelist bytes, clearable event bytes, and the combined reclaimable percentage.
- Largest database tables and indexes.
- Largest sessions ranked by message, part, event, and `session_diff` bytes.
- Oversized individual messages, including session IDs and titles.
- Orphan `session_diff` files.
- Event-log size, including events whose session no longer exists.
- Largest `tool-output` files.

#### cleanup (destructive)

- Refuses to run while any `opencode` process is alive (VACUUM requires exclusive database access) and names the offending PIDs.
- Creates and integrity-checks a timestamped backup (`opencode.db.pre-cleanup-<timestamp>.bak`) before touching anything.
- `--clear-events`: delete `event` and `event_sequence` rows — usually the largest single win.
- `--delete-orphan-diffs`: archive, then remove `session_diff` files with no corresponding live session.
- Then: pre-VACUUM `quick_check`, WAL checkpoint, `VACUUM`, post-`integrity_check`.

Always dry-run first (read-only, no flags needed beyond the ones you plan to use), then exit every OpenCode process and re-run without `--dry-run`.

```bash
# 1. see exactly what would be removed and the estimated reclaim
python3 scripts/opencode_storage.py cleanup --clear-events --delete-orphan-diffs --dry-run

# 2. after exiting all OpenCode processes
python3 scripts/opencode_storage.py cleanup --clear-events --delete-orphan-diffs
```

### Privacy

The audit never reads or prints message, part, event, diff, or tool-output contents — only metadata and byte lengths.

### Notes

- Logical deletion is not physical shrink: SQLite `DELETE` only returns pages to the freelist; only `VACUUM` gives that space back to the filesystem. Reclaimable space = `freelist + event rows`.
- Keep the backup for a day or two; delete it once `integrity_check` is `ok`, OpenCode starts normally, retained sessions open, and you no longer need the deleted history.

### License

MIT — see [LICENSE](LICENSE).

---

## 中文

一个自包含的 [OpenCode](https://opencode.ai) skill：审计并安全清理 OpenCode 的本地存储——SQLite 数据库、WAL、持久化事件日志、`session_diff` 文件，以及体积异常的 `tool-output`。

审计**只读**，且绝不打印对话内容。清理按设计是破坏性操作：必须显式指定参数、先做带校验的备份、并在 OpenCode 进程存活时拒绝运行。

### 环境要求

- Python 3.8+（仅标准库，无第三方依赖）
- Linux 或 WSL（进程检测读取 `/proc`；其他平台请在清理前手动退出 OpenCode）

### 安装

把目录复制进 OpenCode 的 skill 目录：

```bash
git clone https://github.com/GoldenStain/opencode-storage-audit.git
cp -r opencode-storage-audit ~/.config/opencode/skills/
```

或建立软链接：

```bash
ln -s "$PWD/opencode-storage-audit" ~/.config/opencode/skills/opencode-storage-audit
```

### 用法

```bash
python3 scripts/opencode_storage.py audit   [--top N] [--oversized-mib N]
python3 scripts/opencode_storage.py cleanup [--clear-events] [--delete-orphan-diffs] [--dry-run]
```

通用选项：`--data-dir PATH`、`--json`、`--no-color`。

数据目录解析顺序：`--data-dir` → `$OPENCODE_DATA_DIR` → `$XDG_DATA_HOME/opencode` → `~/.local/share/opencode`。

#### audit（只读）

输出内容：

- OpenCode 数据总量，以及数据目录下各条目的体积。
- SQLite 物理大小、WAL 大小、freelist 字节数、可清理的事件字节数，以及综合可回收百分比。
- 最大的数据库表与索引。
- 按 message / part / event / `session_diff` 字节数排序的最大会话。
- 体积异常的单条消息（含会话 ID 与标题）。
- 孤儿 `session_diff` 文件。
- 事件日志大小，包含所属会话已不存在的事件。
- 最大的 `tool-output` 文件。

#### cleanup（破坏性）

- OpenCode 进程存活时**拒绝运行**（VACUUM 需要独占数据库访问），并列出占用进程的 PID。
- 改动前先创建带时间戳的备份（`opencode.db.pre-cleanup-<时间戳>.bak`）并做完整性校验。
- `--clear-events`：删除 `event` 与 `event_sequence` 行——通常是单项收益最大的清理。
- `--delete-orphan-diffs`：先归档，再删除没有对应存活会话的 `session_diff` 文件。
- 随后依次执行：VACUUM 前 `quick_check`、WAL checkpoint、`VACUUM`、VACUUM 后 `integrity_check`。

务必先 dry-run（只读，不会修改任何东西），确认后再退出所有 OpenCode 进程、去掉 `--dry-run` 正式执行。

```bash
# 1. 先看会删掉什么、预计回收多少空间
python3 scripts/opencode_storage.py cleanup --clear-events --delete-orphan-diffs --dry-run

# 2. 退出所有 OpenCode 进程后
python3 scripts/opencode_storage.py cleanup --clear-events --delete-orphan-diffs
```

### 隐私

审计过程绝不读取或打印 message、part、event、diff、tool-output 的内容——只使用元数据与字节长度。

### 说明

- 逻辑删除不等于物理缩小：SQLite 的 `DELETE` 只把页还给 freelist，只有 `VACUUM` 才能把空间真正还给文件系统。可回收空间 = `freelist + 事件行`。
- 备份建议保留一两天；待 `integrity_check` 为 `ok`、OpenCode 正常启动、保留的会话都能打开、且不再需要被删历史后，再删除备份。

### 许可

MIT，见 [LICENSE](LICENSE)。
