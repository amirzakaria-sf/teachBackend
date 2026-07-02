from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from rest_framework.test import APIClient

from api.models import OTPVerification
from api.tasks import (
    finalize_pipeline_task,
    generate_flowchart_task,
    generate_mindmap_task,
    generate_quiz_task,
    summarize_lecture_task,
    transcribe_lecture_task,
    translate_lecture_task,
)


User = get_user_model()


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default)).strip()


def _derive_email(base_email: str, suffix: str) -> str:
    if "@" not in base_email:
        raise CommandError(f"Invalid base email: {base_email}")
    local, domain = base_email.split("@", 1)
    return f"{local}+{suffix}@{domain}"


@dataclass
class Actor:
    email: str
    password: str
    token: str


class Command(BaseCommand):
    help = "Runs full bilingual E2E flow: super admin -> org -> teachers -> students -> syllabus -> lectures -> pipeline -> cleanup"

    def add_arguments(self, parser):
        parser.add_argument("--keep-org", action="store_true", help="Keep organization at the end (skip delete step).")

    def handle(self, *args, **options):
        timestamp = datetime.now(tz=dt_timezone.utc).strftime("%Y%m%d%H%M%S")
        keep_org = bool(options.get("keep_org"))
        client = APIClient()
        client.defaults["HTTP_HOST"] = "localhost"

        e2e_email = _env("E2E_TEST_EMAIL")
        if not e2e_email:
            raise CommandError("E2E_TEST_EMAIL is required.")

        video_en_url = _env("E2E_TEST_VIDEO_EN_URL") or _env("E2E_TEST_VIDEO_EN_FILE")
        video_hi_url = _env("E2E_TEST_VIDEO_HI_URL") or _env("E2E_TEST_VIDEO_HI_FILE")
        if not video_en_url or not video_hi_url:
            raise CommandError("Both English and Hindi lecture URLs are required in E2E_TEST_VIDEO_EN_URL and E2E_TEST_VIDEO_HI_URL.")
        if not video_en_url.startswith("http") or not video_hi_url.startswith("http"):
            raise CommandError("E2E video inputs must be URL values (http/https).")
        self._assert_cloud_accessible_url(video_en_url, "E2E_TEST_VIDEO_EN_URL")
        self._assert_cloud_accessible_url(video_hi_url, "E2E_TEST_VIDEO_HI_URL")

        teacher_email = _env("E2E_TEST_TEACHER_EMAIL") or _derive_email(e2e_email, f"teacher-{timestamp}")
        teacher2_email = _env("E2E_TEST_TEACHER2_EMAIL") or _derive_email(e2e_email, f"teacher2-{timestamp}")
        student_email = _env("E2E_TEST_STUDENT_EMAIL") or _derive_email(e2e_email, f"student-{timestamp}")

        super_email = _env("E2E_SUPERADMIN_EMAIL", "superadmin@example.com")
        super_password = _env("E2E_SUPERADMIN_PASSWORD", "SuperAdmin@123")
        common_password = _env("E2E_DEFAULT_PASSWORD", "Welcome@123")

        self.stdout.write(self.style.WARNING(f"[E2E] Run ID: {timestamp}"))
        self.stdout.write("[E2E] Ensuring super admin exists...")
        self._ensure_super_admin(super_email, super_password)

        super_actor = self._login(client, email=super_email, password=super_password)
        self._set_auth(client, super_actor.token)

        board_id = self._post(
            client,
            "/api/super-admin/boards/",
            {"name": f"CBSE-E2E-{timestamp}", "description": "E2E board"},
            expected=201,
        )["id"]
        grade_id = self._post(
            client,
            "/api/super-admin/grades/",
            {"name": f"Class 10 E2E {timestamp}", "numeric_value": int(timestamp[-4:])},
            expected=201,
        )["id"]
        subject_id = self._post(
            client,
            "/api/super-admin/subjects/",
            {"name": f"Mathematics Trigonometry {timestamp}", "board": board_id, "grade": grade_id},
            expected=201,
        )["id"]

        org = self._post(
            client,
            "/api/admin/organizations/",
            {
                "name": f"Trigo School {timestamp}",
                "supported_languages": ["en", "hi"],
                "board_ids": [board_id],
                "grade_ids": [grade_id],
            },
            expected=201,
        )
        org_id = org["id"]
        org_slug = org["slug"]

        syllabus_text = (
            "System prompt for Mathematics Trigonometry:\n"
            "- Keep explanations concise and school-level.\n"
            "- Focus on trigonometric ratios, identities, and practical applications.\n"
            "- Avoid out-of-syllabus content.\n"
        )
        syllabus_file = SimpleUploadedFile(
            name="trigonometry_prompt.txt",
            content=syllabus_text.encode("utf-8"),
            content_type="text/plain",
        )
        self._post(
            client,
            "/api/admin/syllabus-documents/",
            {
                "organization": str(org_id),
                "subject": str(subject_id),
                "title": f"Trigonometry Prompt {timestamp}",
                "file": syllabus_file,
            },
            expected=201,
            format="multipart",
        )

        self._post(
            client,
            "/api/admin/whitelist-teacher/",
            {
                "email": teacher_email,
                "organization_id": org_id,
                "grade": "10",
                "section": "A",
            },
            expected=201,
        )
        teacher_actor = self._signup_actor(client, teacher_email, org_slug, common_password, full_name="Teacher One", phone="9000000001")

        self._set_auth(client, super_actor.token)
        self._post(
            client,
            "/api/admin/whitelist-teacher/",
            {
                "email": teacher2_email,
                "organization_id": org_id,
                "grade": "10",
                "section": "A",
            },
            expected=201,
        )
        teacher2_actor = self._signup_actor(client, teacher2_email, org_slug, common_password, full_name="Teacher Two", phone="9000000002")

        self._set_auth(client, teacher_actor.token)
        classroom = self._post(
            client,
            "/api/professor/classrooms/",
            {
                "name": f"10A Trigonometry {timestamp}",
                "grade": "10",
                "section": "A",
                "subject_id": subject_id,
            },
            expected=201,
        )
        classroom_id = classroom["id"]
        classroom_detail = self._get(client, f"/api/professor/classrooms/{classroom_id}/")
        invite_code = classroom_detail["invite_code"]

        teacher2_user = User.objects.get(email=teacher2_email)
        self._post(
            client,
            f"/api/teacher/classrooms/{classroom_id}/subject-teachers/",
            {
                "teacher_id": teacher2_user.id,
                "subject_id": subject_id,
            },
            expected=201,
        )

        self._post(
            client,
            "/api/teacher/whitelist-student/",
            {"email": student_email},
            expected=201,
        )
        student_actor = self._signup_actor(
            client,
            student_email,
            org_slug,
            common_password,
            full_name="Student One",
            phone="9000000003",
            is_student=True,
        )

        self._set_auth(client, student_actor.token)
        self._post(client, "/api/student/enroll/", {"invite_code": invite_code}, expected=200)

        self._set_auth(client, teacher_actor.token)
        lecture_en = self._post(
            client,
            f"/api/professor/classrooms/{classroom_id}/lectures/",
            {
                "title": f"Trigonometry English {timestamp}",
                "description": "English lecture",
                "video_url": video_en_url,
            },
            expected=201,
        )
        lecture_hi = self._post(
            client,
            f"/api/professor/classrooms/{classroom_id}/lectures/",
            {
                "title": f"Trigonometry Hindi {timestamp}",
                "description": "Hindi lecture",
                "video_url": video_hi_url,
            },
            expected=201,
        )

        self._run_pipeline(client, lecture_en["id"], teacher_actor)
        self._run_pipeline(client, lecture_hi["id"], teacher_actor)

        for lecture_id in (lecture_en["id"], lecture_hi["id"]):
            quizzes = self._get(client, f"/api/professor/lectures/{lecture_id}/quizzes/")
            if not quizzes:
                raise CommandError(f"No quizzes generated for lecture {lecture_id}")
            quiz_id = quizzes[0]["id"]
            self._post(client, f"/api/professor/quizzes/{quiz_id}/publish/", {}, expected=200)

        self._set_auth(client, student_actor.token)
        dashboard = self._get(client, "/api/student/dashboard/")
        if not dashboard.get("enrolled_classrooms"):
            raise CommandError("Student dashboard has no enrolled classrooms after enrollment.")

        for lecture_id in (lecture_en["id"], lecture_hi["id"]):
            self._get(client, f"/api/student/lectures/{lecture_id}/")
            self._get(client, f"/api/student/lectures/{lecture_id}/transcript/?lang=en")
            self._get(client, f"/api/student/lectures/{lecture_id}/transcript/?lang=hi")
            self._post(
                client,
                f"/api/student/lectures/{lecture_id}/track-progress/",
                {"timestamp_seconds": 120, "duration_seconds": 600},
                expected=200,
            )
            self._post(
                client,
                f"/api/student/lectures/{lecture_id}/chat/",
                {"message": "Explain the sine rule in simple terms."},
                expected=200,
            )

        quizzes = self._get(client, f"/api/student/lectures/{lecture_en['id']}/quizzes/")
        if quizzes:
            quiz_id = quizzes[0]["id"]
            quiz_detail = self._get(client, f"/api/student/quizzes/{quiz_id}/")
            answers = {str(item["id"]): 0 for item in quiz_detail.get("questions", [])}
            self._post(
                client,
                f"/api/student/quizzes/{quiz_id}/submit/",
                {"answers": answers, "started_at": datetime.now(tz=dt_timezone.utc).isoformat()},
                expected=201,
            )

        self._set_auth(client, super_actor.token)
        self._get(client, "/api/admin/analytics/platform/")
        logs = self._get(client, "/api/admin/audit-logs/?action=whitelist")
        if not logs.get("results"):
            raise CommandError("Audit logs missing whitelist actions.")

        if keep_org:
            self.stdout.write(self.style.WARNING(f"[E2E] --keep-org active. Organization {org_id} preserved."))
        else:
            self._delete(client, f"/api/admin/organizations/{org_id}/", expected=204)

        self.stdout.write(self.style.SUCCESS("[E2E] Full end-to-end flow completed successfully."))

    def _ensure_super_admin(self, email: str, password: str):
        user = User.objects.filter(email=email).first()
        if user is None:
            User.objects.create_superuser(email=email, password=password, name="E2E Super Admin")
            return
        changed = False
        if not user.is_superuser:
            user.is_superuser = True
            changed = True
        if not user.is_staff:
            user.is_staff = True
            changed = True
        if user.role != User.Role.ADMIN:
            user.role = User.Role.ADMIN
            changed = True
        user.set_password(password)
        changed = True
        if changed:
            user.save()

    def _signup_actor(
        self,
        client: APIClient,
        email: str,
        org_slug: str,
        password: str,
        *,
        full_name: str,
        phone: str,
        is_student: bool = False,
    ) -> Actor:
        self._post(
            client,
            "/api/auth/request-otp/",
            {"email": email, "organization_slug": org_slug},
            expected=200,
        )

        otp = OTPVerification.objects.filter(email=email, purpose=OTPVerification.Purpose.VERIFY).order_by("-created_at").first()
        if otp is None:
            raise CommandError(f"OTP was not generated for {email}")

        verify_code = str(getattr(settings, "FIXED_TEST_OTP", "") or "").strip()
        if len(verify_code) != 6 or not verify_code.isdigit():
            raise CommandError("FIXED_TEST_OTP must be configured to run the E2E signup flow securely.")

        verify = self._post(
            client,
            "/api/auth/verify-otp/",
            {"email": email, "otp_code": verify_code},
            expected=200,
        )
        setup_token = verify["setup_token"]
        self._post(
            client,
            "/api/auth/set-password/",
            {"password": password, "confirm_password": password},
            expected=200,
            token=setup_token,
        )
        actor = self._login(client, email=email, password=password)
        self._set_auth(client, actor.token)
        profile_payload = {"full_name": full_name, "phone_number": phone}
        if not is_student:
            profile_payload.update({"grade": "10", "section": "A"})
        self._patch(client, "/api/users/profile/", profile_payload, expected=200)
        return actor

    def _run_pipeline(self, client: APIClient, lecture_id: int, actor: Actor):
        refreshed = self._login(client, email=actor.email, password=actor.password)
        actor.token = refreshed.token
        self._set_auth(client, actor.token)
        trigger = self._post(client, f"/api/professor/lectures/{lecture_id}/trigger-pipeline/", {}, expected=202)
        pipeline_run_id = int(trigger["pipeline_run"]["id"])
        payload = transcribe_lecture_task.apply(args=[pipeline_run_id]).get()
        payload = translate_lecture_task.apply(args=[payload]).get()
        payload = summarize_lecture_task.apply(args=[payload]).get()
        payload = generate_flowchart_task.apply(args=[payload]).get()
        payload = generate_mindmap_task.apply(args=[payload]).get()
        payload = generate_quiz_task.apply(args=[payload]).get()
        finalize_pipeline_task.apply(args=[payload]).get()
        refreshed = self._login(client, email=actor.email, password=actor.password)
        actor.token = refreshed.token
        self._set_auth(client, actor.token)
        status_payload = self._get(client, f"/api/professor/lectures/{lecture_id}/pipeline-status/")
        if status_payload.get("status") not in {"completed", "success"}:
            raise CommandError(f"Pipeline failed for lecture {lecture_id}: {status_payload}")

    def _set_auth(self, client: APIClient, token: str):
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _login(self, client: APIClient, *, email: str, password: str) -> Actor:
        self._post(client, "/api/auth/login/", {"email": email, "password": password}, expected=200)
        access_cookie = client.cookies.get(settings.AUTH_ACCESS_COOKIE_NAME)
        if access_cookie is None or not access_cookie.value:
            raise CommandError("Login did not issue an access token cookie.")
        return Actor(email=email, password=password, token=access_cookie.value)

    def _get(self, client: APIClient, path: str):
        response = client.get(path)
        if response.status_code != 200:
            raise CommandError(f"GET {path} failed: {response.status_code} {response.content!r}")
        return response.json()

    def _post(self, client: APIClient, path: str, data: dict, *, expected: int, format: str = "json", token: str | None = None):
        original_credentials = dict(client._credentials)
        if token:
            client.credentials(HTTP_X_SETUP_TOKEN=token)
        response = client.post(path, data, format=format)
        if token:
            client._credentials = original_credentials
        if response.status_code != expected:
            raise CommandError(f"POST {path} failed: expected {expected}, got {response.status_code}, body={response.content!r}")
        if expected == 204:
            return {}
        return response.json() if response.content else {}

    def _patch(self, client: APIClient, path: str, data: dict, *, expected: int):
        response = client.patch(path, data, format="json")
        if response.status_code != expected:
            raise CommandError(f"PATCH {path} failed: {response.status_code} {response.content!r}")
        return response.json() if response.content else {}

    def _delete(self, client: APIClient, path: str, *, expected: int):
        with transaction.atomic():
            response = client.delete(path)
            if response.status_code != expected:
                raise CommandError(f"DELETE {path} failed: {response.status_code} {response.content!r}")
        return {}

    def _assert_cloud_accessible_url(self, value: str, env_name: str):
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
        if hostname in {"localhost", "127.0.0.1", "0.0.0.0"}:
            raise CommandError(
                f"{env_name}={value} is not cloud-accessible. Azure Speech cannot pull localhost URLs. "
                "Use a public HTTPS URL (or blob SAS URL)."
            )
