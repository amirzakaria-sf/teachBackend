from __future__ import annotations

import re

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Avg, Count, Prefetch, Q
from django.utils.dateparse import parse_date
from django.utils import timezone
from rest_framework import generics, serializers as drf_serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from api.models import (
    AuditLog,
    Board,
    Classroom,
    ClassroomEnrollment,
    ClassroomSubjectTeacher,
    FlowChart,
    Grade,
    InteractiveVisualizer,
    Lecture,
    LecturePipelineRun,
    LectureProgress,
    LectureTranslation,
    MindMap,
    Organization,
    Quiz,
    QuizAttemptAnswer,
    QuizQuestion,
    StudentQuizAttempt,
    Subject,
    SyllabusDocument,
    User,
    UserProfile,
    WhitelistedEmail,
)
from api.permissions import IsAdminRole, IsProfessorRole, IsStudentRole, IsSuperAdminRole
from api.serializers import (
    AuditLogSerializer,
    AssignSubjectTeacherSerializer,
    AvailableTeacherSerializer,
    BoardSerializer,
    BulkWhitelistStudentSerializer,
    ClassroomDetailSerializer,
    ClassroomListSerializer,
    ClassroomSubjectTeacherSerializer,
    ClassroomWriteSerializer,
    CustomTokenObtainPairSerializer,
    EnrollStudentsSerializer,
    GradeSerializer,
    InteractiveVisualizerCreateSerializer,
    InteractiveVisualizerSerializer,
    InviteCodeEnrollmentSerializer,
    LectureChatSerializer,
    LectureDetailSerializer,
    LectureListSerializer,
    LecturePipelineRunSerializer,
    LectureWriteSerializer,
    LogoutSerializer,
    OrganizationSerializer,
    QuizQuestionSerializer,
    QuizSerializer,
    QuizSubmissionSerializer,
    QuizSummarySerializer,
    QuizWriteSerializer,
    SubjectSerializer,
    SyllabusDocumentSerializer,
    StudentLectureDetailSerializer,
    StudentQuizSerializer,
    TrackProgressSerializer,
    UserCreateUpdateSerializer,
    UserProfileSerializer,
    UserSerializer,
    WhitelistStudentSerializer,
    WhitelistTeacherSerializer,
)
from api.content_utils import approximate_token_count, extract_text_from_uploaded_file
from api.azure_clients import call_gpt_model
from api.rag import search_index
from api.syllabus_guard import build_syllabus_guardrail_for_lecture
from api.tasks import generate_interactive_visualizer_code, launch_lecture_pipeline


UserModel = get_user_model()


def create_audit_log(
    *,
    action: str,
    actor: User | None = None,
    organization: Organization | None = None,
    status_value: str = AuditLog.Status.SUCCESS,
    target_email: str = "",
    target_user: User | None = None,
    metadata: dict | None = None,
):
    AuditLog.objects.create(
        organization=organization,
        actor=actor,
        action=action,
        status=status_value,
        target_email=target_email,
        target_user=target_user,
        metadata=metadata or {},
    )


def student_user_queryset():
    return UserModel.objects.filter(role=User.Role.STUDENT, is_active=True).select_related("organization")


def classroom_base_queryset():
    student_prefetch = Prefetch(
        "students",
        queryset=student_user_queryset().only("id", "email", "name", "role", "organization_id"),
    )
    return (
        Classroom.objects.select_related(
            "organization",
            "professor",
            "professor__organization",
            "subject",
            "subject__board",
            "subject__grade",
        )
        .prefetch_related(student_prefetch)
        .annotate(student_count=Count("students", distinct=True))
    )


def quiz_base_queryset(include_questions=True, published_only=False):
    questions_queryset = QuizQuestion.objects.all().order_by("order", "id")
    queryset = (
        Quiz.objects.filter(is_active=True)
        .select_related("lecture", "classroom", "classroom__organization")
        .annotate(annotated_question_count=Count("questions", distinct=True))
    )
    if published_only:
        queryset = queryset.filter(is_published=True)
    if include_questions:
        queryset = queryset.prefetch_related(Prefetch("questions", queryset=questions_queryset))
    return queryset


def lecture_base_queryset(*, published_quizzes_only=False):
    quizzes_queryset = quiz_base_queryset(include_questions=False, published_only=published_quizzes_only)
    latest_run_queryset = LecturePipelineRun.objects.order_by("-created_at")
    return (
        Lecture.objects.filter(is_active=True, classroom__is_active=True)
        .select_related(
            "classroom",
            "classroom__organization",
            "classroom__professor",
            "classroom__subject",
            "classroom__subject__board",
            "classroom__subject__grade",
            "uploaded_by",
            "summary",
            "flow_chart",
            "mind_map",
        )
        .prefetch_related(
            Prefetch("translations", queryset=LectureTranslation.objects.order_by("language_code")),
            Prefetch("quizzes", queryset=quizzes_queryset),
            Prefetch("pipeline_runs", queryset=latest_run_queryset, to_attr="prefetched_pipeline_runs"),
        )
        .annotate(quiz_count=Count("quizzes", filter=Q(quizzes__is_active=True), distinct=True))
    )


def progress_map_for_status(status_value: str):
    steps = ["transcribing", "translating", "summarizing", "flowchart", "mindmap", "generating_quiz"]
    stage_to_status = {
        Lecture.ProcessingStatus.PENDING: None,
        Lecture.ProcessingStatus.UPLOADED: None,
        Lecture.ProcessingStatus.TRANSCRIBING: "transcribing",
        Lecture.ProcessingStatus.TRANSLATING: "translating",
        Lecture.ProcessingStatus.SUMMARIZING: "summarizing",
        Lecture.ProcessingStatus.FLOWCHART: "flowchart",
        Lecture.ProcessingStatus.MINDMAP: "mindmap",
        Lecture.ProcessingStatus.GENERATING_QUIZ: "generating_quiz",
        Lecture.ProcessingStatus.COMPLETED: "completed",
        Lecture.ProcessingStatus.FAILED: "failed",
    }
    current = stage_to_status.get(status_value)
    progress = {step: "pending" for step in steps}

    if status_value == Lecture.ProcessingStatus.COMPLETED:
        return {step: "done" for step in steps}
    if status_value == Lecture.ProcessingStatus.FAILED:
        return progress

    if current is None:
        return progress

    for step in steps:
        if step == current:
            progress[step] = "processing"
            break
        progress[step] = "done"
    return progress


