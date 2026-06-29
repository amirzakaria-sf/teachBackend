from __future__ import annotations

import json
from collections.abc import Iterable

from celery import chain, shared_task
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.utils import timezone

from api.azure_clients import call_gpt_model, transcribe_with_azure_speech, translate_text
from api.code_sanitizer import sanitize_visualizer_code
from api.models import (
    PIPELINE_STEPS,
    FlowChart,
    InteractiveVisualizer,
    Lecture,
    LecturePipelineRun,
    LectureTranslation,
    MindMap,
    Quiz,
    QuizQuestion,
    Summary,
)
from api.rag import build_index, chunk_text
from api.schemas import DiagramResponseSchema, QuizResponseSchema, VisualizerResponseSchema, validate_schema
from api.syllabus_guard import build_syllabus_guardrail_for_lecture


PIPELINE_STAGE_TO_LECTURE_STATUS = {
    LecturePipelineRun.Stage.TRANSCRIBING: Lecture.ProcessingStatus.TRANSCRIBING,
    LecturePipelineRun.Stage.TRANSLATING: Lecture.ProcessingStatus.TRANSLATING,
    LecturePipelineRun.Stage.SUMMARIZING: Lecture.ProcessingStatus.SUMMARIZING,
    LecturePipelineRun.Stage.FLOWCHART: Lecture.ProcessingStatus.FLOWCHART,
    LecturePipelineRun.Stage.MINDMAP: Lecture.ProcessingStatus.MINDMAP,
    LecturePipelineRun.Stage.GENERATING_QUIZ: Lecture.ProcessingStatus.GENERATING_QUIZ,
    LecturePipelineRun.Stage.COMPLETED: Lecture.ProcessingStatus.COMPLETED,
    LecturePipelineRun.Stage.FAILED: Lecture.ProcessingStatus.FAILED,
}


def launch_lecture_pipeline(pipeline_run_id: int):
    return chain(
        transcribe_lecture_task.si(pipeline_run_id),
        translate_lecture_task.s(),
        summarize_lecture_task.s(),
        generate_flowchart_task.s(),
        generate_mindmap_task.s(),
        generate_quiz_task.s(),
        finalize_pipeline_task.s(),
    ).apply_async()


def _update_pipeline_state(
    *,
    pipeline_run_id: int,
    stage: str,
    status: str,
    current_task_id: str = "",
    error_message: str = "",
    metadata_update: dict | None = None,
    finished: bool = False,
):
    pipeline_run = LecturePipelineRun.objects.select_related("lecture").get(pk=pipeline_run_id)
    progress = dict(pipeline_run.progress or {})
    for step in PIPELINE_STEPS:
        progress.setdefault(step, "pending")

    if stage in PIPELINE_STEPS:
        for step in PIPELINE_STEPS:
            if progress[step] == "done":
                continue
            if step == stage:
                progress[step] = "done" if status == LecturePipelineRun.Status.COMPLETED else "processing"
                break
            progress[step] = "done"

    metadata = dict(pipeline_run.metadata or {})
    if metadata_update:
        metadata.update(metadata_update)

    update_fields = [
        "current_stage",
        "status",
        "progress",
        "error_message",
        "metadata",
        "updated_at",
    ]
    pipeline_run.current_stage = stage
    pipeline_run.status = status
    pipeline_run.progress = progress
    pipeline_run.error_message = error_message
    pipeline_run.metadata = metadata
    if current_task_id:
        pipeline_run.current_task_id = current_task_id
        update_fields.append("current_task_id")
    if pipeline_run.started_at is None and status in {LecturePipelineRun.Status.RUNNING, LecturePipelineRun.Status.COMPLETED}:
        pipeline_run.started_at = timezone.now()
        update_fields.append("started_at")
    if finished:
        pipeline_run.finished_at = timezone.now()
        update_fields.append("finished_at")
        if status == LecturePipelineRun.Status.COMPLETED:
            pipeline_run.progress = {step: "done" for step in PIPELINE_STEPS}
    pipeline_run.save(update_fields=update_fields)

    lecture = pipeline_run.lecture
    lecture.processing_status = PIPELINE_STAGE_TO_LECTURE_STATUS.get(stage, lecture.processing_status)
    lecture.processing_error = error_message
    lecture.save(update_fields=["processing_status", "processing_error", "updated_at"])

    channel_layer = get_channel_layer()
    if channel_layer is not None:
        payload = {
            "type": "pipeline.progress",
            "lecture_id": lecture.id,
            "stage": stage,
            "status": status,
            "progress": pipeline_run.progress,
            "error_message": error_message,
        }
        async_to_sync(channel_layer.group_send)(
            f"lecture-pipeline-{lecture.id}",
            {"type": "pipeline.progress", "payload": payload},
        )
    return pipeline_run


