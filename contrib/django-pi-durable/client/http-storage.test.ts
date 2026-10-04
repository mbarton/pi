import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { registerStorageConformance } from "@earendil-works/pi-durable/testing";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { HttpStorage, type HttpStorageOptions } from "./http-storage.ts";

// Runs the pi-durable Storage conformance suite against `manage.py runserver` with the test project.
// PI_DURABLE_PYTHON selects an interpreter with Django installed.
const python = process.env.PI_DURABLE_PYTHON ?? "python3";
const appRoot = fileURLToPath(new URL("..", import.meta.url));
let server: ChildProcess | undefined;
let directory: string | undefined;
let options: HttpStorageOptions;

async function freePort(): Promise<number> {
	return new Promise((resolve, reject) => {
		const probe = createServer();
		probe.once("error", reject);
		probe.listen(0, "127.0.0.1", () => {
			const address = probe.address();
			probe.close(() => resolve(typeof address === "object" && address !== null ? address.port : 0));
		});
	});
}

beforeAll(async () => {
	directory = mkdtempSync(join(tmpdir(), "pi-durable-django-"));
	const env = { ...process.env, PI_DURABLE_TEST_DB: join(directory, "db.sqlite3") };
	execFileSync(python, ["manage.py", "migrate", "--verbosity", "0"], { cwd: appRoot, env, stdio: "inherit" });
	const port = await freePort();
	server = spawn(python, ["manage.py", "runserver", "--noreload", "--nothreading", `127.0.0.1:${port}`], {
		cwd: appRoot,
		env,
		stdio: "ignore",
	});
	options = { baseUrl: `http://127.0.0.1:${port}/durable/`, idBlockSize: 8 };
	for (let attempt = 0; ; attempt++) {
		try {
			await fetch(options.baseUrl);
			return;
		} catch (error) {
			if (attempt > 100) throw error;
			await new Promise((resolve) => setTimeout(resolve, 100));
		}
	}
});

afterAll(() => {
	server?.kill();
	if (directory !== undefined) rmSync(directory, { recursive: true, force: true });
});

registerStorageConformance({ describe, it, expect }, "HttpStorage (Django)", async (use) => {
	const storage = await HttpStorage.create(options);
	try {
		await use(storage);
	} finally {
		await storage.close(BACKGROUND_CONTEXT);
	}
});

describe("HttpStorage (Django) stores", () => {
	it("isolates record namespaces between stores", async () => {
		const first = await HttpStorage.create(options);
		const second = await HttpStorage.create(options);
		expect(await first.mintId()).toBe(2);
		expect(await second.mintId()).toBe(2);
		await first.commit([{ type: "conversation", value: { id: 1 as never } }], BACKGROUND_CONTEXT);
		expect(await second.conversation(1 as never, BACKGROUND_CONTEXT)).toBeUndefined();
		const reopened = HttpStorage.open(options, first.id);
		expect(await reopened.conversation(1 as never, BACKGROUND_CONTEXT)).toEqual({ id: 1 });
	});
});