def build_chat_response(lecture: Lecture, message: str):
    summary = getattr(lecture, "summary", None)
    index_path = summary.vector_store_path if summary else ""
    results = search_index(index_path, message, top_k=3) if index_path else []
    if results:
        guardrail = build_syllabus_guardrail_for_lecture(lecture)
        context = "\n\n".join(f"- {item.excerpt}" for item in results)
        prompt = (
            "Answer the student's question using only the retrieved lecture context below. "
            "Be concise and educational.\n\n"
            f"Context:\n{context}\n\nQuestion:\n{message}"
        )
        response = call_gpt_model(
            deployment_name="gpt-5.2-chat",
            system_prompt=guardrail,
            user_prompt=prompt,
            max_output_tokens=700,
        ).strip()
        return {
            "response": response or "I could not generate a grounded answer from the lecture context.",
            "sources": [{"excerpt": item.excerpt, "confidence": round(item.confidence, 2)} for item in results],
        }

    query_terms = {term for term in re.findall(r"\w+", message.lower()) if len(term) > 3}
    candidate_texts = [lecture.original_transcript or "", lecture.whiteboard_notes or "", summary.summary_text if summary else ""]
    sources = []
    for text in candidate_texts:
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            sentence = sentence.strip()
            if not sentence:
                continue
            lowered = sentence.lower()
            if query_terms and not any(term in lowered for term in query_terms):
                continue
            sources.append({"excerpt": sentence[:280], "confidence": 0.72})
            if len(sources) == 2:
                break
        if len(sources) == 2:
            break
    fallback = " ".join(source["excerpt"] for source in sources if source["excerpt"]).strip()
    return {
        "response": fallback or "I could not find enough indexed lecture context yet. Please make sure the lecture pipeline has completed.",
        "sources": sources,
    }


class CustomTokenObtainPairView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class LogoutView(generics.GenericAPIView):
    serializer_class = LogoutSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = RefreshToken(serializer.validated_data["refresh_token"])
        token.blacklist()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminOrganizationListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = OrganizationSerializer

    def get_queryset(self):
        queryset = Organization.objects.all().order_by("name")
        if self.request.user.is_superuser:
            return queryset
        if self.request.user.organization_id:
            return queryset.filter(pk=self.request.user.organization_id)
        return Organization.objects.none()

    def create(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            return Response(
                {"detail": "Only the platform super admin can create schools."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().create(request, *args, **kwargs)


class AdminOrganizationDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = OrganizationSerializer
    lookup_url_kwarg = "org_id"

    def get_queryset(self):
        queryset = Organization.objects.all()
        if self.request.user.is_superuser:
            return queryset
        if self.request.user.organization_id:
            return queryset.filter(pk=self.request.user.organization_id)
        return Organization.objects.none()

    def update(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            return Response(
                {"detail": "Only the platform super admin can update school setup."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            return Response(
                {"detail": "Only the platform super admin can delete schools."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().destroy(request, *args, **kwargs)


class AdminUserListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminRole]

    def get_queryset(self):
        queryset = UserModel.objects.select_related("organization").all()
        if not self.request.user.is_superuser:
            if self.request.user.organization_id is None:
                return UserModel.objects.none()
            queryset = queryset.filter(organization_id=self.request.user.organization_id, is_superuser=False)
        role = self.request.query_params.get("role")
        org_id = self.request.query_params.get("org_id")
        if role:
            queryset = queryset.filter(role=role)
        if org_id and self.request.user.is_superuser:
            queryset = queryset.filter(organization_id=org_id)
        return queryset.order_by("name", "email")

    def perform_create(self, serializer):
        user = serializer.save()
        create_audit_log(
            action="admin.create_user",
            actor=self.request.user,
            organization=user.organization,
            target_user=user,
            target_email=user.email,
            metadata={"role": user.role},
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return UserCreateUpdateSerializer
        return UserSerializer


class AdminUserDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminRole]
    lookup_url_kwarg = "user_id"

    def get_queryset(self):
        queryset = UserModel.objects.select_related("organization").all()
        if self.request.user.is_superuser:
            return queryset
        if self.request.user.organization_id:
            return queryset.filter(organization_id=self.request.user.organization_id, is_superuser=False)
        return UserModel.objects.none()

    def get_serializer_class(self):
        if self.request.method in {"PATCH", "PUT"}:
            return UserCreateUpdateSerializer
        return UserSerializer

    def perform_destroy(self, instance):
        if instance.is_superuser:
            raise drf_serializers.ValidationError({"detail": "Superuser accounts cannot be deactivated from this endpoint."})
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])


class PlatformAnalyticsView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request, *args, **kwargs):
        today = timezone.now().date()
        organization_filter = {}
        if not request.user.is_superuser:
            organization_filter = {"organization": request.user.organization}
        data = {
            "total_organizations": Organization.objects.count() if request.user.is_superuser else int(bool(request.user.organization_id)),
            "total_users": UserModel.objects.filter(**organization_filter).count(),
            "total_classrooms": Classroom.objects.filter(is_active=True, **organization_filter).count(),
            "total_lectures": Lecture.objects.filter(is_active=True, classroom__organization=request.user.organization).count()
            if not request.user.is_superuser
            else Lecture.objects.filter(is_active=True).count(),
            "api_usage": {
                "translation_calls_today": LectureTranslation.objects.filter(
                    created_at__date=today,
                    lecture__classroom__organization=request.user.organization,
                ).count()
                if not request.user.is_superuser
                else LectureTranslation.objects.filter(created_at__date=today).count(),
                "document_ai_calls_today": Lecture.objects.filter(
                    created_at__date=today,
                    **({"classroom__organization": request.user.organization} if not request.user.is_superuser else {}),
                ).exclude(whiteboard_notes__isnull=True).exclude(whiteboard_notes="").count(),
                "pipeline_jobs_completed": Lecture.objects.filter(
                    created_at__date=today,
                    processing_status=Lecture.ProcessingStatus.COMPLETED,
                    **({"classroom__organization": request.user.organization} if not request.user.is_superuser else {}),
                ).count(),
                "pipeline_jobs_failed": Lecture.objects.filter(
                    created_at__date=today,
                    processing_status=Lecture.ProcessingStatus.FAILED,
                    **({"classroom__organization": request.user.organization} if not request.user.is_superuser else {}),
                ).count(),
            },
        }
        return Response(data)


class SuperAdminBoardListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsSuperAdminRole]
    serializer_class = BoardSerializer
    queryset = Board.objects.all().order_by("name")


class SuperAdminBoardDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsSuperAdminRole]
    serializer_class = BoardSerializer
    queryset = Board.objects.all()
    lookup_url_kwarg = "board_id"


class SuperAdminGradeListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsSuperAdminRole]
    serializer_class = GradeSerializer
    queryset = Grade.objects.all().order_by("numeric_value", "name")


class SuperAdminGradeDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsSuperAdminRole]
    serializer_class = GradeSerializer
    queryset = Grade.objects.all()
    lookup_url_kwarg = "grade_id"


class SuperAdminSubjectListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsSuperAdminRole]
    serializer_class = SubjectSerializer

    def get_queryset(self):
        queryset = Subject.objects.select_related("board", "grade").all()
        board_id = self.request.query_params.get("board_id")
        grade_id = self.request.query_params.get("grade_id")
        if board_id:
            queryset = queryset.filter(board_id=board_id)
        if grade_id:
            queryset = queryset.filter(grade_id=grade_id)
        return queryset.order_by("board__name", "grade__numeric_value", "name")


class SuperAdminSubjectDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsSuperAdminRole]
    serializer_class = SubjectSerializer
    queryset = Subject.objects.select_related("board", "grade").all()
    lookup_url_kwarg = "subject_id"


class AdminOrganizationSubjectListView(generics.ListAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = SubjectSerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            org_id = self.request.query_params.get("org_id")
            if org_id:
                organization = Organization.objects.get(pk=org_id)
            else:
                return Subject.objects.none()
        else:
            organization = user.organization
        if organization is None:
            return Subject.objects.none()
        return Subject.objects.select_related("board", "grade").filter(
            board__in=organization.boards.all(),
            grade__in=organization.grades.all(),
            is_active=True,
        )


class AdminSyllabusDocumentListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = SyllabusDocumentSerializer

    def get_queryset(self):
        queryset = SyllabusDocument.objects.select_related("subject", "organization", "uploaded_by")
        if not self.request.user.is_superuser:
            queryset = queryset.filter(organization=self.request.user.organization)
        else:
            org_id = self.request.query_params.get("org_id")
            if org_id:
                queryset = queryset.filter(organization_id=org_id)
        subject_id = self.request.query_params.get("subject_id")
        if subject_id:
            queryset = queryset.filter(subject_id=subject_id)
        return queryset.order_by("-created_at")

    def perform_create(self, serializer):
        user = self.request.user
        organization = user.organization
        if user.is_superuser:
            organization_id = self.request.data.get("organization")
            if organization_id:
                organization = Organization.objects.get(pk=organization_id)
        if organization is None:
            raise drf_serializers.ValidationError({"organization": "Organization is required to upload syllabus files."})

        file_obj = self.request.FILES.get("file")
        if file_obj is None:
            raise drf_serializers.ValidationError({"file": "Syllabus file is required."})

        file_name = file_obj.name.lower()
        file_type = "pdf" if file_name.endswith(".pdf") else "txt" if file_name.endswith(".txt") else ""
        if not file_type:
            raise drf_serializers.ValidationError({"file": "Only .pdf and .txt syllabus files are supported."})

        try:
            extracted_text = extract_text_from_uploaded_file(file_obj, file_type=file_type)
        except RuntimeError as exc:
            raise drf_serializers.ValidationError({"file": str(exc)}) from exc
        file_obj.seek(0)
        serializer.save(
            organization=organization,
            uploaded_by=user,
            file_type=file_type,
            extracted_text=extracted_text,
            text_token_count=approximate_token_count(extracted_text),
            processing_status=SyllabusDocument.ProcessingStatus.READY,
            processing_error="",
        )


class AdminSyllabusDocumentDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = SyllabusDocumentSerializer
    lookup_url_kwarg = "document_id"

    def get_queryset(self):
        queryset = SyllabusDocument.objects.select_related("subject", "organization", "uploaded_by")
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(organization=self.request.user.organization)


class InteractiveVisualizerGenerateView(generics.GenericAPIView):
    permission_classes = [IsStudentRole]
    serializer_class = InteractiveVisualizerCreateSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        lecture = None
        lecture_id = serializer.validated_data.get("lecture_id")
        if lecture_id:
            lecture = Lecture.objects.select_related(
                "classroom",
                "classroom__organization",
                "classroom__subject",
                "classroom__subject__board",
                "classroom__subject__grade",
            ).get(
                pk=lecture_id,
                classroom__organization=request.user.organization,
                classroom__students=request.user,
                is_active=True,
            )

        if lecture is not None:
            guardrail = build_syllabus_guardrail_for_lecture(lecture)
            organization = lecture.classroom.organization
        else:
            organization = request.user.organization
            guardrail = (
                "Generate safe educational visualization code. "
                "If no syllabus context is provided, keep content general and beginner-friendly."
            )

        generated = generate_interactive_visualizer_code(
            guardrail_prompt=guardrail,
            user_prompt=serializer.validated_data["prompt"],
        )
        visualizer = InteractiveVisualizer.objects.create(
            organization=organization,
            lecture=lecture,
            requested_by=request.user,
            prompt=serializer.validated_data["prompt"],
            generated_code=generated["generated_code"],
            code_type=generated["code_type"],
            model_name=generated["model_name"],
            metadata=generated["metadata"],
        )
        return Response(InteractiveVisualizerSerializer(visualizer).data, status=status.HTTP_201_CREATED)


class InteractiveVisualizerListView(generics.ListAPIView):
    permission_classes = [IsStudentRole]
    serializer_class = InteractiveVisualizerSerializer

    def get_queryset(self):
        queryset = InteractiveVisualizer.objects.select_related("lecture", "organization", "requested_by")
        return queryset.filter(
            organization=self.request.user.organization,
            requested_by=self.request.user,
        )


class ProfessorClassroomListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsProfessorRole]

    def get_queryset(self):
        return classroom_base_queryset().filter(
            professor=self.request.user,
            organization=self.request.user.organization,
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ClassroomWriteSerializer
        return ClassroomListSerializer

    def perform_create(self, serializer):
        serializer.save(
            professor=self.request.user,
            organization=self.request.user.organization,
        )


class ProfessorClassroomDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsProfessorRole]
    lookup_url_kwarg = "classroom_id"

    def get_queryset(self):
        return classroom_base_queryset().filter(
            professor=self.request.user,
            organization=self.request.user.organization,
        )

    def get_serializer_class(self):
        if self.request.method in {"PATCH", "PUT"}:
            return ClassroomWriteSerializer
        return ClassroomDetailSerializer

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])


