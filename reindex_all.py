#!/usr/bin/env python3
"""
Force reindex Open WebUI file and knowledge collections for embedding dimension migration.

Use this when your embedding model dimension changed
(for example 2048 -> 3072) and old vector collections must be rebuilt.

Run inside the Open WebUI container from /app/backend.
"""

import sys
import os
import asyncio
import inspect
import logging
import time
import gc
from collections import deque
from threading import Lock

print("Script started!", flush=True)

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)


def log_info(msg):
    print(f"[REINDEX] {msg}", flush=True)


def log_error(msg):
    log.error(msg)
    print(f"[REINDEX ERROR] {msg}", flush=True)


class SlidingWindowRateLimiter:
    def __init__(self, limit_per_minute: int):
        self.limit_per_minute = max(int(limit_per_minute), 1)
        self.events = deque()
        self.lock = Lock()

    def reserve_delay(self, weight: int = 1) -> float:
        now = time.monotonic()
        with self.lock:
            while self.events and now - self.events[0][0] >= 60:
                self.events.popleft()

            used = sum(v for _, v in self.events)
            if used + weight <= self.limit_per_minute:
                self.events.append((now, weight))
                return 0.0

            delay = 60 - (now - self.events[0][0])
            return max(delay, 0.5)


class TokenRateLimiter(SlidingWindowRateLimiter):
    def __init__(self, tokens_per_minute: int, chars_per_token: int = 4):
        super().__init__(tokens_per_minute)
        self.tokens_per_minute = self.limit_per_minute
        self.chars_per_token = max(int(chars_per_token), 1)

    def estimate_tokens(self, value) -> int:
        if value is None:
            return 0
        if isinstance(value, str):
            return max(1, (len(value) + self.chars_per_token - 1) // self.chars_per_token)
        if isinstance(value, (list, tuple)):
            return sum(self.estimate_tokens(v) for v in value)
        return self.estimate_tokens(str(value))


def install_embedding_throttle(app):
    tpm_limit = os.getenv("OWUI_REINDEX_TPM_LIMIT", "").strip()
    rpm_limit = os.getenv("OWUI_REINDEX_RPM_LIMIT", "").strip()
    chars_per_token = os.getenv("OWUI_REINDEX_CHARS_PER_TOKEN", "4").strip() or "4"

    if not tpm_limit and not rpm_limit:
        return False

    token_limiter = TokenRateLimiter(int(tpm_limit), int(chars_per_token)) if tpm_limit else None
    request_limiter = SlidingWindowRateLimiter(int(rpm_limit)) if rpm_limit else None
    original = app.state.EMBEDDING_FUNCTION

    async def async_wait(delay: float, reason: str):
        if delay > 0:
            log_info(reason)
            await asyncio.sleep(delay)

    def sync_wait(delay: float, reason: str):
        if delay > 0:
            log_info(reason)
            time.sleep(delay)

    def describe_input(args, kwargs):
        return args[0] if args else kwargs.get("texts") or kwargs.get("text") or kwargs.get("input")

    if inspect.iscoroutinefunction(original):
        async def throttled_embedding_function(*args, **kwargs):
            text_input = describe_input(args, kwargs)
            if request_limiter:
                delay = request_limiter.reserve_delay(1)
                await async_wait(delay, f"RPM throttle: sleeping {delay:.1f}s before embedding request (limit {request_limiter.limit_per_minute}/min)")
            if token_limiter:
                estimated_tokens = token_limiter.estimate_tokens(text_input)
                delay = token_limiter.reserve_delay(estimated_tokens)
                await async_wait(delay, f"TPM throttle: sleeping {delay:.1f}s before embedding batch (~{estimated_tokens} tokens, limit {token_limiter.tokens_per_minute}/min)")
            return await original(*args, **kwargs)
    else:
        def throttled_embedding_function(*args, **kwargs):
            text_input = describe_input(args, kwargs)
            if request_limiter:
                delay = request_limiter.reserve_delay(1)
                sync_wait(delay, f"RPM throttle: sleeping {delay:.1f}s before embedding request (limit {request_limiter.limit_per_minute}/min)")
            if token_limiter:
                estimated_tokens = token_limiter.estimate_tokens(text_input)
                delay = token_limiter.reserve_delay(estimated_tokens)
                sync_wait(delay, f"TPM throttle: sleeping {delay:.1f}s before embedding batch (~{estimated_tokens} tokens, limit {token_limiter.tokens_per_minute}/min)")
            return original(*args, **kwargs)

    app.state.EMBEDDING_FUNCTION = throttled_embedding_function
    log_info(
        "Installed embedding throttle: "
        f"OWUI_REINDEX_TPM_LIMIT={tpm_limit or 'off'}, "
        f"OWUI_REINDEX_RPM_LIMIT={rpm_limit or 'off'}, "
        f"OWUI_REINDEX_CHARS_PER_TOKEN={chars_per_token}"
    )
    return True


def refresh_vector_clients():
    """Recreate vector DB client and patch loaded modules to avoid stale singleton state."""
    from open_webui.config import VECTOR_DB
    from open_webui.retrieval.vector.factory import Vector
    import open_webui.retrieval.vector.factory as vector_factory
    import open_webui.routers.retrieval as retrieval_router

    fresh_client = Vector.get_vector(VECTOR_DB)
    vector_factory.VECTOR_DB_CLIENT = fresh_client
    retrieval_router.VECTOR_DB_CLIENT = fresh_client
    return fresh_client


def delete_collection_force(collection_name: str):
    client = refresh_vector_clients()
    try:
        client.delete_collection(collection_name=collection_name)
        log_info(f"  Deleted collection: {collection_name}")
        return True
    except Exception as e:
        log_info(f"  No collection deleted for {collection_name}: {e}")
        return False


def get_embedding_dimension(app):
    try:
        probe = app.state.EMBEDDING_FUNCTION("dimension probe")
        if hasattr(probe, '__len__'):
            return len(probe)
    except Exception as e:
        log_info(f"Could not probe embedding dimension: {e}")
    return None


def process_file_with_db(request, file_id, collection_name, user, get_session, process_file, ProcessFileForm):
    db = next(get_session())
    try:
        process_file(
            request,
            ProcessFileForm(file_id=file_id, collection_name=collection_name),
            user=user,
            db=db,
        )
    finally:
        db.close()


def reindex_standalone_files(app):
    from open_webui.models.files import Files
    from open_webui.models.users import Users
    from open_webui.models.knowledge import Knowledges
    from open_webui.routers.retrieval import ProcessFileForm, process_file
    from open_webui.internal.db import get_session

    class Request:
        pass

    request = Request()
    request.app = app

    admin_user = Users.get_super_admin_user()
    if not admin_user:
        log_error("No admin user found!")
        return 0, [], set()

    files = Files.get_files()
    total_files = len(files)
    log_info(f"Checking {total_files} files for standalone force reindex...")

    success_count = 0
    failed_files = []
    skipped_count = 0
    touched_knowledge_ids = set()

    for i, file in enumerate(files, 1):
        try:
            if not file.data or not file.data.get("content"):
                skipped_count += 1
                log_info(f"[{i}/{total_files}] Skipping empty file: {file.filename} ({file.id})")
                continue

            file_collection = f"file-{file.id}"
            progress_pct = (i / total_files) * 100
            log_info(
                f"[{i}/{total_files} - {progress_pct:.1f}%] "
                f"Reindexing standalone file: {file.filename} (ID: {file.id})"
            )

            delete_collection_force(file_collection)
            refresh_vector_clients()

            process_file_with_db(
                request,
                file.id,
                None,
                admin_user,
                get_session,
                process_file,
                ProcessFileForm,
            )

            db = next(get_session())
            try:
                knowledges = Knowledges.get_knowledges_by_file_id(file.id, db=db) or []
            finally:
                db.close()

            for knowledge in knowledges:
                touched_knowledge_ids.add(knowledge.id)

            success_count += 1

            if success_count % 10 == 0:
                gc.collect()
                log_info(f"  Memory cleanup performed (processed {success_count} standalone files)")

        except Exception as e:
            log_error(f"Failed to reindex file {file.filename} (ID: {file.id}): {e}")
            failed_files.append(
                {
                    "file_id": file.id,
                    "filename": file.filename,
                    "error": str(e),
                }
            )
            continue

    log_info(
        f"Standalone file reindex complete. Total files checked: {total_files}, "
        f"Skipped: {skipped_count}, Successfully reindexed: {success_count}, "
        f"Failed: {len(failed_files)}"
    )
    return success_count, failed_files, touched_knowledge_ids


def rebuild_knowledge_collections(app, knowledge_ids):
    from open_webui.models.files import Files
    from open_webui.models.users import Users
    from open_webui.models.knowledge import Knowledges
    from open_webui.routers.retrieval import ProcessFileForm, process_file
    from open_webui.internal.db import get_session

    class Request:
        pass

    request = Request()
    request.app = app

    admin_user = Users.get_super_admin_user()
    if not admin_user:
        log_error("No admin user found for knowledge rebuild!")
        return 0, []

    knowledge_ids = sorted(set(knowledge_ids))
    total = len(knowledge_ids)
    log_info(f"Rebuilding {total} knowledge collection(s) from scratch...")

    success_count = 0
    failed = []

    for i, knowledge_id in enumerate(knowledge_ids, 1):
        try:
            progress_pct = (i / total) * 100 if total else 100
            log_info(f"[{i}/{total} - {progress_pct:.1f}%] Rebuilding knowledge collection: {knowledge_id}")

            delete_collection_force(knowledge_id)
            refresh_vector_clients()

            db = next(get_session())
            try:
                knowledge_files = Knowledges.get_file_metadatas_by_id(knowledge_id, db=db) or []
            finally:
                db.close()

            log_info(f"  Knowledge {knowledge_id} has {len(knowledge_files)} file(s)")

            for meta in knowledge_files:
                file_id = meta.id if hasattr(meta, 'id') else meta.get('id')
                if not file_id:
                    continue

                file = Files.get_file_by_id(file_id)
                if not file or not file.data or not file.data.get('content'):
                    log_info(f"  Skipping empty or missing file in knowledge {knowledge_id}: {file_id}")
                    continue

                process_file_with_db(
                    request,
                    file_id,
                    knowledge_id,
                    admin_user,
                    get_session,
                    process_file,
                    ProcessFileForm,
                )

            success_count += 1

            if success_count % 5 == 0:
                gc.collect()
                log_info(f"  Memory cleanup performed (processed {success_count} knowledge collections)")

        except Exception as e:
            log_error(f"Failed to rebuild knowledge collection {knowledge_id}: {e}")
            failed.append({"knowledge_id": knowledge_id, "error": str(e)})
            continue

    return success_count, failed


def main():
    log_info("=" * 80)
    log_info("Starting complete force reindex process")
    log_info("=" * 80)

    start_time = time.time()

    try:
        log_info("Initializing Open WebUI app...")
        from open_webui.main import app
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            app = client.app

            if not hasattr(app.state, "EMBEDDING_FUNCTION"):
                log_error("App state doesn't have EMBEDDING_FUNCTION.")
                sys.exit(1)

            if not hasattr(app.state, "main_loop"):
                log_error("App state doesn't have main_loop.")
                sys.exit(1)

            refresh_vector_clients()
            dim = get_embedding_dimension(app)
            if dim:
                log_info(f"Detected current embedding dimension: {dim}")
            install_embedding_throttle(app)
            log_info(f"App initialized. Embedding function: {type(app.state.EMBEDDING_FUNCTION)}")

            log_info("\n" + "=" * 80)
            log_info("Reindexing Standalone Files")
            log_info("=" * 80)
            file_success, file_failed, touched_knowledge_ids = reindex_standalone_files(app)
            log_info(f"✓ Standalone files reindexed: {file_success}, failed: {len(file_failed)}")

            log_info("\n" + "=" * 80)
            log_info("Rebuilding Knowledge Collections")
            log_info("=" * 80)
            knowledge_success, knowledge_failed = rebuild_knowledge_collections(app, touched_knowledge_ids)
            log_info(f"✓ Knowledge collections rebuilt: {knowledge_success}, failed: {len(knowledge_failed)}")

        elapsed = time.time() - start_time

        log_info("\n" + "=" * 80)
        log_info("REINDEXING COMPLETE!")
        log_info("=" * 80)
        log_info(f"Total time: {elapsed:.2f} seconds ({elapsed/60:.1f} minutes)")
        log_info(f"Standalone files reindexed: {file_success}")
        log_info(f"Knowledge collections rebuilt: {knowledge_success}")

        if file_failed:
            log_info("\nFailed files:")
            for failed in file_failed[:20]:
                log_info(f"  - {failed.get('filename', 'Unknown')} ({failed['file_id']}): {failed['error']}")
            if len(file_failed) > 20:
                log_info(f"  ... and {len(file_failed) - 20} more")

        if knowledge_failed:
            log_info("\nFailed knowledge collections:")
            for failed in knowledge_failed[:20]:
                log_info(f"  - {failed['knowledge_id']}: {failed['error']}")
            if len(knowledge_failed) > 20:
                log_info(f"  ... and {len(knowledge_failed) - 20} more")

        if file_failed or knowledge_failed:
            sys.exit(1)
        sys.exit(0)

    except Exception as e:
        log_error(f"Fatal error during reindexing: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