def _mark_pipeline_failed(pipeline_run_id: int, stage: str, task_id: str, exc: Exception):
    _update_pipeline_state(
        pipeline_run_id=pipeline_run_id,
        stage=stage,
        status=LecturePipelineRun.Status.FAILED,
        current_task_id=task_id,
        error_message=str(exc),
        finished=True,
    )


def _safe_json_loads(text: str, fallback: dict | list | None = None):
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _extract_json_payload(text: str) -> dict:
    data = _safe_json_loads(text)
    if isinstance(data, dict):
        return data
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        data = _safe_json_loads(text[start : end + 1])
        if isinstance(data, dict):
            return data
    return {}


def _call_structured_chat(*, guardrail: str, task_prompt: str, schema_cls, max_output_tokens: int = 1800, retry_hint: str = "") -> dict:
    attempts = 2
    current_prompt = task_prompt
    for index in range(attempts):
        response = _gpt_chat(guardrail, current_prompt, max_output_tokens=max_output_tokens)
        parsed = _extract_json_payload(response)
        validated = validate_schema(schema_cls, parsed)
        if validated is not None:
            return validated.model_dump()
        if index == 0:
            current_prompt = (
                f"{task_prompt}\n\nIMPORTANT: Your previous response was invalid. "
                f"Return strict valid JSON only. {retry_hint}".strip()
            )
    return {}


def _language_codes_for_translation(lecture: Lecture) -> list[str]:
    languages = list(lecture.classroom.organization.supported_languages or [])
    normalized: list[str] = []
    for language in languages:
        code = str(language or "").strip().lower()
        if code and code not in normalized:
            normalized.append(code)
    if "en" not in normalized:
        normalized.insert(0, "en")
    return normalized


def _gpt_chat(guardrail: str, task_prompt: str, max_output_tokens: int = 1800) -> str:
    return call_gpt_model(
        deployment_name="gpt-5.2-chat",
        system_prompt=guardrail,
        user_prompt=task_prompt,
        max_output_tokens=max_output_tokens,
    )


def _quiz_defaults(lecture: Lecture) -> list[dict]:
    return [
        {
            "question_text": f"What is the core theme of '{lecture.title}'?",
            "options": [lecture.title, "Time management", "Campus navigation", "Sports schedules"],
            "correct_answer": 0,
            "explanation": "The lecture title indicates the primary theme.",
            "difficulty": QuizQuestion.Difficulty.EASY,
            "order": 1,
        },
        {
            "question_text": "Why should students review this lecture summary?",
            "options": [
                "To reinforce key syllabus concepts",
                "To skip the lecture video entirely",
                "To replace all textbook reading",
                "To disable quiz attempts",
            ],
            "correct_answer": 0,
            "explanation": "Summaries support syllabus-aligned revision.",
            "difficulty": QuizQuestion.Difficulty.EASY,
            "order": 2,
        },
    ]


