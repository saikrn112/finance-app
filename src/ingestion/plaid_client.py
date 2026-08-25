import plaid
from plaid.api import plaid_api
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
from datetime import date

from src.config import get_config


def get_plaid_client():
    config = get_config()
    plaid_config = config.get("plaid", {})
    
    env = plaid_config.get("environment", "sandbox").lower()
    # Note: plaid-python v38+ only has Sandbox and Production
    # Development uses Sandbox host with development credentials
    host = {
        "sandbox": plaid.Environment.Sandbox,
        "development": plaid.Environment.Sandbox,  # Dev uses sandbox host
        "production": plaid.Environment.Production,
    }.get(env, plaid.Environment.Sandbox)
    
    configuration = plaid.Configuration(
        host=host,
        api_key={
            "clientId": plaid_config.get("client_id", ""),
            "secret": plaid_config.get("secret", ""),
        }
    )
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))


def create_link_token(user_id: str = "user-1", products: list[str] = None, access_token: str | None = None) -> str:
    """Create a link token for Plaid Link initialization."""
    client = get_plaid_client()
    
    prods = [Products(p) for p in (products or ["transactions"])]
    request_kwargs = dict(
        products=prods,
        client_name="Finance App",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id=user_id),
    )
    if access_token:
        request_kwargs["access_token"] = access_token
    request = LinkTokenCreateRequest(
        **request_kwargs,
    )
    response = client.link_token_create(request)
    return response.link_token


def exchange_public_token(public_token: str, metadata: dict = None) -> tuple[str, str, str]:
    """Exchange public token for access token. Returns (access_token, item_id, institution_name)."""
    client = get_plaid_client()
    
    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    
    institution_name = ""
    if metadata and "institution" in metadata:
        institution_name = metadata["institution"].get("name", "")
    
    return response.access_token, response.item_id, institution_name


def remove_item(access_token: str) -> None:
    """Revoke a Plaid Item so a local disconnect also stops remote access."""
    client = get_plaid_client()
    client.item_remove(ItemRemoveRequest(access_token=access_token))


def sync_transactions(access_token: str, cursor: str | None = None) -> dict:
    """Fetch transactions using sync API. Returns transactions and next cursor."""
    client = get_plaid_client()
    
    request = TransactionsSyncRequest(
        access_token=access_token,
        cursor=cursor or "",
        options={
            "include_original_description": True,
        },
    )
    response = client.transactions_sync(request)
    account_map = {a.account_id: a for a in getattr(response, "accounts", []) or []}
    
    return {
        "added": [_transform_txn(t, account_map) for t in response.added],
        "modified": [_transform_txn(t, account_map) for t in response.modified],
        "removed": [t.transaction_id for t in response.removed],
        "cursor": response.next_cursor,
        "has_more": response.has_more,
    }


def _transform_txn(t, account_map: dict | None = None) -> dict:
    """Transform Plaid transaction to our format."""
    account = (account_map or {}).get(getattr(t, "account_id", None))
    payment_channel = getattr(t, "payment_channel", None)
    payment_channel_value = getattr(payment_channel, "value", payment_channel)
    return {
        "source_id": t.transaction_id,
        "date": t.date if isinstance(t.date, date) else date.fromisoformat(str(t.date)),
        "authorized_date": (
            t.authorized_date if isinstance(getattr(t, "authorized_date", None), date)
            else date.fromisoformat(str(t.authorized_date))
            if getattr(t, "authorized_date", None)
            else None
        ),
        "amount": -float(t.amount),  # Plaid: positive = debit, we want negative for spending
        "merchant_raw": t.name,
        "merchant_clean": t.merchant_name or t.name,
        "account_last4": getattr(account, "mask", None) or (t.account_id[-4:] if t.account_id else None),
        "plaid_account_id": getattr(t, "account_id", None),
        "plaid_mask": getattr(account, "mask", None),
        "original_description": getattr(t, "original_description", None),
        "payment_channel": payment_channel_value,
        "plaid_category": t.personal_finance_category.primary if t.personal_finance_category else None,
        "pending_transaction_id": getattr(t, "pending_transaction_id", None),
        "pending": getattr(t, "pending", False),
        "currency": getattr(t, "iso_currency_code", None) or "USD",
    }


