// EverMemosSearchManager — implements MemorySearchManager via the
// EverMemOS HTTP API on the host.
//
// Layout mirrors mem0/src/search-manager.ts. Differences:
//   * No Python sidecar; HTTP calls go directly to host EverMemOS API.
//   * readFile reads from workspace/memory/<rel> (Spike #2 Decision 1A).
//   * sync() POSTs each new session.md to /api/v1/memories. Each file
//     becomes a single message with sender="system" and the file's full
//     text as content. This is the simplest faithful ingest path; full
//     LoCoMo evaluation may want richer per-message ingestion driven
//     from the eval framework (left as a follow-up).
import { promises as fs } from "node:fs";
import path from "node:path";
import {
  readMemoryFile,
  type MemoryEmbeddingProbeResult,
  type MemoryProviderStatus,
  type MemorySearchManager,
  type MemorySearchResult,
} from "openclaw/plugin-sdk/memory-core-host-engine-storage";
import {
  EverMemosApiClient,
  EverMemosApiError,
  type EverMemosSearchHit,
} from "./api-client.js";

export interface EverMemosSearchManagerParams {
  agentId: string;
  workspaceDir: string;
  apiUrl?: string;
  apiKey?: string;
  apiTimeoutMs?: number;
  syncMode?: boolean;
  retrieveMethod?: string;
  scope?: "personal" | "group";
  memoryTypes?: string[];
  // Optional explicit group_id; when set, overrides the agentId-based
  // default. Used in eval mode to scope search to a specific LoCoMo
  // conversation (passed via EVERMEMOS_GROUP_ID env var by the
  // OpenClawDockerAdapter).
  groupId?: string;
}

const DEFAULT_API_URL = "http://host.docker.internal:1995";

function mapHitToResult(hit: EverMemosSearchHit, fallbackIndex: number): MemorySearchResult {
  const meta = hit.metadata ?? {};
  return {
    path: (meta.message_id as string) || (meta.id as string) || `evermemos/hit-${fallbackIndex + 1}.md`,
    startLine: 1,
    endLine: 1,
    score: hit.score,
    snippet: hit.text,
    source: "memory",
  };
}

export class EverMemosSearchManager implements MemorySearchManager {
  private readonly agentId: string;
  private readonly workspaceDir: string;
  private readonly client: EverMemosApiClient;
  private readonly syncMode: boolean;
  private readonly retrieveMethod: string;
  private readonly scope: "personal" | "group";
  private readonly memoryTypes: string[];
  private readonly groupId: string;
  private statusSnapshot: MemoryProviderStatus;

  constructor(params: EverMemosSearchManagerParams) {
    this.agentId = params.agentId;
    this.workspaceDir = params.workspaceDir;
    this.syncMode = params.syncMode ?? true;
    this.retrieveMethod = params.retrieveMethod ?? "keyword";
    this.scope = params.scope ?? "group";
    this.memoryTypes = params.memoryTypes ?? [];
    // Precedence: explicit groupId > agentId > "openclaw" default.
    // The runtime layer threads EVERMEMOS_GROUP_ID env var into
    // groupId when present, so per-container LoCoMo conversations
    // each get their own evermemos group partition.
    this.groupId = params.groupId || params.agentId || "openclaw";
    this.client = new EverMemosApiClient({
      baseUrl: params.apiUrl ?? DEFAULT_API_URL,
      apiKey: params.apiKey,
      timeoutMs: params.apiTimeoutMs ?? 30000,
    });
    this.statusSnapshot = {
      backend: "builtin",
      provider: "evermemos",
      files: 0,
      chunks: 0,
      dirty: true,
      sources: ["memory"],
      workspaceDir: this.workspaceDir,
      custom: {
        apiUrl: params.apiUrl ?? DEFAULT_API_URL,
        scope: this.scope,
        retrieveMethod: this.retrieveMethod,
        groupId: this.groupId,
      },
    };
  }

  async search(
    query: string,
    opts?: { maxResults?: number; minScore?: number; sessionKey?: string },
  ): Promise<MemorySearchResult[]> {
    const trimmed = query.trim();
    if (!trimmed) return [];
    const hits = await this.client.search({
      query: trimmed,
      topK: opts?.maxResults ?? 10,
      groupId: this.groupId,
      userId: opts?.sessionKey,
      retrieveMethod: this.retrieveMethod,
      scope: this.scope,
      memoryTypes: this.memoryTypes,
    });
    const minScore = opts?.minScore ?? -Infinity;
    return hits
      .filter((h) => h.score >= minScore)
      .map((h, i) => mapHitToResult(h, i));
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

  async probeEmbeddingAvailability(): Promise<MemoryEmbeddingProbeResult> {
    // EverMemOS owns its own embeddings server-side. From the plugin's
    // perspective, embedding is "available" iff the API is reachable.
    try {
      // Cheap reachability probe via empty search.
      await this.client.search({ query: "__probe__", topK: 1, groupId: this.groupId });
      return { ok: true };
    } catch (err) {
      return {
        ok: false,
        error: err instanceof Error ? err.message : String(err),
      };
    }
  }

  async probeVectorAvailability(): Promise<boolean> {
    return true;
  }

  async sync(params?: {
    reason?: string;
    force?: boolean;
    sessionFiles?: string[];
  }): Promise<void> {
    const files = params?.sessionFiles ?? [];
    let ingested = 0;
    for (const rel of files) {
      try {
        const abs = path.join(this.workspaceDir, rel);
        const text = await fs.readFile(abs, "utf8");
        await this.client.ingest(
          {
            group_id: this.groupId,
            group_name: this.groupId,
            message_id: rel,
            create_time: new Date().toISOString(),
            sender: "system",
            sender_name: "system",
            content: text,
            refer_list: [],
          },
          this.syncMode,
        );
        ingested++;
      } catch (err) {
        // Log to status custom; one bad file shouldn't kill the run.
        this.statusSnapshot = {
          ...this.statusSnapshot,
          custom: {
            ...this.statusSnapshot.custom,
            lastSyncError: err instanceof EverMemosApiError ? err.message : String(err),
          },
        };
      }
    }
    this.statusSnapshot = {
      ...this.statusSnapshot,
      files: ingested,
      chunks: ingested,
      dirty: false,
    };
  }

  async close(): Promise<void> {
    // No persistent connection to release.
  }
}