@shared_task(bind=True)
def transcribe_lecture_task(self, pipeline_run_id: int):
    stage = LecturePipelineRun.Stage.TRANSCRIBING
    try:
        _update_pipeline_state(
            pipeline_run_id=pipeline_run_id,
            stage=stage,
            status=LecturePipelineRun.Status.RUNNING,
            current_task_id=self.request.id,
        )
        pipeline_run = LecturePipelineRun.objects.select_related("lecture", "lecture__classroom").get(pk=pipeline_run_id)
        lecture = pipeline_run.lecture
        source_path = ""
        if lecture.video_file:
            try:
                source_path = lecture.video_file.path
            except (AttributeError, NotImplementedError, ValueError):
                source_path = ""

        source_url = lecture.video_url or ""
        if not source_url and lecture.video_file:
            try:
                source_url = lecture.video_file.url
            except Exception:
                source_url = ""
        transcription = transcribe_with_azure_speech(source_path=source_path, source_url=source_url)
        transcript = str(transcription.get("transcript", "")).strip()
        detected_language_code = str(transcription.get("detected_language_code", "en")).strip().lower() or "en"
        detected_locale = str(transcription.get("detected_locale", "en-US")).strip() or "en-US"
        lecture.original_transcript = transcript
        lecture.save(update_fields=["original_transcript", "updated_at"])
        return {
            "pipeline_run_id": pipeline_run_id,
            "lecture_id": lecture.id,
            "transcript": transcript,
            "source_language_code": detected_language_code,
            "source_locale": detected_locale,
        }
    except Exception as exc:
        _mark_pipeline_failed(pipeline_run_id, stage, self.request.id, exc)
        raise


@shared_task(bind=True)
def translate_lecture_task(self, payload: dict):
    pipeline_run_id = payload["pipeline_run_id"]
    stage = LecturePipelineRun.Stage.TRANSLATING
    try:
        _update_pipeline_state(
            pipeline_run_id=pipeline_run_id,
            stage=stage,
            status=LecturePipelineRun.Status.RUNNING,
            current_task_id=self.request.id,
        )
        lecture = Lecture.objects.select_related("classroom", "classroom__organization").get(pk=payload["lecture_id"])
        transcript = payload["transcript"]
        source_language_code = str(payload.get("source_language_code", "en")).strip().lower() or "en"
        canonical_english = transcript
        if source_language_code != "en":
            canonical_english = translate_text(text=transcript, from_lang=source_language_code, to_lang="en")

        translation_objects = []
        for language_code in _language_codes_for_translation(lecture):
            if language_code == "en":
                translated_text = canonical_english
            else:
                translated_text = translate_text(text=canonical_english, from_lang="en", to_lang=language_code)
            translation_objects.append(
                LectureTranslation(
                    lecture=lecture,
                    language_code=language_code,
                    translated_text=translated_text,
                )
            )
        LectureTranslation.objects.filter(lecture=lecture).delete()
        if translation_objects:
            LectureTranslation.objects.bulk_create(translation_objects)
        payload["canonical_transcript"] = canonical_english
        payload["translation_count"] = len(translation_objects)
        return payload
    except Exception as exc:
        _mark_pipeline_failed(pipeline_run_id, stage, self.request.id, exc)
        raise


