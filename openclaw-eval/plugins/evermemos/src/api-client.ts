// HTTP client for the EverMemOS Memory API.
//
// Endpoints (matched to evaluation/src/adapters/evermemos_api_adapter.py):
//   POST /api/v1/memories         — ingest one message
//   GET  /api/v1/memories/search  — retrieve memories
//
// EverMemOS runs on the eval host, NOT inside the openclaw container.
// Plugin reaches it via host.docker.internal:1995 (requires the
// container to be started with --add-host=host.docker.internal:host-gateway,
// which OpenClawDockerAdapter handles for evermemos memory_mode).

export interface EverMemosMessagePayload {
  group_id: string;
  group_name?: string;
  message_id?: string;
  create_time?: string;
  sender?: string;
  sender_name?: string;
  content: string;
  refer_list?: unknown[];
}

export interface EverMemosSearchHit {
  // We surface only a subset of fields; the upstream API returns more
  // detail under ``result.memories[][group_id][]`` that we don't need
  // for tool output.
  text: string;
  score: number;
  metadata: Record<string, unknown>;
}

export interface EverMemosSearchOptions {
  query: string;
  topK?: number;
  groupId?: string;
  userId?: string;
  retrieveMethod?: string;
  scope?: "personal" | "group";
  memoryTypes?: string[];
}

export class EverMemosApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly path: string,
    public readonly bodySnippet: string,
  ) {
    super(message);
    this.name = "EverMemosApiError";
  }
}

export class EverMemosApiClient {
  private readonly baseUrl: string;
  private readonly apiKey?: string;
  private readonly timeoutMs: number;

  constructor(params: { baseUrl: string; apiKey?: string; timeoutMs?: number }) {
    this.baseUrl = params.baseUrl.replace(/\/+$/, "");
    this.apiKey = params.apiKey;
    this.timeoutMs = params.timeoutMs ?? 30000;
  }

  private headers(): Record<string, string> {
    const h: Record<string, string> = { "content-type": "application/json" };
    if (this.apiKey) {
      h["authorization"] = `Bearer ${this.apiKey}`;
    }
    return h;
  }

  private async request<T>(
    method: "GET" | "POST",
    path: string,
    options: { params?: Record<string, string>; body?: unknown } = {},
  ): Promise<T> {
    let url = `${this.baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
    if (options.params && Object.keys(options.params).length > 0) {
      const qs = new URLSearchParams(options.params).toString();
      url = `${url}?${qs}`;
    }
    const init: RequestInit = {
      method,
      headers: options.body ? this.headers() : { authorization: this.apiKey ? `Bearer ${this.apiKey}` : "" },
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal: AbortSignal.timeout(this.timeoutMs),
    };
    let res: Response;
    try {
      res = await fetch(url, init);
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      throw new EverMemosApiError(
        `evermemos ${method} ${path} fetch failed: ${reason}`,
        0,
        path,
        "",
      );
    }
    const text = await res.text();
    if (!res.ok) {
      throw new EverMemosApiError(
        `evermemos ${method} ${path} returned ${res.status}`,
        res.status,
        path,
        text.slice(0, 512),
      );
    }
    if (!text) return {} as T;
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new EverMemosApiError(
        `evermemos ${method} ${path} returned non-JSON body`,
        res.status,
        path,
        text.slice(0, 512),
      );
    }
  }

  async ingest(payload: EverMemosMessagePayload, syncMode: boolean = true): Promise<unknown> {
    return await this.request("POST", "/api/v1/memories", {
      params: syncMode ? { sync_mode: "true" } : undefined,
      body: payload,
    });
  }

  async search(opts: EverMemosSearchOptions): Promise<EverMemosSearchHit[]> {
    const params: Record<string, string> = {
      query: opts.query,
      top_k: String(opts.topK ?? 10),
      retrieve_method: opts.retrieveMethod ?? "keyword",
    };
    const scope = opts.scope ?? "group";
    if (scope === "group" && opts.groupId) {
      params["group_id"] = opts.groupId;
      params["user_id"] = "";
    } else if (opts.userId) {
      params["user_id"] = opts.userId;
    }
    if (opts.memoryTypes && opts.memoryTypes.length > 0) {
      params["memory_types"] = opts.memoryTypes.join(",");
    }
    const data = await this.request<{
      result?: {
        memories?: Array<Record<string, unknown[]>>;
        scores?: Array<Record<string, number[]>>;
      };
    }>("GET", "/api/v1/memories/search", { params });
    return flattenSearchResponse(data);
  }
}

function flattenSearchResponse(data: {
  result?: {
    memories?: Array<Record<string, unknown[]>>;
    scores?: Array<Record<string, number[]>>;
  };
}): EverMemosSearchHit[] {
  const memories = data.result?.memories ?? [];
  const scores = data.result?.scores ?? [];

  // Flatten {group_id: [mem, ...]} bags into one stream + matched scores.
  const memList: Array<{ groupId: string; mem: unknown }> = [];
  for (const bag of memories) {
    for (const [gid, items] of Object.entries(bag)) {
      for (const item of items) {
        memList.push({ groupId: gid, mem: item });
      }
    }
  }
  const scoreList: number[] = [];
  for (const bag of scores) {
    for (const items of Object.values(bag)) {
      for (const s of items) scoreList.push(Number(s) || 0);
    }
  }
  const hits: EverMemosSearchHit[] = [];
  for (let i = 0; i < memList.length; i++) {
    const item = memList[i];
    const mem = (item.mem ?? {}) as Record<string, unknown>;
    const text =
      (mem.content as string) ||
      (mem.text as string) ||
      (mem.summary as string) ||
      JSON.stringify(mem);
    const score = scoreList[i] ?? 0;
    hits.push({
      text,
      score,
      metadata: { ...mem, group_id: item.groupId },
    });
  }
  return hits;
}
