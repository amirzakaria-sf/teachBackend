from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from api.auth_utils import validate_password_or_raise
from api.models import (
    AuditLog,
    Board,
    Classroom,
    ClassroomEnrollment,
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
    Subject,
    SyllabusDocument,
    StudentQuizAttempt,
    Summary,
    User,
    UserProfile,
)


UserModel = get_user_model()


class OrganizationSerializer(serializers.ModelSerializer):
    board_ids = serializers.PrimaryKeyRelatedField(
        source="boards",
        queryset=Board.objects.filter(is_active=True),
        many=True,
        required=False,
    )
    grade_ids = serializers.PrimaryKeyRelatedField(
        source="grades",
        queryset=Grade.objects.filter(is_active=True),
        many=True,
        required=False,
    )

    class Meta:
        model = Organization
        fields = [
            "id",
            "name",
            "slug",
            "logo_url",
            "supported_languages",
            "board_ids",
            "grade_ids",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "slug", "created_at", "updated_at"]


class BoardSerializer(serializers.ModelSerializer):
    class Meta:
        model = Board
        fields = ["id", "name", "description", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class GradeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Grade
        fields = ["id", "name", "numeric_value", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class SubjectSerializer(serializers.ModelSerializer):
    board_name = serializers.CharField(source="board.name", read_only=True)
    grade_name = serializers.CharField(source="grade.name", read_only=True)

    class Meta:
        model = Subject
        fields = [
            "id",
            "name",
            "board",
            "board_name",
            "grade",
            "grade_name",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class SyllabusDocumentSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)

    class Meta:
        model = SyllabusDocument
        fields = [
            "id",
            "organization",
            "subject",
            "subject_name",
            "title",
            "file",
            "file_type",
            "extracted_text",
            "text_token_count",
            "processing_status",
            "processing_error",
            "uploaded_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "file_type",
            "extracted_text",
            "text_token_count",
            "processing_status",
            "processing_error",
            "uploaded_by",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        request = self.context.get("request")
        organization = attrs.get("organization", getattr(self.instance, "organization", None))
        subject = attrs.get("subject", getattr(self.instance, "subject", None))

        if request and request.user.is_authenticated:
            if request.user.is_superuser:
                if organization is None:
                    raise serializers.ValidationError({"organization": "Organization is required to upload syllabus files."})
            else:
                if request.user.organization_id is None:
                    raise serializers.ValidationError({"organization": "Your admin account is not assigned to a school."})
                requested_organization = attrs.get("organization")
                if requested_organization and requested_organization != request.user.organization:
                    raise serializers.ValidationError(
                        {"organization": "School admins can only manage syllabus documents for their own school."}
                    )
                attrs["organization"] = request.user.organization
                organization = request.user.organization

        if organization is None:
            raise serializers.ValidationError({"organization": "Organization is required to upload syllabus files."})

        if subject:
            if not organization.boards.filter(pk=subject.board_id).exists():
                raise serializers.ValidationError({"subject": "Subject board is not enabled for this organization."})
            if not organization.grades.filter(pk=subject.grade_id).exists():
                raise serializers.ValidationError({"subject": "Subject grade is not enabled for this organization."})
        return attrs


class SubjectSummarySerializer(serializers.ModelSerializer):
    board = serializers.CharField(source="board.name", read_only=True)
    grade = serializers.CharField(source="grade.name", read_only=True)

    class Meta:
        model = Subject
        fields = ["id", "name", "board", "grade"]


class UserSummarySerializer(serializers.ModelSerializer):
    organization_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = UserModel
        fields = ["id", "email", "name", "role", "organization_id", "is_active"]


class UserSerializer(serializers.ModelSerializer):
    organization_id = serializers.PrimaryKeyRelatedField(
        source="organization",
        queryset=Organization.objects.all(),
        required=False,
        allow_null=True,
    )
    grade = serializers.CharField(source="profile.grade", read_only=True, allow_null=True)
    section = serializers.CharField(source="profile.section", read_only=True, allow_null=True)
    student_identifier = serializers.CharField(source="profile.student_identifier", read_only=True, allow_null=True)
    mapped_teacher_id = serializers.IntegerField(source="profile.mapped_teacher_id", read_only=True, allow_null=True)
    mapped_teacher_name = serializers.CharField(source="profile.mapped_teacher.name", read_only=True, allow_null=True)

    class Meta:
        model = UserModel
        fields = [
            "id",
            "email",
            "name",
            "role",
            "organization_id",
            "grade",
            "section",
            "student_identifier",
            "mapped_teacher_id",
            "mapped_teacher_name",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class UserCreateUpdateSerializer(UserSerializer):
    password = serializers.CharField(write_only=True, required=False, min_length=8)
    grade = serializers.CharField(source="profile.grade", required=False, allow_blank=True, allow_null=True)
    section = serializers.CharField(source="profile.section", required=False, allow_blank=True, allow_null=True)
    mapped_teacher_id = serializers.PrimaryKeyRelatedField(
        source="profile.mapped_teacher",
        queryset=UserModel.objects.filter(role=User.Role.PROFESSOR, is_active=True),
        required=False,
        allow_null=True,
    )
    mapped_teacher_name = serializers.CharField(source="profile.mapped_teacher.name", read_only=True, allow_null=True)

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ["password"]

    def validate(self, attrs):
        initial_data = getattr(self, "initial_data", {}) or {}
        profile_data = attrs.get("profile", {})
        incoming_role = attrs.get("role")
        incoming_organization = attrs.get("organization") if "organization" in attrs else serializers.empty
        mapped_teacher = profile_data.get("mapped_teacher", serializers.empty)
        role = attrs.get("role", getattr(self.instance, "role", None))
        organization = attrs.get("organization", getattr(self.instance, "organization", None))
        target_is_superuser = getattr(self.instance, "is_superuser", False)
        request = self.context.get("request")

        if self.instance is None:
            raise serializers.ValidationError({"detail": "Direct user creation is disabled. Use the whitelist invite flow instead."})

        if "password" in initial_data:
            raise serializers.ValidationError({
                "password": "Direct password changes are disabled. Use the password reset flow instead.",
            })

        if self.instance is not None:
            if incoming_role is not None and incoming_role != self.instance.role:
                raise serializers.ValidationError(
                    {"role": "Changing a user's role via this endpoint is not supported. Create or whitelist a new user instead."}
                )
            if incoming_organization is not serializers.empty and incoming_organization != self.instance.organization:
                raise serializers.ValidationError(
                    {"organization_id": "Changing a user's organization via this endpoint is not supported."}
                )

        if request and request.user.is_authenticated and not request.user.is_superuser:
            if role == User.Role.ADMIN:
                raise serializers.ValidationError({"role": "Only a platform super admin can create or update school admin users."})
            if request.user.organization_id is None:
                raise serializers.ValidationError({"organization_id": "Your admin account is not assigned to a school."})
            if incoming_organization is not serializers.empty and incoming_organization != request.user.organization:
                raise serializers.ValidationError({"organization_id": "School admins can only manage users inside their own school."})
            attrs["organization"] = request.user.organization
            organization = request.user.organization

        if target_is_superuser:
            if role != User.Role.ADMIN:
                raise serializers.ValidationError({"role": "Platform super admins must keep the admin role."})
            if organization is not None:
                raise serializers.ValidationError({"organization_id": "Platform super admins cannot be assigned to a school."})

        if role == User.Role.ADMIN and any(
            value not in {None, ""}
            for value in [profile_data.get("grade"), profile_data.get("section")]
        ):
            raise serializers.ValidationError({"grade": "Admin users do not support grade/section assignments."})
        if role != User.Role.STUDENT and mapped_teacher not in (serializers.empty, None):
            raise serializers.ValidationError({"mapped_teacher_id": "Only student users can be mapped to a teacher."})
        if role == User.Role.ADMIN and mapped_teacher not in (serializers.empty, None):
            raise serializers.ValidationError({"mapped_teacher_id": "Admin users cannot be mapped to a teacher."})

        if role in {User.Role.ADMIN, User.Role.PROFESSOR, User.Role.STUDENT} and not target_is_superuser and organization is None:
            raise serializers.ValidationError(
                {"organization_id": "School admin, professor, and student users must belong to an organization."}
            )

        if mapped_teacher not in (serializers.empty, None):
            if organization is None or mapped_teacher.organization_id != organization.id:
                raise serializers.ValidationError({"mapped_teacher_id": "Mapped teacher must belong to the same organization."})
        return attrs

    def _save_profile(self, user, profile_data):
        if not profile_data and user.role not in {User.Role.PROFESSOR, User.Role.STUDENT}:
            return

        profile, _ = UserProfile.objects.get_or_create(
            user=user,
            defaults={"full_name": user.name or ""},
        )
        update_fields = []

        if not profile.full_name and user.name:
            profile.full_name = user.name
            update_fields.append("full_name")

        for field in ("grade", "section"):
            if field in profile_data:
                setattr(profile, field, profile_data[field])
                update_fields.append(field)

        if user.role == User.Role.STUDENT:
            if "mapped_teacher" in profile_data:
                profile.mapped_teacher = profile_data.get("mapped_teacher")
                update_fields.append("mapped_teacher")
            elif profile.mapped_teacher_id and profile.mapped_teacher.organization_id != user.organization_id:
                profile.mapped_teacher = None
                update_fields.append("mapped_teacher")
            if user.organization_id and not profile.student_identifier:
                profile.student_identifier = UserProfile.generate_student_id(user.organization_id)
                update_fields.append("student_identifier")
        elif profile.mapped_teacher_id is not None:
            profile.mapped_teacher = None
            update_fields.append("mapped_teacher")

        if update_fields:
            profile.save(update_fields=list(dict.fromkeys(update_fields)))

    def create(self, validated_data):
        raise serializers.ValidationError({"detail": "Direct user creation is disabled. Use the whitelist invite flow instead."})

    def update(self, instance, validated_data):
        profile_data = validated_data.pop("profile", {})
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        self._save_profile(instance, profile_data)
        return instance


class ClassroomStudentSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserModel
        fields = ["id", "email", "name"]


class ClassroomListSerializer(serializers.ModelSerializer):
    professor = UserSummarySerializer(read_only=True)
    student_count = serializers.IntegerField(read_only=True)
    subject = SubjectSummarySerializer(read_only=True)

    class Meta:
        model = Classroom
        fields = [
            "id",
            "name",
            "grade",
            "section",
            "subject",
            "invite_code",
            "professor",
            "student_count",
            "is_active",
            "created_at",
            "updated_at",
        ]


class ClassroomDetailSerializer(ClassroomListSerializer):
    students = ClassroomStudentSerializer(many=True, read_only=True)

    class Meta(ClassroomListSerializer.Meta):
        fields = ClassroomListSerializer.Meta.fields + ["students"]


class ClassroomWriteSerializer(serializers.ModelSerializer):
    subject_id = serializers.PrimaryKeyRelatedField(source="subject", queryset=Subject.objects.filter(is_active=True))

    class Meta:
        model = Classroom
        fields = ["id", "name", "subject_id", "is_active"]
        read_only_fields = ["id"]

    def validate(self, attrs):
        request = self.context.get("request")
        teacher = getattr(request, "user", None)
        subject = attrs.get("subject", getattr(self.instance, "subject", None))
        if teacher and teacher.is_authenticated:
            profile = getattr(teacher, "profile", None)
            if profile is None or not profile.section:
                raise serializers.ValidationError(
                    {"section": "Your section is managed by the school admin. Ask the school admin to assign your class/section before creating classrooms."}
                )
            if subject and teacher.organization:
                if not teacher.organization.boards.filter(pk=subject.board_id).exists():
                    raise serializers.ValidationError({"subject_id": "This subject board is not enabled for your school."})
                if not teacher.organization.grades.filter(pk=subject.grade_id).exists():
                    raise serializers.ValidationError({"subject_id": "This subject grade is not enabled for your school."})
        return attrs

    def create(self, validated_data):
        teacher = self.context["request"].user
        profile = teacher.profile
        subject = validated_data["subject"]
        validated_data["grade"] = subject.grade.name
        validated_data["section"] = profile.section
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if "subject" in validated_data:
            instance.grade = validated_data["subject"].grade.name
        return super().update(instance, validated_data)


class LectureTranslationSerializer(serializers.ModelSerializer):
    class Meta:
        model = LectureTranslation
        fields = ["id", "language_code", "translated_text", "pdf_url", "created_at"]


class SummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Summary
        fields = ["summary_text", "vector_store_path", "created_at"]


class FlowChartSerializer(serializers.ModelSerializer):
    class Meta:
        model = FlowChart
        fields = ["mermaid_code", "node_details", "created_at"]


class MindMapSerializer(serializers.ModelSerializer):
    class Meta:
        model = MindMap
        fields = ["mermaid_code", "node_details", "created_at"]


class InteractiveVisualizerSerializer(serializers.ModelSerializer):
    class Meta:
        model = InteractiveVisualizer
        fields = [
            "id",
            "organization",
            "lecture",
            "requested_by",
            "prompt",
            "generated_code",
            "code_type",
            "model_name",
            "metadata",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "requested_by",
            "generated_code",
            "code_type",
            "model_name",
            "metadata",
            "created_at",
            "updated_at",
        ]


class InteractiveVisualizerCreateSerializer(serializers.Serializer):
    prompt = serializers.CharField(max_length=8000)
    lecture_id = serializers.IntegerField(required=False, min_value=1)


class LecturePipelineRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = LecturePipelineRun
        fields = [
            "id",
            "task_id",
            "current_task_id",
            "status",
            "current_stage",
            "progress",
            "error_message",
            "metadata",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        ]


class QuizQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuizQuestion
        fields = [
            "id",
            "question_text",
            "options",
            "correct_answer",
            "explanation",
            "difficulty",
            "order",
        ]


class StudentQuizQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuizQuestion
        fields = ["id", "question_text", "options", "difficulty", "order"]


class QuizSummarySerializer(serializers.ModelSerializer):
    question_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Quiz
        fields = ["id", "title", "question_count", "is_published", "created_at", "updated_at"]


class QuizSerializer(serializers.ModelSerializer):
    question_count = serializers.SerializerMethodField()
    questions = QuizQuestionSerializer(many=True, read_only=True)

    class Meta:
        model = Quiz
        fields = [
            "id",
            "title",
            "lecture",
            "classroom",
            "is_published",
            "question_count",
            "questions",
            "created_at",
            "updated_at",
        ]

    def get_question_count(self, obj):
        return getattr(obj, "annotated_question_count", None) or obj.question_count


class StudentQuizSerializer(serializers.ModelSerializer):
    question_count = serializers.SerializerMethodField()
    questions = StudentQuizQuestionSerializer(many=True, read_only=True)

    class Meta:
        model = Quiz
        fields = [
            "id",
            "title",
            "lecture",
            "classroom",
            "is_published",
            "question_count",
            "questions",
            "created_at",
            "updated_at",
        ]

    def get_question_count(self, obj):
        return getattr(obj, "annotated_question_count", None) or obj.question_count


class QuizWriteSerializer(serializers.ModelSerializer):
    questions = QuizQuestionSerializer(many=True, required=False)

    class Meta:
        model = Quiz
        fields = ["id", "title", "is_published", "questions"]
        read_only_fields = ["id"]

    @transaction.atomic
    def create(self, validated_data):
        questions_data = validated_data.pop("questions", [])
        quiz = Quiz.objects.create(**validated_data)
        QuizQuestion.objects.bulk_create(
            [QuizQuestion(quiz=quiz, **question_data) for question_data in questions_data]
        )
        return quiz

    def update(self, instance, validated_data):
        instance.title = validated_data.get("title", instance.title)
        instance.is_published = validated_data.get("is_published", instance.is_published)
        instance.is_active = validated_data.get("is_active", instance.is_active)
        instance.save()
        return instance


class LectureListSerializer(serializers.ModelSerializer):
    classroom_id = serializers.IntegerField(source="classroom_id", read_only=True)
    quiz_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Lecture
        fields = [
            "id",
            "title",
            "description",
            "classroom_id",
            "video_url",
            "processing_status",
            "processing_error",
            "quiz_count",
            "is_active",
            "created_at",
            "updated_at",
        ]


class LectureDetailSerializer(serializers.ModelSerializer):
    uploaded_by = UserSummarySerializer(read_only=True)
    translations = LectureTranslationSerializer(many=True, read_only=True)
    summary = SummarySerializer(read_only=True)
    flow_chart = FlowChartSerializer(read_only=True)
    mind_map = MindMapSerializer(read_only=True)
    quizzes = QuizSummarySerializer(many=True, read_only=True)
    latest_pipeline_run = serializers.SerializerMethodField()

    class Meta:
        model = Lecture
        fields = [
            "id",
            "title",
            "description",
            "classroom",
            "video_url",
            "video_file",
            "original_transcript",
            "whiteboard_notes",
            "processing_status",
            "processing_error",
            "uploaded_by",
            "translations",
            "summary",
            "flow_chart",
            "mind_map",
            "quizzes",
            "latest_pipeline_run",
            "is_active",
            "created_at",
            "updated_at",
        ]

    def get_latest_pipeline_run(self, obj):
        prefetched_runs = getattr(obj, "prefetched_pipeline_runs", None)
        if prefetched_runs is not None:
            run = prefetched_runs[0] if prefetched_runs else None
        else:
            run = obj.pipeline_runs.order_by("-created_at").first()
        if run is None:
            return None
        return LecturePipelineRunSerializer(run).data


class StudentLectureDetailSerializer(LectureDetailSerializer):
    quizzes = QuizSummarySerializer(many=True, read_only=True)


class LectureWriteSerializer(serializers.ModelSerializer):
    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance:
            video_file = attrs.get("video_file", self.instance.video_file)
            video_url = attrs.get("video_url", self.instance.video_url)
        else:
            video_file = attrs.get("video_file")
            video_url = attrs.get("video_url")

        if not video_file and not video_url:
            raise serializers.ValidationError(
                {"video_file": "Provide either video_file upload or video_url."}
            )
        return attrs

    class Meta:
        model = Lecture
        fields = [
            "id",
            "title",
            "description",
            "video_file",
            "video_url",
            "whiteboard_notes",
            "processing_status",
            "processing_error",
            "is_active",
        ]
        read_only_fields = ["id", "processing_status", "processing_error"]


class EnrollStudentsSerializer(serializers.Serializer):
    student_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )


class InviteCodeEnrollmentSerializer(serializers.Serializer):
    invite_code = serializers.CharField(max_length=20)


class QuizSubmissionSerializer(serializers.Serializer):
    answers = serializers.DictField(child=serializers.IntegerField(min_value=0))
    started_at = serializers.DateTimeField()


class TrackProgressSerializer(serializers.Serializer):
    timestamp_seconds = serializers.IntegerField(min_value=0)
    duration_seconds = serializers.IntegerField(min_value=1, required=False, allow_null=True)


class LectureChatSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=4000)


class LogoutSerializer(serializers.Serializer):
    refresh_token = serializers.CharField(required=False, allow_blank=True)


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    username_field = UserModel.USERNAME_FIELD

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        is_profile_complete = True if user.role == User.Role.ADMIN else user.is_profile_complete
        token["email"] = user.email
        token["name"] = user.name
        token["role"] = user.role
        token["organization_id"] = user.organization_id
        token["is_superuser"] = user.is_superuser
        token["is_profile_complete"] = is_profile_complete
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        if self.user.role == User.Role.ADMIN and not self.user.is_profile_complete:
            self.user.is_profile_complete = True
            self.user.save(update_fields=["is_profile_complete", "updated_at"])
        data["access_token"] = data.pop("access")
        data["refresh_token"] = data.pop("refresh")
        data["user"] = UserSerializer(self.user).data
        data["is_profile_complete"] = True if self.user.role == User.Role.ADMIN else self.user.is_profile_complete
        return data


# ── Auth flow serializers ──

class RequestOtpSerializer(serializers.Serializer):
    email = serializers.EmailField()
    organization_slug = serializers.CharField(max_length=255, required=False)


class VerifyOtpSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp_code = serializers.CharField(min_length=6, max_length=6)
    organization_slug = serializers.CharField(max_length=255, required=False)


class SetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(min_length=8)
    confirm_password = serializers.CharField(min_length=8)

    def validate(self, attrs):
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})
        validate_password_or_raise(attrs["password"], field_name="password")
        return attrs


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp_code = serializers.CharField(min_length=6, max_length=6)
    new_password = serializers.CharField(min_length=8)

    def validate(self, attrs):
        validate_password_or_raise(attrs["new_password"], field_name="new_password")
        return attrs


