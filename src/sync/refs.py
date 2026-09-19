"""Natural references: how a row is named across devices.

A row's local primary key is useless for sync. `Transaction.id`, `Project.id` and `Contact.id` are
per-device UUIDs, so the same project is a different `id` on the Mac than on the phone, and a link
row's composite primary key -- which looks canonical -- is built out of those UUIDs and is therefore
not stable either.

So every synced row is named by a **natural reference** derived from content that both devices
already agree on:

| kind | reference |
| --- | --- |
| `transaction` | `(source, source_id)` -- already a unique index |
| `project`, `contact` | the name, which is already `unique` |
| `rule`, `subscription` | the minted `uid`, since neither has a natural key |
| link rows | their parents' references |

References are also what tombstones are keyed by, because a deletion has to be expressible after the
row is gone.

Encoded as compact JSON rather than a joined string: a project may legitimately be called `a|b`, and
a separator that can appear in the data is a parsing bug waiting to happen. JSON with fixed
separators is deterministic, so two devices encode the same reference identically.
"""
from __future__ import annotations

import json
from typing import Iterable

# Kind names are part of the wire format and are stored in `tombstones.kind`. Renaming one is a
# format change: an old peer's tombstones would stop matching.
KIND_TRANSACTION = "transaction"
KIND_PROJECT = "project"
KIND_CONTACT = "contact"
KIND_RULE = "rule"
KIND_SUBSCRIPTION = "subscription"
KIND_TRANSACTION_PROJECT = "transaction_project"
KIND_PROJECT_MEMBER = "project_member"
KIND_TRANSACTION_SPLIT = "transaction_split"
KIND_TRANSACTION_PROJECT_SPLIT = "transaction_project_split"
KIND_CONTACT_SPLITWISE_LINK = "contact_splitwise_link"
# Provider facts. Each is named by its own columns, so `TableSpec.ref_attrs` is the whole story and
# no resolver is needed -- see `ref_from_columns`.
KIND_PAYSLIP = "payslip"
KIND_PAYSLIP_LINE_ITEM = "payslip_line_item"
KIND_ACCOUNT_SNAPSHOT = "account_snapshot"
KIND_INVESTMENT_HOLDING_SNAPSHOT = "investment_holding_snapshot"
KIND_SOURCE_BALANCE = "source_balance"
KIND_INVESTMENT_PERIOD_FACT = "investment_period_fact"
KIND_ACCOUNT_ACTIVITY = "account_activity"
KIND_RETIREMENT_TRANSACTION = "retirement_transaction"
KIND_RETIREMENT_STATEMENT = "retirement_statement"

ALL_KINDS = (
    KIND_TRANSACTION,
    KIND_PROJECT,
    KIND_CONTACT,
    KIND_RULE,
    KIND_SUBSCRIPTION,
    KIND_TRANSACTION_PROJECT,
    KIND_PROJECT_MEMBER,
    KIND_TRANSACTION_SPLIT,
    KIND_TRANSACTION_PROJECT_SPLIT,
    KIND_CONTACT_SPLITWISE_LINK,
    KIND_PAYSLIP,
    KIND_PAYSLIP_LINE_ITEM,
    KIND_ACCOUNT_SNAPSHOT,
    KIND_INVESTMENT_HOLDING_SNAPSHOT,
    KIND_SOURCE_BALANCE,
    KIND_INVESTMENT_PERIOD_FACT,
    KIND_ACCOUNT_ACTIVITY,
    KIND_RETIREMENT_TRANSACTION,
    KIND_RETIREMENT_STATEMENT,
)


def encode(*parts: object) -> str:
    """Encode reference parts into one deterministic string."""
    return json.dumps([None if p is None else str(p) for p in parts], separators=(",", ":"))


def decode(ref: str) -> list[str | None]:
    value = json.loads(ref)
    if not isinstance(value, list):
        raise ValueError(f"not a reference: {ref!r}")
    return value


def transaction_ref(source: str, source_id: str) -> str:
    return encode(source, source_id)


def project_ref(name: str) -> str:
    return encode(name)


def contact_ref(name: str) -> str:
    return encode(name)


