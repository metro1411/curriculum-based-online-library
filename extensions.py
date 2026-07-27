"""
extensions.py
-------------
Flask extension instances, created here (not attached to an app yet) so that
models.py, routes/*.py and app.py can all import them without circular
imports. app.py calls .init_app(app) on each of these during app creation.
"""

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()

login_manager.session_protection = "strong"