class ResendOtpSerializer(serializers.Serializer):
    email = serializers.EmailField()
    organization_slug = serializers.CharField(max_length=255, required=False)
    purpose = serializers.ChoiceField(
        choices=["verify", "reset"],
        default="verify",
        required=False,
    )


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField()
    organization_slug = serializers.CharField(max_length=255, required=False)


# ── Whitelist serializers ──

class OrganizationScopedWhitelistSerializer(serializers.Serializer):
    email = serializers.EmailField()
    organization_id = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        request = self.context.get("request")
        if request and request.user.is_authenticated and not request.user.is_superuser:
            if request.user.organization_id is None:
                raise serializers.ValidationError({"organization_id": "Your admin account is not assigned to a school."})
            attrs["organization_id"] = request.user.organization_id
        elif not attrs.get("organization_id"):
            raise serializers.ValidationError({"organization_id": "Organization is required."})
        return attrs


class WhitelistSchoolAdminSerializer(OrganizationScopedWhitelistSerializer):
    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get("request")
        if not request or not request.user.is_authenticated or not request.user.is_superuser:
            raise serializers.ValidationError({"detail": "Only the platform super admin can whitelist school admin users."})
        return attrs


class WhitelistTeacherSerializer(OrganizationScopedWhitelistSerializer):
    grade = serializers.CharField(max_length=50, required=False, allow_blank=True)
    section = serializers.CharField(max_length=50, required=False, allow_blank=True)