def uid_ref(uid: str) -> str:
    return encode(uid)


def transaction_project_ref(txn_ref: str, project_name: str) -> str:
    return encode(txn_ref, project_name)


def project_member_ref(project_name: str, contact_name: str) -> str:
    return encode(project_name, contact_name)


def transaction_split_ref(txn_ref: str, contact_name: str) -> str:
    return encode(txn_ref, contact_name)


def transaction_project_split_ref(txn_ref: str, project_name: str, contact_name: str) -> str:
    return encode(txn_ref, project_name, contact_name)


def contact_splitwise_link_ref(contact_name: str) -> str:
    return encode(contact_name)


def payslip_ref(source: str, signature: str) -> str:
    return encode(source, signature)


def payslip_line_item_ref(parent_ref: str, section: str, label: str) -> str:
    return encode(parent_ref, section, label)


def spec_for_instance(instance):
    """The `TableSpec` whose model this instance is, or None.

    Cached by class: a payload build or merge asks this per row, and the spec list is static.
    """
    from src.sync import schema

    cache = spec_for_instance.__dict__.setdefault("_by_class", {})
    cls = type(instance)
    if cls not in cache:
        found = None
        for spec in schema.TABLES:
            try:
                if isinstance(instance, schema.model_for(spec)):
                    found = spec
                    break
            except Exception:
                continue
        cache[cls] = found
    return cache[cls]


def ref_from_columns(
    instance, attrs: tuple[str, ...], nullable: tuple[str, ...] = ()
) -> str | None:
    """Build a reference from the row's own columns.

    This is what `TableSpec.ref_attrs` means, and driving both naming and lookup from it is the
    point: the field existed but was never read, so the reference a row was *named* by and the
    columns it was *found* by were written out twice, in two modules, free to disagree.

    None if any part is missing -- an unnamed row is a real gap, reported by `unresolvable`, not
    something to paper over with an empty string that two devices would both claim.
    """
    parts = []
    for attr in attrs:
        value = getattr(instance, attr, None)
        if value is None and attr not in nullable:
            return None
        parts.append(value)
    return encode(*parts)


