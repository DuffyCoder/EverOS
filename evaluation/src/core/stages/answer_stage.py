"""
Answer stage - generate answers.
"""
import asyncio
import hashlib
import os
import time
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path
from typing import List, Optional
from logging import Logger
from tqdm import tqdm

from evaluation.src.utils.llm_keys import count_llm_key_pool

from evaluation.src.core.data_models import (
    ANSWER_SENTINEL_DEADLINE,
    ANSWER_SENTINEL_FAILED,
    ANSWER_SENTINEL_TIMEOUT,
    QAPair,
    SearchResult,
    AnswerResult,
)
from evaluation.src.adapters.base import BaseAdapter
from evaluation.src.utils.checkpoint import CheckpointManager
from evaluation.src.core.benchmark_context import (
    LatencyRecorder,
    NULL_RECORDER,
    OUTCOME_FAILED_OTHER,
    OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT,
    max_retries_for,
)


# Tokenizer is loaded lazily so importing answer_stage stays cheap and the
# dependency on tiktoken is only paid when tests / pipelines actually call
# estimate_tokens().
_TOKEN_ENCODING = None

# yaml sentinel for "size concurrency to the LLM key pool".
MAX_CONCURRENT_AUTO = "auto"

# AP1 experiment knob (answer-prompt arm): opt-in suffix appended to every
# question right before adapter.answer(). Unset => byte-identical original
# behavior. Host-side only: in agent_local/docker mode the suffix travels
# inside the bridge agent_run "message" field; in shared_llm mode it lands
# in the prompt's {question} slot — no docker image rebuild required.
ENV_ANSWER_PROMPT_FILE = "EVAL_ANSWER_PROMPT_FILE"
ENV_ANSWER_PROMPT_SUFFIX = "EVAL_ANSWER_PROMPT_SUFFIX"


def resolve_answer_prompt_suffix() -> str:
    """Resolve the opt-in answer-prompt suffix from the environment.

    Precedence: EVAL_ANSWER_PROMPT_FILE (path to a UTF-8 text file) wins
    over EVAL_ANSWER_PROMPT_SUFFIX (inline text). Returns "" when neither
    is set — the stage then behaves exactly as before.

    Fail-fast: if EVAL_ANSWER_PROMPT_FILE is set but unreadable/empty we
    raise instead of silently running the baseline prompt — a 10h+ eval
    arm accidentally running as control is worse than an early crash.
    """
    path = os.environ.get(ENV_ANSWER_PROMPT_FILE, "").strip()
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
        except OSError as err:
            raise RuntimeError(
                f"{ENV_ANSWER_PROMPT_FILE}={path!r} is set but unreadable: {err}"
            ) from err
        if not text:
            raise RuntimeError(
                f"{ENV_ANSWER_PROMPT_FILE}={path!r} is set but the file is empty"
            )
        return text
    return os.environ.get(ENV_ANSWER_PROMPT_SUFFIX, "").strip()
# Fallback when yaml says ``auto`` but no LLM_API_KEY[_<N>] is configured —
# preserves the historical pre-multi-key default for non-sophnet pipelines.
_LEGACY_DEFAULT_CONCURRENCY = 50


def _resolve_max_concurrent(answer_cfg: dict) -> int:
    """Resolve ``answer.max_concurrent`` yaml value (int or ``"auto"``)."""
    raw = answer_cfg.get("max_concurrent", MAX_CONCURRENT_AUTO)
    if isinstance(raw, int):
        return max(raw, 1)
    s = str(raw).strip().lower()
    if s != MAX_CONCURRENT_AUTO:
        try:
            return max(int(s), 1)
        except ValueError:
            # A typo'd value (e.g. "aut0", "sixteen") must not crash the whole
            # answer stage with an opaque ValueError; fall back to auto sizing.
            print(
                f"  ⚠️  answer.max_concurrent={raw!r} is not an int or "
                f"'auto'; falling back to auto (key-pool sizing)."
            )
    pool = count_llm_key_pool()
    return pool if pool >= 1 else _LEGACY_DEFAULT_CONCURRENCY