@shared_task(bind=True)
def summarize_lecture_task(self, payload: dict):
    pipeline_run_id = payload["pipeline_run_id"]
    stage = LecturePipelineRun.Stage.SUMMARIZING
    try:
        _update_pipeline_state(
            pipeline_run_id=pipeline_run_id,
            stage=stage,
            status=LecturePipelineRun.Status.RUNNING,
            current_task_id=self.request.id,
        )
        lecture = Lecture.objects.select_related("classroom", "classroom__subject", "classroom__subject__board", "classroom__subject__grade").get(
            pk=payload["lecture_id"]
        )
        guardrail = build_syllabus_guardrail_for_lecture(lecture)
        summary_prompt = (
            "Generate a concise but complete study summary for the transcript below. "
            "Use syllabus-aligned language. Return plain text only.\n\n"
            f"Transcript:\n{payload.get('canonical_transcript', payload['transcript'])}"
        )
        summary_text = _gpt_chat(guardrail, summary_prompt, max_output_tokens=2000).strip()
        chunks = chunk_text(
            f"{payload.get('canonical_transcript', payload['transcript'])}\n\n{summary_text}\n\n{lecture.whiteboard_notes or ''}"
        )
        try:
            rag_index_path = build_index(chunks, f"{lecture.classroom.organization_id}/lecture-{lecture.id}")
        except Exception:
            rag_index_path = ""
        Summary.objects.update_or_create(
            lecture=lecture,
            defaults={
                "summary_text": summary_text,
                "vector_store_path": rag_index_path,
            },
        )
        payload["summary_text"] = summary_text
        return payload
    except Exception as exc:
        _mark_pipeline_failed(pipeline_run_id, stage, self.request.id, exc)
        raise


@shared_task(bind=True)
def generate_flowchart_task(self, payload: dict):
    pipeline_run_id = payload["pipeline_run_id"]
    stage = LecturePipelineRun.Stage.FLOWCHART
    try:
        _update_pipeline_state(
            pipeline_run_id=pipeline_run_id,
            stage=stage,
            status=LecturePipelineRun.Status.RUNNING,
            current_task_id=self.request.id,
        )
        lecture = Lecture.objects.select_related("classroom", "classroom__subject", "classroom__subject__board", "classroom__subject__grade").get(
            pk=payload["lecture_id"]
        )
        guardrail = build_syllabus_guardrail_for_lecture(lecture)
        prompt = (
            "Create a Mermaid flowchart and node explanations from this lecture summary. "
            "Return strict JSON in this shape: "
            "{\"mermaid_code\": \"...\", \"node_details\": {\"node_id\": \"explanation\"}}."
            f"\n\nSummary:\n{payload['summary_text']}"
        )
        parsed = _call_structured_chat(
            guardrail=guardrail,
            task_prompt=prompt,
            schema_cls=DiagramResponseSchema,
            max_output_tokens=1800,
            retry_hint='Expected keys: mermaid_code (string), node_details (object).',
        )
        mermaid_code = str(parsed.get("mermaid_code", "")).strip() or "flowchart TD\nA[Topic] --> B[Key idea]"
        node_details = parsed.get("node_details") if isinstance(parsed.get("node_details"), dict) else {}
        FlowChart.objects.update_or_create(
            lecture=lecture,
            defaults={"mermaid_code": mermaid_code, "node_details": node_details},
        )
        payload["flowchart_nodes"] = len(node_details)
        return payload
    except Exception as exc:
        _mark_pipeline_failed(pipeline_run_id, stage, self.request.id, exc)
        raise


@shared_task(bind=True)
def generate_mindmap_task(self, payload: dict):
    pipeline_run_id = payload["pipeline_run_id"]
    stage = LecturePipelineRun.Stage.MINDMAP
    try:
        _update_pipeline_state(
            pipeline_run_id=pipeline_run_id,
            stage=stage,
            status=LecturePipelineRun.Status.RUNNING,
            current_task_id=self.request.id,
        )
        lecture = Lecture.objects.select_related("classroom", "classroom__subject", "classroom__subject__board", "classroom__subject__grade").get(
            pk=payload["lecture_id"]
        )
        guardrail = build_syllabus_guardrail_for_lecture(lecture)
        prompt = (
            "Create a Mermaid mindmap and node explanations from this lecture summary. "
            "Return strict JSON in this shape: "
            "{\"mermaid_code\": \"...\", \"node_details\": {\"node_id\": \"explanation\"}}."
            f"\n\nSummary:\n{payload['summary_text']}"
        )
        parsed = _call_structured_chat(
            guardrail=guardrail,
            task_prompt=prompt,
            schema_cls=DiagramResponseSchema,
            max_output_tokens=1800,
            retry_hint='Expected keys: mermaid_code (string), node_details (object).',
        )
        mermaid_code = str(parsed.get("mermaid_code", "")).strip() or "mindmap\n  root((Lecture))\n    Concept"
        node_details = parsed.get("node_details") if isinstance(parsed.get("node_details"), dict) else {}
        MindMap.objects.update_or_create(
            lecture=lecture,
            defaults={"mermaid_code": mermaid_code, "node_details": node_details},
        )
        payload["mindmap_nodes"] = len(node_details)
        return payload
    except Exception as exc:
        _mark_pipeline_failed(pipeline_run_id, stage, self.request.id, exc)
        raise


