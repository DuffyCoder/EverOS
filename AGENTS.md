# AGENTS.md - AI Assistant Guide for EverMemOS

> This file provides project context for AI coding assistants (Claude Code, GitHub Copilot, Cursor, Codeium, etc.) to better understand and work with this project.
>
> **Maintainer Note**: Keep this file updated when project structure changes.

## Project Summary

**EverMemOS** is an enterprise-grade long-term memory system for conversational AI agents.

| Attribute | Value |
|-----------|-------|
| Language | Python 3.12 |
| Framework | FastAPI + LangChain |
| Package Manager | uv |
| Version | v1.1.0 |
| License | Apache 2.0 |

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  API Layer (FastAPI)                │
│            infra_layer/adapters/input/api/          │
├─────────────────────────────────────────────────────┤
│                  Service Layer                      │
│                     service/                        │
├─────────────────────────────────────────────────────┤
│                Business Logic Layer                 │
│                    biz_layer/                       │
├─────────────────────────────────────────────────────┤
│                  Agentic Layer                      │
│      (Memory Management, Vectorization, Retrieval)  │
│                  agentic_layer/                     │
├─────────────────────────────────────────────────────┤
│                  Memory Layer                       │
│        (MemCell, Episode, Profile Extraction)       │
│                  memory_layer/                      │
├─────────────────────────────────────────────────────┤
│                   Core Layer                        │
│      (DI, Middleware, Multi-tenancy, Cache)         │
│                      core/                          │
├─────────────────────────────────────────────────────┤
│               Infrastructure Layer                  │
│       (MongoDB, Milvus, Elasticsearch, Redis)       │
│          infra_layer/adapters/out/                  │
└─────────────────────────────────────────────────────┘
```

## Directory Structure

```
EverMemOS/
├── src/                          # Main source code
│   ├── run.py                    # Application entry point
│   ├── app.py                    # FastAPI app configuration
│   ├── base_app.py               # Base application setup
│   ├── bootstrap.py              # Bootstrap and initialization
│   ├── application_startup.py    # Startup hooks
│   ├── manage.py                 # Management commands
│   ├── run_memorize.py           # Batch memorization runner
│   ├── task.py                   # Task definitions
│   ├── addon.py                  # Plugin system
│   ├── project_meta.py           # Project metadata
│   │
│   ├── agentic_layer/            # Memory orchestration
│   │   ├── memory_manager.py     # Core memory manager
│   │   ├── vectorize_service.py  # Embedding service
│   │   ├── rerank_service.py     # Reranking service
│   │   ├── fetch_mem_service.py  # Memory retrieval
│   │   ├── agentic_utils.py      # Agentic utilities
│   │   ├── retrieval_utils.py    # Retrieval utilities
│   │   ├── vectorize_base.py     # Base vectorizer
│   │   ├── vectorize_vllm.py     # VLLM vectorizer
│   │   ├── vectorize_deepinfra.py # DeepInfra vectorizer
│   │   ├── rerank_vllm.py        # VLLM reranker
│   │   ├── rerank_deepinfra.py   # DeepInfra reranker
│   │   └── metrics/              # Performance metrics
│   │
│   ├── memory_layer/             # Memory extraction
│   │   ├── memory_manager.py     # Memory extraction coordinator
│   │   ├── constants.py          # Constants
│   │   ├── memcell_extractor/    # Atomic memory extraction
│   │   ├── memory_extractor/     # High-level extractors
│   │   │   ├── episode_memory_extractor.py
│   │   │   ├── profile_memory_extractor.py
│   │   │   ├── group_profile_memory_extractor.py
│   │   │   ├── foresight_extractor.py
│   │   │   └── event_log_extractor.py
│   │   ├── cluster_manager/      # Memory clustering
│   │   ├── profile_manager/      # Profile management
│   │   ├── llm/                  # LLM providers
│   │   │   ├── llm_provider.py
│   │   │   ├── openai_provider.py
│   │   │   ├── protocol.py
│   │   │   └── config.py
│   │   └── prompts/              # Prompt templates
│   │       ├── en/               # English prompts
│   │       └── zh/               # Chinese prompts
│   │
│   ├── core/                     # Framework infrastructure
│   │   ├── di/                   # Dependency injection
│   │   ├── tenants/              # Multi-tenancy
│   │   ├── middleware/           # HTTP middleware
│   │   ├── cache/                # Caching layer
│   │   ├── events/               # Event system
│   │   ├── addons/               # Plugin framework
│   │   ├── asynctasks/           # Async task framework
│   │   ├── authorize/            # Authorization
│   │   ├── capability/           # Capability framework
│   │   ├── class_annotations/    # Class annotations
│   │   ├── component/            # Component system
│   │   ├── config/               # Configuration
│   │   ├── constants/            # Constants
│   │   ├── context/              # Context management
│   │   ├── interface/            # Interface definitions
│   │   ├── lifespan/             # FastAPI lifespan
│   │   ├── lock/                 # Distributed locks
│   │   ├── longjob/              # Long-running jobs
│   │   ├── nlp/                  # NLP utilities
│   │   ├── observation/          # Logging & observability
│   │   ├── oxm/                  # Object mapping base
│   │   ├── queue/                # Queue management
│   │   ├── rate_limit/           # Rate limiting
│   │   └── request/              # Request handling
│   │
│   ├── infra_layer/              # External adapters
│   │   ├── adapters/
│   │   │   ├── input/            # Inbound adapters
│   │   │   │   ├── api/          # REST controllers
│   │   │   │   │   ├── memory/   # Memory API
│   │   │   │   │   ├── health/   # Health check
│   │   │   │   │   ├── status/   # Status API
│   │   │   │   │   ├── dto/      # Data transfer objects
│   │   │   │   │   └── mapper/   # Request mappers
│   │   │   │   ├── jobs/         # Job handlers
│   │   │   │   ├── mcp/          # MCP protocol
│   │   │   │   └── mq/           # Message queue consumers
│   │   │   └── out/              # Outbound adapters
│   │   │       ├── persistence/  # Data persistence
│   │   │       │   ├── document/memory/  # MongoDB documents
│   │   │       │   ├── repository/       # Data repositories
│   │   │       │   └── mapper/           # Data mappers
│   │   │       ├── search/       # Search adapters
│   │   │       │   ├── milvus/   # Vector search
│   │   │       │   │   ├── memory/       # Collections
│   │   │       │   │   └── converter/    # Converters
│   │   │       │   ├── elasticsearch/    # Full-text search
│   │   │       │   │   ├── memory/       # Indices
│   │   │       │   │   └── converter/    # Converters
│   │   │       │   └── repository/       # Search repositories
│   │   │       └── event/        # Event publishers
│   │   └── scripts/              # Infrastructure scripts
│   │       └── migrations/       # DB migrations
│   │
│   ├── biz_layer/                # Business logic
│   │   ├── mem_memorize.py       # Memorization logic
│   │   ├── mem_db_operations.py  # Database operations
│   │   ├── mem_sync.py           # Data synchronization
│   │   └── memorize_config.py    # Memorization config
│   │
│   ├── service/                  # Service implementations
│   │   ├── memory_request_log_service.py
│   │   ├── conversation_meta_service.py
│   │   ├── request_status_service.py
│   │   └── memcell_delete_service.py
│   │
│   ├── api_specs/                # API specifications
│   │   ├── memory_models.py      # Memory data models
│   │   ├── memory_types.py       # Memory type enums
│   │   ├── request_converter.py  # Request converters
│   │   └── dtos/                 # Data transfer objects
│   │
│   ├── common_utils/             # Shared utilities
│   │   ├── language_utils.py     # Language detection
│   │   ├── text_utils.py         # Text processing
│   │   ├── datetime_utils.py     # Date/time helpers
│   │   ├── url_extractor.py      # URL extraction
│   │   ├── base62_utils.py       # Base62 encoding
│   │   ├── cli_ui.py             # CLI utilities
│   │   ├── app_meta.py           # App metadata
│   │   ├── project_path.py       # Project paths
│   │   └── load_env.py           # Environment loading
│   │
│   ├── migrations/               # Database migrations
│   │   ├── mongodb/              # MongoDB migrations
│   │   └── postgresql/           # PostgreSQL migrations
│   │
│   ├── config/                   # Configuration files
│   │   └── stopwords/            # Stopword lists
│   │
│   └── devops_scripts/           # DevOps utilities
│       ├── data_fix/             # Data repair scripts
│       ├── i18n/                 # Internationalization
│       └── sensitive_info/       # Sensitive data handling
│
├── tests/                        # Test suite
├── demo/                         # Demo examples
│   ├── simple_demo.py
│   ├── chat_with_memory.py
│   ├── extract_memory.py
│   ├── chat/                     # Chat interface
│   ├── config/                   # Demo configs
│   ├── tools/                    # Demo tools
│   └── utils/                    # Demo utilities
├── docs/                         # Long-lived project documentation
│   └── evaluation/              # Evaluation architecture and policy
├── evaluation/                   # Generic evaluation framework
│   ├── data/                    # Authoritative benchmark datasets
│   └── docs/                    # Framework-coupled operations
├── openclaw-eval/                # OpenClaw-specific evaluation runtime
├── data/                         # Product demo and sample data
├── data_format/                  # Data format specs
├── figs/                         # Figures/images
│
├── docker-compose.yaml           # Infrastructure stack
├── Dockerfile                    # Container build
├── pyproject.toml                # Project dependencies
├── Makefile                      # Build commands
├── config.json                   # Deprecated legacy config (compatibility only)
├── env.template                  # Current environment template
├── pytest.ini                    # Pytest config
├── pyrightconfig.json            # Type checker config
└── .pre-commit-config.yaml       # Pre-commit hooks
```

## Repository Ownership Boundaries

- `evaluation/` is the generic benchmark framework. It owns protocols,
  datasets, metrics, configuration, adapters, and public CLI entry points.
- `openclaw-eval/` owns the OpenClaw-specific container, plugin, build, and
  harness runtime. It may depend on public `evaluation/` contracts;
  `evaluation/` must not depend on its internal file layout.
- `evaluation/data/` is authoritative for benchmark datasets. `data/` owns
  demo inputs; `data/locomo10.json` is a controlled compatibility mirror.
- `docs/evaluation/` owns durable evaluation architecture, policy, and
  reproducibility guidance. `evaluation/docs/` owns implementation-coupled
  operations. `docs/superpowers/` contains historical records, not current
  repository policy.
- Reports under `docs/evaluation/analysis/` and evidence under
  `evaluation/archives/` are local and ignored. Git tracks only the analysis
  policy README and template.

Existing paths, imports, commands, and the EverOS, EverMemOS, and `memsys`
identifiers are compatibility surfaces; do not consolidate names as part of
repository hygiene work.

## Evaluation System Configuration Governance

- Select systems by public ID from `evaluation/config/systems/index.yaml`,
  normally a `canonical` / `active` entry. Do not select a YAML filename.
- The registry is locked to exactly 36 IDs by
  `EXPECTED_PUBLIC_SYSTEM_IDS` and exact-equality tests. Routine changes update
  an existing categorized leaf and its index metadata, or an existing alias's
  index-only row; adding only a leaf and index row cannot expose a 37th ID. A
  deliberate public-ID expansion must also update the constant and catalog
  row/tests, refactor the catalog/index and legacy-baseline gates so the
  immutable original 36 remain a required subset, and test the new ID
  separately. Never edit the pre-cleanup fixture to make a new ID appear
  legacy.
- Existing presets use `canonical/` for supported defaults, `experiments/` for
  exploratory work, `ablations/` for controlled comparisons, and `tooling/`
  for diagnostics. Compatibility aliases live only in the index and have no
  runnable leaf.
- `extends` recursively merges mappings; lists and scalar values replace the
  inherited value. Keep bases minimal and semantic differences in leaves.
- Secret fields in tracked YAML use `${VAR}` or `${VAR:}` with no non-empty
  secret default. `api_key_env` and `env_vars` contain bare environment names.
  The only empty-key exception is the strict-loopback local EverMemOS API
  preset. Never commit a materialized credential.
- Preserve old public IDs and requested-ID result directory names. Metadata
  records the canonical target and source chain for resume safety.
- Follow the active [system-configuration guide](evaluation/docs/system-configs/README.md)
  and run its strict 36-ID validation suite with every registry change.

## Tech Stack

| Category | Technology |
|----------|------------|
| Web Framework | FastAPI, Uvicorn |
| LLM Integration | LangChain, OpenAI, Anthropic, Google GenAI |
| Document Store | MongoDB with Beanie ODM |
| Vector Database | Milvus 2.5 |
| Full-text Search | Elasticsearch 8.x |
| Cache | Redis |
| Message Queue | Kafka, ARQ |
| Validation | Pydantic 2.x |
| Package Manager | uv |

## Code Conventions

### Style
- **Formatter**: Black (line width 88)
- **Import Sorting**: isort
- **Type Checker**: Pyright
- **Naming**: PEP 8 conventions

### Patterns
- Async/await for all I/O operations
- Dependency injection via `core/di/`
- Repository pattern for data access
- Adapter pattern for external services

### File Naming
- Snake_case for modules: `memory_manager.py`
- Classes in PascalCase: `MemoryManager`
- Constants in UPPER_CASE: `DEFAULT_TIMEOUT`

## Key Abstractions

### Memory Types
| Type | Description | Location |
|------|-------------|----------|
| MemCell | Atomic memory unit | `memory_layer/memcell_extractor/` |
| Episode | Aggregated memories by topic | `memory_layer/memory_extractor/episode_memory_extractor.py` |
| Profile | User characteristics | `memory_layer/memory_extractor/profile_memory_extractor.py` |
| GroupProfile | Group chat memories | `memory_layer/memory_extractor/group_profile_memory_extractor.py` |
| Foresight | Predictive memories | `memory_layer/memory_extractor/foresight_extractor.py` |
| EventLog | Timeline events | `memory_layer/memory_extractor/event_log_extractor.py` |

### Retrieval Strategies
| Strategy | Description |
|----------|-------------|
| KEYWORD | BM25 keyword search |
| VECTOR | Milvus vector similarity |
| HYBRID | Combined keyword + vector |
| RRF | Reciprocal Rank Fusion |
| AGENTIC | LLM-guided multi-turn retrieval |

## Database Schema

### MongoDB Collections
Located in `infra_layer/adapters/out/persistence/document/memory/`:
- `EpisodicMemory` - Episodic memories
- `UserProfile` - User profiles
- `GroupProfile` - Group profiles
- `GroupUserProfileMemory` - Group user profile memories
- `MemCell` - Atomic memory units
- `Entity` - Entities
- `Relationship` - Relationships
- `CoreMemory` - Core memories
- `EventLogRecord` - Event logs
- `ForesightRecord` - Foresight records
- `BehaviorHistory` - Behavior history
- `ConversationMeta` - Conversation metadata
- `ConversationStatus` - Conversation status
- `ClusterState` - Cluster state

### Milvus Collections
Located in `infra_layer/adapters/out/search/milvus/memory/`:
- `EpisodicMemoryCollection` - Episodic memory vectors
- `EventLogCollection` - Event log vectors
- `ForesightCollection` - Foresight memory vectors

### Elasticsearch Indices
Located in `infra_layer/adapters/out/search/elasticsearch/memory/`:
- `episodic_memory` - Episodic memory full-text index
- `event_log` - Event log index
- `foresight` - Foresight memory index

## Common Commands

```bash
# Development
uv sync                          # Install dependencies
make run                         # Run application
python src/run.py                # Alternative run