class RefResolver:
    """Computes references, caching the local-id-to-natural-name lookups.

    A link row only stores its parents' local ids, so naming it requires reading those parents. That
    is one query per parent per row without a cache, and a merge or a payload build walks thousands
    of rows.

    The cache is per-instance and short-lived on purpose: it is only valid for as long as nothing
    renames a project underneath it.
    """

    def __init__(self, db):
        self.db = db
        self._project_names: dict[str, str | None] = {}
        self._contact_names: dict[str, str | None] = {}
        self._transaction_refs: dict[str, str | None] = {}
        self._payslip_refs: dict[str, str | None] = {}

    # -- local id -> natural name -------------------------------------------------------------

    def project_name(self, project_id: str | None) -> str | None:
        if project_id is None:
            return None
        if project_id not in self._project_names:
            from src.models import Project

            row = self.db.query(Project.name).filter(Project.id == project_id).first()
            self._project_names[project_id] = row[0] if row else None
        return self._project_names[project_id]

    def contact_name(self, contact_id: str | None) -> str | None:
        if contact_id is None:
            return None
        if contact_id not in self._contact_names:
            from src.models import Contact

            row = self.db.query(Contact.name).filter(Contact.id == contact_id).first()
            self._contact_names[contact_id] = row[0] if row else None
        return self._contact_names[contact_id]

    def transaction_ref(self, transaction_id: str | None) -> str | None:
        if transaction_id is None:
            return None
        if transaction_id not in self._transaction_refs:
            from src.models import Transaction

            row = (
                self.db.query(Transaction.source, Transaction.source_id)
                .filter(Transaction.id == transaction_id)
                .first()
            )
            self._transaction_refs[transaction_id] = (
                transaction_ref(row[0], row[1]) if row else None
            )
        return self._transaction_refs[transaction_id]

    def payslip_ref(self, payslip_id: str | None) -> str | None:
        if payslip_id is None:
            return None
        if payslip_id not in self._payslip_refs:
            from src.models import Payslip

            row = (
                self.db.query(Payslip.source, Payslip.signature)
                .filter(Payslip.id == payslip_id)
                .first()
            )
            self._payslip_refs[payslip_id] = payslip_ref(row[0], row[1]) if row else None
        return self._payslip_refs[payslip_id]

    # -- instance -> (kind, ref) --------------------------------------------------------------

    def describe(self, instance) -> tuple[str, str] | None:
        """The `(kind, ref)` for a model instance, or None if it is not a synced table.

        Returns None rather than raising for an unsynced model: this is called from a flush hook
        that sees every deleted object in the session, most of which are not our business.

        Also returns None when a reference cannot be built -- a link whose parent row is already
        gone, or a project with no name. That is a real gap, not a silent one: `unresolvable` counts
        it so the caller can report it.
        """
        from src.models import (
            Contact,
            ContactSplitwiseLink,
            Project,
            ProjectMember,
            Rule,
            Subscription,
            Transaction,
            TransactionProject,
            TransactionProjectSplit,
            TransactionSplit,
        )

        # Own-column tables -- transactions, projects, contacts, rules, subscriptions and every
        # provider-fact table -- are named entirely by `spec.ref_attrs`. Link rows are named by
        # their parents and are handled below, because that needs a lookup rather than a getattr.
        spec = spec_for_instance(instance)
        if spec is not None and spec.ref_attrs:
            ref = ref_from_columns(instance, spec.ref_attrs, spec.ref_nullable)
            return (spec.kind, ref) if ref is not None else None

        from src.models import PayslipLineItem

        if isinstance(instance, PayslipLineItem):
            parent = self.payslip_ref(instance.payslip_id)
            if parent is None or not instance.section or not instance.label:
                return None
            return KIND_PAYSLIP_LINE_ITEM, payslip_line_item_ref(
                parent, instance.section, instance.label
            )

        if isinstance(instance, TransactionProject):
            txn = self.transaction_ref(instance.transaction_id)
            project = self.project_name(instance.project_id)
            if txn is None or project is None:
                return None
            return KIND_TRANSACTION_PROJECT, transaction_project_ref(txn, project)

        if isinstance(instance, ProjectMember):
            project = self.project_name(instance.project_id)
            contact = self.contact_name(instance.contact_id)
            if project is None or contact is None:
                return None
            return KIND_PROJECT_MEMBER, project_member_ref(project, contact)

        if isinstance(instance, TransactionSplit):
            txn = self.transaction_ref(instance.transaction_id)
            contact = self.contact_name(instance.contact_id)
            if txn is None or contact is None:
                return None
            return KIND_TRANSACTION_SPLIT, transaction_split_ref(txn, contact)

        if isinstance(instance, TransactionProjectSplit):
            txn = self.transaction_ref(instance.transaction_id)
            project = self.project_name(instance.project_id)
            contact = self.contact_name(instance.contact_id)
            if txn is None or project is None or contact is None:
                return None
            return (
                KIND_TRANSACTION_PROJECT_SPLIT,
                transaction_project_split_ref(txn, project, contact),
            )

        if isinstance(instance, ContactSplitwiseLink):
            contact = self.contact_name(instance.contact_id)
            if contact is None:
                return None
            return KIND_CONTACT_SPLITWISE_LINK, contact_splitwise_link_ref(contact)

        return None

    def describe_all(self, instances: Iterable) -> tuple[list[tuple[str, str]], int]:
        """`(described, unresolvable_count)` for a batch."""
        described: list[tuple[str, str]] = []
        unresolvable = 0
        for instance in instances:
            result = self.describe(instance)
            if result is None:
                # Only counted when the model *is* synced; an unrelated model is not a gap.
                if _is_synced_model(instance):
                    unresolvable += 1
                continue
            described.append(result)
        return described, unresolvable


def _is_synced_model(instance) -> bool:
    """Whether this model is one sync is responsible for.

    Driven off the spec list so it cannot fall behind it: when this was a hand-written tuple, adding
    a table meant remembering to add it here too, and forgetting would have hidden unnameable rows
    from `unresolvable` -- silently dropping them from payloads, which is how 77 orphans went
    unnoticed once already.
    """
    return spec_for_instance(instance) is not None