def estimate_tokens(text: str) -> int:
    """Rough token count for latency/context diagnostics.

    Uses tiktoken's o200k_base encoding (matches gpt-4o / gpt-4o-mini). Falls
    back to whitespace splitting if tiktoken is unavailable so the pipeline
    keeps working in stripped-down environments.
    """
    if not text:
        return 0
    global _TOKEN_ENCODING
    if _TOKEN_ENCODING is None:
        try:
            import tiktoken

            _TOKEN_ENCODING = tiktoken.get_encoding("o200k_base")
        except Exception:
            _TOKEN_ENCODING = "fallback"
    if _TOKEN_ENCODING == "fallback":
        return len(text.split())
    return len(_TOKEN_ENCODING.encode(text))


def build_context(search_result: SearchResult) -> str:
    """
    Build context from search results.
    
    Prefer pre-formatted context (dual-speaker scenarios), else use simple numbering (single-speaker scenarios).
    
    Args:
        search_result: Search result
        
    Returns:
        Context string
    """
    # Prefer pre-formatted context (provided by adapter)
    formatted_context = search_result.retrieval_metadata.get("formatted_context", "")
    if formatted_context:
        return formatted_context
    
    # Single speaker scenario: simple formatting
    context_parts = []
    
    # Get top_k from retrieval_metadata, default to len(results) if not specified
    top_k = search_result.retrieval_metadata.get("top_k", len(search_result.results))
    
    # Add memory content (use top_k instead of hardcoded 10)
    for idx, result in enumerate(search_result.results[:top_k], 1):
        content = result.get("content", "")
        context_parts.append(f"{idx}. {content}")
    
    context = "\n\n".join(context_parts)
    
    # For systems supporting preferences (e.g., Memos), add formatted pref_string
    preferences = search_result.retrieval_metadata.get("preferences", {})
    pref_string = preferences.get("pref_string", "")
    
    if pref_string:
        context += "\n\n" + pref_string
    
    return context


