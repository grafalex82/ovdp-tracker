"""Verify command-line database selection through startup and HTTP requests."""

import sys

import pytest
from flask import Flask

from ovdp_tracker import db
from ovdp_tracker.__main__ import main
from ovdp_tracker.db import Database


@pytest.fixture
def default_database_path(tmp_path, monkeypatch):
    path = tmp_path / "default" / "portfolio.sqlite3"
    monkeypatch.setattr(db, "DEFAULT_DATABASE", path)
    return path


@pytest.fixture
def server_runs(monkeypatch):
    runs = []
    monkeypatch.setattr(Flask, "run", lambda app, **options: runs.append((app, options)))
    return runs


def test_start_without_options_uses_default_database(default_database_path, server_runs, monkeypatch, tmp_path):
    # Run the process entry point without arguments from an unrelated directory.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["ovdp_tracker"])
    main()

    # Verify default storage and the local server settings.
    assert default_database_path.is_file()
    app, options = server_runs[0]
    assert options == {
        "host": "127.0.0.1",
        "port": 5000,
        "debug": False,
        "use_debugger": False,
        "use_reloader": False,
        "load_dotenv": False,
    }
    with app.test_client() as client:
        assert client.get("/").status_code == 200


@pytest.mark.parametrize("option", ["-d", "--database"])
@pytest.mark.parametrize("absolute", [False, True])
def test_selected_database_is_created_and_reused(
    option, absolute, tmp_path, monkeypatch, default_database_path, server_runs, issue_data, lot_data
):
    # Select a new nested path containing spaces, relative to the launch directory or absolute.
    monkeypatch.chdir(tmp_path)
    selected_path = tmp_path / "portfolio data" / "selected.sqlite3"
    argument = str(selected_path) if absolute else str(selected_path.relative_to(tmp_path))
    main([option, argument])
    assert selected_path.is_file()

    # Save a purchase in the selected file through the public storage API.
    with Database(selected_path) as database:
        issue = database.add_issue(**issue_data)
        bank = database.add_bank("Test Bank")
        database.add_lot(issue_id=issue["id"], bank_id=bank["id"], **lot_data)

    # Restart with the same argument and read the persisted portfolio through an HTTP request.
    main([option, argument])
    app, _ = server_runs[-1]
    with app.test_client() as client:
        response = client.get("/")
        assert response.status_code == 200
        assert issue["isin"] in response.get_data(as_text=True)
    assert not default_database_path.exists()


@pytest.mark.parametrize(
    "arguments, exit_code, message",
    [
        (["--help"], 0, "--database PATH"),
        (["-d"], 2, "expected one argument"),
        (["--database"], 2, "expected one argument"),
        (["--unknown"], 2, "unrecognized arguments"),
    ],
)
def test_help_and_invalid_arguments_do_not_start_application(
    arguments, exit_code, message, default_database_path, server_runs, capsys
):
    # Stop during command-line parsing before storage or the server is initialized.
    with pytest.raises(SystemExit) as error:
        main(arguments)

    # Verify the diagnostic, exit status, and absence of startup side effects.
    captured = capsys.readouterr()
    assert error.value.code == exit_code
    assert message in captured.out + captured.err
    assert server_runs == []
    assert not default_database_path.exists()
