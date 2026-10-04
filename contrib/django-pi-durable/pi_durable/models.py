"""Tables for pi-durable `Storage`, mirroring packages/durable/src/storage/sqlite/migrations.ts.

One `Store` holds the records of one durable Session. Record IDs (`record_id`) are the Session-global numbers from
`Storage.mintId()`; they are unique per store, not per table, so several Sessions can share one database.

Full records are stored as JSON text in `record`, not `JSONField`: PostgreSQL `jsonb` reorders object keys and rejects
`\\u0000`, and document values must round-trip exactly. Indexed string columns (`kind`, `key_value`, `request_id`) hold
JSON-encoded ASCII strings for the same reason, as the SQLite backend does.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Store(models.Model):
    """Durable namespace of one pi Session: ID allocation, commit sequence, and every record."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="pi_durable_stores",
    )
    label = models.CharField(max_length=200, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    # ID 1 is reserved for the root conversation.
    next_id = models.BigIntegerField(default=2)
    next_seq = models.BigIntegerField(default=1)

    def __str__(self) -> str:
        return self.label or str(self.id)


class RecordId(models.Model):
    """Claims one number of the store-global ID namespace for one record type."""

    class Type(models.TextChoices):
        CONVERSATION = "conversation"
        ENTRY = "entry"
        TASK = "task"
        SUBMISSION = "submission"
        DOCUMENT = "document"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="+")
    record_id = models.BigIntegerField()
    record_type = models.CharField(max_length=16, choices=Type.choices)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "record_id"], name="pi_durable_record_id_unique")]


class Conversation(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="conversations")
    record_id = models.BigIntegerField()
    owner_conversation_id = models.BigIntegerField(null=True)
    owner_task_id = models.BigIntegerField(null=True)
    record = models.TextField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "record_id"], name="pi_durable_conversation_unique")]
        indexes = [
            models.Index(fields=["store", "owner_conversation_id", "record_id"], name="pi_durable_conv_by_owner_conv"),
            models.Index(fields=["store", "owner_task_id", "record_id"], name="pi_durable_conv_by_owner_task"),
        ]


class Entry(models.Model):
    """Immutable transcript entry. Visibility through forks is resolved by walking `ConversationRecord.parent`."""

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="entries")
    record_id = models.BigIntegerField()
    conversation_id = models.BigIntegerField()
    head = models.BigIntegerField(null=True)
    commit_seq = models.BigIntegerField()
    record = models.TextField()

    class Meta:
        verbose_name_plural = "entries"
        constraints = [models.UniqueConstraint(fields=["store", "record_id"], name="pi_durable_entry_unique")]
        indexes = [
            models.Index(fields=["store", "conversation_id", "-record_id"], name="pi_durable_entry_by_conv"),
            models.Index(
                fields=["store", "conversation_id", "-record_id"],
                condition=models.Q(head__isnull=False),
                name="pi_durable_entry_heads",
            ),
        ]


class Task(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        WAITING = "waiting"
        COMPLETING = "completing"
        TERMINAL = "terminal"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="tasks")
    record_id = models.BigIntegerField()
    conversation_id = models.BigIntegerField()
    kind = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices)
    abort_requested = models.BooleanField()
    background = models.BooleanField()
    record = models.TextField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "record_id"], name="pi_durable_task_unique")]
        indexes = [
            models.Index(fields=["store", "status", "record_id"], name="pi_durable_task_by_status"),
            models.Index(fields=["store", "conversation_id", "record_id"], name="pi_durable_task_by_conv"),
            models.Index(fields=["store", "kind", "record_id"], name="pi_durable_task_by_kind"),
        ]


class Submission(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued"
        PLACED = "placed"
        DONE = "done"
        UNANSWERED = "unanswered"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="submissions")
    record_id = models.BigIntegerField()
    conversation_id = models.BigIntegerField()
    request_id = models.TextField(null=True)
    status = models.CharField(max_length=16, choices=Status.choices)
    record = models.TextField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "record_id"], name="pi_durable_submission_unique")]
        indexes = [
            models.Index(fields=["store", "conversation_id", "request_id"], name="pi_durable_sub_by_request"),
            models.Index(fields=["store", "conversation_id", "record_id"], name="pi_durable_sub_by_conv"),
            models.Index(fields=["store", "status", "record_id"], name="pi_durable_sub_by_status"),
        ]


class Document(models.Model):
    """One create-to-retire incarnation of a document address."""

    class ScopeKind(models.TextChoices):
        SESSION = "session"
        CONVERSATION = "conversation"
        TASK = "task"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="documents")
    record_id = models.BigIntegerField()
    kind = models.TextField()
    family = models.BooleanField()
    key_value = models.TextField()
    scope_kind = models.CharField(max_length=16, choices=ScopeKind.choices)
    # Conversation or task ID of the scope; 0 for the Session scope.
    owner_id = models.BigIntegerField()
    created_at = models.BigIntegerField()
    retired_at = models.BigIntegerField(null=True)
    record = models.TextField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "record_id"], name="pi_durable_document_unique")]
        indexes = [
            models.Index(
                fields=["store", "kind", "scope_kind", "owner_id", "family", "key_value", "-created_at"],
                name="pi_durable_doc_by_address",
            ),
            models.Index(fields=["store", "scope_kind", "owner_id", "record_id"], name="pi_durable_doc_by_scope"),
        ]


class DocumentRevision(models.Model):
    """A base checkpoint or a delta batch committed at `seq`. Materialization replays deltas after the newest base."""

    class Kind(models.TextChoices):
        BASE = "base"
        DELTA = "delta"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="revisions")
    seq = models.BigIntegerField()
    kind = models.CharField(max_length=8, choices=Kind.choices)
    version = models.IntegerField()
    # The base value, or the JSON array of delta ops.
    content = models.TextField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "seq"], name="pi_durable_revision_unique")]
        indexes = [models.Index(fields=["document", "kind", "-seq"], name="pi_durable_rev_by_kind")]
