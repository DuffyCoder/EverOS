// Mem0SearchManager — implements MemorySearchManager by delegating to a
// Python sidecar (mem0ai SDK).
//
// Day 1 (this file): scaffolding only. search()/readFile()/probes throw
// or return inert defaults so the plugin LOADS cleanly in the build but
// any actual call surfaces a clear "Day 2 needed" error.
// Day 2 wires sidecar-client.ts in here.
import type {
  MemoryEmbeddingProbeResult,
  MemoryProviderStatus,
  MemorySearchManager,
  MemorySearchResult,
} from "openclaw/plugin-sdk/memory-core-host-engine-storage";

export interface Mem0SearchManagerParams {
  agentId: string;
  // Sidecar URL + timeout will be threaded in from runtime in Day 2.
  // Stored here to keep the constructor shape stable across days.
  sidecarUrl?: string;
  sidecarTimeoutMs?: number;
  writeSessionFiles?: boolean;
}

const NOT_WIRED = "mem0 sidecar-client not yet wired (Stage 1 Week 2 Day 2)";

export class Mem0SearchManager implements MemorySearchManager {
  private readonly agentId: string;
  private readonly sidecarUrl: string;
  private readonly sidecarTimeoutMs: number;
  private readonly writeSessionFiles: boolean;

  constructor(params: Mem0SearchManagerParams) {
    this.agentId = params.agentId;
    this.sidecarUrl = params.sidecarUrl ?? "http://localhost:8765";
    this.sidecarTimeoutMs = params.sidecarTimeoutMs ?? 10000;
    this.writeSessionFiles = params.writeSessionFiles ?? true;
  }

  async search(
    _query: string,
    _opts?: { maxResults?: number; minScore?: number; sessionKey?: string },
  ): Promise<MemorySearchResult[]> {
    throw new Error(NOT_WIRED);
  }

  async readFile(_params: {
    relPath: string;
    from?: number;
    lines?: number;
  }): Promise<{ text: string; path: string }> {
    throw new Error(NOT_WIRED);
  }

  status(): MemoryProviderStatus {
    return {
      backend: "builtin",
      provider: "mem0",
      files: 0,
      chunks: 0,
      dirty: false,
      sources: ["memory"],
      custom: {
        sidecarUrl: this.sidecarUrl,
        sidecarTimeoutMs: this.sidecarTimeoutMs,
        writeSessionFiles: this.writeSessionFiles,
        wired: false,
      },
    };
  }

  async probeEmbeddingAvailability(): Promise<MemoryEmbeddingProbeResult> {
    return { ok: false, error: NOT_WIRED };
  }

  async probeVectorAvailability(): Promise<boolean> {
    return false;
  }

  async sync(): Promise<void> {
    // No-op until sidecar wired (Day 2). Returning silently keeps the
    // periodic sync hook in openclaw harmless.
  }

  async close(): Promise<void> {
    // No resources to release in Day 1 stub.
  }
}
