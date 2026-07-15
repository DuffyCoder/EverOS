from evaluation.src.adapters.openclaw.config_patches import (
    build_agent_llm_runtime_jq,
)


def test_build_agent_llm_runtime_jq_sets_model_max_tokens_and_idle_timeout():
    jq_filter = build_agent_llm_runtime_jq(
        model_max_tokens=8192,
        idle_timeout_seconds=180,
    )

    assert ".maxTokens = 8192" in jq_filter
    assert ".agents.defaults.llm.idleTimeoutSeconds = 180" in jq_filter


def test_build_agent_llm_runtime_jq_omits_unset_values():
    jq_filter = build_agent_llm_runtime_jq()

    assert jq_filter == "."
