"""Agent Demo Helpers

Shared utilities for agent demo scripts (search_agent_demo, coding_agent_demo, etc.).
Provides:
- AgentDemoRunner: stateful helper for API calls (send messages, fetch, search)
- Print helpers: stateless formatters for various memory types
"""

import uuid
from typing import List, Optional

import httpx
from common_utils.datetime_utils import get_now_with_timezone, to_iso_format


DEFAULT_BASE_URL = "http://localhost:1995"


# ==================== Print Helpers ====================


def print_separator(text: str = ""):
    if text:
        print(f"\n{'='*60}")
        print(f"{text}")
        print('=' * 60)
    else:
        print('-' * 60)


def print_episodic_memories(memories: list):
    """Print episodic memories."""
    if not memories:
        print("  (none)")
        return
    for i, m in enumerate(memories, 1):
        print(f"\n  [{i}] {m.get('summary') or m.get('episode') or 'N/A'}")
        if m.get("keywords"):
            print(f"      Keywords : {', '.join(m['keywords'])}")
        if m.get("timestamp"):
            print(f"      Time     : {m['timestamp']}")


def print_event_logs(memories: list):
    """Print event log memories (atomic facts)."""
    if not memories:
        print("  (none)")
        return
    for i, m in enumerate(memories, 1):
        print(f"\n  [{i}] {m.get('atomic_fact', 'N/A')}")
        if m.get("timestamp"):
            print(f"      Time : {m['timestamp']}")


def print_foresights(memories: list):
    """Print foresight memories."""
    if not memories:
        print("  (none)")
        return
    for i, m in enumerate(memories, 1):
        content = m.get("content") or m.get("foresight") or "N/A"
        print(f"\n  [{i}] {content}")
        validity = " ~ ".join(filter(None, [m.get("start_time"), m.get("end_time")]))
        if validity:
            print(f"      Validity : {validity}")
        if m.get("evidence"):
            print(f"      Evidence : {m['evidence']}")


def print_agent_cases(memories: list):
    """Print agent experience memories."""
    if not memories:
        print("  (none)")
        return
    for i, exp in enumerate(memories, 1):
        print(f"\n  [{i}] {exp.get('task_intent', 'N/A')}")
        print(f"      Parent   : {exp.get('parent_id', 'N/A')}")
        approach = exp.get("approach", "")
        if approach:
            print(f"      Approach : {approach}")
        if exp.get("quality_score") is not None:
            print(f"      Quality  : {exp['quality_score']}")


def print_agent_skills(memories: list):
    """Print agent skills."""
    if not memories:
        print("  (none)")
        return

    for i, m in enumerate(memories, 1):
        print(f"\n  [{i}] {m.get('name') or 'Unnamed'}")
        if m.get("description"):
            print(f"      Description: {m['description']}")
        print(f"      Content    : {m.get('content', 'N/A')}")
        print(f"      Confidence : {m.get('confidence', 0):.2f}")
        print(f"      Cluster    : {m.get('cluster_id', 'N/A')}")


def print_search_experience_results(hits: list):
    """Print search results for agent_case."""
    if not hits:
        print("  (no results)")
        return
    for i, h in enumerate(hits, 1):
        score = h.get("score", 0.0)
        task_intent = h.get("task_intent") or ""
        print(f"\n  [{i}] score={score:.4f}")
        print(f"      Intent   : {task_intent}")


def print_search_skill_results(hits: list):
    """Print search results for agent_skill."""
    if not hits:
        print("  (no results — skill extraction requires 2+ clustered experiences)")
        return
    for i, h in enumerate(hits, 1):
        score = h.get("score", 0.0)
        name = h.get("name") or "Unnamed"
        content = h.get("content") or ""
        print(f"\n  [{i}] score={score:.4f}  {name}")
        print(f"      {content}")
        if h.get("description"):
            print(f"      Description: {h['description']}")
        print(f"      Confidence : {h.get('confidence', 0.0):.2f}")


# Memory type -> (label, printer) mapping for fetch step
MEMORY_TYPE_PRINTERS = [
    ("episodic_memory",  "Episodic Memory",   print_episodic_memories),
    ("event_log",        "Event Log",          print_event_logs),
    ("foresight",        "Foresight",          print_foresights),
    ("agent_case", "Agent Experience",   print_agent_cases),
    ("agent_skill",      "Agent Skill",        print_agent_skills),
]


# ==================== AgentDemoRunner ====================


