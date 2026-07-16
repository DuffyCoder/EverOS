from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from evaluation.src.config.system_index import (
    DEFAULT_SYSTEM_INDEX_PATH,
    load_system_index,
)
from evaluation.src.config.system_loader import SystemConfigError, resolve_system_config
from evaluation.src.config.system_policy import (
    PolicyFinding,
    SystemPolicyError,
    validate_raw_system_policy,
    validate_runtime_system_policy,
)

FAKE_ENVIRONMENT = {
    "EVERMEMOS_API_KEY": "evermemos-key",
    "EVERMEMOS_API_URL": "https://memory.example/api/v1/memories",
    "HERMES_REPO_PATH": "/tmp/hermes",
    "LLM_API_KEY": "llm-key",
    "LLM_BASE_URL": "https://llm.example/v1",
    "LLM_MODEL": "test-model",
    "MEM0_API_KEY": "mem0-key",
    "MEMOS_KEY": "memos-key",
    "MEMU_API_KEY": "memu-key",
    "OPENCLAW_EMBED_MODEL": "embedding-model",
    "OPENCLAW_EMBED_PROVIDER": "test-provider",
    "OPENCLAW_REPO_PATH": "/tmp/openclaw",
    "OPENVIKING_INGEST_URL": "http://127.0.0.1:1933",
    "SOPH_API_KEY": "embed-key",
    "SOPH_EMBED_EASYLLM_ID": "deployment",
    "SOPH_EMBED_URL": "https://embed.example/v1",
    "ZEP_API_KEY": "zep-key",
}

LEGACY_EMBEDDING_REQUESTED_IDS = {
    "openclaw",
    "openclaw-hybrid",
    "openclaw-hybrid-noflush",
    "openclaw-native-embed",
    "openclaw-vector",
    "openclaw-vector-noflush",
}
LEGACY_REQUESTED_IDS = LEGACY_EMBEDDING_REQUESTED_IDS | {"openclaw-docker-stub"}


def _raw_config(**updates: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "adapter": "mem0",
        "api_key": "${MEM0_API_KEY}",
        "llm": {"api_key": "${LLM_API_KEY:}", "max_tokens": 1024},
    }
    config.update(updates)
    return config


@pytest.mark.parametrize("marker", ["${API_KEY}", "${API_KEY:}"])
def test_secret_fields_accept_only_empty_default_environment_markers(
    marker: str,
) -> None:
    config = _raw_config(api_key=marker)

    assert validate_raw_system_policy("mem0", config, canonical_id="mem0") == ()


@pytest.mark.parametrize(
    "bad_marker",
    [
        "plaintext-secret",
        "prefix-${API_KEY}",
        "${API_KEY:default}",
        "${1BAD}",
        "${BAD-NAME}",
        123,
    ],
)
def test_secret_fields_reject_plaintext_partial_and_defaulted_markers(
    bad_marker: object,
) -> None:
    config = _raw_config(api_key=bad_marker)

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    assert error.value.findings[0].pointer == "/api_key"
    assert "plaintext-secret" not in str(error.value)


@pytest.mark.parametrize(
    "secret_key",
    [
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "private_key",
        "credential",
    ],
)
def test_exact_nested_secret_key_segments_are_protected(secret_key: str) -> None:
    config = _raw_config(
        plugin_config={"nested": {secret_key: "must-not-appear-in-error"}}
    )

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    assert error.value.findings[0].pointer == f"/plugin_config/nested/{secret_key}"
    assert "must-not-appear-in-error" not in str(error.value)


