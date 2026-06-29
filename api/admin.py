from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

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
)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "name", "role", "organization", "is_active", "is_staff")
    search_fields = ("email", "name")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("name", "role", "organization")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login",)}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "name", "role", "organization", "password1", "password2"),
            },
        ),
    )


admin.site.register(Organization)
admin.site.register(Board)
admin.site.register(Grade)
admin.site.register(Subject)
admin.site.register(Classroom)
admin.site.register(ClassroomEnrollment)
admin.site.register(Lecture)
admin.site.register(LecturePipelineRun)
admin.site.register(LectureTranslation)
admin.site.register(Summary)
admin.site.register(FlowChart)
admin.site.register(MindMap)
admin.site.register(SyllabusDocument)
admin.site.register(InteractiveVisualizer)
admin.site.register(Quiz)
admin.site.register(QuizQuestion)
admin.site.register(StudentQuizAttempt)
admin.site.register(QuizAttemptAnswer)
admin.site.register(LectureProgress)
admin.site.register(AuditLog)
