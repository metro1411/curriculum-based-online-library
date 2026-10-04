"""Application entry point for Smart DIT Learning Hub."""

import logging
import os
from datetime import datetime

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, logout_user
from sqlalchemy import inspect, text
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config, DATA_DIR, ensure_directories
import performance
from extensions import csrf, db, login_manager
from utils import human_filesize

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("smart_dit_learning_hub")

def create_app():
    ensure_directories()
    app = Flask(__name__)
    app.config.from_object(Config)
    if app.config["IS_PRODUCTION"]:
        required = [
            "SECRET_KEY", "DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
            "INITIAL_STUDENT_PASSWORD", "INITIAL_LECTURER_PASSWORD", "HOD_ACTIVATION_CODE",
        ]
        missing = [key for key in required if not os.environ.get(key, "").strip()]
        if missing:
            raise RuntimeError("Missing production configuration: " + ", ".join(missing))
        for key in ("INITIAL_STUDENT_PASSWORD", "INITIAL_LECTURER_PASSWORD"):
            if len(app.config[key]) < 12:
                raise RuntimeError(f"{key} must be at least 12 characters in production.")
        if len(app.config["HOD_ACTIVATION_CODE"]) < 16:
            raise RuntimeError("HOD_ACTIVATION_CODE must be at least 16 characters in production.")
    if app.config["TRUST_PROXY_HEADERS"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    app.jinja_env.filters["filesize"] = human_filesize
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    _register_login_manager()
    _register_account_guard(app)
    _register_blueprints(app)
    _register_error_handlers(app)
    _register_context_processors(app)
    _register_health_check(app)
    _register_security_headers(app)
    performance.init_app(app)
    _register_commands(app)

    with app.app_context():
        db.create_all()
        _apply_schema_migrations(app)
        db.create_all()
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
        return redirect(url_for("auth.login", next=request.path))


def _register_account_guard(app):
    @app.before_request
    def reject_deactivated_session():
        if current_user.is_authenticated and not current_user.is_active_account:
            logout_user()
            flash("Your account is not active. Please contact your department.", "warning")
            return redirect(url_for("auth.login"))


def _register_blueprints(app):
    from routes.ai import ai_bp
    from routes.api import api_bp
    from routes.auth import auth_bp
    from routes.lecturer import lecturer_bp
    from routes.main import main_bp
    from routes.profile import profile_bp
    from routes.department import department_bp
    from routes.notifications import notifications_bp
    from routes.student import student_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(student_bp)
    app.register_blueprint(lecturer_bp)
    app.register_blueprint(department_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(api_bp)


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
    """Apply small, idempotent schema migrations without deleting learner data."""
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    # New account/profile fields must be added explicitly because SQLAlchemy's
    # create_all intentionally does not alter tables that already exist.
    if "users" in tables:
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        additions = {
            "registration_number": "VARCHAR(10)",
            "account_status": "VARCHAR(20) NOT NULL DEFAULT 'active'",
            "profile_photo_filename": "VARCHAR(300)",
            "profile_photo_mime_type": "VARCHAR(120)",
            "academic_year_id": "INTEGER",
            "deactivated_at": "TIMESTAMP",
            "deactivated_by_id": "INTEGER",
        }
        with db.engine.begin() as connection:
            for name, definition in additions.items():
                if name not in user_columns:
                    connection.execute(text(f"ALTER TABLE users ADD COLUMN {name} {definition}"))
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_registration_number "
                "ON users (registration_number)"
            ))

    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if "student_preferences" in tables:
        preference_columns = {column["name"] for column in inspector.get_columns("student_preferences")}
        additions = {
            "learning_goal": "VARCHAR(220)",
            "data_saver": "BOOLEAN NOT NULL DEFAULT FALSE",
            "reminder_frequency": "VARCHAR(20) NOT NULL DEFAULT 'daily'",
            "optional_emails": "BOOLEAN NOT NULL DEFAULT TRUE",
            "goal_reminders": "BOOLEAN NOT NULL DEFAULT TRUE",
        }
        with db.engine.begin() as connection:
            for name, definition in additions.items():
                if name not in preference_columns:
                    connection.execute(text(f"ALTER TABLE student_preferences ADD COLUMN {name} {definition}"))

    additions_by_table = {
        "modules": {
            "academic_year_id": "INTEGER",
            "module_type": "VARCHAR(30) NOT NULL DEFAULT 'core'",
            "cohort_label": "VARCHAR(80)",
            "publication_status": "VARCHAR(20) NOT NULL DEFAULT 'published'",
            "created_by_id": "INTEGER",
            "updated_at": "TIMESTAMP",
        },
        "lecturer_assignments": {
            "status": "VARCHAR(20) NOT NULL DEFAULT 'approved'",
            "reviewed_by_id": "INTEGER",
            "reviewed_at": "TIMESTAMP",
            "rejection_reason": "VARCHAR(500)",
        },
        "topics": {
            "category": "VARCHAR(30) NOT NULL DEFAULT 'concept'",
            "status": "VARCHAR(30) NOT NULL DEFAULT 'planned'",
            "is_published": "BOOLEAN NOT NULL DEFAULT TRUE",
            "updated_at": "TIMESTAMP",
        },
        "resources": {
            "topic_id": "INTEGER",
        },
        "learning_events": {
            "qualifies_for_streak": "BOOLEAN NOT NULL DEFAULT FALSE",
        },
        "announcements": {
            "recipient_count": "INTEGER NOT NULL DEFAULT 0",
            "emailed": "BOOLEAN NOT NULL DEFAULT FALSE",
        },
    }
    for table_name, additions in additions_by_table.items():
        inspector = inspect(db.engine)
        if table_name not in inspector.get_table_names():
            continue
        column_names = {column["name"] for column in inspector.get_columns(table_name)}
        with db.engine.begin() as connection:
            for name, definition in additions.items():
                if name not in column_names:
                    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}"))

    inspector = inspect(db.engine)
    if "resources" in inspector.get_table_names():
        column_names = {column["name"] for column in inspector.get_columns("resources")}
        legacy_column = "is_demo_content"
        if legacy_column in column_names:
            # This obsolete field had no learner-facing data and prevented new
            # uploads in old SQLite packages.
            with db.engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE resources DROP COLUMN {legacy_column}"))
            app.logger.info("Removed legacy resources schema column during startup migration.")

    # Historical AI prompts must never remain available as lecturer analytics.
    if "learning_events" in inspector.get_table_names():
        with db.engine.begin() as connection:
            connection.execute(text(
                "UPDATE learning_events SET detail = 'Private AI learning question' "
                "WHERE event_type = 'ai_question'"
            ))


