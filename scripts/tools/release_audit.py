#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from src.data_paths import raw_source_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_PROD_DB = REPO_ROOT / "data" / "runtime" / "prod" / "finances.db"
DEFAULT_REPLAY_DB = REPO_ROOT / "data" / "runtime" / "replay" / "finances-replay.db"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def first_existing(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def multipart_post(url: str, *, fields: dict[str, str], file_field: str, file_path: Path) -> dict:
    boundary = "----ReleaseAuditBoundary7f3a"
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

    req = urllib.request.Request(
        url,
        data=b"".join(body_parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def _print_result(name: str, ok: bool, details: dict) -> bool:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}")
    print(json.dumps(details, indent=2, sort_keys=True))
    return ok


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=90) as resp:
        return json.loads(resp.read())


def run_parser_regression(base_url: str) -> bool:
    preview_url = f"{base_url.rstrip('/')}/api/imports/preview"
    # Parser regression cases — discovered from data/raw/ directories.
    # Each source must have at least one fixture PDF in its raw directory.
    cases = []
    statement_sources = ["bofa", "chase", "amex", "discover", "apple"]
    for source in statement_sources:
        source_dir = raw_source_dir(source)
        if not source_dir.exists():
            continue
        pdfs = sorted(p for p in source_dir.iterdir() if p.suffix.lower() == ".pdf")
        if pdfs:
            cases.append({
                "name": f"{source}_statement",
                "source": source,
                "kind": "statement_pdf",
                "path": pdfs[0],
                "check": lambda data: data["record_type"] == "transactions"
                and data["duplicate_summary"]["total_transactions"] > 0,
            })

    # Payslip sources — discover from raw directories
    from src.plugins.registry import get_import_source_defs
    source_defs = get_import_source_defs()
    for source_key, meta in source_defs.items():
        if "payslip_pdf" not in meta.get("allowed_kinds", set()):
            continue
        source_dir = raw_source_dir(source_key)
        if not source_dir.exists():
            continue
        pdfs = sorted(p for p in source_dir.iterdir() if p.suffix.lower() == ".pdf")
        if pdfs:
            cases.append({
                "name": f"{source_key}_payslip",
                "source": source_key,
                "kind": "payslip_pdf",
                "path": pdfs[0],
                "check": lambda data: data["record_type"] == "payslip"
                and (data.get("payload") or {}).get("employer"),
            })

    # Retirement CSV sources
    for source_key, meta in source_defs.items():
        if "retirement_csv" not in meta.get("allowed_kinds", set()):
            continue
        source_dir = raw_source_dir(source_key)
        if not source_dir.exists():
            continue
        csvs = sorted(p for p in source_dir.iterdir() if p.suffix.lower() == ".csv")
        if csvs:
            cases.append({
                "name": f"{source_key}_retirement",
                "source": source_key,
                "kind": "retirement_csv",
                "path": csvs[0],
                "check": lambda data: data["record_type"] == "retirement"
                and data["duplicate_summary"]["total_transactions"] > 0,
            })

    failures = []
    for case in cases:
        if not case["path"].exists():
            failures.append({"name": case["name"], "error": f"missing fixture: {case['path']}"})
            continue
        try:
            preview = multipart_post(
                preview_url,
                fields={"source": case["source"], "kind": case["kind"]},
                file_field="file",
                file_path=case["path"],
            )
        except urllib.error.HTTPError as exc:
            failures.append({"name": case["name"], "error": f"http {exc.code}"})
            continue

        if not case["check"](preview):
            failures.append(
                {
                    "name": case["name"],
                    "record_type": preview.get("record_type"),
                    "duplicate_summary": preview.get("duplicate_summary"),
                    "payload": preview.get("payload"),
                }
            )

    return _print_result(
        "parser_regression",
        ok=not failures,
        details={"base_url": base_url, "failures": failures, "checked": len(cases)},
    )


def _payslip_sort_key(payslip: dict) -> tuple[str, str]:
    raw = str(payslip.get("pay_date") or "")
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return (datetime.strptime(raw, fmt).date().isoformat(), raw)
        except ValueError:
            continue
    return (raw, raw)


def run_payroll_integrity(base_url: str, tolerance: float = 1e-6) -> bool:
    payload = fetch_json(f"{base_url.rstrip('/')}/api/payslips/")
    payslips = payload.get("payslips") or []
    failures: list[dict] = []
    ytd_failures: list[dict] = []
    employer_counts: dict[str, int] = {}

    by_employer: dict[str, list[dict]] = {}
    for payslip in payslips:
        employer = str(payslip.get("employer") or "")
        if not employer:
            continue
        by_employer.setdefault(employer, []).append(payslip)

    for employer, rows in by_employer.items():
        ordered = sorted(rows, key=_payslip_sort_key)
        employer_counts[employer] = len(ordered)
        previous_ytd: dict[str, float] = {}

        for payslip in ordered:
            pay_date = payslip.get("pay_date")
            gross = payslip.get("gross")
            net = payslip.get("net")
            total_taxes = float(payslip.get("total_taxes") or 0)
            total_deductions = float(payslip.get("total_deductions") or 0)
            taxes = payslip.get("taxes") or {}
            deductions = payslip.get("deductions") or {}

            if gross is None or net is None:
                failures.append({
                    "employer": employer,
                    "pay_date": pay_date,
                    "type": "missing_core_field",
                    "gross": gross,
                    "net": net,
                    "total_taxes": total_taxes,
                    "total_deductions": total_deductions,
                })
                continue

            gross = float(gross)
            net = float(net)
            tax_sum = sum(float(v or 0) for k, v in taxes.items() if not str(k).endswith("_ytd"))
            deduction_sum = sum(float(v or 0) for k, v in deductions.items() if not str(k).endswith("_ytd"))
            bridge_delta = gross - total_taxes - total_deductions - net

            if abs(bridge_delta) > tolerance:
                failures.append({
                    "employer": employer,
                    "pay_date": pay_date,
                    "type": "net_bridge",
                    "gross": gross,
                    "net": net,
                    "total_taxes": total_taxes,
                    "total_deductions": total_deductions,
                    "delta": round(bridge_delta, 6),
                })

            if abs(total_taxes - tax_sum) > tolerance:
                failures.append({
                    "employer": employer,
                    "pay_date": pay_date,
                    "type": "tax_sum_mismatch",
                    "total_taxes": total_taxes,
                    "tax_sum": round(tax_sum, 6),
                    "taxes": taxes,
                })

            if abs(total_deductions - deduction_sum) > tolerance:
                failures.append({
                    "employer": employer,
                    "pay_date": pay_date,
                    "type": "deduction_sum_mismatch",
                    "total_deductions": total_deductions,
                    "deduction_sum": round(deduction_sum, 6),
                    "deductions": deductions,
                })

            for bucket_name, bucket in (("taxes", taxes), ("deductions", deductions), ("earnings", payslip.get("earnings") or {})):
                for key, value in bucket.items():
                    numeric = float(value or 0)
                    if key.endswith("_ytd"):
                        previous = previous_ytd.get(f"{bucket_name}:{key}")
                        if previous is not None and numeric + tolerance < previous:
                            ytd_failures.append({
                                "employer": employer,
                                "pay_date": pay_date,
                                "type": "ytd_decreased",
                                "field": f"{bucket_name}:{key}",
                                "previous": previous,
                                "current": numeric,
                            })
                        previous_ytd[f"{bucket_name}:{key}"] = numeric

    ok = not failures and not ytd_failures
    return _print_result(
        "payroll_integrity",
        ok=ok,
        details={
            "base_url": base_url,
            "tolerance": tolerance,
            "employer_counts": employer_counts,
            "failures": failures,
            "ytd_failures": ytd_failures,
        },
    )


def _load_monthly_totals(db_path: Path) -> dict[tuple[str, str], tuple[int, float]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            select source,
                   substr(date, 1, 7) as month,
                   count(*) as txn_count,
                   round(sum(amount), 2) as total_amount
            from transactions
            where coalesce(origin, '') != 'plaid'
            group by source, month
            order by source, month
            """
        ).fetchall()
        return {(source, month): (int(txn_count), float(total_amount or 0)) for source, month, txn_count, total_amount in rows}
    finally:
        conn.close()


def _load_source_totals(db_path: Path) -> dict[str, tuple[int, float]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            select source, count(*) as txn_count, round(sum(amount), 2) as total_amount
            from transactions
            where coalesce(origin, '') != 'plaid'
            group by source
            order by source
            """
        ).fetchall()
        return {source: (int(txn_count), float(total_amount or 0)) for source, txn_count, total_amount in rows}
    finally:
        conn.close()


def run_parity(prod_db: Path, replay_db: Path) -> bool:
    prod_by_source = _load_source_totals(prod_db)
    replay_by_source = _load_source_totals(replay_db)
    prod_by_month = _load_monthly_totals(prod_db)
    replay_by_month = _load_monthly_totals(replay_db)

    source_diffs = []
    for key in sorted(set(prod_by_source) | set(replay_by_source)):
        if prod_by_source.get(key) != replay_by_source.get(key):
            source_diffs.append({"source": key, "prod": prod_by_source.get(key), "replay": replay_by_source.get(key)})

    month_diffs = []
    for key in sorted(set(prod_by_month) | set(replay_by_month)):
        if prod_by_month.get(key) != replay_by_month.get(key):
            source, month = key
            month_diffs.append({"source": source, "month": month, "prod": prod_by_month.get(key), "replay": replay_by_month.get(key)})

    return _print_result(
        "per_source_date_range_parity",
        ok=not source_diffs and not month_diffs,
        details={
            "prod_db": str(prod_db),
            "replay_db": str(replay_db),
            "source_diffs": source_diffs,
            "month_diffs": month_diffs[:50],
            "truncated_month_diffs": max(0, len(month_diffs) - 50),
        },
    )


def run_integrity(db_path: Path) -> bool:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        duplicate_source_ids = [
            dict(row)
            for row in conn.execute(
                """
                select source, source_id, count(*) as dupes
                from transactions
                where coalesce(source_id, '') != ''
                group by source, source_id
                having count(*) > 1
                order by dupes desc, source, source_id
                """
            )
        ]
        plaid_origin_mismatches = [
            dict(row)
            for row in conn.execute(
                """
                select source, source_id, origin, date, amount, merchant_raw
                from transactions
                where source_id like 'plaid-%' and coalesce(origin, '') != 'plaid'
                order by date desc
                """
            )
        ]
        negative_salary = [
            dict(row)
            for row in conn.execute(
                """
                select source, source_id, date, amount, category, merchant_raw
                from transactions
                where category like 'Salary/%' and amount < 0
                order by date desc
                """
            )
        ]
        duplicate_import_hashes = [
            dict(row)
            for row in conn.execute(
                """
                select sync_type, file_hash, count(*) as dupes
                from sync_log
                where status = 'success' and coalesce(file_hash, '') != ''
                group by sync_type, file_hash
                having count(*) > 1
                order by dupes desc, sync_type, file_hash
                """
            )
        ]
        uncategorized_count = conn.execute(
            "select count(*) from transactions where coalesce(category, 'Uncategorized') = 'Uncategorized'"
        ).fetchone()[0]

        ok = not duplicate_source_ids and not plaid_origin_mismatches and not negative_salary and not duplicate_import_hashes
        return _print_result(
            "data_integrity",
            ok=ok,
            details={
                "db": str(db_path),
                "duplicate_source_ids": duplicate_source_ids[:20],
                "plaid_origin_mismatches": plaid_origin_mismatches[:20],
                "negative_salary": negative_salary[:20],
                "duplicate_import_hashes": duplicate_import_hashes[:20],
                "uncategorized_count": uncategorized_count,
            },
        )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Release audit helpers for finance_app 1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_all = subparsers.add_parser("all")
    parser_all.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser_all.add_argument("--prod-db", type=Path, default=DEFAULT_PROD_DB)
    parser_all.add_argument("--replay-db", type=Path, default=DEFAULT_REPLAY_DB)

    parser_regression = subparsers.add_parser("parser-regression")
    parser_regression.add_argument("--base-url", default=DEFAULT_BASE_URL)

    parser_parity = subparsers.add_parser("parity")
    parser_parity.add_argument("--prod-db", type=Path, default=DEFAULT_PROD_DB)
    parser_parity.add_argument("--replay-db", type=Path, default=DEFAULT_REPLAY_DB)

    parser_integrity = subparsers.add_parser("integrity")
    parser_integrity.add_argument("--db", type=Path, default=DEFAULT_PROD_DB)

    parser_payroll = subparsers.add_parser("payroll-integrity")
    parser_payroll.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser_payroll.add_argument("--tolerance", type=float, default=1e-6)

    args = parser.parse_args()

    if args.command == "parser-regression":
        return 0 if run_parser_regression(args.base_url) else 1
    if args.command == "parity":
        return 0 if run_parity(args.prod_db, args.replay_db) else 1
    if args.command == "integrity":
        return 0 if run_integrity(args.db) else 1
    if args.command == "payroll-integrity":
        return 0 if run_payroll_integrity(args.base_url, args.tolerance) else 1
    if args.command == "all":
        ok = True
        ok = run_parser_regression(args.base_url) and ok
        ok = run_parity(args.prod_db, args.replay_db) and ok
        ok = run_integrity(args.prod_db) and ok
        ok = run_payroll_integrity(args.base_url) and ok
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
