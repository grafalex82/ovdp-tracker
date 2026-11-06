"""Start the application with python -m ovdp_tracker."""

from argparse import ArgumentParser

from . import create_app


def main(argv: list[str] | None = None) -> None:
    """Serve the local application, parsing argv or the process command line when omitted."""

    # Parse the optional storage location before initializing the application.
    parser = ArgumentParser(prog="python -m ovdp_tracker", description="Run the local OVDP Tracker application.")
    parser.add_argument(
        "-d",
        "--database",
        metavar="PATH",
        help="SQLite database file (default: instance/portfolio.sqlite3 in the project directory)",
    )
    args = parser.parse_args(argv)

    # Pass the selected file to startup checks and request-specific database instances.
    create_app({"DATABASE": args.database}).run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_debugger=False,
        use_reloader=False,
        load_dotenv=False,
    )


if __name__ == "__main__":
    main()
