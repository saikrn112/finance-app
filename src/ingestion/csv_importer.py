"""CSV Importer with provider-specific parsers and deduplication."""
import csv
import hashlib
from pathlib import Path
from datetime import datetime, date
from decimal import Decimal
from typing import Iterator
from dataclasses import dataclass

@dataclass
class RawTransaction:
    source_id: str
    date: date
    amount: Decimal
    merchant_raw: str
    account_last4: str = ""


def _get_config(source: str) -> dict | None:
    """Get CSV column config from the plugin registry."""
    from src.plugins.registry import get_csv_config

    csv_config = get_csv_config(source)
    if csv_config is None:
        return None
    return {
        "date_col": csv_config.date_col,
        "amount_col": csv_config.amount_col,
        "merchant_col": csv_config.merchant_col,
        "date_fmt": csv_config.date_fmt,
        "negate_amount": csv_config.negate_amount,
    }


def parse_csv(file_path: Path, source: str) -> Iterator[RawTransaction]:
    """Parse CSV file using provider-specific config."""
    config = _get_config(source)
    if not config:
        raise ValueError(f"Unknown source: {source}")
    
    with open(file_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                date_str = row.get(config["date_col"], "").strip()
                if not date_str:
                    continue
                
                txn_date = datetime.strptime(date_str, config["date_fmt"]).date()
                
                amount_str = row.get(config["amount_col"], "0").strip()
                amount_str = amount_str.replace("$", "").replace(",", "")
                amount = Decimal(amount_str) if amount_str else Decimal(0)
                
                if config.get("negate_amount") and amount > 0:
                    amount = -amount
                
                merchant = row.get(config["merchant_col"], "").strip()
                
                # Generate source_id from content hash
                source_id = hashlib.md5(
                    f"{date_str}|{amount}|{merchant}".encode()
                ).hexdigest()[:16]
                
                yield RawTransaction(
                    source_id=source_id,
                    date=txn_date,
                    amount=amount,
                    merchant_raw=merchant,
                )
            except (ValueError, KeyError) as e:
                continue  # Skip malformed rows


def get_file_hash(file_path: Path) -> str:
    """SHA256 hash of file contents for duplicate detection."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def import_csv_file(file_path: str, source: str, db) -> dict:
    """Import CSV file into database with deduplication."""
    from src.models import Transaction
    from src.processing.categorizer import RuleMatcher
    
    matcher = RuleMatcher()
    imported = 0
    skipped = 0
    
    for txn in parse_csv(Path(file_path), source):
        existing = db.query(Transaction).filter(
            Transaction.source == source,
            Transaction.source_id == txn.source_id
        ).first()
        
        if existing:
            skipped += 1
            continue
        
        category = matcher.match(txn.merchant_raw, float(txn.amount))
        
        db.add(Transaction(
            source=source,
            source_id=txn.source_id,
            date=txn.date,
            amount=float(txn.amount),
            merchant_raw=txn.merchant_raw,
            category=category,
            account_last4=txn.account_last4 or None,
        ))
        imported += 1
    
    db.commit()
    return {"imported": imported, "skipped": skipped}
