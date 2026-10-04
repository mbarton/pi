"""Django ORM implementation of the pi-durable `Storage` contract (packages/durable/src/types.ts).

This is a port of `SqliteStorage` (packages/durable/src/storage/sqlite/storage.ts). Method names, arguments, results,
and error messages follow the TypeScript interface so the HTTP client can expose them unchanged. Records are plain
parsed JSON (dicts using the TypeScript field names).

`DurableStorage` can also be used directly from Django code, for example to render a transcript.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from django.db.models import Q

from . import delta
from .models import (
    Conversation,
    Document,
    DocumentRevision,
    Entry,
    RecordId,
    Store,
    Submission,
    Task,
)

Json = Any
MAX_SAFE_INTEGER = 2**53 - 1
CURRENT = "current"


class StorageError(Exception):
    """The batch or read is invalid. Mirrors a plain `Error` thrown by the TypeScript backends."""


class StorageRejected(StorageError):
    """The batch was rejected before any durable effect; the owning Session may continue (`StorageRejected` in TS)."""


class _MissingBase(Exception):
    pass


def _encode(value: Json) -> str:
    # ASCII escapes keep lone surrogates and NUL storable in every database.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _istr(value: str) -> str:
    """Indexed string column value. See models.py."""
    return json.dumps(value, ensure_ascii=True)


def _cursor_after(cursor: dict[str, Json] | None) -> int | None:
    if cursor is None:
        return None
    after = cursor.get("after")
    if after is None:
        return None
    if isinstance(after, bool) or not isinstance(after, int) or abs(after) > MAX_SAFE_INTEGER:
        raise StorageError("Invalid storage cursor")
    return after


def _page(values: list[dict[str, Json]], limit: int) -> dict[str, Json]:
    items = values[:limit]
    if len(values) <= limit:
        return {"items": items}
    return {"items": items, "next": {"after": items[-1]["id"]}}


def _scope_columns(scope: dict[str, Json]) -> tuple[str, int]:
    kind = scope["kind"]
    if kind == "session":
        return "session", 0
    if kind == "conversation":
        return "conversation", scope["conversationId"]
    if kind == "task":
        return "task", scope["taskId"]
    raise StorageError(f"Unknown document scope: {kind}")


def _address_filter(address: dict[str, Json]) -> Q:
    scope_kind, owner_id = _scope_columns(address["scope"])
    key = address.get("key")
    return Q(
        kind=_istr(address["kind"]),
        scope_kind=scope_kind,
        owner_id=owner_id,
        family=key is not None,
        key_value=_istr(key if key is not None else ""),
    )


def _address_key(address: dict[str, Json]) -> str:
    scope_kind, owner_id = _scope_columns(address["scope"])
    return json.dumps([address["kind"], scope_kind, owner_id, address.get("key")])


def _alive_filter(at: Json) -> Q:
    if at == CURRENT:
        return Q(retired_at__isnull=True)
    return Q(created_at__lte=at) & (Q(retired_at__isnull=True) | Q(retired_at__gt=at))


def _is_alive_at(record: dict[str, Json], at: Json) -> bool:
    retired = record.get("retiredAt")
    if at == CURRENT:
        return retired is None
    return record["createdAt"] <= at and (retired is None or at < retired)


def _is_current_only(record: dict[str, Json]) -> bool:
    return record["scope"]["kind"] != "conversation" or record.get("history") == "latest"


@dataclass
class _DocumentAction:
    create: dict[str, Json] | None = None
    copy: dict[str, Json] | None = None
    content: dict[str, Json] | None = None
    retire: bool = False


@dataclass
class DurableStorage:
    """Storage operations of one `Store`. Stateless apart from the store reference; safe to create per request."""

    store: Store
    _conversations: dict[int, dict[str, Json] | None] = field(default_factory=dict, repr=False)

    # ─── IDs and commits ────────────────────────────────────────────────────────

    def reserve_ids(self, count: int) -> dict[str, int]:
        """Reserve `[start, end)` from the ID namespace. Clients mint from the block; unused IDs become gaps."""
        if count < 1:
            raise StorageError("count must be positive")
        with transaction.atomic():
            store = Store.objects.select_for_update().get(pk=self.store.pk)
            start = store.next_id
            if start > MAX_SAFE_INTEGER:
                raise StorageError("ID space is exhausted")
            end = min(start + count, MAX_SAFE_INTEGER + 1)
            store.next_id = end
            store.save(update_fields=["next_id"])
        return {"start": start, "end": end}

    def commit(self, writes: list[dict[str, Json]]) -> int:
        """Atomically persist one batch and return its sequence."""
        actions = self._prepare_document_actions(writes)
        with transaction.atomic():
            # The row lock serializes commits of one store and makes `next_seq` strictly increasing.
            store = Store.objects.select_for_update().get(pk=self.store.pk)
            seq = store.next_seq
            self._check_global_ids(writes)
            self._check_document_actions(actions)
            for write in writes:
                self._apply_table_write(write, seq)
            self._apply_document_actions(actions, seq)
            candidate = store.next_id
            for write in writes:
                record_id = self._write_id(write)
                if record_id is not None:
                    candidate = max(candidate, record_id + 1)
            store.next_id = candidate
            store.next_seq = seq + 1
            store.save(update_fields=["next_id", "next_seq"])
        return seq

    # ─── Conversations and entries ──────────────────────────────────────────────

    def conversation(self, conversation_id: int) -> dict[str, Json] | None:
        if conversation_id not in self._conversations:
            # Conversations are immutable once created; a miss is not cached because it may be created later.
            record = (
                Conversation.objects.filter(store=self.store, record_id=conversation_id)
                .values_list("record", flat=True)
                .first()
            )
            if record is None:
                return None
            self._conversations[conversation_id] = json.loads(record)
        return self._conversations[conversation_id]

    def scan_conversations(self, query: dict[str, Json], limit: int, cursor: dict[str, Json] | None) -> dict[str, Json]:
        rows = Conversation.objects.filter(store=self.store, record_id__gt=_cursor_after(cursor) or -1)
        if query.get("ownerConversationId") is not None:
            rows = rows.filter(owner_conversation_id=query["ownerConversationId"])
        if query.get("ownerTaskId") is not None:
            rows = rows.filter(owner_task_id=query["ownerTaskId"])
        records = rows.order_by("record_id").values_list("record", flat=True)[: limit + 1]
        return _page([json.loads(record) for record in records], limit)

    def _known_conversation(self, conversation_id: int) -> dict[str, Json]:
        conversation = self.conversation(conversation_id)
        if conversation is None:
            raise StorageError(f"Unknown conversation: {conversation_id}")
        return conversation

    def entry(self, entry_id: int, conversation_id: int | None = None) -> dict[str, Json] | None:
        """Global lookup, or only when visible through `conversation_id`'s fork ancestry."""
        conversation = None if conversation_id is None else self._known_conversation(conversation_id)
        row = Entry.objects.filter(store=self.store, record_id=entry_id).values_list("record", "commit_seq").first()
        if row is None:
            return None
        entry = json.loads(row[0])
        if conversation is not None:
            upper: int | None = None
            while conversation["id"] != entry["conversationId"]:
                parent = conversation.get("parent")
                if parent is None:
                    return None
                upper = parent["at"] if upper is None else min(upper, parent["at"])
                conversation = self._known_conversation(parent["conversationId"])
            if upper is not None and entry["id"] > upper:
                return None
        return {"entry": entry, "commitSeq": row[1]}

    def find_latest_head_marker(self, conversation_id: int, at_or_before: int | None) -> dict[str, Json] | None:
        conversation = self._known_conversation(conversation_id)
        upper = at_or_before
        while True:
            rows = Entry.objects.filter(store=self.store, conversation_id=conversation["id"], head__isnull=False)
            if upper is not None:
                rows = rows.filter(record_id__lte=upper)
            record = rows.order_by("-record_id").values_list("record", flat=True).first()
            if record is not None:
                return json.loads(record)
            parent = conversation.get("parent")
            if parent is None:
                return None
            upper = parent["at"] if upper is None else min(upper, parent["at"])
            conversation = self._known_conversation(parent["conversationId"])

    def scan_entries(self, query: dict[str, Json], limit: int, cursor: dict[str, Json] | None) -> dict[str, Json]:
        """Newest-first scan of the visible range, continuing into fork parents."""
        conversation = self._known_conversation(query["conversationId"])
        minimum = query.get("minEntryId")
        upper = query.get("maxEntryId")
        after = _cursor_after(cursor)
        if after is not None:
            upper = after - 1 if upper is None else min(upper, after - 1)
        values: list[dict[str, Json]] = []
        while True:
            rows = Entry.objects.filter(store=self.store, conversation_id=conversation["id"])
            if minimum is not None:
                rows = rows.filter(record_id__gte=minimum)
            if upper is not None:
                rows = rows.filter(record_id__lte=upper)
            records = rows.order_by("-record_id").values_list("record", flat=True)[: limit + 1 - len(values)]
            values.extend(json.loads(record) for record in records)
            parent = conversation.get("parent")
            if len(values) > limit or parent is None:
                break
            upper = parent["at"] if upper is None else min(upper, parent["at"])
            if minimum is not None and upper < minimum:
                break
            conversation = self._known_conversation(parent["conversationId"])
        return _page(values, limit)

    # ─── Tasks and submissions ──────────────────────────────────────────────────

    def task(self, task_id: int) -> dict[str, Json] | None:
        record = Task.objects.filter(store=self.store, record_id=task_id).values_list("record", flat=True).first()
        return None if record is None else json.loads(record)

    def scan_tasks(self, query: dict[str, Json], limit: int, cursor: dict[str, Json] | None) -> dict[str, Json]:
        rows = Task.objects.filter(store=self.store, record_id__gt=_cursor_after(cursor) or -1)
        if query.get("conversationId") is not None:
            rows = rows.filter(conversation_id=query["conversationId"])
        if query.get("kind") is not None:
            rows = rows.filter(kind=_istr(query["kind"]))
        if query.get("status") is not None:
            rows = rows.filter(status=query["status"])
        if query.get("abortRequested") is not None:
            rows = rows.filter(abort_requested=query["abortRequested"])
        if query.get("background") is not None:
            rows = rows.filter(background=query["background"])
        records = rows.order_by("record_id").values_list("record", flat=True)[: limit + 1]
        return _page([json.loads(record) for record in records], limit)

    def submission(self, submission_id: int) -> dict[str, Json] | None:
        record = (
            Submission.objects.filter(store=self.store, record_id=submission_id)
            .values_list("record", flat=True)
            .first()
        )
        return None if record is None else json.loads(record)

    def scan_submissions(self, query: dict[str, Json], limit: int, cursor: dict[str, Json] | None) -> dict[str, Json]:
        rows = Submission.objects.filter(store=self.store, record_id__gt=_cursor_after(cursor) or -1)
        if query.get("conversationId") is not None:
            rows = rows.filter(conversation_id=query["conversationId"])
        if query.get("status") is not None:
            rows = rows.filter(status=query["status"])
        records = rows.order_by("record_id").values_list("record", flat=True)[: limit + 1]
        return _page([json.loads(record) for record in records], limit)

    def submission_by_request(self, conversation_id: int, request_id: str) -> dict[str, Json] | None:
        record = (
            Submission.objects.filter(store=self.store, conversation_id=conversation_id, request_id=_istr(request_id))
            .values_list("record", flat=True)
            .first()
        )
        return None if record is None else json.loads(record)

    # ─── Documents ──────────────────────────────────────────────────────────────

    def find_document(self, address: dict[str, Json], at: Json) -> dict[str, Json] | None:
        record = (
            Document.objects.filter(_address_filter(address), _alive_filter(at), store=self.store)
            .order_by("-created_at")
            .values_list("record", flat=True)
            .first()
        )
        return None if record is None else json.loads(record)

    def document(self, document_id: int, at: Json) -> dict[str, Json] | None:
        while True:
            row = Document.objects.filter(store=self.store, record_id=document_id).values_list("pk", "record").first()
            if row is None:
                return None
            try:
                return self._materialize(row[0], json.loads(row[1]), at)
            except _MissingBase:
                # A commit may have retired the document between the two reads. Retry when the record changed.
                refreshed = Document.objects.filter(pk=row[0]).values_list("record", flat=True).first()
                if refreshed == row[1]:
                    raise StorageError(f"Document {document_id} is missing a required base") from None

    def scan_documents(self, query: dict[str, Json], limit: int, cursor: dict[str, Json] | None) -> dict[str, Json]:
        scope_kind, owner_id = _scope_columns(query["scope"])
        rows = Document.objects.filter(
            _alive_filter(query["at"]),
            store=self.store,
            scope_kind=scope_kind,
            owner_id=owner_id,
            record_id__gt=_cursor_after(cursor) or -1,
        )
        if query.get("kind") is not None:
            rows = rows.filter(kind=_istr(query["kind"]))
        records = rows.order_by("record_id").values_list("record", flat=True)[: limit + 1]
        return _page([json.loads(record) for record in records], limit)

    def _materialize(self, pk: int, record: dict[str, Json], at: Json) -> dict[str, Json] | None:
        """Replay deltas after the newest base at or below `at`; None when the incarnation is not alive at `at`."""
        document_id = record["id"]
        if at != CURRENT and _is_current_only(record):
            raise StorageError(f"Document {document_id} does not retain historical content")
        if not _is_alive_at(record, at):
            return None
        upper = MAX_SAFE_INTEGER if at == CURRENT else at
        newest_base = (
            DocumentRevision.objects.filter(document_id=pk, kind=DocumentRevision.Kind.BASE, seq__lte=upper)
            .order_by("-seq")
            .values("seq")[:1]
        )
        # One statement reads the base and its tail, so a concurrent commit cannot split them.
        chain = list(
            DocumentRevision.objects.filter(document_id=pk, seq__lte=upper, seq__gte=newest_base)
            .order_by("seq")
            .values_list("seq", "kind", "version", "content")
        )
        if not chain or chain[0][1] != DocumentRevision.Kind.BASE:
            raise _MissingBase
        _, _, version, content = chain[0]
        value = json.loads(content)
        for _seq, kind, revision_version, ops in chain[1:]:
            if kind != DocumentRevision.Kind.DELTA or revision_version != version:
                raise StorageError(f"Document {document_id} crosses a stored version boundary without a base")
            value = delta.apply(value, json.loads(ops))
        return {"record": record, "version": version, "value": value, "deltasSinceBase": len(chain) - 1}

    # ─── Commit internals ───────────────────────────────────────────────────────

    @staticmethod
    def _write_id(write: dict[str, Json]) -> int | None:
        kind = write["type"]
        if kind in ("conversation", "entry", "task", "submission"):
            return write["value"]["id"]
        if kind in ("document.create", "document.copy"):
            return write["record"]["id"]
        return None

    def _check_global_ids(self, writes: list[dict[str, Json]]) -> None:
        claimed: dict[int, str] = {}
        for write in writes:
            if write["type"] in ("document.change", "document.retire"):
                continue
            is_document = write["type"] in ("document.create", "document.copy")
            table = "document" if is_document else write["type"]
            record_id = write["record"]["id"] if is_document else write["value"]["id"]
            existing = (
                RecordId.objects.filter(store=self.store, record_id=record_id)
                .values_list("record_type", flat=True)
                .first()
            )
            earlier = claimed.get(record_id)
            if table in ("conversation", "entry", "document"):
                if existing is not None:
                    raise StorageError(f"ID {record_id} already belongs to {existing}")
                if earlier is not None:
                    raise StorageError(f"ID {record_id} is written more than once")
            else:
                if existing is not None and existing != table:
                    raise StorageError(f"ID {record_id} already belongs to {existing}")
                if earlier is not None and earlier != table:
                    raise StorageError(f"ID {record_id} is written as two record types")
            claimed[record_id] = table

    @staticmethod
    def _prepare_document_actions(writes: list[dict[str, Json]]) -> dict[int, _DocumentAction]:
        actions: dict[int, _DocumentAction] = {}
        for write in writes:
            kind = write["type"]
            if not kind.startswith("document."):
                continue
            document_id = write["record"]["id"] if kind in ("document.create", "document.copy") else write["id"]
            action = actions.setdefault(document_id, _DocumentAction())
            if kind in ("document.create", "document.copy"):
                if action.create is not None or action.content is not None or action.copy is not None:
                    raise StorageError(f"Document {document_id} has more than one content command")
                action.create = write["record"]
                if kind == "document.create":
                    action.content = write["content"]
                else:
                    action.copy = write["source"]
            elif kind == "document.change":
                if action.content is not None or action.copy is not None:
                    raise StorageError(f"Document {document_id} has more than one content command")
                action.content = write["content"]
            elif kind == "document.retire":
                if action.retire:
                    raise StorageError(f"Document {document_id} is retired more than once")
                action.retire = True
            else:
                raise StorageError(f"Unknown storage write: {kind}")
        return actions

    def _document_row(self, document_id: int) -> Document | None:
        return Document.objects.filter(store=self.store, record_id=document_id).first()

    def _check_document_actions(self, actions: dict[int, _DocumentAction]) -> None:
        live_counts: dict[str, int] = {}
        for document_id, action in actions.items():
            if action.copy is not None and action.copy["id"] in actions:
                raise StorageRejected(f"Document copy {document_id} source is changed in the copy batch")
            row = self._document_row(document_id)
            existing = None if row is None else json.loads(row.record)
            if action.create is None and existing is None:
                raise StorageError(f"Unknown document: {document_id}")
            if action.create is not None and existing is not None:
                raise StorageError(f"Document {document_id} already exists")
            if existing is not None and existing.get("retiredAt") is not None:
                raise StorageError(f"Document {document_id} is retired")
            if action.content is not None and action.content["kind"] == "delta":
                previous = (
                    DocumentRevision.objects.filter(document=row).order_by("-seq").values_list("version", flat=True)
                    if row is not None
                    else []
                )
                if not previous:
                    raise StorageError(f"Document {document_id} delta has no base")
                if previous[0] != action.content["version"]:
                    raise StorageError(f"Document {document_id} version transition requires a base")
            record = action.create if action.create is not None else existing
            key = _address_key(record)
            live = live_counts.get(key)
            if live is None:
                live = int(
                    Document.objects.filter(_address_filter(record), store=self.store, retired_at__isnull=True).exists()
                )
            if action.retire and existing is not None:
                live -= 1
            if action.create is not None and not action.retire:
                live += 1
            live_counts[key] = live
        if any(live > 1 for live in live_counts.values()):
            raise StorageError("Document address already has a current incarnation")

    def _claim_id(self, record_id: int, record_type: str) -> None:
        RecordId.objects.get_or_create(store=self.store, record_id=record_id, defaults={"record_type": record_type})

    def _apply_table_write(self, write: dict[str, Json], seq: int) -> None:
        kind = write["type"]
        if kind.startswith("document."):
            return
        value = write["value"]
        self._claim_id(value["id"], kind)
        if kind == "conversation":
            owner = value.get("owner") or {}
            Conversation.objects.create(
                store=self.store,
                record_id=value["id"],
                owner_conversation_id=owner.get("conversationId"),
                owner_task_id=owner.get("taskId"),
                record=_encode(value),
            )
        elif kind == "entry":
            Entry.objects.create(
                store=self.store,
                record_id=value["id"],
                conversation_id=value["conversationId"],
                head=value.get("head"),
                commit_seq=seq,
                record=_encode(value),
            )
        elif kind == "task":
            Task.objects.update_or_create(
                store=self.store,
                record_id=value["id"],
                defaults={
                    "conversation_id": value["conversationId"],
                    "kind": _istr(value["kind"]),
                    "status": value["state"]["status"],
                    "abort_requested": bool(value["abortRequested"]),
                    "background": bool(value["background"]),
                    "record": _encode(value),
                },
            )
        elif kind == "submission":
            request_id = value.get("requestId")
            Submission.objects.update_or_create(
                store=self.store,
                record_id=value["id"],
                defaults={
                    "conversation_id": value["conversationId"],
                    "request_id": None if request_id is None else _istr(request_id),
                    "status": value["status"],
                    "record": _encode(value),
                },
            )
        else:
            raise StorageError(f"Unknown storage write: {kind}")

    def _apply_document_actions(self, actions: dict[int, _DocumentAction], seq: int) -> None:
        for document_id, action in actions.items():
            content = action.content
            if action.copy is not None:
                content = self._copy_content(document_id, action)

            if action.create is not None:
                record = {**action.create, "createdAt": seq}
                if action.retire:
                    record["retiredAt"] = seq
                scope_kind, owner_id = _scope_columns(record["scope"])
                key = record.get("key")
                self._claim_id(document_id, "document")
                row = Document.objects.create(
                    store=self.store,
                    record_id=document_id,
                    kind=_istr(record["kind"]),
                    family=key is not None,
                    key_value=_istr(key if key is not None else ""),
                    scope_kind=scope_kind,
                    owner_id=owner_id,
                    created_at=seq,
                    retired_at=seq if action.retire else None,
                    record=_encode(record),
                )
            else:
                row = self._document_row(document_id)
                assert row is not None
                record = json.loads(row.record)

            if content is not None:
                if content["kind"] == "base" and _is_current_only(record):
                    row.revisions.all().delete()
                DocumentRevision.objects.create(
                    document=row,
                    seq=seq,
                    kind=content["kind"],
                    version=content["version"],
                    content=_encode(content["value"] if content["kind"] == "base" else content["ops"]),
                )

            if action.retire:
                if action.create is None:
                    record = {**record, "retiredAt": seq}
                    row.retired_at = seq
                    row.record = _encode(record)
                    row.save(update_fields=["retired_at", "record"])
                if _is_current_only(record):
                    row.revisions.all().delete()

    def _copy_content(self, document_id: int, action: _DocumentAction) -> dict[str, Json]:
        source = action.copy
        create = action.create
        assert source is not None and create is not None
        try:
            row = self._document_row(source["id"])
            stored = None if row is None else self._materialize(row.pk, json.loads(row.record), source["at"])
            if stored is None:
                raise StorageError(f"Fork source document {source['id']} cannot be read")
            record = stored["record"]
            if (
                record["scope"]["kind"] != "conversation"
                or create["scope"]["kind"] != "conversation"
                or record["kind"] != create["kind"]
                or record.get("key") != create.get("key")
                or record.get("history") != create.get("history")
                or record.get("fork") != create.get("fork")
            ):
                raise StorageError(f"Fork source document {source['id']} does not match the copied record")
        except StorageRejected:
            raise
        except (StorageError, _MissingBase, delta.DeltaError) as error:
            raise StorageRejected(f"Document copy {document_id} was rejected") from error
        return {"kind": "base", "version": stored["version"], "value": stored["value"]}