@pytest.mark.parametrize(
    "secret_key",
    [
        "apiKey",
        "api-key",
        "accessToken",
        "access-token",
        "clientSecret",
        "private-key",
        "openaiApiKey",
        "dbPassword",
        "botToken",
        "appToken",
        "webhookToken",
        "slackBotToken",
        "discordAppToken",
        "fooWebhookToken",
        "apiToken",
        "userToken",
        "verificationToken",
        "accountSecret",
        "signingSecret",
        "webhookSecret",
        "appSecret",
        "channelSecret",
        "botSecret",
        "serviceCredential",
        "serviceCredentials",
        "awsSecretAccessKey",
    ],
)
def test_camel_and_kebab_secret_key_segments_are_protected(secret_key: str) -> None:
    config = _raw_config(
        plugin_config={"nested": {secret_key: "must-not-appear-in-error"}}
    )

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    assert error.value.findings[0].pointer == f"/plugin_config/nested/{secret_key}"
    assert "must-not-appear-in-error" not in str(error.value)


@pytest.mark.parametrize(
    "secret_key",
    [
        "authorization",
        "Authorization",
        "proxyAuthorization",
        "proxy-authorization",
        "Proxy-Authorization",
    ],
)
def test_authorization_secret_leaves_require_environment_markers(
    secret_key: str,
) -> None:
    config = _raw_config(
        plugin_config={"headers": {secret_key: "${AUTHORIZATION_HEADER}"}}
    )
    assert validate_raw_system_policy("mem0", config, canonical_id="mem0") == ()

    plaintext = "Bearer must-not-appear-in-error"
    config["plugin_config"]["headers"][secret_key] = plaintext
    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    assert error.value.findings[0].pointer == (f"/plugin_config/headers/{secret_key}")
    assert plaintext not in str(error.value)


@pytest.mark.parametrize("container_key", ["credential", "credentials"])
def test_credential_mappings_are_structural_containers(container_key: str) -> None:
    config = _raw_config(plugin_config={container_key: {"apiKey": "${PLUGIN_API_KEY}"}})
    assert validate_raw_system_policy("mem0", config, canonical_id="mem0") == ()

    plaintext = "must-not-appear-in-error"
    config["plugin_config"][container_key]["apiKey"] = plaintext
    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    assert error.value.findings[0].pointer == (f"/plugin_config/{container_key}/apiKey")
    assert plaintext not in str(error.value)


def test_credential_lists_require_markers_for_scalar_entries() -> None:
    config = _raw_config(
        plugin_config={
            "credentials": [
                "${PRIMARY_CREDENTIAL}",
                {"apiKey": "${SECONDARY_CREDENTIAL}"},
                ["${FALLBACK_CREDENTIAL}"],
            ]
        }
    )
    assert validate_raw_system_policy("mem0", config, canonical_id="mem0") == ()

    plaintext = "must-not-appear-in-error"
    config["plugin_config"]["credentials"][0] = plaintext
    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    assert error.value.findings[0].pointer == "/plugin_config/credentials/0"
    assert plaintext not in str(error.value)


@pytest.mark.parametrize(
    "bad_value", [{"value": "must-not-appear-in-error"}, ["${PLUGIN_API_KEY}"]]
)
def test_noncontainer_secret_keys_reject_structured_values(bad_value: object) -> None:
    config = _raw_config(plugin_config={"apiKey": bad_value})

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    assert error.value.findings[0].pointer == "/plugin_config/apiKey"
    assert "must-not-appear-in-error" not in str(error.value)


@pytest.mark.parametrize("env_key", ["apiKeyEnv", "api-key-env"])
def test_camel_and_kebab_env_reference_fields_require_bare_names(env_key: str) -> None:
    config = _raw_config(plugin_config={env_key: "${API_KEY}"})

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    assert error.value.findings[0].pointer == f"/plugin_config/{env_key}"


def test_raw_policy_traverses_secrets_inside_dataset_overrides() -> None:
    config = _raw_config(
        dataset_overrides={"future-dataset": {"apiKey": "must-not-appear-in-error"}}
    )

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    assert error.value.findings[0].pointer == (
        "/dataset_overrides/future-dataset/apiKey"
    )
    assert "must-not-appear-in-error" not in str(error.value)


