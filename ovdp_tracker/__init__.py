"""OVDP Tracker: a local application for tracking Ukrainian government bonds."""

from secrets import token_hex

from flask import Flask, render_template
from itsdangerous import URLSafeSerializer

from .db import Database
from .nbu import NbuClient
from .messages import user_message
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

    # Present request errors in Russian while retaining internal exception descriptions.
    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(500)
    def request_error(error):
        message = "Произошла внутренняя ошибка. Повторите попытку позже." if error.code == 500 else None
        return render_template("request_error.html", message=message or user_message(error.description)), error.code

    # Register the home page with an explicitly supplied database dependency.
    @app.get("/")
    @with_database
    def index(database: Database):
        return render_portfolio(database)

    # Return the fully configured application to the caller.
    return app