class AgentDemoRunner:
    """Stateful helper for running agent demo scripts.

    Encapsulates group/session config and provides API call methods.
    Each demo creates its own runner with unique group_id.

    Usage:
        runner = AgentDemoRunner(
            group_id_prefix="search_agent_demo",
            group_name="Search Agent Demo Session",
            description="Agent Memory Demo - Search Agent",
            tags=["demo", "agent"],
        )
        await runner.save_conversation_meta()
        await runner.send_agent_message(msg, 0, flush=True)
    """

    def __init__(
        self,
        group_id_prefix: str,
        group_name: str,
        description: str = "",
        tags: Optional[List[str]] = None,
        msg_prefix: str = "agent_msg",
        user_id: str = "demo_user",
        base_url: str = DEFAULT_BASE_URL,
    ):
        self.run_id = uuid.uuid4().hex[:8]
        self.group_id = f"{group_id_prefix}_{self.run_id}"
        self.group_name = group_name
        self.description = description
        self.tags = tags or ["demo", "agent"]
        self.msg_prefix = msg_prefix
        self.user_id = user_id
        self.base_url = base_url

        self.memorize_url = f"{base_url}/api/v0/memories"
        self.fetch_url = f"{base_url}/api/v0/memories"
        self.search_url = f"{base_url}/api/v0/memories/search"
        self.conversation_meta_url = f"{base_url}/api/v0/memories/conversation-meta"

    async def save_conversation_meta(self):
        """Initialize conversation metadata for this demo group."""
        now = get_now_with_timezone()

        meta = {
            "name": self.group_name,
            "description": self.description,
            "group_id": self.group_id,
            "created_at": to_iso_format(now),
            "default_timezone": "Asia/Shanghai",
            "user_details": {
                self.user_id: {"full_name": "Demo User", "role": "user", "extra": {}},
            },
            "tags": self.tags,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self.conversation_meta_url, json=meta)
            resp.raise_for_status()
            result = resp.json()
            if result.get("status") == "ok":
                print("  Initialized group config (group_id={})".format(self.group_id))
            else:
                print(f"  Warning: conversation-meta failed: {result.get('message')}")

    async def send_agent_message(
        self, msg: dict, msg_index: int, flush: bool = False
    ) -> bool:
        """Send a single agent message via POST /api/v0/memories."""
        create_time = to_iso_format(get_now_with_timezone())

        role = msg.get("role", "user")
        sender = self.user_id if role == "user" else "assistant"

        payload = {
            "message_id": f"{self.msg_prefix}_{self.run_id}_{msg_index:03d}",
            "create_time": create_time,
            "sender": sender,
            "sender_name": sender,
            "role": role,
            "content": msg.get("content") or "",
            "group_id": self.group_id,
            "group_name": self.group_name,
            "scene": "assistant",
            "raw_data_type": "AgentConversation",
            "flush": flush,
        }

        if msg.get("tool_calls"):
            payload["tool_calls"] = msg["tool_calls"]
        if msg.get("tool_call_id"):
            payload["tool_call_id"] = msg["tool_call_id"]

        try:
            async with httpx.AsyncClient(timeout=500.0) as client:
                resp = await client.post(self.memorize_url, json=payload)
                resp.raise_for_status()
                result = resp.json()

                if result.get("status") == "ok":
                    count = result.get("result", {}).get("count", 0)
                    role_label = f"[{role}]".ljust(12)
                    content_preview = (msg.get("content") or "(tool_calls)")[:50]
                    if count > 0:
                        print(f"  {role_label} {content_preview}  -> Extracted {count} memories")
                    else:
                        print(f"  {role_label} {content_preview}")
                    return True
                else:
                    print(f"  Failed: {result.get('message')}")
                    return False
        except httpx.ConnectError:
            print(f"  Cannot connect to API server ({self.base_url})")
            print(f"  Please start first: uv run python src/run.py")
            return False
        except Exception as e:
            print(f"  Error: {e}")
            return False

    async def fetch_memories(self, memory_type: str) -> list:
        """Fetch memories of a given type via GET /api/v0/memories."""
        params = {
            "group_ids": self.group_id,
            "memory_type": memory_type,
            "user_id": self.user_id,
            "page_size": 20,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(self.fetch_url, params=params)
                resp.raise_for_status()
                result = resp.json()
                if result.get("status") == "ok":
                    memories = result.get("result", {}).get("memories", [])
                    return memories if memories else []
                else:
                    print(f"  [{memory_type}] Fetch failed: {result.get('message')}")
                    return []
        except Exception as e:
            print(f"  [{memory_type}] Fetch error: {e}")
            return []

    async def search_memories(
        self,
        query: str,
        memory_type: str,
        top_k: int = 5,
        retrieve_method: str = "hybrid",
    ) -> list:
        """Search memories via GET /api/v0/memories/search."""
        params = {
            "query": query,
            "group_ids": self.group_id,
            "memory_types": memory_type,
            "retrieve_method": retrieve_method,
            "top_k": top_k,
            "user_id": self.user_id,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(self.search_url, params=params)
                resp.raise_for_status()
                result = resp.json()
                if result.get("status") == "ok":
                    return result.get("result", {}).get("memories", [])
                else:
                    print(f"  Search failed: {result.get('message')}")
                    return []
        except Exception as e:
            print(f"  Search error: {e}")
            return []
