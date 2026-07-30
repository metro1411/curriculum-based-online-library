"""
models.py
---------
SQLAlchemy models for Smart DIT Online Archive.

Curriculum hierarchy (mirrors the DIT / NTA academic structure):

    Department -> Programme -> NtaLevel -> Semester -> Module -> Resource

Supporting models:
    User            students and lecturers (role-based)
    ResourceChunk   extracted text chunks used by the lightweight RAG engine
    ResourceView    "recently viewed" tracking
    Download        "recently downloaded" tracking
    AIConversation  a saved chat thread with the DIT AI Learning Assistant
    AIMessage       one message (user or assistant) inside a conversation
"""

from datetime import datetime, timezone
import secrets

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db


def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False, index=True)
    username = db.Column(db.String(80), unique=True, nullable=True, index=True)
    # Registration/staff identifiers are the primary human-facing identities.
    # They remain nullable only so existing deployments can migrate safely.
    registration_number = db.Column(db.String(10), unique=True, nullable=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    # 'student', 'lecturer' or 'department_head'
    role = db.Column(db.String(20), nullable=False, default="student", index=True)
    # Pending lecturer accounts cannot sign in or publish until the department
    # head reviews the request. Existing accounts are treated as active.
    account_status = db.Column(db.String(20), nullable=False, default="active", index=True)

    # Student-only academic placement (nullable so lecturers don't need it)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    programme_id = db.Column(db.Integer, db.ForeignKey("programmes.id"), nullable=True)
    nta_level_id = db.Column(db.Integer, db.ForeignKey("nta_levels.id"), nullable=True)
    semester_id = db.Column(db.Integer, db.ForeignKey("semesters.id"), nullable=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey("academic_years.id"), nullable=True)

    is_active_account = db.Column(db.Boolean, default=True, nullable=False)
    deactivated_at = db.Column(db.DateTime, nullable=True)
    deactivated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    profile_photo_filename = db.Column(db.String(300), nullable=True)
    profile_photo_mime_type = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    department = db.relationship("Department", foreign_keys=[department_id])
    programme = db.relationship("Programme", foreign_keys=[programme_id])
    nta_level = db.relationship("NtaLevel", foreign_keys=[nta_level_id])
    semester = db.relationship("Semester", foreign_keys=[semester_id])
    academic_year = db.relationship("AcademicYear", foreign_keys=[academic_year_id])
    deactivated_by = db.relationship("User", remote_side=[id], foreign_keys=[deactivated_by_id])

    uploaded_resources = db.relationship(
        "Resource", back_populates="uploaded_by", foreign_keys="Resource.uploaded_by_id"
    )

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_student(self):
        return self.role == "student"

    @property
    def is_lecturer(self):
        return self.role == "lecturer"

    @property
    def is_department_head(self):
        return self.role == "department_head"

    @property
    def is_pending(self):
        return self.account_status == "pending"

    @property
    def is_active(self):
        """Flask-Login must reject deactivated accounts immediately."""
        return bool(self.is_active_account and self.account_status == "active")

    # Flask-Login uses get_id(); UserMixin already provides this from .id
    def __repr__(self):
        return f"<User {self.email} ({self.role})>"


# ---------------------------------------------------------------------------
# Curriculum hierarchy
# ---------------------------------------------------------------------------

class Department(db.Model):
    __tablename__ = "departments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    slug = db.Column(db.String(150), nullable=False, unique=True, index=True)
    description = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    display_order = db.Column(db.Integer, default=0, nullable=False)

    programmes = db.relationship(
        "Programme", back_populates="department",
        cascade="all, delete-orphan", order_by="Programme.display_order",
    )
    academic_years = db.relationship(
        "AcademicYear", back_populates="department",
        cascade="all, delete-orphan", order_by="AcademicYear.label.desc()",
    )


class AcademicYear(db.Model):
    """A durable curriculum edition.

    Modules are attached to an edition instead of being overwritten when a
    new academic year starts. Historical content therefore stays available
    for audit and authorised review.
    """

    __tablename__ = "academic_years"

    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False, index=True)
    label = db.Column(db.String(20), nullable=False)
    is_current = db.Column(db.Boolean, default=False, nullable=False, index=True)
    status = db.Column(db.String(20), default="active", nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    department = db.relationship("Department", back_populates="academic_years")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    modules = db.relationship("Module", back_populates="academic_year")

    __table_args__ = (
        db.UniqueConstraint("department_id", "label", name="uq_academic_year_department_label"),
    )


class Programme(db.Model):
    __tablename__ = "programmes"

    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    slug = db.Column(db.String(150), nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    display_order = db.Column(db.Integer, default=0, nullable=False)

    department = db.relationship("Department", back_populates="programmes")
    nta_levels = db.relationship(
        "NtaLevel", back_populates="programme",
        cascade="all, delete-orphan", order_by="NtaLevel.level_number",
    )

    __table_args__ = (
        db.UniqueConstraint("department_id", "slug", name="uq_programme_dept_slug"),
    )


class NtaLevel(db.Model):
    __tablename__ = "nta_levels"

    id = db.Column(db.Integer, primary_key=True)
    programme_id = db.Column(db.Integer, db.ForeignKey("programmes.id"), nullable=False)
    level_number = db.Column(db.Integer, nullable=False)  # 4-9
    is_active = db.Column(db.Boolean, default=False, nullable=False)

    programme = db.relationship("Programme", back_populates="nta_levels")
    semesters = db.relationship(
        "Semester", back_populates="nta_level",
        cascade="all, delete-orphan", order_by="Semester.semester_number",
    )

    __table_args__ = (
        db.UniqueConstraint("programme_id", "level_number", name="uq_level_programme_number"),
    )

    @property
    def label(self):
        return f"NTA Level {self.level_number}"


class Semester(db.Model):
    __tablename__ = "semesters"

    id = db.Column(db.Integer, primary_key=True)
    nta_level_id = db.Column(db.Integer, db.ForeignKey("nta_levels.id"), nullable=False)
    semester_number = db.Column(db.Integer, nullable=False)  # 1 or 2
    is_active = db.Column(db.Boolean, default=False, nullable=False)

    nta_level = db.relationship("NtaLevel", back_populates="semesters")
    modules = db.relationship(
        "Module", back_populates="semester",
        cascade="all, delete-orphan", order_by="Module.display_order",
    )

    __table_args__ = (
        db.UniqueConstraint("nta_level_id", "semester_number", name="uq_semester_level_number"),
    )

    @property
    def label(self):
        return f"Semester {self.semester_number}"


class Module(db.Model):
    __tablename__ = "modules"

    id = db.Column(db.Integer, primary_key=True)
    semester_id = db.Column(db.Integer, db.ForeignKey("semesters.id"), nullable=False)
    academic_year_id = db.Column(db.Integer, db.ForeignKey("academic_years.id"), nullable=True, index=True)
    name = db.Column(db.String(200), nullable=False)
    # Official module codes are intentionally left blank unless DIT provides
    # them - see project documentation. Never auto-generate a fake code.
    code = db.Column(db.String(50), nullable=True)
    module_type = db.Column(db.String(30), nullable=False, default="core")
    cohort_label = db.Column(db.String(80), nullable=True)
    description = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    publication_status = db.Column(db.String(20), nullable=False, default="published", index=True)
    display_order = db.Column(db.Integer, default=0, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    semester = db.relationship("Semester", back_populates="modules")
    academic_year = db.relationship("AcademicYear", back_populates="modules")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    resources = db.relationship(
        "Resource", back_populates="module", cascade="all, delete-orphan"
    )

    @property
    def type_label(self):
        return "General Studies Module" if self.module_type == "general_studies" else "Core Module"

    @property
    def is_published(self):
        return self.is_active and self.publication_status == "published"


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

RESOURCE_TYPES = [
    ("notes", "Notes"),
    ("presentation", "PowerPoint Presentation"),
    ("book", "Book / Reference"),
    ("video", "Video"),
    ("practical_guide", "Practical Guide"),
    ("past_paper", "Past Paper"),
    ("worked_example", "Worked Example"),
    ("assignment", "Assignment"),
    ("revision", "Revision Material"),
    ("other", "Other Resource"),
]
RESOURCE_TYPE_KEYS = [key for key, _ in RESOURCE_TYPES]
RESOURCE_TYPE_LABELS = dict(RESOURCE_TYPES)

TOPIC_CATEGORIES = [
    ("concept", "Core Concept"),
    ("theory", "Theory"),
    ("practical", "Practical / Lab"),
    ("tutorial", "Tutorial"),
    ("project", "Project"),
    ("revision", "Revision"),
]
TOPIC_CATEGORY_KEYS = [key for key, _ in TOPIC_CATEGORIES]
TOPIC_CATEGORY_LABELS = dict(TOPIC_CATEGORIES)


class Resource(db.Model):
    __tablename__ = "resources"

    id = db.Column(db.Integer, primary_key=True)
    module_id = db.Column(db.Integer, db.ForeignKey("modules.id"), nullable=False)
    topic_id = db.Column(db.Integer, db.ForeignKey("topics.id"), nullable=True, index=True)

    title = db.Column(db.String(250), nullable=False)
    description = db.Column(db.Text, nullable=True)
    resource_type = db.Column(db.String(30), nullable=False, default="other")

    # Uploaded file (mutually exclusive-ish with external_url, but both are
    # allowed to be blank-checked independently for flexibility)
    stored_filename = db.Column(db.String(300), nullable=True)
    original_filename = db.Column(db.String(300), nullable=True)
    file_size_bytes = db.Column(db.Integer, nullable=True)
    mime_type = db.Column(db.String(120), nullable=True)

    external_url = db.Column(db.String(500), nullable=True)

    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    verification_status = db.Column(db.String(20), nullable=False, default="pending")
    # 'verified' or 'pending'

    text_extraction_status = db.Column(db.String(20), nullable=False, default="not_applicable")
    # 'success' | 'failed' | 'not_applicable' (e.g. external link, or format
    # with no extractable text)

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    module = db.relationship("Module", back_populates="resources")
    topic = db.relationship("Topic", back_populates="resources")
    uploaded_by = db.relationship(
        "User", back_populates="uploaded_resources", foreign_keys=[uploaded_by_id]
    )
    chunks = db.relationship(
        "ResourceChunk", back_populates="resource", cascade="all, delete-orphan"
    )
    views = db.relationship(
        "ResourceView", back_populates="resource", cascade="all, delete-orphan"
    )
    downloads = db.relationship(
        "Download", back_populates="resource", cascade="all, delete-orphan"
    )

    @property
    def type_label(self):
        return RESOURCE_TYPE_LABELS.get(self.resource_type, "Other Resource")

    @property
    def is_verified(self):
        return self.verification_status == "verified"

    @property
    def is_external(self):
        return bool(self.external_url) and not self.stored_filename

    @property
    def file_extension(self):
        if self.original_filename and "." in self.original_filename:
            return self.original_filename.rsplit(".", 1)[-1].lower()
        return None


class ResourceChunk(db.Model):
    __tablename__ = "resource_chunks"

    id = db.Column(db.Integer, primary_key=True)
    resource_id = db.Column(db.Integer, db.ForeignKey("resources.id"), nullable=False)
    chunk_index = db.Column(db.Integer, nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    resource = db.relationship("Resource", back_populates="chunks")


class ResourceView(db.Model):
    __tablename__ = "resource_views"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    resource_id = db.Column(db.Integer, db.ForeignKey("resources.id"), nullable=False)
    viewed_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    student = db.relationship("User")
    resource = db.relationship("Resource", back_populates="views")


class Download(db.Model):
    __tablename__ = "downloads"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    resource_id = db.Column(db.Integer, db.ForeignKey("resources.id"), nullable=False)
    downloaded_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    student = db.relationship("User")
    resource = db.relationship("Resource", back_populates="downloads")


# ---------------------------------------------------------------------------
# AI Learning Assistant conversation history
# ---------------------------------------------------------------------------

AI_MODES = [
    ("explain", "Explain Concept"),
    ("solve", "Solve Question"),
    ("summarize", "Summarize Material"),
    ("example", "Give Example"),
    ("quiz", "Generate Quiz"),
    ("practice", "Generate Practice Questions"),
    ("flashcards", "Create Flashcards"),
    ("revision", "Revision Sheet"),
    ("ask_resource", "Ask About This Resource"),
]
AI_MODE_LABELS = dict(AI_MODES)


class AIConversation(db.Model):
    __tablename__ = "ai_conversations"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    module_id = db.Column(db.Integer, db.ForeignKey("modules.id"), nullable=True)
    title = db.Column(db.String(200), nullable=False, default="New Conversation")
    mode = db.Column(db.String(30), nullable=False, default="explain")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    student = db.relationship("User")
    module = db.relationship("Module")
    messages = db.relationship(
        "AIMessage", back_populates="conversation",
        cascade="all, delete-orphan", order_by="AIMessage.created_at",
    )


class AIMessage(db.Model):
    __tablename__ = "ai_messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("ai_conversations.id"), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'user' or 'assistant'
    content = db.Column(db.Text, nullable=False)
    content_html = db.Column(db.Text, nullable=True)  # rendered markdown (assistant only)
    sources_json = db.Column(db.Text, nullable=True)  # JSON list of {id, title, type}
    general_guidance = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    conversation = db.relationship("AIConversation", back_populates="messages")


# ---------------------------------------------------------------------------
# Learning workspace, content, and analytics
# ---------------------------------------------------------------------------

class LecturerAssignment(db.Model):
    """Explicit academic scope for a lecturer's workspace.

    Keeping assignments in their own table means lecturers can only open a
    dashboard and manage content for modules that have been assigned to them.
    """

    __tablename__ = "lecturer_assignments"

    id = db.Column(db.Integer, primary_key=True)
    lecturer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    module_id = db.Column(db.Integer, db.ForeignKey("modules.id"), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="approved", index=True)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    rejection_reason = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    lecturer = db.relationship("User", foreign_keys=[lecturer_id])
    module = db.relationship("Module")
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_id])

    __table_args__ = (
        db.UniqueConstraint("lecturer_id", "module_id", name="uq_lecturer_module_assignment"),
    )

    @property
    def is_approved(self):
        return self.status == "approved"


class Topic(db.Model):
    """A curriculum unit managed within one module."""

    __tablename__ = "topics"

    id = db.Column(db.Integer, primary_key=True)
    module_id = db.Column(db.Integer, db.ForeignKey("modules.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(30), nullable=False, default="concept", index=True)
    learning_outcome = db.Column(db.Text, nullable=True)
    unit_label = db.Column(db.String(100), nullable=True)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), nullable=False, default="planned")
    is_published = db.Column(db.Boolean, nullable=False, default=True)
    display_order = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    module = db.relationship("Module")
    resources = db.relationship("Resource", back_populates="topic")

    @property
    def category_label(self):
        return TOPIC_CATEGORY_LABELS.get(self.category, "Core Concept")


class Announcement(db.Model):
    __tablename__ = "announcements"

    id = db.Column(db.Integer, primary_key=True)
    module_id = db.Column(db.Integer, db.ForeignKey("modules.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_pinned = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    module = db.relationship("Module")
    author = db.relationship("User", foreign_keys=[author_id])


class LearningEvent(db.Model):
    """Privacy-conscious, event-level learning telemetry used for insights."""

    __tablename__ = "learning_events"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    module_id = db.Column(db.Integer, db.ForeignKey("modules.id"), nullable=False, index=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("topics.id"), nullable=True, index=True)
    event_type = db.Column(db.String(40), nullable=False, index=True)
    duration_minutes = db.Column(db.Integer, default=0, nullable=False)
    detail = db.Column(db.String(240), nullable=True)
    qualifies_for_streak = db.Column(db.Boolean, default=False, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    student = db.relationship("User", foreign_keys=[student_id])
    module = db.relationship("Module")
    topic = db.relationship("Topic")


class StudentPreference(db.Model):
    __tablename__ = "student_preferences"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)
    response_style = db.Column(db.String(30), nullable=False, default="guided")
    weekly_goal_minutes = db.Column(db.Integer, nullable=False, default=120)
    learning_goal = db.Column(db.String(220), nullable=True)
    data_saver = db.Column(db.Boolean, nullable=False, default=False)
    reminder_frequency = db.Column(db.String(20), nullable=False, default="daily")
    optional_emails = db.Column(db.Boolean, nullable=False, default=True)
    goal_reminders = db.Column(db.Boolean, nullable=False, default=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    student = db.relationship("User", foreign_keys=[student_id])


class AIAnswerFeedback(db.Model):
    __tablename__ = "ai_answer_feedback"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("ai_messages.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    rating = db.Column(db.Integer, nullable=True)
    is_unclear = db.Column(db.Boolean, default=False, nullable=False)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    message = db.relationship("AIMessage")
    student = db.relationship("User", foreign_keys=[student_id])


class SavedItem(db.Model):
    __tablename__ = "saved_items"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    resource_id = db.Column(db.Integer, db.ForeignKey("resources.id"), nullable=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("ai_conversations.id"), nullable=True)
    label = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    student = db.relationship("User", foreign_keys=[student_id])
    resource = db.relationship("Resource")
    conversation = db.relationship("AIConversation")

    __table_args__ = (
        db.UniqueConstraint("student_id", "resource_id", name="uq_saved_student_resource"),
    )


# ---------------------------------------------------------------------------
# Department-head approval workflow
# ---------------------------------------------------------------------------

class LecturerRequest(db.Model):
    """A lecturer registration awaiting review by the Electrical HOD."""

    __tablename__ = "lecturer_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True, index=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False, index=True)
    teaching_interest = db.Column(db.String(300), nullable=True)
    note = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])
    department = db.relationship("Department", foreign_keys=[department_id])
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_id])


# ---------------------------------------------------------------------------
# Private academic questions, engagement sessions, notifications and audit
# ---------------------------------------------------------------------------

QUESTION_STATUSES = (
    ("new", "New"),
    ("reviewing", "Reviewing"),
    ("answered", "Answered"),
    ("will_address_in_class", "Will Address in Class"),
    ("closed", "Closed"),
)
QUESTION_STATUS_KEYS = {key for key, _label in QUESTION_STATUSES}


def anonymous_question_reference():
    return f"Q-{secrets.token_hex(4).upper()}"


class AcademicQuestion(db.Model):
    """A private student-to-lecturer question.

    The lecturer-facing UI exposes ``anonymous_ref`` but never ``student_id``.
    The ownership link remains server-side so only the correct student receives
    the response and email notification.
    """

    __tablename__ = "academic_questions"

    id = db.Column(db.Integer, primary_key=True)
    anonymous_ref = db.Column(
        db.String(20), unique=True, nullable=False, default=anonymous_question_reference, index=True
    )
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    module_id = db.Column(db.Integer, db.ForeignKey("modules.id"), nullable=False, index=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("topics.id"), nullable=True, index=True)
    lecturer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    subject = db.Column(db.String(180), nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), nullable=False, default="new", index=True)
    answer = db.Column(db.Text, nullable=True)
    academic_verified = db.Column(db.Boolean, nullable=False, default=True)
    responded_at = db.Column(db.DateTime, nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    student = db.relationship("User", foreign_keys=[student_id])
    module = db.relationship("Module")
    topic = db.relationship("Topic")
    lecturer = db.relationship("User", foreign_keys=[lecturer_id])

    @property
    def status_label(self):
        return dict(QUESTION_STATUSES).get(self.status, self.status.replace("_", " ").title())


class ResourceStudySession(db.Model):
    """Server-verified active study time for one student and resource."""

    __tablename__ = "resource_study_sessions"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    resource_id = db.Column(db.Integer, db.ForeignKey("resources.id"), nullable=False, index=True)
    module_id = db.Column(db.Integer, db.ForeignKey("modules.id"), nullable=False, index=True)
    active_seconds = db.Column(db.Integer, nullable=False, default=0)
    last_heartbeat_at = db.Column(db.DateTime, nullable=True)
    qualified_at = db.Column(db.DateTime, nullable=True)
    started_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    student = db.relationship("User", foreign_keys=[student_id])
    resource = db.relationship("Resource")
    module = db.relationship("Module")


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(40), nullable=False, index=True)
    title = db.Column(db.String(180), nullable=False)
    body = db.Column(db.String(500), nullable=False)
    target_url = db.Column(db.String(500), nullable=True)
    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    email_status = db.Column(db.String(30), nullable=False, default="pending")
    email_attempted_at = db.Column(db.DateTime, nullable=True)
    email_error = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    user = db.relationship("User", foreign_keys=[user_id])


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True, index=True)
    action = db.Column(db.String(80), nullable=False, index=True)
    target_type = db.Column(db.String(80), nullable=False)
    target_id = db.Column(db.String(80), nullable=True)
    target_label = db.Column(db.String(240), nullable=True)
    details_json = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    actor = db.relationship("User", foreign_keys=[actor_id])
    department = db.relationship("Department", foreign_keys=[department_id])
