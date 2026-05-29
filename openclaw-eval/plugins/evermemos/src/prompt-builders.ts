// Pre-baked prompt-section variants for Stage 2 Track B ablation.
//
// Each plugin ships ALL THREE prompts so we can swap via env var
// without rebuilding multiple images per plugin. The three variants:
//   - "native"      — this plugin's own prompt (mem0 docs style)
//   - "memory-core" — memory-core's upstream prompt
//                     (extensions/memory-core/src/prompt-section.ts)
//   - "evermemos"   — evermemos plugin's prompt (group-conversation
//                     framing from this repo's docs)
//
// Calling code resolves OPENCLAW_PROMPT_STYLE env var and picks one.

export function buildMemoryPromptSection(
  style: string,
  availableTools: Set<string>,
  citationsMode?: "off" | "default",
): string[] {
  const hasSearch = availableTools.has("memory_search");
  const hasGet = availableTools.has("memory_get");
  if (!hasSearch && !hasGet) {
    return [];
  }
  const normalized = (style || "native").trim().toLowerCase();
  if (normalized === "memory-core") {
    return buildMemoryCorePrompt(hasSearch, hasGet, citationsMode);
  }
  if (normalized === "evermemos") {
    return buildEverMemosPrompt(hasSearch, hasGet, citationsMode);
  }
  return buildMem0Prompt(hasSearch, hasGet, citationsMode);
}

function buildMem0Prompt(
  hasSearch: boolean,
  hasGet: boolean,
  citationsMode?: "off" | "default",
): string[] {
  // Faithful adaptation of mem0's recommended agent prompts:
  //   docs/integrations/elevenlabs.mdx (full template)
  //   skills/mem0/references/integration-patterns.md (concise)
  const lines: string[] = [
    "## Memory",
    "You have access to a memory of past conversations with this user — their preferences, personal details, decisions, and important things they have shared.",
  ];
  if (hasSearch) {
    lines.push(
      "Use memory_search to recall relevant context from prior conversations whenever the user asks about people, events, dates, preferences, or things they previously mentioned. Before responding to such questions, always check memory first.",
    );
  }
  if (hasGet) {
    lines.push(
      "Use memory_get to read a specific memory entry in full when memory_search surfaces a snippet you want to expand.",
    );
  }
  if (citationsMode === "off") {
    lines.push(
      "Citations are disabled: do not mention memory paths or IDs in replies unless the user explicitly asks.",
    );
  }
  lines.push("");
  return lines;
}

function buildMemoryCorePrompt(
  hasSearch: boolean,
  hasGet: boolean,
  citationsMode?: "off" | "default",
): string[] {
  // Verbatim from openclaw/extensions/memory-core/src/prompt-section.ts.
  // Used in Track B to test whether memory-core's directive wording
  // explains its score relative to natively-prompted plugins.
  let toolGuidance: string;
  if (hasSearch && hasGet) {
    toolGuidance =
      "Before answering anything about prior work, decisions, dates, people, preferences, or todos: run memory_search on MEMORY.md + memory/*.md + indexed session transcripts; then use memory_get to pull only the needed lines. If low confidence after search, say you checked.";
  } else if (hasSearch) {
    toolGuidance =
      "Before answering anything about prior work, decisions, dates, people, preferences, or todos: run memory_search on MEMORY.md + memory/*.md + indexed session transcripts and answer from the matching results. If low confidence after search, say you checked.";
  } else {
    toolGuidance =
      "Before answering anything about prior work, decisions, dates, people, preferences, or todos that already point to a specific memory file or note: run memory_get to pull only the needed lines. If low confidence after reading them, say you checked.";
  }
  const lines: string[] = ["## Memory Recall", toolGuidance];
  if (citationsMode === "off") {
    lines.push(
      "Citations are disabled: do not mention file paths or line numbers in replies unless the user explicitly asks.",
    );
  } else {
    lines.push(
      "Citations: include Source: <path#line> when it helps the user verify memory snippets.",
    );
  }
  lines.push("");
  return lines;
}

function buildEverMemosPrompt(
  hasSearch: boolean,
  hasGet: boolean,
  citationsMode?: "off" | "default",
): string[] {
  const lines: string[] = [
    "## Memory (EverMemOS)",
    "You have access to the prior conversation history of this group, along with extracted events, decisions, and profile facts. Memory is retrieved by querying the EverMemOS index.",
  ];
  if (hasSearch) {
    lines.push(
      "Use memory_search whenever the user asks about earlier turns of the conversation, what someone said, decisions made, dates and people mentioned, or facts the user previously shared. Run memory_search before answering such questions and use the returned snippets to ground your reply.",
    );
  }
  if (hasGet) {
    lines.push(
      "Use memory_get to read the full text of a specific memory entry (e.g. when memory_search surfaces a snippet you want to expand).",
    );
  }
  if (citationsMode === "off") {
    lines.push(
      "Citations are disabled: do not mention message_ids in replies unless the user explicitly asks.",
    );
  }
  lines.push("");
  return lines;
}
