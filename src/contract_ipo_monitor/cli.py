from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from .config import Settings
from .db import Database
from .service import MonitorService
from .research import ResearchStore, checkpoint_database, write_report
from .sources.state_local import AdapterInventory

app = typer.Typer(no_args_is_help=True, help="Company, IPO, world news and conditional trade research monitor")


@app.command("init-db")
def init_db(database: Path = typer.Option(Path("data/monitor.db"), "--database", help="SQLite database path")) -> None:
    db = Database(database)
    db.initialize()
    typer.echo(f"Initialized database at {database}")


@app.command("check-config")
def check_config(env_file: Path = typer.Option(Path(".env"), "--env-file")) -> None:
    settings = Settings.from_env(env_file)
    errors = settings.runtime_errors()
    if errors:
        for error in errors:
            typer.echo(f"ERROR: {error}")
        raise typer.Exit(code=2)
    typer.echo("Configuration is valid.")


@app.command("run-once")
def run_once(env_file: Path = typer.Option(Path(".env"), "--env-file"), report_dir: Path = typer.Option(Path("data/reports"), "--report-dir"), checkpoint: Path | None = typer.Option(None, "--checkpoint")) -> None:
    settings = Settings.from_env(env_file)
    errors = settings.runtime_errors()
    if errors:
        for error in errors:
            typer.echo(f"ERROR: {error}")
        raise typer.Exit(code=2)
    service = MonitorService.build_default(settings)
    async def execute():
        try:
            return await service.run_once()
        finally:
            await service.aclose()
    summary = asyncio.run(execute())
    write_report(service.last_report, report_dir)
    if checkpoint:
        checkpoint_database(service.db, checkpoint)
    typer.echo(json.dumps(summary, indent=2, sort_keys=True))
    typer.echo(f"Run status: {service.last_report['status']}. Report: {report_dir / 'latest.md'}")
    if not service.last_report["health"]["ready"]:
        raise typer.Exit(code=3)


@app.command("status")
def status(env_file: Path = typer.Option(Path(".env"), "--env-file")) -> None:
    settings = Settings.from_env(env_file)
    if not settings.database_path.is_file():
        typer.echo("No monitor database exists yet.")
        raise typer.Exit(code=1)
    store = ResearchStore(Database(settings.database_path))
    store.initialize()
    report = store.latest_run()
    typer.echo(json.dumps(report or {"status": "not_run"}, indent=2))


@app.command("report")
def report(env_file: Path = typer.Option(Path(".env"), "--env-file"), output: Path = typer.Option(Path("data/reports"), "--output")) -> None:
    settings = Settings.from_env(env_file)
    db = Database(settings.database_path)
    if not settings.database_path.is_file():
        raise typer.BadParameter("Run the monitor first to collect evidence.")
    store = ResearchStore(db)
    store.initialize()
    latest = store.latest_run()
    if latest is None:
        raise typer.BadParameter("No completed run receipt exists yet.")
    write_report(latest, output)
    typer.echo(f"Report written to {output / 'latest.md'}")


@app.command("run")
def run(env_file: Path = typer.Option(Path(".env"), "--env-file"), no_health: bool = typer.Option(False, "--no-health")) -> None:
    settings = Settings.from_env(env_file)
    errors = settings.runtime_errors()
    if errors:
        for error in errors:
            typer.echo(f"ERROR: {error}")
        raise typer.Exit(code=2)
    service = MonitorService.build_default(settings)
    asyncio.run(service.run_forever(serve_health=not no_health))


@app.command("adapters")
def adapters() -> None:
    inventory = AdapterInventory()
    inventory.register("federal-usaspending", "USAspending prime contract awards", enabled=True)
    inventory.register("federal-sam", "SAM.gov Contract Awards confirmation", enabled=False)
    typer.echo(json.dumps({"nationwide_complete": inventory.nationwide_complete, "adapters": inventory.report()}, indent=2))
