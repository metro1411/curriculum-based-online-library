"""Curriculum context for DIT AI.

Builds what the assistant knows about a student's place in the DIT
curriculum, retrieves structured curriculum and published prospectus text,
and classifies each question so answers are honestly labelled:

    lecturer_content    a lecturer-approved DIT resource answers it
    dit_curriculum      it matches structured curriculum / published prospectus
    general_academic    related to the student's modules, but not in DIT material
    outside_curriculum  not identified as part of the student's current curriculum

The goal is context, not restriction: every category is still answered.
Retrieval priority used by ai_engine (highest first):

    1. lecturer-approved resources for the selected/current module
    2. structured curriculum data (module records, topics, outcomes)
    3. approved DIT academic documents (published prospectus text)
    4. other approved resources in the student's current modules
    5. general knowledge (optionally web-grounded), always labelled
"""

from __future__ import annotations

from dataclasses import dataclass, field

CONTEXT_LABELS = {
    "lecturer_content": "Lecturer-approved DIT learning content",
    "dit_curriculum": "DIT curriculum information",
    "general_academic": "General academic knowledge — related to your modules but not found in DIT materials",
    "outside_curriculum": "General information — not identified as part of your current DIT curriculum.",
}

PROMPT_GUIDANCE = {
    "lecturer_content": (
        "The question is covered by lecturer-approved DIT material. Base the answer on the "
        "retrieved lecturer excerpts first and cite them."
    ),
    "dit_curriculum": (
        "The question matches the student's structured DIT curriculum (module records or the "
        "published prospectus) but no lecturer resource covers it in detail. You may state what "
        "the curriculum records say, then teach the concept with general knowledge, clearly "
        "separating the two."
    ),
    "general_academic": (
        "The question relates to the student's modules but is not covered by DIT material. "
        "Answer helpfully with general academic knowledge and say plainly that it was not found "
        "in the student's DIT lecturer materials."
    ),
    "outside_curriculum": (
        "The question was NOT identified as part of the student's current DIT curriculum. Still "
        "answer it helpfully as general educational information, but do not present it as an "
        "official DIT curriculum topic, and do not link it to a DIT module unless the student did."
    ),
}


@dataclass
class CurriculumContext:
    student_id: int | None = None
    department: str | None = None
    programme: str | None = None
    level: str | None = None
    semester: str | None = None
    academic_year: str | None = None
    modules: list = field(default_factory=list)
    modules_from_registration: bool = False
    selected_module: object = None
    curriculum_versions: list = field(default_factory=list)
    source_label: str | None = None
    missing: list = field(default_factory=list)

    @property
    def is_known(self):
        return bool(self.programme and self.modules)

    def summary(self):
        """JSON-safe summary for API responses and the UI."""
        return {
            "programme": self.programme,
            "level": self.level,
            "semester": self.semester,
            "academic_year": self.academic_year,
            "module": self.selected_module.name if self.selected_module is not None else None,
            "module_code": getattr(self.selected_module, "code", None),
            "curriculum_versions": self.curriculum_versions,
            "source": self.source_label,
            "missing": self.missing,
        }


def build_context(student=None, module=None):
    """Assemble curriculum context; never raises (missing data is reported)."""
    import academic_context

    ctx = CurriculumContext(selected_module=module)
    if student is not None and getattr(student, "is_student", False):
        details = academic_context.describe_context(student)
        ctx.student_id = student.id
        ctx.department = details["department"].name if details["department"] else None
        ctx.programme = details["programme"].name if details["programme"] else None
        ctx.level = details["level"].display_label if details["level"] else None
        ctx.semester = details["semester"].label if details["semester"] else None
        ctx.academic_year = details["academic_year"].label if details["academic_year"] else None
        ctx.modules = list(details["modules"])
        ctx.modules_from_registration = details["modules_from_registration"]
        ctx.curriculum_versions = details["curriculum_versions"]
        ctx.source_label = details["source_label"]
        ctx.missing = details["missing"]
    if module is not None:
        if module not in ctx.modules:
            ctx.modules.append(module)
        semester = module.semester
        level = semester.nta_level if semester else None
        programme = level.programme if level else None
        ctx.programme = ctx.programme or (programme.name if programme else None)
        ctx.department = ctx.department or (programme.department.name if programme else None)
        ctx.level = ctx.level or (level.display_label if level else None)
        ctx.semester = ctx.semester or (semester.label if semester else None)
    return ctx


