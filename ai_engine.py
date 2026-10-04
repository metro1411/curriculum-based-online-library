"""
ai_engine.py
------------
The DIT AI Learning Assistant.

This module implements:

1. A lightweight, dependency-free Retrieval-Augmented Generation (RAG)
   search over ResourceChunk rows (keyword/TF-IDF-style scoring - no
   embeddings, no vector database; intentionally lightweight and maintainable).
2. Prompt construction that grounds Gemini's answer in retrieved chunks
   and the student's academic context.
3. A Gemini API call via the official `google-genai` SDK.
4. A graceful, honest fallback when GEMINI_API_KEY is not configured, or
   when the API call fails for any reason - the app never breaks and never
   pretends a non-functional feature is AI-powered.

Nothing in this module ever invents DIT curriculum facts, course codes, or
citations: retrieved sources are always real Resource rows already present
in the archive, and the system prompt explicitly forbids fabrication.
"""

import json
import html
import logging
import math
import re
from collections import Counter

import bleach
import markdown as md_lib
from flask import current_app

logger = logging.getLogger("smart_dit_archive.ai_engine")

# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

_client_cache = {}


def is_available():
    """True only if the google-genai package imports AND an API key is set."""
    try:
        import google.genai  # noqa: F401
    except ImportError:
        return False
    api_key = (current_app.config.get("GEMINI_API_KEY") or "").strip()
    return bool(api_key)


