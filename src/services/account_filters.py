from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import aliased

from src.models import ConnectedAccount, Transaction


def transaction_account_filter(sources: list[str]):
    account_keys = [value.removeprefix("account:") for value in sources if value.startswith("account:")]
    providers = [value for value in sources if not value.startswith("account:")]
    other = aliased(ConnectedAccount)
    # Imported rows have no provider account ID. Match explicit institution, mask and
    # currency only when that identity identifies a single connected account.
    ambiguous = exists(select(other.id).where(
        other.source == ConnectedAccount.source,
        other.mask == ConnectedAccount.mask,
        other.currency == ConnectedAccount.currency,
        other.external_account_id != ConnectedAccount.external_account_id,
    )).correlate(ConnectedAccount)
    imported = exists(select(ConnectedAccount.id).where(
        ConnectedAccount.id.in_(account_keys),
        ConnectedAccount.source == Transaction.source,
        ConnectedAccount.mask.is_not(None),
        ConnectedAccount.mask != "",
        ConnectedAccount.mask == Transaction.account_last4,
        ConnectedAccount.currency == Transaction.currency,
        ~ambiguous,
    )).correlate(Transaction)
    return or_(
        Transaction.source.in_(providers),
        Transaction.plaid_account_id.in_(select(ConnectedAccount.external_account_id).where(ConnectedAccount.id.in_(account_keys))),
        and_(or_(Transaction.plaid_account_id.is_(None), Transaction.plaid_account_id == ""), imported),
    )
