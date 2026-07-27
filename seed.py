"""
seed.py
-------
Idempotent learning-data setup: curriculum tree (departments -> programmes ->
NTA levels -> semesters -> modules), access accounts and a starter collection
of learning resources so the hub is useful on first launch.

Safe to call on every startup: everything is looked up before being
created, so re-running never duplicates rows.
"""

import os
import logging
import mimetypes

from flask import current_app

from extensions import db
from models import (
    User, Department, Programme, NtaLevel, Semester, Module, Resource, ResourceChunk,
    LecturerAssignment, Topic,
)
from utils import build_stored_filename
from file_processing import process_resource_text
from config import RESOURCE_UPLOAD_DIR
from storage_backend import upload_local_file

logger = logging.getLogger("smart_dit_archive.seed")

STUDENT_EMAIL = "student@dit.ac.tz"
LECTURER_EMAIL = "lecturer@dit.ac.tz"

MODULE_NAMES = [
    ("Control Engineering",
     "Analysis and design of feedback control systems, system modelling, stability and controller response."),
    ("Data Structures and Algorithms",
     "Fundamental data organization techniques and algorithmic problem-solving methods "
     "used in software development."),
    ("Probability and Statistics",
     "Core probability theory and statistical methods for engineering data analysis and "
     "decision-making."),
    ("Power Utilization",
     "Principles of electrical power demand, utilization factors, tariffs and efficient "
     "use of electrical energy."),
    ("Microprocessor",
     "Architecture, operation and application of microprocessors and microcontrollers in "
     "electrical and electronic systems."),
    ("Technical Writing",
     "Principles of clear, structured technical communication for engineering reports and "
     "documentation."),
    ("Special Electrical Machines",
     "Study of specialized electrical machines beyond standard motors and generators, "
     "including their construction and applications."),
]


def run():
    """Entry point called once at every application startup."""
    lecturer = _seed_users()
    modules_by_name = _seed_curriculum()
    _seed_control_workspace(modules_by_name["Control Engineering"], lecturer)
    _seed_foundation_resources(modules_by_name, lecturer)
    db.session.commit()
    logger.info("Learning data check complete.")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _seed_users():
    student = User.query.filter_by(email=STUDENT_EMAIL).first()
    lecturer = User.query.filter_by(email=LECTURER_EMAIL).first()

    # Curriculum placement is wired up after the curriculum tree exists, so we create
    # bare accounts here first if they don't exist yet.
    if not lecturer:
        lecturer = User(
            full_name="Lecturer Account",
            email=LECTURER_EMAIL,
            username="lecturer.account",
            role="lecturer",
        )
        lecturer.set_password(current_app.config["INITIAL_LECTURER_PASSWORD"])
        db.session.add(lecturer)
    else:
        lecturer.email = LECTURER_EMAIL
        lecturer.username = "lecturer.account"
        lecturer.full_name = lecturer.full_name or "Lecturer Account"

    if not student:
        student = User(
            full_name="Student Account",
            email=STUDENT_EMAIL,
            username="student.account",
            role="student",
        )
        student.set_password(current_app.config["INITIAL_STUDENT_PASSWORD"])
        db.session.add(student)
    else:
        student.email = STUDENT_EMAIL
        student.username = "student.account"
        student.full_name = student.full_name or "Student Account"

    db.session.flush()
    return lecturer


# ---------------------------------------------------------------------------
# Curriculum tree
# ---------------------------------------------------------------------------

def _get_or_create_department(name, description, is_active, order):
    dept = Department.query.filter_by(name=name).first()
    if dept:
        return dept
    from utils import slugify
    dept = Department(
        name=name, slug=slugify(name), description=description,
        is_active=is_active, display_order=order,
    )
    db.session.add(dept)
    db.session.flush()
    return dept