def test_token_like_nonsecret_names_are_not_misclassified() -> None:
    config = _raw_config(
        max_tokens=4096,
        plugin_config={
            "token_count": 12,
            "tokenCount": 12,
            "pageToken": "pagination-cursor",
            "fileToken": "file-identifier",
            "docToken": "document-identifier",
        },
        openclaw={"honor_silent_token": False, "honorSilentToken": False},
    )

    assert validate_raw_system_policy("mem0", config, canonical_id="mem0") == ()


@pytest.mark.parametrize(
    "pointer",
    [
        ("agent_llm", "api_key_env"),
        ("embedding", "api_key_env"),
        ("ov_ingest", "api_key_env"),
        ("plugin_config", "nested_env"),
    ],
)
@pytest.mark.parametrize("bad_value", ["${ENV_NAME}", "bad-name", "1BAD", "", 7])
def test_every_recursive_env_reference_must_be_a_bare_valid_name(
    pointer: tuple[str, str], bad_value: object
) -> None:
    block, field = pointer
    config = _raw_config(openclaw={block: {field: bad_value}})

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    assert error.value.findings[0].pointer == f"/openclaw/{block}/{field}"


def test_valid_recursive_env_references_are_accepted() -> None:
    config = _raw_config(
        openclaw={
            "agent_llm": {
                "api_key_env": "LLM_API_KEY",
                "env_vars": ["LLM_API_KEY", "_SECONDARY_KEY"],
            },
            "embedding": {"api_key_env": "_EMBED_KEY"},
            "ov_ingest": {"api_key_env": "OPENVIKING_API_KEY"},
        }
    )

    assert validate_raw_system_policy("mem0", config, canonical_id="mem0") == ()


@pytest.mark.parametrize("bad_value", ["${ENV_NAME}", "bad-name", " ", 7])
def test_recursive_env_var_lists_require_bare_valid_names(bad_value: object) -> None:
    config = _raw_config(openclaw={"agent_llm": {"env_vars": ["VALID_ENV", bad_value]}})

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    assert error.value.findings[0].pointer == "/openclaw/agent_llm/env_vars/1"


def test_strict_mode_rejects_embedding_api_key_even_when_it_is_a_marker() -> None:
    config = _raw_config(
        adapter="openclaw", openclaw={"embedding": {"api_key": "${SOPH_API_KEY}"}}
    )

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("openclaw", config, canonical_id="openclaw")

    assert len(error.value.findings) == 1
    assert error.value.findings[0].code == "legacy-embedding-api-key"
    assert error.value.findings[0].pointer == "/openclaw/embedding/api_key"


@pytest.mark.parametrize(
    "canonical_id",
    [
        "openclaw",
        "openclaw-hybrid-noflush",
        "openclaw-native-embed",
        "openclaw-vector",
        "openclaw-vector-noflush",
    ],
)
def test_legacy_mode_allows_only_the_exact_known_embedding_marker(
    canonical_id: str,
) -> None:
    config = _raw_config(
        adapter="openclaw", openclaw={"embedding": {"api_key": "${SOPH_API_KEY}"}}
    )

    findings = validate_raw_system_policy(
        "openclaw", config, canonical_id=canonical_id, allow_legacy=True
    )

    assert len(findings) == 1
    assert findings[0].code == "legacy-embedding-api-key"
    assert findings[0].pointer == "/openclaw/embedding/api_key"


@pytest.mark.parametrize(
    ("canonical_id", "value"),
    [
        ("openclaw-fts", "${SOPH_API_KEY}"),
        ("openclaw", "${OTHER_API_KEY}"),
        ("openclaw", "plaintext-secret"),
        ("openclaw", "${SOPH_API_KEY:}"),
    ],
)
def test_legacy_mode_does_not_broaden_the_embedding_exception(
    canonical_id: str, value: str
) -> None:
    config = _raw_config(adapter="openclaw", openclaw={"embedding": {"api_key": value}})

    with pytest.raises(SystemPolicyError):
        validate_raw_system_policy(
            "openclaw", config, canonical_id=canonical_id, allow_legacy=True
        )


