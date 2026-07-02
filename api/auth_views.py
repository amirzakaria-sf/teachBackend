from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken, UntypedToken
from rest_framework_simplejwt.exceptions import TokenError

from api.communication.email_service import (
    send_password_reset_otp,
    send_verification_email,
)
from api.auth_utils import revoke_user_refresh_tokens, validate_password_or_raise
from api.models import AuditLog, OTPVerification, Organization, User, UserProfile, WhitelistedEmail
from api.serializers import (
    ForgotPasswordSerializer,
    ResendOtpSerializer,
    RequestOtpSerializer,
    ResetPasswordSerializer,
    SetPasswordSerializer,
    VerifyOtpSerializer,
    CustomTokenObtainPairSerializer,
)
from api.throttles import (
    OtpRequestEmailRateThrottle,
    OtpRequestIPRateThrottle,
    OtpVerifyEmailRateThrottle,
    OtpVerifyIPRateThrottle,
    PasswordResetConfirmEmailRateThrottle,
    PasswordResetConfirmIPRateThrottle,
    PasswordResetRequestEmailRateThrottle,
    PasswordResetRequestIPRateThrottle,
)

logger = logging.getLogger(__name__)
UserModel = get_user_model()

SETUP_TOKEN_LIFETIME = timedelta(minutes=15)
OTP_RESEND_COOLDOWN_SECONDS = 60
SIGNUP_REQUEST_MESSAGE = "If your invitation is valid, a verification code will be sent to your email."
SIGNUP_VERIFY_ERROR = "Invalid or expired verification code. Please request a new code and try again."


def _record_email_delivery(email: str, action: str, sent: bool, *, organization: Organization | None = None, metadata: dict | None = None):
    AuditLog.objects.create(
        organization=organization,
        action=action,
        status=AuditLog.Status.SUCCESS if sent else AuditLog.Status.FAILED,
        target_email=email,
        metadata=metadata or {},
    )


def _cooldown_retry_after(email: str, purpose: str) -> int:
    latest = OTPVerification.objects.filter(email=email, purpose=purpose).order_by("-created_at").first()
    if latest is None:
        return 0
    elapsed = (timezone.now() - latest.created_at).total_seconds()
    if elapsed >= OTP_RESEND_COOLDOWN_SECONDS:
        return 0
    return int(OTP_RESEND_COOLDOWN_SECONDS - elapsed)


def _mask_email(email: str) -> str:
    local_part, _, domain = str(email or "").partition("@")
    if not local_part or not domain:
        return "***"
    masked_local = f"{local_part[0]}***" if len(local_part) > 1 else "*"
    return f"{masked_local}@{domain}"


def _generate_setup_token(email: str, role: str, organization_id: int) -> str:
    """Create a short-lived JWT that encodes the pending user's details."""
    token = AccessToken()
    token["email"] = email
    token["role"] = role
    token["organization_id"] = organization_id
    token["token_type"] = "setup"
    token.set_exp(lifetime=SETUP_TOKEN_LIFETIME)
    return str(token)


def _resolve_pending_whitelist(email: str, organization_slug: str | None = None) -> tuple[WhitelistedEmail | None, str | None]:
    normalized_email = UserModel.objects.normalize_email(email)
    queryset = WhitelistedEmail.objects.select_related("organization", "created_by").filter(
        email=normalized_email,
        is_used=False,
    )
    if organization_slug:
        queryset = queryset.filter(organization__slug=organization_slug)

    entries = list(queryset.order_by("-created_at")[:2])
    if not entries:
        return None, None
    if organization_slug:
        return entries[0], None
    if len(entries) > 1:
        return None, "Organization slug is required to continue signup."
    return entries[0], None


def _decode_setup_token(token_str: str) -> dict | None:
    try:
        token = UntypedToken(token_str)
        if token.get("token_type") != "setup":
            return None
        return {
            "email": token["email"],
            "role": token["role"],
            "organization_id": token["organization_id"],
        }
    except (TokenError, KeyError):
        return None


