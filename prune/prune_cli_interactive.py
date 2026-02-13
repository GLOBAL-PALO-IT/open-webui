#!/usr/bin/env python3
"""
Open WebUI Interactive Prune CLI

This is an interactive command-line interface for the Open WebUI prune operations,
featuring a beautiful UI with menus, confirmations, and visual feedback.

Requires: rich library for terminal UI
  pip install rich
"""

import sys
import logging
from pathlib import Path

# Setup path to import modules
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from rich.console import Console
    from rich.prompt import Prompt, Confirm, IntPrompt
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.markdown import Markdown
except ImportError:
    print("ERROR: This script requires the 'rich' library for terminal UI")
    print("Install it with: pip install rich")
    sys.exit(1)

try:
    from prune_models import PruneDataForm, PrunePreviewResult
    from prune_core import PruneLock, get_vector_database_cleaner
    from prune_operations import (
        collect_preview_data,
        get_all_folders,
        execute_prune_deletion,
    )
    # Import Open WebUI modules using compatibility layer (handles pip/docker/git installs)
    from prune_imports import (
        Users, CACHE_DIR, VECTOR_DB_CLIENT, VECTOR_DB
    )
except ImportError as e:
    print(f"ERROR: Failed to import required modules: {e}")
    print("\nMake sure:")
    print("  1. You're running from the Open WebUI directory")
    print("  2. Open WebUI dependencies are installed")
    print("  3. For git install: backend/requirements.txt must be installed")
    sys.exit(1)

console = Console()
log = logging.getLogger(__name__)