def test_legacy_embedding_exception_requires_the_openclaw_adapter() -> None:
    config = _raw_config(
        adapter="openclaw", openclaw={"embedding": {"api_key": "${SOPH_API_KEY}"}}
    )

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy(
            "mem0", config, canonical_id="openclaw", allow_legacy=True
        )

    assert error.value.findings[0].code == "forbidden-embedding-api-key"


@pytest.mark.parametrize("allow_legacy", [False, True])
def test_dataset_override_embedding_api_key_never_uses_the_legacy_exception(
    allow_legacy: bool,
) -> None:
    config = _raw_config(
        adapter="openclaw",
        dataset_overrides={
            "future-dataset": {
                "openclaw": {"embedding": {"api_key": "${SOPH_API_KEY}"}}
            }
        },
    )

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy(
            "openclaw", config, canonical_id="openclaw", allow_legacy=allow_legacy
        )

    assert len(error.value.findings) == 1
    assert error.value.findings[0].code == "forbidden-embedding-api-key"
    assert error.value.findings[0].pointer == (
        "/dataset_overrides/future-dataset/openclaw/embedding/api_key"
    )


@pytest.mark.parametrize(
    "image",
    [
        "registry.example/openclaw:stable",
        "openclaw-eval:7da23c3-memory-core-0000000-slim",
        "registry.example/openclaw@sha256:" + "a" * 64,
    ],
)
def test_docker_image_policy_accepts_tags_digests_and_zero_revision(image: str) -> None:
    config = _raw_config(adapter="openclaw-docker", openclaw_docker={"image": image})

    assert (
        validate_raw_system_policy("openclaw-docker", config, canonical_id="docker")
        == ()
    )
    assert (
        validate_runtime_system_policy("openclaw-docker", config, canonical_id="docker")
        == ()
    )


@pytest.mark.parametrize(
    "image", ["openclaw:PLUGIN_REV", "openclaw:TODO", "openclaw:${IMAGE_TAG}"]
)
def test_docker_image_policy_rejects_build_placeholders(image: str) -> None:
    config = _raw_config(adapter="openclaw-docker", openclaw_docker={"image": image})

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("openclaw-docker", config, canonical_id="docker")
    assert error.value.findings[0].pointer == "/openclaw_docker/image"


def test_stub_image_legacy_exception_is_exact() -> None:
    exact = _raw_config(
        adapter="openclaw-docker",
        openclaw_docker={"image": "openclaw-eval:7da23c3-stub-PLUGIN_REV-slim"},
    )
    findings = validate_raw_system_policy(
        "openclaw-docker", exact, canonical_id="openclaw-docker-stub", allow_legacy=True
    )
    assert len(findings) == 1
    assert findings[0].code == "legacy-docker-image-placeholder"

    changed = deepcopy(exact)
    changed["openclaw_docker"]["image"] = "openclaw-eval:other-PLUGIN_REV"
    with pytest.raises(SystemPolicyError):
        validate_raw_system_policy(
            "openclaw-docker",
            changed,
            canonical_id="openclaw-docker-stub",
            allow_legacy=True,
        )


def test_runtime_stub_image_legacy_exception_is_exact() -> None:
    exact = _raw_config(
        adapter="openclaw-docker",
        openclaw_docker={"image": "openclaw-eval:7da23c3-stub-PLUGIN_REV-slim"},
    )

    with pytest.raises(SystemPolicyError):
        validate_runtime_system_policy(
            "openclaw-docker", exact, canonical_id="openclaw-docker-stub"
        )

    findings = validate_runtime_system_policy(
        "openclaw-docker", exact, canonical_id="openclaw-docker-stub", allow_legacy=True
    )
    assert len(findings) == 1
    assert findings[0].code == "legacy-docker-image-placeholder"

    with pytest.raises(SystemPolicyError):
        validate_runtime_system_policy(
            "openclaw-docker", exact, canonical_id="another-system", allow_legacy=True
        )
    with pytest.raises(SystemPolicyError):
        validate_runtime_system_policy(
            "openclaw", exact, canonical_id="openclaw-docker-stub", allow_legacy=True
        )


