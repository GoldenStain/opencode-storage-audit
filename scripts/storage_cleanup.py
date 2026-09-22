"""Back up and compact the OpenCode database while OpenCode is fully stopped."""

from __future__ import annotations

import datetime
import sqlite3
import zipfile
from pathlib import Path

import storage_db
import storage_env
import storage_render as render


class CleanupError(Exception):
    """A cleanup precondition failed or an operation could not complete."""


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup_database(db_path: Path, backup: Path) -> None:
    source = storage_db.connect_readonly(db_path)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination, pages=4096)
        check = destination.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        destination.close()
        source.close()
    if check != "ok":
        backup.unlink(missing_ok=True)
        raise CleanupError(f"backup integrity_check failed: {check}")


def _archive_and_remove(paths: list[Path], archive: Path) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in paths:
            output.write(path, arcname=path.name)
    with zipfile.ZipFile(archive) as verify:
        if verify.testzip() is not None:
            raise CleanupError(f"orphan diff archive failed verification: {archive}")
        if len(verify.namelist()) != len(paths):
            raise CleanupError(f"orphan diff archive count mismatch: {archive}")
    for path in paths:
        path.unlink()


def build_plan(
    artifacts: storage_env.Artifacts, clear_events: bool, delete_orphan_diffs: bool
) -> dict:
    """Describe what cleanup would remove, without modifying anything."""
    plan = {
        "db_bytes": artifacts.db_path.stat().st_size if artifacts.db_path.is_file() else 0,
        "wal_bytes": artifacts.wal_path.stat().st_size if artifacts.wal_path.exists() else 0,
        "freelist_bytes": 0,
        "clear_events": {"enabled": bool(clear_events), "rows": 0, "bytes": 0},
        "orphan_diffs": {"enabled": bool(delete_orphan_diffs), "count": 0, "bytes": 0, "files": []},
        "estimated_reclaim_bytes": 0,
    }

    if artifacts.db_path.is_file():
        connection = storage_db.connect_readonly(artifacts.db_path)
        try:
            plan["freelist_bytes"] = storage_db.page_metrics(connection)["freelist_bytes"]
            live_sessions = {row[0] for row in connection.execute("SELECT id FROM session")}
            if clear_events and storage_db.table_exists(connection, "event"):
                plan["clear_events"]["rows"] = storage_db.scalar(
                    connection, "SELECT COUNT(*) FROM event"
                )
                plan["clear_events"]["bytes"] = storage_db.scalar(
                    connection, "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM event"
                )
        finally:
            connection.close()

        if delete_orphan_diffs and artifacts.diff_dir.is_dir():
            orphan_paths = [
                path
                for path in artifacts.diff_dir.glob("*.json")
                if path.stem not in live_sessions
            ]
            plan["orphan_diffs"]["files"] = [path.name for path in orphan_paths]
            plan["orphan_diffs"]["count"] = len(orphan_paths)
            plan["orphan_diffs"]["bytes"] = sum(path.stat().st_size for path in orphan_paths)

    plan["estimated_reclaim_bytes"] = (
        plan["freelist_bytes"]
        + plan["clear_events"]["bytes"]
        + plan["orphan_diffs"]["bytes"]
    )
    return plan


def run(
    artifacts: storage_env.Artifacts,
    clear_events: bool,
    delete_orphan_diffs: bool,
    backup_dir: Path,
    dry_run: bool = False,
) -> dict:
    """Back up the database, optionally clean it, and VACUUM it back to the filesystem."""
    processes = storage_env.running_processes()
    plan = build_plan(artifacts, clear_events, delete_orphan_diffs)
    if dry_run:
        return {
            "dry_run": True,
            "plan": plan,
            "processes": processes,
            "backup_dir": str(Path(backup_dir).expanduser()),
        }

    if processes:
        detail = "\n".join(f"  PID {entry['pid']}: {entry['command']}" for entry in processes)
        raise CleanupError(
            "OpenCode is running; VACUUM requires exclusive access.\n"
            f"{detail}\nExit every OpenCode serve/attach process, then run this command again."
        )
    if not artifacts.db_path.is_file():
        raise CleanupError(f"database not found: {artifacts.db_path}")
    backup_dir = Path(backup_dir).expanduser()
    if not backup_dir.is_dir():
        raise CleanupError(f"backup directory not found: {backup_dir}")

    stamp = _timestamp()
    backup = backup_dir / f"opencode.db.pre-cleanup-{stamp}.bak"
    before = artifacts.db_path.stat().st_size
    _backup_database(artifacts.db_path, backup)

    events = {"deleted": False, "rows": 0, "bytes": 0}
    live_sessions: set[str] = set()
    integrity = "unknown"
    connection = sqlite3.connect(artifacts.db_path, timeout=10)
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise CleanupError(f"pre-VACUUM quick_check failed: {check}")

        if clear_events and storage_db.table_exists(connection, "event"):
            events["rows"] = storage_db.scalar(connection, "SELECT COUNT(*) FROM event")
            events["bytes"] = storage_db.scalar(
                connection, "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM event"
            )
            connection.execute("DELETE FROM event")
            if storage_db.table_exists(connection, "event_sequence"):
                connection.execute("DELETE FROM event_sequence")
            connection.commit()
            events["deleted"] = True

        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise CleanupError(f"post-VACUUM integrity_check failed: {integrity}")
        live_sessions = {row[0] for row in connection.execute("SELECT id FROM session")}
    finally:
        connection.close()

    orphans = {"deleted": False, "count": 0, "bytes": 0, "archive": None}
    if delete_orphan_diffs and artifacts.diff_dir.is_dir():
        orphan_paths = [
            path for path in artifacts.diff_dir.glob("*.json") if path.stem not in live_sessions
        ]
        if orphan_paths:
            archive = backup_dir / f"opencode-orphan-session-diffs-{stamp}.zip"
            bytes_total = sum(path.stat().st_size for path in orphan_paths)
            _archive_and_remove(orphan_paths, archive)
            orphans = {
                "deleted": True,
                "count": len(orphan_paths),
                "bytes": bytes_total,
                "archive": str(archive),
            }

    after = artifacts.db_path.stat().st_size
    wal_after = artifacts.wal_path.stat().st_size if artifacts.wal_path.exists() else 0
    return {
        "dry_run": False,
        "before_bytes": before,
        "after_bytes": after,
        "wal_bytes": wal_after,
        "reclaimed_bytes": before - after,
        "backup_path": str(backup),
        "backup_bytes": backup.stat().st_size,
        "events": events,
        "orphans": orphans,
        "integrity": integrity,
        "freelist_bytes": plan["freelist_bytes"],
    }


