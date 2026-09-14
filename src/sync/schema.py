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
            money("priority"),
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
    TableSpec(
        name="transaction_project_splits",
        kind=refs.KIND_TRANSACTION_PROJECT_SPLIT,
        behaviour=LINK,
        model_name="TransactionProjectSplit",
        mutable=(money("share_amount", attr="_share_amount"),),
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