def _get_or_create_programme(department, name, description, is_active, order):
    prog = Programme.query.filter_by(department_id=department.id, name=name).first()
    if prog:
        return prog
    from utils import slugify
    prog = Programme(
        department_id=department.id, name=name, slug=slugify(name),
        description=description, is_active=is_active, display_order=order,
    )
    db.session.add(prog)
    db.session.flush()
    return prog


def _get_or_create_level(programme, level_number, is_active):
    level = NtaLevel.query.filter_by(programme_id=programme.id, level_number=level_number).first()
    if level:
        return level
    level = NtaLevel(programme_id=programme.id, level_number=level_number, is_active=is_active)
    db.session.add(level)
    db.session.flush()
    return level


def _get_or_create_semester(level, semester_number, is_active):
    sem = Semester.query.filter_by(nta_level_id=level.id, semester_number=semester_number).first()
    if sem:
        return sem
    sem = Semester(nta_level_id=level.id, semester_number=semester_number, is_active=is_active)
    db.session.add(sem)
    db.session.flush()
    return sem


def _get_or_create_module(semester, name, description, order):
    mod = Module.query.filter_by(semester_id=semester.id, name=name).first()
    if mod:
        return mod
    mod = Module(
        semester_id=semester.id, name=name, code=None, description=description,
        is_active=True, display_order=order,
    )
    db.session.add(mod)
    db.session.flush()
    return mod


def _seed_curriculum():
    # --- Departments --------------------------------------------------
    ee_dept = _get_or_create_department(
        "Electrical Engineering",
        "Electrical power systems, electronics, renewable energy, biomedical "
        "instrumentation and related technologies.",
        is_active=True, order=1,
    )
    _get_or_create_department(
        "Civil Engineering",
        "Structural, geotechnical, water resources and transportation engineering.",
        is_active=False, order=2,
    )
    _get_or_create_department(
        "Computer Engineering",
        "Computing, software and computer systems engineering.",
        is_active=False, order=3,
    )
    _get_or_create_department(
        "Mechanical Engineering",
        "Mechanical design, thermodynamics, manufacturing and industrial maintenance.",
        is_active=False, order=4,
    )

    # --- Programmes under Electrical Engineering -----------------------
    # Electrical Engineering is the currently published programme pathway;
    # additional programmes can be enabled as their curriculum is approved.
    ee_programme = _get_or_create_programme(
        ee_dept, "Electrical Engineering",
        "Core electrical power, electronics and control engineering programme.",
        is_active=True, order=1,
    )
    _get_or_create_programme(
        ee_dept, "Biomedical Engineering",
        "Biomedical instrumentation and healthcare technology.",
        is_active=False, order=2,
    )
    _get_or_create_programme(
        ee_dept, "Renewable Energies Technology",
        "Solar, wind and other renewable power generation technologies.",
        is_active=False, order=3,
    )

    # --- NTA Levels under the Electrical Engineering programme ---------
    active_level = None
    for level_number in (4, 5, 6, 7, 8, 9):
        level = _get_or_create_level(ee_programme, level_number, is_active=(level_number == 5))
        if level_number == 5:
            active_level = level

    # --- Semesters under NTA Level 5 -----------------------------------
    sem1 = _get_or_create_semester(active_level, 1, is_active=False)
    sem2 = _get_or_create_semester(active_level, 2, is_active=True)

    # --- Modules under Semester 2 ---------------------------------------
    modules_by_name = {}
    for i, (name, description) in enumerate(MODULE_NAMES, start=1):
        modules_by_name[name] = _get_or_create_module(sem2, name, description, i)

    db.session.flush()
    _place_student_account(ee_dept, ee_programme, active_level, sem2)

    return modules_by_name


def _place_student_account(department, programme, level, semester):
    student = User.query.filter_by(email=STUDENT_EMAIL).first()
    if student and not student.department_id:
        student.department_id = department.id
        student.programme_id = programme.id
        student.nta_level_id = level.id
        student.semester_id = semester.id


# ---------------------------------------------------------------------------
# Learning resource content
# ---------------------------------------------------------------------------