def module_profile_text(module):
    """Structured curriculum facts for one module, as plain text."""
    from models import Topic

    lines = [f"Module: {module.name}" + (f" ({module.code})" if module.code else "")]
    semester = module.semester
    if semester is not None:
        level = semester.nta_level
        lines.append(f"Placement: {level.programme.name} · {level.display_label} · {semester.label}")
    if module.academic_year is not None:
        lines.append(f"Academic year: {module.academic_year.label}")
    lines.append(f"Type: {module.type_label}")
    if module.credits_label:
        lines.append(f"Credits: {module.credits_label}")
    if module.description:
        lines.append(f"Description: {module.description}")
    prerequisites = module.prerequisites
    if prerequisites:
        lines.append("Prerequisites: " + ", ".join(
            f"{p.name}" + (f" ({p.code})" if p.code else "") for p in prerequisites))
    topics = Topic.query.filter_by(module_id=module.id, is_published=True).order_by(Topic.display_order, Topic.id).all()
    for topic in topics:
        outcome = f" — {topic.learning_outcome}" if topic.learning_outcome else ""
        lines.append(f"Topic: {topic.title}{outcome}")
    if module.provenance == "legacy_seed":
        lines.append("Note: this module record has not yet been verified against the official DIT prospectus.")
    return "\n".join(lines)


def module_search_text(module):
    """Only curriculum content (no field labels), so words such as "module",
    "year" or "credits" in a question do not count as curriculum matches."""
    from models import Topic

    parts = [module.name, module.code or "", module.description or ""]
    for topic in Topic.query.filter_by(module_id=module.id, is_published=True):
        parts.extend([topic.title, topic.learning_outcome or "", topic.description or ""])
    return " ".join(parts)


def retrieve_curriculum(query_tokens, ctx, tokenize):
    """Score structured module records against the query (priority 2)."""
    matches = []
    query = set(query_tokens)
    if not query:
        return matches
    for module in ctx.modules:
        text = module_profile_text(module)
        tokens = set(tokenize(module_search_text(module)))
        name_tokens = set(tokenize(f"{module.name} {module.code or ''}"))
        overlap = query & tokens
        if not overlap:
            continue
        score = len(overlap) + 1.5 * len(query & name_tokens)
        if ctx.selected_module is not None and module.id == ctx.selected_module.id:
            score *= 1.2
        matches.append({"kind": "curriculum", "module": module, "text": text,
                        "score": round(score, 2), "terms": len(overlap)})
    matches.sort(key=lambda item: item["score"], reverse=True)
    return matches[:3]


def retrieve_prospectus(query_tokens, ctx, tokenize, top_k=2):
    """Search published prospectus text (priority 3). Drafts are never used."""
    from models import CurriculumVersion, ProspectusChunk

    query = set(query_tokens)
    if not query:
        return []
    documents = {v.prospectus_document_id for v in CurriculumVersion.query.filter(
        CurriculumVersion.status == "published", CurriculumVersion.prospectus_document_id.isnot(None))}
    if not documents:
        return []
    scored = []
    for chunk in ProspectusChunk.query.filter(ProspectusChunk.document_id.in_(documents)).limit(2000):
        overlap = query & set(tokenize(chunk.content))
        if len(overlap) >= 2:
            scored.append({"kind": "prospectus", "chunk": chunk, "text": chunk.content,
                           "title": chunk.document.title, "score": float(len(overlap)), "terms": len(overlap)})
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:top_k]


def curriculum_vocabulary(ctx, tokenize):
    words = set()
    for module in ctx.modules:
        words |= set(tokenize(module_search_text(module)))
    return words


def classify(query_tokens, ctx, retrieved, tokenize):
    """Return one of CONTEXT_LABELS keys. Pure function of the evidence."""
    if retrieved.get("has_good_match"):
        return "lecturer_content"
    curriculum = retrieved.get("curriculum_matches") or []
    prospectus = retrieved.get("prospectus_matches") or []
    if curriculum and (curriculum[0]["terms"] >= 2 or curriculum[0]["score"] >= 2.5):
        return "dit_curriculum"
    if prospectus and prospectus[0]["terms"] >= 3:
        return "dit_curriculum"
    if not ctx.modules:
        return "outside_curriculum"
    vocabulary = curriculum_vocabulary(ctx, tokenize)
    if set(query_tokens) & vocabulary:
        return "general_academic"
    return "outside_curriculum"