class ProfessorClassroomEnrollView(generics.GenericAPIView):
    permission_classes = [IsProfessorRole]
    serializer_class = EnrollStudentsSerializer

    def post(self, request, *args, **kwargs):
        classroom = classroom_base_queryset().get(
            pk=kwargs["classroom_id"],
            professor=request.user,
            organization=request.user.organization,
        )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        students = list(
            student_user_queryset().filter(
                organization=request.user.organization,
                id__in=serializer.validated_data["student_ids"],
            )
        )
        enrollments = [ClassroomEnrollment(user=student, classroom=classroom) for student in students]
        ClassroomEnrollment.objects.bulk_create(enrollments, ignore_conflicts=True)
        return Response({"enrolled": len(students)}, status=status.HTTP_200_OK)


class ProfessorClassroomRemoveStudentView(APIView):
    permission_classes = [IsProfessorRole]

    def delete(self, request, *args, **kwargs):
        deleted, _ = ClassroomEnrollment.objects.filter(
            classroom__professor=request.user,
            classroom__organization=request.user.organization,
            classroom_id=kwargs["classroom_id"],
            user_id=kwargs["student_id"],
        ).delete()
        if not deleted:
            return Response({"detail": "Enrollment not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProfessorClassroomLectureListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsProfessorRole]

    def get_queryset(self):
        return lecture_base_queryset().filter(
            classroom_id=self.kwargs["classroom_id"],
            classroom__professor=self.request.user,
            classroom__organization=self.request.user.organization,
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return LectureWriteSerializer
        return LectureListSerializer

    def perform_create(self, serializer):
        classroom = Classroom.objects.select_related("organization").get(
            pk=self.kwargs["classroom_id"],
            professor=self.request.user,
            organization=self.request.user.organization,
            is_active=True,
        )
        serializer.save(classroom=classroom, uploaded_by=self.request.user)


class ProfessorLectureDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsProfessorRole]
    lookup_url_kwarg = "lecture_id"

    def get_queryset(self):
        return lecture_base_queryset().filter(
            classroom__professor=self.request.user,
            classroom__organization=self.request.user.organization,
        )

    def get_serializer_class(self):
        if self.request.method in {"PATCH", "PUT"}:
            return LectureWriteSerializer
        return LectureDetailSerializer

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])


class ProfessorLectureTriggerPipelineView(APIView):
    permission_classes = [IsProfessorRole]

    def post(self, request, *args, **kwargs):
        lecture = Lecture.objects.select_related("classroom", "classroom__organization").get(
            pk=kwargs["lecture_id"],
            classroom__professor=request.user,
            classroom__organization=request.user.organization,
            is_active=True,
        )
        active_run = lecture.pipeline_runs.filter(
            status__in=[LecturePipelineRun.Status.QUEUED, LecturePipelineRun.Status.RUNNING]
        ).first()
        if active_run:
            return Response(
                {
                    "detail": "A pipeline job is already active for this lecture.",
                    "job_id": active_run.task_id,
                },
                status=status.HTTP_409_CONFLICT,
            )

        pipeline_run = LecturePipelineRun.objects.create(
            lecture=lecture,
            triggered_by=request.user,
        )
        lecture.processing_status = Lecture.ProcessingStatus.UPLOADED
        lecture.processing_error = ""
        lecture.save(update_fields=["processing_status", "processing_error", "updated_at"])
        async_result = launch_lecture_pipeline(pipeline_run.id)
        pipeline_run.task_id = async_result.id
        pipeline_run.save(update_fields=["task_id", "updated_at"])
        return Response(
            {
                "job_id": pipeline_run.task_id,
                "pipeline_run": LecturePipelineRunSerializer(pipeline_run).data,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ProfessorLecturePipelineStatusView(APIView):
    permission_classes = [IsProfessorRole]

    def get(self, request, *args, **kwargs):
        lecture = Lecture.objects.prefetch_related(
            Prefetch("pipeline_runs", queryset=LecturePipelineRun.objects.order_by("-created_at"))
        ).get(
            pk=kwargs["lecture_id"],
            classroom__professor=request.user,
            classroom__organization=request.user.organization,
            is_active=True,
        )
        pipeline_runs = list(lecture.pipeline_runs.all())
        pipeline_run = pipeline_runs[0] if pipeline_runs else None
        if pipeline_run:
            return Response(
                {
                    "lecture_id": lecture.id,
                    "job_id": pipeline_run.task_id,
                    "pipeline_run": LecturePipelineRunSerializer(pipeline_run).data,
                    "status": pipeline_run.status,
                    "progress": pipeline_run.progress,
                    "errors": [pipeline_run.error_message] if pipeline_run.error_message else [],
                }
            )
        return Response(
            {
                "lecture_id": lecture.id,
                "status": lecture.processing_status,
                "progress": progress_map_for_status(lecture.processing_status),
                "errors": [lecture.processing_error] if lecture.processing_error else [],
            }
        )


class ProfessorLectureQuizListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsProfessorRole]

    def get_queryset(self):
        return quiz_base_queryset(include_questions=True).filter(
            lecture_id=self.kwargs["lecture_id"],
            lecture__classroom__professor=self.request.user,
            lecture__classroom__organization=self.request.user.organization,
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return QuizWriteSerializer
        return QuizSerializer

    def perform_create(self, serializer):
        lecture = Lecture.objects.select_related("classroom").get(
            pk=self.kwargs["lecture_id"],
            classroom__professor=self.request.user,
            classroom__organization=self.request.user.organization,
            is_active=True,
        )
        serializer.save(lecture=lecture, classroom=lecture.classroom)


class ProfessorQuizDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsProfessorRole]
    lookup_url_kwarg = "quiz_id"

    def get_queryset(self):
        return quiz_base_queryset(include_questions=True).filter(
            lecture__classroom__professor=self.request.user,
            lecture__classroom__organization=self.request.user.organization,
        )

    def get_serializer_class(self):
        if self.request.method in {"PATCH", "PUT"}:
            return QuizWriteSerializer
        return QuizSerializer

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])


class ProfessorQuizQuestionDetailView(generics.UpdateAPIView):
    permission_classes = [IsProfessorRole]
    serializer_class = QuizQuestionSerializer
    lookup_url_kwarg = "qid"

    def get_queryset(self):
        return QuizQuestion.objects.select_related(
            "quiz",
            "quiz__lecture",
            "quiz__classroom",
        ).filter(
            quiz_id=self.kwargs["quiz_id"],
            quiz__lecture__classroom__professor=self.request.user,
            quiz__lecture__classroom__organization=self.request.user.organization,
            quiz__is_active=True,
        )


