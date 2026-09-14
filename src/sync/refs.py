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

        if isinstance(instance, Transaction):
            if instance.source is None or instance.source_id is None:
                return None
            return KIND_TRANSACTION, transaction_ref(instance.source, instance.source_id)

        if isinstance(instance, Project):
            return (KIND_PROJECT, project_ref(instance.name)) if instance.name else None

        if isinstance(instance, Contact):
            return (KIND_CONTACT, contact_ref(instance.name)) if instance.name else None

        if isinstance(instance, Rule):
            return (KIND_RULE, uid_ref(instance.uid)) if instance.uid else None

        if isinstance(instance, Subscription):
            return (KIND_SUBSCRIPTION, uid_ref(instance.uid)) if instance.uid else None

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

    return isinstance(
        instance,
        (
            Transaction,
            Project,
            Contact,
            Rule,
            Subscription,
            TransactionProject,
            ProjectMember,
            TransactionSplit,
            TransactionProjectSplit,
            ContactSplitwiseLink,
        ),
    )
