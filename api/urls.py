from django.urls import path

from api import auth_views, views


urlpatterns = [
    path("auth/login/", views.CustomTokenObtainPairView.as_view(), name="auth-login"),
    path("auth/token/refresh/", views.CookieTokenRefreshView.as_view(), name="auth-token-refresh"),
    path("auth/logout/", views.LogoutView.as_view(), name="auth-logout"),
    path("auth/session/", views.AuthSessionView.as_view(), name="auth-session"),

    # ── Auth flow (signup, forgot/reset password) ──
    path("auth/request-otp/", auth_views.RequestOtpView.as_view(), name="auth-request-otp"),
    path("auth/resend-otp/", auth_views.ResendOtpView.as_view(), name="auth-resend-otp"),
    path("auth/verify-otp/", auth_views.VerifyOtpView.as_view(), name="auth-verify-otp"),
    path("auth/set-password/", auth_views.SetPasswordView.as_view(), name="auth-set-password"),
    path("auth/forgot-password/", auth_views.ForgotPasswordView.as_view(), name="auth-forgot-password"),
    path("auth/reset-password/", auth_views.ResetPasswordView.as_view(), name="auth-reset-password"),

    # ── Profile ──
    path("users/profile/", views.UserProfileView.as_view(), name="user-profile"),

    # ── Admin ──
    path("admin/whitelist-school-admin/", views.AdminWhitelistSchoolAdminView.as_view(), name="admin-whitelist-school-admin"),
    path("admin/whitelist-teacher/", views.AdminWhitelistTeacherView.as_view(), name="admin-whitelist-teacher"),
    path("admin/whitelist-student/", views.AdminWhitelistStudentView.as_view(), name="admin-whitelist-student"),
    path("admin/organizations/", views.AdminOrganizationListCreateView.as_view(), name="admin-organizations"),
    path("admin/organizations/<int:org_id>/", views.AdminOrganizationDetailView.as_view(), name="admin-organization-detail"),
    path("admin/organizations/subjects/", views.AdminOrganizationSubjectListView.as_view(), name="admin-organization-subjects"),
    path("admin/users/", views.AdminUserListCreateView.as_view(), name="admin-users"),
    path("admin/users/<int:user_id>/", views.AdminUserDetailView.as_view(), name="admin-user-detail"),
    path("admin/syllabus-documents/", views.AdminSyllabusDocumentListCreateView.as_view(), name="admin-syllabus-documents"),
    path("admin/syllabus-documents/<int:document_id>/", views.AdminSyllabusDocumentDetailView.as_view(), name="admin-syllabus-document-detail"),
    path("admin/analytics/platform/", views.PlatformAnalyticsView.as_view(), name="admin-platform-analytics"),
    path("admin/audit-logs/", views.AdminAuditLogListView.as_view(), name="admin-audit-logs"),

    path("super-admin/boards/", views.SuperAdminBoardListCreateView.as_view(), name="super-admin-boards"),
    path("super-admin/boards/<int:board_id>/", views.SuperAdminBoardDetailView.as_view(), name="super-admin-board-detail"),
    path("super-admin/grades/", views.SuperAdminGradeListCreateView.as_view(), name="super-admin-grades"),
    path("super-admin/grades/<int:grade_id>/", views.SuperAdminGradeDetailView.as_view(), name="super-admin-grade-detail"),
    path("super-admin/subjects/", views.SuperAdminSubjectListCreateView.as_view(), name="super-admin-subjects"),
    path("super-admin/subjects/<int:subject_id>/", views.SuperAdminSubjectDetailView.as_view(), name="super-admin-subject-detail"),

    # ── Professor ──
    path("teacher/whitelist-student/", views.TeacherWhitelistStudentView.as_view(), name="teacher-whitelist-student"),
    path("teacher/whitelist-student/bulk/", views.TeacherBulkWhitelistStudentView.as_view(), name="teacher-whitelist-student-bulk"),
    path("teacher/available-subjects/", views.TeacherAvailableSubjectsView.as_view(), name="teacher-available-subjects"),
    path("teacher/available-teachers/", views.TeacherAvailableTeachersView.as_view(), name="teacher-available-teachers"),
    path("teacher/classrooms/<int:classroom_id>/subject-teachers/", views.ClassroomSubjectTeacherListView.as_view(), name="teacher-classroom-subject-teachers"),
    path("teacher/classrooms/<int:classroom_id>/subject-teachers/<int:st_id>/", views.ClassroomSubjectTeacherRemoveView.as_view(), name="teacher-classroom-subject-teacher-remove"),
    path("professor/classrooms/", views.ProfessorClassroomListCreateView.as_view(), name="professor-classrooms"),
    path("professor/classrooms/<int:classroom_id>/", views.ProfessorClassroomDetailView.as_view(), name="professor-classroom-detail"),
    path("professor/classrooms/<int:classroom_id>/enroll/", views.ProfessorClassroomEnrollView.as_view(), name="professor-classroom-enroll"),
    path("professor/classrooms/<int:classroom_id>/enroll/<int:student_id>/", views.ProfessorClassroomRemoveStudentView.as_view(), name="professor-classroom-remove-student"),
    path("professor/classrooms/<int:classroom_id>/lectures/", views.ProfessorClassroomLectureListCreateView.as_view(), name="professor-classroom-lectures"),
    path("professor/classrooms/<int:classroom_id>/analytics/", views.ProfessorClassroomAnalyticsView.as_view(), name="professor-classroom-analytics"),
    path("professor/lectures/<int:lecture_id>/", views.ProfessorLectureDetailView.as_view(), name="professor-lecture-detail"),
    path("professor/lectures/<int:lecture_id>/trigger-pipeline/", views.ProfessorLectureTriggerPipelineView.as_view(), name="professor-lecture-trigger-pipeline"),
    path("professor/lectures/<int:lecture_id>/pipeline-status/", views.ProfessorLecturePipelineStatusView.as_view(), name="professor-lecture-pipeline-status"),
    path("professor/lectures/<int:lecture_id>/quizzes/", views.ProfessorLectureQuizListCreateView.as_view(), name="professor-lecture-quizzes"),
    path("professor/lectures/<int:lecture_id>/analytics/", views.ProfessorLectureAnalyticsView.as_view(), name="professor-lecture-analytics"),
    path("professor/quizzes/<int:quiz_id>/", views.ProfessorQuizDetailView.as_view(), name="professor-quiz-detail"),
    path("professor/quizzes/<int:quiz_id>/questions/<int:qid>/", views.ProfessorQuizQuestionDetailView.as_view(), name="professor-quiz-question-detail"),
    path("professor/quizzes/<int:quiz_id>/publish/", views.ProfessorQuizPublishView.as_view(), name="professor-quiz-publish"),

    # ── Student ──
    path("student/dashboard/", views.StudentDashboardView.as_view(), name="student-dashboard"),
    path("student/enroll/", views.StudentEnrollView.as_view(), name="student-enroll"),
    path("student/classrooms/<int:classroom_id>/", views.StudentClassroomDetailView.as_view(), name="student-classroom-detail"),
    path("student/classrooms/<int:classroom_id>/lectures/", views.StudentClassroomLectureListView.as_view(), name="student-classroom-lectures"),
    path("student/lectures/<int:lecture_id>/", views.StudentLectureDetailView.as_view(), name="student-lecture-detail"),
    path("student/lectures/<int:lecture_id>/transcript/", views.StudentLectureTranscriptView.as_view(), name="student-lecture-transcript"),
    path("student/lectures/<int:lecture_id>/track-progress/", views.StudentLectureProgressView.as_view(), name="student-lecture-track-progress"),
    path("student/lectures/<int:lecture_id>/quizzes/", views.StudentLectureQuizListView.as_view(), name="student-lecture-quizzes"),
    path("student/lectures/<int:lecture_id>/chat/", views.StudentLectureChatView.as_view(), name="student-lecture-chat"),
    path("student/interactive-visualizers/", views.InteractiveVisualizerListView.as_view(), name="student-interactive-visualizers"),
    path("student/interactive-visualizers/generate/", views.InteractiveVisualizerGenerateView.as_view(), name="student-generate-interactive-visualizer"),
    path("student/quizzes/<int:quiz_id>/", views.StudentQuizDetailView.as_view(), name="student-quiz-detail"),
    path("student/quizzes/<int:quiz_id>/submit/", views.StudentQuizSubmitView.as_view(), name="student-quiz-submit"),
]
