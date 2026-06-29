from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.text import slugify


SUPPORTED_LANGUAGE_CODES = [
    "hi",
    "mr",
    "gu",
    "bn",
    "te",
    "ta",
    "ur",
    "kn",
    "ml",
    "pa",
    "es",
    "fr",
    "de",
    "it",
    "ja",
    "zh-cn",
    "ar",
]


def default_supported_languages() -> list[str]:
    return SUPPORTED_LANGUAGE_CODES.copy()


PIPELINE_STEPS = [
    "transcribing",
    "translating",
    "summarizing",
    "flowchart",
    "mindmap",
    "generating_quiz",
]


def default_pipeline_progress() -> dict[str, str]:
    return {step: "pending" for step in PIPELINE_STEPS}


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ActiveModel(TimeStampedModel):
    is_active = models.BooleanField(default=True)

    class Meta:
        abstract = True


class Board(ActiveModel):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name"]),
        ]

    def __str__(self):
        return self.name


class Grade(ActiveModel):
    name = models.CharField(max_length=50)
    numeric_value = models.PositiveIntegerField(unique=True)

    class Meta:
        ordering = ["numeric_value", "name"]
        indexes = [
            models.Index(fields=["numeric_value"]),
            models.Index(fields=["name"]),
        ]

    def __str__(self):
        return self.name


class Subject(ActiveModel):
    board = models.ForeignKey(
        Board,
        on_delete=models.CASCADE,
        related_name="subjects",
    )
    grade = models.ForeignKey(
        Grade,
        on_delete=models.CASCADE,
        related_name="subjects",
    )
    name = models.CharField(max_length=255)

    class Meta:
        ordering = ["board__name", "grade__numeric_value", "name"]
        unique_together = ["board", "grade", "name"]
        indexes = [
            models.Index(fields=["board", "grade"]),
            models.Index(fields=["name"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.board.name} - {self.grade.name})"


class Organization(TimeStampedModel):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    logo_url = models.URLField(max_length=500, blank=True, null=True)
    supported_languages = models.JSONField(default=default_supported_languages)
    boards = models.ManyToManyField(Board, related_name="organizations", blank=True)
    grades = models.ManyToManyField(Grade, related_name="organizations", blank=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["slug"]),
        ]

    def clean(self):
        super().clean()
        invalid_codes = sorted(set(self.supported_languages) - set(SUPPORTED_LANGUAGE_CODES))
        if invalid_codes:
            raise ValidationError(
                {"supported_languages": f"Unsupported language codes: {', '.join(invalid_codes)}"}
            )

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name) or "organization"
            candidate = base_slug
            counter = 1
            while Organization.objects.exclude(pk=self.pk).filter(slug=candidate).exists():
                counter += 1
                candidate = f"{base_slug}-{counter}"
            self.slug = candidate
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("The email field must be set.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        role = extra_fields.get("role")
        organization = extra_fields.get("organization")

        if role in {User.Role.PROFESSOR, User.Role.STUDENT} and organization is None:
            raise ValueError("Professor and student accounts must belong to an organization.")

        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault("role", User.Role.ADMIN)
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("name", "Platform Admin")

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


class User(AbstractUser, TimeStampedModel):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        PROFESSOR = "professor", "Professor"
        STUDENT = "student", "Student"

    username = None

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255)
    role = models.CharField(max_length=20, choices=Role.choices)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="users",
        blank=True,
        null=True,
    )
    is_profile_complete = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name", "role"]

    objects = UserManager()

    class Meta:
        ordering = ["name", "email"]
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["role"]),
            models.Index(fields=["organization", "role"]),
        ]

    def clean(self):
        super().clean()
        if self.role in {self.Role.PROFESSOR, self.Role.STUDENT} and not self.organization_id:
            raise ValidationError({"organization": "Professor and student accounts require an organization."})

    def save(self, *args, **kwargs):
        self.email = self.__class__.objects.normalize_email(self.email)
        if not self.username:
            self.username = None
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.role})"


class UserProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="profile"
    )
    full_name = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=20, blank=True)
    student_identifier = models.CharField(max_length=30, unique=True, null=True, blank=True)
    grade = models.CharField(max_length=50, null=True, blank=True)
    section = models.CharField(max_length=50, null=True, blank=True)
    mapped_teacher = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="mapped_students",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Profile: {self.user.email}"

    @classmethod
    def generate_student_id(cls, organization_id: int) -> str:
        year = str(timezone.now().year)
        prefix = f"STU-{organization_id}-{year}-"
        existing_ids = cls.objects.filter(student_identifier__startswith=prefix).values_list("student_identifier", flat=True)
        max_sequence = 0
        for identifier in existing_ids:
            try:
                max_sequence = max(max_sequence, int(str(identifier).rsplit("-", 1)[1]))
            except (ValueError, IndexError):
                continue
        return f"{prefix}{max_sequence + 1:04d}"


