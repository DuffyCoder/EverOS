"""
AgentSkillExtractor for EverMemOS

Incrementally extracts reusable skills from new AgentCase records,
merging into existing cluster skills.

Pipeline:
1. Format the NEW AgentCaseRecord(s) as JSON context
2. Format existing skills for the cluster (accumulated state)
3. Single LLM call: merge new experience into existing skills
4. Embed each skill item for semantic retrieval
5. Replace cluster skills in storage
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from common_utils.json_utils import parse_json_response

from memory_layer.llm.llm_provider import LLMProvider
from memory_layer.prompts import get_prompt_by
from core.observation.logger import get_logger

logger = get_logger(__name__)


class AgentSkillExtractor:
    """
    Incrementally extracts reusable skills from a MemScene.

    For each new experience added to a cluster, this extractor:
    - Takes only the NEW AgentCaseRecord(s)
    - Reads existing skills for the cluster (accumulated state)
    - Uses an LLM to merge new insights into existing skills
    - Embeds the results and saves them back to storage
    """

    def __init__(
        self,
        llm_provider: Optional[LLMProvider] = None,
        extract_prompt: Optional[str] = None,
    ):
        self.llm_provider = llm_provider
        self.extract_prompt = extract_prompt or get_prompt_by(
            "AGENT_SKILL_EXTRACT_PROMPT"
        )

    @staticmethod
    def _json_default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        return str(obj)

    def _format_experiences(self, experience_records: List[Any]) -> str:
        """Format new AgentCaseRecords as a concise JSON string for the LLM."""
        formatted = []
        for rec in experience_records:
            formatted.append(
                {
                    "timestamp": rec.timestamp.isoformat() if rec.timestamp else None,
                    "task_intent": getattr(rec, "task_intent", "") or "",
                    "approach": getattr(rec, "approach", "") or "",
                    "quality_score": getattr(rec, "quality_score", None),
                }
            )
        return json.dumps(
            formatted, ensure_ascii=False, indent=2, default=self._json_default
        )

    def _format_existing_skills(self, existing_records: List[Any]) -> str:
        """Format existing AgentSkillRecords for the LLM (for incremental merging)."""
        if not existing_records:
            return "[]"
        formatted = []
        for rec in existing_records:
            formatted.append(
                {
                    "name": rec.name,
                    "description": rec.description,
                    "content": rec.content,
                    "confidence": rec.confidence,
                }
            )
        return json.dumps(
            formatted, ensure_ascii=False, indent=2, default=self._json_default
        )

    async def _compute_embedding(self, text: str) -> Optional[Dict[str, Any]]:
        """Compute embedding for a skill item's name + description."""
        try:
            if not text:
                return None
            from agentic_layer.vectorize_service import get_vectorize_service

            vs = get_vectorize_service()
            vec = await vs.get_embedding(text)
            return {
                "embedding": vec.tolist() if hasattr(vec, "tolist") else list(vec),
                "vector_model": vs.get_model_name(),
            }
        except Exception as e:
            logger.error(f"[AgentSkillExtractor] Embedding failed: {e}")
            return None

    async def _call_llm(
        self, new_experience_json: str, existing_skills_json: str
    ) -> Optional[Dict[str, Any]]:
        """Single LLM call to merge new experience into existing skills."""
        prompt = self.extract_prompt.format(
            new_experience_json=new_experience_json,
            existing_skills_json=existing_skills_json,
        )
        for attempt in range(3):
            try:
                resp = await self.llm_provider.generate(prompt)
                data = parse_json_response(resp)
                if data and isinstance(data.get("skills"), list):
                    return data
                logger.warning(
                    f"[AgentSkillExtractor] LLM retry {attempt + 1}/3: invalid format"
                )
            except Exception as e:
                logger.warning(f"[AgentSkillExtractor] LLM retry {attempt + 1}/3: {e}")
        return None

    async def extract_and_save(
        self,
        cluster_id: str,
        group_id: Optional[str],
        new_experience_records: List[Any],
        existing_skill_records: List[Any],
        skill_repo: Any,
        user_id: Optional[str] = None,
    ) -> List[Any]:
        """Incrementally extract skills from new experience and merge into existing.

        Args:
            cluster_id: The MemScene cluster ID
            group_id: Group ID for scoping
            new_experience_records: Only the NEW AgentCaseRecord(s) to integrate
            existing_skill_records: Previously saved AgentSkillRecord for this cluster
            skill_repo: AgentSkillRawRepository instance
            user_id: User ID (agent owner)

        Returns:
            List of newly saved AgentSkillRecord. Empty list if no changes were made
            (LLM failed, no new experiences, or existing skills kept as-is).
        """
        if not new_experience_records:
            logger.debug(
                f"[AgentSkillExtractor] No new experiences for cluster={cluster_id}, skipping"
            )
            return []

        new_experience_json = self._format_experiences(new_experience_records)
        existing_skills_json = self._format_existing_skills(existing_skill_records)

        logger.debug(
            f"[AgentSkillExtractor] Incremental extraction: cluster={cluster_id}, "
            f"new_experiences={len(new_experience_records)}, existing_skills={len(existing_skill_records)}"
        )

        result = await self._call_llm(new_experience_json, existing_skills_json)
        if not result:
            logger.warning(
                f"[AgentSkillExtractor] LLM extraction failed for cluster={cluster_id}"
            )
            return []

        new_records = []

        for item in result.get("skills", []):
            name = item.get("name", "")
            content = item.get("content", "")
            if not content:
                continue

            confidence = float(item.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))

            description = item.get("description", "")
            embed_text = "\n".join(s for s in [name, description] if s)
            embedding_data = await self._compute_embedding(embed_text)

            from infra_layer.adapters.out.persistence.document.memory.agent_skill import (
                AgentSkillRecord,
            )
            record = AgentSkillRecord(
                cluster_id=cluster_id,
                user_id=user_id,
                group_id=group_id,
                name=name,
                description=item.get("description"),
                content=content,
                confidence=confidence,
                vector=(embedding_data["embedding"] if embedding_data else None),
                vector_model=(
                    embedding_data["vector_model"] if embedding_data else None
                ),
            )
            new_records.append(record)

        if not new_records and existing_skill_records:
            logger.warning(
                f"[AgentSkillExtractor] LLM returned empty skills but "
                f"{len(existing_skill_records)} existing skills present for "
                f"cluster={cluster_id}, keeping existing skills"
            )
            return []

        old_record_ids = [r.id for r in existing_skill_records] if existing_skill_records else []
        saved = await skill_repo.replace_cluster_skills(
            cluster_id, new_records, old_record_ids=old_record_ids
        )
        logger.info(
            f"[AgentSkillExtractor] Saved {len(saved)} skill items for cluster={cluster_id}"
        )
        return saved