class RequestOtpView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = RequestOtpSerializer
    throttle_classes = [OtpRequestIPRateThrottle, OtpRequestEmailRateThrottle]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = UserModel.objects.normalize_email(serializer.validated_data["email"])
        org_slug = serializer.validated_data.get("organization_slug")
        whitelist, whitelist_error = _resolve_pending_whitelist(email, org_slug)

        if whitelist_error:
            return Response({"detail": whitelist_error}, status=status.HTTP_400_BAD_REQUEST)
        if whitelist is None:
            return Response({"message": SIGNUP_REQUEST_MESSAGE, "expires_in": 600})

        retry_after = _cooldown_retry_after(email, OTPVerification.Purpose.VERIFY)
        if retry_after > 0:
            return Response(
                {
                    "detail": "Please wait before requesting another OTP.",
                    "retry_after": retry_after,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        otp, otp_code = OTPVerification.generate(email=email, purpose=OTPVerification.Purpose.VERIFY)
        sent = send_verification_email(email=email, name=email.split("@")[0], otp_code=otp_code)
        _record_email_delivery(
            email,
            "email.otp_verify",
            sent,
            organization=whitelist.organization,
            metadata={"purpose": OTPVerification.Purpose.VERIFY},
        )
        logger.info(
            "Verification OTP dispatched for %s — email_sent=%s",
            _mask_email(email),
            sent,
        )
        return Response({
            "message": SIGNUP_REQUEST_MESSAGE,
            "expires_in": 600,
        })


class VerifyOtpView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = VerifyOtpSerializer
    throttle_classes = [OtpVerifyIPRateThrottle, OtpVerifyEmailRateThrottle]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = UserModel.objects.normalize_email(serializer.validated_data["email"])
        otp_code = serializer.validated_data["otp_code"]
        org_slug = serializer.validated_data.get("organization_slug")

        otp_record = OTPVerification.objects.filter(
            email=email, purpose=OTPVerification.Purpose.VERIFY, is_used=False,
        ).order_by("-created_at").first()

        if otp_record is None or otp_record.is_expired:
            return Response(
                {"detail": SIGNUP_VERIFY_ERROR},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not otp_record.matches_code(otp_code):
            otp_record.register_failure()
            return Response(
                {"detail": SIGNUP_VERIFY_ERROR},
                status=status.HTTP_400_BAD_REQUEST,
            )

        whitelist, whitelist_error = _resolve_pending_whitelist(email, org_slug)

        if whitelist_error:
            return Response({"detail": SIGNUP_VERIFY_ERROR}, status=status.HTTP_400_BAD_REQUEST)
        if whitelist is None:
            return Response({"detail": SIGNUP_VERIFY_ERROR}, status=status.HTTP_400_BAD_REQUEST)

        otp_record.is_used = True
        otp_record.save(update_fields=["is_used"])

        setup_token = _generate_setup_token(
            email=email,
            role=whitelist.role,
            organization_id=whitelist.organization_id,
        )

        return Response({
            "setup_token": setup_token,
            "message": "OTP verified. You can now set your password.",
        })


class SetPasswordView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = SetPasswordSerializer

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        token_str = (request.headers.get("X-Setup-Token", "") or "").strip()
        if not token_str:
            auth_header = request.headers.get("Authorization", "")
            token_str = auth_header.replace("Bearer ", "", 1) if auth_header.startswith("Bearer ") else ""
        if not token_str:
            return Response(
                {"detail": "Missing setup token. Please verify your OTP first."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        claims = _decode_setup_token(token_str)
        if claims is None:
            return Response(
                {"detail": "Invalid or expired setup token. Please verify your OTP again."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        email = claims["email"]
        organization_id = claims["organization_id"]
        organization = Organization.objects.filter(pk=organization_id).first()
        if organization is None:
            return Response(
                {"detail": "The invitation organization is no longer available."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        whitelist = WhitelistedEmail.objects.filter(
            email=email, organization_id=organization_id, is_used=False,
        ).first()
        if whitelist is None:
            return Response(
                {"detail": "No pending signup invitation found for this email."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if whitelist.role != claims["role"]:
            return Response(
                {"detail": "This setup token no longer matches the active invitation. Please verify OTP again."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            role = whitelist.mapped_user_role
        except DjangoValidationError:
            return Response(
                {"detail": "The invitation role is invalid. Please contact support."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing = User.objects.select_related("organization").filter(email=email).first()
        if existing:
            if existing.is_superuser:
                return Response(
                    {"detail": "This email belongs to a platform super admin and cannot use invitation signup."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if existing.organization_id != organization_id:
                return Response(
                    {"detail": "This email is already assigned to another organization."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if existing.role != role:
                return Response(
                    {"detail": "This email already exists with a different role."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            validate_password_or_raise(serializer.validated_data["password"], user=existing, field_name="password")
            existing.set_password(serializer.validated_data["password"])
            update_fields = ["password"]
            if not existing.is_active:
                existing.is_active = True
                update_fields.append("is_active")
            if whitelist.mapped_is_profile_complete and not existing.is_profile_complete:
                existing.is_profile_complete = True
                update_fields.append("is_profile_complete")
            existing.save(update_fields=update_fields)

            profile, _ = UserProfile.objects.get_or_create(user=existing, defaults={"full_name": ""})
            profile.grade = whitelist.grade
            profile.section = whitelist.section
            profile_updates = ["grade", "section"]
            if role == User.Role.STUDENT:
                if not profile.student_identifier:
                    profile.student_identifier = UserProfile.generate_student_id(organization_id)
                    profile_updates.append("student_identifier")
                profile.mapped_teacher = whitelist.created_by if whitelist.created_by.role == User.Role.PROFESSOR else None
                profile_updates.append("mapped_teacher")
            profile.save(update_fields=profile_updates)

            revoke_user_refresh_tokens(existing)
            whitelist.consume(existing)
            return Response({
                "message": "Password updated. Please sign in.",
                "user_id": existing.id,
            })

        validate_password_or_raise(serializer.validated_data["password"], field_name="password")
        user = User.objects.create_user(
            email=email,
            password=serializer.validated_data["password"],
            role=role,
            organization=organization,
            is_profile_complete=whitelist.mapped_is_profile_complete,
        )
        profile = UserProfile.objects.create(
            user=user,
            full_name="",
            grade=whitelist.grade,
            section=whitelist.section,
        )
        if role == User.Role.STUDENT:
            profile.student_identifier = UserProfile.generate_student_id(organization_id)
            profile.mapped_teacher = whitelist.created_by if whitelist.created_by.role == User.Role.PROFESSOR else None
            profile.save(update_fields=["student_identifier", "mapped_teacher"])

        whitelist.consume(user)
        return Response({
            "message": "Account created successfully. Please sign in.",
            "user_id": user.id,
        })


class ForgotPasswordView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = ForgotPasswordSerializer
    throttle_classes = [PasswordResetRequestIPRateThrottle, PasswordResetRequestEmailRateThrottle]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        user = User.objects.filter(email=email, is_active=True).first()
        if user:
            retry_after = _cooldown_retry_after(email, OTPVerification.Purpose.RESET)
            if retry_after > 0:
                return Response(
                    {
                        "detail": "Please wait before requesting another reset OTP.",
                        "retry_after": retry_after,
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )
            otp, otp_code = OTPVerification.generate(email=email, purpose=OTPVerification.Purpose.RESET)
            sent = send_password_reset_otp(email=email, name=user.name, otp_code=otp_code)
            _record_email_delivery(
                email,
                "email.otp_reset",
                sent,
                organization=user.organization,
                metadata={"purpose": OTPVerification.Purpose.RESET},
            )
            logger.info(
                "Password reset OTP dispatched for %s — email_sent=%s",
                _mask_email(email),
                sent,
            )
        return Response({
            "message": "If the email exists in our system, a reset code has been sent.",
        })


class ResendOtpView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = ResendOtpSerializer
    throttle_classes = [OtpRequestIPRateThrottle, OtpRequestEmailRateThrottle]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = UserModel.objects.normalize_email(serializer.validated_data["email"])
        purpose = serializer.validated_data.get("purpose", OTPVerification.Purpose.VERIFY)
        organization_slug = serializer.validated_data.get("organization_slug")

        retry_after = _cooldown_retry_after(email, purpose)
        if retry_after > 0:
            return Response(
                {
                    "detail": "Please wait before requesting another OTP.",
                    "retry_after": retry_after,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        if purpose == OTPVerification.Purpose.VERIFY:
            whitelist, whitelist_error = _resolve_pending_whitelist(email, organization_slug)
            if whitelist_error:
                return Response({"detail": whitelist_error}, status=status.HTTP_400_BAD_REQUEST)
            if whitelist is None:
                return Response({"message": SIGNUP_REQUEST_MESSAGE, "expires_in": 600})
            otp, otp_code = OTPVerification.generate(email=email, purpose=OTPVerification.Purpose.VERIFY)
            sent = send_verification_email(email=email, name=email.split("@")[0], otp_code=otp_code)
            _record_email_delivery(
                email,
                "email.otp_verify",
                sent,
                organization=whitelist.organization,
                metadata={"purpose": OTPVerification.Purpose.VERIFY, "source": "resend"},
            )
            logger.info(
                "Verification OTP re-dispatched for %s — email_sent=%s",
                _mask_email(email),
                sent,
            )
            return Response({"message": SIGNUP_REQUEST_MESSAGE, "expires_in": 600})

        user = User.objects.filter(email=email, is_active=True).first()
        if user:
            otp, otp_code = OTPVerification.generate(email=email, purpose=OTPVerification.Purpose.RESET)
            sent = send_password_reset_otp(email=email, name=user.name, otp_code=otp_code)
            _record_email_delivery(
                email,
                "email.otp_reset",
                sent,
                organization=user.organization,
                metadata={"purpose": OTPVerification.Purpose.RESET, "source": "resend"},
            )
        return Response({"message": "If the email exists in our system, a reset code has been resent.", "expires_in": 600})


class ResetPasswordView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = ResetPasswordSerializer
    throttle_classes = [PasswordResetConfirmIPRateThrottle, PasswordResetConfirmEmailRateThrottle]

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"]
        otp_code = serializer.validated_data["otp_code"]
        new_password = serializer.validated_data["new_password"]

        otp_record = OTPVerification.objects.filter(
            email=email, purpose=OTPVerification.Purpose.RESET, is_used=False,
        ).order_by("-created_at").first()

        if otp_record is None or otp_record.is_expired or not otp_record.matches_code(otp_code):
            if otp_record is not None and not otp_record.is_expired:
                otp_record.register_failure()
            return Response(
                {"detail": "Invalid or expired reset code. Please request a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.filter(email=email, is_active=True).first()
        if user is None:
            return Response(
                {"detail": "No active account found for this email."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        validate_password_or_raise(new_password, user=user, field_name="new_password")
        user.set_password(new_password)
        user.save(update_fields=["password", "updated_at"])
        revoke_user_refresh_tokens(user)
        otp_record.is_used = True
        otp_record.save(update_fields=["is_used"])

        return Response({"message": "Password has been reset successfully. Please sign in."})