async def run_answer_stage(
    adapter: BaseAdapter,
    qa_pairs: List[QAPair],
    search_results: List[SearchResult],
    checkpoint_manager: Optional[CheckpointManager],
    logger: Logger,
    latency_recorder: Optional[LatencyRecorder] = None,
) -> List[AnswerResult]:
    """
    Generate answers with fine-grained checkpointing.
    
    Save checkpoint every SAVE_INTERVAL questions.
    
    Args:
        adapter: System adapter
        qa_pairs: List of QA pairs
        search_results: List of search results
        checkpoint_manager: Checkpoint manager for resume
        logger: Logger
        
    Returns:
        List of answer results
    """
    print(f"\n{'='*60}")
    print(f"Stage 3/4: Answer")
    print(f"{'='*60}")
    
    SAVE_INTERVAL = 400  # Save every 400 tasks
    # v0.7 D5: configurable concurrency. ``max_concurrent`` may be an int or
    # ``"auto"`` — auto scales to the host's LLM_API_KEY[_<N>] pool size so
    # adding keys to .env automatically widens cross-conv parallelism.
    answer_cfg = (getattr(adapter, "config", None) or {}).get("answer") or {}
    MAX_CONCURRENT = _resolve_max_concurrent(answer_cfg)

    # AP1 knob: resolved once per stage (single file read / env lookup);
    # "" means the knob is off and queries are passed through untouched.
    answer_prompt_suffix = resolve_answer_prompt_suffix()
    answer_prompt_suffix_sha1 = ""
    if answer_prompt_suffix:
        answer_prompt_suffix_sha1 = hashlib.sha1(
            answer_prompt_suffix.encode("utf-8")
        ).hexdigest()[:12]
        source = (
            ENV_ANSWER_PROMPT_FILE
            if os.environ.get(ENV_ANSWER_PROMPT_FILE, "").strip()
            else ENV_ANSWER_PROMPT_SUFFIX
        )
        print(
            f"  📎 Answer-prompt suffix ACTIVE via {source}: "
            f"{len(answer_prompt_suffix)} chars, sha1={answer_prompt_suffix_sha1}"
        )

    # Load fine-grained checkpoint
    all_answer_results = {}
    if checkpoint_manager:
        loaded_results = checkpoint_manager.load_answer_progress()
        # Convert to {question_id: AnswerResult} format
        for result in loaded_results.values():
            all_answer_results[result["question_id"]] = result
    
    total_qa_count = len(qa_pairs)
    processed_count = len(all_answer_results)
    
    print(f"Total questions: {total_qa_count}")
    if processed_count > 0:
        print(f"Already processed: {processed_count} questions (from checkpoint)")
        print(f"Remaining: {total_qa_count - processed_count} questions")
    
    # Pair qa with its search_result by question_id (stashed by
    # search_stage in retrieval_metadata) with positional fallback for
    # SearchResults that never saw search_stage. Shared helper keeps
    # the logic in lockstep with retrieval_metrics / content_overlap.
    from evaluation.src.metrics.pairing import pair_by_question_id

    search_by_id, _ = pair_by_question_id(qa_pairs, search_results)

    pending_tasks = []
    for qa in qa_pairs:
        if qa.question_id in all_answer_results:
            continue
        sr = search_by_id.get(qa.question_id)
        if sr is None:
            # No retrieval output for this question - build an empty one
            # so the stage proceeds (answer_stage is resilient to empty
            # context but we must not drop the qa).
            sr = SearchResult(
                query=qa.question,
                conversation_id=qa.metadata.get("conversation_id", ""),
                results=[],
                retrieval_metadata={"question_id": qa.question_id,
                                    "error": "no search_result for question_id"},
            )
        pending_tasks.append((qa, sr))

    # Interleave pending tasks round-robin by conversation so the global
    # semaphore's first MAX_CONCURRENT slots fan out to distinct conv ids
    # instead of being monopolized by the first conv's QAs (which would
    # all serialize behind the same per-conv lock and defeat parallelism).
    # In serial mode (MAX_CONCURRENT == 1) round-robin is a no-op for
    # concurrency but reorders wall-clock execution from conv-major to
    # qa_idx-major, which breaks any post-hoc tooling that assumes the
    # canonical (conv_id, qa_idx) order (e.g. qa_logs annotate.py). Skip it.
    if pending_tasks:
        by_conv: dict[str, list] = defaultdict(list)
        for qa, sr in pending_tasks:
            by_conv[sr.conversation_id].append((qa, sr))
        if MAX_CONCURRENT > 1:
            pending_tasks = [
                task
                for column in zip_longest(*by_conv.values())
                for task in column
                if task is not None
            ]
        else:
            # serial: conv-major preserves (conv_id, qa_idx) wall-clock order
            pending_tasks = [task for tasks in by_conv.values() for task in tasks]

    if not pending_tasks:
        print(f"✅ All questions already processed!")
        # Convert to AnswerResult object list (original order)
        results = []
        for qa in qa_pairs:
            if qa.question_id in all_answer_results:
                result_dict = all_answer_results[qa.question_id]
                results.append(AnswerResult(
                    question_id=result_dict["question_id"],
                    question=result_dict["question"],
                    answer=result_dict["answer"],
                    golden_answer=result_dict["golden_answer"],
                    category=result_dict.get("category"),
                    conversation_id=result_dict.get("conversation_id", ""),
                    formatted_context=result_dict.get("formatted_context", ""),  # Load formatted_context
                    # search_results not loaded to save space
                ))
        return results
    
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    # Per-conv serialization: adapters whose backing store mutates per-conv
    # state at answer time (e.g. openclaw-docker bundles all QAs into one OV
    # session_id + shared session jsonl) cannot tolerate concurrent QAs of
    # the same conv. The global semaphore alone is not enough — if it admits
    # two QAs of the same conv simultaneously they race the in-container
    # write lock. ``conv_locks`` enforces strict within-conv serialization
    # while leaving cross-conv work free to run up to MAX_CONCURRENT.
    conv_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
    completed = processed_count
    failed = 0
    start_time = time.time()
    print(f"Answer concurrency: {MAX_CONCURRENT} (per-conv serialized)")

    recorder = latency_recorder or NULL_RECORDER
    answer_max_retries = max_retries_for(recorder.retry_policy)

    # Use tqdm progress bar
    pbar = tqdm(
        total=total_qa_count,
        initial=processed_count,
        desc="💬 Answer Progress",
        unit="qa"
    )

    async def answer_single_with_tracking(qa, search_result):
        nonlocal completed, failed

        # Acquire the per-conv lock BEFORE the global semaphore: otherwise a QA
        # that holds a semaphore slot while blocked on its conv lock starves
        # other conversations of slots, collapsing cross-conv concurrency toward
        # 1. Grabbing the conv lock first means a slot is only held once the QA
        # can actually run. (Single lock-acquire order everywhere → no cycle.)
        async with conv_locks[search_result.conversation_id], semaphore:
            # Wall-clock start of the QA's actual run (after lock acquisition).
            # Pair with answer_latency_ms to reconstruct per-QA wall-clock
            # windows post-hoc — needed by qa_logs raw_dump when session-bundle
            # adapters don't write per-qa session jsonl (so user_ts is absent).
            qa_start_unix_ms = int(time.time() * 1000)
            context = ""
            context_chars = 0
            context_tokens = 0
            answer_latency_ms = None
            answer = ANSWER_SENTINEL_FAILED

            try:
                # Build context
                context = build_context(search_result)
                context_chars = len(context)
                context_tokens = estimate_tokens(context)

                # Detect multiple-choice and enhance question if needed
                query = qa.question
                if "all_options" in qa.metadata:
                    options = qa.metadata["all_options"]
                    options_text = "\n".join([f"{key} {value}" for key, value in options.items()])

                    # Integrate options and requirements into question
                    query = f"""{qa.question}

OPTIONS:
{options_text}

IMPORTANT: This is a multiple-choice question. You MUST analyze the context and select the BEST option. In your FINAL ANSWER, return ONLY the option letter like (a), (b), (c), or (d), nothing else."""

                # AP1 knob: append the suffix AFTER any multiple-choice
                # enhancement so the MC format contract stays adjacent to
                # the options; the suffix text itself re-affirms it.
                if answer_prompt_suffix:
                    query = f"{query}\n\n{answer_prompt_suffix}"

                # Call adapter's answer method with timeout and retry.
                # Every attempt is recorded to the LatencyRecorder so
                # Layer-1 four-view aggregation can distinguish clean
                # first-hit latency from retry-inflated wall time.
                # max_retries is set by the pipeline's retry_policy; the
                # strict_no_retry value of 1 disables retries entirely.
                max_retries = answer_max_retries
                # v0.7: Negotiate timeout with adapter. Default 120s preserves
                # historical behavior; adapters like OpenClaw agent_local need
                # longer (e.g. agent_timeout_seconds + 30s margin).
                timeout_seconds = (
                    adapter.get_answer_timeout()
                    if hasattr(adapter, "get_answer_timeout")
                    else 120.0
                )
                retry_wait_seconds = 2.0

                async with recorder.measure("answer", qa.question_id) as ctx:
                    for attempt in range(max_retries):
                        # Skip retries once the harness-level deadline
                        # elapsed so one adapter call can't burn all
                        # of another sample's budget. fallback=true
                        # excludes the sample from clean stats.
                        if attempt > 0 and ctx.deadline_exceeded():
                            tqdm.write(
                                f"  ⏹️  Answer deadline exceeded before attempt {attempt + 1}; "
                                f"stopping retries for {qa.question_id}."
                            )
                            ctx.record_fallback()
                            answer = ANSWER_SENTINEL_DEADLINE
                            failed += 1
                            break
                        t_start = time.perf_counter()
                        try:
                            answer = await asyncio.wait_for(
                                adapter.answer(
                                    query=query,
                                    context=context,
                                    conversation_id=search_result.conversation_id,
                                    question_id=qa.question_id,
                                    benchmark_ctx=ctx,
                                ),
                                timeout=timeout_seconds
                            )
                            answer_latency_ms = (time.perf_counter() - t_start) * 1000.0
                            ctx.record_attempt(attempt + 1, answer_latency_ms, OUTCOME_SUCCESS)
                            answer = answer.strip()
                            break  # Success, exit retry loop

                        except asyncio.TimeoutError:
                            attempt_ms = (time.perf_counter() - t_start) * 1000.0
                            is_last = attempt >= max_retries - 1
                            ctx.record_attempt(
                                attempt + 1, attempt_ms, OUTCOME_TIMEOUT,
                                wait_ms_before_next=(0.0 if is_last else retry_wait_seconds * 1000.0),
                            )
                            if not is_last:
                                tqdm.write(f"  ⏱️  Timeout ({timeout_seconds}s) for {qa.question_id}, retry {attempt + 1}/{max_retries}...")
                                await asyncio.sleep(retry_wait_seconds)
                            else:
                                tqdm.write(f"  ❌ Timeout after {max_retries} attempts for {qa.question_id}: {qa.question[:50]}...")
                                ctx.record_fallback()
                                answer = ANSWER_SENTINEL_TIMEOUT
                                failed += 1

            except Exception as e:
                tqdm.write(f"  ⚠️ Answer generation failed for {qa.question_id}: {e}")
                answer = ANSWER_SENTINEL_FAILED
                failed += 1

            retrieval_meta = search_result.retrieval_metadata or {}
            extra_metrics: dict = {}
            if hasattr(adapter, "pop_answer_metrics"):
                extra_metrics = adapter.pop_answer_metrics(qa.question_id) or {}
            final_tokens = extra_metrics.get("final_context_tokens")
            if final_tokens is None:
                final_tokens = context_tokens
            metadata = {
                **qa.metadata,
                "qa_start_unix_ms": qa_start_unix_ms,
                "answer_latency_ms": answer_latency_ms,
                "final_context_chars": context_chars,
                "final_context_tokens": final_tokens,
                "retrieval_latency_ms": retrieval_meta.get("retrieval_latency_ms"),
                "retrieval_route": retrieval_meta.get("retrieval_route"),
                "backend_mode": retrieval_meta.get("backend_mode"),
            }
            if answer_prompt_suffix:
                # Post-hoc attribution marker: lets results/checkpoints be
                # identified as an AP arm (and which prompt revision) even
                # when the launching shell's env is long gone.
                metadata["answer_prompt_suffix_chars"] = len(answer_prompt_suffix)
                metadata["answer_prompt_suffix_sha1"] = answer_prompt_suffix_sha1
            for key, value in extra_metrics.items():
                if key != "final_context_tokens":
                    metadata[key] = value

            result = AnswerResult(
                question_id=qa.question_id,
                question=qa.question,
                answer=answer,
                golden_answer=qa.answer,
                category=qa.category,
                conversation_id=search_result.conversation_id,
                formatted_context=context,  # Save actual context used
                metadata=metadata,
            )

            # Save result
            all_answer_results[qa.question_id] = {
                "question_id": result.question_id,
                "question": result.question,
                "answer": result.answer,
                "golden_answer": result.golden_answer,
                "category": result.category,
                "conversation_id": result.conversation_id,
                "formatted_context": result.formatted_context,  # Save formatted_context
                "metadata": result.metadata,  # Save metadata (contains all_options + latency)
            }
            
            completed += 1
            pbar.update(1)  # Update progress bar
            
            # Save checkpoint periodically
            if checkpoint_manager and (completed % SAVE_INTERVAL == 0 or completed == total_qa_count):
                elapsed = time.time() - start_time
                speed = completed / elapsed if elapsed > 0 else 0
                eta = (total_qa_count - completed) / speed if speed > 0 else 0
                
                tqdm.write(f"Progress: {completed}/{total_qa_count} ({completed/total_qa_count*100:.1f}%) | "
                          f"Speed: {speed:.1f} qa/s | Failed: {failed} | ETA: {eta/60:.1f} min")
                
                checkpoint_manager.save_answer_progress(all_answer_results, completed, total_qa_count)
            
            return result
    
    # Create all pending tasks
    tasks = [
        answer_single_with_tracking(qa, sr)
        for qa, sr in pending_tasks
    ]
    
    # Execute concurrently
    await asyncio.gather(*tasks)
    
    # Close progress bar
    pbar.close()
    
    # Statistics
    elapsed_time = time.time() - start_time
    success_rate = (completed - failed) / completed * 100 if completed > 0 else 0
    
    print(f"\n{'='*60}")
    print(f"✅ All responses generated!")
    print(f"   - Total questions: {total_qa_count}")
    print(f"   - Successful: {completed - failed}")
    print(f"   - Failed: {failed}")
    print(f"   - Success rate: {success_rate:.1f}%")
    print(f"   - Time elapsed: {elapsed_time/60:.1f} minutes ({elapsed_time:.0f}s)")
    print(f"   - Average speed: {total_qa_count/elapsed_time:.1f} qa/s")
    print(f"{'='*60}\n")
    
    # Delete fine-grained checkpoints after completion
    if checkpoint_manager:
        checkpoint_manager.delete_answer_checkpoints()
    
    # Convert to AnswerResult object list (original order)
    results = []
    for qa in qa_pairs:
        if qa.question_id in all_answer_results:
            result_dict = all_answer_results[qa.question_id]
            results.append(AnswerResult(
                question_id=result_dict["question_id"],
                question=result_dict["question"],
                answer=result_dict["answer"],
                golden_answer=result_dict["golden_answer"],
                category=result_dict.get("category"),
                conversation_id=result_dict.get("conversation_id", ""),
                formatted_context=result_dict.get("formatted_context", ""),
                search_results=result_dict.get("search_results", []),
                metadata=result_dict.get("metadata", {}),  # Restore metadata
            ))
    
    return results

