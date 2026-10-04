import type { Context, JsonValue } from "@earendil-works/chord";
import {
	type ConversationId,
	type ConversationQuery,
	type ConversationRecord,
	type Cursor,
	type DocumentAddress,
	type DocumentId,
	type DocumentPoint,
	type DocumentQuery,
	type DocumentRecord,
	type EntryId,
	type EntryQuery,
	type EntryRecord,
	type Id,
	type Page,
	type Seq,
	type Storage,
	StorageRejected,
	type StorageWrite,
	type StoredDocument,
	type SubmissionId,
	type SubmissionQuery,
	type SubmissionRecord,
	type TaskId,
	type TaskQuery,
	type TaskRecord,
} from "@earendil-works/pi-durable";

type JsonTask = TaskRecord<JsonValue, JsonValue, JsonValue>;
type StoredEntry = { readonly entry: EntryRecord; readonly commitSeq: Seq };
type Reply = {
	readonly result?: unknown;
	readonly error?: { readonly type: "rejected" | "error"; readonly message: string };
};

export type HttpStorageOptions = {
	/** App mount point, e.g. `https://example.com/durable/`. */
	readonly baseUrl: string;
	/** Extra request headers, e.g. `Authorization`. */
	readonly headers?: Readonly<Record<string, string>>;
	/** IDs reserved per round trip. Unused IDs of a block become gaps when the storage closes. */
	readonly idBlockSize?: number;
	readonly fetch?: typeof fetch;
};

async function post(options: HttpStorageOptions, path: string, body: unknown, context?: Context): Promise<unknown> {
	const response = await (options.fetch ?? fetch)(new URL(path, options.baseUrl), {
		method: "POST",
		headers: { "Content-Type": "application/json", ...options.headers },
		body: JSON.stringify(body),
		signal: context?.abortSignal,
	});
	const reply = (await response.json()) as Reply;
	if (reply.error !== undefined) {
		if (reply.error.type === "rejected") throw new StorageRejected(reply.error.message);
		throw new Error(reply.error.message);
	}
	return reply.result;
}

/**
 * `Storage` backed by the `pi_durable` Django app. The server performs every check and commit; this client only mints
 * IDs from reserved blocks and maps JSON `null` results to `undefined`.
 */
export class HttpStorage implements Storage {
	private readonly options: HttpStorageOptions;
	private readonly storeId: string;
	private nextId = 0;
	private reservedEnd = 0;
	private closed = false;

	private constructor(options: HttpStorageOptions, storeId: string) {
		this.options = options;
		this.storeId = storeId;
	}

	/** Create a new store on the server and open it. */
	static async create(options: HttpStorageOptions, label?: string): Promise<HttpStorage> {
		const result = (await post(options, "stores", label === undefined ? {} : { label })) as { readonly id: string };
		return new HttpStorage(options, result.id);
	}

	/** Open an existing store. The server checks access on every call. */
	static open(options: HttpStorageOptions, storeId: string): HttpStorage {
		return new HttpStorage(options, storeId);
	}

	get id(): string {
		return this.storeId;
	}

	private async call<T>(method: string, args: Record<string, unknown>, context?: Context): Promise<T> {
		if (this.closed) throw new Error("HttpStorage is closed");
		const result = await post(this.options, `stores/${this.storeId}/${method}`, args, context);
		return (result ?? undefined) as T;
	}

	async commit(writes: readonly StorageWrite[], context: Context): Promise<Seq> {
		const seq = await this.call<Seq>("commit", { writes }, context);
		// Explicit IDs in the batch advance the server's allocator past them; drop a block they overlap.
		for (const write of writes) {
			const id = "value" in write ? write.value.id : "record" in write ? write.record.id : undefined;
			if (id !== undefined && id >= this.nextId) this.reservedEnd = this.nextId;
		}
		return seq;
	}

	async mintId<I extends Id<string>>(): Promise<I> {
		if (this.closed) throw new Error("HttpStorage is closed");
		if (this.nextId >= this.reservedEnd) {
			const block = await this.call<{ readonly start: number; readonly end: number }>("reserveIds", {
				count: this.options.idBlockSize ?? 64,
			});
			this.nextId = block.start;
			this.reservedEnd = block.end;
		}
		return this.nextId++ as I;
	}

	conversation(id: ConversationId, context: Context): Promise<ConversationRecord | undefined> {
		return this.call("conversation", { id }, context);
	}

	scanConversations(
		query: ConversationQuery,
		limit: number,
		cursor: Cursor | undefined,
		context: Context,
	): Promise<Page<ConversationRecord, Cursor>> {
		return this.call("scanConversations", { query, limit, cursor }, context);
	}

	entry(id: EntryId, context: Context): Promise<StoredEntry | undefined>;
	entry(conversationId: ConversationId, id: EntryId, context: Context): Promise<StoredEntry | undefined>;
	entry(
		idOrConversationId: EntryId | ConversationId,
		idOrContext: EntryId | Context,
		context?: Context,
	): Promise<StoredEntry | undefined> {
		if (typeof idOrContext === "number") {
			return this.call("entry", { conversationId: idOrConversationId, id: idOrContext }, context);
		}
		return this.call("entry", { id: idOrConversationId }, idOrContext);
	}

	findLatestHeadMarker(
		conversationId: ConversationId,
		atOrBeforeEntryId: EntryId | undefined,
		context: Context,
	): Promise<(EntryRecord & { readonly head: EntryId }) | undefined> {
		return this.call("findLatestHeadMarker", { conversationId, atOrBeforeEntryId }, context);
	}

	scanEntries(
		query: EntryQuery,
		limit: number,
		cursor: Cursor | undefined,
		context: Context,
	): Promise<Page<EntryRecord, Cursor>> {
		return this.call("scanEntries", { query, limit, cursor }, context);
	}

	task(id: TaskId, context: Context): Promise<JsonTask | undefined> {
		return this.call("task", { id }, context);
	}

	scanTasks(query: TaskQuery, limit: number, cursor: Cursor | undefined, context: Context): Promise<Page<JsonTask, Cursor>> {
		return this.call("scanTasks", { query, limit, cursor }, context);
	}

	submission(id: SubmissionId, context: Context): Promise<SubmissionRecord | undefined> {
		return this.call("submission", { id }, context);
	}

	scanSubmissions(
		query: SubmissionQuery,
		limit: number,
		cursor: Cursor | undefined,
		context: Context,
	): Promise<Page<SubmissionRecord, Cursor>> {
		return this.call("scanSubmissions", { query, limit, cursor }, context);
	}

	submissionByRequest(
		conversationId: ConversationId,
		requestId: string,
		context: Context,
	): Promise<SubmissionRecord | undefined> {
		return this.call("submissionByRequest", { conversationId, requestId }, context);
	}

	findDocument(address: DocumentAddress, at: DocumentPoint, context: Context): Promise<DocumentRecord | undefined> {
		return this.call("findDocument", { address, at }, context);
	}

	document(id: DocumentId, at: DocumentPoint, context: Context): Promise<StoredDocument | undefined> {
		return this.call("document", { id, at }, context);
	}

	scanDocuments(
		query: DocumentQuery,
		limit: number,
		cursor: Cursor | undefined,
		context: Context,
	): Promise<Page<DocumentRecord, Cursor>> {
		return this.call("scanDocuments", { query, limit, cursor }, context);
	}

	async close(_context: Context): Promise<void> {
		this.closed = true;
	}
}
