"""Raw and runtime policy checks for evaluation system configurations."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_MARKER = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::)?\}$")
_UNRESOLVED_MARKER = re.compile(r"\$\{")
_PRIVATE_KEY_PEM = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "refresh_token",
        "auth_token",
        "authorization",
        "app_token",
        "api_token",
        "bot_token",
        "token",
        "user_token",
        "verification_token",
        "client_secret",
        "password",
        "private_key",
        "proxy_authorization",
        "credential",
        "credentials",
        "secret",
        "secret_key",
        "webhook_token",
    }
)
_SECRET_KEY_SUFFIXES = (
    "_secret",
    "_credential",
    "_credentials",
    "_api_key",
    "_api_token",
    "_access_token",
    "_refresh_token",
    "_auth_token",
    "_app_token",
    "_bot_token",
    "_client_secret",
    "_private_key",
    "_secret_access_key",
    "_secret_key",
    "_user_token",
    "_verification_token",
    "_webhook_token",
    "_password",
)
_NON_SECRET_KEYS = frozenset({"honor_silent_token"})
_STRUCTURED_SECRET_KEYS = frozenset(
    {"authorization", "proxy_authorization", "credential", "credentials"}
)
_STRUCTURED_SECRET_KEY_SUFFIXES = ("_credential", "_credentials")
_LEGACY_EMBEDDING_CANONICAL_IDS = frozenset(
    {
        "openclaw",
        "openclaw-hybrid-noflush",
        "openclaw-native-embed",
        "openclaw-vector",
        "openclaw-vector-noflush",
    }
)
_LEGACY_EMBEDDING_POINTER = "/openclaw/embedding/api_key"
_LEGACY_EMBEDDING_VALUE = "${SOPH_API_KEY}"
_LEGACY_STUB_ID = "openclaw-docker-stub"
_LEGACY_STUB_POINTER = "/openclaw_docker/image"
_LEGACY_STUB_IMAGE = "openclaw-eval:7da23c3-stub-PLUGIN_REV-slim"


@dataclass(frozen=True)
class PolicyFinding:
    code: str
    pointer: str
    message: str


class SystemPolicyError(ValueError):
    """Raised when a config violates raw or runtime safety policy."""

    def __init__(self, findings: tuple[PolicyFinding, ...]):
        self.findings = tuple(
            sorted(findings, key=lambda finding: (finding.pointer, finding.code))
        )
        rendered = "; ".join(
            f"{finding.pointer} [{finding.code}]: {finding.message}"
            for finding in self.findings
        )
        super().__init__(f"system configuration policy violation: {rendered}")


def validate_raw_system_policy(
    adapter: str,
    config: Mapping[str, Any],
    *,
    canonical_id: str,
    allow_legacy: bool = False,
) -> tuple[PolicyFinding, ...]:
    """Validate merged, unexpanded YAML and return visible legacy findings."""
    if not isinstance(config, Mapping):
        raise SystemPolicyError(
            (
                PolicyFinding(
                    code="config-not-mapping",
                    pointer="",
                    message="raw system config must be a mapping",
                ),
            )
        )

    violations: list[PolicyFinding] = []
    legacy: list[PolicyFinding] = []
    _inspect_raw(
        config,
        pointer="",
        adapter=adapter,
        canonical_id=canonical_id,
        allow_legacy=allow_legacy,
        violations=violations,
        legacy=legacy,
    )
    if violations:
        raise SystemPolicyError(tuple(violations))
    return tuple(sorted(legacy, key=lambda finding: (finding.pointer, finding.code)))


def validate_runtime_system_policy(
    adapter: str,
    config: Mapping[str, Any],
    *,
    canonical_id: str,
    allow_legacy: bool = False,
) -> tuple[PolicyFinding, ...]:
    """Validate runtime-safe non-secret policy, currently Docker images."""
    if not isinstance(config, Mapping):
        raise SystemPolicyError(
            (
                PolicyFinding(
                    code="config-not-mapping",
                    pointer="",
                    message="runtime system config must be a mapping",
                ),
            )
        )

    violations: list[PolicyFinding] = []
    legacy: list[PolicyFinding] = []
    _inspect_docker_image(
        config,
        adapter=adapter,
        canonical_id=canonical_id,
        allow_legacy=allow_legacy,
        violations=violations,
        legacy=legacy,
    )
    if violations:
        raise SystemPolicyError(tuple(violations))
    return tuple(sorted(legacy, key=lambda finding: (finding.pointer, finding.code)))


def _inspect_env_var_names(
    value: Any, pointer: str, violations: list[PolicyFinding]
) -> None:
    if not isinstance(value, list):
        violations.append(
            PolicyFinding(
                code="invalid-env-reference",
                pointer=pointer,
                message="environment references must be a list of bare valid names",
            )
        )
        return
    for index, name in enumerate(value):
        if not isinstance(name, str) or _ENV_NAME.fullmatch(name) is None:
            violations.append(
                PolicyFinding(
                    code="invalid-env-reference",
                    pointer=f"{pointer}/{index}",
                    message="environment reference must be a bare valid name",
                )
            )


def _inspect_secret_list(
    value: list[Any], pointer: str, violations: list[PolicyFinding]
) -> None:
    for index, nested in enumerate(value):
        nested_pointer = f"{pointer}/{index}"
        if isinstance(nested, Mapping):
            continue
        if isinstance(nested, list):
            _inspect_secret_list(nested, nested_pointer, violations)
            continue
        if not isinstance(nested, str) or _SECRET_MARKER.fullmatch(nested) is None:
            violations.append(
                PolicyFinding(
                    code="invalid-secret-reference",
                    pointer=nested_pointer,
                    message="secret value must use ${ENV_NAME} or ${ENV_NAME:}",
                )
            )


def _inspect_raw(
    value: Any,
    *,
    pointer: str,
    adapter: str,
    canonical_id: str,
    allow_legacy: bool,
    violations: list[PolicyFinding],
    legacy: list[PolicyFinding],
) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            segment = str(key)
            normalized_segment = _normalize_policy_key(segment)
            nested_pointer = f"{pointer}/{_escape_pointer(segment)}"
            if normalized_segment == "env_vars":
                _inspect_env_var_names(nested, nested_pointer, violations)
            elif normalized_segment.endswith("_env"):
                if not isinstance(nested, str) or _ENV_NAME.fullmatch(nested) is None:
                    violations.append(
                        PolicyFinding(
                            code="invalid-env-reference",
                            pointer=nested_pointer,
                            message="environment reference must be a bare valid name",
                        )
                    )
            elif nested_pointer.endswith(_LEGACY_EMBEDDING_POINTER):
                if (
                    nested_pointer == _LEGACY_EMBEDDING_POINTER
                    and adapter == "openclaw"
                    and canonical_id in _LEGACY_EMBEDDING_CANONICAL_IDS
                    and nested == _LEGACY_EMBEDDING_VALUE
                ):
                    finding = PolicyFinding(
                        code="legacy-embedding-api-key",
                        pointer=nested_pointer,
                        message=(
                            "legacy embedding api_key marker must migrate to "
                            "api_key_env"
                        ),
                    )
                    if allow_legacy:
                        legacy.append(finding)
                    else:
                        violations.append(finding)
                else:
                    violations.append(
                        PolicyFinding(
                            code="forbidden-embedding-api-key",
                            pointer=nested_pointer,
                            message=("embedding credentials must use api_key_env"),
                        )
                    )
            elif nested_pointer.endswith(_LEGACY_STUB_POINTER):
                _inspect_docker_image_value(
                    nested,
                    pointer=nested_pointer,
                    adapter=adapter,
                    canonical_id=canonical_id,
                    allow_legacy=allow_legacy,
                    violations=violations,
                    legacy=legacy,
                )
            elif _is_secret_key(normalized_segment):
                if _is_loopback_empty_key_exception(
                    adapter, value, nested_pointer, segment, nested
                ):
                    pass
                elif _is_structured_secret_key(normalized_segment) and isinstance(
                    nested, Mapping
                ):
                    pass
                elif _is_structured_secret_key(normalized_segment) and isinstance(
                    nested, list
                ):
                    _inspect_secret_list(nested, nested_pointer, violations)
                elif (
                    not isinstance(nested, str)
                    or _SECRET_MARKER.fullmatch(nested) is None
                ):
                    violations.append(
                        PolicyFinding(
                            code="invalid-secret-reference",
                            pointer=nested_pointer,
                            message=(
                                "secret value must use ${ENV_NAME} or ${ENV_NAME:}"
                            ),
                        )
                    )
            _inspect_raw(
                nested,
                pointer=nested_pointer,
                adapter=adapter,
                canonical_id=canonical_id,
                allow_legacy=allow_legacy,
                violations=violations,
                legacy=legacy,
            )
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _inspect_raw(
                nested,
                pointer=f"{pointer}/{index}",
                adapter=adapter,
                canonical_id=canonical_id,
                allow_legacy=allow_legacy,
                violations=violations,
                legacy=legacy,
            )
        return
    if not isinstance(value, str):
        return
    if _PRIVATE_KEY_PEM.search(value):
        violations.append(
            PolicyFinding(
                code="plaintext-private-key",
                pointer=pointer,
                message="private-key material must not be stored in system config",
            )
        )
    if _url_contains_userinfo(value):
        violations.append(
            PolicyFinding(
                code="url-userinfo",
                pointer=pointer,
                message="URLs in system config must not contain userinfo",
            )
        )


def _inspect_docker_image(
    config: Mapping[str, Any],
    *,
    adapter: str,
    canonical_id: str,
    allow_legacy: bool,
    violations: list[PolicyFinding],
    legacy: list[PolicyFinding],
) -> None:
    docker = config.get("openclaw_docker")
    if not isinstance(docker, Mapping):
        return
    image = docker.get("image")
    _inspect_docker_image_value(
        image,
        pointer=_LEGACY_STUB_POINTER,
        adapter=adapter,
        canonical_id=canonical_id,
        allow_legacy=allow_legacy,
        violations=violations,
        legacy=legacy,
    )


def _inspect_docker_image_value(
    image: Any,
    *,
    pointer: str,
    adapter: str,
    canonical_id: str,
    allow_legacy: bool,
    violations: list[PolicyFinding],
    legacy: list[PolicyFinding],
) -> None:
    if not isinstance(image, str) or not (
        "PLUGIN_REV" in image or "TODO" in image or _UNRESOLVED_MARKER.search(image)
    ):
        return
    finding = PolicyFinding(
        code="docker-image-placeholder",
        pointer=pointer,
        message="Docker image must not contain build or environment placeholders",
    )
    if (
        pointer == _LEGACY_STUB_POINTER
        and allow_legacy
        and adapter == "openclaw-docker"
        and canonical_id == _LEGACY_STUB_ID
        and image == _LEGACY_STUB_IMAGE
    ):
        legacy.append(
            PolicyFinding(
                code="legacy-docker-image-placeholder",
                pointer=_LEGACY_STUB_POINTER,
                message="legacy stub image placeholder must migrate to a real image",
            )
        )
    else:
        violations.append(finding)


def _is_loopback_empty_key_exception(
    adapter: str, parent: Mapping[object, Any], pointer: str, key: str, value: Any
) -> bool:
    return (
        adapter == "evermemos_api"
        and pointer == "/api_key"
        and key == "api_key"
        and value == ""
        and _is_strict_loopback_url(parent.get("base_url"))
    )


def _is_strict_loopback_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    del port
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    host = parsed.hostname
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address in {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}


def _url_contains_userinfo(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        bool(parsed.scheme)
        and bool(parsed.netloc)
        and (parsed.username is not None or parsed.password is not None)
    )


def _escape_pointer(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _normalize_policy_key(segment: str) -> str:
    normalized = segment.replace("-", "_")
    normalized = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", normalized)
    normalized = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", normalized)
    return normalized.lower()


def _is_secret_key(normalized_segment: str) -> bool:
    if normalized_segment in _NON_SECRET_KEYS:
        return False
    return normalized_segment in _SECRET_KEYS or normalized_segment.endswith(
        _SECRET_KEY_SUFFIXES
    )


def _is_structured_secret_key(normalized_segment: str) -> bool:
    return normalized_segment in _STRUCTURED_SECRET_KEYS or normalized_segment.endswith(
        _STRUCTURED_SECRET_KEY_SUFFIXES
    )