class InteractivePruneUI:
    """Interactive UI for prune operations."""

    def __init__(self):
        self.form_data = PruneDataForm()
        self.vector_cleaner = None

    def _get_all_folders_safe(self, db=None):
        """
        Safely get all folders using compatibility helper.
        Handles API changes between Open WebUI versions.

        Args:
            db: Optional database session to reuse
        """
        return get_all_folders(db=db)

    def run(self):
        """Main entry point for interactive UI."""
        self.show_welcome()

        # Check environment
        if not self.check_environment():
            return 1

        # Initialize prune lock
        PruneLock.init(Path(CACHE_DIR))

        # Main menu loop
        while True:
            action = self.show_main_menu()

            if action == "configure":
                self.configure_settings()
            elif action == "preview":
                self.run_preview()
            elif action == "execute":
                if self.confirm_execution():
                    self.run_execution()
            elif action == "help":
                self.show_help()
            elif action == "exit":
                console.print("\n[yellow]Goodbye![/yellow]")
                return 0

    def show_welcome(self):
        """Show welcome message."""
        console.clear()
        console.print(Panel.fit(
            "[bold cyan]Open WebUI Interactive Prune Tool[/bold cyan]\n\n"
            "A safe and powerful way to clean up your Open WebUI database\n"
            "and reclaim disk space.",
            border_style="cyan"
        ))
        console.print()

    def check_environment(self) -> bool:
        """Check if environment is properly configured."""
        console.print("[bold]Checking environment...[/bold]")

        try:
            # Check database connection
            users = Users.get_users()
            console.print(f"[green]✓[/green] Database connection successful ({len(users['users'])} users found)")

            # Initialize vector cleaner
            self.vector_cleaner = get_vector_database_cleaner(
                VECTOR_DB, VECTOR_DB_CLIENT, Path(CACHE_DIR)
            )
            console.print(f"[green]✓[/green] Vector database: {VECTOR_DB}")

            console.print()
            return True

        except Exception as e:
            console.print(f"[red]✗ Failed to connect to database: {e}[/red]")
            console.print("\nMake sure:")
            console.print("  • DATABASE_URL environment variable is set")
            console.print("  • Database file exists and is accessible")
            console.print("  • Open WebUI dependencies are installed")
            return False

    def show_main_menu(self) -> str:
        """Show main menu and get user choice."""
        console.print("\n" + "=" * 70)
        console.print("[bold cyan]Main Menu[/bold cyan]")
        console.print("=" * 70)

        console.print("\n[1] [green]Configure Settings[/green] - Set up what to delete")
        console.print("[2] [yellow]Preview Changes[/yellow] - See what will be deleted (safe)")
        console.print("[3] [red]Execute Pruning[/red] - Actually delete data (DESTRUCTIVE)")
        console.print("[4] [blue]Help & Information[/blue] - Learn about prune operations")
        console.print("[5] [dim]Exit[/dim]")

        console.print()
        choice = Prompt.ask(
            "Choose an option",
            choices=["1", "2", "3", "4", "5"],
            default="1"
        )

        actions = {
            "1": "configure",
            "2": "preview",
            "3": "execute",
            "4": "help",
            "5": "exit"
        }
        return actions[choice]

    def configure_settings(self):
        """Interactive configuration menu."""
        console.clear()
        console.print(Panel.fit(
            "[bold]Configuration Settings[/bold]",
            border_style="blue"
        ))

        while True:
            console.print("\n[bold]Configuration Categories:[/bold]")
            console.print("[1] User Account Deletion")
            console.print("[2] Chat Deletion Settings")
            console.print("[3] Orphaned Data Cleanup")
            console.print("[4] Audio Cache Cleanup")
            console.print("[5] System Optimization (VACUUM)")
            console.print("[6] View Current Settings")
            console.print("[7] Reset to Defaults")
            console.print("[8] Back to Main Menu")

            choice = Prompt.ask("Choose category", choices=["1", "2", "3", "4", "5", "6", "7", "8"])

            if choice == "1":
                self.configure_user_deletion()
            elif choice == "2":
                self.configure_chat_deletion()
            elif choice == "3":
                self.configure_orphaned_cleanup()
            elif choice == "4":
                self.configure_audio_cache()
            elif choice == "5":
                self.configure_vacuum()
            elif choice == "6":
                self.show_current_settings()
            elif choice == "7":
                self.form_data = PruneDataForm()
                console.print("[green]Settings reset to defaults[/green]")
            elif choice == "8":
                break

    def configure_user_deletion(self):
        """Configure inactive user deletion settings."""
        console.print("\n[bold yellow]⚠ Warning: User Deletion is VERY DESTRUCTIVE[/bold yellow]")
        console.print("Deleting users will cascade delete ALL their data:")
        console.print("  • All their chats and messages")
        console.print("  • All their files and uploads")
        console.print("  • All their custom tools, functions, prompts")
        console.print("  • All their knowledge bases")
        console.print("  • Everything they created")
        console.print()

        if Confirm.ask("Do you want to enable inactive user deletion?"):
            days = IntPrompt.ask(
                "Delete users inactive for more than how many days?",
                default=180
            )
            self.form_data.delete_inactive_users_days = days

            if days < 30:
                console.print("[red]⚠ WARNING: Less than 30 days is very aggressive![/red]")
                console.print("You might accidentally delete users who are just on vacation.")
                if not Confirm.ask("Are you SURE you want such a short period?"):
                    self.form_data.delete_inactive_users_days = None
                    return

            self.form_data.exempt_admin_users = Confirm.ask(
                "Exempt admin users from deletion? (STRONGLY RECOMMENDED)",
                default=True
            )
            self.form_data.exempt_pending_users = Confirm.ask(
                "Exempt pending/unapproved users from deletion?",
                default=True
            )

            console.print(f"[green]✓[/green] Will delete users inactive for {days}+ days")
        else:
            self.form_data.delete_inactive_users_days = None
            console.print("[green]User deletion disabled[/green]")

    def configure_chat_deletion(self):
        """Configure chat deletion settings."""
        console.print("\n[bold]Chat Deletion Settings[/bold]")
        console.print("You can delete chats based on age (when they were last updated)")
        console.print()

        if Confirm.ask("Enable age-based chat deletion?"):
            days = IntPrompt.ask(
                "Delete chats older than how many days?",
                default=90
            )
            self.form_data.days = days

            self.form_data.exempt_archived_chats = Confirm.ask(
                "Keep archived chats even if old?",
                default=True
            )
            self.form_data.exempt_chats_in_folders = Confirm.ask(
                "Keep chats in folders/pinned even if old?",
                default=False
            )

            console.print(f"[green]✓[/green] Will delete chats older than {days} days")
        else:
            self.form_data.days = None
            console.print("[green]Age-based chat deletion disabled[/green]")

        # Orphaned chats (from deleted users)
        console.print("\n[bold]Orphaned Chats[/bold]")
        console.print("Chats from deleted users that no longer have an owner")
        self.form_data.delete_orphaned_chats = Confirm.ask(
            "Delete orphaned chats?",
            default=True
        )

        self.form_data.delete_orphaned_folders = Confirm.ask(
            "Delete orphaned folders?",
            default=True
        )

    def configure_orphaned_cleanup(self):
        """Configure orphaned data cleanup."""
        console.print("\n[bold]Orphaned Data Cleanup[/bold]")
        console.print("Clean up workspace items from deleted users")
        console.print()

        table = Table(show_header=True, header_style="bold")
        table.add_column("Item Type")
        table.add_column("Current Setting")
        table.add_column("Description")

        items = [
            ("Knowledge Bases", "delete_orphaned_knowledge_bases", "User knowledge bases"),
            ("Tools", "delete_orphaned_tools", "Custom tools"),
            ("Functions", "delete_orphaned_functions", "Actions, Pipes, Filters"),
            ("Prompts", "delete_orphaned_prompts", "Custom prompts"),
            ("Models", "delete_orphaned_models", "Custom model configs"),
            ("Notes", "delete_orphaned_notes", "User notes"),
        ]

        for name, attr, desc in items:
            current = "✓ Enabled" if getattr(self.form_data, attr) else "✗ Disabled"
            table.add_row(name, current, desc)

        console.print(table)
        console.print()

        if Confirm.ask("Would you like to change these settings?"):
            for name, attr, desc in items:
                current = getattr(self.form_data, attr)
                new_value = Confirm.ask(
                    f"Delete orphaned {name}?",
                    default=current
                )
                setattr(self.form_data, attr, new_value)

            console.print("[green]✓ Orphaned data settings updated[/green]")

    def configure_audio_cache(self):
        """Configure audio cache cleanup."""
        console.print("\n[bold]Audio Cache Cleanup[/bold]")
        console.print("Remove old TTS (text-to-speech) and STT (speech-to-text) files")
        console.print()

        if Confirm.ask("Enable audio cache cleanup?", default=True):
            days = IntPrompt.ask(
                "Delete audio files older than how many days?",
                default=30
            )
            self.form_data.audio_cache_max_age_days = days
            console.print(f"[green]✓[/green] Will delete audio cache older than {days} days")
        else:
            self.form_data.audio_cache_max_age_days = None
            console.print("[green]Audio cache cleanup disabled[/green]")

    def configure_vacuum(self):
        """Configure VACUUM optimization."""
        console.print("\n[bold red]⚠ DATABASE VACUUM WARNING[/bold red]")
        console.print()
        console.print("VACUUM reclaims disk space by rebuilding the database file.")
        console.print()
        console.print("[bold yellow]⚠ Critical Warnings:[/bold yellow]")
        console.print("  • LOCKS the entire database during execution")
        console.print("  • ALL users will experience errors during VACUUM")
        console.print("  • Can take 5-30+ minutes depending on database size")
        console.print("  • Should ONLY be run during maintenance windows")
        console.print("  • Not required for routine cleanups")
        console.print()

        self.form_data.run_vacuum = Confirm.ask(
            "[bold]Enable VACUUM optimization?[/bold]",
            default=False
        )

        if self.form_data.run_vacuum:
            console.print("[yellow]⚠ VACUUM enabled - ensure this is a maintenance window![/yellow]")
        else:
            console.print("[green]VACUUM disabled (recommended for routine use)[/green]")

    def show_current_settings(self):
        """Display current configuration."""
        console.clear()
        console.print(Panel.fit(
            "[bold]Current Configuration[/bold]",
            border_style="cyan"
        ))

        # User deletion
        console.print("\n[bold cyan]User Account Deletion:[/bold cyan]")
        if self.form_data.delete_inactive_users_days is not None:
            console.print(f"  [yellow]Enabled[/yellow] - Delete users inactive for {self.form_data.delete_inactive_users_days}+ days")
            console.print(f"    Exempt admins: {'Yes' if self.form_data.exempt_admin_users else 'No'}")
            console.print(f"    Exempt pending: {'Yes' if self.form_data.exempt_pending_users else 'No'}")
        else:
            console.print("  [dim]Disabled[/dim]")

        # Chat deletion
        console.print("\n[bold cyan]Chat Deletion:[/bold cyan]")
        if self.form_data.days is not None:
            console.print(f"  [yellow]Enabled[/yellow] - Delete chats older than {self.form_data.days} days")
            console.print(f"    Exempt archived: {'Yes' if self.form_data.exempt_archived_chats else 'No'}")
            console.print(f"    Exempt in folders: {'Yes' if self.form_data.exempt_chats_in_folders else 'No'}")
        else:
            console.print("  [dim]Disabled[/dim]")

        # Orphaned data
        console.print("\n[bold cyan]Orphaned Data Cleanup:[/bold cyan]")
        orphaned_items = [
            ("Chats", self.form_data.delete_orphaned_chats),
            ("Knowledge Bases", self.form_data.delete_orphaned_knowledge_bases),
            ("Tools", self.form_data.delete_orphaned_tools),
            ("Functions", self.form_data.delete_orphaned_functions),
            ("Prompts", self.form_data.delete_orphaned_prompts),
            ("Models", self.form_data.delete_orphaned_models),
            ("Notes", self.form_data.delete_orphaned_notes),
            ("Folders", self.form_data.delete_orphaned_folders),
        ]
        for name, enabled in orphaned_items:
            status = "[green]✓[/green]" if enabled else "[dim]✗[/dim]"
            console.print(f"  {status} {name}")

        # Audio cache
        console.print("\n[bold cyan]Audio Cache:[/bold cyan]")
        if self.form_data.audio_cache_max_age_days is not None:
            console.print(f"  [yellow]Enabled[/yellow] - Delete files older than {self.form_data.audio_cache_max_age_days} days")
        else:
            console.print("  [dim]Disabled[/dim]")

        # VACUUM
        console.print("\n[bold cyan]System Optimization:[/bold cyan]")
        if self.form_data.run_vacuum:
            console.print("  [red]⚠ VACUUM ENABLED[/red] - Will lock database!")
        else:
            console.print("  [dim]VACUUM disabled[/dim]")

        console.print()
        Prompt.ask("Press Enter to continue")

    def run_preview(self):
        """Run preview and show results."""
        console.clear()
        console.print(Panel.fit(
            "[bold yellow]Preview Mode[/bold yellow]\n"
            "Calculating what would be deleted...",
            border_style="yellow"
        ))

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            task = progress.add_task("Starting analysis...", total=None)

            def rich_progress_callback(stage: str, message: str):
                """Progress callback for Rich UI."""
                progress.update(task, description=message)
                # Only print supplementary info for key stages
                if stage in ["load_kb", "load_users", "scan_files", "check_embeddings", "compile"]:
                    pass  # Skip extra console prints for these to reduce clutter

            try:
                # Use shared preview data collection function
                result = collect_preview_data(
                    self.form_data,
                    self.vector_cleaner,
                    progress_callback=rich_progress_callback
                )

                progress.update(task, description="Analysis complete!", completed=True)
                console.print("\n[green]✓ Analysis completed successfully![/green]")

            except Exception as e:
                console.print(f"\n[red]Error during preview: {e}[/red]")
                log.exception("Preview failed")
                return

        # Display results
        console.print()
        self.display_preview_results(result)

        console.print()
        Prompt.ask("Press Enter to continue")

    def display_preview_results(self, result: PrunePreviewResult):
        """Display preview results in a beautiful table."""
        console.print("\n" + "=" * 70)
        console.print("[bold]PREVIEW RESULTS - What Will Be Deleted[/bold]")
        console.print("=" * 70)

        if not result.has_items():
            console.print("\n[green]✓ Nothing to delete - your database is clean![/green]")
            return

        summary = result.get_summary_dict()

        for category, items in summary.items():
            # Skip empty categories
            if isinstance(items, dict):
                # Check if there are any integers > 0 OR any non-empty lists OR any non-empty nested dicts
                has_content = any(
                    (isinstance(v, int) and v > 0) or 
                    (isinstance(v, list) and len(v) > 0) or
                    (isinstance(v, dict) and len(v) > 0)
                    for v in items.values()
                )
                if not has_content:
                    continue
            else:
                if not items:
                    continue

            console.print(f"\n[bold cyan]{category}:[/bold cyan]")
            for name, value in items.items():
                # Handle nested dicts (file names grouped by KB)
                if isinstance(value, dict):
                    if value:
                        total_count = sum(len(files) for files in value.values())
                        console.print(f"  [yellow]{total_count}[/yellow] {name} across {len(value)} knowledge base(s)")
                        for kb_name, files in value.items():
                            if files:
                                console.print(f"    [bold]{kb_name}:[/bold] {len(files)} file(s)")
                                # Show up to 10 files per KB
                                for i, filename in enumerate(files[:10]):
                                    console.print(f"      [dim]• {filename}[/dim]")
                                if len(files) > 10:
                                    console.print(f"      [dim]• ... and {len(files) - 10} more[/dim]")
                # Handle lists (file names or enriched collections)
                elif isinstance(value, list):
                    if value:
                        # Check if it's a list of dicts (enriched collections)
                        if value and isinstance(value[0], dict):
                            console.print(f"  [yellow]{len(value)}[/yellow] {name}")
                            for i, item in enumerate(value[:20]):
                                coll_type = item.get('type', 'Unknown')
                                coll_name = item.get('name', 'Unknown')
                                coll_id = item.get('id', '')
                                console.print(f"    [dim]• [{coll_type}] {coll_name} ({coll_id})[/dim]")
                            if len(value) > 20:
                                console.print(f"    [dim]• ... and {len(value) - 20} more[/dim]")
                        else:
                            # Regular string list
                            console.print(f"  [yellow]{len(value)}[/yellow] {name}")
                            for i, item in enumerate(value[:20]):
                                console.print(f"    [dim]• {item}[/dim]")
                            if len(value) > 20:
                                console.print(f"    [dim]• ... and {len(value) - 20} more[/dim]")
                # Handle integers (counts)
                elif isinstance(value, int) and value > 0:
                    console.print(f"  [yellow]{value:,}[/yellow] {name}")

        console.print("\n" + "=" * 70)
        console.print(f"[bold red]TOTAL ITEMS: {result.total_items():,}[/bold red]")
        console.print("=" * 70)

        # Show what will NOT be deleted
        console.print("\n" + "=" * 70)
        console.print("[bold]WHAT WILL NOT BE DELETED - Files Without Embeddings[/bold]")
        console.print("=" * 70)
        console.print("\n[bold cyan]Files Pending Vector Embeddings:[/bold cyan]")
        console.print("[dim]These files are active and will be preserved[/dim]\n")
        
        if hasattr(result, 'non_embedded_files') and result.non_embedded_files:
            total_files = 0
            for kb_name, files in result.non_embedded_files.items():
                if files:
                    total_files += len(files)
                    console.print(f"  [green]{len(files)}[/green] {kb_name}")
                    # Show up to 20 files per KB
                    for i, filename in enumerate(files[:20]):
                        console.print(f"    [dim]• {filename}[/dim]")
                    if len(files) > 20:
                        console.print(f"    [dim]• ... and {len(files) - 20} more[/dim]")
            
            console.print("\n" + "=" * 70)
            console.print(f"[bold green]TOTAL FILES TO PRESERVE: {total_files:,}[/bold green]")
            console.print("=" * 70)
        else:
            console.print("  [dim]No files without embeddings found[/dim]")
            console.print("\n" + "=" * 70)
            console.print("[bold green]TOTAL FILES TO PRESERVE: 0[/bold green]")
            console.print("=" * 70)

    def confirm_execution(self) -> bool:
        """Confirm execution with multiple warnings."""
        console.clear()
        console.print(Panel.fit(
            "[bold red]⚠ DESTRUCTIVE OPERATION WARNING ⚠[/bold red]\n\n"
            "You are about to PERMANENTLY DELETE data from your database.\n"
            "This action CANNOT be undone!",
            border_style="red",
            title="[bold]DANGER[/bold]"
        ))

        console.print("\n[bold yellow]Before proceeding:[/bold yellow]")
        console.print("  [red]✓[/red] Have you created a database backup?")
        console.print("  [red]✓[/red] Have you reviewed the preview?")
        console.print("  [red]✓[/red] Are you sure you want to proceed?")
        console.print()

        if not Confirm.ask("[bold red]Do you want to proceed with deletion?[/bold red]", default=False):
            console.print("\n[green]Cancelled - no changes made[/green]")
            return False

        # Second confirmation
        console.print("\n[bold red]FINAL CONFIRMATION[/bold red]")
        console.print("Type 'DELETE' (all caps) to confirm:")
        confirmation = Prompt.ask("Type DELETE to continue")

        if confirmation != "DELETE":
            console.print("\n[green]Cancelled - no changes made[/green]")
            return False

        return True

    def run_execution(self):
        """Execute the actual pruning operation."""
        console.print("\n[bold red]Starting pruning operation...[/bold red]")

        # Acquire lock
        if not PruneLock.acquire():
            console.print("\n[red]ERROR: Another prune operation is already in progress[/red]")
            console.print("Please wait for it to complete.")
            return

        try:
            self.execute_prune_stages()
        finally:
            PruneLock.release()

        console.print("\n[bold green]✓ Pruning operation completed successfully![/bold green]")
        Prompt.ask("\nPress Enter to continue")

    def execute_prune_stages(self):
        """Execute all prune stages with progress display."""
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            task = progress.add_task("Starting deletion...", total=None)
            
            def rich_progress_callback(stage: str, message: str, count: int):
                """Progress callback for Rich UI."""
                # Update spinner with current operation
                progress.update(task, description=message)
                
                # Print completion messages with checkmark
                if count > 0:
                    console.print(f"[green]✓[/green] {message}: {count}")
            
            # Use shared deletion function
            try:
                results = execute_prune_deletion(
                    self.form_data,
                    self.vector_cleaner,
                    rich_progress_callback
                )
                
                progress.update(task, description="Completed!")
                progress.stop()
                
                # Show summary
                console.print("\n[bold green]Deletion Summary:[/bold green]")
                console.print(f"  Users: {results['inactive_users']}")
                console.print(f"  Old chats: {results['old_chats']}")
                console.print(f"  Orphaned files: {results['orphaned_files']}")
                console.print(f"  Orphaned uploads: {results['orphaned_uploads']}")
                console.print(f"  Vector collections: {results['orphaned_vectors']}")
                console.print(f"  Embedded files: {results['embedded_files']}")
                console.print(f"  Audio cache files: {results['audio_cache']}")
                    
                total = (results['inactive_users'] + results['old_chats'] + 
                        results['orphaned_files'] + results['orphaned_chats'] +
                        results['orphaned_tools'] + results['orphaned_functions'] +
                        results['orphaned_prompts'] + results['orphaned_models'] +
                        results['orphaned_notes'] + results['orphaned_folders'])
                console.print(f"\n[bold]Total items deleted: {total}[/bold]")
                
            except Exception as e:
                progress.stop()
                console.print(f"\n[red]Error during deletion: {e}[/red]")
                log.exception("Error in execute_prune_stages")
                raise


    def show_help(self):
        """Show help information."""
        console.clear()
        help_text = """
# Open WebUI Prune Tool Help

## What This Tool Does

This interactive tool helps you clean up your Open WebUI database by:
- Deleting inactive user accounts
- Removing old conversations
- Cleaning up orphaned data from deleted users
- Removing unused files and uploads
- Cleaning vector database collections
- Reclaiming disk space

## Safety Features

✓ **Dry-run preview** - See what will be deleted before committing
✓ **Multiple confirmations** - Prevents accidental deletion
✓ **Granular control** - Choose exactly what to clean
✓ **File-based locking** - Prevents concurrent operations
✓ **Comprehensive logging** - Track all operations

## Recommended Workflow

1. **Configure settings** - Choose what to clean
2. **Run preview** - See what will be deleted
3. **Backup database** - Create a backup before executing
4. **Execute** - Perform the actual cleanup
5. **Verify** - Check logs and database size

## Warning Categories

🟡 **Yellow** - Safe, reversible, or preview
🟠 **Orange** - Potentially destructive, needs care
🔴 **Red** - Very destructive, backup required

## Getting Help

- Review the README.md for detailed documentation
- Check ANALYSIS.md for technical details
- Review logs for operation history
"""
        console.print(Markdown(help_text))
        console.print()
        Prompt.ask("Press Enter to continue")


def main():
    """Main entry point."""
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler()]
    )

    try:
        ui = InteractivePruneUI()
        return ui.run()
    except KeyboardInterrupt:
        console.print("\n\n[yellow]Interrupted by user[/yellow]")
        return 130
    except Exception as e:
        console.print(f"\n[red]Fatal error: {e}[/red]")
        log.exception("Fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
