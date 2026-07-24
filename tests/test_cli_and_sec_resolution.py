from pathlib import Path

from typer.testing import CliRunner

from contract_ipo_monitor.cli import app
from contract_ipo_monitor.sources.sec import SECNormalizer


def test_sec_index_resolver_prefers_primary_filing_document():
    html = '''
    <html><body>
      <a href="/Archives/edgar/data/1/0001-index.htm">index</a>
      <a href="/Archives/edgar/data/1/s-1.htm">S-1</a>
      <a href="/Archives/edgar/data/1/instance.xml">XBRL</a>
    </body></html>
    '''
    url = SECNormalizer().primary_document_url(
        "https://www.sec.gov/Archives/edgar/data/1/0001-index.htm", html
    )
    assert url == "https://www.sec.gov/Archives/edgar/data/1/s-1.htm"


def test_cli_init_db_creates_schema(tmp_path: Path):
    db_path = tmp_path / "monitor.db"
    runner = CliRunner()
    result = runner.invoke(app, ["init-db", "--database", str(db_path)])
    assert result.exit_code == 0, result.output
    assert db_path.exists()
    assert "initialized" in result.output.lower()


def test_cli_check_config_reports_missing_settings(monkeypatch):
    for name in ("SEC_USER_AGENT", "SMTP_HOST", "SMTP_SENDER", "SMTP_RECIPIENTS"):
        monkeypatch.delenv(name, raising=False)
    runner = CliRunner()
    result = runner.invoke(app, ["check-config", "--env-file", "/nonexistent/.env"])
    assert result.exit_code == 2
    assert "SEC_USER_AGENT" in result.output
