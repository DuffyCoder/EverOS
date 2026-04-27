// Mem0SearchManager — implements MemorySearchManager by delegating
// search/probes/sync to the Python sidecar (mem0ai SDK) and reading
// memory files directly from the workspace (Spike #2 Decision 1 Option A).
//
// Day 2 (this commit): wires real sidecar HTTP. sync() pushes session
// files to sidecar for ingestion. status() returns a cached snapshot
// updated lazily via refreshStatus().
import {
  readMemoryFile,
  type MemoryEmbeddingProbeResult,
  type MemoryProviderStatus,
  type MemorySearchManager,
  type MemorySearchResult,
} from "openclaw/plugin-sdk/memory-core-host-engine-storage";
import {
  Mem0SidecarClient,
  Mem0SidecarError,
  type Mem0Hit,
} from "./sidecar-client.js";

export interface Mem0SearchManagerParams {
  agentId: string;
  workspaceDir: string;
  sidecarUrl?: string;
  sidecarTimeoutMs?: number;
  writeSessionFiles?: boolean;
}

function mapHitToResult(hit: Mem0Hit, fallbackIndex: number): MemorySearchResult {
  return {
    path: hit.path ?? `mem0/hit-${fallbackIndex + 1}.md`,
    startLine: hit.startLine ?? 1,
    endLine: hit.endLine ?? 1,
    score: hit.score,
    snippet: hit.snippet,
    source: hit.source ?? "memory",
    citation: hit.citation,
  };
}

export class Mem0SearchManager implements MemorySearchManager {
  private readonly agentId: string;
  private readonly workspaceDir: string;
  private readonly sidecarUrl: string;
  private readonly sidecarTimeoutMs: number;
  private readonly writeSessionFiles: boolean;
  private readonly client: Mem0SidecarClient;
  private statusSnapshot: MemoryProviderStatus;

  constructor(params: Mem0SearchManagerParams) {
    this.agentId = params.agentId;
    this.workspaceDir = params.workspaceDir;
    this.sidecarUrl = params.sidecarUrl ?? "http://localhost:8765";
    this.sidecarTimeoutMs = params.sidecarTimeoutMs ?? 10000;
    this.writeSessionFiles = params.writeSessionFiles ?? true;
    this.client = new Mem0SidecarClient(this.sidecarUrl, this.sidecarTimeoutMs);
    this.statusSnapshot = {
      backend: "builtin",
      provider: "mem0",
      files: 0,
      chunks: 0,
      dirty: true,
      sources: ["memory"],
      workspaceDir: this.workspaceDir,
      custom: {
        sidecarUrl: this.sidecarUrl,
        sidecarTimeoutMs: this.sidecarTimeoutMs,
        writeSessionFiles: this.writeSessionFiles,
        wired: true,
      },
    };
  }

  async search(
    query: string,
    opts?: { maxResults?: number; minScore?: number; sessionKey?: string },
  ): Promise<MemorySearchResult[]> {
    const trimmed = query.trim();
    if (!trimmed) {
      return [];
    }
    const res = await this.client.search(trimmed, opts);
    return res.hits.map(mapHitToResult);
  }

  async readFile(params: {
    relPath: string;
    from?: number;
    lines?: number;
  }): Promise<{ text: string; path: string }> {
    return await readMemoryFile({
      workspaceDir: this.workspaceDir,
      relPath: params.relPath,
      from: params.from,
      lines: params.lines,
    });
  }

  status(): MemoryProviderStatus {
    return this.statusSnapshot;
  }

  private async refreshStatus(): Promise<void> {
    try {
      const remote = await this.client.stats();
      this.statusSnapshot = {
        ...this.statusSnapshot,
        provider: remote.provider ?? "mem0",
        model: remote.model,
        files: remote.files ?? this.statusSnapshot.files,
        chunks: remote.chunks ?? this.statusSnapshot.chunks,
        dirty: remote.dirty ?? false,
      };
    } catch (err) {
      const reason = err instanceof Mem0SidecarError ? err.message : String(err);
      this.statusSnapshot = {
        ...this.statusSnapshot,
        dirty: true,
        custom: { ...this.statusSnapshot.custom, lastStatsError: reason },
      };
    }
  }

  async probeEmbeddingAvailability(): Promise<MemoryEmbeddingProbeResult> {
    try {
      const res = await this.client.probeEmbedding();
      if (res.ok) {
        return { ok: true };
      }
      return { ok: false, error: res.error ?? "embedding unavailable" };
    } catch (err) {
      return {
        ok: false,
        error: err instanceof Error ? err.message : String(err),
      };
    }
  }

  async probeVectorAvailability(): Promise<boolean> {
    try {
      return await this.client.probeVector();
    } catch {
      return false;
    }
  }

  async sync(params?: {
    reason?: string;
    force?: boolean;
    sessionFiles?: string[];
  }): Promise<void> {
    await this.client.sync({
      reason: params?.reason,
      force: params?.force,
      sessionFiles: params?.sessionFiles,
    });
    await this.refreshStatus();
  }

  async close(): Promise<void> {
    await this.client.close();
  }
}
