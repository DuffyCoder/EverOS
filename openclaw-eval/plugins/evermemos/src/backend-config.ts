// resolveMemoryBackendConfig for EverMemOS plugin.
//
// Like memory-core and mem0, EverMemOS doesn't expose qmd-style
// external interfaces to openclaw — the agent talks to it only via
// memory_search / memory_get tools. So backend stays "builtin" and
// we don't surface a separate qmd config block.
import type { MemoryRuntimeBackendConfig } from "openclaw/plugin-sdk/memory-core-host-engine-storage";

export function resolveEverMemosBackendConfig(): MemoryRuntimeBackendConfig {
  return { backend: "builtin" };
}