def render_plan(
    plan: dict, processes: list[dict[str, str]], backup_dir: str, console: render.Console
) -> None:
    console.section("Preflight")
    if processes:
        console.write(
            "  "
            + console.yellow(f"! {len(processes)} OpenCode process(es) running")
            + console.dim("  (fine for a dry run; blocks a real VACUUM)")
        )
    else:
        console.write("  " + console.green("✓") + " no OpenCode processes running")
    found = plan["db_bytes"] > 0
    console.write(
        "  "
        + (console.green("✓") if found else console.red("✗"))
        + f" database {'found' if found else 'missing'}"
        + console.dim(f"  {render.human_bytes(plan['db_bytes'])}")
    )

    console.section("Planned changes")
    table = render.Table(
        [("what", "left", "bold"), ("amount", "left", ""), ("flag", "left", "dim")]
    )
    events = plan["clear_events"]
    table.add(
        "event rows",
        f"{render.human_count(events['rows'])} rows · {render.human_bytes(events['bytes'])}",
        "--clear-events" if events["enabled"] else "(not enabled)",
    )
    orphans = plan["orphan_diffs"]
    table.add(
        "orphan diffs",
        f"{render.human_count(orphans['count'])} files · {render.human_bytes(orphans['bytes'])}",
        "--delete-orphan-diffs" if orphans["enabled"] else "(not enabled)",
    )
    table.add("freelist pages", render.human_bytes(plan["freelist_bytes"]), "VACUUM")
    table.add(
        "reclaimable total",
        console.green(render.human_bytes(plan["estimated_reclaim_bytes"])),
        "",
    )
    table.write(console)
    console.write("")
    console.write(
        console.dim("  backup would be written to ") + console.dim(str(backup_dir))
    )
    console.section("Dry run")
    console.write("  " + console.yellow("nothing was modified"))


def render_result(result: dict, console: render.Console) -> None:
    console.section("Preflight")
    console.write("  " + console.green("✓") + " no OpenCode processes running")

    console.section("Backup")
    console.write(
        "  "
        + console.green("✓")
        + f" {render.human_bytes(result['backup_bytes'])} written"
        + console.dim(f"  {result['backup_path']}")
    )
    console.write("  " + console.green("✓") + " backup integrity_check ok")

    console.section("Cleanup")
    events = result["events"]
    if events["deleted"]:
        console.write(
            "  "
            + console.green("✓")
            + f" {render.human_count(events['rows'])} event rows deleted"
            + console.dim(f"  {render.human_bytes(events['bytes'])}")
        )
    else:
        console.write("  " + console.dim("· events left untouched"))
    console.write("  " + console.green("✓") + " WAL checkpointed and VACUUMed")
    console.write("  " + console.green("✓") + f" integrity_check={result['integrity']}")
    orphans = result["orphans"]
    if orphans["deleted"]:
        console.write(
            "  "
            + console.green("✓")
            + f" {orphans['count']} orphan diffs archived"
            + console.dim(f"  {orphans['archive']}")
        )
    else:
        console.write("  " + console.dim("· orphan diffs left untouched"))

    console.section("Result")
    before = result["before_bytes"]
    after = result["after_bytes"]
    reclaimed = result["reclaimed_bytes"]
    fraction = reclaimed / before if before else 0.0
    console.write(
        f"  {render.human_bytes(before)} → "
        + console.bold(render.human_bytes(after))
        + console.dim(f"   WAL {render.human_bytes(result['wal_bytes'])}")
    )
    console.write(
        "  "
        + console.green(render.human_bytes(reclaimed))
        + f" reclaimed ({render.ratio_text(fraction)})  "
        + console.green(render.bar(fraction))
    )
    console.write("")
    console.write(
        console.dim("  delete the backup only after OpenCode starts and retained sessions open")
    )