import { fileURLToPath } from "node:url";
import { defineConfig, mergeConfig } from "vitest/config";
import baseConfig from "../../../vitest.base.ts";

const durable = (path: string) => fileURLToPath(new URL(`../../../packages/durable/src/${path}`, import.meta.url));

export default mergeConfig(
	baseConfig,
	defineConfig({
		test: { environment: "node", testTimeout: 30_000, hookTimeout: 60_000 },
		resolve: {
			conditions: ["source"],
			alias: [
				{ find: /^@earendil-works\/pi-durable$/, replacement: durable("index.ts") },
				{ find: /^@earendil-works\/pi-durable\/testing$/, replacement: durable("testing/index.ts") },
			],
		},
	}),
);
