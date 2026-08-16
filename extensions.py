"""Shared Flask extension instances (kept separate to avoid circular imports)."""
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()

# Schema changes go through Alembic (`flask db migrate` / `flask db upgrade`),
# not create_all(). create_all() silently ignores altered columns on an existing
# table, so it would quietly skip any change to a table that already exists.
migrate = Migrate()

# Global, with no exemptions. This app was built with CSRF from the start, so
# every form carries a token and every state-changing fetch() sends the
# X-CSRFToken header — see static/petmap/csrf.js.
csrf = CSRFProtect()

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "info"