def get_investment_holdings(access_token: str) -> dict:
    """Fetch investment holdings."""
    from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
    client = get_plaid_client()
    request = InvestmentsHoldingsGetRequest(access_token=access_token)
    response = client.investments_holdings_get(request)
    
    securities = {s.security_id: s for s in response.securities}
    holdings = []
    for h in response.holdings:
        sec = securities.get(h.security_id)
        holdings.append({
            "account_id": h.account_id,
            "security_id": h.security_id,
            "ticker": sec.ticker_symbol if sec else None,
            "name": sec.name if sec else None,
            "quantity": float(h.quantity),
            "price": float(h.institution_price),
            "value": float(h.institution_value),
            "cost_basis": float(h.cost_basis) if h.cost_basis else None,
            "type": sec.type if sec else None,
        })
    
    accounts = []
    for a in response.accounts:
        # InvestmentAccount is a composed Plaid model. In some SDK versions,
        # direct access to ``balances`` raises ApiValueError even though the
        # response is valid; its serialized representation remains reliable.
        account = a.to_dict()
        balances = account.get("balances") or {}
        accounts.append({
            "account_id": account.get("account_id"),
            "name": account.get("name"),
            "type": account.get("type"),
            "subtype": account.get("subtype"),
            "balance": float(balances["current"]) if balances.get("current") is not None else None,
        })
    
    return {"holdings": holdings, "accounts": accounts}


def get_investment_transactions(
    access_token: str,
    *,
    start_date: date,
    end_date: date,
) -> list[dict]:
    """Fetch all investment transactions in a date range."""
    from plaid.model.investments_transactions_get_request import InvestmentsTransactionsGetRequest
    from plaid.model.investments_transactions_get_request_options import InvestmentsTransactionsGetRequestOptions

    client = get_plaid_client()
    offset = 0
    page_size = 500
    transactions: list[dict] = []
    while True:
        response = client.investments_transactions_get(
            InvestmentsTransactionsGetRequest(
                access_token=access_token,
                start_date=start_date,
                end_date=end_date,
                options=InvestmentsTransactionsGetRequestOptions(count=page_size, offset=offset),
            )
        )
        accounts = {item["account_id"]: item for item in (account.to_dict() for account in response.accounts)}
        securities = {item.security_id: item for item in response.securities}
        for transaction in response.investment_transactions:
            account = accounts.get(transaction.account_id, {})
            security = securities.get(transaction.security_id)
            activity_type = str(transaction.type.value if hasattr(transaction.type, "value") else transaction.type)
            subtype = str(transaction.subtype.value if hasattr(transaction.subtype, "value") else transaction.subtype)
            normalized_type = (
                "interest" if subtype == "interest"
                else "dividend" if subtype == "dividend"
                else "trade" if activity_type in {"buy", "sell"}
                else activity_type
            )
            transactions.append({
                "source_id": transaction.investment_transaction_id,
                "date": transaction.date,
                # Plaid investment amounts are positive for cash outflow.
                "amount": -float(transaction.amount),
                "merchant_raw": transaction.name,
                "merchant_clean": security.name if security else transaction.name,
                "account_last4": account.get("mask"),
                "plaid_account_id": transaction.account_id,
                "original_description": transaction.name,
                "currency": transaction.iso_currency_code or "USD",
                "pending": False,
                "activity_type": normalized_type,
                "raw_data": {
                    "investment_type": activity_type,
                    "investment_subtype": subtype,
                    "security_id": transaction.security_id,
                    "ticker": security.ticker_symbol if security else None,
                    "quantity": float(transaction.quantity),
                    "price": float(transaction.price),
                    "fees": float(transaction.fees or 0),
                    "transaction_datetime": transaction.transaction_datetime.isoformat() if transaction.transaction_datetime else None,
                    "cancel_transaction_id": transaction.cancel_transaction_id,
                },
            })
        offset += len(response.investment_transactions)
        if offset >= int(response.total_investment_transactions or 0) or not response.investment_transactions:
            break
    return transactions


def get_account_balances(access_token: str) -> list[dict]:
    """Fetch account balances."""
    from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
    client = get_plaid_client()
    request = AccountsBalanceGetRequest(access_token=access_token)
    response = client.accounts_balance_get(request)
    return [{"account_id": a.account_id, "name": a.name,
             "mask": getattr(a, "mask", None),
             "type": a.type.value if a.type else None,
             "subtype": a.subtype.value if a.subtype else None,
             "current": float(a.balances.current) if a.balances.current is not None else None}
            for a in response.accounts]
