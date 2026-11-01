"""Start the application with python -m ovdp_tracker."""

from . import create_app


def main() -> None:
    """Serve the local application until interrupted."""
    create_app().run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_debugger=False,
        use_reloader=False,
        load_dotenv=False,
    )


if __name__ == "__main__":
    main()