def _get_client():
    api_key = (current_app.config.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return None
    if api_key in _client_cache:
        return _client_cache[api_key]
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        _client_cache[api_key] = client
        return client
    except Exception as error:
        logger.exception("Could not initialize the Gemini client.")
        return None


# ---------------------------------------------------------------------------
# Lightweight RAG retrieval
# ---------------------------------------------------------------------------

_STOPWORDS = set("""
a an the is are was were be been being of in on at to for with and or but
if then else than so such this that these those it its as by from into
about over under above below more most some any no not do does did can
could should would may might will shall i you he she we they them his her
their our your my me us what which who whom where when why how
""".split())


def _tokenize(text):
    tokens = []
    for word in re.findall(r"[a-zA-Z0-9]+", (text or "").lower()):
        if len(word) <= 2 or word in _STOPWORDS:
            continue
        # A compact stemmer makes lecturer content such as "controllers" and
        # student wording such as "controller" retrieve one another without
        # adding a heavyweight search dependency.
        for suffix in ("ments", "ment", "tion", "ions", "ing", "ers", "er", "ed", "es", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[:-len(suffix)]
                break
        tokens.append(word)
    return tokens


def retrieve_context(module=None, query="", resource=None, extra_resources=None, top_k=4, context=None):
    """Search lecturer resources, structured curriculum and published prospectus text.

    Lecturer-resource search scope, in order of preference (see
    curriculum_context.py for the full retrieval priority):
      - `resource` (a specific Resource, for "Ask About This Resource" mode)
      - all resources belonging to `module`
      - `extra_resources` and resources of the student's other current
        modules (from `context`), weighted slightly lower

    Returns a dict: {'chunks': [{'resource', 'chunk', 'score'}...],
    'has_good_match': bool, 'curriculum_matches': [...],
    'prospectus_matches': [...], 'retrieval_error': bool}
    """
    try:
        result = _retrieve_resources(module, query, resource, extra_resources, top_k, context)
        if context is not None:
            import curriculum_context
            tokens = _tokenize(query)
            result["curriculum_matches"] = curriculum_context.retrieve_curriculum(tokens, context, _tokenize)
            result["prospectus_matches"] = curriculum_context.retrieve_prospectus(tokens, context, _tokenize)
            result["programme_matches"] = curriculum_context.retrieve_programmes(tokens, _tokenize)
        return result
    except Exception:
        # A retrieval fault must never break the chat; answer as general
        # guidance and let the label say so.
        logger.exception("Curriculum retrieval failed")
        return {"chunks": [], "has_good_match": False, "curriculum_matches": [],
                "prospectus_matches": [], "programme_matches": [], "retrieval_error": True}


def _resource_metadata_text(r):
    topic_title = r.topic.title if getattr(r, "topic", None) is not None else ""
    return " ".join(part for part in (
        r.title, r.description or "", getattr(r, "learning_objectives", None) or "", topic_title,
    ) if part)


def _retrieve_resources(module, query, resource, extra_resources, top_k, context):
    empty = {"chunks": [], "has_good_match": False, "curriculum_matches": [],
             "prospectus_matches": [], "retrieval_error": False}
    query_tokens = _tokenize(query)
    query_token_set = set(query_tokens)

    candidates = []
    weights = {}

    def add(r, weight):
        if r is not None and r.is_verified and r not in candidates:
            candidates.append(r)
            weights[r.id] = weight

    if resource is not None:
        add(resource, 1.0)
    if module is not None:
        for r in module.resources:
            add(r, 1.0)
    for r in extra_resources or []:
        add(r, 0.85)
    if context is not None:
        # Priority 4: the student's other current modules. Without a selected
        # module they are the natural scope, so they are not down-weighted.
        secondary_weight = 0.85 if module is not None else 1.0
        for other in context.modules:
            if module is not None and other.id == module.id:
                continue
            for r in other.resources:
                add(r, secondary_weight)

    if not query_tokens or not candidates:
        return dict(empty)

    all_pairs = [(r, c) for r in candidates for c in r.chunks]
    if not all_pairs:
        return dict(empty)

    # Document-frequency table across the candidate chunk pool, for a
    # simple inverse-document-frequency weighting.
    doc_freq = Counter()
    token_sets = []
    for _, c in all_pairs:
        toks = set(_tokenize(c.content))
        token_sets.append(toks)
        for t in toks:
            doc_freq[t] += 1
    n_docs = len(all_pairs)

    metadata_tokens = {r.id: set(_tokenize(_resource_metadata_text(r))) for r in candidates}
    scored = []
    for (r, c), toks in zip(all_pairs, token_sets):
        score = 0.0
        matched_terms = 0
        for qt in query_tokens:
            if qt in toks:
                idf = math.log((n_docs + 1) / (doc_freq[qt] + 1)) + 1.0
                score += idf
                matched_terms += 1
        if score <= 0:
            continue
        # A clear match in a lecturer resource title, description, learning
        # objectives or topic is a particularly useful signal in a short
        # academic corpus.
        title_matches = len(query_token_set & metadata_tokens[r.id])
        score += title_matches * 1.25
        if title_matches:
            matched_terms += title_matches
        if resource is not None and r.id == resource.id:
            score *= 1.5
        if r.is_verified:
            score *= 1.15
        score *= weights.get(r.id, 1.0)
        scored.append((score, matched_terms, r, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]
    # Require either multiple overlapping query terms, or one unusually
    # strong/rare term, before treating this as a confident, grounded match.
    # A single coincidental shared word in a small corpus especially
    # is not enough evidence to claim archive-grounded relevance.
    has_good_match = bool(top) and top[0][0] >= 1.35 and (top[0][1] >= 2 or top[0][0] >= 3.0)
    top = [(s, r, c) for s, _terms, r, c in top]

    chunks_out = [{"resource": r, "chunk": c, "score": round(s, 2)} for s, r, c in top]
    return {**empty, "chunks": chunks_out, "has_good_match": has_good_match}


def unique_sources(chunks_out, limit=3):
    seen = set()
    sources = []
    for item in chunks_out:
        r = item["resource"]
        if r.id in seen:
            continue
        seen.add(r.id)
        sources.append(r)
        if len(sources) >= limit:
            break
    return sources


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the DIT AI Learning Assistant inside Smart DIT Learning Hub for
students of the Dar es Salaam Institute of Technology (DIT).

Your purpose is to help DIT students understand academic concepts and coursework.

Your primary knowledge source is the academic content stored in the Smart DIT Online \
Archive. You will be given retrieved excerpts from lecturer-provided resources when \
available - treat these as your primary source of truth for DIT-specific content, and \
prefer them over general knowledge whenever they are relevant.

Use the student's department, programme, NTA level, semester and module to keep answers \
relevant and to pitch explanations at the right academic level.

Do not invent DIT curriculum information, module names, course codes, or lecturer names. \
Do not fabricate citations or sources - only ever reference resource titles that were \
actually given to you in the retrieved context below.

If the retrieved context is empty or insufficient to answer confidently, say so plainly. \
When a supplementary web research section is provided, use it only to support the current \
module's academic concepts; never treat it as official DIT curriculum, policy, or lecturer \
material. Prefer authoritative educational, standards-body, or primary technical sources, and \
distinguish supplementary web knowledge from lecturer-provided knowledge.

Adapt the depth and technical level of your explanation to the student's NTA level and the
selected response preference. Be friendly, encouraging and academically precise.

Depending on the requested mode you may provide: simple explanations, technical \
explanations, worked examples, step-by-step solutions, practice questions, quizzes, or \
revision summaries.

For engineering and mathematical questions, explain your method and reasoning clearly,
step by step, showing your working. Use real Unicode mathematical notation directly, such as
√, π, ∑, ∫, ×, ÷, ≤, ≥, ≈, →, ² and ³. Put important equations on their own line, use a⁄b
for readable fractions, define every symbol, keep units visible and always state a final
answer. Never output LaTeX commands, dollar-sign math delimiters, or TeX markup. For programming
questions, provide complete runnable code in a fenced block with the language specified, then
explain the important sections and expected output.

The live DIT prospectus is the official source for programmes, modules and DIT rules. When \
prospectus programme outlines or excerpts are provided, answer questions about modules, \
credits, prerequisites, progression, examinations, fees or other DIT regulations directly \
from them and say they come from the DIT prospectus. If the prospectus content provided does \
not cover the question, say so and suggest asking the department; never fill the gap.

Never claim to represent official DIT policy, official curriculum, or official approval \
unless that is explicitly present in the retrieved context.

Respect the CURRICULUM SCOPE line in each request. Retrieval priority is: lecturer-approved DIT \
resources, then structured DIT curriculum records, then published DIT prospectus text, then other \
approved material, then general knowledge. When the scope is general or outside the curriculum, \
still help the student, but never present the topic as part of their official DIT curriculum. \
The application shows the scope label beside your answer, so do not invent a different label.

Format your answers using polished Markdown (headings, bold text, numbered or bulleted lists,
tables and fenced code blocks) so they are easy to scan in a chat interface. Start directly
with the answer rather than announcing your process. When suitable, use this structure:
Concept overview, Clear explanation, Worked example, Key revision points, Practice questions.
Always distinguish facts drawn from lecturer materials from general supporting knowledge.
Keep answers focused, complete and free of filler.

When lecturer excerpts are used, add their supplied reference number after the supported claim,
for example [1]. Never create a reference number that is absent from the retrieved context.
Do not repeat a separate source list in the answer because the application displays the verified
resource cards beneath it.

When web research is available, make web-derived claims traceable: refer to them as \
supplementary information and do not invent a link, title, or source. The application displays \
the verified web sources returned by the research tool beneath your answer.

Use a clear teaching contract in every response:
- Lead with a one- or two-sentence direct answer before deeper detail.
- Use meaningful headings, short paragraphs, and lists rather than dense blocks of text.
- For calculations, define the symbols, show each substitution, keep units, then add a clearly
  labelled final answer and a brief reasonableness check.
- Use **Final answer** for the result of a calculation and make it visually easy to find.
- For revision, surface the few ideas worth remembering and one common misconception.
- For flashcards, use `**Front:**` and `**Back:**` pairs so each card becomes an interactive
  study card in the interface.
- Write like a calm, capable human tutor: direct, specific, warm, and free of repetitive filler.
- Never output raw LaTeX, TeX commands, or dollar-sign mathematics delimiters."""


STYLE_INSTRUCTIONS = {
    "guided": "Use a balanced, step-by-step tutoring style with enough detail to build confidence.",
    "simple": "Use short sentences, plain language and one idea at a time before adding technical terms.",
    "detailed": "Provide technical depth, assumptions, derivations and precise terminology where useful.",
    "exam": "Prioritise exam-ready definitions, method marks, worked steps and concise revision points.",
}


MODE_INSTRUCTIONS = {
    "explain": (
        "The student wants a clear conceptual explanation of the topic or question below. "
        "Explain the underlying idea clearly, building from fundamentals, at a level "
        "appropriate for the student's NTA level."
    ),
    "solve": (
        "The student has an academic question they want solved. Solve it correctly and "
        "show your full step-by-step method, not just the final answer. Use the headings "
        "**Given**, **Method**, **Working**, **Answer**, and **Check** where they fit."
    ),
    "summarize": (
        "Summarize the key points of the following topic or material concisely, as a "
        "revision aid. Use short bullet points grouped under clear headings."
    ),
    "example": (
        "Provide one or more clear, fully worked examples related to the following topic, "
        "including the reasoning for each step, a final answer, and one common mistake to avoid."
    ),
    "quiz": (
        "Generate a short quiz of 5 questions on the following topic, appropriate for the "
        "student's academic level. After the questions, add a line containing exactly "
        "'---ANSWERS---' on its own, then provide the answer key with brief explanations."
    ),
    "practice": (
        "Generate 5 practice questions for self-study on the following topic, ordered from "
        "easier to harder. After the questions, add a line containing exactly "
        "'---ANSWERS---' on its own, then provide a concise answer key."
    ),
    "flashcards": (
        "Create 8 concise active-recall flashcards for the requested topic. Format every card "
        "as `**Front:** question` followed by `**Back:** answer`. Keep each answer precise, "
        "include equations where useful, and finish with one short study tip."
    ),
    "revision": (
        "Create an exam-ready revision sheet. Use the headings **Core ideas**, **Key equations "
        "or definitions**, **Common mistakes**, and **Self-check**. Prioritise high-value recall "
        "over long prose."
    ),
    "ask_resource": (
        "The student is asking specifically about one uploaded resource (identified below). "
        "Focus your answer on that resource's content using the excerpts provided, and be "
        "explicit when something they ask about is not covered in that resource."
    ),
}

DEFAULT_MODE = "explain"

MODE_LABELS = {
    "explain": "Explain Concept",
    "solve": "Solve Question",
    "summarize": "Summarize Material",
    "example": "Give Example",
    "quiz": "Generate Quiz",
    "practice": "Generate Practice Questions",
    "flashcards": "Create Flashcards",
    "revision": "Revision Sheet",
    "ask_resource": "Ask About This Resource",
}


def _resolve_chain(student=None, module=None):
    """Best-effort description of department/programme/level/semester for the prompt."""
    dept = programme = level = semester = None
    if module is not None:
        semester = module.semester
        level = semester.nta_level if semester else None
        programme = level.programme if level else None
        dept = programme.department if programme else None
    elif student is not None:
        dept = student.department
        programme = student.programme
        level = student.nta_level
        semester = student.semester

    return {
        "department": dept.name if dept else "Not specified",
        "programme": programme.name if programme else "Not specified",
        "level": level.label if level else "Not specified",
        "semester": semester.label if semester else "Not specified",
    }


def _build_context_block(student, module, resource, retrieved, context=None, scope=None):
    import curriculum_context

    if context is not None:
        chain = {
            "department": context.department or "Not specified",
            "programme": context.programme or "Not specified",
            "level": context.level or "Not specified",
            "semester": context.semester or "Not specified",
        }
    else:
        chain = _resolve_chain(student=student, module=module)
    lines = [
        "STUDENT ACADEMIC CONTEXT:",
        f"- Department: {chain['department']}",
        f"- Programme: {chain['programme']}",
        f"- NTA Level: {chain['level']}",
        f"- Semester: {chain['semester']}",
        f"- Module: {module.name if module else 'General study help (no specific module selected)'}",
    ]
    if context is not None:
        if context.academic_year:
            lines.append(f"- Academic year: {context.academic_year}")
        if context.modules:
            label = "Registered modules" if context.modules_from_registration else "Current semester modules"
            lines.append(f"- {label}: " + "; ".join(
                f"{m.name}" + (f" ({m.code})" if m.code else "") for m in context.modules[:12]))
        if context.missing:
            lines.append("- Missing student context: " + ", ".join(context.missing)
                         + " (do not guess these details).")
    if resource is not None:
        lines.append(f"- Resource in focus: \"{resource.title}\" ({resource.type_label})")

    if module is not None:
        # Structured curriculum data (priority 2) for the selected module:
        # placement, credits, prerequisites, topics and learning outcomes.
        lines.append("")
        lines.append("STRUCTURED DIT CURRICULUM RECORD FOR THE SELECTED MODULE:")
        lines.append(curriculum_context.module_profile_text(module))

    lines.append("")
    chunks = retrieved.get("chunks") or []
    if chunks:
        lines.append("RETRIEVED LECTURER-APPROVED CONTEXT (priority 1 - use as your primary source of truth):")
        for i, item in enumerate(chunks, start=1):
            r = item["resource"]
            tag = "DIT Verified" if r.is_verified else "Pending Review"
            lines.append(f"[{i}] Source: \"{r.title}\" ({r.type_label} - {tag} - module: {r.module.name})")
            if getattr(r, "learning_objectives", None):
                lines.append(f"Learning objectives: {r.learning_objectives.strip()}")
            lines.append(item["chunk"].content.strip())
            lines.append("")
    else:
        lines.append(
            "RETRIEVED ARCHIVE CONTEXT: No directly relevant lecturer material was found for this query."
        )

    other_modules = [m for m in (retrieved.get("curriculum_matches") or [])
                     if module is None or m["module"].id != module.id]
    if other_modules:
        lines.append("")
        lines.append("RELATED STRUCTURED CURRICULUM RECORDS (priority 2):")
        for item in other_modules:
            lines.append(item["text"])
            lines.append("")
    programmes = retrieved.get("programme_matches") or []
    if programmes:
        lines.append("")
        lines.append("LIVE DIT PROSPECTUS PROGRAMME OUTLINES (priority 2 - the complete published module list):")
        for item in programmes:
            lines.append(item["text"])
            lines.append("")
    prospectus = retrieved.get("prospectus_matches") or []
    if prospectus:
        lines.append("")
        lines.append("LIVE DIT PROSPECTUS EXCERPTS (priority 3 - official document text, including rules):")
        for i, item in enumerate(prospectus, start=1):
            lines.append(f"[P{i}] {item['title']}: {item['text'].strip()}")

    if scope:
        lines.append("")
        lines.append(f"CURRICULUM SCOPE: {scope} - {curriculum_context.CONTEXT_LABELS[scope]}")
        lines.append(curriculum_context.PROMPT_GUIDANCE[scope])
    return "\n".join(lines)


def _build_current_turn(mode, student, module, resource, retrieved, question, response_style="guided",
                        context=None, scope=None):
    instruction = MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS[DEFAULT_MODE])
    context_block = _build_context_block(student, module, resource, retrieved, context=context, scope=scope)
    return (
        f"MODE: {MODE_LABELS.get(mode, mode)}\n"
        f"RESPONSE PREFERENCE: {STYLE_INSTRUCTIONS.get(response_style, STYLE_INSTRUCTIONS['guided'])}\n"
        f"LEARNER PROFILE: {_learner_profile(student, response_style)}\n"
        f"{instruction}\n\n"
        f"{context_block}\n"
        f"STUDENT QUESTION / REQUEST:\n{question.strip()}"
    )


# ---------------------------------------------------------------------------
# Markdown rendering (sanitized)
# ---------------------------------------------------------------------------

_ALLOWED_TAGS = [
    "p", "br", "strong", "em", "ul", "ol", "li", "h1", "h2", "h3", "h4",
    "blockquote", "code", "pre", "a", "details", "summary", "table",
    "thead", "tbody", "tr", "th", "td", "hr", "span", "sup", "sub", "div",
]
_ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "span": ["class"],
    "div": ["class"],
    "details": ["class"],
    "summary": ["class"],
}

_MATH_REPLACEMENTS = {
    r"\cdot": "×", r"\times": "×", r"\div": "÷", r"\pm": "±",
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥",
    r"\neq": "≠", r"\approx": "≈", r"\lt": "<", r"\gt": ">",
    r"\infty": "∞", r"\theta": "θ", r"\omega": "ω", r"\alpha": "α",
    r"\beta": "β", r"\gamma": "γ", r"\delta": "δ", r"\lambda": "λ",
    r"\mu": "μ", r"\pi": "π", r"\phi": "φ", r"\sigma": "σ",
    r"\Delta": "Δ", r"\Sigma": "Σ", r"\sum": "∑", r"\int": "∫",
    r"\rightarrow": "→", r"\to": "→", r"\Rightarrow": "⇒",
    r"\degree": "°",
}


def _math_html(expression, block=False):
    """Safely format a broad, readable subset of lecturer/AI LaTex.

    This intentionally prioritises legibility over pretending to be a full
    TeX engine. Unsupported commands are converted to plain readable text,
    rather than leaking raw LaTex into a student's answer.
    """
    raw_value = (expression or "").strip()
    matrices = {}

    def render_matrix(match):
        matrix_type = match.group(1)
        rows = [row.strip() for row in re.split(r"\\\\", match.group(2)) if row.strip()]
        if not rows or len(rows) > 12:
            return match.group(0)
        cells = [row.split("&") for row in rows]
        if max((len(row) for row in cells), default=0) > 12:
            return match.group(0)
        token = f"SMARTDITMATRIX{len(matrices)}TOKEN"
        rendered_rows = []
        for row in cells:
            rendered_cells = "".join(
                f'<span class="math-matrix__cell">{_math_html(cell.strip())}</span>'
                for cell in row
            )
            rendered_rows.append(f'<span class="math-matrix__row">{rendered_cells}</span>')
        matrices[token] = (
            f'<span class="math-matrix math-matrix--{matrix_type}" role="math">'
            + "".join(rendered_rows)
            + "</span>"
        )
        return token

    raw_value = re.sub(
        r"\\begin\{(bmatrix|pmatrix|matrix)\}(.+?)\\end\{\1\}",
        render_matrix,
        raw_value,
        flags=re.S,
    )
    value = html.escape(raw_value)
    for source, target in _MATH_REPLACEMENTS.items():
        value = value.replace(source, target)

    # Resolve simple fractions and square roots repeatedly so common nested
    # forms remain readable instead of exposing the original command.
    def read_group(text, start):
        if start >= len(text) or text[start] != "{":
            return None, start
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    return text[start + 1:index], index + 1
        return None, start

    def replace_fractions(text):
        output = []
        index = 0
        while index < len(text):
            match = re.search(r"\\d?frac", text[index:])
            if not match:
                output.append(text[index:])
                break
            command_start = index + match.start()
            command_end = index + match.end()
            numerator, after_numerator = read_group(text, command_end)
            denominator, after_denominator = read_group(text, after_numerator)
            if numerator is None or denominator is None:
                output.append(text[index:command_end])
                index = command_end
                continue
            output.append(text[index:command_start])
            output.append(
                '<span class="fraction"><span>' + numerator + "</span><span>" + denominator + "</span></span>"
            )
            index = after_denominator
        return "".join(output)

    root_pattern = re.compile(r"\\sqrt(?:\[([^\]]+)\])?\{([^{}]+)\}")
    for _ in range(4):
        updated = replace_fractions(value)
        updated = root_pattern.sub(
            lambda match: ("<sup>" + match.group(1) + "</sup>√(" + match.group(2) + ")")
            if match.group(1) else "√(" + match.group(2) + ")", updated
        )
        if updated == value:
            break
        value = updated

    value = re.sub(r"\\(?:text|mathrm|operatorname)\{([^{}]*)\}", r"\1", value)
    script_base = r"([A-Za-z0-9Σ∑∫)])"
    # Handle a base with both scripts before inserting tags, otherwise the
    # first tag would separate the base from its second script.
    value = re.sub(
        script_base + r"_\{([^{}]+)\}\^\{([^{}]+)\}",
        r"\1<sub>\2</sub><sup>\3</sup>",
        value,
    )
    value = re.sub(
        script_base + r"\^\{([^{}]+)\}_\{([^{}]+)\}",
        r"\1<sup>\2</sup><sub>\3</sub>",
        value,
    )
    value = re.sub(script_base + r"\^\{([^{}]+)\}", r"\1<sup>\2</sup>", value)
    value = re.sub(script_base + r"_\{([^{}]+)\}", r"\1<sub>\2</sub>", value)
    value = re.sub(script_base + r"\^([A-Za-z0-9+\-=]+)", r"\1<sup>\2</sup>", value)
    value = re.sub(script_base + r"_([A-Za-z0-9+\-=]+)", r"\1<sub>\2</sub>", value)
    value = value.replace("\\left", "").replace("\\right", "")
    value = value.replace("{", "").replace("}", "")
    value = re.sub(r"\\([A-Za-z]+)", r"\1", value)
    for token, matrix_html in matrices.items():
        value = value.replace(token, matrix_html)
    tag = "div" if block else "span"
    class_name = "math-expression math-expression--block" if block else "math-expression"
    return f'<{tag} class="{class_name}">{value}</{tag}>'


def _replace_math(text):
    def replace_outside_code(section):
        section = re.sub(r"\$\$(.+?)\$\$", lambda m: _math_html(m.group(1), block=True), section, flags=re.S)
        section = re.sub(r"\\\[(.+?)\\\]", lambda m: _math_html(m.group(1), block=True), section, flags=re.S)
        section = re.sub(r"\\\((.+?)\\\)", lambda m: _math_html(m.group(1)), section, flags=re.S)
        section = re.sub(r"(?<!\\)\$([^$\n]+)\$", lambda m: _math_html(m.group(1)), section)
        # If a model omits delimiters around legacy TeX, convert the most
        # common educational forms instead of showing commands to students.
        section = re.sub(
            r"\\begin\{(?:bmatrix|pmatrix|matrix)\}.+?\\end\{(?:bmatrix|pmatrix|matrix)\}",
            lambda m: _math_html(m.group(0), block=True),
            section,
            flags=re.S,
        )
        section = re.sub(
            r"\\(?:d?frac)\{[^{}\n]+\}\{[^{}\n]+\}|\\sqrt(?:\[[^\]]+\])?\{[^{}\n]+\}",
            lambda m: _math_html(m.group(0)),
            section,
        )
        for source, target in _MATH_REPLACEMENTS.items():
            section = section.replace(source, target)
        return section

    parts = re.split(r"(```.*?```)", text or "", flags=re.S)
    return "".join(part if part.startswith("```") else replace_outside_code(part) for part in parts)


def render_markdown(text):
    html = md_lib.markdown(_replace_math(text or ""), extensions=["fenced_code", "tables", "nl2br"])
    return bleach.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True)


def _answer_document(content_html, mode):
    """Wrap sanitized content in a consistent, accessible learning document."""
    safe_mode = mode if mode in MODE_LABELS else DEFAULT_MODE
    mode_label = html.escape(MODE_LABELS[safe_mode])
    return (
        f'<article class="ai-answer-document ai-answer-document--{safe_mode}">'
        '<header class="ai-answer-document__header">'
        '<span class="ai-answer-document__mark" aria-hidden="true">✦</span>'
        '<span class="ai-answer-document__identity">'
        '<small>DIT AI learning response</small>'
        f"<strong>{mode_label}</strong>"
        "</span>"
        '<span class="ai-answer-document__quality">Clear · structured · reviewable</span>'
        "</header>"
        f'<div class="ai-answer-document__content">{content_html}</div>'
        "</article>"
    )


def render_ai_answer(raw_text, mode):
    safe_mode = mode if mode in MODE_LABELS else DEFAULT_MODE
    marker = "---ANSWERS---"
    if mode in ("quiz", "practice") and marker in raw_text:
        before, after = raw_text.split(marker, 1)
        html = render_markdown(before.strip())
        html += (
            '<details class="answer-key">'
            "<summary>Reveal Answer Key</summary>"
            f'<div class="answer-key-body">{render_markdown(after.strip())}</div>'
            "</details>"
        )
        return _answer_document(html, safe_mode)
    if mode == "flashcards":
        pattern = re.compile(
            r"\*\*Front:\*\*\s*(.+?)\s*\n+\s*\*\*Back:\*\*\s*(.+?)"
            r"(?=\n+\s*(?:#{1,4}\s*Card\s*\d+\s*)?\*\*Front:\*\*|\Z)",
            flags=re.S | re.I,
        )
        cards = pattern.findall(raw_text or "")
        if cards:
            rendered = ['<div class="study-flashcard-deck" data-flashcard-deck>']
            for index, (front, back) in enumerate(cards, start=1):
                rendered.append(
                    '<article class="study-flashcard" data-flashcard>'
                    '<button type="button" class="study-flashcard__surface" '
                    'data-flashcard-toggle aria-expanded="false">'
                    '<span class="study-flashcard__counter">'
                    f"Card {index} of {len(cards)}</span>"
                    '<span class="study-flashcard__face study-flashcard__front">'
                    f"{render_markdown(front.strip())}"
                    '<span class="study-flashcard__hint">Select to reveal answer</span></span>'
                    '<span class="study-flashcard__face study-flashcard__back">'
                    f"{render_markdown(back.strip())}"
                    '<span class="study-flashcard__hint">Select to show question</span></span>'
                    "</button></article>"
                )
            rendered.append("</div>")
            return _answer_document("".join(rendered), safe_mode)
    return _answer_document(render_markdown(raw_text), safe_mode)


def suggested_followups(question, mode, module=None):
    """Return short, useful next questions for a ChatGPT-like learning flow."""
    topic = "this topic"
    clean_question = " ".join((question or "").split())
    if clean_question:
        topic = clean_question[:90].rstrip("?.! ")
    module_hint = f" for {module.name}" if module else ""
    if mode in ("solve", "example"):
        return [
            "Show another worked example with different values.",
            "Explain the reasoning behind each step more simply.",
            f"Create an exam-style practice question{module_hint}.",
        ]
    if mode in ("quiz", "practice"):
        return [
            "Give me a harder version of the questions.",
            "Explain the answer to question 1 step by step.",
            f"Summarise the key revision points for {topic}.",
        ]
    if mode in ("flashcards", "revision"):
        return [
            "Turn this into a five-question self-test.",
            "Explain the most difficult idea with a worked example.",
            f"What should I revise next{module_hint}?",
        ]
    return [
        f"Give me a worked example of {topic}.",
        f"What are the most important revision points for {topic}?",
        f"Quiz me on {topic}{module_hint}.",
    ]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

UNAVAILABLE_MESSAGE = (
    "AI assistance will be available when the AI service is connected. Add a Gemini API key "
    "to enable the learning assistant."
)

GENERIC_ERROR_MESSAGE = (
    "The DIT AI Learning Assistant is temporarily unavailable. Please try again in a "
    "moment. If this keeps happening, check that the Gemini API key is valid and that you "
    "have connectivity."
)


def _provider_error_message(error):
    """Return a safe, actionable message without exposing provider internals."""
    detail = str(error).lower()
    if any(token in detail for token in (
        "api key", "api_key", "unauthenticated", "authentication", "invalid credentials",
        "access_token_type_unsupported", "permission denied", "401", "403",
    )):
        return (
            "Gemini rejected the API credentials. Create a Gemini API key in Google AI Studio, "
            "replace GEMINI_API_KEY, and fully restart the app."
        )
    if any(token in detail for token in ("quota", "rate limit", "resource exhausted", "429")):
        return "The Gemini project has reached a quota or rate limit. Please check its billing and quota settings."
    if any(token in detail for token in ("model", "not found", "404")):
        return "The configured Gemini model is unavailable. Set GEMINI_MODEL=gemini-3.5-flash and restart the app."
    if any(token in detail for token in ("connecterror", "connection", "network", "timed out", "10013")):
        return "The app could not reach Gemini. Check the internet connection, firewall, proxy, or VPN and try again."
    return GENERIC_ERROR_MESSAGE


def _learner_profile(student, response_style):
    """Build a compact, non-sensitive learning preference for the current answer."""
    if student is None:
        return "No saved learner preference is available; use the selected response style."
    try:
        from models import StudentPreference
        preference = StudentPreference.query.filter_by(student_id=student.id).first()
    except Exception:
        preference = None
    if preference is None:
        return "Use the selected response style and invite the learner to choose a preferred depth."
    goal = preference.weekly_goal_minutes or 120
    return (
        f"Preferred response style: {response_style}. Weekly study goal: {goal} minutes. "
        "Adapt examples, pace, and revision actions to this learner's academic context."
    )


def _web_grounding_allowed(retrieved, module):
    return bool(
        module is not None
        and not retrieved.get("has_good_match")
        and current_app.config.get("GEMINI_ENABLE_WEB_GROUNDING", True)
        and not current_app.extensions.get("gemini_web_grounding_unavailable")
    )


def _remember_web_grounding_capability_error(error):
    """Avoid a failing web-grounding attempt on every later student request.

    Normal Gemini generation can be available while the configured key lacks
    the separate entitlement required for Google Search grounding. Credential
    and permission failures are stable for the lifetime of an app process, so
    remember those failures and immediately use the archive-first fallback on
    later requests. Transient service and quota errors are deliberately not
    cached, allowing the next request to try again.
    """
    status_code = getattr(error, "code", None) or getattr(error, "status_code", None)
    detail = str(error).lower()
    credential_or_permission_error = (
        status_code in {401, 403}
        or "access_token_type_unsupported" in detail
        or "invalid authentication credentials" in detail
    )
    if credential_or_permission_error:
        current_app.extensions["gemini_web_grounding_unavailable"] = True
        logger.warning(
            "Google Search grounding is unavailable for the configured Gemini key; "
            "using archive-first AI answers for the rest of this app session."
        )


def _field(value, *names):
    if value is None:
        return None
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        result = getattr(value, name, None)
        if result is not None:
            return result
    return None


def _web_sources_from_response(response):
    """Extract public citations supplied by Gemini Google Search grounding metadata."""
    candidates = _field(response, "candidates") or []
    candidate = candidates[0] if candidates else None
    metadata = _field(candidate, "grounding_metadata", "groundingMetadata")
    chunks = _field(metadata, "grounding_chunks", "groundingChunks") or []
    sources, seen_urls = [], set()
    for chunk in chunks:
        web = _field(chunk, "web")
        url = _field(web, "uri", "url")
        title = _field(web, "title")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")) or url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append({"title": (title or "Supplementary web source")[:220], "url": url})
        if len(sources) >= 5:
            break
    return sources


ATTACHMENT_TYPES = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "image/webp": b"RIFF",
    "application/pdf": b"%PDF",
}
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


def read_attachment(payload):
    """Validate a base64 attachment from the chat. Returns (bytes, mime, name) or raises ValueError."""
    import base64
    import binascii

    if not isinstance(payload, dict):
        raise ValueError("The attachment could not be read.")
    mime = (payload.get("mime") or "").lower()
    if mime not in ATTACHMENT_TYPES:
        raise ValueError("Attach a PNG, JPEG or WebP image, or a PDF.")
    try:
        data = base64.b64decode(payload.get("data") or "", validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("The attachment could not be read.") from None
    if not data:
        raise ValueError("The attachment is empty.")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Attachments must be 8 MB or smaller.")
    if not data.startswith(ATTACHMENT_TYPES[mime]) or (mime == "image/webp" and data[8:12] != b"WEBP"):
        raise ValueError("The file contents do not match its type.")
    name = " ".join(str(payload.get("name") or "attachment").split())[:120]
    return data, mime, name


def ask(*, mode, question, module=None, resource=None, history=None, student=None,
        extra_resources=None, response_style="guided", attachment=None):
    """Ask the DIT AI Learning Assistant a question.

    ``attachment`` is an optional (bytes, mime, name) image or PDF from
    read_attachment; Gemini reads it alongside the question. It is never stored.

    Returns a dict with keys: ok, error, answer_text, answer_html, sources
    (list of Resource objects), general_guidance (bool).
    """
    if not is_available():
        return {
            "ok": False,
            "error": UNAVAILABLE_MESSAGE,
            "answer_text": "", "answer_html": "", "sources": [], "web_sources": [],
            "general_guidance": True, "web_grounded": False,
        }

    import curriculum_context

    mode = mode if mode in MODE_INSTRUCTIONS else DEFAULT_MODE
    context = curriculum_context.build_context(student=student, module=module)
    retrieved = retrieve_context(module=module, query=question, resource=resource,
                                  extra_resources=extra_resources, context=context)
    scope = curriculum_context.classify(_tokenize(question), context, retrieved, _tokenize)
    general_guidance = scope != "lecturer_content"
    sources = [] if general_guidance else unique_sources(retrieved["chunks"])

    client = _get_client()
    if client is None:
        return {
            "ok": False,
            "error": UNAVAILABLE_MESSAGE,
            "answer_text": "", "answer_html": "", "sources": [], "web_sources": [],
            "general_guidance": True, "web_grounded": False,
        }

    current_turn_text = _build_current_turn(
        mode, student, module, resource, retrieved, question, response_style=response_style,
        context=context, scope=scope,
    )

    contents = []
    for turn in (history or [])[-12:]:
        role = "model" if turn.get("role") == "assistant" else "user"
        content = (turn.get("content") or "").strip()
        if content:
            contents.append({"role": role, "parts": [{"text": content}]})
    parts = [{"text": current_turn_text}]
    if attachment is not None:
        data, mime, name = attachment
        parts[0]["text"] += f"\n\nATTACHED FILE: {name} ({mime}). Read it carefully and answer about it."
        parts.append({"inline_data": {"mime_type": mime, "data": data}})
    contents.append({"role": "user", "parts": parts})

    web_grounding_requested = _web_grounding_allowed(retrieved, module) and attachment is None
    web_sources = []
    try:
        from google.genai import types
        model_name = current_app.config.get("GEMINI_MODEL", "gemini-3.5-flash")
        thinking_level = "high" if mode == "solve" or response_style in {"detailed", "exam"} else "medium"
        config_options = {
            "system_instruction": SYSTEM_PROMPT,
            "max_output_tokens": current_app.config.get("GEMINI_MAX_OUTPUT_TOKENS", 4096),
            "thinking_config": types.ThinkingConfig(thinking_level=thinking_level),
        }
        if web_grounding_requested:
            config_options["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(**config_options),
        )
        answer_text = (getattr(response, "text", None) or "").strip()
        if not answer_text:
            raise ValueError("Empty response from Gemini")
        web_sources = _web_sources_from_response(response) if web_grounding_requested else []
    except Exception as error:
        if not web_grounding_requested:
            logger.exception("Gemini generate_content call failed")
            return {
                "ok": False,
                "error": _provider_error_message(error),
                "answer_text": "", "answer_html": "", "sources": [], "web_sources": [],
                "general_guidance": True, "web_grounded": False,
            }
        # Google Search grounding can be unavailable for a model, quota, or
        # region. A normal curriculum-aware answer is still preferable to a
        # failed chat request, so retry once without the optional tool.
        _remember_web_grounding_capability_error(error)
        logger.warning("Web grounding failed; retrying Gemini without web search.", exc_info=True)
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    max_output_tokens=current_app.config.get("GEMINI_MAX_OUTPUT_TOKENS", 4096),
                    thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
                ),
            )
            answer_text = (getattr(response, "text", None) or "").strip()
            if not answer_text:
                raise ValueError("Empty fallback response from Gemini")
        except Exception as fallback_error:
            logger.exception("Gemini fallback call failed")
            return {
                "ok": False,
                "error": _provider_error_message(fallback_error),
                "answer_text": "", "answer_html": "", "sources": [], "web_sources": [],
                "general_guidance": True, "web_grounded": False,
            }

    answer_html = render_ai_answer(answer_text, mode)

    return {
        "ok": True,
        "error": None,
        "answer_text": answer_text,
        "answer_html": answer_html,
        "sources": sources,
        "web_sources": web_sources,
        "general_guidance": general_guidance,
        "web_grounded": bool(web_sources),
        "followups": suggested_followups(question, mode, module),
        "context_label": scope,
        "context_label_text": curriculum_context.CONTEXT_LABELS[scope],
        "curriculum_context": context.summary(),
        "curriculum_sources": [
            {"module_id": item["module"].id, "name": item["module"].name, "code": item["module"].code}
            for item in retrieved.get("curriculum_matches") or []
        ],
        "prospectus_sources": [
            {"title": item["title"]} for item in retrieved.get("prospectus_matches") or []
        ],
        "retrieval_error": retrieved.get("retrieval_error", False),
    }


def sources_to_json(sources, web_sources=None):
    archive_sources = [
        {"kind": "archive", "id": r.id, "title": r.title, "type": r.type_label, "verified": r.is_verified}
        for r in sources
    ]
    supplementary_sources = [
        {"kind": "web", "title": item["title"], "url": item["url"], "type": "Supplementary web source"}
        for item in (web_sources or [])
        if item.get("title") and item.get("url")
    ]
    return json.dumps(archive_sources + supplementary_sources)


def sources_from_json(raw):
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def split_sources(raw):
    archive_sources, web_sources = [], []
    for item in sources_from_json(raw):
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "web":
            if item.get("url"):
                web_sources.append(item)
        elif item.get("id"):
            archive_sources.append(item)
    return archive_sources, web_sources
