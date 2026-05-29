// HTTP client for the mem0 Python sidecar.
//
// Endpoints (Spike #2 Decision 4):
//   POST /index    {documents: [{id, content, metadata}, ...]} → {ok, ingested}
//   POST /search   {query, max_results, session_key?}        → {hits: [{score, snippet, ...}]}
//   GET  /stats                                              → {provider, files, chunks, dirty}
//   GET  /probe_embedding                                    → {ok, model?, error?}
//   GET  /probe_vector                                       → bool
//   POST /sync     {reason, force, session_files}            → {ok}
//   GET  /healthz                                            → {ok}
//   POST /close                                              → {ok}
//
// Day 2 (this file): wire real HTTP via Node 18+ native fetch +
// AbortSignal.timeout. NO retries; sidecar process is co-located so
// transient failures are diagnostic, not noise.

export interface Mem0Document {
  id: string;
  content: string;
  metadata?: Record<string, unknown>;
}

export interface Mem0Hit {
  score: number;
  snippet: string;
  path?: string;
  startLine?: number;
  endLine?: number;
  source?: "memory" | "sessions";
  citation?: string;
  metadata?: Record<string, unknown>;
}

export interface Mem0SearchResponse {
  hits: Mem0Hit[];
  provider?: string;
  model?: string;
}

export interface Mem0StatsResponse {
  provider: string;
  files?: number;
  chunks?: number;
  dirty?: boolean;
  model?: string;
}

export interface Mem0ProbeEmbeddingResponse {
  ok: boolean;
  model?: string;
  error?: string;
}

export class Mem0SidecarError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly path: string,
    public readonly bodySnippet: string,
  ) {
    super(message);
    this.name = "Mem0SidecarError";
  }
}

export class Mem0SidecarClient {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;

  constructor(baseUrl: string, timeoutMs: number) {
    // Strip trailing slash so we can join cleanly.
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.timeoutMs = timeoutMs;
  }

  private async request<T>(
    method: "GET" | "POST",
    path: string,
    body?: unknown,
  ): Promise<T> {
    const url = `${this.baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
    const init: RequestInit = {
      method,
      headers: body ? { "content-type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(this.timeoutMs),
    };
    let res: Response;
    try {
      res = await fetch(url, init);
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      throw new Mem0SidecarError(
        `mem0 sidecar ${method} ${path} failed: ${reason}`,
        0,
        path,
        "",
      );
    }
    const text = await res.text();
    if (!res.ok) {
      throw new Mem0SidecarError(
        `mem0 sidecar ${method} ${path} returned ${res.status}`,
        res.status,
        path,
        text.slice(0, 512),
      );
    }
    if (!text) {
      return {} as T;
    }
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new Mem0SidecarError(
        `mem0 sidecar ${method} ${path} returned non-JSON body`,
        res.status,
        path,
        text.slice(0, 512),
      );
    }
  }

  async healthz(): Promise<boolean> {
    try {
      const res = await this.request<{ ok?: boolean }>("GET", "/healthz");
      return res.ok === true;
    } catch {
      return false;
    }
  }

  async index(documents: Mem0Document[]): Promise<{ ok: boolean; ingested: number }> {
    return await this.request("POST", "/index", { documents });
  }

  async search(
    query: string,
    opts?: { maxResults?: number; minScore?: number; sessionKey?: string },
  ): Promise<Mem0SearchResponse> {
    return await this.request("POST", "/search", {
      query,
      max_results: opts?.maxResults,
      min_score: opts?.minScore,
      session_key: opts?.sessionKey,
    });
  }

  async stats(): Promise<Mem0StatsResponse> {
    return await this.request("GET", "/stats");
  }

  async probeEmbedding(): Promise<Mem0ProbeEmbeddingResponse> {
    return await this.request("GET", "/probe_embedding");
  }

  async probeVector(): Promise<boolean> {
    const res = await this.request<{ enabled?: boolean }>("GET", "/probe_vector");
    return res.enabled === true;
  }

  async sync(params: {
    reason?: string;
    force?: boolean;
    sessionFiles?: string[];
  }): Promise<{ ok: boolean }> {
    return await this.request("POST", "/sync", {
      reason: params.reason,
      force: params.force,
      session_files: params.sessionFiles,
    });
  }

  async close(): Promise<void> {
    try {
      await this.request("POST", "/close");
    } catch {
      // close is best-effort: container teardown will clean up regardless.
    }
  }
}
