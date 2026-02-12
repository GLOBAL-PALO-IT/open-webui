#!/usr/bin/env python3
"""
Open WebUI Standalone Prune Script

This is a standalone command-line script that replicates the full logic and
configurability of the prune.py API router, but runs independently without
requiring the web server to be running.

Usage:
    python standalone_prune.py --help
    python standalone_prune.py --dry-run  # Preview what will be deleted
    python standalone_prune.py --days 60 --run-vacuum  # Delete chats older than 60 days

Requirements:
    - Must be run from Open WebUI installation directory or have PYTHONPATH set
    - Requires same environment variables as Open WebUI (DATABASE_URL, etc.)
    - Requires same Python dependencies as Open WebUI backend
"""

import sys
import argparse
import logging
from pathlib import Path

# Add parent directory to path to import Open WebUI modules
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

# Now we can import from prune modules
try:
    from prune_models import PruneDataForm, PrunePreviewResult
    from prune_core import (
        PruneLock,
        get_vector_database_cleaner,
    )
    from prune_operations import (
        collect_preview_data,
        execute_prune_deletion,
    )
    from prune_imports import (
        VECTOR_DB, VECTOR_DB_CLIENT, CACHE_DIR
    )
except ImportError as e:
    print(f"ERROR: Failed to import Open WebUI modules: {e}", file=sys.stderr)
    print("\nThis script must be run with access to Open WebUI's backend modules.", file=sys.stderr)
    print("Try one of the following:", file=sys.stderr)
    print("  1. Run from the Open WebUI installation directory", file=sys.stderr)
    print("  2. Set PYTHONPATH to include the Open WebUI directory", file=sys.stderr)
    print("  3. Install Open WebUI as a package", file=sys.stderr)
    sys.exit(1)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Open WebUI Standalone Data Pruning Script',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview what will be deleted (safe, no changes)
  %(prog)s --dry-run

  # Delete chats older than 60 days
  %(prog)s --days 60

  # Delete inactive users (90+ days) and their data
  %(prog)s --delete-inactive-users-days 90

  # Full cleanup with VACUUM optimization
  %(prog)s --days 90 --delete-inactive-users-days 180 --run-vacuum

  # Clean orphaned data only (no age-based deletion)
  %(prog)s --delete-orphaned-chats --delete-orphaned-files

  # Audio cache cleanup
  %(prog)s --audio-cache-max-age-days 30

