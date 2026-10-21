"""Plugins that share a source_key extend each other instead of the last one replacing the rest.

Found as "choosing Chase credit card goes to Chase bank accounts, and the import parser breaks": a
private plugin meant only to add Plaid account names replaced the bundled Chase template wholesale,
dropping its statement parser, its CSV support and its "Credit Cards" group.
"""
from src.plugins.base import CsvColumnConfig, ParserPlugin
from src.plugins.loader import _merge_same_key


def _parser(*_args, **_kwargs):
    return []


def _profile(account):
    return {"display_name": "Example Rewards"}


TEMPLATE = ParserPlugin(
    source_key="example_card", label="Example Card", record_type="transactions",
    allowed_kinds={"csv", "statement_pdf"}, domain="financial_accounts", directory_name="example",
    group="Credit Cards", statement_parser=_parser,
    csv_config=CsvColumnConfig(date_col="Date", amount_col="Amount", merchant_col="Description"),
    is_credit_card=True,
)
METADATA_ONLY = ParserPlugin(
    source_key="example_card", label="Example Card", record_type="transactions",
    allowed_kinds={"statement_pdf"}, domain="financial_accounts", directory_name="example",
    plaid_account_profile=_profile,
)


def test_a_metadata_plugin_keeps_the_templates_parser_and_group():
    (merged,) = _merge_same_key([TEMPLATE, METADATA_ONLY])
    assert merged.statement_parser is _parser, "imports broke when the parser was dropped"
    assert merged.csv_config is not None
    assert merged.group == "Credit Cards", "Chase cards were filed under Bank Accounts"


def test_what_the_later_plugin_sets_still_wins():
    (merged,) = _merge_same_key([TEMPLATE, METADATA_ONLY])
    assert merged.plaid_account_profile is _profile


def test_an_extension_cannot_take_away_an_import_kind():
    (merged,) = _merge_same_key([TEMPLATE, METADATA_ONLY])
    assert merged.allowed_kinds == {"csv", "statement_pdf"}


def test_an_explicit_override_is_respected():
    relabelled = ParserPlugin(
        source_key="example_card", label="Example Card", record_type="transactions",
        allowed_kinds={"statement_pdf"}, domain="financial_accounts", directory_name="example",
        group="Bank Accounts",
    )
    (merged,) = _merge_same_key([TEMPLATE, relabelled])
    assert merged.group == "Bank Accounts"


def test_distinct_plugins_are_untouched():
    other = ParserPlugin(source_key="other", label="Other", record_type="transactions",
                         allowed_kinds={"csv"}, domain="financial_accounts", directory_name="other")
    assert [p.source_key for p in _merge_same_key([TEMPLATE, other])] == ["example_card", "other"]
