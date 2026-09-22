"""Build and render a read-only report of OpenCode's database and storage usage."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import storage_db
import storage_env
import storage_render as render


def _format_date(milliseconds) -> str:
    if not milliseconds:
        return "unknown"
    return datetime.datetime.fromtimestamp(milliseconds / 1000).strftime("%Y-%m-%d")


def _database_report(connection: sqlite3.Connection) -> dict:
    metrics = storage_db.page_metrics(connection)
    physical_bytes = metrics["physical_bytes"]
    freelist_bytes = metrics["freelist_bytes"]

    try:
        objects = [
            {"name": name, "bytes": size}
            for name, size in connection.execute(
                "SELECT name, SUM(pgsize) FROM dbstat "
                "GROUP BY name ORDER BY SUM(pgsize) DESC LIMIT 10"
            )
        ]
    except sqlite3.OperationalError:
        objects = []

    rows = {}
    for table in ("session", "message", "part", "event"):
        if storage_db.table_exists(connection, table):
            rows[table] = storage_db.scalar(connection, f'SELECT COUNT(*) FROM "{table}"')

    clearable_event_bytes = 0
    if storage_db.table_exists(connection, "event"):
        clearable_event_bytes = storage_db.scalar(
            connection, "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM event"
        )

    reclaimable_bytes = freelist_bytes + clearable_event_bytes
    ratio = reclaimable_bytes / physical_bytes if physical_bytes else 0.0

    return {
        "page_size": metrics["page_size"],
        "page_count": metrics["page_count"],
        "physical_bytes": physical_bytes,
        "freelist_bytes": freelist_bytes,
        "clearable_event_bytes": clearable_event_bytes,
        "reclaimable_bytes": reclaimable_bytes,
        "reclaimable_ratio": min(1.0, ratio),
        "objects": objects,
        "rows": rows,
    }


def _session_report(connection: sqlite3.Connection, artifacts: storage_env.Artifacts) -> tuple[list[dict], set[str]]:
    message_stats = {
        session_id: (count, size)
        for session_id, count, size in connection.execute(
            "SELECT session_id, COUNT(*), COALESCE(SUM(LENGTH(data)), 0) "
            "FROM message GROUP BY session_id"
        )
    }
    part_stats = {
        session_id: (count, size)
        for session_id, count, size in connection.execute(
            "SELECT session_id, COUNT(*), COALESCE(SUM(LENGTH(data)), 0) "
            "FROM part GROUP BY session_id"
        )
    }
    event_stats = {}
    if storage_db.table_exists(connection, "event"):
        event_stats = {
            aggregate_id: (count, size)
            for aggregate_id, count, size in connection.execute(
                "SELECT aggregate_id, COUNT(*), COALESCE(SUM(LENGTH(data)), 0) "
                "FROM event GROUP BY aggregate_id"
            )
        }

    diff_sizes = {}
    if artifacts.diff_dir.is_dir():
        diff_sizes = {
            path.stem: path.stat().st_size for path in artifacts.diff_dir.glob("*.json")
        }

    sessions = []
    live_sessions: set[str] = set()
    for session_id, title, directory, updated in connection.execute(
        "SELECT id, title, directory, time_updated FROM session"
    ):
        live_sessions.add(session_id)
        message_count, message_bytes = message_stats.get(session_id, (0, 0))
        part_count, part_bytes = part_stats.get(session_id, (0, 0))
        event_count, event_bytes = event_stats.get(session_id, (0, 0))
        diff_bytes = diff_sizes.get(session_id, 0)
        sessions.append(
            {
                "id": session_id,
                "title": (title or "").replace("\n", " ").strip(),
                "directory": directory or "",
                "updated": _format_date(updated),
                "message_count": message_count,
                "message_bytes": message_bytes,
                "part_count": part_count,
                "part_bytes": part_bytes,
                "event_count": event_count,
                "event_bytes": event_bytes,
                "diff_bytes": diff_bytes,
                "total_bytes": message_bytes + part_bytes + event_bytes + diff_bytes,
            }
        )
    sessions.sort(key=lambda session: session["total_bytes"], reverse=True)
    return sessions, live_sessions


def build_report(
    data_dir: Path, top: int = 15, oversized_mib: float = 10.0
) -> dict:
    """Collect storage, database, and session metadata without reading content."""
    artifacts = storage_env.Artifacts(data_dir)
    entries = storage_env.usage_entries(data_dir)
    total_bytes = sum(int(entry["bytes"]) for entry in entries)
    wal_bytes = artifacts.wal_path.stat().st_size if artifacts.wal_path.exists() else 0

    report = {
        "data_dir": str(data_dir),
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "wal_bytes": wal_bytes,
        "storage": {"total_bytes": total_bytes, "entries": entries},
        "database": {"present": False},
        "top": top,
        "oversized_mib": oversized_mib,
        "sessions": [],
        "session_count": 0,
        "oversized_messages": [],
        "orphan_diffs": {"count": 0, "bytes": 0, "files": []},
        "events": {"present": False, "count": 0, "bytes": 0, "orphan_count": 0, "orphan_bytes": 0},
        "tool_output": {"count": 0, "bytes": 0, "files": []},
    }

    if not artifacts.db_path.is_file():
        return report

    report["database"]["present"] = True
    connection = storage_db.connect_readonly(artifacts.db_path)
    try:
        report["database"] = _database_report(connection)
        report["database"]["present"] = True
        sessions, live_sessions = _session_report(connection, artifacts)
        report["session_count"] = len(sessions)
        report["sessions"] = sessions[:top]

        threshold = int(oversized_mib * 1024 * 1024)
        report["oversized_messages"] = [
            {
                "id": message_id,
                "session_id": session_id,
                "bytes": size,
                "title": (title or "").replace("\n", " ").strip(),
            }
            for message_id, session_id, size, title in connection.execute(
                "SELECT m.id, m.session_id, LENGTH(m.data), COALESCE(s.title, '') "
                "FROM message m LEFT JOIN session s ON s.id=m.session_id "
                "WHERE LENGTH(m.data) >= ? ORDER BY LENGTH(m.data) DESC",
                (threshold,),
            )
        ]

        orphan_files = []
        if artifacts.diff_dir.is_dir():
            for path in artifacts.diff_dir.glob("*.json"):
                if path.stem not in live_sessions:
                    orphan_files.append({"name": path.name, "bytes": path.stat().st_size})
        orphan_files.sort(key=lambda item: item["bytes"], reverse=True)
        report["orphan_diffs"] = {
            "count": len(orphan_files),
            "bytes": sum(item["bytes"] for item in orphan_files),
            "files": orphan_files,
        }

        if storage_db.table_exists(connection, "event"):
            orphan_count, orphan_bytes = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(e.data)), 0) FROM event e "
                "WHERE e.aggregate_id LIKE 'ses_%' "
                "AND NOT EXISTS (SELECT 1 FROM session s WHERE s.id=e.aggregate_id)"
            ).fetchone()
            report["events"] = {
                "present": True,
                "count": storage_db.scalar(connection, "SELECT COUNT(*) FROM event"),
                "bytes": storage_db.scalar(
                    connection, "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM event"
                ),
                "orphan_count": orphan_count,
                "orphan_bytes": orphan_bytes,
            }
    finally:
        connection.close()

    tool_files = []
    if artifacts.tool_output_dir.is_dir():
        for path in artifacts.tool_output_dir.iterdir():
            if path.is_file():
                tool_files.append({"name": path.name, "bytes": path.stat().st_size})
    tool_files.sort(key=lambda item: item["bytes"], reverse=True)
    report["tool_output"] = {
        "count": len(tool_files),
        "bytes": sum(item["bytes"] for item in tool_files),
        "files": tool_files[:10],
    }
    return report


def render_report(report: dict, console: render.Console) -> None:
    """Write a human-readable report to the console."""
    database = report["database"]
    storage = report["storage"]

    header = [
        f"data dir     {report['data_dir']}",
        f"generated    {report['generated_at']}",
    ]
    if database["present"]:
        header.append(
            f"database     {render.human_bytes(database['physical_bytes'])}"
            f"   WAL {render.human_bytes(report['wal_bytes'])}"
        )
        header.append(
            f"reclaimable  {render.human_bytes(database['reclaimable_bytes'])}"
            f" ({render.ratio_text(database['reclaimable_ratio'])})"
        )
    render.panel("OpenCode Storage Audit", header, console)

    console.section("Storage")
    total_bytes = storage["total_bytes"]
    table = render.Table(
        [
            ("size", "right", "bold"),
            ("share", "left", "cyan"),
            ("pct", "right", "dim"),
            ("entry", "left", ""),
        ]
    )
    for entry in storage["entries"]:
        fraction = int(entry["bytes"]) / total_bytes if total_bytes else 0.0
        suffix = "/" if entry["is_dir"] else ""
        table.add(
            render.human_bytes(int(entry["bytes"])),
            render.bar(fraction),
            render.ratio_text(fraction),
            f"{entry['name']}{suffix}",
        )
    table.write(console)
    console.line("total", console.bold(render.human_bytes(total_bytes)))

    if not database["present"]:
        console.section("Database")
        console.warning("  database not found")
    else:
        console.section("Database")
        console.line("physical", render.human_bytes(database["physical_bytes"]))
        console.line("freelist", render.human_bytes(database["freelist_bytes"]) + console.dim("  returned only by VACUUM"))
        console.line(
            "clearable",
            render.human_bytes(database["clearable_event_bytes"])
            + console.dim("  event rows, --clear-events"),
        )
        console.line(
            "reclaimable",
            console.green(render.human_bytes(database["reclaimable_bytes"]))
            + console.dim(f"  {render.ratio_text(database['reclaimable_ratio'])} of physical"),
        )
        console.line(
            "rows",
            ", ".join(f"{name}={render.human_count(count)}" for name, count in database["rows"].items()),
        )
        if database["objects"]:
            console.write("")
            objects = render.Table(
                [("size", "right", "dim"), ("object", "left", "")],
            )
            for item in database["objects"]:
                objects.add(render.human_bytes(item["bytes"]), item["name"])
            objects.write(console)

    sessions = report["sessions"]
    if sessions:
        console.section(f"Largest sessions (top {len(sessions)} of {report['session_count']})")
        largest = max(session["total_bytes"] for session in sessions) or 1
        table = render.Table(
            [
                ("#", "right", "dim"),
                ("size", "right", "bold"),
                ("bar", "left", "cyan"),
                ("msg", "right", ""),
                ("part", "right", ""),
                ("event", "right", ""),
                ("diff", "right", ""),
                ("updated", "left", "dim"),
                ("session", "left", ""),
            ]
        )
        for index, session in enumerate(sessions, start=1):
            location = session["id"] + (f"  {session['directory']}" if session["directory"] else "")
            table.add(
                index,
                render.human_bytes(session["total_bytes"]),
                render.bar(session["total_bytes"] / largest),
                render.human_bytes(session["message_bytes"]),
                render.human_bytes(session["part_bytes"]),
                render.human_bytes(session["event_bytes"]),
                render.human_bytes(session["diff_bytes"]),
                session["updated"],
                render.truncate(session["title"], 46) or "(untitled)",
                below=location,
            )
        table.write(console)

    console.section(f"Oversized messages (>= {report['oversized_mib']:g} MiB)")
    if report["oversized_messages"]:
        table = render.Table(
            [
                ("size", "right", "yellow"),
                ("message", "left", ""),
                ("session", "left", "dim"),
                ("title", "left", ""),
            ]
        )
        for item in report["oversized_messages"]:
            table.add(
                render.human_bytes(item["bytes"]),
                item["id"],
                item["session_id"],
                render.truncate(item["title"], 40),
            )
        table.write(console)
    else:
        console.write("  " + console.dim("none"))

    orphan = report["orphan_diffs"]
    console.section("Orphan session diffs")
    console.write(
        f"  {render.human_count(orphan['count'])} files · "
        f"{render.human_bytes(orphan['bytes'])}"
        + console.dim("  removable with --delete-orphan-diffs")
    )
    for item in orphan["files"][:5]:
        if item["bytes"] < 1024:
            break
        console.write(f"    {render.human_bytes(item['bytes']):>10}  {item['name']}")

    if report["events"]["present"]:
        events = report["events"]
        console.section("Event log")
        console.write(
            f"  {render.human_count(events['count'])} rows · {render.human_bytes(events['bytes'])}"
        )
        detail = (
            f"  without a live session: {render.human_count(events['orphan_count'])} rows · "
            f"{render.human_bytes(events['orphan_bytes'])}"
        )
        console.write(console.dim(detail) if not events["orphan_count"] else console.yellow(detail))

    tool = report["tool_output"]
    console.section("Tool output")
    console.write(f"  {render.human_count(tool['count'])} files · {render.human_bytes(tool['bytes'])}")
    for item in tool["files"]:
        if item["bytes"] < 1024 * 1024:
            break
        console.write(f"    {render.human_bytes(item['bytes']):>10}  {item['name']}")

    console.write("")
    console.write(
        console.dim("  DELETE only frees pages; run cleanup with OpenCode stopped to VACUUM them back.")
    )