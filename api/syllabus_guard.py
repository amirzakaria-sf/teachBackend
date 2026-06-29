from __future__ import annotations

from django.conf import settings

from api.models import Lecture, SyllabusDocument


def build_syllabus_guardrail_for_lecture(lecture: Lecture) -> str:
    """Build a full-text guardrail block from active syllabus docs.

    User-selected strategy: full syllabus text is injected directly into system prompt.
    """

    subject = lecture.classroom.subject
    docs = list(
        SyllabusDocument.objects.filter(
            organization=lecture.classroom.organization,
            subject=subject,
            processing_status=SyllabusDocument.ProcessingStatus.READY,
        )
        .only("title", "extracted_text", "text_token_count")
        .order_by("-created_at")
    )

    if not docs:
        return (
            "No syllabus document is attached for this subject yet. "
            "Respond conservatively and avoid claiming syllabus alignment."
        )

    max_chars = int(getattr(settings, "SYLLABUS_PROMPT_MAX_CHARS", 200000))
    chunks: list[str] = []
    total_tokens = 0
    running_chars = 0
    for doc in docs:
        text = (doc.extracted_text or "").strip()
        if not text:
            continue
        block = f"[{doc.title}]\n{text}"
        if running_chars + len(block) > max_chars:
            remaining = max_chars - running_chars
            if remaining > 0:
                chunks.append(block[:remaining])
            break
        chunks.append(block)
        running_chars += len(block)
        total_tokens += int(doc.text_token_count or 0)

    syllabus_text = "\n\n---\n\n".join(chunks)
    return (
        f"You are an educational AI tutor for board '{subject.board.name}', grade '{subject.grade.name}', "
        f"subject '{subject.name}'.\n"
        f"Strictly align responses to the official syllabus content below.\n"
        f"You may include at most ~5% extra relevant context for clarity, not beyond that.\n"
        f"If asked beyond syllabus scope, clearly state that it is out of scope.\n"
        f"Syllabus reference (~{total_tokens} tokens):\n\n{syllabus_text}"
    )
