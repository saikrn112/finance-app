import click
import os
import shutil
from pathlib import Path
from datetime import datetime


@click.group()
def cli():
    """Personal Finance CLI"""
    pass


@cli.command()
def fetch():
    """Fetch transactions from Plaid"""
    click.echo("Plaid integration not yet configured.")
    click.echo("Configure app-owned credentials first.")


@cli.command("import")
@click.option("--file", "-f", "file_path", required=True, type=click.Path(exists=True))
@click.option("--source", "-s", required=True, type=str, help="Source key from your plugins (e.g., my_bank)")
@click.option("--dry-run", is_flag=True, help="Preview without inserting")
def import_csv(file_path: str, source: str, dry_run: bool):
    """Import transactions from CSV"""
    from src.ingestion.csv_importer import parse_csv, get_file_hash
    from src.processing.categorizer import CategorizationEngine
    from src.models import SessionLocal, Transaction, SyncLog, init_db
    from src.config import settings
    from src.data_paths import raw_source_dir
    
    init_db()
    
    file_path = Path(file_path)
    file_hash = get_file_hash(file_path)
    
    db = SessionLocal()
    
    # Check if file already imported
    existing = db.query(SyncLog).filter(SyncLog.file_hash == file_hash).first()
    if existing and not dry_run:
        click.echo(f"⚠️  File already imported on {existing.created_at}")
        if not click.confirm("Import anyway?"):
            return
    
    # Parse CSV
    transactions = list(parse_csv(file_path, source))
    click.echo(f"Parsed {len(transactions)} transactions from {file_path.name}")
    
    if dry_run:
        click.echo("\n[DRY RUN] First 5 transactions (raw amounts, no currency conversion):")
        for t in transactions[:5]:
            click.echo(f"  {t.date} | {float(t._amount):>10.2f} | {t.merchant_raw[:40]}")
        click.echo(f"\n[DRY RUN] Would import {len(transactions)} transactions")
        return
    
    # Categorize and insert
    categorizer = CategorizationEngine(settings.gemini.api_key)
    inserted, skipped = 0, 0
    
    for raw in transactions:
        # Check for duplicate
        exists = db.query(Transaction).filter(
            Transaction.source == source,
            Transaction.source_id == raw.source_id
        ).first()
        
        if exists:
            skipped += 1
            continue
        
        # Categorize
        cat_result = categorizer.categorize(raw.merchant_raw, float(raw._amount), use_llm=False)
        
        txn = Transaction(
            source_id=raw.source_id,
            source=source,
            date=raw.date,
            amount=raw._amount,
            merchant_raw=raw.merchant_raw,
            merchant_clean=cat_result.merchant_clean or _clean_merchant(raw.merchant_raw),
            category=cat_result.category,
            category_source=cat_result.source,
        )
        db.add(txn)
        inserted += 1
    
    # Archive raw file
    raw_dir = raw_source_dir(source)
    raw_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{source}_{file_path.name}"
    shutil.copy(file_path, raw_dir / archive_name)
    
    # Log sync
    log = SyncLog(
        source=source,
        sync_type="csv",
        file_hash=file_hash,
        record_count=inserted,
        status="success",
    )
    db.add(log)
    db.commit()
    
    click.echo(f"✓ Imported {inserted} transactions, skipped {skipped} duplicates")
    click.echo(f"✓ Archived to {raw_dir / archive_name}")


@cli.command()
@click.option("--host", default="0.0.0.0")
@click.option("--port", default=8000)
@click.option("--reload/--no-reload", default=True, help="Auto-reload on code changes")
@click.option("--demo/--live", default=False, help="Use isolated seeded demo data")
def serve(host: str, port: int, reload: bool, demo: bool):
    """Start the web server"""
    _run_server(host=host, port=port, demo=demo, reload=reload)


@cli.command()
@click.option("--host", default="0.0.0.0")
@click.option("--port", default=8001)
@click.option("--reload/--no-reload", default=True, help="Auto-reload on code changes")
def demo(host: str, port: int, reload: bool):
    """Start an isolated seeded demo server."""
    _run_server(host=host, port=port, demo=True, reload=reload)


