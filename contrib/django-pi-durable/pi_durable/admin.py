"""Read-only admin. Records are written only through `DurableStorage.commit()`, which keeps the ID namespace,
commit sequence, and document revisions consistent; editing rows by hand would break those invariants."""

from django.contrib import admin

from .models import Conversation, Document, Entry, Store, Submission, Task


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ["id", "label", "owner", "created", "next_seq"]
    search_fields = ["id", "label"]
    readonly_fields = ["id", "created", "next_id", "next_seq"]


@admin.register(Conversation)
class ConversationAdmin(ReadOnlyAdmin):
    list_display = ["store", "record_id", "owner_conversation_id", "owner_task_id"]
    list_filter = ["store"]


@admin.register(Entry)
class EntryAdmin(ReadOnlyAdmin):
    list_display = ["store", "record_id", "conversation_id", "commit_seq"]
    list_filter = ["store"]


@admin.register(Task)
class TaskAdmin(ReadOnlyAdmin):
    list_display = ["store", "record_id", "conversation_id", "kind", "status", "abort_requested", "background"]
    list_filter = ["status", "store"]


@admin.register(Submission)
class SubmissionAdmin(ReadOnlyAdmin):
    list_display = ["store", "record_id", "conversation_id", "status"]
    list_filter = ["status", "store"]


@admin.register(Document)
class DocumentAdmin(ReadOnlyAdmin):
    list_display = ["store", "record_id", "kind", "key_value", "scope_kind", "owner_id", "created_at", "retired_at"]
    list_filter = ["scope_kind", "store"]
