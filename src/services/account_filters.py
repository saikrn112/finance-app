from sqlalchemy import or_, select

from src.models import ConnectedAccount, Transaction


def transaction_account_filter(sources: list[str]):
    account_keys = [value.removeprefix("account:") for value in sources if value.startswith("account:")]
    providers = [value for value in sources if not value.startswith("account:")]
    return or_(
        Transaction.source.in_(providers),
        Transaction.plaid_account_id.in_(select(ConnectedAccount.external_account_id).where(ConnectedAccount.id.in_(account_keys))),
    )
