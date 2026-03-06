# Prompts for agent memory extraction

AGENT_TOOL_PRE_COMPRESS_PROMPT = """You are a tool call compression expert. Compress OpenAI chat messages while preserving the message structure and essential information.

The input contains two types of messages:
- **role="assistant"** with tool_calls: Compress the "arguments" field in each tool call's function
- **role="tool"**: Compress the "content" field

Previously compressed tool interactions (context, do NOT include in output):
{context_json}

New messages to compress:
{messages_json}

Return in JSON format:
{{
    "compressed_messages": [
        // Compressed version of the new messages only — exactly {new_count} messages
    ]
}}

Rules:
- Return exactly {new_count} compressed messages corresponding to the new messages
- Maintain the same order and structure — only compress function.arguments and tool content
- Keep all fields (role, tool_call_id, id, function.name, etc.) unchanged
- Use the context to understand the overall task flow for better compression
- Be concise but do not lose critical details (e.g., search queries, key findings, error messages)
"""

AGENT_CASE_COMPRESS_PROMPT = """You are an expert at distilling agent interaction trajectories into concise experience records.

Given an agent trajectory (a JSON list of messages from a single MemCell segment), extract ONE experience record that captures the specific problem solved and the concrete problem-solving process.

An **experience** is a compressed record of how a specific task was solved — preserving all key steps, decisions, and results. It serves two purposes:
1. **Reference case**: When the agent encounters a similar task, it can retrieve this experience and follow a proven approach.
2. **Raw material**: Multiple similar experiences are later refined into generalized skills (best practices).

Input messages are in OpenAI chat completion format:
- role="user": User input
- role="assistant" without tool_calls: Agent's direct response
- role="assistant" with tool_calls: Agent decides to call tools (may include reasoning in content)
- role="tool" with tool_call_id: Tool execution result (may have been pre-compressed)

NOTE: Both tool-use and purely conversational interactions can produce valid experiences:
- **Tool-use pattern**: user -> assistant(tool_calls) -> tool -> ... -> assistant(final response)
- **Conversational pattern**: user -> assistant(response) — the agent solves tasks through reasoning without calling tools

Pre-processed trajectory:
{messages}

Instructions:

**0. Filter: Skip Non-Task Interactions**
ONLY extract an experience where the agent **meaningfully helped the user accomplish a task or solve a problem**. Skip:
- Casual chitchat, greetings, small talk
- Pure opinion/preference exchange with no actionable outcome
- Simple factual Q&A requiring no problem-solving (e.g., "What is X?" with a one-line answer)
- **Single-turn conversations without tool calls** (one user message + one assistant response, no tool_calls). A single Q&A exchange without tools rarely captures meaningful problem-solving. Non-tool conversations need **multiple turns** (2+ user messages) of iterative problem-solving.

DO extract when the agent (with OR without tool use):
- Used tools to research, compute, or execute actions (even in a single turn)
- Guided the user through a multi-step problem-solving process via multi-turn conversation
- Helped debug or troubleshoot through iterative reasoning across multiple turns
- Delivered detailed, actionable recommendations through multi-turn dialogue requiring domain expertise

If the conversation is not worth recording, return {{"task_intent": null}}.

**CRITICAL LANGUAGE RULE**: You MUST output in the SAME language as the input conversation content. If the conversation content is in Chinese, ALL output MUST be in Chinese. If in English, output in English. This is mandatory.

**1. Extract the Experience:**
- **task_intent**: Synthesize the specific task from ALL turns into a single, self-contained statement (not a question). This serves as a retrieval key for finding similar past cases.
- **approach**: A compressed record that decomposes the task into sub-problems, each capturing what was attempted and what resulted:
  - Each numbered step = one sub-problem the agent needed to solve on the way to the overall task.
  - Under each step: what the agent tried (tool used or reasoning applied) and the result obtained (findings, errors, metrics).
  - If a sub-problem required multiple attempts (e.g., first attempt failed, then revised), compress them into one step showing the key attempts and the final resolution.
  - End with "Outcome:" summarizing the final result of the overall task.
  - Keep it concise but complete — another agent should be able to follow this decomposition to solve the same problem.
- **quality_score**: How well the agent completed this task (0.0 = failure, 1.0 = perfect).

Return in JSON format:
{{
    "task_intent": "The specific task as a self-contained statement",
    "approach": "1. <sub-problem>\\n   - Tried: <what was attempted, tool or reasoning>\\n   - Result: <what was found/achieved or why it failed>\\n2. <next sub-problem>\\n   - Tried: <attempt>\\n   - Result: <outcome>\\n...\\n\\nOutcome: <final result of the overall task>",
    "quality_score": 0.0-1.0
}}
"""