Safety Features:
  - Uses file-based locking to prevent concurrent runs
  - Dry-run mode enabled by default (use --execute to actually delete)
  - Detailed logging of all operations
  - Preserves archived chats and folder-organized chats by default
  - Admin users exempted from deletion by default
        """
    )

    # Execution mode
    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Preview what will be deleted without making changes (default if --execute not specified)'
    )
    parser.add_argument(
        '--execute',
        action='store_true',
        default=False,
        help='Actually perform deletions (required for real cleanup)'
    )

    # Age-based deletion
    parser.add_argument(
        '--days',
        type=int,
        default=None,
        metavar='N',
        help='Delete chats older than N days (based on last update time)'
    )
    parser.add_argument(
        '--exempt-archived-chats',
        action='store_true',
        default=True,
        help='Keep archived chats even if old (default: True)'
    )
    parser.add_argument(
        '--no-exempt-archived-chats',
        action='store_false',
        dest='exempt_archived_chats',
        help='Include archived chats in age-based deletion'
    )
    parser.add_argument(
        '--exempt-chats-in-folders',
        action='store_true',
        default=False,
        help='Keep chats in folders/pinned even if old'
    )

    # Inactive user deletion
    parser.add_argument(
        '--delete-inactive-users-days',
        type=int,
        default=None,
        metavar='N',
        help='Delete users inactive for more than N days (DESTRUCTIVE)'
    )
    parser.add_argument(
        '--exempt-admin-users',
        action='store_true',
        default=True,
        help='Never delete admin users (default: True, STRONGLY RECOMMENDED)'
    )
    parser.add_argument(
        '--no-exempt-admin-users',
        action='store_false',
        dest='exempt_admin_users',
        help='Include admin users in inactivity deletion (NOT RECOMMENDED)'
    )
    parser.add_argument(
        '--exempt-pending-users',
        action='store_true',
        default=True,
        help='Never delete pending users (default: True)'
    )
    parser.add_argument(
        '--no-exempt-pending-users',
        action='store_false',
        dest='exempt_pending_users',
        help='Include pending users in inactivity deletion'
    )

    # Orphaned data deletion
    parser.add_argument(
        '--delete-orphaned-chats',
        action='store_true',
        default=True,
        help='Delete orphaned chats from deleted users (default: True)'
    )
    parser.add_argument(
        '--no-delete-orphaned-chats',
        action='store_false',
        dest='delete_orphaned_chats',
        help='Skip orphaned chat deletion'
    )
    parser.add_argument(
        '--delete-orphaned-tools',
        action='store_true',
        default=False,
        help='Delete orphaned tools from deleted users'
    )
    parser.add_argument(
        '--delete-orphaned-functions',
        action='store_true',
        default=False,
        help='Delete orphaned functions from deleted users'
    )
    parser.add_argument(
        '--delete-orphaned-prompts',
        action='store_true',
        default=True,
        help='Delete orphaned prompts from deleted users (default: True)'
    )
    parser.add_argument(
        '--delete-orphaned-knowledge-bases',
        action='store_true',
        default=True,
        help='Delete orphaned knowledge bases from deleted users (default: True)'
    )
    parser.add_argument(
        '--delete-orphaned-models',
        action='store_true',
        default=True,
        help='Delete orphaned models from deleted users (default: True)'
    )
    parser.add_argument(
        '--delete-orphaned-notes',
        action='store_true',
        default=True,
        help='Delete orphaned notes from deleted users (default: True)'
    )
    parser.add_argument(
        '--delete-orphaned-folders',
        action='store_true',
        default=True,
        help='Delete orphaned folders from deleted users (default: True)'
    )

    # Audio cache cleanup
    parser.add_argument(
        '--audio-cache-max-age-days',
        type=int,
        default=None,
        metavar='N',
        help='Delete audio cache files (TTS/STT) older than N days'
    )

    # Database optimization
    parser.add_argument(
        '--run-vacuum',
        action='store_true',
        default=False,
        help='Run VACUUM to reclaim disk space (LOCKS DATABASE, use during maintenance)'
    )

    # Logging
    parser.add_argument(
        '--verbose',
        '-v',
        action='store_true',
        default=False,
        help='Enable verbose debug logging'
    )
    parser.add_argument(
        '--quiet',
        '-q',
        action='store_true',
        default=False,
        help='Suppress all output except errors'
    )

    args = parser.parse_args()

    # If neither --dry-run nor --execute specified, default to dry-run
    if not args.dry_run and not args.execute:
        args.dry_run = True
        log.info("No execution mode specified, defaulting to --dry-run (preview mode)")

    # Can't have both dry-run and execute
    if args.dry_run and args.execute:
        parser.error("Cannot specify both --dry-run and --execute")

    return args


def configure_logging(verbose: bool, quiet: bool):
    """Configure logging level based on arguments."""
    if quiet:
        logging.getLogger().setLevel(logging.ERROR)
    elif verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)


def create_prune_form(args) -> PruneDataForm:
    """Create PruneDataForm from command-line arguments."""
    return PruneDataForm(
        days=args.days,
        exempt_archived_chats=args.exempt_archived_chats,
        exempt_chats_in_folders=args.exempt_chats_in_folders,
        delete_orphaned_chats=args.delete_orphaned_chats,
        delete_orphaned_tools=args.delete_orphaned_tools,
        delete_orphaned_functions=args.delete_orphaned_functions,
        delete_orphaned_prompts=args.delete_orphaned_prompts,
        delete_orphaned_knowledge_bases=args.delete_orphaned_knowledge_bases,
        delete_orphaned_models=args.delete_orphaned_models,
        delete_orphaned_notes=args.delete_orphaned_notes,
        delete_orphaned_folders=args.delete_orphaned_folders,
        audio_cache_max_age_days=args.audio_cache_max_age_days,
        delete_inactive_users_days=args.delete_inactive_users_days,
        exempt_admin_users=args.exempt_admin_users,
        exempt_pending_users=args.exempt_pending_users,
        run_vacuum=args.run_vacuum,
        dry_run=args.dry_run,
    )


def print_preview_results(result: PrunePreviewResult):
    """Pretty-print preview results."""
    print("\n" + "="*70)
    print("  PRUNE PREVIEW - What will be deleted")
    print("="*70)

    total_items = 0

    if result.inactive_users > 0:
        print(f"\n👤 Inactive Users:")
        print(f"   {result.inactive_users} user accounts")
        total_items += result.inactive_users

    if result.old_chats > 0 or result.orphaned_chats > 0:
        print(f"\n💬 Chats:")
        if result.old_chats > 0:
            print(f"   {result.old_chats} old chats (age-based)")
        if result.orphaned_chats > 0:
            print(f"   {result.orphaned_chats} orphaned chats")
        total_items += result.old_chats + result.orphaned_chats

    if result.orphaned_files > 0:
        print(f"\n📁 Files:")
        print(f"   {result.orphaned_files} orphaned file records")
        total_items += result.orphaned_files

    workspace_total = (result.orphaned_tools + result.orphaned_functions +
                       result.orphaned_prompts + result.orphaned_knowledge_bases +
                       result.orphaned_models + result.orphaned_notes)
    if workspace_total > 0:
        print(f"\n🔧 Workspace Items:")
        if result.orphaned_tools > 0:
            print(f"   {result.orphaned_tools} orphaned tools")
        if result.orphaned_functions > 0:
            print(f"   {result.orphaned_functions} orphaned functions")
        if result.orphaned_prompts > 0:
            print(f"   {result.orphaned_prompts} orphaned prompts")
        if result.orphaned_knowledge_bases > 0:
            print(f"   {result.orphaned_knowledge_bases} orphaned knowledge bases")
        if result.orphaned_models > 0:
            print(f"   {result.orphaned_models} orphaned models")
        if result.orphaned_notes > 0:
            print(f"   {result.orphaned_notes} orphaned notes")
        total_items += workspace_total

    if result.orphaned_folders > 0:
        print(f"\n📂 Folders:")
        print(f"   {result.orphaned_folders} orphaned folders")
        total_items += result.orphaned_folders

    # Display embedded files broken down by knowledge base
    total_embedded_records = sum(len(files) for files in result.embedded_file_records.values())
    total_embedded_uploads = sum(len(files) for files in result.embedded_upload_files.values())
    
    if total_embedded_records > 0 or total_embedded_uploads > 0:
        print(f"\n📦 Embedded Files (by Knowledge Base):")
        print(f"   {total_embedded_records} file record(s) with embeddings across {len(result.embedded_file_records)} knowledge base(s)")
        
        # Show breakdown by KB (limit to first few for readability)
        for kb_name, files in sorted(result.embedded_file_records.items()):
            print(f"    {kb_name}: {len(files)} file(s)")
            # Show first few files
            for filename in files[:5]:
                print(f"      • {filename}")
            if len(files) > 5:
                print(f"      • ... and {len(files) - 5} more")
        
        total_items += total_embedded_records + total_embedded_uploads

    # Handle orphaned_uploads and orphaned_vector_collections as lists
    uploads_count = len(result.orphaned_uploads) if isinstance(result.orphaned_uploads, list) else result.orphaned_uploads
    collections_count = len(result.orphaned_vector_collections) if isinstance(result.orphaned_vector_collections, list) else result.orphaned_vector_collections
    
    if uploads_count > 0 or collections_count > 0:
        print(f"\n💾 Storage:")
        if uploads_count > 0:
            print(f"   {uploads_count} orphaned upload files")
        if collections_count > 0:
            print(f"   {collections_count} orphaned vector collections")
        total_items += uploads_count + collections_count

    if result.audio_cache_files > 0:
        print(f"\n🔊 Audio Cache:")
        print(f"   {result.audio_cache_files} old audio cache files")
        total_items += result.audio_cache_files

    print("\n" + "="*70)
    print(f"  TOTAL ITEMS TO DELETE: {total_items}")
    print("="*70)

    # Show files that will NOT be deleted (files without embeddings)
    total_non_embedded = sum(len(files) for files in result.non_embedded_files.values())
    if total_non_embedded > 0:
        print("\n" + "="*70)
        print("  WHAT WILL NOT BE DELETED - Files Without Embeddings")
        print("="*70)
        print("\nFiles Pending Vector Embeddings:")
        print("These files are active and will be preserved\n")
        
        for kb_name, files in sorted(result.non_embedded_files.items()):
            print(f"  {kb_name}: {len(files)} file(s)")
            # Show first few files
            for filename in files[:5]:
                print(f"    • {filename}")
            if len(files) > 5:
                print(f"    • ... and {len(files) - 5} more")
        
        print("\n" + "="*70)
        print(f"  TOTAL FILES TO PRESERVE: {total_non_embedded}")
        print("="*70)

    if total_items == 0:
        print("\n✅ Nothing to delete - your database is clean!")
    else:
        print("\n⚠️  Run with --execute to perform actual deletion")
        print("   (This is a preview only, no changes were made)")
    print()


def run_prune(form_data: PruneDataForm):
    """
    Execute the prune operation with the given configuration.
    This replicates the logic from prune.py's prune_data function.
    """
    # Acquire lock to prevent concurrent operations
    if not PruneLock.acquire():
        log.error("A prune operation is already in progress. Please wait for it to complete.")
        return False

    try:
        # Get vector database cleaner based on configuration
        vector_cleaner = get_vector_database_cleaner(VECTOR_DB, VECTOR_DB_CLIENT, Path(CACHE_DIR))

        if form_data.dry_run:
            log.info("Starting data pruning preview (dry run)")

            # Use shared preview data collection function
            result = collect_preview_data(form_data, vector_cleaner)

            log.info("Data pruning preview completed")
            print_preview_results(result)
            return True

        # Actual deletion logic (dry_run=False)
        log.info("Starting data pruning process (ACTUAL DELETION)")

        # Use shared execution function with logging callback
        def log_progress(stage: str, message: str, count: int):
            """Progress callback for logging."""
            if count > 0:
                log.info(f"{message}: {count}")
            else:
                log.info(message)

        results = execute_prune_deletion(form_data, vector_cleaner, log_progress)

        # Log summary
        log.info("Data pruning completed successfully")
        log.info(f"Summary: {results['inactive_users']} users, {results['old_chats']} old chats, "
                 f"{results['orphaned_files']} orphaned files, {results['orphaned_uploads']} orphaned uploads, "
                 f"{results['orphaned_vectors']} vector collections deleted")
        
        return True

    except Exception as e:
        log.exception(f"Error during data pruning: {e}")
        return False
    finally:
        # Always release lock, even if operation fails
        PruneLock.release()


def main():
    """Main entry point for standalone prune script."""
    args = parse_arguments()
    configure_logging(args.verbose, args.quiet)

    log.info("="*70)
    log.info("  Open WebUI Standalone Prune Script")
    log.info("="*70)

    # Verify environment
    log.info("Checking environment configuration...")

    # Check if we can access database
    try:
        users = Users.get_users()
        log.info(f"✓ Database connection successful ({len(users['users'])} users found)")
    except Exception as e:
        log.error(f"✗ Failed to connect to database: {e}")
        log.error("  Make sure DATABASE_URL environment variable is set correctly")
        return 1

    # Initialize prune lock system
    PruneLock.init(Path(CACHE_DIR))

    # Create prune configuration
    form_data = create_prune_form(args)

    # Log configuration
    log.info("\nPrune Configuration:")
    if form_data.dry_run:
        log.info("  Mode: DRY RUN (preview only, no changes)")
    else:
        log.info("  Mode: EXECUTE (actual deletion)")

    if form_data.days is not None:
        log.info(f"  Delete chats older than: {form_data.days} days")
        log.info(f"    Exempt archived chats: {form_data.exempt_archived_chats}")
        log.info(f"    Exempt chats in folders: {form_data.exempt_chats_in_folders}")

    if form_data.delete_inactive_users_days is not None:
        log.info(f"  Delete inactive users: {form_data.delete_inactive_users_days} days")
        log.info(f"    Exempt admin users: {form_data.exempt_admin_users}")
        log.info(f"    Exempt pending users: {form_data.exempt_pending_users}")

    log.info("  Orphaned data deletion:")
    log.info(f"    Chats: {form_data.delete_orphaned_chats}")
    log.info(f"    Tools: {form_data.delete_orphaned_tools}")
    log.info(f"    Functions: {form_data.delete_orphaned_functions}")
    log.info(f"    Prompts: {form_data.delete_orphaned_prompts}")
    log.info(f"    Knowledge Bases: {form_data.delete_orphaned_knowledge_bases}")
    log.info(f"    Models: {form_data.delete_orphaned_models}")
    log.info(f"    Notes: {form_data.delete_orphaned_notes}")
    log.info(f"    Folders: {form_data.delete_orphaned_folders}")

    if form_data.audio_cache_max_age_days is not None:
        log.info(f"  Audio cache cleanup: {form_data.audio_cache_max_age_days} days")

    if form_data.run_vacuum:
        log.info("  Database VACUUM: ENABLED (will lock database!)")

    log.info("")

    # Run the prune operation
    success = run_prune(form_data)

    if success:
        log.info("\n✓ Prune operation completed successfully")
        return 0
    else:
        log.error("\n✗ Prune operation failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
from prune_imports import Users
        