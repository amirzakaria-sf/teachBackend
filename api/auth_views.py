from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
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

logger = logging.getLogger(__name__)
UserModel = get_user_model()

SETUP_TOKEN_LIFETIME = timedelta(minutes=15)
OTP_RESEND_COOLDOWN_SECONDS = 60


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


def _generate_setup_token(email: str, role: str, organization_id: int) -> str:
    """Create a short-lived JWT that encodes the pending user's details."""
    token = AccessToken()
    token["email"] = email
    token["role"] = role
    token["organization_id"] = organization_id
    token["token_type"] = "setup"
    token.set_exp(lifetime=SETUP_TOKEN_LIFETIME)
    return str(token)


def _normalize_user_role(invite_role: str) -> str:
    role = (invite_role or "").strip().lower()
    if role == WhitelistedEmail.InviteRole.TEACHER:
        return User.Role.PROFESSOR
    if role == WhitelistedEmail.InviteRole.STUDENT:
        return User.Role.STUDENT
    return role


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

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"]
        org_slug = serializer.validated_data.get("organization_slug")
        org_filter = {"slug": org_slug} if org_slug else {}
        org = None
        if org_filter:
            org = Organization.objects.filter(**org_filter).first()
        if org:
            whitelist = WhitelistedEmail.objects.filter(
                email=email, organization=org, is_used=False,
            ).first()
        else:
            # Without org context, find the pending whitelist entry
            whitelist = WhitelistedEmail.objects.filter(
                email=email, is_used=False,
            ).order_by("-created_at").first()

        if whitelist is None:
            return Response(
                {"detail": "This email has not been authorized for signup. Please contact your school administrator."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        retry_after = _cooldown_retry_after(email, OTPVerification.Purpose.VERIFY)
        if retry_after > 0:
            return Response(
                {
                    "detail": "Please wait before requesting another OTP.",
                    "retry_after": retry_after,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        otp = OTPVerification.generate(email=email, purpose=OTPVerification.Purpose.VERIFY)
        sent = send_verification_email(email=email, name=email.split("@")[0], otp_code=otp.otp_code)
        _record_email_delivery(
            email,
            "email.otp_verify",
            sent,
            organization=whitelist.organization,
            metadata={"purpose": OTPVerification.Purpose.VERIFY},
        )
        logger.info(
            "OTP generated for %s — otp_code=%s — email_sent=%s",
            email,
            otp.otp_code,
            sent,
        )
        return Response({
            "message": "Verification code sent to your email.",
            "expires_in": 600,
        })


class VerifyOtpView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = VerifyOtpSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"]
        otp_code = serializer.validated_data["otp_code"]

        otp_record = OTPVerification.objects.filter(
            email=email, purpose=OTPVerification.Purpose.VERIFY, is_used=False,
        ).order_by("-created_at").first()

        if otp_record is None or otp_record.is_expired:
            return Response(
                {"detail": "OTP is invalid or has expired. Please request a new code."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if otp_record.otp_code != otp_code:
            return Response(
                {"detail": "The code you entered is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        otp_record.is_used = True
        otp_record.save(update_fields=["is_used"])

        whitelist = WhitelistedEmail.objects.filter(
            email=email, is_used=False,
        ).order_by("-created_at").first()

        if whitelist is None:
            return Response(
                {"detail": "No pending signup invitation found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

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
        role = _normalize_user_role(claims["role"])
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

        # Check if user already exists
        existing = User.objects.filter(email=email, organization_id=organization_id).first()
        if existing:
            # Edge case: user was created but whitelist not consumed
            existing.set_password(serializer.validated_data["password"])
            existing.save(update_fields=["password"])
            whitelist.consume(existing)
            return Response({
                "message": "Password updated. Please sign in.",
                "user_id": existing.id,
            })

        # Create new user
        user = User.objects.create_user(
            email=email,
            password=serializer.validated_data["password"],
            role=role,
            organization=organization,
            is_profile_complete=False,
        )
        # Create empty profile
        profile = UserProfile.objects.create(
            user=user,
            full_name="",
            grade=whitelist.grade,
            section=whitelist.section,
        )
        if role == User.Role.STUDENT:
            profile.student_identifier = UserProfile.generate_student_id(organization_id)
            profile.mapped_teacher = whitelist.created_by
            profile.save(update_fields=["student_identifier", "mapped_teacher"])

        whitelist.consume(user)
        return Response({
            "message": "Account created successfully. Please sign in.",
            "user_id": user.id,
        })


class ForgotPasswordView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = ForgotPasswordSerializer

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
            otp = OTPVerification.generate(email=email, purpose=OTPVerification.Purpose.RESET)
            sent = send_password_reset_otp(email=email, name=user.name, otp_code=otp.otp_code)
            _record_email_delivery(
                email,
                "email.otp_reset",
                sent,
                organization=user.organization,
                metadata={"purpose": OTPVerification.Purpose.RESET},
            )
            logger.info(
                "Password reset OTP for %s — otp_code=%s — email_sent=%s",
                email,
                otp.otp_code,
                sent,
            )
        return Response({
            "message": "If the email exists in our system, a reset code has been sent.",
        })


class ResendOtpView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = ResendOtpSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        purpose = serializer.validated_data.get("purpose", OTPVerification.Purpose.VERIFY)

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
            whitelist = WhitelistedEmail.objects.filter(email=email, is_used=False).order_by("-created_at").first()
            if whitelist is None:
                return Response(
                    {"detail": "This email has not been authorized for signup. Please contact your school administrator."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            otp = OTPVerification.generate(email=email, purpose=OTPVerification.Purpose.VERIFY)
            sent = send_verification_email(email=email, name=email.split("@")[0], otp_code=otp.otp_code)
            _record_email_delivery(
                email,
                "email.otp_verify",
                sent,
                organization=whitelist.organization,
                metadata={"purpose": OTPVerification.Purpose.VERIFY, "source": "resend"},
            )
            return Response({"message": "Verification code resent.", "expires_in": 600})

        user = User.objects.filter(email=email, is_active=True).first()
        if user:
            otp = OTPVerification.generate(email=email, purpose=OTPVerification.Purpose.RESET)
            sent = send_password_reset_otp(email=email, name=user.name, otp_code=otp.otp_code)
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

        if otp_record is None or otp_record.is_expired or otp_record.otp_code != otp_code:
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

        user.set_password(new_password)
        user.save(update_fields=["password", "updated_at"])
        otp_record.is_used = True
        otp_record.save(update_fields=["is_used"])

        return Response({"message": "Password has been reset successfully. Please sign in."})