def test_runtime_policy_nonmapping_config_is_a_structured_domain_error() -> None:
    config = ["must-not-appear-in-error"]

    with pytest.raises(SystemPolicyError) as error:
        validate_runtime_system_policy(  # type: ignore[arg-type]
            "openclaw-docker", config, canonical_id="docker"
        )

    assert error.value.findings == (
        PolicyFinding(
            code="config-not-mapping",
            pointer="",
            message="runtime system config must be a mapping",
        ),
    )
    assert config[0] not in str(error.value)


@pytest.mark.parametrize("allow_legacy", [False, True])
@pytest.mark.parametrize(
    "image",
    [
        "openclaw-eval:7da23c3-stub-PLUGIN_REV-slim",
        "openclaw:TODO",
        "openclaw:${IMAGE_TAG}",
    ],
)
def test_dataset_override_docker_image_placeholders_never_use_the_legacy_exception(
    image: str, allow_legacy: bool
) -> None:
    config = _raw_config(
        adapter="openclaw-docker",
        dataset_overrides={"future-dataset": {"openclaw_docker": {"image": image}}},
    )

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy(
            "openclaw-docker",
            config,
            canonical_id="openclaw-docker-stub",
            allow_legacy=allow_legacy,
        )

    assert len(error.value.findings) == 1
    assert error.value.findings[0].code == "docker-image-placeholder"
    assert error.value.findings[0].pointer == (
        "/dataset_overrides/future-dataset/openclaw_docker/image"
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:1995",
        "https://localhost/api/v1/memories",
        "http://127.0.0.1:1995",
        "http://[::1]:1995",
        "http://[0:0:0:0:0:0:0:1]/",
    ],
)
def test_empty_evermemos_api_key_is_allowed_only_for_strict_loopback_urls(
    base_url: str,
) -> None:
    config = _raw_config(adapter="evermemos_api", api_key="", base_url=base_url)

    assert (
        validate_raw_system_policy(
            "evermemos_api", config, canonical_id="evermemos_local_api"
        )
        == ()
    )


def test_loopback_empty_key_exception_does_not_apply_to_nested_llm_key() -> None:
    config = _raw_config(
        adapter="evermemos_api",
        api_key="${EVERMEMOS_API_KEY}",
        base_url="https://memory.example/api/v1/memories",
        llm={"api_key": "", "base_url": "http://localhost:8000"},
    )

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy(
            "evermemos_api", config, canonical_id="evermemos_local_api"
        )

    assert error.value.findings[0].pointer == "/llm/api_key"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.evermind.ai",
        "http://localhost.example.com",
        "http://localhost@evil.example",
        "http://user:pass@localhost:1995",
        "http://127.0.0.2:1995",
        "localhost:1995",
        "//localhost:1995",
        "ftp://localhost:1995",
        "http://localhost:bad-port",
        "http:///missing-host",
    ],
)
def test_empty_evermemos_api_key_rejects_hosted_lookalike_and_malformed_urls(
    base_url: str,
) -> None:
    config = _raw_config(adapter="evermemos_api", api_key="", base_url=base_url)

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy(
            "evermemos_api", config, canonical_id="evermemos_local_api"
        )
    assert error.value.findings[0].pointer == "/api_key"


@pytest.mark.parametrize(
    "value",
    [
        "-----BEGIN PRIVATE KEY-----\nredacted\n-----END PRIVATE KEY-----",
        "https://user:password@example.com/api",
        "postgresql://user:password@db.example/memory",
    ],
)
def test_high_confidence_plaintext_secret_fallbacks(value: str) -> None:
    config = _raw_config(notes=value)

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    assert value not in str(error.value)