class OTPVerification(models.Model):
    class Purpose(models.TextChoices):
        VERIFY = "verify", "Email Verification"
        RESET = "reset", "Password Reset"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    email = models.EmailField()
    otp_code = models.CharField(max_length=6)
    purpose = models.CharField(max_length=10, choices=Purpose.choices)
    is_used = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["email", "purpose", "is_used"]),
        ]

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(minutes=10)
        super().save(*args, **kwargs)

    @classmethod
    def generate(cls, email: str, purpose: str):
        import random
        cls.objects.filter(email=email, purpose=purpose, is_used=False).update(is_used=True)
        fixed_otp = str(getattr(settings, "FIXED_TEST_OTP", "") or "").strip()
        if getattr(settings, "DEBUG", False) and len(fixed_otp) == 6 and fixed_otp.isdigit():
            otp = fixed_otp
        else:
            otp = str(random.randint(100000, 999999))
        return cls.objects.create(email=email, otp_code=otp, purpose=purpose)

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    @property
    def is_valid(self) -> bool:
        return not self.is_used and not self.is_expired

    def __str__(self):
        return f"OTP for {self.email} ({self.purpose})"


class WhitelistedEmail(models.Model):
    class InviteRole(models.TextChoices):
        TEACHER = "teacher", "Teacher"
        STUDENT = "student", "Student"

    email = models.EmailField()
    role = models.CharField(max_length=10, choices=InviteRole.choices)
    created_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="whitelisted_emails",
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="whitelisted_emails",
    )
    grade = models.CharField(max_length=50, null=True, blank=True)
    section = models.CharField(max_length=50, null=True, blank=True)
    is_used = models.BooleanField(default=False)
    used_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="whitelist_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [["email", "organization"]]
        indexes = [
            models.Index(fields=["organization", "is_used"]),
        ]

    def clean(self):
        super().clean()
        if not self.pk:
            pending = WhitelistedEmail.objects.filter(
                email=self.email, organization=self.organization, is_used=False,
            )
            if pending.exists():
                raise ValidationError({"email": "This email already has a pending invitation in this organization."})

    def consume(self, user: User):
        self.is_used = True
        self.used_by = user
        self.save(update_fields=["is_used", "used_by"])

    def __str__(self):
        return f"{self.email} ({self.role}) — {'used' if self.is_used else 'pending'}"


class AuditLog(TimeStampedModel):
    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="audit_logs",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUCCESS)
    target_email = models.EmailField(blank=True)
    target_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="audit_log_targets",
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "created_at"]),
            models.Index(fields=["action", "created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"{self.action} ({self.status})"


class Classroom(ActiveModel):
    name = models.CharField(max_length=255)
    grade = models.CharField(max_length=50)
    section = models.CharField(max_length=50)
    subject = models.ForeignKey(
        Subject,
        on_delete=models.PROTECT,
        related_name="classrooms",
    )
    invite_code = models.CharField(max_length=20, unique=True, blank=True)
    professor = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="classrooms_teaching",
        limit_choices_to={"role": User.Role.PROFESSOR},
    )
    class_teacher = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="classroom_as_class_teacher",
        limit_choices_to={"role": User.Role.PROFESSOR},
    )
    subject_teachers = models.ManyToManyField(
        User,
        through="ClassroomSubjectTeacher",
        related_name="classrooms_as_subject_teacher",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="classrooms",
    )
    students = models.ManyToManyField(
        User,
        through="ClassroomEnrollment",
        related_name="classrooms_enrolled",
    )

    class Meta:
        ordering = ["grade", "section", "name"]
        indexes = [
            models.Index(fields=["invite_code"]),
            models.Index(fields=["professor"]),
            models.Index(fields=["organization"]),
            models.Index(fields=["subject"]),
        ]

    def clean(self):
        super().clean()
        if self.professor.role != User.Role.PROFESSOR:
            raise ValidationError({"professor": "Classroom professor must have the professor role."})
        if self.professor.organization_id != self.organization_id:
            raise ValidationError({"organization": "Professor and classroom must belong to the same organization."})
        if self.subject_id:
            if not self.organization.boards.filter(pk=self.subject.board_id).exists():
                raise ValidationError({"subject": "Subject board is not enabled for this organization."})
            if not self.organization.grades.filter(pk=self.subject.grade_id).exists():
                raise ValidationError({"subject": "Subject grade is not enabled for this organization."})

    def save(self, *args, **kwargs):
        if not self.invite_code:
            candidate = get_random_string(8).upper()
            while Classroom.objects.filter(invite_code=candidate).exclude(pk=self.pk).exists():
                candidate = get_random_string(8).upper()
            self.invite_code = candidate
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ClassroomEnrollment(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="classroom_enrollments",
        limit_choices_to={"role": User.Role.STUDENT},
    )
    classroom = models.ForeignKey(
        Classroom,
        on_delete=models.CASCADE,
        related_name="enrollments",
    )
    enrolled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-enrolled_at"]
        unique_together = ["user", "classroom"]
        indexes = [
            models.Index(fields=["user", "classroom"]),
        ]

    def clean(self):
        super().clean()
        if self.user.role != User.Role.STUDENT:
            raise ValidationError({"user": "Only students can be enrolled in classrooms."})
        if self.user.organization_id != self.classroom.organization_id:
            raise ValidationError({"classroom": "Student and classroom must belong to the same organization."})

    def __str__(self):
        return f"{self.user} -> {self.classroom}"


