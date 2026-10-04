"""Curriculum-aware retrieval, out-of-curriculum labelling and module context."""

from types import SimpleNamespace

import pytest

import ai_engine
import curriculum_context
from conftest import STUDENT, db
from models import AIMessage, CurriculumVersion, Module, ProspectusChunk, ProspectusDocument, Resource, User


def _student(app):
    return User.query.filter_by(email=STUDENT[0]).one()


def _module(name):
    return Module.query.filter_by(name=name).one()


def _scope(question, student, module=None):
    ctx = curriculum_context.build_context(student=student, module=module)
    retrieved = ai_engine.retrieve_context(module=module, query=question, context=ctx)
    return curriculum_context.classify(ai_engine._tokenize(question), ctx, retrieved, ai_engine._tokenize), retrieved, ctx


def test_lecturer_resources_rank_first_for_curriculum_questions(app, seeded):
    with app.app_context():
        student = _student(app)
        module = _module("Control Engineering")
        scope, retrieved, _ = _scope("Explain closed-loop feedback and the transfer function stability", student, module)
        assert scope == "lecturer_content"
        assert retrieved["has_good_match"]
        assert retrieved["chunks"][0]["resource"].module_id == module.id


def test_general_question_without_module_searches_current_modules(app, seeded):
    with app.app_context():
        scope, retrieved, ctx = _scope("Explain the fetch decode execute cycle of a microprocessor", _student(app))
        assert ctx.modules and scope == "lecturer_content"
        assert retrieved["chunks"][0]["resource"].module.name == "Microprocessor"


def test_structured_curriculum_answers_when_no_resource_covers_it(app, seeded):
    with app.app_context():
        module = _module("Special Electrical Machines")
        assert not [r for r in module.resources if r.is_verified]
        scope, retrieved, _ = _scope("What are specialized electrical machines and how are they constructed?",
                                     _student(app), module)
        assert scope == "dit_curriculum"
        assert retrieved["curriculum_matches"][0]["module"].id == module.id


def test_out_of_curriculum_question_is_labelled_not_refused(app, seeded):
    with app.app_context():
        scope, _, _ = _scope("Who won the football world cup in South Africa?", _student(app))
        assert scope == "outside_curriculum"
        assert curriculum_context.CONTEXT_LABELS[scope] == (
            "General information — not identified as part of your current DIT curriculum.")


def test_general_academic_classification_uses_curriculum_vocabulary():
    tokenize = ai_engine._tokenize
    module = SimpleNamespace(id=1, name="Control Engineering", code=None, description="feedback control systems")
    ctx = curriculum_context.CurriculumContext(programme="DEMO", modules=[module])
    original = curriculum_context.module_search_text
    curriculum_context.module_search_text = lambda m: f"{m.name} {m.description}"
    try:
        empty = {"has_good_match": False, "curriculum_matches": [], "prospectus_matches": []}
        assert curriculum_context.classify(tokenize("history of feedback amplifiers"), ctx, empty, tokenize) == "general_academic"
        assert curriculum_context.classify(tokenize("recipe for chapati"), ctx, empty, tokenize) == "outside_curriculum"
        no_context = curriculum_context.CurriculumContext()
        assert curriculum_context.classify(tokenize("feedback"), no_context, empty, tokenize) == "outside_curriculum"
    finally:
        curriculum_context.module_search_text = original


def test_prompt_carries_student_and_module_context(app, seeded):
    with app.app_context():
        student = _student(app)
        module = _module("Control Engineering")
        scope, retrieved, ctx = _scope("Explain feedback", student, module)
        prompt = ai_engine._build_current_turn("explain", student, module, None, retrieved, "Explain feedback",
                                               context=ctx, scope=scope)
        assert f"Programme: {student.programme.name}" in prompt
        assert f"Semester: {student.semester.label}" in prompt
        assert "STRUCTURED DIT CURRICULUM RECORD FOR THE SELECTED MODULE" in prompt
        assert "Module: Control Engineering" in prompt
        assert "Topic: Feedback and closed-loop response" in prompt
        assert "not yet been verified against the official DIT prospectus" in prompt
        assert f"CURRICULUM SCOPE: {scope}" in prompt
        assert "Current semester modules:" in prompt