class AdminWhitelistStudentSerializer(OrganizationScopedWhitelistSerializer):
    grade = serializers.CharField(max_length=50, required=False, allow_blank=True)
    section = serializers.CharField(max_length=50, required=False, allow_blank=True)


class WhitelistStudentSerializer(serializers.Serializer):
    email = serializers.EmailField()


class BulkWhitelistStudentSerializer(serializers.Serializer):
    emails = serializers.ListField(child=serializers.EmailField(), allow_empty=False)


# ── Profile serializers ──

class UserProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    role = serializers.CharField(source="user.role", read_only=True)
    organization_name = serializers.CharField(source="user.organization.name", read_only=True)
    is_profile_complete = serializers.BooleanField(source="user.is_profile_complete", read_only=True)
    teacher_name = serializers.CharField(source="mapped_teacher.name", read_only=True)

    class Meta:
        model = None  # handled in __init__
        fields = [
            "id",
            "email",
            "role",
            "organization_name",
            "full_name",
            "phone_number",
            "student_identifier",
            "grade",
            "section",
            "teacher_name",
            "mapped_teacher",
            "is_profile_complete",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "email",
            "role",
            "organization_name",
            "student_identifier",
            "teacher_name",
            "is_profile_complete",
            "created_at",
            "updated_at",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.Meta.model = __import__("api.models", fromlist=["UserProfile"]).UserProfile
        instance = kwargs.get("instance")
        if instance is None:
            return
        user = getattr(instance, "user", None)
        if not user:
            return
        if user.role == "student":
            self.fields["grade"].read_only = True
            self.fields["section"].read_only = True
            self.fields["mapped_teacher"].read_only = True
        elif user.role == "professor" and user.is_profile_complete:
            self.fields["grade"].read_only = True
            self.fields["section"].read_only = True

    def validate(self, attrs):
        user = getattr(getattr(self, "instance", None), "user", None)
        initial_data = getattr(self, "initial_data", {}) or {}
        if user and user.role in {User.Role.ADMIN, User.Role.PROFESSOR, User.Role.STUDENT}:
            forbidden_fields = {"grade", "section", "mapped_teacher"} & set(initial_data.keys())
            if forbidden_fields:
                raise serializers.ValidationError(
                    {field: "This field is managed by the school admin/onboarding flow." for field in forbidden_fields}
                )
        return attrs


# ── Classroom orchestration serializers ──

class ClassroomSubjectTeacherSerializer(serializers.ModelSerializer):
    teacher_name = serializers.CharField(source="teacher.name", read_only=True)
    teacher_email = serializers.EmailField(source="teacher.email", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    board_name = serializers.CharField(source="subject.board.name", read_only=True)
    grade_name = serializers.CharField(source="subject.grade.name", read_only=True)

    class Meta:
        model = None
        fields = [
            "id",
            "classroom",
            "teacher",
            "teacher_name",
            "teacher_email",
            "subject",
            "subject_name",
            "board_name",
            "grade_name",
            "assigned_at",
        ]
        read_only_fields = ["id", "assigned_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.Meta.model = __import__("api.models", fromlist=["ClassroomSubjectTeacher"]).ClassroomSubjectTeacher


class AssignSubjectTeacherSerializer(serializers.Serializer):
    teacher_id = serializers.IntegerField(min_value=1)
    subject_id = serializers.IntegerField(min_value=1)


class AvailableTeacherSerializer(serializers.ModelSerializer):
    grade = serializers.CharField(source="profile.grade", read_only=True)
    section = serializers.CharField(source="profile.section", read_only=True)

    class Meta:
        model = User
        fields = ["id", "name", "email", "grade", "section"]


class AuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.name", read_only=True)
    actor_email = serializers.EmailField(source="actor.email", read_only=True)
    target_user_name = serializers.CharField(source="target_user.name", read_only=True)
    target_user_email = serializers.EmailField(source="target_user.email", read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "organization",
            "organization_name",
            "actor",
            "actor_name",
            "actor_email",
            "action",
            "status",
            "target_email",
            "target_user",
            "target_user_name",
            "target_user_email",
            "metadata",
            "created_at",
        ]
