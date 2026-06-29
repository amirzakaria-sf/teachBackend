from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError


class DiagramResponseSchema(BaseModel):
    mermaid_code: str = Field(min_length=1)
    node_details: dict[str, str] = Field(default_factory=dict)


class QuizQuestionSchema(BaseModel):
    question_text: str
    options: list[str]
    correct_answer: int = 0
    explanation: str = ''
    difficulty: str = 'medium'
    order: int = 1


class QuizResponseSchema(BaseModel):
    questions: list[QuizQuestionSchema]


class VisualizerResponseSchema(BaseModel):
    code_type: str = 'html_canvas'
    generated_code: str = Field(min_length=1)
    metadata: dict = Field(default_factory=dict)


def validate_schema(schema_cls: type[BaseModel], payload: dict) -> BaseModel | None:
    try:
        return schema_cls.model_validate(payload)
    except ValidationError:
        return None