# Testing
pytest                           # Run all tests
pytest tests/test_memory_layer/  # Specific tests
pytest --cov=src                 # With coverage

# Code Quality
black src/                       # Format code
isort src/                       # Sort imports
pyright                          # Type check
make format                      # Format all

# Infrastructure
docker-compose up -d             # Start databases
docker-compose down              # Stop databases
docker-compose logs -f           # View logs
```

## Environment Variables

Copy `env.template` to `.env` and fill only the services used by the selected
runtime or evaluation preset. `env.template` is the canonical starting
template; the selected system YAML and its `env_vars` are the exact per-preset
contract. Do not duplicate a second variable list here. Never commit `.env` or
copy a materialized secret into tracked configuration, fixtures, results, or
docs.

## Development Guidelines

### Adding a New Memory Type
1. Define enum in `src/api_specs/memory_types.py`
2. Create extractor in `src/memory_layer/memory_extractor/`
3. Add MongoDB document in `src/infra_layer/adapters/out/persistence/document/memory/`
4. Create repository in `src/infra_layer/adapters/out/persistence/repository/`
5. Add vector collection if needed in `src/infra_layer/adapters/out/search/milvus/memory/`
6. Add ES index if needed in `src/infra_layer/adapters/out/search/elasticsearch/memory/`

### Adding a New API Endpoint
1. Create controller in `src/infra_layer/adapters/input/api/`
2. Define request/response DTOs in `src/api_specs/dtos/`
3. Implement service in `src/service/`
4. Register route in app configuration (`src/app.py`)

### Adding a New LLM Provider
1. Create provider in `src/memory_layer/llm/`
2. Implement `LLMProvider` interface from `protocol.py`
3. Register in DI container

## Important Considerations

1. **Multi-tenancy**: All data operations are tenant-scoped via `core/tenants/`
2. **Async First**: Use `async/await` for all I/O operations
3. **Type Safety**: Add type hints to all functions
4. **Error Handling**: Use custom exceptions from `core/`
5. **Logging**: Use logger from `core/observation/logger.py`
6. **Configuration**: Current configuration comes from `.env` (based on
   `env.template`) plus typed and component runtime configs. Root `config.json`
   is deprecated legacy configuration retained unchanged for compatibility; do
   not treat it as the primary configuration source or add secrets to it.

## Documentation References

- [Architecture](docs/ARCHITECTURE.md)
- [Setup Guide](docs/installation/SETUP.md)
- [Docker Setup](docs/installation/DOCKER_SETUP.md)
- [API Reference](docs/api_docs/memory_api.md)
- [Development Guide](docs/dev_docs/development_guide.md)
- [Usage Examples](docs/usage/USAGE_EXAMPLES.md)
- [Configuration Guide](docs/usage/CONFIGURATION_GUIDE.md)
- [Evaluation Ownership](docs/evaluation/README.md)

## Testing Approach

- Unit tests in `tests/` mirroring `src/` structure
- Use pytest fixtures for database mocking
- Async tests with `pytest-asyncio`
- Integration tests require running infrastructure (`docker-compose up -d`)

## Demos

Located in `demo/` directory:
- `simple_demo.py` - Basic usage
- `chat_with_memory.py` - Interactive chat with memory
- `extract_memory.py` - Memory extraction example
- `tools/` - Additional demo tools and utilities