AGENT_SKILL_EXTRACT_PROMPT = """You are an expert at refining the best problem-solving processes from concrete agent task experiences.

You will receive:
1. **New AgentCase(s)** just added to a MemScene cluster (a group of semantically similar tasks). Each experience is a concrete record of how the agent solved a specific task — step-by-step process with decisions and results, along with a quality_score (0.0-1.0) indicating how well the task was completed.
2. **Existing skills** previously extracted for this cluster (may be empty if this is the first experience)

Your job is to distill the **best problem-solving process** from accumulated experiences into reusable **Skills**. A skill is an optimized, generalized version of the problem-solving steps — refined through seeing what works (and what doesn't) across multiple similar cases.

**Experience vs. Skill:**
- Experience = concrete case: "Fixed PostgreSQL connection pool timeout: checked pg_stat_activity logs, found spikes at 2-4pm correlating with 3x traffic, increased pool from 10 to 50, timeouts dropped 95%"
- Skill = best practice process: "Diagnose database connection pool exhaustion: 1) Check database activity logs for timeout patterns, 2) Correlate error timing with load patterns, 3) Compare pool size against peak concurrent connections, 4) Set pool size to 2x peak demand, 5) Monitor after change to verify resolution\\n\\nPitfalls:\\n- Do not blindly increase buffer size without checking traffic patterns — it masks the root cause and wastes memory\\n- Avoid restarting the service as a fix — it temporarily clears the pool but the issue recurs under load"

Each skill should be:
- **A process**: Step-by-step — not abstract theory, but a concrete procedure an agent can follow
- **Optimized**: Refined from multiple cases — keeps what works, drops dead ends and unnecessary steps
- **Generalized**: Replaces case-specific values (file names, error messages, numbers) with general descriptions, so it applies to similar but different instances
- **Self-contained**: Clear name + when to apply + the steps themselves
- **Searchable**: Keep specific technology names, tool names, and common search terms in the description field. An agent searching for "PostgreSQL connection pool" should find a skill about database connection management. Do NOT over-generalize away all domain-specific keywords.
- **Pitfall-aware**: When failed experiences reveal common mistakes or traps, append a "Pitfalls:" section at the end of the content listing what to avoid and why. This helps agents sidestep known failure modes.

New AgentCase(s) to integrate:
{new_experience_json}

Existing skills for this cluster (accumulated from previous experiences):
{existing_skills_json}

Instructions:
- If existing skills are empty, extract initial skills from the new experience (generalize from this first case)
- If existing skills are present, compare the new experience against each existing skill:
  - **Similar problem → Update the skill**: If the new experience solves a problem similar to an existing skill, iterate on that skill — refine its steps, increase confidence if confirmed, revise if the new case reveals a better approach
  - **Different problem → Add a new skill**: If the new experience solves a problem not covered by any existing skill, create a new skill for it
  - **Contradicted → Lower confidence or remove**: If the new experience contradicts an existing skill, lower its confidence or remove it
- **Quality-weighted**: Give more weight to high quality_score experiences (closer to 1.0) when determining the best approach. Low quality_score experiences (< 0.3) may represent failed attempts — do NOT adopt their steps as the recommended process; instead, distill their failure patterns into the "Pitfalls:" section of the relevant skill's content
- Replace case-specific details with generalized descriptions, but keep technology/tool names
- The output must be the COMPLETE updated skill set (not just the delta)

**CRITICAL LANGUAGE RULE**: You MUST output in the SAME language as the input conversation content. If the conversation content is in Chinese, ALL output MUST be in Chinese. If in English, output in English. This is mandatory.

Return JSON:
{{
    "skills": [
        {{
            "name": "Short descriptive name (max 10 words)",
            "description": "When to apply this skill — the type of problem it solves.",
            "content": "Best-practice process: numbered steps that an agent can directly follow. If failed experiences revealed common mistakes, append a Pitfalls section listing what to avoid.",
            "confidence": 0.0-1.0
        }}
    ]
}}
"""