@cli.command()
def reparse():
    """Rebuild database from raw files"""
    from src.data_paths import RAW_ROOT

    raw_dirs = [path for path in RAW_ROOT.rglob("*") if path.is_dir()]
    csv_files = [file_path for raw_dir in raw_dirs for file_path in raw_dir.glob("*.csv")]
    if not csv_files:
        click.echo("No raw files found")
        return

    click.echo(f"Found {len(csv_files)} raw CSV files")
    click.echo("Reparse not yet implemented - would rebuild from these files")


@cli.command()
@click.option("--use-llm", is_flag=True, help="Use Gemini for uncategorized")
def categorize(use_llm: bool):
    """Re-run categorization on uncategorized transactions"""
    from src.processing.categorizer import CategorizationEngine
    from src.models import SessionLocal, Transaction, init_db
    from src.config import settings
    
    init_db()
    db = SessionLocal()
    
    uncategorized = db.query(Transaction).filter(
        Transaction.category.in_(["Uncategorized", None])
    ).all()
    
    if not uncategorized:
        click.echo("No uncategorized transactions")
        return
    
    click.echo(f"Found {len(uncategorized)} uncategorized transactions")
    
    categorizer = CategorizationEngine(settings.gemini.api_key if use_llm else "")
    updated = 0
    
    for txn in uncategorized:
        result = categorizer.categorize(txn.merchant_raw, float(txn._amount), use_llm=use_llm)
        if result.category == "Uncategorized" and txn.merchant_clean:
            result = categorizer.categorize(txn.merchant_clean, float(txn._amount), use_llm=use_llm)
        if result.category != "Uncategorized":
            txn.category = result.category
            txn.category_source = result.source
            if result.merchant_clean:
                txn.merchant_clean = result.merchant_clean
            updated += 1
    
    db.commit()
    click.echo(f"✓ Updated {updated} transactions")



def _clean_merchant(raw: str) -> str:
    """Basic merchant name cleanup."""
    import re
    # Remove common suffixes/prefixes
    cleaned = re.sub(r'\*.*$', '', raw)  # Remove * and everything after
    cleaned = re.sub(r'#\d+', '', cleaned)  # Remove #123 patterns
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:50] if cleaned else raw[:50]


def _run_server(host: str, port: int, demo: bool, reload: bool):
    if demo:
        os.environ["FINANCE_APP_MODE"] = "demo"
        os.environ.setdefault("FINANCE_APP_DATA_DIR", "data")
        os.environ.setdefault("FINANCE_APP_RUNTIME_DIR", "data/runtime/demo")
        os.environ.setdefault("FINANCE_APP_DB_PATH", "data/runtime/demo/finances-demo.db")
        click.echo(f"Starting DEMO server at http://{host}:{port}")
        click.echo(f"Using isolated demo database: {os.environ['FINANCE_APP_DB_PATH']}")
    else:
        os.environ.pop("FINANCE_APP_MODE", None)

    os.environ["FINANCE_APP_HOST"] = host
    os.environ["FINANCE_APP_PORT"] = str(port)

    from src.models import init_db

    init_db()

    import uvicorn

    if reload:
        click.echo("Reload enabled for source files under src/")
        uvicorn.run("src.api.server:app", host=host, port=port, reload=True, reload_dirs=["src"])
    else:
        click.echo("Reload disabled")
        uvicorn.run("src.api.server:app", host=host, port=port, reload=False)


@cli.command("backfill-schema")
def backfill_schema():
    """One-time migration: backfill normalized tables from SyncLog JSON."""
    from src.models import SessionLocal, init_db
    from src.ingestion.backfill_schema import run_backfill

    init_db()
    db = SessionLocal()
    try:
        run_backfill(db)
        click.echo("Backfill complete.")
    finally:
        db.close()


@cli.command("backfill-payslips")
def backfill_payslips_command():
    """One-time migration: backfill normalized payslips from SyncLog JSON."""
    from src.models import SessionLocal, init_db
    from src.ingestion.backfill_schema import backfill_payslips

    init_db()
    db = SessionLocal()
    try:
        inserted = backfill_payslips(db)
        click.echo(f"Payslip backfill complete. Inserted {inserted} row(s).")
    finally:
        db.close()