def _normalize_quiz_questions(items: Iterable[dict], lecture: Lecture) -> list[dict]:
    questions: list[dict] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        options = item.get("options")
        if not isinstance(options, list) or len(options) < 2:
            continue
        correct_answer = item.get("correct_answer", 0)
        try:
            correct_answer = int(correct_answer)
        except (TypeError, ValueError):
            correct_answer = 0
        if correct_answer < 0 or correct_answer >= len(options):
            correct_answer = 0
        difficulty = str(item.get("difficulty", QuizQuestion.Difficulty.MEDIUM)).lower()
        if difficulty not in {
            QuizQuestion.Difficulty.EASY,
            QuizQuestion.Difficulty.MEDIUM,
            QuizQuestion.Difficulty.HARD,
        }:
            difficulty = QuizQuestion.Difficulty.MEDIUM
        questions.append(
            {
                "question_text": str(item.get("question_text", f"Question {index} about {lecture.title}"))[:5000],
                "options": [str(option)[:1000] for option in options[:6]],
                "correct_answer": correct_answer,
                "explanation": str(item.get("explanation", ""))[:3000],
                "difficulty": difficulty,
                "order": int(item.get("order", index)),
            }
        )
    return questions


@shared_task(bind=True)
def generate_quiz_task(self, payload: dict):
    pipeline_run_id = payload["pipeline_run_id"]
    stage = LecturePipelineRun.Stage.GENERATING_QUIZ
    try:
        _update_pipeline_state(
            pipeline_run_id=pipeline_run_id,
            stage=stage,
            status=LecturePipelineRun.Status.RUNNING,
            current_task_id=self.request.id,
        )
        lecture = Lecture.objects.select_related("classroom", "classroom__subject", "classroom__subject__board", "classroom__subject__grade").get(
            pk=payload["lecture_id"]
        )
        guardrail = build_syllabus_guardrail_for_lecture(lecture)
        prompt = (
            "Generate 5 quiz questions from the lecture summary and transcript. "
            "Return strict JSON: {\"questions\": [{\"question_text\":...,\"options\":[...],"
            "\"correct_answer\":0,\"explanation\":...,\"difficulty\":\"easy|medium|hard\",\"order\":1}]}."
            f"\n\nSummary:\n{payload.get('summary_text', '')}\n\nTranscript:\n"
            f"{payload.get('canonical_transcript', payload.get('transcript', ''))}"
        )
        parsed = _call_structured_chat(
            guardrail=guardrail,
            task_prompt=prompt,
            schema_cls=QuizResponseSchema,
            max_output_tokens=2200,
            retry_hint='Expected top-level key: questions (list).',
        )
        candidate_questions = parsed.get("questions") if isinstance(parsed.get("questions"), list) else []
        questions = _normalize_quiz_questions(candidate_questions, lecture)
        if not questions:
            questions = _quiz_defaults(lecture)

        quiz, _ = Quiz.objects.update_or_create(
            lecture=lecture,
            title=f"{lecture.title} Assessment",
            defaults={
                "classroom": lecture.classroom,
                "is_published": False,
                "is_active": True,
            },
        )
        QuizQuestion.objects.filter(quiz=quiz).delete()
        QuizQuestion.objects.bulk_create([QuizQuestion(quiz=quiz, **question) for question in questions])
        payload["quiz_id"] = quiz.id
        payload["question_count"] = len(questions)
        return payload
    except Exception as exc:
        _mark_pipeline_failed(pipeline_run_id, stage, self.request.id, exc)
        raise