class ClassroomSubjectTeacher(models.Model):
    classroom = models.ForeignKey(
        Classroom, on_delete=models.CASCADE, related_name="subject_teacher_assignments",
    )
    teacher = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="subject_teacher_assignments",
        limit_choices_to={"role": User.Role.PROFESSOR},
    )
    subject = models.ForeignKey(
        Subject, on_delete=models.CASCADE, related_name="subject_teacher_assignments",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["subject__name"]
        unique_together = [["classroom", "teacher", "subject"]]
        indexes = [
            models.Index(fields=["classroom", "subject"]),
        ]

    def clean(self):
        super().clean()
        if self.teacher.organization_id != self.classroom.organization_id:
            raise ValidationError({"teacher": "Subject teacher must belong to the same organization as the classroom."})
        subject_org = self.classroom.organization
        if not subject_org.boards.filter(pk=self.subject.board_id).exists():
            raise ValidationError({"subject": "Subject board must be enabled for this organization."})

    def __str__(self):
        return f"{self.teacher.name} teaches {self.subject.name} in {self.classroom.name}"


class Lecture(ActiveModel):
    class ProcessingStatus(models.TextChoices):
        PENDING = "pending", "Pending Upload"
        UPLOADED = "uploaded", "Video Uploaded"
        TRANSCRIBING = "transcribing", "Transcribing Audio"
        TRANSLATING = "translating", "Translating Transcript"
        SUMMARIZING = "summarizing", "Generating Summary"
        FLOWCHART = "flowchart", "Building Flow Chart"
        MINDMAP = "mindmap", "Building Mind Map"
        GENERATING_QUIZ = "generating_quiz", "Generating Quiz Questions"
        COMPLETED = "completed", "Processing Complete"
        FAILED = "failed", "Processing Failed"

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    classroom = models.ForeignKey(
        Classroom,
        on_delete=models.CASCADE,
        related_name="lectures",
    )
    video_file = models.FileField(upload_to="lectures/%Y/%m/", blank=True, null=True)
    video_url = models.URLField(max_length=1000, blank=True, null=True)
    original_transcript = models.TextField(blank=True, null=True)
    whiteboard_notes = models.TextField(blank=True, null=True)
    processing_status = models.CharField(
        max_length=20,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.PENDING,
    )
    processing_error = models.TextField(blank=True, null=True)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="uploaded_lectures",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["classroom", "processing_status"]),
            models.Index(fields=["processing_status"]),
        ]

    def clean(self):
        super().clean()
        if self.uploaded_by_id and self.uploaded_by.organization_id != self.classroom.organization_id:
            raise ValidationError({"uploaded_by": "Uploader and classroom must belong to the same organization."})

    def __str__(self):
        return self.title


