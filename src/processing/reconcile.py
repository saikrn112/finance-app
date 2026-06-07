"""Balance reconciliation — replay transactions against known balances."""
from sqlalchemy.orm import Session
from sqlalchemy import func
from collections import defaultdict

from src.models import Transaction, Balance
from src.plugins.registry import get_credit_card_sources, get_all_sources


def reconcile(db: Session, rate_map: dict[str, float] | None = None) -> dict:
    """Reconcile all accounts by replaying transactions."""
    if rate_map is None:
        rate_map = {}

    def _convert(txn: Transaction) -> float:
        raw = float(txn._amount)
        txn_currency = getattr(txn, "currency", None) or "USD"
        rate = rate_map.get(txn_currency, 1.0)
        return raw * rate

    # Get all transactions grouped by source
    txns = db.query(Transaction).order_by(Transaction.date).all()

    # Get known balances
    balances = db.query(Balance).order_by(Balance.date).all()
    balance_map = defaultdict(list)  # source -> [{date, balance, type}]
    for b in balances:
        balance_map[b.source].append({
            "date": b.date.isoformat(),
            "balance": float(b.balance),
            "type": b.balance_type,
        })

    # Primary bank reconciliation — find the primary bank account source
    # Use the first non-credit-card financial_accounts source as the "bank" source
    all_sources = get_all_sources()
    bank_source_label = ""  # determined from registry
    for p in all_sources.values():
        if p.domain == "financial_accounts" and not p.is_credit_card:
            bank_source_label = p.label
            break

    bank = _reconcile_bank(txns, balance_map.get(bank_source_label, []), bank_source_label, _convert)

    # Per credit card reconciliation — use registry
    cc_labels = get_credit_card_sources()
    credit_cards = {}
    for cc in cc_labels:
        credit_cards[cc] = _reconcile_cc(txns, cc, bank_source_label, _convert)

    return {"bank": bank, "credit_cards": credit_cards}


def _reconcile_bank(txns: list, known_balances: list, bank_source: str = "", convert=None) -> dict:
    """Reconcile primary bank checking account."""
    bank_txns = [t for t in txns if t.source == bank_source]

    income = sum(convert(t) for t in bank_txns if float(t._amount) > 0)

    # Break down outflows
    expenses = 0
    transfers = 0
    cc_payments = defaultdict(float)

    for t in bank_txns:
        amt = float(t._amount)
        if amt >= 0:
            continue
        cat = t.category or "Uncategorized"
        top = cat.split("/")[0]
        sub = cat.split("/")[1] if "/" in cat else None

        converted = abs(convert(t))
        if top == "Credit Card" and sub:
            cc_payments[sub] += converted
        elif top in ("Investment", "Remittance"):
            transfers += converted
        else:
            expenses += converted

    total_cc = sum(cc_payments.values())
    computed_net = income - expenses - transfers - total_cc

    return {
        "transaction_count": len(bank_txns),
        "income": round(income, 2),
        "expenses": round(expenses, 2),
        "transfers": round(transfers, 2),
        "cc_payments": {k: round(v, 2) for k, v in cc_payments.items()},
        "total_cc_payments": round(total_cc, 2),
        "computed_net_change": round(computed_net, 2),
        "known_balances": known_balances,
    }


def _reconcile_cc(txns: list, cc_source: str, bank_source: str = "", convert=None) -> dict:
    """Reconcile a credit card: charges vs payments."""
    # CC-side transactions (itemized charges)
    cc_txns = [t for t in txns if t.source == cc_source]
    total_charges = sum(abs(convert(t)) for t in cc_txns if float(t._amount) < 0)
    total_credits = sum(convert(t) for t in cc_txns if float(t._amount) > 0)

    # Bank-side payments to this CC — match any "Credit Card/<sub>" category
    # where sub could be the full name, a short alias, or partial match
    bank_payments = 0.0
    for t in txns:
        if t.source != bank_source:
            continue
        category = t.category or ""
        if not category.startswith("Credit Card/"):
            continue
        sub = category.split("/", 1)[1]
        # Match if the sub-category contains or matches the cc_source label
        if sub.lower() in cc_source.lower() or cc_source.lower() in sub.lower():
            bank_payments += abs(convert(t))

    residual = total_charges - total_credits

    return {
        "transaction_count": len(cc_txns),
        "total_charges": round(total_charges, 2),
        "total_credits": round(total_credits, 2),
        "payments_from_bank": round(bank_payments, 2),
        "residual": round(residual, 2),  # should match current CC balance
        "cross_check_diff": round(total_credits - bank_payments, 2),  # refunds/rewards not from bank
    }