def _clean_presentation_text(value):
    """Normalize text before it is stored as a learner-facing resource."""
    if value is None:
        return value
    return str(value)


def _finalize_seed_file(path, stored_filename):
    """Make starter resources durable when a cloud storage backend is enabled."""
    upload_local_file(path, stored_filename, mimetypes.guess_type(path)[0])
    return stored_filename, os.path.getsize(path)

def _write_text_file(original_name, text):
    original_name = _clean_presentation_text(original_name)
    text = _clean_presentation_text(text)
    stored = build_stored_filename(original_name)
    path = os.path.join(RESOURCE_UPLOAD_DIR, stored)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return _finalize_seed_file(path, stored)


def _write_docx_file(original_name, title, body_paragraphs):
    import docx
    doc = docx.Document()
    doc.add_heading(_clean_presentation_text(title), level=1)
    for para in body_paragraphs:
        doc.add_paragraph(_clean_presentation_text(para))
    original_name = _clean_presentation_text(original_name)
    stored = build_stored_filename(original_name)
    path = os.path.join(RESOURCE_UPLOAD_DIR, stored)
    doc.save(path)
    return _finalize_seed_file(path, stored)


def _write_pptx_file(original_name, title, slides):
    """slides: list of (heading, [bullet lines])"""
    from pptx import Presentation
    prs = Presentation()

    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = _clean_presentation_text(title)
    if len(title_slide.placeholders) > 1:
        title_slide.placeholders[1].text = "Smart DIT Learning Hub"

    for heading, bullets in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = _clean_presentation_text(heading)
        tf = slide.placeholders[1].text_frame
        tf.text = _clean_presentation_text(bullets[0])
        for line in bullets[1:]:
            p = tf.add_paragraph()
            p.text = _clean_presentation_text(line)

    stored = build_stored_filename(_clean_presentation_text(original_name))
    path = os.path.join(RESOURCE_UPLOAD_DIR, stored)
    prs.save(path)
    return _finalize_seed_file(path, stored)


def _add_resource(module, title, description, resource_type, lecturer, verified,
                   stored_filename=None, original_filename=None, file_size=None,
                   mime_type=None, external_url=None):
    title = _clean_presentation_text(title)
    description = _clean_presentation_text(description)
    original_filename = _clean_presentation_text(original_filename)
    existing = Resource.query.filter_by(module_id=module.id, title=title).first()
    if existing:
        return existing

    resource = Resource(
        module_id=module.id,
        title=title,
        description=description,
        resource_type=resource_type,
        stored_filename=stored_filename,
        original_filename=original_filename,
        file_size_bytes=file_size,
        mime_type=mime_type,
        external_url=external_url,
        uploaded_by_id=lecturer.id,
        verification_status="verified" if verified else "pending",
    )
    db.session.add(resource)
    db.session.flush()

    if stored_filename and original_filename:
        ext = original_filename.rsplit(".", 1)[-1].lower()
        path = os.path.join(RESOURCE_UPLOAD_DIR, stored_filename)
        chunks, status = process_resource_text(path, ext)
        resource.text_extraction_status = status
        for idx, chunk in enumerate(chunks):
            db.session.add(ResourceChunk(resource_id=resource.id, chunk_index=idx, content=chunk))
    elif external_url:
        resource.text_extraction_status = "not_applicable"
        seed_text = _clean_presentation_text(f"{title}. {description or ''}".strip())
        db.session.add(ResourceChunk(resource_id=resource.id, chunk_index=0, content=seed_text))
    else:
        resource.text_extraction_status = "not_applicable"

    return resource


_DISCLAIMER_MD = "\n\n> **Learning resource.** Prepared for independent study within Smart DIT Learning Hub.\n"
_DISCLAIMER_PLAIN = "\n\nLearning resource. Prepared for independent study within Smart DIT Learning Hub."


