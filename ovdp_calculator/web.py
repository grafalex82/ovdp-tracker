"""Adapt the independent database API to Flask request handlers."""

from functools import wraps

from flask import current_app

from .db import Database


def with_database(view):
    """Wrap a Flask view with a request-specific Database and return the decorated handler."""

    # Build a wrapper that preserves the handler's metadata and manages its database.
    @wraps(view)
    def wrapped(*args, **kwargs):
        with Database(current_app.config.get("DATABASE")) as database:
            return view(database, *args, **kwargs)

    # Return the decorated handler for Flask route registration.
    return wrapped
