#!/usr/bin/env python3
"""Command-line entry point for auditing and cleaning OpenCode's local storage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import storage_audit
import storage_cleanup
import storage_env
import storage_render


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="OpenCode data directory (default: XDG data home)",
    )
    common.add_argument(
        "--no-color",
        action="store_true",
        default=argparse.SUPPRESS,
        help="disable ANSI color (NO_COLOR is also honored)",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit machine-readable JSON instead of a rendered report",
    )

    parser = argparse.ArgumentParser(
        prog="opencode_storage",
        description="Audit and clean OpenCode's local storage.",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser(
        "audit", parents=[common], help="read-only storage and database report"
    )
    audit.add_argument("--top", type=int, default=15, help="sessions to list (default: 15)")
    audit.add_argument(
        "--oversized-mib",
        type=float,
        default=10.0,
        help="message size warning threshold in MiB (default: 10)",
    )
    audit.add_argument(
        "--session",
        metavar="ID",
        default=None,
        help="show a focused report for one session by full id or unique prefix",
    )

    cleanup = subparsers.add_parser(
        "cleanup", parents=[common], help="back up, optionally clean, and VACUUM the database"
    )
    cleanup.add_argument(
        "--clear-events",
        action="store_true",
        help="delete event and event_sequence rows before VACUUM",
    )
    cleanup.add_argument(
        "--delete-orphan-diffs",
        action="store_true",
        help="archive then delete session_diff files without a live session",
    )
    cleanup.add_argument(
        "--backup-dir",
        type=Path,
        default=Path.home(),
        help="directory for timestamped backups (default: home directory)",
    )
    cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without modifying anything",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = storage_env.resolve_data_dir(getattr(args, "data_dir", None))
    use_json = getattr(args, "json", False)
    console = storage_render.Console(
        color=False if use_json or getattr(args, "no_color", False) else None
    )

    if args.command == "audit":
        if not data_dir.is_dir():
            storage_render.Console(stream=sys.stderr).error(
                f"data directory not found: {data_dir}"
            )
            return 1
        report = storage_audit.build_report(
            data_dir, top=args.top, oversized_mib=args.oversized_mib, session=args.session
        )
        if use_json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            storage_audit.render_report(report, console)
        detail = report.get("session_detail")
        if args.session and detail is not None and not detail.get("found"):
            return 1
        return 0

    if args.command == "cleanup":
        artifacts = storage_env.Artifacts(data_dir)
        try:
            result = storage_cleanup.run(
                artifacts,
                clear_events=args.clear_events,
                delete_orphan_diffs=args.delete_orphan_diffs,
                backup_dir=args.backup_dir,
                dry_run=args.dry_run,
            )
        except storage_cleanup.CleanupError as error:
            storage_render.Console(stream=sys.stderr).error(f"error: {error}")
            return 1
        if use_json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif result["dry_run"]:
            storage_cleanup.render_plan(
                result["plan"], result["processes"], result["backup_dir"], console
            )
        else:
            storage_cleanup.render_result(result, console)
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())