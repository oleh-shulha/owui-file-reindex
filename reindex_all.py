#!/usr/bin/env python3
"""
Reindex all knowledge bases and files for vector database migration
Run this inside the Open WebUI container with the app already initialized
"""

import sys
import logging
import time
import gc

print("Script started!", flush=True)

# Set up logging only for errors - Open WebUI will override INFO logs
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)


def log_info(msg):
    """Print info messages to stdout so they're visible"""
    print(f"[REINDEX] {msg}", flush=True)


def log_error(msg):
    """Log errors using the logger"""
    log.error(msg)
    print(f"[REINDEX ERROR] {msg}", flush=True)


def reindex_standalone_files(app):
    """Reindex all standalone files (file-{id} collections) using existing app context"""
    from open_webui.models.files import Files
    from open_webui.routers.retrieval import ProcessFileForm, process_file
    from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT
    from open_webui.models.users import Users
    from open_webui.internal.db import get_session

    class Request:
        pass

    request = Request()
    request.app = app

    admin_user = Users.get_super_admin_user()
    if not admin_user:
        log_error("No admin user found!")
        return 0, []

    files = Files.get_files()
    total_files = len(files)
    log_info(f"Checking {total_files} files for reindexing...")

    success_count = 0
    failed_files = []
    skipped_count = 0

    for i, file in enumerate(files, 1):
        try:
            # Only process files that have content (skip empty/placeholder files)
            if not file.data or not file.data.get("content"):
                skipped_count += 1
                if i % 10 == 0:
                    progress_pct = (i / total_files) * 100
                    log_info(
                        f"Progress: {i}/{total_files} ({progress_pct:.1f}%) - "
                        f"Processed: {success_count}, Skipped: {skipped_count}"
                    )
                continue

            file_collection = f"file-{file.id}"

            try:
                if VECTOR_DB_CLIENT.has_collection(collection_name=file_collection):
                    result = VECTOR_DB_CLIENT.query(
                        collection_name=file_collection,
                        filter={"file_id": file.id}
                    )
                    if result and len(result.ids[0]) > 0:
                        skipped_count += 1
                        if i % 10 == 0:
                            progress_pct = (i / total_files) * 100
                            log_info(
                                f"Progress: {i}/{total_files} ({progress_pct:.1f}%) - "
                                f"Processed: {success_count}, Skipped: {skipped_count}"
                            )
                        continue
            except Exception:
                pass

            progress_pct = (i / total_files) * 100
            log_info(
                f"[{i}/{total_files} - {progress_pct:.1f}%] "
                f"Reindexing file: {file.filename} (ID: {file.id})"
            )

            db = next(get_session())
            try:
                process_file(
                    request,
                    ProcessFileForm(file_id=file.id, collection_name=None),
                    user=admin_user,
                    db=db,
                )
            finally:
                db.close()

            success_count += 1

            # Force garbage collection every 10 files to manage memory
            if success_count % 10 == 0:
                gc.collect()
                log_info(f"  Memory cleanup performed (processed {success_count} files)")

        except Exception as e:
            log_error(f"Failed to reindex file {file.filename} (ID: {file.id}): {e}")
