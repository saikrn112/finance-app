#!/usr/bin/env python3
"""Replay all historical statements into the replay stack via the import API.

Usage (inside container):
    python scripts/tools/replay_import.py [--base-url http://localhost:8002]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

from src.data_paths import raw_source_dir

BASE_URL = "http://localhost:8002"

def default_sources() -> list[tuple[str, str]]:
    from src.plugins.loader import load_plugins
    from src.plugins.registry import get_import_source_defs

    load_plugins()
    return [
        (source_key, source_key)
        for source_key, meta in get_import_source_defs().items()
        if "statement_pdf" in meta.get("allowed_kinds", set()) and meta.get("record_type") == "transactions"
    ]


def multipart_post(url: str, fields: dict[str, str], file_field: str, file_path: Path) -> dict:
    boundary = "----ReplayBoundary7f3a"
    body_parts: list[bytes] = []

    for name, value in fields.items():
        body_parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )

    file_bytes = file_path.read_bytes()
    body_parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{file_path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
        + file_bytes
        + b"\r\n"
    )
    body_parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(body_parts)
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def json_post(url: str) -> dict:
    req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def import_file(base_url: str, source: str, file_path: Path) -> dict:
    preview = multipart_post(
        f"{base_url}/api/imports/preview",
        {"source": source, "kind": "statement_pdf"},
        "file",
        file_path,
    )

    import_id = preview["import_id"]

    if preview.get("already_imported"):
        return {"file": file_path.name, "status": "already_imported", "imported": 0, "skipped": 0}

    result = json_post(f"{base_url}/api/imports/{import_id}/commit")
    return {
        "file": file_path.name,
        "status": result.get("status", "unknown"),
        "imported": result.get("imported", 0),
        "skipped": result.get("skipped", 0),
        "duplicates": preview.get("duplicate_summary", {}).get("duplicate_count", 0),
        "total": preview.get("duplicate_summary", {}).get("total_transactions", 0),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--sources", nargs="*", help="Source keys to replay (default: all known)")
    args = parser.parse_args()

    if args.sources:
        sources = [(s, s) for s in args.sources]
    else:
        sources = default_sources()

    totals = {"imported": 0, "skipped": 0, "already_imported": 0, "errors": 0}

    for source, _dirname in sources:
        stmt_dir = raw_source_dir(source)
        if not stmt_dir.exists():
            print(f"[WARN] {stmt_dir} not found, skipping", flush=True)
            continue

        files = sorted(p for p in stmt_dir.iterdir() if p.suffix.lower() == ".pdf")
        print(f"\n=== {source} ({len(files)} files) ===", flush=True)

        for file_path in files:
            try:
                r = import_file(args.base_url, source, file_path)
                status_tag = r["status"]
                if status_tag == "already_imported":
                    totals["already_imported"] += 1
                    print(f"  SKIP {file_path.name} (already imported)", flush=True)
                else:
                    totals["imported"] += r["imported"]
                    totals["skipped"] += r["skipped"]
                    print(
                        f"  OK   {file_path.name}  imported={r['imported']} skipped={r['skipped']} dupes={r['duplicates']}/{r['total']}",
                        flush=True,
                    )
            except Exception as exc:
                totals["errors"] += 1
                print(f"  ERR  {file_path.name}: {exc}", flush=True)

    print(f"\n=== DONE ===")
    print(f"  imported={totals['imported']}  skipped={totals['skipped']}  already_imported={totals['already_imported']}  errors={totals['errors']}")


if __name__ == "__main__":
    main()
