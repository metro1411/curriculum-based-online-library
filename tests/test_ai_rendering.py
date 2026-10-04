"""AI answer formatting: maths, flashcards, quizzes and provider errors."""

import ai_engine


def test_authentication_failures_give_an_actionable_message():
    message = ai_engine._provider_error_message(Exception("401 UNAUTHENTICATED"))
    assert "credentials" in message.lower()


def test_flashcards_render_as_interactive_cards():
    html = ai_engine.render_ai_answer(
        "**Front:** What is negative feedback?\n**Back:** Returning part of the output to oppose the input.\n\n"
        "**Front:** Write Ohm’s law.\n**Back:** V = I × R",
        "flashcards",
    )
    assert "study-flashcard-deck" in html and "data-flashcard-toggle" in html


def test_latex_becomes_readable_symbols_and_matrices():
    html = ai_engine.render_ai_answer(
        r"""## Worked mathematics

\[
\sum_{i=1}^{n} x_i \leq \sqrt{n}
\]

\[
\begin{bmatrix}1 & 2 \\ 3 & 4\end{bmatrix}
\]

The ratio is \frac{1}{2}.""",
        "solve",
    )
    assert "\\sum" not in html and "\\frac" not in html and "\\begin" not in html
    assert "∑" in html and "≤" in html and "math-matrix" in html
    assert 'class="ai-answer-document__header"' in html
    assert 'class="ai-answer-document__content"' in html
    assert "Solve Question" in html


def test_stray_commands_convert_but_code_is_untouched():
    html = ai_engine.render_ai_answer(
        "The condition is x \\leq y and \\sum values remain bounded.\n\n```python\nliteral = r\"\\sum\"\n```",
        "explain",
    )
    assert "x ≤ y" in html and "∑ values" in html
    assert "literal = r&quot;\\sum&quot;" in html


def test_quiz_answer_key_is_a_disclosure():
    html = ai_engine.render_ai_answer("1. What is 2 + 2?\n\n---ANSWERS---\n\n1. 4", "quiz")
    assert 'class="answer-key"' in html
