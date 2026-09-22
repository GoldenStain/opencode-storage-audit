---
name: opencode-storage-audit
description: Use when OpenCode storage, opencode.db, tool-output, session_diff, or old sessions consume excessive disk space; audits usage, identifies the largest sessions, and safely prepares cleanup plus offline VACUUM.
---

# OpenCode Storage Audit

Diagnose OpenCode's local storage without reading conversation content, then clean it only with explicit user approval.

## Scripts

One entry point, standard-library Python only:

```bash
python3 scripts/opencode_storage.py audit   [--top N] [--oversized-mib N]
python3 scripts/opencode_storage.py cleanup [--clear-events] [--delete-orphan-diffs] [--dry-run]
```

Shared options on both subcommands:

- `--data-dir PATH`: override the data directory (defaults to `$XDG_DATA_HOME/opencode`).
- `--no-color`: disable ANSI color; `NO_COLOR` is honored automatically and color is off when output is not a TTY.
- `--json`: emit machine-readable JSON instead of the rendered report.

`audit` is read-only. `cleanup` is destructive and refuses to run while OpenCode is active.

Resolve script paths relative to this skill directory. Do not assume copies exist in the user's home directory.

## Audit Workflow

1. Run the read-only report first:

   ```bash
   python3 scripts/opencode_storage.py audit
   ```

   Optional arguments:

   ```bash
   python3 scripts/opencode_storage.py audit --top 20 --oversized-mib 10
   ```

2. Report these findings to the user:

   - Total OpenCode data size and per-entry sizes under the data directory.
   - Physical SQLite size, WAL size, freelist bytes, clearable event bytes, and the combined reclaimable percentage.
   - Largest database tables or indexes.
   - Largest sessions ranked by message, part, event, and `session_diff` bytes.
   - Oversized individual messages, including their session IDs and titles.
   - Orphan `session_diff` files.
   - Event-log size, including events whose session no longer exists.
   - Largest `tool-output` files.

3. Explain the difference between logical deletion and physical shrinkage. SQLite `DELETE` adds pages to the freelist; only `VACUUM` returns that space to the filesystem. Reclaimable space is `freelist + event rows`, so `--clear-events` is usually the largest single win.

4. Never print message, part, event, diff, or tool-output contents. Use metadata and byte lengths only.

## Session Cleanup

- Size alone does not prove a session is useless. Ask the user which sessions may be removed.
- Prefer normal OpenCode session deletion when the whole session is disposable.
- If one oversized `message.data` row dominates a valuable session, offer a surgical cleanup of `summary.diffs` instead of deleting the session. Back up the database first, preserve the summary object, and replace only its `diffs` array with an empty array.
- If OpenCode reports `session not found`, verify the session ID directly in the database. Child agent sessions may already have been removed or may be hidden from the normal session list.
- Re-run the audit after session deletion. Confirm that no orphan message or part rows remain.

Any direct SQL modification to messages, sessions, parts, or event data is destructive. State the exact rows and estimated bytes, then obtain explicit approval before changing them.

## Tool Output

`tool-output` contains complete output that OpenCode moved out of the UI after truncation. Large files can be legitimate command output or temporary diagnostics.

- Identify files created by the current investigation before deleting them.
- Do not delete unrelated tool output merely because it is large.
- If provenance is uncertain, report the filename and size and ask the user.

## Offline Cleanup

`VACUUM` requires exclusive database access. An agent running inside OpenCode cannot complete this final step while its own `serve` or `attach` process remains active.

1. Decide which optional cleanup flags are approved:

   - `--clear-events`: delete `event` and `event_sequence`. Warn that this discards persistent event history and may affect present or future synchronization behavior.
   - `--delete-orphan-diffs`: archive, then remove `session_diff` files with no corresponding live session.

2. Show a dry run first. It is read-only, needs no exclusive access, and lists exactly which rows and files would be removed with the estimated reclaim:

   ```bash
   python3 scripts/opencode_storage.py cleanup --clear-events --delete-orphan-diffs --dry-run
   ```

   Omit flags that were not explicitly approved.

3. Tell the user to exit every OpenCode process and run the prepared command from a normal terminal:

   ```bash
   python3 scripts/opencode_storage.py cleanup --clear-events --delete-orphan-diffs
   ```

   Omit flags that were not explicitly approved.

4. The script must refuse to run while OpenCode is active. It creates and verifies a timestamped SQLite backup before modifying the database, checkpoints WAL, performs `VACUUM`, and runs `integrity_check` afterward. Each step prints as a `✓` line with the before/after sizes and reclaimed percentage.

5. After OpenCode restarts, run the audit again and ask the user to open several retained sessions.

6. A backup may be deleted after all of the following are true:

   - The cleanup reports `integrity_check=ok`.
   - OpenCode starts normally.
   - Retained sessions open correctly.
   - The user no longer needs deleted history.

Keeping the backup for one or two days is a reasonable precaution.

## Final Report

State:

- Before and after disk usage.
- Reclaimed database, event, orphan-diff, and tool-output bytes separately.
- Sessions removed or surgically modified.
- Backup path and when it can be deleted.
- Any remaining large objects or expected growth sources.
