"""Application entry point for Smart DIT Learning Hub."""

import logging
import os
import warnings

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import inspect, text
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config, DATA_DIR, ensure_directories
from extensions import csrf, db, login_manager
from utils import human_filesize

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("smart_dit_learning_hub")

try:
    from sqlalchemy.exc import LegacyAPIWarning
    warnings.filterwarnings("ignore", category=LegacyAPIWarning)
except ImportError:
    pass


def create_app():
    ensure_directories()
    app = Flask(__name__)
    app.config.from_object(Config)
    if app.config["IS_PRODUCTION"]:
        required = [
            "SECRET_KEY", "DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
            "INITIAL_STUDENT_PASSWORD", "INITIAL_LECTURER_PASSWORD",
        ]
        missing = [key for key in required if not os.environ.get(key, "").strip()]
        if missing:
            raise RuntimeError("Missing production configuration: " + ", ".join(missing))
        for key in ("INITIAL_STUDENT_PASSWORD", "INITIAL_LECTURER_PASSWORD"):
            if len(app.config[key]) < 12:
                raise RuntimeError(f"{key} must be at least 12 characters in production.")
    if app.config["TRUST_PROXY_HEADERS"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    app.jinja_env.filters["filesize"] = human_filesize
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    _register_login_manager()
    _register_blueprints(app)
    _register_error_handlers(app)
    _register_context_processors(app)
    _register_health_check(app)

    with app.app_context():
        db.create_all()
        _apply_schema_migrations(app)
        from storage_backend import ensure_storage_bucket
        ensure_storage_bucket()
        try:
            import seed
            seed.run()
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Initial learning data setup encountered an error.")
            if app.config["IS_PRODUCTION"]:
                raise
    return app


def _register_login_manager():
    from models import User
    login_manager.login_message = ""

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        flash("Please log in to continue.", "warning")
        if request.path.startswith("/lecturer"):
            return redirect(url_for("auth.lecturer_login", next=request.path))
        return redirect(url_for("auth.student_login", next=request.path))


def _register_blueprints(app):
    from routes.ai import ai_bp
    from routes.api import api_bp
    from routes.auth import auth_bp
    from routes.lecturer import lecturer_bp
    from routes.main import main_bp
    from routes.student import student_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(student_bp)
    app.register_blueprint(lecturer_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(api_bp)
    csrf.exempt(api_bp)


def _register_error_handlers(app):
    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def too_large(_error):
        flash("That file is too large. The maximum upload size is 30 MB.", "error")
        return redirect(request.referrer or url_for("main.landing"))

    @app.errorhandler(500)
    def server_error(error):
        db.session.rollback()
        app.logger.exception("Unhandled server error: %s", error)
        return render_template("errors/500.html"), 500


def _apply_schema_migrations(app):
    """Apply small, idempotent schema repairs for databases created by older builds."""
    inspector = inspect(db.engine)
    if "resources" not in inspector.get_table_names():
        return
    column_names = {column["name"] for column in inspector.get_columns("resources")}
    legacy_column = "is_demo_content"
    if legacy_column not in column_names:
        return

    # This legacy, non-null field was removed from the model. Without a server
    # default, old SQLite databases reject every new lecturer upload. It held no
    # learner-facing data, so removing it restores compatibility cleanly.
    with db.engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE resources DROP COLUMN {legacy_column}"))
    app.logger.info("Removed legacy resources schema column during startup migration.")


def _register_context_processors(app):
    import ai_engine

    @app.context_processor
    def inject_globals():
        return {
            "app_name": app.config["APP_NAME"],
            "app_tagline": app.config["APP_TAGLINE"],
            "ai_available": ai_engine.is_available(),
        }


def _register_health_check(app):
    @app.get("/health")
    def health_check():
        try:
            db.session.execute(text("SELECT 1"))
        except Exception:
            app.logger.exception("Health check database query failed.")
            return jsonify(status="unavailable"), 503
        return jsonify(status="ok"), 200


def _print_startup_banner(app):
    ai_configured = bool(app.config.get("GEMINI_API_KEY"))
    print("\n".join([
        "", "=" * 62, "  SMART DIT LEARNING HUB",
        "  Resources, learning support and academic insights.", "=" * 62,
        f"  Database:        {os.path.join(DATA_DIR, 'smart_dit_archive.db')}",
        f"  Gemini AI:       {'CONFIGURED' if ai_configured else 'NOT CONFIGURED'}", "",
        f"  Open in your browser:  http://{app.config['HOST']}:{app.config['PORT']}", "",
        "  Student login:         student@dit.ac.tz  /  Student@123",
        "  Lecturer login:        lecturer@dit.ac.tz /  Lecturer@123", "=" * 62, "",
    ]))


app = create_app()


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    _print_startup_banner(app)
    app.run(
        host=app.config["HOST"], port=app.config["PORT"],
        debug=debug_mode, use_reloader=debug_mode,
    )
