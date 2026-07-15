import { afterEach, describe, expect, it, vi } from "vitest";

import type { OpenVikingClient } from "../../client.js";
import { memoryOpenVikingConfigSchema } from "../../config.js";
import { createMemoryOpenVikingContextEngine } from "../../context-engine.js";

function makeLogger() {
  return {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  };
}

describe("context-engine archive index", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("injects pre-archive abstracts as an Archive Index for archive tools", async () => {
    const cfg = memoryOpenVikingConfigSchema.parse({
      mode: "remote",
      baseUrl: "http://127.0.0.1:1933",
      autoCapture: false,
      autoRecall: false,
    });
    const client = {
      getSessionContext: vi.fn().mockResolvedValue({
        latest_archive_overview: "Summary says the user discussed books.",
        pre_archive_abstracts: [
          {
            archive_id: "archive_007",
            abstract: "Tim and John discussed The Hobbit and signed basketball cards.",
          },
        ],
        messages: [],
        estimatedTokens: 100,
        stats: {
          totalArchives: 1,
          includedArchives: 1,
          droppedArchives: 0,
          failedArchives: 0,
          activeTokens: 0,
          archiveTokens: 100,
        },
      }),
    } as unknown as OpenVikingClient;
    const engine = createMemoryOpenVikingContextEngine({
      id: "openviking",
      name: "Context Engine (OpenViking)",
      version: "test",
      cfg,
      logger: makeLogger(),
      getClient: vi.fn().mockResolvedValue(client),
      resolveAgentId: vi.fn(() => "agent:session-1"),
    });

    const result = await engine.assemble({
      sessionId: "session-1",
      messages: [{ role: "user", content: "Which books did Tim read?" }],
      availableTools: [],
    });

    expect(JSON.stringify(result.messages)).toContain(
      "[Archive Index]\\n" +
        "archive_007: Tim and John discussed The Hobbit and signed basketball cards.",
    );
    expect(result.systemPromptAddition).toContain("ov_archive_search");
  });

  it("keeps the Archive Index bounded to the configured trim limit", async () => {
    const cfg = memoryOpenVikingConfigSchema.parse({
      mode: "remote",
      baseUrl: "http://127.0.0.1:1933",
      autoCapture: false,
      autoRecall: false,
    });
    const client = {
      getSessionContext: vi.fn().mockResolvedValue({
        latest_archive_overview: "Summary says the user discussed books.",
        pre_archive_abstracts: Array.from({ length: 22 }, (_, index) => ({
          archive_id: `archive_${String(index + 1).padStart(3, "0")}`,
          abstract: `Archive ${index + 1} details.`,
        })),
        messages: [],
        estimatedTokens: 100,
        stats: {
          totalArchives: 22,
          includedArchives: 22,
          droppedArchives: 0,
          failedArchives: 0,
          activeTokens: 0,
          archiveTokens: 100,
        },
      }),
    } as unknown as OpenVikingClient;
    const engine = createMemoryOpenVikingContextEngine({
      id: "openviking",
      name: "Context Engine (OpenViking)",
      version: "test",
      cfg,
      logger: makeLogger(),
      getClient: vi.fn().mockResolvedValue(client),
      resolveAgentId: vi.fn(() => "agent:session-1"),
    });

    const result = await engine.assemble({
      sessionId: "session-1",
      messages: [{ role: "user", content: "Which books did Tim read?" }],
      availableTools: [],
    });
    const rendered = JSON.stringify(result.messages);

    expect(rendered).toContain("archive_020: Archive 20 details.");
    expect(rendered).not.toContain("archive_021: Archive 21 details.");
    expect(rendered).not.toContain("archive_022: Archive 22 details.");
  });

  it("allows archive index trim limit to be lowered through environment", async () => {
    vi.stubEnv("OPENVIKING_ARCHIVE_INDEX_TRIM_LIMIT", "12");

    const cfg = memoryOpenVikingConfigSchema.parse({
      mode: "remote",
      baseUrl: "http://127.0.0.1:1933",
      autoCapture: false,
      autoRecall: false,
    });
    const client = {
      getSessionContext: vi.fn().mockResolvedValue({
        latest_archive_overview: "Summary says the user discussed books.",
        pre_archive_abstracts: Array.from({ length: 14 }, (_, index) => ({
          archive_id: `archive_${String(index + 1).padStart(3, "0")}`,
          abstract: `Archive ${index + 1} details.`,
        })),
        messages: [],
        estimatedTokens: 100,
        stats: {
          totalArchives: 14,
          includedArchives: 14,
          droppedArchives: 0,
          failedArchives: 0,
          activeTokens: 0,
          archiveTokens: 100,
        },
      }),
    } as unknown as OpenVikingClient;
    const engine = createMemoryOpenVikingContextEngine({
      id: "openviking",
      name: "Context Engine (OpenViking)",
      version: "test",
      cfg,
      logger: makeLogger(),
      getClient: vi.fn().mockResolvedValue(client),
      resolveAgentId: vi.fn(() => "agent:session-1"),
    });

    const result = await engine.assemble({
      sessionId: "session-1",
      messages: [{ role: "user", content: "Which books did Tim read?" }],
      availableTools: [],
    });
    const rendered = JSON.stringify(result.messages);

    expect(rendered).toContain("archive_012: Archive 12 details.");
    expect(rendered).not.toContain("archive_013: Archive 13 details.");
    expect(rendered).not.toContain("archive_014: Archive 14 details.");
  });

  it("can render the most recent archive abstracts first", async () => {
    vi.stubEnv("OPENVIKING_ARCHIVE_INDEX_ORDER", "recent");

    const cfg = memoryOpenVikingConfigSchema.parse({
      mode: "remote",
      baseUrl: "http://127.0.0.1:1933",
      autoCapture: false,
      autoRecall: false,
    });
    const client = {
      getSessionContext: vi.fn().mockResolvedValue({
        latest_archive_overview: "Summary says the user discussed books.",
        pre_archive_abstracts: Array.from({ length: 22 }, (_, index) => ({
          archive_id: `archive_${String(index + 1).padStart(3, "0")}`,
          abstract: `Archive ${index + 1} details.`,
        })),
        messages: [],
        estimatedTokens: 100,
        stats: {
          totalArchives: 22,
          includedArchives: 22,
          droppedArchives: 0,
          failedArchives: 0,
          activeTokens: 0,
          archiveTokens: 100,
        },
      }),
    } as unknown as OpenVikingClient;
    const engine = createMemoryOpenVikingContextEngine({
      id: "openviking",
      name: "Context Engine (OpenViking)",
      version: "test",
      cfg,
      logger: makeLogger(),
      getClient: vi.fn().mockResolvedValue(client),
      resolveAgentId: vi.fn(() => "agent:session-1"),
    });

    const result = await engine.assemble({
      sessionId: "session-1",
      messages: [{ role: "user", content: "Which books did Tim read?" }],
      availableTools: [],
    });
    const rendered = JSON.stringify(result.messages);

    expect(rendered).toContain(
      "[Archive Index]\\narchive_022: Archive 22 details.",
    );
    expect(rendered).toContain("archive_003: Archive 3 details.");
    expect(rendered).not.toContain("archive_001: Archive 1 details.");
    expect(rendered).not.toContain("archive_002: Archive 2 details.");
  });

  it("can disable Archive Index injection for ablation runs", async () => {
    vi.stubEnv("OPENVIKING_ARCHIVE_INDEX_ORDER", "off");

    const cfg = memoryOpenVikingConfigSchema.parse({
      mode: "remote",
      baseUrl: "http://127.0.0.1:1933",
      autoCapture: false,
      autoRecall: false,
    });
    const client = {
      getSessionContext: vi.fn().mockResolvedValue({
        latest_archive_overview: "Summary says the user discussed books.",
        pre_archive_abstracts: [
          {
            archive_id: "archive_007",
            abstract: "Tim and John discussed The Hobbit and signed basketball cards.",
          },
        ],
        messages: [],
        estimatedTokens: 100,
        stats: {
          totalArchives: 1,
          includedArchives: 1,
          droppedArchives: 0,
          failedArchives: 0,
          activeTokens: 0,
          archiveTokens: 100,
        },
      }),
    } as unknown as OpenVikingClient;
    const engine = createMemoryOpenVikingContextEngine({
      id: "openviking",
      name: "Context Engine (OpenViking)",
      version: "test",
      cfg,
      logger: makeLogger(),
      getClient: vi.fn().mockResolvedValue(client),
      resolveAgentId: vi.fn(() => "agent:session-1"),
    });

    const result = await engine.assemble({
      sessionId: "session-1",
      messages: [{ role: "user", content: "Which books did Tim read?" }],
      availableTools: [],
    });
    const rendered = JSON.stringify(result.messages);

    expect(rendered).toContain("[Session History Summary]");
    expect(rendered).not.toContain("[Archive Index]");
    expect(rendered).not.toContain("archive_007");
  });
});
