"""
PhDiscover Engine - Main Entry Point
"""

from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.traceback import install as install_rich_traceback

from phdiscover.config import get_settings, reload_settings

# Configure structlog
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

# Rich setup
install_rich_traceback(show_locals=True)
console = Console()

app = typer.Typer(
    name="phdiscover",
    help="PhDiscover Engine - PI-First PhD Position Discovery",
    add_completion=False,
)

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan():
    """Application lifespan manager"""
    settings = get_settings()
    logger.info("starting_phdiscover", version=settings.app.version, env=settings.app.environment)
    yield
    logger.info("shutting_down_phdiscover")


@app.command()
def crawl(
    source: Optional[str] = typer.Option(None, "--source", "-s", help="Specific source to crawl"),
    country: Optional[str] = typer.Option(None, "--country", "-c", help="Country code to crawl"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run without saving to database"),
    max_items: int = typer.Option(0, "--max-items", "-n", help="Maximum items to process (0 = unlimited)"),
):
    """Run the crawler for all or specific sources"""
    from phdiscover.crawlers.run_all import main as run_crawlers

    console.print(f"[bold blue]Starting crawl[/bold blue] source={source or 'all'} country={country or 'all'}")
    asyncio.run(run_crawlers(source=source, country=country, dry_run=dry_run, max_items=max_items))


@app.command()
def bot(
    polling: bool = typer.Option(True, "--polling/--webhook", help="Use polling or webhook mode"),
    webhook_url: Optional[str] = typer.Option(None, "--webhook-url", help="Webhook URL for production"),
):
    """Start the Telegram bot"""
    from phdiscover.bot.run import main as run_bot

    console.print(f"[bold green]Starting Telegram bot[/bold green] polling={polling}")
    asyncio.run(run_bot(polling=polling, webhook_url=webhook_url))


@app.command()
def scheduler(
    run_once: bool = typer.Option(False, "--once", help="Run scheduled jobs once and exit"),
):
    """Start the scheduler for crawl and alert jobs"""
    from phdiscover.scheduler.run import main as run_scheduler

    console.print(f"[bold yellow]Starting scheduler[/bold yellow] run_once={run_once}")
    asyncio.run(run_scheduler(run_once=run_once))


@app.command()
def export(
    format: str = typer.Option("excel", "--format", "-f", help="Export format: excel, json, csv"),
    output: Path = typer.Option(Path("exports/phdiscover_export"), "--output", "-o", help="Output file path"),
    min_score: float = typer.Option(70.0, "--min-score", help="Minimum rank score to export"),
    country: Optional[str] = typer.Option(None, "--country", "-c", help="Filter by country"),
    days: int = typer.Option(30, "--days", help="Only positions from last N days"),
):
    """Export positions to file"""
    from phdiscover.export.cli import main as run_export

    console.print(f"[bold magenta]Exporting positions[/bold magenta] format={format} min_score={min_score}")
    asyncio.run(run_export(format=format, output=output, min_score=min_score, country=country, days=days))


@app.command()
def test_parsers(
    source: Optional[str] = typer.Option(None, "--source", "-s", help="Test specific parser"),
    golden: bool = typer.Option(True, "--golden/--no-golden", help="Run against golden dataset"),
):
    """Test parsers against golden dataset"""
    from scripts.test_parsers import main as run_parser_tests

    console.print(f"[bold cyan]Testing parsers[/bold cyan] source={source or 'all'} golden={golden}")
    asyncio.run(run_parser_tests(source=source, golden=golden))


@app.command()
def seed_fingerprint(
    cv_path: Path = typer.Argument(..., help="Path to CV file (PDF, DOCX, TXT)"),
    user_id: str = typer.Option("default", "--user-id", "-u", help="User ID for fingerprint"),
):
    """Extract research fingerprint from CV"""
    from scripts.seed_fingerprint import main as run_seed

    console.print(f"[bold green]Seeding fingerprint[/bold green] cv={cv_path} user={user_id}")
    asyncio.run(run_seed(cv_path=cv_path, user_id=user_id))


@app.command()
def init_db(
    drop: bool = typer.Option(False, "--drop", help="Drop existing tables first"),
):
    """Initialize database schema"""
    from scripts.migrate_db import main as run_migrate

    console.print(f"[bold yellow]Initializing database[/bold yellow] drop={drop}")
    asyncio.run(run_migrate(drop=drop))


@app.command()
def generate_content(
    days: int = typer.Option(7, "--days", help="Generate content for last N days"),
    output_dir: Path = typer.Option(Path("content"), "--output", "-o", help="Output directory"),
):
    """Generate SEO content and social posts"""
    from phdiscover.content.generator import main as run_content_gen

    console.print(f"[bold blue]Generating content[/bold blue] days={days}")
    asyncio.run(run_content_gen(days=days, output_dir=output_dir))


@app.command()
def reload_config():
    """Reload configuration from YAML files"""
    reload_settings()
    console.print("[green]Configuration reloaded[/green]")


@app.command()
def version():
    """Show version information"""
    settings = get_settings()
    console.print(f"PhDiscover Engine v{settings.app.version}")
    console.print(f"Environment: {settings.app.environment}")
    console.print(f"Python: {sys.version.split()[0]}")


def main():
    """Main entry point"""
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        sys.exit(130)
    except Exception as e:
        logger.exception("fatal_error", error=str(e))
        console.print(f"[bold red]Fatal error:[/bold red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()