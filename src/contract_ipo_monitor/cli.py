from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from .config import Settings
from .db import Database
from .service import MonitorService
from .sources.state_local import AdapterInventory

app = typer.Typer(no_args_is_help=True, help="Evidence-gated government contract + IPO monitor")


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
def run_once(env_file: Path = typer.Option(Path(".env"), "--env-file")) -> None:
    settings = Settings.from_env(env_file)
    errors = settings.runtime_errors()
    if errors:
        for error in errors:
            typer.echo(f"ERROR: {error}")
        raise typer.Exit(code=2)
    service = MonitorService.build_default(settings)
    summary = asyncio.run(service.run_once())
    typer.echo(json.dumps(summary, indent=2, sort_keys=True))


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
