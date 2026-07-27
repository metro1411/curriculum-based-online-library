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
    password_hash = db.Column(db.String(255), nullable=False)

    # 'student' or 'lecturer'
    role = db.Column(db.String(20), nullable=False, default="student", index=True)

    # Student-only academic placement (nullable so lecturers don't need it)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    programme_id = db.Column(db.Integer, db.ForeignKey("programmes.id"), nullable=True)
    nta_level_id = db.Column(db.Integer, db.ForeignKey("nta_levels.id"), nullable=True)
    semester_id = db.Column(db.Integer, db.ForeignKey("semesters.id"), nullable=True)

    is_active_account = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    department = db.relationship("Department", foreign_keys=[department_id])
    programme = db.relationship("Programme", foreign_keys=[programme_id])
    nta_level = db.relationship("NtaLevel", foreign_keys=[nta_level_id])
    semester = db.relationship("Semester", foreign_keys=[semester_id])

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
    name = db.Column(db.String(200), nullable=False)
    # Official module codes are intentionally left blank unless DIT provides
    # them - see project documentation. Never auto-generate a fake code.
    code = db.Column(db.String(50), nullable=True)
    description = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    display_order = db.Column(db.Integer, default=0, nullable=False)

    semester = db.relationship("Semester", back_populates="modules")
    resources = db.relationship(
        "Resource", back_populates="module", cascade="all, delete-orphan"
    )


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


class Resource(db.Model):
    __tablename__ = "resources"

    id = db.Column(db.Integer, primary_key=True)
    module_id = db.Column(db.Integer, db.ForeignKey("modules.id"), nullable=False)

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
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    lecturer = db.relationship("User", foreign_keys=[lecturer_id])
    module = db.relationship("Module")

    __table_args__ = (
        db.UniqueConstraint("lecturer_id", "module_id", name="uq_lecturer_module_assignment"),
    )


class Topic(db.Model):
    """A curriculum unit managed within one module."""

    __tablename__ = "topics"

    id = db.Column(db.Integer, primary_key=True)
    module_id = db.Column(db.Integer, db.ForeignKey("modules.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    learning_outcome = db.Column(db.Text, nullable=True)
    unit_label = db.Column(db.String(100), nullable=True)
    description = db.Column(db.Text, nullable=True)
    display_order = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    module = db.relationship("Module")


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
