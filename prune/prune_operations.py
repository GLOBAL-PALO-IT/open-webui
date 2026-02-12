"""
Prune Operations - All Helper Functions

This module contains all the helper functions from backend/open_webui/routers/prune.py
that perform the actual pruning operations, counting, and cleanup.
"""

import inspect
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional, Set, Callable, Any, Dict
from sqlalchemy import select, text, func, and_, or_, not_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def retry_on_db_lock(func: Callable, max_retries: int = 3, base_delay: float = 0.5) -> Any:
    """
    Retry a database operation if it fails due to database lock.
    Uses exponential backoff: 0.5s, 1s, 2s

    Args:
        func: Function to retry
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds (doubles each retry)

    Returns:
        Result from the function

    Raises:
        Last exception if all retries fail
    """
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            return func()
        except OperationalError as e:
            last_exception = e
            if 'database is locked' in str(e).lower() and attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                log.warning(f"Database locked, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise

    # This should never be reached, but just in case
    raise last_exception

# Import Open WebUI modules using compatibility layer (handles pip/docker/git installs)
try:
    from prune_imports import (
        Users, Chat, Chats, ChatFile, ChannelFile, Message, File, Files, Note, Notes,
        Prompt, Prompts, Model, Models, Knowledge, Knowledges,
        Function, Functions, Tool, Tools, Folder, Folders, FolderModel,
        get_db, get_db_context, CACHE_DIR
    )
except ImportError as e:
    log.error(f"Failed to import Open WebUI modules: {e}")
    log.error("This module requires Open WebUI backend modules to be importable")
    raise

from prune_models import PruneDataForm, PrunePreviewResult
from prune_core import collect_file_ids_from_dict


def collect_preview_data(
    form_data: PruneDataForm,
    vector_cleaner,
    progress_callback: Optional[Callable[[str, str], None]] = None
) -> PrunePreviewResult:
    """
    Collect all preview data for prune operations.
    
    This is shared logic used by both standalone_prune.py and prune_cli_interactive.py
    to avoid code duplication and ensure consistency.
    
    Args:
        form_data: Configuration for what to delete
        vector_cleaner: Vector database cleaner instance
        progress_callback: Optional callback(stage_name, message) for progress updates
    
    Returns:
        PrunePreviewResult with counts and lists of items that would be deleted
    """
    def report_progress(stage: str, message: str):
        """Helper to report progress if callback is provided."""
        if progress_callback:
            progress_callback(stage, message)
        else:
            log.debug(f"[{stage}] {message}")
    
    report_progress("load_kb", "Loading knowledge bases...")
    knowledge_bases = Knowledges.get_knowledge_bases()
    
    report_progress("load_users", "Loading user accounts...")
    all_users = Users.get_users()["users"]
    
    report_progress("build_active_users", "Building active user set...")
    active_user_ids = {user.id for user in all_users}
    
    report_progress("filter_kbs", "Filtering active knowledge bases...")
    active_kb_ids = {
        kb.id
        for kb in knowledge_bases
        if kb.user_id in active_user_ids
    }
    
    report_progress("scan_files", "Scanning files in knowledge bases...")
    active_file_ids = get_active_file_ids(knowledge_bases, active_user_ids)
    all_active_file_ids = set().union(*active_file_ids.values()) if active_file_ids else set()
    
    report_progress("count_orphaned", "Counting orphaned database records...")
    orphaned_counts = count_orphaned_records(form_data, all_active_file_ids, active_user_ids)
    
    report_progress("check_embeddings", "Checking vector embeddings...")
    embedded_file_ids = files_already_embedded()
    
    report_progress("group_embedded_files", "Grouping embedded files by knowledge base...")
    embedded_file_records_by_kb = get_embedded_files_by_knowledge_base(
        embedded_file_ids, active_file_ids, knowledge_bases
    )
    
    report_progress("group_embedded_uploads", "Grouping embedded uploads by knowledge base...")
    embedded_upload_files_by_kb = get_embedded_uploads_by_knowledge_base(
        embedded_file_ids, active_file_ids, knowledge_bases
    )
    
    report_progress("find_non_embedded", "Identifying files without embeddings...")
    non_embedded_files_by_kb = get_non_embedded_files_by_knowledge_base(
        embedded_file_ids, active_file_ids, knowledge_bases, active_user_ids
    )
    
    report_progress("scan_vectors", "Scanning vector database for orphaned collections...")
    orphaned_collections_raw = vector_cleaner.get_orphaned_collection_names(
        all_active_file_ids, active_kb_ids, active_user_ids
    )
    
    report_progress("enrich_collections", "Enriching collection metadata...")
    orphaned_collections_enriched = enrich_orphaned_collections(orphaned_collections_raw)
    
    # Count inactive users (if enabled)
    if form_data.delete_inactive_users_days is not None:
        report_progress("count_inactive_users", f"Counting inactive users ({form_data.delete_inactive_users_days}+ days)...")
        inactive_user_count = count_inactive_users(
            form_data.delete_inactive_users_days,
            form_data.exempt_admin_users,
            form_data.exempt_pending_users,
            all_users,
        )
    else:
        inactive_user_count = 0
    
    # Count old chats (if enabled)
    if form_data.days is not None:
        report_progress("count_old_chats", f"Counting old chats ({form_data.days}+ days)...")
        old_chat_count = count_old_chats(
            form_data.days,
            form_data.exempt_archived_chats,
            form_data.exempt_chats_in_folders,
        )
    else:
        old_chat_count = 0
    
    report_progress("scan_uploads", "Scanning upload directory for orphaned files...")
    orphaned_uploads = get_orphaned_uploads(all_active_file_ids)
    
    # Count audio cache files (if enabled)
    if form_data.audio_cache_max_age_days is not None:
        report_progress("count_audio", f"Counting audio cache files ({form_data.audio_cache_max_age_days}+ days)...")
        audio_cache_count = count_audio_cache_files(form_data.audio_cache_max_age_days)
    else:
        audio_cache_count = 0
    
    report_progress("compile", "Compiling preview results...")
    result = PrunePreviewResult(
        inactive_users=inactive_user_count,
        old_chats=old_chat_count,
        orphaned_chats=orphaned_counts["chats"],
        orphaned_files=orphaned_counts["files"],
        orphaned_tools=orphaned_counts["tools"],
        orphaned_functions=orphaned_counts["functions"],
        orphaned_prompts=orphaned_counts["prompts"],
        orphaned_knowledge_bases=orphaned_counts["knowledge_bases"],
        orphaned_models=orphaned_counts["models"],
        orphaned_notes=orphaned_counts["notes"],
        orphaned_folders=orphaned_counts["folders"],
        orphaned_uploads=orphaned_uploads,
        orphaned_vector_collections=orphaned_collections_enriched,
        embedded_file_records=embedded_file_records_by_kb,
        embedded_upload_files=embedded_upload_files_by_kb,
        non_embedded_files=non_embedded_files_by_kb,
        audio_cache_files=audio_cache_count,
    )
    
    report_progress("complete", "Analysis complete!")
    return result


# API Compatibility Helpers
def get_all_folders(db: Optional[Session] = None):
    """
    Get all folders from database.
    Compatibility helper for newer Folders API that doesn't have get_all_folders().

    Args:
        db: Optional database session to reuse (for efficient bulk operations)
    """
    try:
        # Try new API first - if get_all_folders exists, use it
        if hasattr(Folders, 'get_all_folders'):
            # Check if the method supports db parameter
            if 'db' in inspect.signature(Folders.get_all_folders).parameters:
                return Folders.get_all_folders(db=db)
            else:
                return Folders.get_all_folders()

        # Otherwise query directly from database
        with get_db_context(db) as session:
            folders = session.query(Folder).all()
            # Convert to FolderModel instances
            return [FolderModel.model_validate(f) for f in folders]
    except Exception as e:
        log.error(f"Error getting all folders: {e}")
        return []


def count_inactive_users(
    inactive_days: Optional[int], exempt_admin: bool, exempt_pending: bool, all_users
) -> int:
    """Count users that would be deleted for inactivity.

    Args:
        inactive_days: Number of days of inactivity before deletion
        exempt_admin: Whether to exempt admin users
        exempt_pending: Whether to exempt pending users
        all_users: Pre-fetched list of users to avoid duplicate queries
    """
    if inactive_days is None:
        return 0

    cutoff_time = int(time.time()) - (inactive_days * 86400)
    count = 0

    try:
        for user in all_users:
            if exempt_admin and user.role == "admin":
                continue
            if exempt_pending and user.role == "pending":
                continue
            if user.last_active_at < cutoff_time:
                count += 1
    except Exception as e:
        log.debug(f"Error counting inactive users: {e}")

    return count


def count_old_chats(
    days: Optional[int], exempt_archived: bool, exempt_in_folders: bool
) -> int:
    """Count chats that would be deleted by age.

    Uses a SQL COUNT query instead of loading full ORM objects,
    avoiding the expensive deserialization of large JSONB chat columns.
    """
    if days is None:
        return 0

    cutoff_time = int(time.time()) - (days * 86400)

    try:
        with get_db_context() as db:
            # Build filter conditions
            conditions = [Chat.updated_at < cutoff_time]

            if exempt_archived:
                conditions.append(or_(Chat.archived == False, Chat.archived == None))

            if exempt_in_folders:
                conditions.append(and_(
                    Chat.folder_id == None,
                    or_(Chat.pinned == False, Chat.pinned == None)
                ))

            count = db.query(func.count(Chat.id)).filter(*conditions).scalar()
            return count or 0
    except Exception as e:
        log.debug(f"Error counting old chats: {e}")
        return 0


def count_orphaned_records(
    form_data: PruneDataForm,
    active_file_ids: Set[str],
    active_user_ids: Set[str]
) -> dict:
    """Count orphaned database records that would be deleted.

    Uses SQL COUNT queries instead of loading full ORM objects,
    avoiding the expensive deserialization of large JSONB columns
    (chat history, tool specs, function content, etc.).
    """
    counts = {
        "chats": 0,
        "files": 0,
        "tools": 0,
        "functions": 0,
        "prompts": 0,
        "knowledge_bases": 0,
        "models": 0,
        "notes": 0,
        "folders": 0,
    }

    try:
        with get_db_context() as db:
            # Count orphaned files (not in active_file_ids OR owner not in active_user_ids)
            counts["files"] = db.query(func.count(File.id)).filter(
                or_(
                    not_(File.id.in_(active_file_ids)) if active_file_ids else True,
                    not_(File.user_id.in_(active_user_ids)) if active_user_ids else True,
                )
            ).scalar() or 0

            # Count other orphaned records by user ownership
            _table_flag_map = [
                ("chats",          Chat,      Chat.user_id,      form_data.delete_orphaned_chats),
                ("tools",          Tool,      Tool.user_id,      form_data.delete_orphaned_tools),
                ("functions",      Function,  Function.user_id,  form_data.delete_orphaned_functions),
                ("prompts",        Prompt,    Prompt.user_id,    form_data.delete_orphaned_prompts),
                ("knowledge_bases", Knowledge, Knowledge.user_id, form_data.delete_orphaned_knowledge_bases),
                ("models",         Model,     Model.user_id,     form_data.delete_orphaned_models),
                ("notes",          Note,      Note.user_id,      form_data.delete_orphaned_notes),
                ("folders",        Folder,    Folder.user_id,    form_data.delete_orphaned_folders),
            ]

            for key, table_cls, user_id_col, enabled in _table_flag_map:
                if enabled and active_user_ids:
                    counts[key] = db.query(func.count()).select_from(table_cls).filter(
                        not_(user_id_col.in_(active_user_ids))
                    ).scalar() or 0

    except Exception as e:
        log.debug(f"Error counting orphaned records: {e}")

    return counts


def get_orphaned_uploads(active_file_ids: Set[str]) -> list[str]:
    """Get list of orphaned file names in uploads directory."""
    upload_dir = Path(CACHE_DIR).parent / "uploads"
    if not upload_dir.exists():
        return []

    orphaned_files = []
    try:
        for file_path in upload_dir.iterdir():
            if not file_path.is_file():
                continue

            filename = file_path.name
            file_id = None

            # Extract file ID from filename patterns
            if len(filename) > 36:
                potential_id = filename[:36]
                if potential_id.count("-") == 4:
                    file_id = potential_id

            if not file_id and filename.count("-") == 4 and len(filename) == 36:
                file_id = filename

            if not file_id:
                for active_id in active_file_ids:
                    if active_id in filename:
                        file_id = active_id
                        break

            if file_id and file_id not in active_file_ids:
                orphaned_files.append(filename)
    except Exception as e:
        log.debug(f"Error getting orphaned uploads: {e}")

    return orphaned_files


def count_orphaned_uploads(active_file_ids: Set[str]) -> int:
    """Count orphaned files in uploads directory."""
    return len(get_orphaned_uploads(active_file_ids))


def count_audio_cache_files(max_age_days: Optional[int]) -> int:
    """Count audio cache files that would be deleted."""
    if max_age_days is None:
        return 0

    cutoff_time = time.time() - (max_age_days * 86400)
    count = 0

    audio_dirs = [
        Path(CACHE_DIR) / "audio" / "speech",
        Path(CACHE_DIR) / "audio" / "transcriptions",
    ]

    for audio_dir in audio_dirs:
        if not audio_dir.exists():
            continue

        try:
            for file_path in audio_dir.iterdir():
                if file_path.is_file() and file_path.stat().st_mtime < cutoff_time:
                    count += 1
        except Exception as e:
            log.debug(f"Error counting audio files in {audio_dir}: {e}")

    return count


def get_active_file_ids(knowledge_bases, active_user_ids) -> dict[str, Set[str]]:
    """
    Get all file IDs by knowledge base (or category) that are actively referenced by knowledge bases, chats, folders, messages, and models.

    Args:
        knowledge_bases: Pre-fetched list of knowledge bases to avoid duplicate queries
        active_user_ids: Set of active user IDs to filter knowledge bases
    """
    active_file_ids = dict()

    try:
        # Preload all valid file IDs to avoid N database queries during validation
        # This is O(1) set lookup instead of O(n) DB queries
        # Use retry logic in case database is locked
        all_file_ids = retry_on_db_lock(lambda: {f.id for f in Files.get_files()})
        log.debug(f"Preloaded {len(all_file_ids)} file IDs for validation")

        # Scan knowledge bases for file references
        # Note: Since v0.6.41, knowledge.data column was removed and replaced with
        # knowledge_file table. We now use the existing API to query files per KB.
        log.debug(f"Found {len(knowledge_bases)} knowledge bases")

        # Memory-safe processing: iterate through KBs and extract file IDs incrementally
        # We don't keep file objects in memory, just collect IDs
        for kb in knowledge_bases:
            active_file_ids[kb.name] = set()
            # CRITICAL FIX: Skip KBs owned by inactive/deleted users to maintain
            # consistency with active_kb_ids filtering. This prevents false positives
            # where files are considered "active" but their KB is marked as orphaned,
            # leading to incorrectly deleted vector collections.
            if kb.user_id not in active_user_ids:
                log.debug(f"Skipping KB {kb.id} - owner {kb.user_id} not in active users")
                continue

            try:
                # Use existing API method that queries knowledge_file table
                # get_files_by_id() performs:
                # SELECT * FROM file JOIN knowledge_file WHERE knowledge_id = kb.id
                kb_files = Knowledges.get_files_by_id(kb.id)

                # Extract file IDs only (memory efficient - don't keep full objects)
                for file in kb_files:
                    if file.id and file.id in all_file_ids:
                        active_file_ids[kb.name].add(file.id)

                # Help GC by clearing the list immediately after processing
                del kb_files

            except Exception as e:
                log.debug(f"Error scanning files for knowledge base {kb.id}: {e}")

        # Scan chats for file references
        # Stream chats using Core SELECT to avoid ORM overhead
        # Wrap in retry logic in case of database lock
        def scan_chats():
            chat_count = 0
            active_file_ids["chats"] = set()
            with get_db() as db:
                stmt = select(Chat.id, Chat.chat)
                # SQLAlchemy 2.0+ compatibility: execution_options moved to statement
                try:
                    result = db.execute(stmt.execution_options(stream_results=True))
                except AttributeError:
                    # Fallback for older SQLAlchemy versions
                    result = db.execution_options(stream_results=True).execute(stmt)

                while True:
                    rows = result.fetchmany(1000)
                    if not rows:
                        break

                    for chat_id, chat_dict in rows:
                        chat_count += 1

                        # Skip if no chat data or not a dict
                        if not chat_dict or not isinstance(chat_dict, dict):
                            continue

                        try:
                            # Direct dict traversal (no json.dumps needed)
                            collect_file_ids_from_dict(chat_dict, active_file_ids["chats"], all_file_ids)
                        except Exception as e:
                            log.debug(f"Error processing chat {chat_id} for file references: {e}")

            return chat_count

        chat_count = retry_on_db_lock(scan_chats)
        log.debug(f"Scanned {chat_count} chats for file references")

        # Scan chat_file table for file references
        # Note: Since v0.6.41+, chat files are stored in dedicated chat_file junction table.
        # We scan both the chat.chat JSON (legacy) and chat_file table (new) to ensure completeness.
        try:
            with get_db() as db:
                stmt = select(ChatFile.file_id)
                # SQLAlchemy 2.0+ compatibility
                try:
                    result = db.execute(stmt.execution_options(stream_results=True))
                except AttributeError:
                    result = db.execution_options(stream_results=True).execute(stmt)

                chat_file_count = 0
                while True:
                    rows = result.fetchmany(1000)
                    if not rows:
                        break

                    for (file_id,) in rows:
                        chat_file_count += 1
                        if file_id and file_id in all_file_ids:
                            active_file_ids["chats"].add(file_id)

                log.debug(f"Scanned {chat_file_count} chat_file entries for file references")
        except Exception as e:
            # chat_file table might not exist in older database versions
            log.debug(f"Error scanning chat_file table (table may not exist yet): {e}")

        # Scan channel_file table for file references
        # Note: Channels feature uses dedicated channel_file junction table for file attachments.
        # Files referenced only by channels would be incorrectly marked as orphaned without this scan.
        try:
            with get_db() as db:
                stmt = select(ChannelFile.file_id)
                # SQLAlchemy 2.0+ compatibility
                try:
                    result = db.execute(stmt.execution_options(stream_results=True))
                except AttributeError:
                    result = db.execution_options(stream_results=True).execute(stmt)

                channel_file_count = 0
                while True:
                    rows = result.fetchmany(1000)
                    if not rows:
                        break

                    for (file_id,) in rows:
                        channel_file_count += 1
                        if file_id and file_id in all_file_ids:
                            # Add to chats category for consistency (channels are similar to chats)
                            active_file_ids["chats"].add(file_id)

                log.debug(f"Scanned {channel_file_count} channel_file entries for file references")
        except Exception as e:
            # channel_file table might not exist in older database versions
            log.debug(f"Error scanning channel_file table (table may not exist yet): {e}")

        # Scan folders for file references
        # Stream folders using Core SELECT to avoid ORM overhead
        try:
            active_file_ids["folders"] = set()
            with get_db() as db:
                stmt = select(Folder.id, Folder.items, Folder.data)
                # SQLAlchemy 2.0+ compatibility: execution_options moved to statement
                try:
                    result = db.execute(stmt.execution_options(stream_results=True))
                except AttributeError:
                    # Fallback for older SQLAlchemy versions
                    result = db.execution_options(stream_results=True).execute(stmt)

                while True:
                    rows = result.fetchmany(100)
                    if not rows:
                        break

                    for folder_id, items_dict, data_dict in rows:
                        # Process folder.items
                        if items_dict:
                            try:
                                # Direct dict traversal (no json.dumps needed)
                                collect_file_ids_from_dict(items_dict, active_file_ids["folders"], all_file_ids)
                            except Exception as e:
                                log.debug(f"Error processing folder {folder_id} items: {e}")

                        # Process folder.data
                        if data_dict:
                            try:
                                # Direct dict traversal (no json.dumps needed)
                                collect_file_ids_from_dict(data_dict, active_file_ids['folders'], all_file_ids)
                            except Exception as e:
                                log.debug(f"Error processing folder {folder_id} data: {e}")

        except Exception as e:
            log.debug(f"Error scanning folders for file references: {e}")

        # Scan standalone messages for file references
        # Stream messages using Core SELECT to avoid text() and yield_per issues
        try:
            active_file_ids["messages"] = set()
            with get_db() as db:
                stmt = select(Message.id, Message.data).where(Message.data.isnot(None))
                # SQLAlchemy 2.0+ compatibility: execution_options moved to statement
                try:
                    result = db.execute(stmt.execution_options(stream_results=True))
                except AttributeError:
                    # Fallback for older SQLAlchemy versions
                    result = db.execution_options(stream_results=True).execute(stmt)

                while True:
                    rows = result.fetchmany(1000)
                    if not rows:
                        break

                    for message_id, message_data_dict in rows:
                        if message_data_dict:
                            try:
                                # Direct dict traversal (no json.dumps needed)
                                collect_file_ids_from_dict(message_data_dict, active_file_ids['messages'], all_file_ids)
                            except Exception as e:
                                log.debug(f"Error processing message {message_id} data: {e}")

        except Exception as e:
            log.debug(f"Error scanning messages for file references: {e}")

        # Scan models for file references in params and meta fields
        # Models can have files attached (e.g. in meta or params JSON fields)
        try:
            active_file_ids["models"] = set()
            with get_db() as db:
                stmt = select(Model.id, Model.params, Model.meta)
                # SQLAlchemy 2.0+ compatibility
                try:
                    result = db.execute(stmt.execution_options(stream_results=True))
                except AttributeError:
                    result = db.execution_options(stream_results=True).execute(stmt)

                model_count = 0
                while True:
                    rows = result.fetchmany(100)
                    if not rows:
                        break

                    for model_id, params_dict, meta_dict in rows:
                        model_count += 1

                        # Scan params JSON field for file references
                        if params_dict and isinstance(params_dict, dict):
                            try:
                                collect_file_ids_from_dict(params_dict, active_file_ids['models'], all_file_ids)
                            except Exception as e:
                                log.debug(f"Error processing model {model_id} params: {e}")

                        # Scan meta JSON field for file references
                        if meta_dict and isinstance(meta_dict, dict):
                            try:
                                collect_file_ids_from_dict(meta_dict, active_file_ids['models'], all_file_ids)
                            except Exception as e:
                                log.debug(f"Error processing model {model_id} meta: {e}")

                log.debug(f"Scanned {model_count} models for file references")

        except Exception as e:
            log.debug(f"Error scanning models for file references: {e}")

    except Exception as e:
        log.error(f"Error determining active file IDs: {e}")
        return {
            'Knowledge bases': set(), 
            'chats': set(), 
            'folders': set(), 
            'messages': set(), 
            'models': set()
        }

    log.info(f"Found {len(active_file_ids)} active file IDs")
    return active_file_ids


def safe_delete_file_by_id(file_id: str, vector_cleaner, db: Optional[Session] = None) -> bool:
    """
    Safely delete a file record and its associated vector collection.

    Args:
        file_id: The file ID to delete
        vector_cleaner: Vector database cleaner instance
        db: Optional database session to reuse (for efficient bulk operations)

    Returns:
        True if deletion succeeded, False otherwise
    """
    try:
        with get_db_context(db) as session:
            file_record = Files.get_file_by_id(file_id, db=session)
            if not file_record:
                return True

            # Use modular vector database cleaner
            collection_name = f"file-{file_id}"
            vector_cleaner.delete_collection(collection_name)

            Files.delete_file_by_id(file_id, db=session)
            return True

    except Exception as e:
        log.error(f"Error deleting file {file_id}: {e}")
        return False


def cleanup_orphaned_uploads(active_file_ids: Set[str]) -> int:
    """
    Clean up orphaned files in the uploads directory.

    Returns the number of files deleted.
    """
    upload_dir = Path(CACHE_DIR).parent / "uploads"
    if not upload_dir.exists():
        return 0

    deleted_count = 0

    try:
        for file_path in upload_dir.iterdir():
            if not file_path.is_file():
                continue

            filename = file_path.name
            file_id = None

            # Extract file ID from filename patterns
            if len(filename) > 36:
                potential_id = filename[:36]
                if potential_id.count("-") == 4:
                    file_id = potential_id

            if not file_id and filename.count("-") == 4 and len(filename) == 36:
                file_id = filename

            if not file_id:
                for active_id in active_file_ids:
                    if active_id in filename:
                        file_id = active_id
                        break

            if file_id and file_id not in active_file_ids:
                try:
                    file_path.unlink()
                    deleted_count += 1
                except Exception as e:
                    log.error(f"Failed to delete upload file {filename}: {e}")

    except Exception as e:
        log.error(f"Error cleaning uploads directory: {e}")

    if deleted_count > 0:
        log.info(f"Deleted {deleted_count} orphaned upload files")

    return deleted_count


def delete_inactive_users(
    inactive_days: int, exempt_admin: bool = True, exempt_pending: bool = True
) -> int:
    """
    Delete users who have been inactive for the specified number of days.

    Returns the number of users deleted.
    """
    if inactive_days is None:
        return 0

    cutoff_time = int(time.time()) - (inactive_days * 86400)
    deleted_count = 0

    try:
        users_to_delete = []

        # Get all users and check activity
        all_users = Users.get_users()["users"]

        for user in all_users:
            # Skip if user is exempt
            if exempt_admin and user.role == "admin":
                continue
            if exempt_pending and user.role == "pending":
                continue

            # Check if user is inactive based on last_active_at
            if user.last_active_at < cutoff_time:
                users_to_delete.append(user)

        # Delete inactive users with shared database session
        with get_db() as db:
            for user in users_to_delete:
                try:
                    # Delete the user - this will cascade to all their data
                    Users.delete_user_by_id(user.id, db=db)
                    deleted_count += 1
                    log.info(
                        f"Deleted inactive user: {user.email} (last active: {user.last_active_at})"
                    )
                except Exception as e:
                    log.error(f"Failed to delete user {user.id}: {e}")

    except Exception as e:
        log.error(f"Error during inactive user deletion: {e}")

    return deleted_count


def files_already_embedded() -> Set[str]:
    """
    Get set of file IDs that have vector embeddings in the DocumentChunk table.
    These are files that have been processed and have chunks in the pgvector database.

    Returns:
        Set of file IDs that have vector embeddings
    """
    embedded_file_ids = set()

    try:
        from open_webui.retrieval.vector.dbs.pgvector import DocumentChunk

        with get_db() as db:
            # Query all unique collection names from DocumentChunk table
            # Collection names follow pattern: "file-{file_id}"
            stmt = select(DocumentChunk.collection_name).distinct()
            result = db.execute(stmt)

            for (collection_name,) in result:
                if collection_name and collection_name.startswith("file-"):
                    file_id = collection_name[5:]  # Remove "file-" prefix
                    embedded_file_ids.add(file_id)

        log.info(f"Found {len(embedded_file_ids)} files with vector embeddings")

    except ImportError:
        log.warning("pgvector DocumentChunk not available, cannot determine embedded files")
    except Exception as e:
        log.error(f"Error determining embedded files: {e}")

    return embedded_file_ids


def uploads_already_embedded(embedded_file_ids: Set[str]) -> list[str]:
    """
    Get list of upload file names that have vector embeddings.
    These are physical files in the uploads directory that correspond to embedded files.

    Args:
        embedded_file_ids: Pre-computed set of embedded file IDs (from files_already_embedded)

    Returns:
        List of file names in uploads directory that have embeddings
    """
    if not embedded_file_ids:
        return []

    upload_dir = Path(CACHE_DIR).parent / "uploads"
    if not upload_dir.exists():
        log.error(f"Upload directory does not exist: {upload_dir}")
        return []

    embedded_uploads = []

    try:
        for file_path in upload_dir.iterdir():
            if not file_path.is_file():
                continue

            filename = file_path.name
            file_id = None

            # Extract file ID from filename patterns
            if len(filename) > 36:
                potential_id = filename[:36]
                if potential_id.count("-") == 4:
                    file_id = potential_id

            if not file_id and filename.count("-") == 4 and len(filename) == 36:
                file_id = filename

            if not file_id:
                for embedded_id in embedded_file_ids:
                    if embedded_id in filename:
                        file_id = embedded_id
                        break

            if file_id and file_id in embedded_file_ids:
                embedded_uploads.append(filename)

    except Exception as e:
        log.error(f"Error getting embedded uploads: {e}")

    log.info(f"Found {len(embedded_uploads)} upload files with embeddings")
    return embedded_uploads


def cleanup_audio_cache(max_age_days: Optional[int] = 30) -> int:
    """
    Clean up audio cache files older than specified days.

    Returns:
        Number of files deleted
    """
    if max_age_days is None:
        log.info("Skipping audio cache cleanup (max_age_days is None)")
        return 0

    cutoff_time = time.time() - (max_age_days * 86400)
    deleted_count = 0
    total_size_deleted = 0

    audio_dirs = [
        Path(CACHE_DIR) / "audio" / "speech",
        Path(CACHE_DIR) / "audio" / "transcriptions",
    ]

    for audio_dir in audio_dirs:
        if not audio_dir.exists():
            continue

        try:
            for file_path in audio_dir.iterdir():
                if not file_path.is_file():
                    continue

                stat_info = file_path.stat()
                file_mtime = stat_info.st_mtime
                if file_mtime < cutoff_time:
                    try:
                        file_size = stat_info.st_size
                        file_path.unlink()
                        deleted_count += 1
                        total_size_deleted += file_size
                        log.debug(f"Deleted audio cache file: {file_path} ({file_size} bytes)")
                    except Exception as e:
                        log.error(f"Failed to delete audio file {file_path}: {e}")

        except Exception as e:
            log.error(f"Error cleaning audio directory {audio_dir}: {e}")

    log.info(f"Audio cache cleanup: deleted {deleted_count} files, freed {total_size_deleted} bytes")
    return deleted_count


def get_embedded_files_by_knowledge_base(
    embedded_file_ids: Set[str],
    active_file_ids: dict[str, Set[str]],
    knowledge_bases
) -> dict[str, list[str]]:
    """
    Get embedded file records grouped by knowledge base name.
    
    Args:
        embedded_file_ids: Pre-computed set of embedded file IDs (from files_already_embedded)
        active_file_ids: Dict mapping KB names to sets of file IDs (from get_active_file_ids)
        knowledge_bases: Pre-fetched list of knowledge bases
    
    Returns:
        Dict mapping knowledge base names to lists of file names that have embeddings
    """
    if not embedded_file_ids:
        return {}
    
    # Get all files from database
    try:
        with get_db() as db:
            all_files = {f.id: f for f in Files.get_files(db=db)}
    except Exception as e:
        log.error(f"Error getting files from database: {e}")
        return {}
    
    # Build result: KB name -> list of file names
    result = {}
    
    for kb_name, file_ids in active_file_ids.items():
        embedded_in_kb = []
        for file_id in file_ids:
            if file_id in embedded_file_ids:
                file = all_files.get(file_id)
                if file:
                    # Get file name from meta or fallback to filename
                    if hasattr(file, 'meta') and file.meta:
                        filename = file.meta.get('name', file.filename)
                    else:
                        filename = file.filename
                    embedded_in_kb.append(filename)
        
        if embedded_in_kb:
            result[kb_name] = embedded_in_kb
    
    total_files = sum(len(files) for files in result.values())
    log.info(f"Found {total_files} embedded file records across {len(result)} knowledge bases")
    
    return result


def get_embedded_uploads_by_knowledge_base(
    embedded_file_ids: Set[str],
    active_file_ids: dict[str, Set[str]],
    knowledge_bases
) -> dict[str, list[str]]:
    """
    Get embedded upload files grouped by knowledge base name.
    
    Args:
        embedded_file_ids: Pre-computed set of embedded file IDs (from files_already_embedded)
        active_file_ids: Dict mapping KB names to sets of file IDs (from get_active_file_ids)
        knowledge_bases: Pre-fetched list of knowledge bases
    
    Returns:
        Dict mapping knowledge base names to lists of upload filenames that have embeddings
    """
    if not embedded_file_ids:
        return {}
    
    upload_dir = Path(CACHE_DIR).parent / "uploads"
    if not upload_dir.exists():
        log.error(f"Upload directory does not exist: {upload_dir}")
        return {}
    
    # Get all upload files that are embedded
    embedded_uploads_list = uploads_already_embedded(embedded_file_ids)
    if not embedded_uploads_list:
        return {}
    
    # Create a mapping from file IDs to upload filenames
    file_id_to_upload = {}
    for filename in embedded_uploads_list:
        # Extract file ID from filename
        file_id = None
        if len(filename) > 36:
            potential_id = filename[:36]
            if potential_id.count("-") == 4:
                file_id = potential_id
        
        if not file_id and filename.count("-") == 4 and len(filename) == 36:
            file_id = filename
        
        if not file_id:
            for embedded_id in embedded_file_ids:
                if embedded_id in filename:
                    file_id = embedded_id
                    break
        
        if file_id:
            file_id_to_upload[file_id] = filename
    
    # Build result: KB name -> list of upload filenames
    result = {}
    
    for kb_name, file_ids in active_file_ids.items():
        embedded_uploads_in_kb = []
        for file_id in file_ids:
            if file_id in file_id_to_upload:
                embedded_uploads_in_kb.append(file_id_to_upload[file_id])
        
        if embedded_uploads_in_kb:
            result[kb_name] = embedded_uploads_in_kb
    
    total_uploads = sum(len(files) for files in result.values())
    log.info(f"Found {total_uploads} embedded upload files across {len(result)} knowledge bases")
    
    return result


def get_non_embedded_files_by_knowledge_base(
    embedded_file_ids: Set[str],
    active_file_ids: dict[str, Set[str]],
    knowledge_bases,
    active_user_ids: Set[str]
) -> dict[str, list[str]]:
    """
    Get non-embedded file records grouped by knowledge base name.
    These are files that are active but don't have vector embeddings yet.
    
    Args:
        embedded_file_ids: Set of embedded file IDs (from files_already_embedded)
        active_file_ids: Dict mapping KB IDs to sets of file IDs (from get_active_file_ids)
        knowledge_bases: List of knowledge bases
        active_user_ids: Set of active user IDs to filter knowledge bases
    
    Returns:
        Dict mapping knowledge base names to lists of file names that don't have embeddings
    """
    # Get all files from database
    try:
        with get_db() as db:
            all_files = {f.id: f for f in Files.get_files(db=db)}
    except Exception as e:
        log.error(f"Error getting files from database: {e}")
        return {}
    
    # Build result: KB name -> list of file names (non-embedded only)
    result = {}
    
    for kb in knowledge_bases:
        if kb.user_id in active_user_ids:
            kb_name = kb.name
            kb_file_ids = active_file_ids.get(kb.id, set())
            # Files that are active but NOT embedded
            non_embedded_ids = kb_file_ids - embedded_file_ids
            
            if non_embedded_ids:
                non_embedded_files = []
                for file_id in non_embedded_ids:
                    file = all_files.get(file_id)
                    if file:
                        # Get file name from meta or fallback to filename
                        if hasattr(file, 'meta') and file.meta:
                            filename = file.meta.get('name', file.filename)
                        else:
                            filename = file.filename
                        non_embedded_files.append(filename)
                
                if non_embedded_files:
                    result[kb_name] = non_embedded_files
    
    total_files = sum(len(files) for files in result.values())
    log.info(f"Found {total_files} non-embedded file records across {len(result)} knowledge bases")
    
    return result


def enrich_orphaned_collections(
    collection_names: list[str]
) -> list[dict[str, str]]:
    """
    Enrich orphaned vector collection names with additional metadata.
    
    Args:
        collection_names: List of orphaned collection names (e.g., 'file-uuid', 'kb-uuid')
    
    Returns:
        List of dictionaries with collection metadata including type, ID, and name lookups
    """
    enriched = []
    
    # Build lookup maps
    file_map = {}
    kb_map = {}
    
    try:
        with get_db() as db:
            # Get all files for name lookups
            for f in Files.get_files(db=db):
                file_map[f.id] = f.meta.get('name', f.filename) if f.meta else f.filename
            
            # Get all knowledge bases for name lookups
            for kb in Knowledges.get_knowledge_bases(db=db):
                kb_map[kb.id] = kb.name
    except Exception as e:
        log.error(f"Error building lookup maps for orphaned collections: {e}")
    
    for collection_name in collection_names:
        if not collection_name:
            continue
            
        # Parse collection name to extract type and ID
        if collection_name.startswith('file-'):
            file_id = collection_name[5:]  # Remove 'file-' prefix
            name = file_map.get(file_id, "[Deleted file]")
            enriched.append({
                'collection': collection_name,
                'type': 'File',
                'id': file_id,
                'name': name
            })
        elif collection_name.startswith('kb-'):
            kb_id = collection_name[3:]  # Remove 'kb-' prefix
            name = kb_map.get(kb_id, "[Deleted knowledge base]")
            enriched.append({
                'collection': collection_name,
                'type': 'Knowledge Base',
                'id': kb_id,
                'name': name
            })
        elif collection_name.startswith('user-'):
            user_id = collection_name[5:]  # Remove 'user-' prefix
            enriched.append({
                'collection': collection_name,
                'type': 'User',
                'id': user_id,
                'name': "[User collection]"
            })
        elif collection_name.startswith('<unknown:'):
            enriched.append({
                'collection': collection_name,
                'type': 'Unknown',
                'id': collection_name,
                'name': "[Orphaned segment]"
            })
        else:
            # Unknown format, just show as-is
            enriched.append({
                'collection': collection_name,
                'type': 'Other',
                'id': collection_name,
                'name': collection_name
            })
    
    log.info(f"Enriched {len(enriched)} orphaned collection names")
    return enriched


def execute_prune_deletion(
    form_data: "PruneDataForm",
    vector_cleaner,
    progress_callback: Optional[Callable[[str, str, int], None]] = None
) -> Dict[str, int]:
    """
    Execute the actual prune deletion operations.
    
    This is the shared deletion logic used by both standalone_prune.py and 
    prune_cli_interactive.py to avoid code duplication.
    
    Args:
        form_data: PruneDataForm with configuration for what to delete
        vector_cleaner: Vector database cleaner instance (ChromaDatabaseCleaner or PGVectorDatabaseCleaner)
        progress_callback: Optional callback function(stage, message, count) for progress updates
            - stage: str - e.g., "stage0", "stage1", etc.
            - message: str - human-readable description
            - count: int - number of items affected
    
    Returns:
        Dict with deletion counts for each category:
        {
            'inactive_users': int,
            'old_chats': int,
            'orphaned_files': int,
            'orphaned_kbs': int,
            'orphaned_chats': int,
            'orphaned_tools': int,
            'orphaned_functions': int,
            'orphaned_prompts': int,
            'orphaned_models': int,
            'orphaned_notes': int,
            'orphaned_folders': int,
            'orphaned_uploads': int,
            'orphaned_vectors': int,
            'embedded_files': int,
            'embedded_uploads': int,
            'audio_cache': int,
            'vacuumed': bool
        }
    """
    def report_progress(stage: str, message: str, count: int = 0):
        """Helper to report progress if callback is provided."""
        if progress_callback:
            progress_callback(stage, message, count)
        else:
            log.info(f"{message}: {count}" if count > 0 else message)
    
    results = {
        'inactive_users': 0,
        'old_chats': 0,
        'orphaned_files': 0,
        'orphaned_kbs': 0,
        'orphaned_chats': 0,
        'orphaned_tools': 0,
        'orphaned_functions': 0,
        'orphaned_prompts': 0,
        'orphaned_models': 0,
        'orphaned_notes': 0,
        'orphaned_folders': 0,
        'orphaned_uploads': 0,
        'orphaned_vectors': 0,
        'embedded_files': 0,
        'embedded_uploads': 0,
        'audio_cache': 0,
        'vacuumed': False
    }
    
    # Import here to avoid circular dependency
    from prune_core import ChromaDatabaseCleaner, PGVectorDatabaseCleaner
    
    # Stage 0: Delete inactive users (if enabled)
    if form_data.delete_inactive_users_days is not None:
        report_progress(
            "stage0",
            f"Deleting users inactive for more than {form_data.delete_inactive_users_days} days",
            0
        )
        deleted_users = delete_inactive_users(
            form_data.delete_inactive_users_days,
            form_data.exempt_admin_users,
            form_data.exempt_pending_users,
        )
        results['inactive_users'] = deleted_users
        report_progress("stage0", "Deleted inactive users", deleted_users)
    else:
        report_progress("stage0", "Skipping inactive user deletion (disabled)", 0)

    # Stage 1: Delete old chats based on user criteria
    if form_data.days is not None:
        report_progress("stage1", f"Deleting chats older than {form_data.days} days", 0)
        cutoff_time = int(time.time()) - (form_data.days * 86400)
        chats_to_delete = []

        with get_db() as db:
            for chat in Chats.get_chats(db=db):
                if chat.updated_at < cutoff_time:
                    if form_data.exempt_archived_chats and chat.archived:
                        continue
                    if form_data.exempt_chats_in_folders and (
                        getattr(chat, "folder_id", None) is not None
                        or getattr(chat, "pinned", False)
                    ):
                        continue
                    chats_to_delete.append(chat)

            if chats_to_delete:
                for chat in chats_to_delete:
                    Chats.delete_chat_by_id(chat.id, db=db)
                results['old_chats'] = len(chats_to_delete)
                report_progress("stage1", "Deleted old chats", len(chats_to_delete))
            else:
                report_progress("stage1", f"No chats found older than {form_data.days} days", 0)
    else:
        report_progress("stage1", "Skipping chat deletion (days parameter is None)", 0)

    # Stage 2: Build preservation set
    report_progress("stage2", "Building preservation set", 0)

    active_user_ids = {user.id for user in Users.get_users()["users"]}
    log.debug(f"Found {len(active_user_ids)} active users")

    active_kb_ids = set()
    knowledge_bases = Knowledges.get_knowledge_bases()

    for kb in knowledge_bases:
        if kb.user_id in active_user_ids:
            active_kb_ids.add(kb.id)

    log.debug(f"Found {len(active_kb_ids)} active knowledge bases")

    active_file_ids = get_active_file_ids(knowledge_bases, active_user_ids)
    all_active_file_ids = set().union(*active_file_ids.values()) if active_file_ids else set()

    # Stage 3: Delete orphaned database records
    report_progress("stage3", "Deleting orphaned database records", 0)

    # Delete orphaned files
    deleted_files = 0
    with get_db() as db:
        for file_record in Files.get_files(db=db):
            should_delete = (
                file_record.id not in all_active_file_ids
                or file_record.user_id not in active_user_ids
            )

            if should_delete:
                if safe_delete_file_by_id(file_record.id, vector_cleaner, db=db):
                    deleted_files += 1

    results['orphaned_files'] = deleted_files
    report_progress("stage3", "Deleted orphaned files", deleted_files)

    # Delete orphaned knowledge bases
    deleted_kbs = 0
    if form_data.delete_orphaned_knowledge_bases:
        with get_db() as db:
            for kb in Knowledges.get_knowledge_bases(db=db):
                if kb.user_id not in active_user_ids:
                    if vector_cleaner.delete_collection(kb.id):
                        Knowledges.delete_knowledge_by_id(kb.id, db=db)
                        deleted_kbs += 1

        results['orphaned_kbs'] = deleted_kbs
        report_progress("stage3", "Deleted orphaned knowledge bases", deleted_kbs)
    else:
        report_progress("stage3", "Skipping knowledge base deletion (disabled)", 0)

    # Delete orphaned chats
    if form_data.delete_orphaned_chats:
        chats_deleted = 0
        with get_db() as db:
            for chat in Chats.get_chats(db=db):
                if chat.user_id not in active_user_ids:
                    Chats.delete_chat_by_id(chat.id, db=db)
                    chats_deleted += 1
        results['orphaned_chats'] = chats_deleted
        report_progress("stage3", "Deleted orphaned chats", chats_deleted)
    else:
        report_progress("stage3", "Skipping orphaned chat deletion (disabled)", 0)

    # Delete orphaned tools
    if form_data.delete_orphaned_tools:
        tools_deleted = 0
        with get_db() as db:
            for tool in Tools.get_tools(db=db):
                if tool.user_id not in active_user_ids:
                    Tools.delete_tool_by_id(tool.id, db=db)
                    tools_deleted += 1
        results['orphaned_tools'] = tools_deleted
        report_progress("stage3", "Deleted orphaned tools", tools_deleted)
    else:
        report_progress("stage3", "Skipping tool deletion (disabled)", 0)

    # Delete orphaned functions
    if form_data.delete_orphaned_functions:
        functions_deleted = 0
        with get_db() as db:
            for function in Functions.get_functions(db=db):
                if function.user_id not in active_user_ids:
                    Functions.delete_function_by_id(function.id, db=db)
                    functions_deleted += 1
        results['orphaned_functions'] = functions_deleted
        report_progress("stage3", "Deleted orphaned functions", functions_deleted)
    else:
        report_progress("stage3", "Skipping function deletion (disabled)", 0)

    # Delete orphaned notes
    if form_data.delete_orphaned_notes:
        notes_deleted = 0
        with get_db() as db:
            for note in Notes.get_notes(db=db):
                if note.user_id not in active_user_ids:
                    Notes.delete_note_by_id(note.id, db=db)
                    notes_deleted += 1
        results['orphaned_notes'] = notes_deleted
        report_progress("stage3", "Deleted orphaned notes", notes_deleted)
    else:
        report_progress("stage3", "Skipping note deletion (disabled)", 0)

    # Delete orphaned prompts
    if form_data.delete_orphaned_prompts:
        prompts_deleted = 0
        with get_db() as db:
            for prompt in Prompts.get_prompts(db=db):
                if prompt.user_id not in active_user_ids:
                    Prompts.delete_prompt_by_command(prompt.command, db=db)
                    prompts_deleted += 1
        results['orphaned_prompts'] = prompts_deleted
        report_progress("stage3", "Deleted orphaned prompts", prompts_deleted)
    else:
        report_progress("stage3", "Skipping prompt deletion (disabled)", 0)

    # Delete orphaned models
    if form_data.delete_orphaned_models:
        models_deleted = 0
        with get_db() as db:
            for model in Models.get_all_models(db=db):
                if model.user_id not in active_user_ids:
                    Models.delete_model_by_id(model.id, db=db)
                    models_deleted += 1
        results['orphaned_models'] = models_deleted
        report_progress("stage3", "Deleted orphaned models", models_deleted)
    else:
        report_progress("stage3", "Skipping model deletion (disabled)", 0)

    # Delete orphaned folders
    if form_data.delete_orphaned_folders:
        folders_deleted = 0
        with get_db() as db:
            for folder in get_all_folders(db=db):
                if folder.user_id not in active_user_ids:
                    Folders.delete_folder_by_id_and_user_id(
                        folder.id, folder.user_id, db=db
                    )
                    folders_deleted += 1
        results['orphaned_folders'] = folders_deleted
        report_progress("stage3", "Deleted orphaned folders", folders_deleted)
    else:
        report_progress("stage3", "Skipping folder deletion (disabled)", 0)

    # Stage 4: Clean up orphaned physical files
    report_progress("stage4", "Cleaning up orphaned physical files", 0)

    final_active_user_ids = {user.id for user in Users.get_users()["users"]}
    final_knowledge_bases = Knowledges.get_knowledge_bases()
    final_active_kb_ids = {kb.id for kb in final_knowledge_bases if kb.user_id in final_active_user_ids}
    final_active_file_ids = get_active_file_ids(final_knowledge_bases, final_active_user_ids)
    all_final_active_file_ids = set().union(*final_active_file_ids.values()) if final_active_file_ids else set()

    deleted_uploads = cleanup_orphaned_uploads(all_final_active_file_ids)
    results['orphaned_uploads'] = deleted_uploads
    report_progress("stage4", "Deleted orphaned upload files", deleted_uploads)

    # Use modular vector database cleanup
    deleted_vector_count, vector_error = vector_cleaner.cleanup_orphaned_collections(
        all_final_active_file_ids, final_active_kb_ids, final_active_user_ids
    )
    if vector_error:
        log.warning(f"Vector cleanup completed with errors: {vector_error}")
    results['orphaned_vectors'] = deleted_vector_count
    report_progress("stage4", "Deleted orphaned vector collections", deleted_vector_count)

    # Delete embedded files (files with vector embeddings)
    report_progress("stage4", "Deleting embedded file records", 0)
    embedded_file_ids = files_already_embedded()
    deleted_embedded = 0
    with get_db() as db:
        for file_id in embedded_file_ids:
            if safe_delete_file_by_id(file_id, vector_cleaner, db=db):
                deleted_embedded += 1
    results['embedded_files'] = deleted_embedded
    report_progress("stage4", "Deleted embedded file records", deleted_embedded)

    # Delete embedded upload files
    report_progress("stage4", "Deleting embedded upload files", 0)
    embedded_upload_filenames = uploads_already_embedded(embedded_file_ids)
    deleted_embedded_uploads = 0
    upload_dir = Path(CACHE_DIR).parent / "uploads"
    if upload_dir.exists():
        for filename in embedded_upload_filenames:
            try:
                file_path = upload_dir / filename
                if file_path.exists():
                    file_path.unlink()
                    deleted_embedded_uploads += 1
            except Exception as e:
                log.error(f"Failed to delete embedded upload file {filename}: {e}")
    results['embedded_uploads'] = deleted_embedded_uploads
    report_progress("stage4", "Deleted embedded upload files", deleted_embedded_uploads)

    # Audio cache cleanup
    if form_data.audio_cache_max_age_days is not None:
        report_progress("stage4", f"Cleaning audio cache files older than {form_data.audio_cache_max_age_days} days", 0)
        deleted_audio = cleanup_audio_cache(form_data.audio_cache_max_age_days)
        results['audio_cache'] = deleted_audio
        report_progress("stage4", "Deleted audio cache files", deleted_audio)

    # Stage 5: Database optimization (optional)
    if form_data.run_vacuum:
        report_progress("stage5", "Optimizing database with VACUUM (this may take a while and lock the database)", 0)

        try:
            with get_db() as db:
                engine = db.get_bind()
                db_url = str(engine.url)

                if 'postgresql' in db_url:
                    # PostgreSQL: VACUUM requires autocommit mode (no transaction)
                    raw_connection = engine.raw_connection()
                    try:
                        raw_connection.set_isolation_level(0)  # AUTOCOMMIT mode
                        cursor = raw_connection.cursor()
                        cursor.execute("VACUUM ANALYZE")
                        cursor.close()
                        raw_connection.commit()
                        log.info("Vacuumed PostgreSQL main database")
                    finally:
                        raw_connection.close()
                else:
                    # SQLite: Can run in transaction
                    db.execute(text("VACUUM"))
                    log.info("Vacuumed main database")
                results['vacuumed'] = True
        except Exception as e:
            log.error(f"Failed to vacuum main database: {e}")

        # Vector database-specific optimization
        if isinstance(vector_cleaner, ChromaDatabaseCleaner):
            try:
                with sqlite3.connect(str(vector_cleaner.chroma_db_path)) as conn:
                    conn.execute("VACUUM")
                    log.info("Vacuumed ChromaDB database")
            except Exception as e:
                log.error(f"Failed to vacuum ChromaDB database: {e}")
        elif (
            isinstance(vector_cleaner, PGVectorDatabaseCleaner)
            and vector_cleaner.session
        ):
            try:
                engine = vector_cleaner.session.get_bind()
                raw_connection = engine.raw_connection()
                try:
                    raw_connection.set_isolation_level(0)  # AUTOCOMMIT mode
                    cursor = raw_connection.cursor()
                    cursor.execute("VACUUM ANALYZE")
                    cursor.close()
                    raw_connection.commit()
                    log.info("Executed VACUUM ANALYZE on PostgreSQL database")
                finally:
                    raw_connection.close()
            except Exception as e:
                log.error(f"Failed to vacuum PostgreSQL database: {e}")
        
        report_progress("stage5", "Database optimization completed", 0)
    else:
        report_progress("stage5", "Skipping VACUUM optimization (not enabled)", 0)

    log.info("Data pruning deletion stages completed successfully")
    return results
