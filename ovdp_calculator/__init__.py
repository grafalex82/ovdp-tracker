"""Local application for tracking Ukrainian government bonds."""

from secrets import token_hex

from flask import Flask
from itsdangerous import URLSafeSerializer

from .db import Database
from .nbu import NbuClient
from .web import purchase_pages, render_portfolio, with_database


def create_app(config: dict | None = None) -> Flask:
    """Create and return a configured Flask application, applying optional configuration overrides."""

    # Create the application and apply its configuration.
    app = Flask(__name__)
    app.config.from_mapping(DEBUG=False)
    if config is not None:
        app.config.update(config)
    app.extensions["nbu"] = NbuClient()
    app.extensions["issue_signer"] = URLSafeSerializer(app.config.get("SECRET_KEY") or token_hex(32), salt="nbu-issue")

    # Initialize or verify storage before accepting requests.
    with Database(app.config.get("DATABASE")):
        pass

    # Register purchase forms with request-specific storage.
    app.register_blueprint(purchase_pages)

    # Register the home page with an explicitly supplied database dependency.
    @app.get("/")
    @with_database
    def index(database: Database):
        return render_portfolio(database)

    # Return the fully configured application to the caller.
    return app