class LecturePipelineRun(TimeStampedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class Stage(models.TextChoices):
        QUEUED = "queued", "Queued"
        TRANSCRIBING = "transcribing", "Transcribing"
        TRANSLATING = "translating", "Translating"
        SUMMARIZING = "summarizing", "Summarizing"
        FLOWCHART = "flowchart", "Flowchart"
        MINDMAP = "mindmap", "Mindmap"
        GENERATING_QUIZ = "generating_quiz", "Generating Quiz"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    lecture = models.ForeignKey(
        Lecture,
        on_delete=models.CASCADE,
        related_name="pipeline_runs",
    )
    triggered_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="triggered_pipeline_runs",
        blank=True,
        null=True,
    )
    task_id = models.CharField(max_length=255, unique=True, blank=True)
    current_task_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    current_stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.QUEUED)
    progress = models.JSONField(default=default_pipeline_progress)
    error_message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["lecture", "status"]),
            models.Index(fields=["task_id"]),
        ]

    def save(self, *args, **kwargs):
        if not self.task_id:
            self.task_id = f"pipeline-{uuid4()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Pipeline {self.pk} for lecture {self.lecture_id}"


class LectureTranslation(models.Model):
    lecture = models.ForeignKey(
        Lecture,
        on_delete=models.CASCADE,
        related_name="translations",
    )
    language_code = models.CharField(max_length=10)
    translated_text = models.TextField()
    pdf_url = models.URLField(max_length=1000, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["language_code"]
        unique_together = ["lecture", "language_code"]
        indexes = [
            models.Index(fields=["lecture", "language_code"]),
        ]

    def clean(self):
        super().clean()
        supported_languages = set(self.lecture.classroom.organization.supported_languages)
        if self.language_code not in supported_languages:
            raise ValidationError(
                {"language_code": "Language must be enabled for the lecture organization."}
            )

    def __str__(self):
        return f"{self.lecture.title} [{self.language_code}]"


class Summary(models.Model):
    lecture = models.OneToOneField(
        Lecture,
        on_delete=models.CASCADE,
        related_name="summary",
    )
    summary_text = models.TextField()
    vector_store_path = models.CharField(max_length=500, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Summary: {self.lecture.title}"


class FlowChart(models.Model):
    lecture = models.OneToOneField(
        Lecture,
        on_delete=models.CASCADE,
        related_name="flow_chart",
    )
    mermaid_code = models.TextField()
    node_details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"FlowChart: {self.lecture.title}"


class MindMap(models.Model):
    lecture = models.OneToOneField(
        Lecture,
        on_delete=models.CASCADE,
        related_name="mind_map",
    )
    mermaid_code = models.TextField()
    node_details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"MindMap: {self.lecture.title}"


class SyllabusDocument(TimeStampedModel):
    class FileType(models.TextChoices):
        PDF = "pdf", "PDF"
        TXT = "txt", "TXT"

    class ProcessingStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="syllabus_documents",
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="syllabus_documents",
    )
    title = models.CharField(max_length=500)
    file = models.FileField(upload_to="syllabus/%Y/%m/")
    file_type = models.CharField(max_length=10, choices=FileType.choices)
    extracted_text = models.TextField(blank=True)
    text_token_count = models.PositiveIntegerField(default=0)
    processing_status = models.CharField(
        max_length=20,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.PENDING,
    )
    processing_error = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="uploaded_syllabus_documents",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "subject"]),
            models.Index(fields=["processing_status"]),
        ]

    def clean(self):
        super().clean()
        if self.subject_id:
            if not self.organization.boards.filter(pk=self.subject.board_id).exists():
                raise ValidationError({"subject": "Subject board is not enabled for this organization."})
            if not self.organization.grades.filter(pk=self.subject.grade_id).exists():
                raise ValidationError({"subject": "Subject grade is not enabled for this organization."})

    def __str__(self):
        return f"{self.organization.name} - {self.subject.name}: {self.title}"


class InteractiveVisualizer(TimeStampedModel):
    class CodeType(models.TextChoices):
        THREE_JS = "threejs", "Three.js"
        P5_JS = "p5js", "p5.js"
        HTML_CANVAS = "html_canvas", "HTML Canvas"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="interactive_visualizers",
    )
    lecture = models.ForeignKey(
        Lecture,
        on_delete=models.SET_NULL,
        related_name="interactive_visualizers",
        blank=True,
        null=True,
    )
    requested_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="interactive_visualizer_requests",
    )
    prompt = models.TextField()
    generated_code = models.TextField()
    code_type = models.CharField(max_length=20, choices=CodeType.choices, default=CodeType.HTML_CANVAS)
    model_name = models.CharField(max_length=120, default="gpt-5.2-codex")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "created_at"]),
            models.Index(fields=["requested_by", "created_at"]),
        ]

    def clean(self):
        super().clean()
        if self.requested_by.organization_id != self.organization_id:
            raise ValidationError({"requested_by": "Requester and organization must match."})
        if self.lecture_id and self.lecture.classroom.organization_id != self.organization_id:
            raise ValidationError({"lecture": "Lecture and organization must match."})


