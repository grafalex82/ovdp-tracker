"""Local application for tracking Ukrainian government bonds."""

from flask import Flask, render_template

from .db import Database
from .web import with_database


def create_app(config: dict | None = None) -> Flask:
    """Create and return a configured Flask application, applying optional configuration overrides."""

    # Create the application and apply its configuration.
    app = Flask(__name__)
    app.config.from_mapping(DEBUG=False)
    if config is not None:
        app.config.update(config)

    # Initialize or verify storage before accepting requests.
    with Database(app.config.get("DATABASE")):
        pass

    # Register the home page with an explicitly supplied database dependency.
    @app.get("/")
    @with_database
    def index(database: Database):
        return render_template("index.html")

    # Return the fully configured application to the caller.
    return app
