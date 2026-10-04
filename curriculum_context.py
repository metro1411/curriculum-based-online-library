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
    2. structured curriculum data (any live module or whole programme outline)
    3. the live DIT prospectus text (rules, regulations, programme descriptions)
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
    """Score live module records against the query (priority 2).

    The student's own modules are searched in full; every other live module
    in the prospectus is searched by name and code, so questions about any
    DIT module can be answered from the record.
    """
    from models import Module

    matches = []
    query = set(query_tokens)
    if not query:
        return matches
    own = {module.id for module in ctx.modules}
    others = Module.query.filter(Module.publication_status == "published", Module.is_active.is_(True),
                                 Module.id.notin_(own or [0])).limit(2000).all()
    for module in list(ctx.modules) + others:
        mine = module.id in own
        name_tokens = set(tokenize(f"{module.name} {module.code or ''}"))
        tokens = set(tokenize(module_search_text(module))) if mine else name_tokens
        overlap = query & tokens
        if not overlap or (not mine and len(query & name_tokens) < min(2, len(name_tokens))):
            continue
        score = len(overlap) + 1.5 * len(query & name_tokens)
        if ctx.selected_module is not None and module.id == ctx.selected_module.id:
            score *= 1.2
        if not mine:
            score *= 0.8
        matches.append({"kind": "curriculum", "module": module, "text": module_profile_text(module),
                        "score": round(score, 2), "terms": len(overlap)})
    matches.sort(key=lambda item: item["score"], reverse=True)
    return matches[:4]


# Words shared by most programme names; they cannot identify one programme.
_GENERIC_PROGRAMME_WORDS = (
    "diploma ordinary bachelor certificate degree programme program engineering technology "
    "science higher basic technician master studies national"
)


def retrieve_programmes(query_tokens, tokenize, limit=2):
    """Whole live programme outlines when a question names a programme."""
    from models import Programme

    query = set(query_tokens)
    if not query:
        return []
    generic = set(tokenize(_GENERIC_PROGRAMME_WORDS))
    scored = []
    for programme in Programme.query.filter_by(is_active=True).all():
        words = set(tokenize(programme.name))
        name = (words - generic) or words
        overlap = query & name
        if not overlap or len(overlap) / len(name) < 0.5:
            continue
        score = len(overlap) / len(name) + 0.25 * len(query & set(tokenize(programme.department.name)))
        scored.append({"kind": "programme", "programme": programme, "score": round(score, 2)})
    scored.sort(key=lambda item: item["score"], reverse=True)
    for item in scored[:limit]:
        item["text"] = programme_outline_text(item["programme"])
    return [item for item in scored[:limit] if item["text"]]


def programme_outline_text(programme, max_modules=80):
    """Every live module of a programme, by level and semester, as plain text."""
    from models import Module, NtaLevel, Semester

    modules = (Module.query.join(Semester).join(NtaLevel)
               .filter(NtaLevel.programme_id == programme.id, Module.publication_status == "published",
                       Module.is_active.is_(True))
               .order_by(NtaLevel.level_number, Semester.semester_number, Module.display_order)
               .limit(max_modules).all())
    if not modules:
        return ""
    lines = [f"Programme: {programme.name} · Department: {programme.department.name}"]
    current = None
    for module in modules:
        semester = module.semester
        heading = f"{semester.nta_level.display_label} · {semester.label}"
        if heading != current:
            lines.append(heading + ":")
            current = heading
        facts = [module.type_label]
        if module.credits_label:
            facts.append(f"{module.credits_label} credits")
        if module.prerequisites:
            facts.append("prerequisites " + ", ".join(p.code or p.name for p in module.prerequisites))
        lines.append(f"- {module.code + ' ' if module.code else ''}{module.name} ({'; '.join(facts)})")
    return "\n".join(lines)


def retrieve_prospectus(query_tokens, ctx, tokenize, top_k=4):
    """Search the live prospectus text (priority 3): rules, regulations and
    programme descriptions as printed. Stopped or replaced uploads are never used."""
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
    if retrieved.get("programme_matches"):
        return "dit_curriculum"
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