@pytest.mark.parametrize(
    "value", ["postgresql://db.example/memory", "redis://cache.example:6379/0"]
)
def test_non_http_uris_without_userinfo_are_allowed(value: str) -> None:
    config = _raw_config(connection_uri=value)

    assert validate_raw_system_policy("mem0", config, canonical_id="mem0") == ()


def test_findings_are_sorted_stable_and_redacted() -> None:
    config = _raw_config(
        api_key="top-level-secret",
        nested={"password": "nested-secret", "api_key_env": "bad-name"},
    )

    with pytest.raises(SystemPolicyError) as error:
        validate_raw_system_policy("mem0", config, canonical_id="mem0")

    findings = error.value.findings
    assert findings == tuple(
        sorted(findings, key=lambda item: (item.pointer, item.code))
    )
    message = str(error.value)
    assert "top-level-secret" not in message
    assert "nested-secret" not in message
    assert all(
        "secret" not in finding.message.lower() or "value" in finding.message.lower()
        for finding in findings
    )


def test_all_shipped_systems_have_the_exact_legacy_policy_surface() -> None:
    index = load_system_index(DEFAULT_SYSTEM_INDEX_PATH)
    seen_findings: dict[str, tuple[PolicyFinding, ...]] = {}

    for system_id in sorted(index.systems):
        resolved = resolve_system_config(
            system_id, environ=FAKE_ENVIRONMENT, allow_legacy=True
        )
        seen_findings[system_id] = resolved.policy_findings
        assert resolved.config["adapter"] == resolved.adapter

    assert set(seen_findings) == set(index.systems)
    assert {
        system_id for system_id, findings in seen_findings.items() if findings
    } == LEGACY_REQUESTED_IDS
    assert all(len(findings) == 1 for findings in seen_findings.values() if findings)
    assert {
        system_id
        for system_id, findings in seen_findings.items()
        if findings and findings[0].code == "legacy-embedding-api-key"
    } == LEGACY_EMBEDDING_REQUESTED_IDS
    assert seen_findings["openclaw-docker-stub"][0].code == (
        "legacy-docker-image-placeholder"
    )


def test_strict_loader_rejects_exactly_the_seven_shipped_legacy_ids() -> None:
    index = load_system_index(DEFAULT_SYSTEM_INDEX_PATH)
    rejected: set[str] = set()

    for system_id in sorted(index.systems):
        try:
            resolved = resolve_system_config(system_id, environ=FAKE_ENVIRONMENT)
        except SystemConfigError:
            rejected.add(system_id)
        else:
            assert resolved.policy_findings == ()

    assert rejected == LEGACY_REQUESTED_IDS


def test_loader_applies_raw_policy_before_environment_substitution(
    tmp_path: Path,
) -> None:
    index = load_system_index(DEFAULT_SYSTEM_INDEX_PATH)
    systems_root = tmp_path / "systems"
    systems_root.mkdir()
    index_text = DEFAULT_SYSTEM_INDEX_PATH.read_text(encoding="utf-8").replace(
        "path: mem0.yaml", "path: custom.yaml", 1
    )
    index_path = systems_root / "index.yaml"
    index_path.write_text(index_text, encoding="utf-8")
    (systems_root / "custom.yaml").write_text(
        """
adapter: mem0
api_key: plaintext-secret
llm:
  provider: openai
  model: test-model
  api_key: ${LLM_API_KEY}
  base_url: https://llm.example/v1
  max_tokens: 1024
search:
  top_k: 10
answer:
  max_retries: 3
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(SystemConfigError) as error:
        resolve_system_config(
            "mem0",
            index_path=index_path,
            systems_root=systems_root,
            environ={**FAKE_ENVIRONMENT, "plaintext-secret": "${SAFE_KEY}"},
        )

    assert "plaintext-secret" not in str(error.value)
