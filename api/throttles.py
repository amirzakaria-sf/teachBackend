from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework.throttling import SimpleRateThrottle


UserModel = get_user_model()


class IPRateThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        if not ident:
            return None
        return self.cache_format % {"scope": self.scope, "ident": ident}


class EmailRateThrottle(SimpleRateThrottle):
    email_field = "email"

    def get_cache_key(self, request, view):
        raw_email = request.data.get(self.email_field)
        email = UserModel.objects.normalize_email(str(raw_email or "").strip())
        if not email:
            return None
        return self.cache_format % {"scope": self.scope, "ident": email.lower()}


class LoginIPRateThrottle(IPRateThrottle):
    scope = "login_ip"


class LoginEmailRateThrottle(EmailRateThrottle):
    scope = "login_email"


class OtpRequestIPRateThrottle(IPRateThrottle):
    scope = "otp_request_ip"


class OtpRequestEmailRateThrottle(EmailRateThrottle):
    scope = "otp_request_email"


class OtpVerifyIPRateThrottle(IPRateThrottle):
    scope = "otp_verify_ip"


class OtpVerifyEmailRateThrottle(EmailRateThrottle):
    scope = "otp_verify_email"


class PasswordResetRequestIPRateThrottle(IPRateThrottle):
    scope = "password_reset_request_ip"


class PasswordResetRequestEmailRateThrottle(EmailRateThrottle):
    scope = "password_reset_request_email"


class PasswordResetConfirmIPRateThrottle(IPRateThrottle):
    scope = "password_reset_confirm_ip"


class PasswordResetConfirmEmailRateThrottle(EmailRateThrottle):
    scope = "password_reset_confirm_email"