@shared_task(bind=True)
def finalize_pipeline_task(self, payload: dict):
    pipeline_run_id = payload["pipeline_run_id"]
    try:
        _update_pipeline_state(
            pipeline_run_id=pipeline_run_id,
            stage=LecturePipelineRun.Stage.COMPLETED,
            status=LecturePipelineRun.Status.COMPLETED,
            current_task_id=self.request.id,
            metadata_update={
                "translation_count": payload.get("translation_count", 0),
                "flowchart_nodes": payload.get("flowchart_nodes", 0),
                "mindmap_nodes": payload.get("mindmap_nodes", 0),
                "quiz_id": payload.get("quiz_id"),
                "question_count": payload.get("question_count", 0),
            },
            finished=True,
        )
        return payload
    except Exception as exc:
        _mark_pipeline_failed(pipeline_run_id, LecturePipelineRun.Stage.FAILED, self.request.id, exc)
        raise


def generate_interactive_visualizer_code(*, guardrail_prompt: str, user_prompt: str) -> dict:
    """Synchronous utility for API view.

    Returns dict with generated code payload for storage/response.
    """

    instruction = (
        "Generate runnable raw frontend visualization code based on the prompt. "
        "Prefer Three.js for 3D scenes, p5.js for 2D simulations, or HTML Canvas JS when suitable. "
        "Return strict JSON with keys: code_type (threejs|p5js|html_canvas), generated_code, metadata."
        f"\n\nUser prompt:\n{user_prompt}"
    )
    text = call_gpt_model(
        deployment_name="gpt-5.2-codex",
        system_prompt=guardrail_prompt,
        user_prompt=instruction,
        max_output_tokens=3000,
    )
    payload = _extract_json_payload(text)
    validated = validate_schema(VisualizerResponseSchema, payload)
    if validated is None:
        retry_instruction = f"{instruction}\n\nIMPORTANT: Return strict valid JSON only with code_type, generated_code, metadata."
        text = call_gpt_model(
            deployment_name="gpt-5.2-codex",
            system_prompt=guardrail_prompt,
            user_prompt=retry_instruction,
            max_output_tokens=3000,
        )
        payload = _extract_json_payload(text)
        validated = validate_schema(VisualizerResponseSchema, payload)
    if validated is not None:
        payload = validated.model_dump()
    code_type = str(payload.get("code_type", InteractiveVisualizer.CodeType.HTML_CANVAS)).strip().lower()
    if code_type not in {
        InteractiveVisualizer.CodeType.THREE_JS,
        InteractiveVisualizer.CodeType.P5_JS,
        InteractiveVisualizer.CodeType.HTML_CANVAS,
    }:
        code_type = InteractiveVisualizer.CodeType.HTML_CANVAS
    generated_code = str(payload.get("generated_code", "")).strip()
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    generated_code, sanitization_metadata = sanitize_visualizer_code(generated_code)
    metadata = {**metadata, **sanitization_metadata}
    if not generated_code:
        generated_code = (
            "<canvas id='viz'></canvas><script>"
            "const c=document.getElementById('viz');const ctx=c.getContext('2d');"
            "c.width=800;c.height=500;ctx.fillStyle='#111';ctx.fillRect(0,0,c.width,c.height);"
            "ctx.fillStyle='#fff';ctx.font='24px sans-serif';ctx.fillText('Visualizer placeholder',220,250);"
            "</script>"
        )
    return {
        "code_type": code_type,
        "generated_code": generated_code,
        "metadata": metadata,
        "model_name": "gpt-5.2-codex",
    }
