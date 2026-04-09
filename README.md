# Open WebUI File Reindex Script

A Python script for force reindexing files and knowledge collections in Open WebUI after a vector database or embedding-dimension migration.

The current version is adapted for newer Open WebUI releases and Chroma-backed migrations where old collections must be rebuilt, not just topped up.

## What It Does

This script reindexes Open WebUI content by:
- Scanning all files in the database
- Rebuilding standalone `file-{id}` collections from scratch
- Detecting knowledge bases that reference those files
- Rebuilding affected knowledge collections from scratch as well
- Reprocessing files through Open WebUI's own indexing pipeline

## How It Works

The script:
1. Initializes the Open WebUI application context with all necessary dependencies
2. Connects to the existing database and vector store
3. Retrieves all files from the database
4. Rebuilds standalone file collections from scratch
5. Finds affected knowledge bases
6. Rebuilds those knowledge collections from scratch

### Data Access

The script accesses data by:
- Using Open WebUI's internal models (`Files`, `Users`) to read from the PostgreSQL database
- Leveraging the vector database client (Qdrant/Chroma/etc.) configured in Open WebUI
- Processing files through Open WebUI's existing `process_file` function
- Running within the application context to access all initialized components

## Important Notes

### ⚠️ Backup Your Data
**Always backup your database before running this script.**

This version intentionally deletes old vector collections before rebuilding them. That is exactly what you want for embedding dimension migrations, but it also means this is no longer a harmless "fill in missing vectors" helper.

### Container Scaling Required
When running this script in an Azure container (or similar environment):
- **Scale up the container** before running to prevent OOM (Out of Memory) errors on larger files
- **Do not turn off the original app** - the script requires the app to be running and initialized
- The script performs memory cleanup every 10 files, but sufficient container resources are still needed

### Resumable Process
If the process fails or is interrupted:
- **You can safely run it again**
- Existing collections will be rebuilt again as needed
- Progress is logged so you can track what was already processed

### Open WebUI auth/env note
Some Open WebUI container setups expose empty auth-related env vars. In that case the script may fail during app initialization unless you provide temporary values just for the script process.

## Usage

### Quick Start

1. **Set up the new vector store** (ensure Open WebUI is configured to use it)

2. **Navigate to the Open WebUI backend directory:**
```bash
cd /app/backend
```

3. **Download the script:**
```bash
curl -o reindex_all.py https://raw.githubusercontent.com/oleh-shulha/owui-file-reindex/refs/heads/main/reindex_all.py
```

4. **Run the script:**
```bash
WEBUI_AUTH=False \
WEBUI_SECRET_KEY='temp-reindex-key' \
WEBUI_JWT_SECRET_KEY='temp-reindex-key' \
OAUTH_SESSION_TOKEN_ENCRYPTION_KEY='temp-reindex-key' \
OAUTH_CLIENT_INFO_ENCRYPTION_KEY='temp-reindex-key' \
python3 reindex_all.py
```

If your embedding provider has strict minute-based limits, you can enable a simple built-in throttle:

```bash
WEBUI_AUTH=False \
WEBUI_SECRET_KEY='temp-reindex-key' \
WEBUI_JWT_SECRET_KEY='temp-reindex-key' \
OAUTH_SESSION_TOKEN_ENCRYPTION_KEY='temp-reindex-key' \
OAUTH_CLIENT_INFO_ENCRYPTION_KEY='temp-reindex-key' \
OWUI_REINDEX_TPM_LIMIT='30000' \
OWUI_REINDEX_RPM_LIMIT='100' \
python3 reindex_all.py
```

Optional:
- `OWUI_REINDEX_TPM_LIMIT` - approximate embedding tokens per minute cap for this script
- `OWUI_REINDEX_RPM_LIMIT` - embedding requests per minute cap for this script
- `OWUI_REINDEX_CHARS_PER_TOKEN` - rough estimator used by the token throttle, default `4`

**Note:** The script must be run from `/app/backend` (or wherever the `open_webui` package is located) to access the necessary Python imports.

## Output

The script provides detailed logging including:
- Detected embedding dimension
- Optional TPM/RPM throttle sleeps when enabled
- Progress percentage and file counts
- Standalone files being rebuilt
- Knowledge collections being rebuilt
- Memory cleanup notifications
- Summary of successful and failed items
- Total execution time
- List of any failed files or knowledge collections with error messages

Example output:
```
[REINDEX] Starting complete reindexing process
[REINDEX] App initialized. Embedding function: <class 'sentence_transformers.SentenceTransformer'>
[REINDEX] Checking 150 files for standalone collections
[REINDEX] Progress: 50/150 (33.3%) - Processed: 35, Skipped: 15
[REINDEX] [51/150 - 34.0%] Reindexing file: document.pdf (ID: abc123)
...
[REINDEX] REINDEXING COMPLETE!
[REINDEX] Total time: 245.30 seconds (4.1 minutes)
[REINDEX] Standalone files reindexed: 135
```

## Requirements

- Python 3
- Open WebUI installation with initialized database
- Access to Open WebUI's Python environment (dependencies included)
- Sufficient container/system resources for processing files  
