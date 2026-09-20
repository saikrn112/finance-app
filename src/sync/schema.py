"""What travels for each synced table, and how it merges.

One table of declarations rather than ten hand-written serialisers, so the payload builder and the
merge engine cannot disagree about a field -- a field the builder sends and the merge ignores is a
change that silently never arrives, and that class of bug is invisible in testing until two real
devices disagree.

## The three merge behaviours

`FACT`
    Insert if unseen; if already present, only `mutable` fields are considered, by last-write-wins.
    `transactions` is the important case: the amount, date and merchant are provider facts and are
    never overwritten by a peer, while category, tags and notes are user annotations and are. So
    money is never subject to LWW -- only the labels on it are.

`OWNED`
    A row this app's user creates and edits. Whole-row last-write-wins on `updated_at`, matched by
    `uid` first and then by natural name, so two devices that independently created "Japan Trip"
    converge instead of duplicating.

`LINK`
    A join row named by its parents. Insert if unseen, LWW on `mutable`. Applied after everything
    it references.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from src.sync import coding, refs

FACT = "fact"
OWNED = "owned"
LINK = "link"


@dataclass(frozen=True)
class Field:
    """One column on the wire."""

    name: str
    to_wire: Callable[[object], object]
    from_wire: Callable[[object], object]
    #: The attribute to read/write, when it differs from the wire name. Money columns are private
    #: (`_amount`) because reading the public property raises on purpose.
    attr: str | None = None

    @property
    def attribute(self) -> str:
        return self.attr or self.name


def _identity(value):
    return value


def text(name: str, attr: str | None = None) -> Field:
    return Field(name, _identity, _identity, attr)


def flag(name: str, attr: str | None = None) -> Field:
    return Field(name, bool, coding.bool_from_wire, attr)


def money(name: str, attr: str | None = None) -> Field:
    return Field(name, coding.decimal_to_wire, coding.decimal_from_wire, attr)


def day(name: str, attr: str | None = None) -> Field:
    return Field(name, coding.date_to_wire, coding.date_from_wire, attr)


def moment(name: str, attr: str | None = None) -> Field:
    return Field(name, coding.datetime_to_wire, coding.datetime_from_wire, attr)


def integer(name: str, attr: str | None = None) -> Field:
    """A whole-number column. Distinct from `money` so an ordering value does not travel as a
    decimal string and come back as a `Decimal` -- harmless in effect, misleading to read."""
    return Field(name, lambda v: None if v is None else int(v), lambda v: None if v in (None, "") else int(v), attr)


def blob(name: str, attr: str | None = None) -> Field:
    """A JSON column. Passed through -- it is already JSON-representable."""
    return Field(name, _identity, _identity, attr)


@dataclass(frozen=True)
class TableSpec:
    #: Key in the payload's `records` map. Also the SQL table name.
    name: str
    kind: str
    behaviour: str
    model_name: str
    #: Set once at insert and never overwritten by a peer.
    immutable: tuple[Field, ...] = ()
    #: Considered on every merge, by last-write-wins.
    mutable: tuple[Field, ...] = ()
    #: Fields identifying the row locally, used to build the natural reference on the way out.
    ref_attrs: tuple[str, ...] = ()
    #: Key parts that may legitimately be NULL, encoded as null rather than making the row
    #: unnameable. Declared per table rather than inferred from column nullability: `rules.uid` is
    #: nullable only because it is added by ALTER and minted later, and treating its NULL as a value
    #: would give every un-backfilled rule the same reference.
    ref_nullable: tuple[str, ...] = ()

    @property
    def all_fields(self) -> tuple[Field, ...]:
        return self.immutable + self.mutable


# Order matters: the merge applies these top to bottom, so a row is never applied before something
# it is named after. Contacts and projects first, then facts, then the links between them.
TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        name="contacts",
        kind=refs.KIND_CONTACT,
        behaviour=OWNED,
        model_name="Contact",
        immutable=(text("uid"), moment("created_at")),
        mutable=(text("name"), text("color"), flag("is_self")),
        ref_attrs=("name",),
    ),
    TableSpec(
        name="projects",
        kind=refs.KIND_PROJECT,
        behaviour=OWNED,
        model_name="Project",
        immutable=(text("uid"), moment("created_at")),
        mutable=(
            text("name"),
            text("color"),
            day("start_date"),
            day("end_date"),
            money("budget"),
            text("status"),
            text("notes"),
            text("splitwise_group_id"),
        ),
        ref_attrs=("name",),
    ),
    TableSpec(
        name="rules",
        kind=refs.KIND_RULE,
        behaviour=OWNED,
        model_name="Rule",
        immutable=(text("uid"), moment("created_at")),
        mutable=(
            text("pattern"),
            text("match_field"),
            text("category"),
            text("merchant_clean"),
            text("source"),
            integer("priority"),
        ),
        ref_attrs=("uid",),
    ),
    TableSpec(
        name="subscriptions",
        kind=refs.KIND_SUBSCRIPTION,
        behaviour=OWNED,
        model_name="Subscription",
        immutable=(text("uid"), moment("created_at")),
        mutable=(
            text("merchant"),
            money("amount"),
            text("frequency"),
            text("source"),
            text("account_last4"),
            day("last_charged"),
            day("next_expected"),
            text("status"),
            blob("price_history"),
        ),
        ref_attrs=("uid",),
    ),
    TableSpec(
        name="transactions",
        kind=refs.KIND_TRANSACTION,
        behaviour=FACT,
        model_name="Transaction",
        # The money and the provider's description of it. A peer never rewrites these; if the row
        # exists locally, these are left exactly as ingested.
        immutable=(
            text("source"),
            text("source_id"),
            day("date"),
            day("authorized_date"),
            money("amount", attr="_amount"),
            text("currency"),
            text("merchant_raw"),
            text("original_description"),
            text("payment_channel"),
            text("account_last4"),
            text("plaid_account_id"),
            text("plaid_mask"),
            flag("pending"),
            text("pending_transaction_id"),
            text("origin"),
            moment("created_at"),
        ),
        # What the user changes about a transaction.
        mutable=(
            text("category"),
            text("category_source"),
            text("merchant_clean"),
            flag("is_recurring"),
            blob("tags"),
            text("notes"),
        ),
        ref_attrs=("source", "source_id"),
    ),
    TableSpec(
        name="transaction_projects",
        kind=refs.KIND_TRANSACTION_PROJECT,
        behaviour=LINK,
        model_name="TransactionProject",
        mutable=(text("description"),),
    ),
    TableSpec(
        name="project_members",
        kind=refs.KIND_PROJECT_MEMBER,
        behaviour=LINK,
        model_name="ProjectMember",
    ),
    TableSpec(
        name="transaction_splits",
        kind=refs.KIND_TRANSACTION_SPLIT,
        behaviour=LINK,
        model_name="TransactionSplit",
        mutable=(money("share_amount", attr="_share_amount"),),
    ),
    # The legacy per-project split table, superseded by `transaction_splits`. Synced only so the two
    # apps agree while it still holds rows; **delete this spec when the table is dropped**, along with
    # KIND_TRANSACTION_PROJECT_SPLIT and its ref helper.
    TableSpec(
        name="transaction_project_splits",
        kind=refs.KIND_TRANSACTION_PROJECT_SPLIT,
        behaviour=LINK,
        model_name="TransactionProjectSplit",
        mutable=(money("share_amount", attr="_share_amount"),),
    ),
    # --- provider facts -------------------------------------------------------------------------
    #
    # Observed or computed by whichever device ingested them, and never edited by a user, so every
    # field is immutable and there is no last-write-wins. Two devices that both derive the same row
    # produce byte-identical values under the same natural key, so the second one is simply seen as
    # already present.
    #
    # They travel rather than being recomputed per device because two of the three clients cannot
    # ingest: an iOS app runs neither Plaid nor PDF imports, and if these did not sync it would show
    # a wrong net worth until it reimplemented every derivation in Swift.
    TableSpec(
        name="payslips",
        kind=refs.KIND_PAYSLIP,
        behaviour=FACT,
        model_name="Payslip",
        immutable=(
            text("source"),
            text("signature"),
            text("employer"),
            day("pay_date"),
            day("pay_period_start"),
            day("pay_period_end"),
            text("currency"),
            money("gross", attr="_gross"),
            money("net", attr="_net"),
            money("total_taxes", attr="_total_taxes"),
            money("total_deductions", attr="_total_deductions"),
            text("filename"),
            moment("created_at"),
        ),
        ref_attrs=("source", "signature"),
    ),
    TableSpec(
        name="payslip_line_items",
        kind=refs.KIND_PAYSLIP_LINE_ITEM,
        behaviour=LINK,
        model_name="PayslipLineItem",
        # Named by (parent payslip reference, section, label): `payslip_id` is a local UUID and so
        # means nothing on another device.
        immutable=(text("section"), text("label")),
        mutable=(money("amount", attr="_amount"), money("ytd", attr="_ytd")),
    ),
    TableSpec(
        name="account_snapshots",
        kind=refs.KIND_ACCOUNT_SNAPSHOT,
        behaviour=FACT,
        model_name="AccountSnapshot",
        immutable=(
            text("source"),
            text("account_key"),
            text("account_name"),
            text("account_group"),
            text("connection_state"),
            money("current_value", attr="_current_value"),
            text("currency"),
            moment("synced_at"),
            moment("created_at"),
        ),
        # synced_at is part of the key: a snapshot *is* a reading at a moment, and two readings of
        # the same account on the same day are two facts, not a conflict. account_name joins the key
        # because 260 of 269 real rows have no account_key, and (source, synced_at) alone collides
        # for a provider that snapshots several accounts at once.
        ref_attrs=("source", "account_key", "account_name", "synced_at"),
        ref_nullable=("account_key", "account_name"),
    ),
    TableSpec(
        name="investment_holding_snapshots",
        kind=refs.KIND_INVESTMENT_HOLDING_SNAPSHOT,
        behaviour=FACT,
        model_name="InvestmentHoldingSnapshot",
        immutable=(
            text("source"),
            text("plaid_account_id"),
            text("account_name"),
            text("security_id"),
            text("ticker"),
            text("name"),
            money("quantity"),
            money("price"),
            money("value", attr="_value"),
            money("cost_basis", attr="_cost_basis"),
            text("currency"),
            text("type"),
            moment("synced_at"),
            moment("created_at"),
        ),
        ref_attrs=("source", "plaid_account_id", "security_id", "synced_at"),
        ref_nullable=("plaid_account_id", "security_id"),
    ),
    TableSpec(
        name="source_balance_history",
        kind=refs.KIND_SOURCE_BALANCE,
        behaviour=FACT,
        model_name="SourceBalanceHistory",
        immutable=(
            text("source_key"),
            text("account_key"),
            text("account_name"),
            text("source"),
            text("account_group"),
            day("date"),
            money("value", attr="_value"),
            text("currency"),
            text("provenance"),
            moment("created_at"),
        ),
        # provenance belongs in the key: the same account on the same day legitimately has both an
        # observed snapshot value and a ledger-derived one, and collapsing them would silently
        # replace an observation with a derivation. account_key is NULL on 7236 of 7245 real rows,
        # so it has to be a nullable part rather than a required one.
        ref_attrs=("source", "account_key", "date", "provenance"),
        ref_nullable=("account_key",),
    ),
    TableSpec(
        name="investment_period_facts",
        kind=refs.KIND_INVESTMENT_PERIOD_FACT,
        behaviour=FACT,
        model_name="InvestmentPeriodFact",
        immutable=(
            text("source_key"),
            text("source"),
            day("period_start"),
            day("period_end"),
            money("beginning_value", attr="_beginning_value"),
            money("ending_value", attr="_ending_value"),
            money("inflow", attr="_inflow"),
            money("market_gain", attr="_market_gain"),
            text("currency"),
            text("provenance"),
            moment("created_at"),
        ),
        ref_attrs=("source", "period_end"),
    ),
    TableSpec(
        name="account_activity",
        kind=refs.KIND_ACCOUNT_ACTIVITY,
        behaviour=FACT,
        model_name="AccountActivity",
        immutable=(
            text("source_id"),
            text("source_key"),
            text("source"),
            text("account_id"),
            text("account_last4"),
            day("date"),
            day("authorized_date"),
            money("amount", attr="_amount"),
            text("description"),
            text("merchant"),
            text("activity_type"),
            text("currency"),
            flag("pending"),
            text("pending_activity_id"),
        ),
        ref_attrs=("source", "source_id"),
    ),
    TableSpec(
        name="retirement_transactions",
        kind=refs.KIND_RETIREMENT_TRANSACTION,
        behaviour=FACT,
        model_name="RetirementTransaction",
        immutable=(
            text("source"),
            text("source_id"),
            day("date"),
            text("type"),
            text("contribution_source"),
            text("fund"),
            text("currency"),
            money("amount", attr="_amount"),
            money("units"),
            money("unit_price"),
            moment("created_at"),
        ),
        ref_attrs=("source", "source_id"),
    ),
    TableSpec(
        name="retirement_statements",
        kind=refs.KIND_RETIREMENT_STATEMENT,
        behaviour=FACT,
        model_name="RetirementStatement",
        immutable=(
            text("source"),
            text("plan_name"),
            day("period_start"),
            day("period_end"),
            text("currency"),
            money("beginning_balance", attr="_beginning_balance"),
            money("ending_balance", attr="_ending_balance"),
            money("employee_contributions", attr="_employee_contributions"),
            money("employer_contributions", attr="_employer_contributions"),
            money("market_change", attr="_market_change"),
            money("vested_balance", attr="_vested_balance"),
            money("rate_of_return"),
            moment("created_at"),
        ),
        ref_attrs=("source", "period_end"),
    ),
    TableSpec(
        name="contact_splitwise_links",
        kind=refs.KIND_CONTACT_SPLITWISE_LINK,
        behaviour=LINK,
        model_name="ContactSplitwiseLink",
        mutable=(text("splitwise_user_id"), text("display_name"), moment("linked_at")),
    ),
)

BY_NAME: dict[str, TableSpec] = {spec.name: spec for spec in TABLES}
BY_KIND: dict[str, TableSpec] = {spec.kind: spec for spec in TABLES}


def model_for(spec: TableSpec):
    import src.models as models

    return getattr(models, spec.model_name)