@cli.command("backfill-audit-facts")
@click.option("--apply", "apply_changes", is_flag=True, help="Persist the previewed rows")
def backfill_audit_facts_command(apply_changes: bool):
    """Backfill persisted investment periods and Plaid product enrollment."""
    from src.models import SessionLocal, init_db
    from src.ingestion.backfill_audit_facts import preview_backfill, run_backfill

    init_db()
    db = SessionLocal()
    try:
        click.echo(f"Preview: {preview_backfill(db)}")
        if not apply_changes:
            click.echo("Dry run only. Re-run with --apply to persist changes.")
            return
        click.echo(f"Applied: {run_backfill(db)}")
    finally:
        db.close()


@cli.command("reroute-plaid-transactions")
@click.option("--apply", "apply_changes", is_flag=True, help="Move rows after preview")
def reroute_plaid_transactions_command(apply_changes: bool):
    """Apply plugin routing policy to previously stored Plaid account activity."""
    from src.models import SessionLocal, init_db
    from src.ingestion.plaid_activity import reroute_account_activity_to_transactions

    init_db()
    db = SessionLocal()
    try:
        result = reroute_account_activity_to_transactions(db, apply=apply_changes)
        click.echo(f"{'Applied' if apply_changes else 'Preview'}: {result}")
        if not apply_changes:
            click.echo("Dry run only. Re-run with --apply after backing up the database.")
    finally:
        db.close()


@cli.command("backfill-sync-identity")
@click.option("--apply", "apply_changes", is_flag=True, help="Persist the previewed rows")
def backfill_sync_identity_command(apply_changes: bool):
    """Mint multi-device sync identity (uid, updated_at) for pre-existing rows."""
    from src.models import SessionLocal, init_db
    from src.ingestion.backfill_sync_identity import preview_backfill, run_backfill

    init_db()
    db = SessionLocal()
    try:
        click.echo(f"Preview: {preview_backfill(db)}")
        if not apply_changes:
            click.echo("Dry run only. Re-run with --apply after backing up the database.")
            return
        click.echo(f"Applied: {run_backfill(db)}")
    finally:
        db.close()


@cli.command("sync")
@click.option("--label", default=None, help="Label to publish for this device")
@click.option("--status", "status_only", is_flag=True, help="Show what would be used, run nothing")
def sync_command(label: str | None, status_only: bool):
    """Run one multi-device sync round (publish, fetch peers, merge)."""
    from src.models import SessionLocal, init_db
    from src.sync import device
    from src.sync.payload import find_unsyncable
    from src.sync.runner import choose_transport, sync_once

    init_db()
    db = SessionLocal()
    try:
        click.echo(f"device: {device.current_device_id()}  ({device.default_device_label()})")
        choice = choose_transport(db)
        click.echo(f"transport: {choice.mode} -- {choice.reason}")

        unsyncable = find_unsyncable(db)
        if unsyncable:
            # Reported every run: these rows cannot be named, so they never travel, and a silent
            # omission is exactly the kind of difference nobody notices until numbers disagree.
            click.echo(f"warning: {unsyncable} row(s) cannot sync (orphaned links)")

        if status_only:
            return
        if choice.transport is None:
            click.echo("Nothing to do. Set FINANCE_APP_SYNC=1 to enable sync.")
            return

        result = sync_once(db, device_label=label)
        click.echo(f"peers seen: {result.get('peers_seen')}")
        for peer, report in (result.get("merges") or {}).items():
            interesting = {
                k: v for k, v in report.items()
                if v and k in ("inserted", "updated", "deletions_applied", "uid_converged",
                               "unresolved", "rename_conflicts", "error")
            }
            click.echo(f"  from {peer}: {interesting or 'nothing new'}")
        click.echo(f"published: {result.get('published')}")
        if result.get("error"):
            click.echo(f"error: {result['error']}")
    finally:
        db.close()


if __name__ == "__main__":
    cli()
