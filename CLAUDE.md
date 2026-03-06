# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EverMemOS is an enterprise-grade intelligent memory system for AI agents. It extracts, structures, and retrieves information from conversations, achieving 93% reasoning accuracy on the LoCoMo benchmark. The system operates along two cognitive tracks: **memory construction** (extract, structure, consolidate) and **memory perception** (retrieve, apply).

## Commands

```bash
# Install dependencies
uv sync

# Start development services (MongoDB, Elasticsearch, Milvus, Redis)
docker-compose up -d

# Run the server (default port 1995)
uv run python src/run.py --port 8001

# Run tests
PYTHONPATH=src pytest tests/

# Run a single test file
PYTHONPATH=src pytest tests/path/to/test_file.py

# Run a specific test
PYTHONPATH=src pytest tests/path/to/test_file.py::test_function_name

# Lint and format
make lint                    # Runs black + i18n check
black src/                   # Format only

# Run bootstrap scripts (for demos/utilities)
uv run python src/bootstrap.py demo/simple_demo.py

# Run evaluation benchmarks
uv sync --group evaluation
uv run python -m evaluation.cli --dataset locomo --system evermemos
```

## Architecture

### Layered Structure (top to bottom)

1. **Agentic Layer** (`src/agentic_layer/`) - Orchestration: memory extraction agents, vectorization, retrieval coordination, reranking
2. **Memory Layer** (`src/memory_layer/`) - Core memory operations: MemCell extraction, episodic/profile memory construction, LLM prompts
3. **Business Layer** (`src/biz_layer/`, `src/service/`) - Controllers, services, API endpoints, data validation
4. **Infrastructure Layer** (`src/infra_layer/`) - Database adapters for MongoDB, Redis, Elasticsearch, Milvus
5. **Core Framework** (`src/core/`) - DI container, middleware, lifecycle management, async tasks, ORM (`core/oxm/`)
6. **Common Utilities** (`src/common_utils/`) - DateTime utils, text processing, language detection

### Memory Extraction Pipeline

Conversations flow through: **MemCell Extraction** → **Episode Memory** → **Profile Memory** → **Storage** → **Indexing**

- `memory_layer/memcell_extractor/` - Extracts atomic memory units from conversations
- `memory_layer/memory_extractor/` - Builds higher-level memories (episodes, profiles, events, foresight)
- `memory_layer/llm/` - Multi-provider LLM support (OpenAI, OpenRouter, Anthropic, Google GenAI)

### Retrieval Strategies

- **Hybrid (RRF)** - Combines BM25 keyword search + vector semantic search via Reciprocal Rank Fusion
- **Lightweight** - Fast BM25-only for latency-sensitive scenarios
- **Agentic** - Multi-round retrieval with LLM-generated query expansion

### Key Patterns

**Dependency Injection**: Custom DI container in `core/di/`. Use `@component` decorator to register beans:
```python
from core.di.decorators import component
from core.di.utils import get_bean_by_type

@component
class MyService:
    pass

service = get_bean_by_type(MyService)
```

**DateTime Handling**: Always use `common_utils.datetime_utils` instead of direct `datetime` module.

**API Models**: Request/response types defined in `src/api_specs/`.

## Code Style

- Python 3.12+ required
- PEP 8 with 88-char line length (Black)
- Type hints required for function parameters and returns
- Absolute imports only (no relative imports)
- No wildcard imports
- No code in `__init__.py` files
- **CRITICAL: NO CJK characters (Chinese/Japanese/Korean) in this project** - Use English only for all code, comments, docstrings, and variable names

## Commit Convention

Uses Gitmoji format: `<emoji> <type>: <description>`

Examples:
- `✨ feat: Add new memory retrieval algorithm`
- `🐛 fix: Fix memory leak in vector indexing`
- `♻️ refactor: Simplify memory extraction logic`
- `✅ test: Add tests for profile extraction`

## Environment

Copy `env.template` to `.env` and configure:
- `LLM_API_KEY` - For memory extraction
- `VECTORIZE_API_KEY` - For embedding/reranking
- Database connections: `MONGODB_HOST`, `ELASTICSEARCH_HOST`, `MILVUS_HOST`, `REDIS_HOST`