class Quiz(ActiveModel):
    title = models.CharField(max_length=255)
    lecture = models.ForeignKey(
        Lecture,
        on_delete=models.CASCADE,
        related_name="quizzes",
    )
    classroom = models.ForeignKey(
        Classroom,
        on_delete=models.CASCADE,
        related_name="quizzes",
    )
    is_published = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["lecture"]),
            models.Index(fields=["classroom", "is_published"]),
        ]

    def clean(self):
        super().clean()
        if self.classroom_id != self.lecture.classroom_id:
            raise ValidationError({"classroom": "Quiz classroom must match the lecture classroom."})

    @property
    def question_count(self):
        annotated_count = getattr(self, "annotated_question_count", None)
        return annotated_count if annotated_count is not None else self.questions.count()

    def __str__(self):
        return self.title


class QuizQuestion(models.Model):
    class Difficulty(models.TextChoices):
        EASY = "easy", "Easy"
        MEDIUM = "medium", "Medium"
        HARD = "hard", "Hard"

    quiz = models.ForeignKey(
        Quiz,
        on_delete=models.CASCADE,
        related_name="questions",
    )
    question_text = models.TextField()
    options = models.JSONField()
    correct_answer = models.IntegerField()
    explanation = models.TextField()
    difficulty = models.CharField(
        max_length=10,
        choices=Difficulty.choices,
        default=Difficulty.MEDIUM,
    )
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]

    def clean(self):
        super().clean()
        if not isinstance(self.options, list) or len(self.options) < 2:
            raise ValidationError({"options": "Options must be a list with at least 2 entries."})
        if self.correct_answer < 0 or self.correct_answer >= len(self.options):
            raise ValidationError({"correct_answer": "Correct answer must be a valid option index."})

    def __str__(self):
        return self.question_text[:80]


class StudentQuizAttempt(models.Model):
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="quiz_attempts",
        limit_choices_to={"role": User.Role.STUDENT},
    )
    quiz = models.ForeignKey(
        Quiz,
        on_delete=models.CASCADE,
        related_name="attempts",
    )
    answers = models.JSONField()
    score = models.FloatField()
    total_questions = models.PositiveIntegerField()
    correct_count = models.PositiveIntegerField()
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-completed_at"]
        unique_together = ["student", "quiz"]
        indexes = [
            models.Index(fields=["student", "quiz"]),
        ]

    def clean(self):
        super().clean()
        if self.student.role != User.Role.STUDENT:
            raise ValidationError({"student": "Only students can submit quiz attempts."})

    def __str__(self):
        return f"{self.student} -> {self.quiz}"


class QuizAttemptAnswer(models.Model):
    attempt = models.ForeignKey(
        StudentQuizAttempt,
        on_delete=models.CASCADE,
        related_name="question_details",
    )
    question = models.ForeignKey(
        QuizQuestion,
        on_delete=models.CASCADE,
        related_name="attempt_answers",
    )
    selected_answer = models.IntegerField(blank=True, null=True)
    is_correct = models.BooleanField()

    class Meta:
        unique_together = ["attempt", "question"]

    def __str__(self):
        return f"Attempt {self.attempt_id} / Question {self.question_id}"


class LectureProgress(models.Model):
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="lecture_progress_records",
        limit_choices_to={"role": User.Role.STUDENT},
    )
    lecture = models.ForeignKey(
        Lecture,
        on_delete=models.CASCADE,
        related_name="progress_records",
    )
    timestamp_seconds = models.PositiveIntegerField(default=0)
    duration_seconds = models.PositiveIntegerField(blank=True, null=True)
    last_accessed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_accessed_at"]
        unique_together = ["student", "lecture"]
        indexes = [
            models.Index(fields=["student", "lecture"]),
            models.Index(fields=["lecture", "last_accessed_at"]),
        ]

    def clean(self):
        super().clean()
        if self.student.role != User.Role.STUDENT:
            raise ValidationError({"student": "Only students can track lecture progress."})
        if not ClassroomEnrollment.objects.filter(
            user_id=self.student_id,
            classroom_id=self.lecture.classroom_id,
        ).exists():
            raise ValidationError({"lecture": "Student must be enrolled in the lecture classroom."})

    def __str__(self):
        return f"{self.student} @ {self.lecture}"
