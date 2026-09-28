"""Build and render a read-only report of OpenCode's database and storage usage."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import storage_db
import storage_env
import storage_render as render


MESSAGE_DETAIL_LIMIT = 50


def _format_date(milliseconds) -> str:
    if not milliseconds:
        return "unknown"
    return datetime.datetime.fromtimestamp(milliseconds / 1000).strftime("%Y-%m-%d")


def _amount(count: int, size: int) -> str:
    """Render a row count and its byte size as one compact cell."""
    return f"{render.human_count(count)} · {render.human_bytes(size)}"


def _resolve_session(sessions: list[dict], query: str) -> tuple[dict | None, list[dict]]:
    """Match a session by exact id or unique prefix, returning it plus any ambiguity."""
    exact = [session for session in sessions if session["id"] == query]
    if exact:
        return exact[0], []
    matches = [session for session in sessions if session["id"].startswith(query)]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


def _session_detail(connection: sqlite3.Connection, session: dict) -> dict:
    """Collect metadata and the largest messages for one session."""
    session_id = session["id"]
    row = connection.execute(
        "SELECT id, title, directory, project_id, parent_id, time_created, "
        "time_updated, time_archived FROM session WHERE id=?",
        (session_id,),
    ).fetchone()
    (
        _id,
        title,
        directory,
        project_id,
        parent_id,
        created,
        updated,
        archived,
    ) = row

    messages = [
        {
            "id": message_id,
            "bytes": size or 0,
            "parts": parts,
            "created": _format_date(message_created),
        }
        for message_id, size, message_created, parts in connection.execute(
            "SELECT m.id, LENGTH(m.data), m.time_created, "
            "(SELECT COUNT(*) FROM part p WHERE p.message_id=m.id) "
            "FROM message m WHERE m.session_id=? ORDER BY LENGTH(m.data) DESC",
            (session_id,),
        )
    ]

    return {
        "found": True,
        "id": session_id,
        "title": (title or "").replace("\n", " ").strip(),
        "directory": directory or "",
        "project_id": project_id or "",
        "parent_id": parent_id or "",
        "created": _format_date(created),
        "updated": _format_date(updated),
        "archived": _format_date(archived) if archived else "",
        "message_count": session["message_count"],
        "message_bytes": session["message_bytes"],
        "part_count": session["part_count"],
        "part_bytes": session["part_bytes"],
        "event_count": session["event_count"],
        "event_bytes": session["event_bytes"],
        "diff_bytes": session["diff_bytes"],
        "total_bytes": session["total_bytes"],
        "messages": messages[:MESSAGE_DETAIL_LIMIT],
        "message_limit": MESSAGE_DETAIL_LIMIT,
    }


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
    data_dir: Path, top: int = 15, oversized_mib: float = 10.0, session: str | None = None
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
        "session_query": session or "",
        "session_detail": None,
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

        if session:
            matched, matches = _resolve_session(sessions, session)
            if matched is not None:
                report["session_detail"] = _session_detail(connection, matched)
            else:
                report["session_detail"] = {
                    "found": False,
                    "query": session,
                    "matches": [
                        {"id": item["id"], "title": item["title"]}
                        for item in matches[:10]
                    ],
                }

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


def _render_session_detail(report: dict, console: render.Console) -> None:
    """Write the focused single-session view selected by --session."""
    detail = report["session_detail"]
    console.section("Session detail")
    if not detail.get("found"):
        console.warning(f"  no session matching {detail['query']!r}")
        if detail["matches"]:
            console.write(console.dim("  prefix matches:"))
            for item in detail["matches"]:
                title = render.truncate(item["title"], 46) or "(untitled)"
                console.write(f"    {item['id']}  {title}")
        return

    console.line("id", detail["id"])
    console.line("title", render.truncate(detail["title"], 60) or "(untitled)")
    console.line("directory", detail["directory"] or "-")
    if detail["parent_id"]:
        console.line("parent", detail["parent_id"])
    if detail["project_id"]:
        console.line("project", detail["project_id"])
    console.line("created", detail["created"])
    console.line("updated", detail["updated"])
    if detail["archived"]:
        console.line("archived", detail["archived"])
    console.line("messages", _amount(detail["message_count"], detail["message_bytes"]))
    console.line("parts", _amount(detail["part_count"], detail["part_bytes"]))
    console.line("events", _amount(detail["event_count"], detail["event_bytes"]))
    console.line("diffs", render.human_bytes(detail["diff_bytes"]))
    console.line("total", console.bold(render.human_bytes(detail["total_bytes"])))

    messages = detail["messages"]
    header = (
        f"Largest messages (top {len(messages)} of "
        f"{render.human_count(detail['message_count'])})"
    )
    console.section(header)
    if messages:
        table = render.Table(
            [
                ("size", "right", "yellow"),
                ("parts", "right", ""),
                ("created", "left", "dim"),
                ("message", "left", ""),
            ]
        )
        for item in messages:
            table.add(
                render.human_bytes(item["bytes"]),
                render.human_count(item["parts"]),
                item["created"],
                item["id"],
            )
        table.write(console)
    else:
        console.write("  " + console.dim("none"))


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
    detail = report.get("session_detail")
    if detail is not None:
        _render_session_detail(report, console)
        console.write("")
        console.write(
            console.dim(
                "  DELETE only frees pages; run cleanup with OpenCode stopped to VACUUM them back."
            )
        )
        return

    if sessions:
        console.section(f"Largest sessions (top {len(sessions)} of {report['session_count']})")
        largest = max(session["total_bytes"] for session in sessions) or 1
        table = render.Table(
            [
                ("#", "right", "dim"),
                ("size", "right", "bold"),
                ("bar", "left", "cyan"),
                ("msgs", "right", ""),
                ("parts", "right", ""),
                ("events", "right", ""),
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
                _amount(session["message_count"], session["message_bytes"]),
                _amount(session["part_count"], session["part_bytes"]),
                _amount(session["event_count"], session["event_bytes"]),
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