def _seed_foundation_resources(modules_by_name, lecturer):
    control = modules_by_name["Control Engineering"]
    dsa = modules_by_name["Data Structures and Algorithms"]
    prob = modules_by_name["Probability and Statistics"]
    power = modules_by_name["Power Utilization"]
    micro = modules_by_name["Microprocessor"]
    techw = modules_by_name["Technical Writing"]
    # "Special Electrical Machines" is intentionally left with zero seeded
    # resources so lecturers can begin publishing module material when ready.

    # --- Control Engineering ----------------------------------------------
    if not Resource.query.filter_by(module_id=control.id).first():
        text = (
            "# Control Engineering: Feedback Systems\n\n"
            "## Learning outcome\nExplain the role of feedback in a control system and analyse "
            "the effect of gain on closed-loop behaviour.\n\n"
            "## Open-loop and closed-loop control\nAn open-loop system acts without measuring "
            "its output. A closed-loop system measures the output, compares it with a reference "
            "value, and uses the error to adjust the plant input. This feedback makes a system "
            "more able to reject disturbances, although poor design can make it oscillate.\n\n"
            "## Core relationship\nFor a negative-feedback system with forward path G(s) and "
            "feedback path H(s), the closed-loop transfer function is T(s) = G(s) / (1 + G(s)H(s)). "
            "The characteristic equation is 1 + G(s)H(s) = 0; its roots determine stability.\n\n"
            "## Worked example\nIf G(s) = 10/(s + 2) and H(s) = 1, then T(s) = "
            "[10/(s+2)] / [1 + 10/(s+2)] = 10/(s+12). The closed-loop pole is -12, so this "
            "first-order model is stable and responds faster than the open-loop pole at -2.\n\n"
            "## Revision points\n- Identify the reference, comparator, controller, plant, sensor and output.\n"
            "- Form the closed-loop transfer function before interpreting the poles.\n"
            "- Stability is a system property; increasing gain can improve response but may reduce stability margin.\n"
        )
        stored, size = _write_text_file("control_engineering_feedback_systems.md", text)
        _add_resource(
            control, "Feedback Systems and Closed-Loop Response",
            "Lecturer-managed notes on feedback systems, transfer functions, stability and a worked example.",
            "notes", lecturer, verified=True, stored_filename=stored,
            original_filename="control_engineering_feedback_systems.md", file_size=size,
            mime_type="text/markdown",
        )


    # --- Data Structures and Algorithms ---------------------------------
    if not Resource.query.filter_by(module_id=dsa.id).first():
        text = (
            "# Arrays and Linked Lists – Learning Notes\n"
            + _DISCLAIMER_MD +
            "\n## Arrays\nAn array is a fixed-size, contiguous block of memory that stores "
            "elements of the same type. Because elements sit next to each other in memory, "
            "any element can be accessed directly using its index in constant time, O(1). "
            "Inserting or deleting an element in the middle of an array is comparatively "
            "expensive, O(n), because later elements must be shifted to keep the array "
            "contiguous.\n"
            "\n## Linked Lists\nA linked list stores elements as separate nodes scattered in "
            "memory, where each node holds its data plus a reference to the next node (and, "
            "in a doubly linked list, the previous node too). Accessing an arbitrary element "
            "requires walking the list from the head, which takes O(n) time, but inserting or "
            "deleting a node - once you already have a reference to the correct position - "
            "takes O(1) time, since only a few references need to change.\n"
            "\n## Choosing Between Them\n"
            "- Use an array when you need fast random access and the collection size is "
            "known or changes infrequently.\n"
            "- Use a linked list when you need frequent insertions or deletions at arbitrary "
            "positions and can tolerate slower access by index.\n"
            "\n## Common Operations Summary\n"
            "| Operation | Array | Linked List |\n|---|---|---|\n"
            "| Access by index | O(1) | O(n) |\n"
            "| Search | O(n) | O(n) |\n"
            "| Insert/Delete at end | O(1) amortized | O(1) with tail pointer |\n"
            "| Insert/Delete at start | O(n) | O(1) |\n"
        )
        stored, size = _write_text_file("arrays_and_linked_lists_learning_notes.md", text)
        _add_resource(
            dsa, "Arrays and Linked Lists – Learning Notes",
            "Learning notes comparing array and linked-list storage, "
            "operations, and time complexity.",
            "notes", lecturer, verified=True,
            stored_filename=stored, original_filename="arrays_and_linked_lists_learning_notes.md",
            file_size=size, mime_type="text/markdown",
        )

        docx_paragraphs = [
            "Learning Resource — Worked Example",
            "Topic: Tracing Bubble Sort",
            "Bubble Sort repeatedly steps through a list, compares each pair of adjacent "
            "elements, and swaps them if they are in the wrong order, until a full pass "
            "makes no swaps.",
            "Initial list: [5, 2, 4, 1]",
            "Pass 1: Compare 5 and 2 -> swap -> [2, 5, 4, 1]. Compare 5 and 4 -> swap -> "
            "[2, 4, 5, 1]. Compare 5 and 1 -> swap -> [2, 4, 1, 5].",
            "Pass 2: Compare 2 and 4 -> no swap. Compare 4 and 1 -> swap -> [2, 1, 4, 5]. "
            "Compare 4 and 5 -> no swap.",
            "Pass 3: Compare 2 and 1 -> swap -> [1, 2, 4, 5]. Remaining comparisons produce "
            "no further swaps.",
            "No swaps occur in the next pass, so the algorithm stops. Final sorted list: "
            "[1, 2, 4, 5].",
            "Complexity: in the worst case, Bubble Sort compares roughly n-squared over 2 "
            "pairs for a list of n elements, giving worst-case and average-case time "
            "complexity of O(n^2). Its best case, when the list is already sorted, is O(n) "
            "if implemented to stop early once a pass makes no swaps.",
            _DISCLAIMER_PLAIN,
        ]
        stored, size = _write_docx_file(
            "sorting_algorithms_worked_example.docx",
            "Sorting Algorithms – Worked Example", docx_paragraphs,
        )
        _add_resource(
            dsa, "Sorting Algorithms – Worked Example",
            "Worked example tracing Bubble Sort step by step, "
            "with a note on time complexity.",
            "worked_example", lecturer, verified=True,
            stored_filename=stored,
            original_filename="sorting_algorithms_worked_example.docx",
            file_size=size,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    # --- Probability and Statistics -------------------------------------
    if not Resource.query.filter_by(module_id=prob.id).first():
        text = (
            "# Introduction to Probability – Learning Notes\n"
            + _DISCLAIMER_MD +
            "\n## Basic Definitions\nProbability measures how likely an event is to occur, "
            "expressed as a number between 0 (impossible) and 1 (certain). For an experiment "
            "with equally likely outcomes, the probability of an event A is P(A) = (number of "
            "outcomes favorable to A) / (total number of possible outcomes).\n"
            "\n## Addition Rule\nFor two events A and B: P(A or B) = P(A) + P(B) - P(A and B). "
            "If A and B cannot happen at the same time (mutually exclusive), P(A and B) = 0, "
            "so P(A or B) = P(A) + P(B).\n"
            "\n## Multiplication Rule\nFor two independent events A and B (the outcome of one "
            "does not affect the other): P(A and B) = P(A) x P(B). If the events are not "
            "independent, P(A and B) = P(A) x P(B given A), the probability of B given that A "
            "has occurred.\n"
            "\n## Worked Example\nA fair six-sided die is rolled once. Let A = 'the result is "
            "even' = {2, 4, 6}, so P(A) = 3/6 = 0.5. Let B = 'the result is greater than 4' = "
            "{5, 6}, so P(B) = 2/6. A and B overlap at {6}, so P(A and B) = 1/6. Using the "
            "addition rule: P(A or B) = 0.5 + 2/6 - 1/6 = 4/6, approximately 0.667.\n"
        )
        stored, size = _write_text_file("introduction_to_probability_learning_notes.md", text)
        _add_resource(
            prob, "Introduction to Probability – Learning Notes",
            "Learning notes covering basic probability definitions, the "
            "addition and multiplication rules, and a worked example.",
            "notes", lecturer, verified=True,
            stored_filename=stored,
            original_filename="introduction_to_probability_learning_notes.md",
            file_size=size, mime_type="text/markdown",
        )

    # --- Power Utilization ------------------------------------------------
    if not Resource.query.filter_by(module_id=power.id).first():
        text = (
            "# Power Utilization Fundamentals – Learning Notes\n"
            + _DISCLAIMER_MD +
            "\n## Key Terms\n"
            "- Connected Load: the sum of the continuous ratings of all electrical "
            "equipment connected to a supply system.\n"
            "- Maximum Demand: the greatest load a consumer actually uses over a given "
            "period, usually lower than the connected load because not everything runs at "
            "full rating simultaneously.\n"
            "- Demand Factor: Maximum Demand divided by Connected Load.\n"
            "- Load Factor: Average Load divided by Maximum Demand, over a given period. A "
            "high load factor means the load is used consistently rather than in short, "
            "high peaks.\n"
            "- Power Factor: the ratio of real power (kW) to apparent power (kVA) in an AC "
            "circuit. A power factor close to 1 means the electrical supply is being used "
            "efficiently.\n"
            "\n## Why Utilization Matters\nElectricity tariffs for industrial and commercial "
            "consumers are frequently based on both energy consumed (kWh) and maximum demand "
            "(kVA or kW), and may include a power-factor penalty.\n"
            "\n## Worked Example\nA workshop has a connected load of 100 kW and a recorded "
            "maximum demand of 60 kW. Demand Factor = 60 / 100 = 0.6. If the average load "
            "over 24 hours is 36 kW, Load Factor = 36 / 60 = 0.6.\n"
        )
        stored, size = _write_text_file("power_utilization_fundamentals_learning_notes.md", text)
        _add_resource(
            power, "Power Utilization Fundamentals – Learning Notes",
            "Learning notes on connected load, maximum demand, demand "
            "factor, load factor and power factor.",
            "notes", lecturer, verified=True,
            stored_filename=stored,
            original_filename="power_utilization_fundamentals_learning_notes.md",
            file_size=size, mime_type="text/markdown",
        )

    # --- Microprocessor -------------------------------------------------
    if not Resource.query.filter_by(module_id=micro.id).first():
        text = (
            "# Microprocessor Architecture – Learning Notes\n"
            + _DISCLAIMER_MD +
            "\n## What is a Microprocessor?\nA microprocessor is a single integrated "
            "circuit that contains the central processing unit (CPU) of a computer or "
            "embedded system. It fetches instructions from memory, decodes them, and "
            "executes them in sequence.\n"
            "\n## Core Internal Components\n"
            "- Arithmetic and Logic Unit (ALU): performs arithmetic operations (addition, "
            "subtraction) and logic operations (AND, OR, NOT, comparisons).\n"
            "- Control Unit (CU): generates timing and control signals that coordinate the "
            "fetch-decode-execute cycle and direct data movement between registers, memory "
            "and the ALU.\n"
            "- Registers: small, extremely fast storage locations inside the processor, "
            "including the Accumulator, the Program Counter (holds the address of the next "
            "instruction), the Instruction Register (holds the instruction being decoded), "
            "and general-purpose registers.\n"
            "- Buses: the address bus carries the memory address being accessed; the data "
            "bus carries the actual data being transferred; the control bus carries "
            "control and status signals.\n"
            "\n## The Fetch-Decode-Execute Cycle\n"
            "1. Fetch: the Control Unit places the address held in the Program Counter "
            "onto the address bus, reads the instruction at that address into the "
            "Instruction Register, and increments the Program Counter.\n"
            "2. Decode: the Control Unit interprets the instruction's opcode to determine "
            "which operation is required and which registers or memory locations are "
            "involved.\n"
            "3. Execute: the ALU or other internal circuitry carries out the operation, for "
            "example adding two register values or moving data between a register and "
            "memory.\n"
            "4. The cycle then repeats for the next instruction.\n"
            "\n## Clock Speed\nA microprocessor's clock signal synchronizes every step of "
            "the fetch-decode-execute cycle. Clock speed, usually measured in megahertz "
            "(MHz) or gigahertz (GHz), indicates how many cycles the processor can perform "
            "per second, though the number of cycles needed per instruction varies by "
            "instruction type and processor architecture.\n"
            "\n## Why This Matters for Electrical Engineering\nMicroprocessors and "
            "microcontrollers are used to monitor sensors, control actuators, and manage "
            "communication in devices ranging from power meters to industrial control "
            "panels, which is why understanding their internal architecture is "
            "foundational for this module.\n"
        )
        stored, size = _write_text_file("microprocessor_architecture_learning_notes.md", text)
        _add_resource(
            micro, "Microprocessor Architecture – Learning Notes",
            "Learning notes covering ALU, control unit, registers, buses "
            "and the fetch-decode-execute cycle.",
            "notes", lecturer, verified=True,
            stored_filename=stored,
            original_filename="microprocessor_architecture_learning_notes.md",
            file_size=size, mime_type="text/markdown",
        )

        docx_paragraphs = [
            "Learning Resource — Practical Guide",
            "Objective: identify and describe the register set of the Intel 8085, a "
            "classic 8-bit microprocessor commonly used for teaching microprocessor "
            "fundamentals.",
            "Accumulator (A): 8-bit register that holds one operand before an "
            "arithmetic/logic operation and the result afterwards.",
            "General-purpose register pairs B-C, D-E, H-L: each pair can be used as two "
            "separate 8-bit registers or combined into one 16-bit register pair (H-L is "
            "often used to hold a 16-bit memory address).",
            "Program Counter (PC): 16-bit register holding the address of the next "
            "instruction to fetch.",
            "Stack Pointer (SP): 16-bit register pointing to the top of the stack in "
            "memory, used for subroutine calls and interrupts.",
            "Flags Register: holds status flags (Sign, Zero, Auxiliary Carry, Parity, "
            "Carry) set or cleared depending on the result of the most recent ALU "
            "operation.",
            "Practical steps: (1) draw the 8085 register block diagram from memory; (2) "
            "for a given instruction sequence, trace how the Accumulator and Flags "
            "register change after each instruction; (3) identify which flag would be set "
            "after subtracting two equal 8-bit numbers (the Zero flag); (4) explain why the "
            "H-L pair is useful for accessing data tables stored in memory.",
            _DISCLAIMER_PLAIN,
        ]
        stored, size = _write_docx_file(
            "8085_register_set_practical_guide.docx",
            "8085 Register Set – Practical Guide", docx_paragraphs,
        )
        _add_resource(
            micro, "8085 Register Set – Practical Guide",
            "Practical guide to the Intel 8085 register set, "
            "with a short hands-on exercise.",
            "practical_guide", lecturer, verified=True,
            stored_filename=stored,
            original_filename="8085_register_set_practical_guide.docx",
            file_size=size,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        slides = [
            ("What is a Microprocessor?", [
                "Single integrated circuit containing the CPU",
                "Fetches, decodes, and executes instructions",
                "Found in computers and embedded systems",
            ]),
            ("Core Components", [
                "ALU - arithmetic and logic",
                "Control Unit - timing and coordination",
                "Registers - fast internal storage",
                "Buses - address, data, control",
            ]),
            ("Fetch-Decode-Execute Cycle", [
                "1. Fetch instruction from memory",
                "2. Decode the opcode",
                "3. Execute the operation",
                "4. Repeat",
            ]),
            ("Why It Matters", [
                "Powers sensors and actuators",
                "Used in industrial control panels",
                "Foundational for embedded systems design",
            ]),
        ]
        stored, size = _write_pptx_file(
            "microprocessor_overview_learning_slides.pptx", "Microprocessor Overview", slides,
        )
        _add_resource(
            micro, "Microprocessor Overview – Learning Slides",
            "Learning slide deck summarizing microprocessor components and "
            "the fetch-decode-execute cycle.",
            "presentation", lecturer, verified=True,
            stored_filename=stored,
            original_filename="microprocessor_overview_learning_slides.pptx",
            file_size=size,
            mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )

        _add_resource(
            micro, "External Reference: Microprocessor (Wikipedia)",
            "External reference link for introductory microprocessor study.",
            "book", lecturer, verified=False,
            external_url="https://en.wikipedia.org/wiki/Microprocessor",
        )

    # --- Technical Writing -------------------------------------------------
    if not Resource.query.filter_by(module_id=techw.id).first():
        text = (
            "# Principles of Technical Writing – Learning Notes\n"
            + _DISCLAIMER_MD +
            "\n## Purpose and Audience\nBefore writing any technical document, identify its "
            "purpose (what should the reader be able to do after reading it?) and its "
            "audience (how much prior technical knowledge can you assume?).\n"
            "\n## Structure\nMost technical documents benefit from a predictable structure: "
            "a short introduction stating purpose and scope, a body organized under clear "
            "headings in a logical sequence, and a conclusion or recommendations section. "
            "Use numbered steps for procedures and bullet points for non-sequential lists.\n"
            "\n## Clarity and Precision\n"
            "- Prefer short, direct sentences over long, complex ones.\n"
            "- Use precise technical terms consistently; do not switch between synonyms "
            "for the same concept.\n"
            "- Support claims with data, diagrams, or references rather than vague "
            "statements.\n"
            "- Define any abbreviation or acronym the first time it is used.\n"
            "\n## Common Pitfalls\n"
            "- Burying the main point in the middle of a paragraph instead of stating it "
            "up front.\n"
            "- Mixing instructions with background explanation in the same step.\n"
            "- Omitting units, tolerances, or version numbers in specifications.\n"
            "\n## Revision Checklist\n"
            "1. Does the document achieve its stated purpose for its intended reader?\n"
            "2. Is every section necessary, and is nothing important missing?\n"
            "3. Are headings, numbering and terminology consistent throughout?\n"
            "4. Would a diagram or table communicate this point more clearly than prose?\n"
        )
        stored, size = _write_text_file("principles_of_technical_writing_learning_notes.md", text)
        _add_resource(
            techw, "Principles of Technical Writing – Learning Notes",
            "Learning notes on structuring, clarity, and revising "
            "engineering technical documents.",
            "notes", lecturer, verified=True,
            stored_filename=stored,
            original_filename="principles_of_technical_writing_learning_notes.md",
            file_size=size, mime_type="text/markdown",
        )


def _seed_control_workspace(control, lecturer):
    """Create the one complete lecturer workspace delivered in this release."""
    if not LecturerAssignment.query.filter_by(lecturer_id=lecturer.id, module_id=control.id).first():
        db.session.add(LecturerAssignment(lecturer_id=lecturer.id, module_id=control.id))

    topics = [
        ("Unit 1", "Control-system foundations", "Identify open-loop and closed-loop system components."),
        ("Unit 2", "Mathematical modelling and transfer functions", "Develop transfer-function models from system descriptions."),
        ("Unit 3", "Feedback and closed-loop response", "Analyse the effect of negative feedback on response."),
        ("Unit 4", "Stability and performance", "Interpret poles, stability and transient-response measures."),
    ]
    for order, (unit, title, outcome) in enumerate(topics, start=1):
        if not Topic.query.filter_by(module_id=control.id, title=title).first():
            db.session.add(Topic(
                module_id=control.id, unit_label=unit, title=title,
                learning_outcome=outcome, display_order=order,
            ))