def _register_context_processors(app):
    import ai_engine
    from flask_login import current_user
    from models import Notification, StudentPreference
    import announcements
    from navigation import app_navigation, role_label

    @app.context_processor
    def inject_globals():
        data_saver = False
        unread_notification_count = 0
        nav = {"sections": [], "active": None}
        user_role = ""
        if current_user.is_authenticated and current_user.is_student:
            preference = StudentPreference.query.filter_by(student_id=current_user.id).first()
            data_saver = bool(preference and preference.data_saver)
        if current_user.is_authenticated:
            unread_notification_count = Notification.query.filter_by(
                user_id=current_user.id, is_read=False
            ).count()
            counts = {"unread_notification_count": unread_notification_count}
            if current_user.is_student and unread_notification_count:
                counts["unread_announcements"] = announcements.unread_count(current_user)
            nav = app_navigation(current_user, counts)
            user_role = role_label(current_user)
        return {
            "app_name": app.config["APP_NAME"],
            "app_tagline": app.config["APP_TAGLINE"],
            "ai_available": ai_engine.is_available(),
            "data_saver": data_saver,
            "unread_notification_count": unread_notification_count,
            "nav": nav,
            "user_role": user_role,
        }


def _register_security_headers(app):
    @app.after_request
    def secure_response(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; media-src 'self' https:; "
            "frame-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'",
        )
        if app.config["IS_PRODUCTION"]:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def _register_commands(app):
    @app.cli.command("send-study-reminders")
    def send_study_reminders():
        """Send one privacy-safe goal reminder at the selected cadence."""
        from models import Notification, StudentPreference, User, utcnow_naive
        from notifications import notify, reminder_is_due

        now = utcnow_naive()
        today = datetime.combine(now.date(), datetime.min.time())
        sent = 0
        preferences = StudentPreference.query.all()
        for preference in preferences:
            student = db.session.get(User, preference.student_id)
            if not student or not student.is_active_account or not reminder_is_due(preference, now):
                continue
            already_sent = Notification.query.filter(
                Notification.user_id == student.id,
                Notification.kind == "study_reminder",
                Notification.created_at >= today,
            ).first()
            if already_sent:
                continue
            notify(
                student,
                "study_reminder",
                "Keep your learning goal moving",
                f"Your weekly goal is {preference.weekly_goal_minutes} focused minutes. "
                "Open a module resource or ask a genuine academic question when you are ready.",
                target_url="/learning-insights",
                optional_email=True,
            )
            sent += 1
        db.session.commit()
        print(f"Sent {sent} study reminder notification(s).")


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
        "  Use a registered account to sign in.", "=" * 62, "",
    ]))


app = create_app()


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    _print_startup_banner(app)
    app.run(
        host=app.config["HOST"], port=app.config["PORT"],
        debug=debug_mode, use_reloader=debug_mode,
    )