class ProfessorQuizPublishView(APIView):
    permission_classes = [IsProfessorRole]

    def post(self, request, *args, **kwargs):
        quiz = Quiz.objects.get(
            pk=kwargs["quiz_id"],
            lecture__classroom__professor=request.user,
            lecture__classroom__organization=request.user.organization,
            is_active=True,
        )
        quiz.is_published = True
        quiz.save(update_fields=["is_published", "updated_at"])
        return Response(QuizSerializer(quiz).data)


class ProfessorClassroomAnalyticsView(APIView):
    permission_classes = [IsProfessorRole]

    def get(self, request, *args, **kwargs):
        classroom = classroom_base_queryset().get(
            pk=kwargs["classroom_id"],
            professor=request.user,
            organization=request.user.organization,
        )
        lectures = list(
            Lecture.objects.filter(classroom=classroom, is_active=True)
            .prefetch_related(
                Prefetch("progress_records", queryset=LectureProgress.objects.filter(student__is_active=True)),
                Prefetch(
                    "quizzes",
                    queryset=Quiz.objects.filter(is_active=True).prefetch_related("attempts"),
                ),
            )
        )
        attempts = StudentQuizAttempt.objects.filter(quiz__classroom=classroom, quiz__is_active=True)
        lecture_cards = []
        for lecture in lectures:
            progress_records = list(lecture.progress_records.all())
            watch_percentages = [
                min(record.timestamp_seconds / record.duration_seconds, 1)
                for record in progress_records
                if record.duration_seconds
            ]
            lecture_attempts = [attempt.score for quiz in lecture.quizzes.all() for attempt in quiz.attempts.all()]
            lecture_cards.append(
                {
                    "id": lecture.id,
                    "title": lecture.title,
                    "views": len(progress_records),
                    "avg_watch_percentage": round(sum(watch_percentages) / len(watch_percentages), 2)
                    if watch_percentages
                    else 0,
                    "most_rewatched_section": None,
                    "quiz_avg_score": round(sum(lecture_attempts) / len(lecture_attempts), 2)
                    if lecture_attempts
                    else 0,
                }
            )

        total_expected_attempts = classroom.student_count * max(
            Quiz.objects.filter(classroom=classroom, is_active=True, is_published=True).count(),
            1,
        )
        completion_rate = round(attempts.count() / total_expected_attempts, 2) if total_expected_attempts else 0
        return Response(
            {
                "classroom_id": classroom.id,
                "total_students": classroom.student_count,
                "total_lectures": len(lectures),
                "avg_quiz_score": round(attempts.aggregate(avg=Avg("score"))["avg"] or 0, 2),
                "completion_rate": completion_rate,
                "lectures": lecture_cards,
            }
        )


class ProfessorLectureAnalyticsView(APIView):
    permission_classes = [IsProfessorRole]

    def get(self, request, *args, **kwargs):
        lecture = Lecture.objects.filter(
            pk=kwargs["lecture_id"],
            classroom__professor=request.user,
            classroom__organization=request.user.organization,
            is_active=True,
        ).prefetch_related(
            Prefetch("progress_records", queryset=LectureProgress.objects.filter(student__is_active=True)),
            Prefetch("quizzes", queryset=Quiz.objects.filter(is_active=True).prefetch_related("attempts")),
        ).get()

        progress_records = list(lecture.progress_records.all())
        watch_percentages = [
            min(record.timestamp_seconds / record.duration_seconds, 1)
            for record in progress_records
            if record.duration_seconds
        ]
        quiz_attempts = [attempt.score for quiz in lecture.quizzes.all() for attempt in quiz.attempts.all()]
        data = {
            "lecture_id": lecture.id,
            "title": lecture.title,
            "views": len(progress_records),
            "avg_watch_percentage": round(sum(watch_percentages) / len(watch_percentages), 2)
            if watch_percentages
            else 0,
            "most_rewatched_section": None,
            "quiz_avg_score": round(sum(quiz_attempts) / len(quiz_attempts), 2) if quiz_attempts else 0,
        }
        return Response(data)


class StudentDashboardView(APIView):
    permission_classes = [IsStudentRole]

    def get(self, request, *args, **kwargs):
        classrooms = list(
            classroom_base_queryset()
            .filter(
                students=request.user,
                organization=request.user.organization,
                is_active=True,
            )
            .prefetch_related(
                Prefetch("lectures", queryset=Lecture.objects.filter(is_active=True)),
            )
            .distinct()
        )

        pending_quizzes = list(
            quiz_base_queryset(include_questions=False, published_only=True)
            .filter(classroom__students=request.user, classroom__is_active=True)
            .exclude(attempts__student=request.user)
            .distinct()
        )
        completed_lecture_ids = set(
            LectureProgress.objects.filter(
                student=request.user,
                lecture__classroom__in=[classroom.id for classroom in classrooms],
            ).values_list("lecture_id", flat=True)
        )
        resume_progress = (
            LectureProgress.objects.select_related("lecture", "lecture__classroom")
            .filter(student=request.user, lecture__is_active=True)
            .order_by("-last_accessed_at")
            .first()
        )

        enrolled_classrooms = []
        for classroom in classrooms:
            total_lectures = len(classroom.lectures.all())
            completed_lectures = sum(
                1
                for lecture in classroom.lectures.all()
                if lecture.id in completed_lecture_ids
            )
            enrolled_classrooms.append(
                {
                    "id": classroom.id,
                    "name": classroom.name,
                    "subject": classroom.subject.name,
                    "professor_name": classroom.professor.name,
                    "lecture_count": total_lectures,
                    "completed_lectures": completed_lectures,
                    "progress": round(completed_lectures / total_lectures, 2) if total_lectures else 0,
                }
            )

        return Response(
            {
                "enrolled_classrooms": enrolled_classrooms,
                "pending_quizzes": [
                    {
                        "quiz_id": quiz.id,
                        "title": quiz.title,
                        "lecture_title": quiz.lecture.title,
                        "classroom_name": quiz.classroom.name,
                        "question_count": quiz.question_count,
                    }
                    for quiz in pending_quizzes
                ],
                "resume_lecture": {
                    "lecture_id": resume_progress.lecture_id,
                    "title": resume_progress.lecture.title,
                    "classroom_name": resume_progress.lecture.classroom.name,
                    "progress_seconds": resume_progress.timestamp_seconds,
                    "duration_seconds": resume_progress.duration_seconds,
                }
                if resume_progress
                else None,
            }
        )


class StudentClassroomDetailView(generics.RetrieveAPIView):
    permission_classes = [IsStudentRole]
    serializer_class = ClassroomDetailSerializer
    lookup_url_kwarg = "classroom_id"

    def get_queryset(self):
        return classroom_base_queryset().filter(
            students=self.request.user,
            organization=self.request.user.organization,
            is_active=True,
        ).distinct()