def test_learning_objectives_improve_retrieval(app, seeded, lecturer):
    with app.app_context():
        module = _module("Control Engineering")
        lecturer_id = User.query.filter_by(role="lecturer", email="lecturer@dit.ac.tz").one().id
        resource = Resource(module_id=module.id, title="Unit 9 reference link", resource_type="other",
                            external_url="https://example.test/ref", uploaded_by_id=lecturer_id,
                            verification_status="verified",
                            learning_objectives="Sketch a Nyquistplot and interpret encirclements")
        db.session.add(resource)
        db.session.flush()
        from models import ResourceChunk
        db.session.add(ResourceChunk(resource_id=resource.id, chunk_index=0,
                                     content="Unit 9 reference link. Nyquistplot encirclements"))
        db.session.flush()
        retrieved = ai_engine.retrieve_context(module=module, query="How do I read a Nyquistplot encirclements?")
        assert retrieved["chunks"][0]["resource"].id == resource.id
        db.session.rollback()


def test_retrieval_failure_degrades_gracefully(app, seeded, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("index unavailable")
    monkeypatch.setattr(ai_engine, "_retrieve_resources", boom)
    with app.app_context():
        result = ai_engine.retrieve_context(module=None, query="feedback", context=None)
        assert result["retrieval_error"] and result["chunks"] == [] and not result["has_good_match"]


def test_draft_prospectus_text_is_never_retrieved(app, seeded):
    with app.app_context():
        document = ProspectusDocument(title="DEMO draft prospectus", stored_filename="x", original_filename="x.txt",
                                      extraction_status="success", extracted_text="zygomorphic flux capacitor")
        db.session.add(document)
        db.session.flush()
        db.session.add(ProspectusChunk(document_id=document.id, chunk_index=0,
                                       content="DEMO zygomorphic flux capacitor theory module"))
        version = CurriculumVersion(label="DEMO retrieval draft", academic_year_label="2040/2041",
                                    status="draft", prospectus_document_id=document.id, is_demo=True)
        db.session.add(version)
        db.session.flush()
        ctx = curriculum_context.build_context(student=_student(app))
        tokens = ai_engine._tokenize("zygomorphic flux capacitor theory")
        assert curriculum_context.retrieve_prospectus(tokens, ctx, ai_engine._tokenize) == []
        version.status = "published"
        db.session.flush()
        matches = curriculum_context.retrieve_prospectus(tokens, ctx, ai_engine._tokenize)
        assert matches and matches[0]["title"] == "DEMO draft prospectus"
        db.session.rollback()


class _FakeModels:
    def __init__(self):
        self.prompts = []

    def generate_content(self, model, contents, config):
        self.prompts.append(contents[-1]["parts"][0]["text"])
        return SimpleNamespace(text="**Answer** for testing.", candidates=[])


@pytest.fixture
def fake_gemini(monkeypatch):
    fake = SimpleNamespace(models=_FakeModels())
    monkeypatch.setattr(ai_engine, "is_available", lambda: True)
    monkeypatch.setattr(ai_engine, "_get_client", lambda: fake)
    return fake


def test_ai_answer_is_labelled_and_saved(app, student, seeded, fake_gemini):
    with app.app_context():
        module_id = _module("Control Engineering").id
    response = student.post("/ai/ask", json={
        "message": "Explain closed-loop feedback and the transfer function stability", "module_id": module_id,
    })
    data = response.get_json()
    assert response.status_code == 200, data
    assert data["context_label"] == "lecturer_content"
    assert data["curriculum_context"]["module"] == "Control Engineering"
    assert data["sources"]
    off_topic = student.post("/ai/ask", json={"message": "Who won the football world cup in South Africa?"}).get_json()
    assert off_topic["context_label"] == "outside_curriculum"
    assert off_topic["context_label_text"].startswith("General information")
    assert off_topic["general_guidance"] and not off_topic["sources"]
    assert "CURRICULUM SCOPE: outside_curriculum" in fake_gemini.models.prompts[-1]
    with app.app_context():
        saved = db.session.get(AIMessage, off_topic["message_id"])
        assert saved.context_label == "outside_curriculum"
    page = student.get(f"/ai?conversation_id={off_topic['conversation_id']}")
    assert b"not identified as part of your current DIT curriculum" in page.data


def test_api_ask_returns_context_label(app, student, seeded, fake_gemini):
    data = student.post("/api/v1/ai/ask", json={"question": "Explain the fetch decode execute cycle"}).get_json()
    assert data["ok"] and data["context_label"] in curriculum_context.CONTEXT_LABELS
    assert data["curriculum_context"]["programme"]
