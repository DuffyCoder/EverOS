# Import retained for type annotations and field definitions
from elasticsearch.dsl import field as e_field
from core.tenants.tenantize.oxm.es.tenant_aware_async_document import (
    TenantAwareAliasDoc,
)
from core.oxm.es.analyzer import (
    lower_keyword_analyzer,
    whitespace_lowercase_trim_stop_analyzer,
)


class AgentCaseDoc(
    TenantAwareAliasDoc(
        "agent-case", number_of_shards=1, number_of_replicas=0
    )
):
    """
    Agent case Elasticsearch document

    Uses a separate agent-case index for BM25 keyword retrieval.
    """

    class CustomMeta:
        # Specify the field name used to automatically populate meta.id
        id_source_field = "id"

    # Basic identification fields
    user_id = e_field.Keyword()
    group_id = e_field.Keyword()

    # Timestamp field
    timestamp = e_field.Date(required=True)

    # BM25 retrieval core field - supports multi-value storage for search content
    search_content = e_field.Text(
        multi=True,
        required=True,
        analyzer="standard",
        fields={
            "original": e_field.Text(
                analyzer=lower_keyword_analyzer,
                search_analyzer=lower_keyword_analyzer,
            )
        },
    )

    # Core content fields
    task_intent = e_field.Text(
        analyzer=whitespace_lowercase_trim_stop_analyzer,
        search_analyzer=whitespace_lowercase_trim_stop_analyzer,
    )
    approach = e_field.Text(
        analyzer=whitespace_lowercase_trim_stop_analyzer,
        search_analyzer=whitespace_lowercase_trim_stop_analyzer,
    )

    # Parent info
    parent_type = e_field.Keyword()
    parent_id = e_field.Keyword()

    # Extension field
    extend = e_field.Object(dynamic=True)

    # Audit fields
    created_at = e_field.Date()
    updated_at = e_field.Date()