class StudentClassroomLectureListView(generics.ListAPIView):
    permission_classes = [IsStudentRole]
    serializer_class = LectureListSerializer

    def get_queryset(self):
        return lecture_base_queryset(published_quizzes_only=True).filter(
            classroom_id=self.kwargs["classroom_id"],
            classroom__students=self.request.user,
            classroom__organization=self.request.user.organization,
            processing_status=Lecture.ProcessingStatus.COMPLETED,
        ).distinct()


class StudentLectureDetailView(generics.RetrieveAPIView):
    permission_classes = [IsStudentRole]
    serializer_class = StudentLectureDetailSerializer
    lookup_url_kwarg = "lecture_id"

    def get_queryset(self):
        return lecture_base_queryset(published_quizzes_only=True).filter(
            classroom__students=self.request.user,
            classroom__organization=self.request.user.organization,
            processing_status=Lecture.ProcessingStatus.COMPLETED,
        ).distinct()


class StudentLectureTranscriptView(APIView):
    permission_classes = [IsStudentRole]

    def get(self, request, *args, **kwargs):
        lecture = Lecture.objects.select_related("classroom", "classroom__organization").prefetch_related("translations").get(
            pk=kwargs["lecture_id"],
            classroom__students=request.user,
            classroom__organization=request.user.organization,
            is_active=True,
            processing_status=Lecture.ProcessingStatus.COMPLETED,
        )
        lang = request.query_params.get("lang", "en")
        if lang == "en":
            english_translation = next((item for item in lecture.translations.all() if item.language_code == "en"), None)
            return Response(
                {
                    "language_code": "en",
                    "translated_text": (
                        english_translation.translated_text
                        if english_translation and english_translation.translated_text
                        else lecture.original_transcript
                    ),
                    "pdf_url": None,
                }
            )

        translation = next((item for item in lecture.translations.all() if item.language_code == lang), None)
        if translation is None:
            return Response({"detail": "Translation not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(
            {
                "language_code": translation.language_code,
                "translated_text": translation.translated_text,
                "pdf_url": translation.pdf_url,
            }
        )


class StudentLectureProgressView(generics.GenericAPIView):
    permission_classes = [IsStudentRole]
    serializer_class = TrackProgressSerializer

    def post(self, request, *args, **kwargs):
        lecture = Lecture.objects.get(
            pk=kwargs["lecture_id"],
            classroom__students=request.user,
            classroom__organization=request.user.organization,
            is_active=True,
            processing_status=Lecture.ProcessingStatus.COMPLETED,
        )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        progress, _ = LectureProgress.objects.update_or_create(
            student=request.user,
            lecture=lecture,
            defaults=serializer.validated_data,
        )
        return Response(
            {
                "lecture_id": lecture.id,
                "timestamp_seconds": progress.timestamp_seconds,
                "duration_seconds": progress.duration_seconds,
                "last_accessed_at": progress.last_accessed_at,
            }
        )


class StudentLectureQuizListView(generics.ListAPIView):
    permission_classes = [IsStudentRole]
    serializer_class = QuizSummarySerializer

    def get_queryset(self):
        return quiz_base_queryset(include_questions=False, published_only=True).filter(
            lecture_id=self.kwargs["lecture_id"],
            lecture__classroom__students=self.request.user,
            lecture__classroom__organization=self.request.user.organization,
        ).distinct()


class StudentQuizDetailView(generics.RetrieveAPIView):
    permission_classes = [IsStudentRole]
    serializer_class = StudentQuizSerializer
    lookup_url_kwarg = "quiz_id"

    def get_queryset(self):
        return quiz_base_queryset(include_questions=True, published_only=True).filter(
            classroom__students=self.request.user,
            classroom__organization=self.request.user.organization,
        ).distinct()


class StudentQuizSubmitView(generics.GenericAPIView):
    permission_classes = [IsStudentRole]
    serializer_class = QuizSubmissionSerializer

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        quiz = quiz_base_queryset(include_questions=True, published_only=True).get(
            pk=kwargs["quiz_id"],
            classroom__students=request.user,
            classroom__organization=request.user.organization,
        )
        if StudentQuizAttempt.objects.filter(student=request.user, quiz=quiz).exists():
            return Response(
                {"detail": "Quiz has already been submitted by this student."},
                status=status.HTTP_409_CONFLICT,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        answers = serializer.validated_data["answers"]
        questions = list(quiz.questions.all())
        per_question = []
        correct_count = 0
        for question in questions:
            selected_answer = answers.get(str(question.id))
            is_correct = selected_answer == question.correct_answer
            correct_count += int(is_correct)
            per_question.append(
                {
                    "question": question,
                    "selected_answer": selected_answer,
                    "correct_answer": question.correct_answer,
                    "is_correct": is_correct,
                    "explanation": question.explanation,
                }
            )

        total_questions = len(questions)
        score = round((correct_count / total_questions) * 100, 2) if total_questions else 0
        attempt = StudentQuizAttempt.objects.create(
            student=request.user,
            quiz=quiz,
            answers=answers,
            score=score,
            total_questions=total_questions,
            correct_count=correct_count,
            started_at=serializer.validated_data["started_at"],
        )
        QuizAttemptAnswer.objects.bulk_create(
            [
                QuizAttemptAnswer(
                    attempt=attempt,
                    question=item["question"],
                    selected_answer=item["selected_answer"],
                    is_correct=item["is_correct"],
                )
                for item in per_question
            ]
        )

        return Response(
            {
                "attempt_id": attempt.id,
                "score": score,
                "total_questions": total_questions,
                "correct_count": correct_count,
                "completed_at": attempt.completed_at,
                "per_question": [
                    {
                        "question_id": item["question"].id,
                        "selected_answer": item["selected_answer"],
                        "correct_answer": item["correct_answer"],
                        "is_correct": item["is_correct"],
                        "explanation": item["explanation"],
                    }
                    for item in per_question
                ],
            },
            status=status.HTTP_201_CREATED,
        )


class StudentEnrollView(generics.GenericAPIView):
    permission_classes = [IsStudentRole]
    serializer_class = InviteCodeEnrollmentSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        classroom = Classroom.objects.select_related("professor", "organization").get(
            invite_code=serializer.validated_data["invite_code"],
            organization=request.user.organization,
            is_active=True,
        )
        ClassroomEnrollment.objects.get_or_create(user=request.user, classroom=classroom)
        serialized = ClassroomDetailSerializer(
            classroom_base_queryset().get(pk=classroom.pk),
            context={"request": request},
        )
        return Response(serialized.data, status=status.HTTP_200_OK)


class StudentLectureChatView(generics.GenericAPIView):
    permission_classes = [IsStudentRole]
    serializer_class = LectureChatSerializer

    def post(self, request, *args, **kwargs):
        lecture = Lecture.objects.select_related("summary", "classroom").get(
            pk=kwargs["lecture_id"],
            classroom__students=request.user,
            classroom__organization=request.user.organization,
            is_active=True,
            processing_status=Lecture.ProcessingStatus.COMPLETED,
        )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(build_chat_response(lecture, serializer.validated_data["message"]))


# ── Whitelist endpoints ──

class AdminWhitelistTeacherView(generics.GenericAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = WhitelistTeacherSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from api.models import WhitelistedEmail
        email = UserModel.objects.normalize_email(serializer.validated_data["email"])
        organization_id = serializer.validated_data["organization_id"]

        existing_teacher = UserModel.objects.filter(
            email=email,
            organization_id=organization_id,
            role=User.Role.PROFESSOR,
            is_active=True,
        ).first()
        if existing_teacher:
            return Response(
                {
                    "email": email,
                    "status": "already_exists",
                    "user_id": existing_teacher.id,
                    "message": "This teacher already has an active account for this school.",
                },
                status=status.HTTP_200_OK,
            )

        existing_invite = WhitelistedEmail.objects.filter(
            email=email,
            organization_id=organization_id,
        ).first()
        if existing_invite:
            return Response(
                {
                    "id": existing_invite.id,
                    "email": existing_invite.email,
                    "role": existing_invite.role,
                    "status": "already_used" if existing_invite.is_used else "already_whitelisted",
                    "message": "This teacher email is already present in the onboarding list for this school.",
                },
                status=status.HTTP_200_OK,
            )

        entry = WhitelistedEmail.objects.create(
            email=email,
            role=WhitelistedEmail.InviteRole.TEACHER,
            created_by=request.user,
            organization_id=organization_id,
            grade=serializer.validated_data.get("grade"),
            section=serializer.validated_data.get("section"),
        )
        create_audit_log(
            action="admin.whitelist_teacher",
            actor=request.user,
            organization=entry.organization,
            target_email=entry.email,
            metadata={"grade": entry.grade, "section": entry.section},
        )
        return Response({"id": entry.id, "email": entry.email, "role": entry.role, "status": "whitelisted"}, status=status.HTTP_201_CREATED)


class TeacherWhitelistStudentView(generics.GenericAPIView):
    permission_classes = [IsProfessorRole]
    serializer_class = WhitelistStudentSerializer

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        teacher = request.user
        profile = getattr(teacher, "profile", None)
        if profile is None:
            return Response({"detail": "Please complete your profile before whitelisting students."}, status=status.HTTP_400_BAD_REQUEST)
        org = teacher.organization

        # Check for returning student
        existing_student = User.objects.filter(
            email=email, organization=org, role=User.Role.STUDENT, is_active=True,
        ).first()
        if existing_student:
            from api.models import WhitelistedEmail
            from api.communication.email_service import send_reassignment_notification
            student_profile = existing_student.profile
            student_profile.grade = profile.grade
            student_profile.section = profile.section
            student_profile.mapped_teacher = teacher
            student_profile.save(update_fields=["grade", "section", "mapped_teacher"])
            WhitelistedEmail.objects.create(
                email=email, role=WhitelistedEmail.InviteRole.STUDENT, created_by=teacher,
                organization=org, is_used=True, used_by=existing_student,
                grade=profile.grade, section=profile.section,
            )
            send_reassignment_notification(
                email=email, student_name=existing_student.name,
                teacher_name=teacher.name, grade=profile.grade, section=profile.section,
            )
            create_audit_log(
                action="teacher.reassign_student",
                actor=teacher,
                organization=org,
                target_email=email,
                target_user=existing_student,
                metadata={"grade": profile.grade, "section": profile.section},
            )
            return Response({"status": "reassigned", "user_id": existing_student.id, "message": "Returning student reassigned to your class."})

        from api.models import WhitelistedEmail
        entry = WhitelistedEmail.objects.create(
            email=email, role=WhitelistedEmail.InviteRole.STUDENT, created_by=teacher,
            organization=org, grade=profile.grade, section=profile.section,
        )
        create_audit_log(
            action="teacher.whitelist_student",
            actor=teacher,
            organization=org,
            target_email=email,
            metadata={"grade": profile.grade, "section": profile.section},
        )
        return Response({"id": entry.id, "email": entry.email, "status": "whitelisted"}, status=status.HTTP_201_CREATED)


class TeacherBulkWhitelistStudentView(generics.GenericAPIView):
    permission_classes = [IsProfessorRole]
    serializer_class = BulkWhitelistStudentSerializer

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        teacher = request.user
        profile = getattr(teacher, "profile", None)
        if profile is None:
            return Response({"detail": "Please complete your profile first."}, status=status.HTTP_400_BAD_REQUEST)
        org = teacher.organization
        from api.models import WhitelistedEmail
        results = []
        for email in serializer.validated_data["emails"]:
            existing = User.objects.filter(email=email, organization=org, role=User.Role.STUDENT, is_active=True).first()
            if existing:
                sp = existing.profile
                sp.grade = profile.grade
                sp.section = profile.section
                sp.mapped_teacher = teacher
                sp.save(update_fields=["grade", "section", "mapped_teacher"])
                WhitelistedEmail.objects.create(
                    email=email, role=WhitelistedEmail.InviteRole.STUDENT, created_by=teacher,
                    organization=org, is_used=True, used_by=existing,
                    grade=profile.grade, section=profile.section,
                )
                create_audit_log(
                    action="teacher.reassign_student",
                    actor=teacher,
                    organization=org,
                    target_email=email,
                    target_user=existing,
                    metadata={"grade": profile.grade, "section": profile.section, "source": "bulk"},
                )
                results.append({"email": email, "status": "reassigned", "user_id": existing.id})
            else:
                entry = WhitelistedEmail.objects.create(
                    email=email, role=WhitelistedEmail.InviteRole.STUDENT, created_by=teacher,
                    organization=org, grade=profile.grade, section=profile.section,
                )
                create_audit_log(
                    action="teacher.whitelist_student",
                    actor=teacher,
                    organization=org,
                    target_email=email,
                    metadata={"grade": profile.grade, "section": profile.section, "source": "bulk"},
                )
                results.append({"email": email, "status": "whitelisted", "id": entry.id})
        return Response({"results": results}, status=status.HTTP_201_CREATED)


class TeacherAvailableSubjectsView(generics.ListAPIView):
    permission_classes = [IsProfessorRole]
    serializer_class = SubjectSerializer

    def get_queryset(self):
        organization = self.request.user.organization
        if organization is None:
            return Subject.objects.none()
        queryset = Subject.objects.select_related("board", "grade").filter(
            is_active=True,
            board__in=organization.boards.all(),
            grade__in=organization.grades.all(),
        )
        classroom_id = self.request.query_params.get("classroom_id")
        if classroom_id:
            classroom = Classroom.objects.filter(
                pk=classroom_id,
                organization=organization,
                is_active=True,
            ).first()
            if classroom is None:
                return Subject.objects.none()
            queryset = queryset.filter(grade=classroom.subject.grade)
        return queryset.order_by("board__name", "grade__numeric_value", "name")


class TeacherAvailableTeachersView(generics.ListAPIView):
    permission_classes = [IsProfessorRole]
    serializer_class = AvailableTeacherSerializer

    def get_queryset(self):
        organization = self.request.user.organization
        queryset = User.objects.select_related("profile").filter(
            role=User.Role.PROFESSOR,
            organization=organization,
            is_active=True,
        )
        classroom_id = self.request.query_params.get("classroom_id")
        if classroom_id:
            classroom = Classroom.objects.filter(
                pk=classroom_id,
                organization=organization,
                is_active=True,
            ).first()
            if classroom is None:
                return User.objects.none()
            if classroom.class_teacher and classroom.class_teacher != self.request.user:
                return User.objects.none()
            queryset = queryset.exclude(pk=classroom.professor_id)
        return queryset.order_by("name", "email")


class AdminAuditLogListView(generics.ListAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = AuditLogSerializer
    class AuditLogPagination(PageNumberPagination):
        page_size = 20
        page_size_query_param = "page_size"
        max_page_size = 100

    pagination_class = AuditLogPagination

    def get_queryset(self):
        queryset = AuditLog.objects.select_related("organization", "actor", "target_user")
        if not self.request.user.is_superuser:
            queryset = queryset.filter(organization=self.request.user.organization)
        else:
            org_id = self.request.query_params.get("org_id")
            if org_id:
                queryset = queryset.filter(organization_id=org_id)
        action = self.request.query_params.get("action")
        if action:
            queryset = queryset.filter(action__icontains=action)
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)
        from_date_raw = self.request.query_params.get("from_date")
        to_date_raw = self.request.query_params.get("to_date")
        if from_date_raw:
            from_date = parse_date(from_date_raw)
            if from_date is None:
                raise drf_serializers.ValidationError({"from_date": "Invalid date format. Use YYYY-MM-DD."})
            queryset = queryset.filter(created_at__date__gte=from_date)
        if to_date_raw:
            to_date = parse_date(to_date_raw)
            if to_date is None:
                raise drf_serializers.ValidationError({"to_date": "Invalid date format. Use YYYY-MM-DD."})
            queryset = queryset.filter(created_at__date__lte=to_date)
        return queryset.order_by("-created_at")


# ── Profile endpoint ──

class UserProfileView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserProfileSerializer

    def get_object(self):
        from api.models import UserProfile
        profile, _ = UserProfile.objects.get_or_create(user=self.request.user)
        return profile

    def perform_update(self, serializer):
        serializer.save()
        user = self.request.user
        if user.role == User.Role.ADMIN and not user.is_profile_complete:
            user.is_profile_complete = True
            user.save(update_fields=["is_profile_complete", "updated_at"])
            return
        if user.role in {User.Role.PROFESSOR, User.Role.STUDENT} and not user.is_profile_complete:
            user.is_profile_complete = True
            user.save(update_fields=["is_profile_complete", "updated_at"])


# ── Classroom orchestration endpoints ──

class ClassroomSubjectTeacherListView(generics.ListCreateAPIView):
    permission_classes = [IsProfessorRole]

    def get_queryset(self):
        from api.models import ClassroomSubjectTeacher
        return ClassroomSubjectTeacher.objects.select_related("teacher", "subject", "subject__board", "subject__grade").filter(
            classroom_id=self.kwargs["classroom_id"],
            classroom__organization=self.request.user.organization,
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return AssignSubjectTeacherSerializer
        return ClassroomSubjectTeacherSerializer

    def perform_create(self, serializer):
        from api.models import Classroom, ClassroomSubjectTeacher, Subject, User
        classroom = Classroom.objects.get(
            pk=self.kwargs["classroom_id"], organization=self.request.user.organization,
        )
        if classroom.class_teacher and classroom.class_teacher != self.request.user:
            raise drf_serializers.ValidationError({"detail": "Only the class teacher can assign subject teachers."})
        teacher = User.objects.get(
            pk=serializer.validated_data["teacher_id"],
            organization=self.request.user.organization,
            role=User.Role.PROFESSOR,
        )
        subject = Subject.objects.get(pk=serializer.validated_data["subject_id"], is_active=True)
        assignment = ClassroomSubjectTeacher.objects.create(classroom=classroom, teacher=teacher, subject=subject)
        create_audit_log(
            action="teacher.assign_subject_teacher",
            actor=self.request.user,
            organization=classroom.organization,
            target_user=teacher,
            target_email=teacher.email,
            metadata={
                "classroom_id": classroom.id,
                "classroom_name": classroom.name,
                "subject_id": subject.id,
                "subject_name": subject.name,
                "assignment_id": assignment.id,
            },
        )


class ClassroomSubjectTeacherRemoveView(generics.DestroyAPIView):
    permission_classes = [IsProfessorRole]
    lookup_url_kwarg = "st_id"

    def get_queryset(self):
        from api.models import ClassroomSubjectTeacher
        return ClassroomSubjectTeacher.objects.filter(
            classroom_id=self.kwargs["classroom_id"],
            classroom__organization=self.request.user.organization,
        )

    def perform_destroy(self, instance):
        classroom = instance.classroom
        if classroom.class_teacher and classroom.class_teacher != self.request.user:
            raise drf_serializers.ValidationError({"detail": "Only the class teacher can remove subject teachers."})
        create_audit_log(
            action="teacher.remove_subject_teacher",
            actor=self.request.user,
            organization=classroom.organization,
            target_user=instance.teacher,
            target_email=instance.teacher.email,
            metadata={
                "classroom_id": classroom.id,
                "classroom_name": classroom.name,
                "subject_id": instance.subject_id,
                "subject_name": instance.subject.name,
                "assignment_id": instance.id,
            },
        )
        instance.delete()
