from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings as jwt_api_settings
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken


UserModel = get_user_model()


def effective_profile_complete(user) -> bool:
    return True if user.role == UserModel.Role.ADMIN else bool(user.is_profile_complete)


def build_auth_payload(user) -> dict:
    from api.serializers import UserSerializer

    return {
        "user": UserSerializer(user).data,
        "is_profile_complete": effective_profile_complete(user),
    }


def issue_token_pair_for_user(user) -> tuple[str, str]:
    from api.serializers import CustomTokenObtainPairSerializer

    refresh = CustomTokenObtainPairSerializer.get_token(user)
    return str(refresh.access_token), str(refresh)


def set_auth_cookies(response: Response, *, access_token: str, refresh_token: str | None = None) -> None:
    response.set_cookie(
        settings.AUTH_ACCESS_COOKIE_NAME,
        access_token,
        max_age=int(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds()),
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path=settings.AUTH_COOKIE_PATH,
        domain=settings.AUTH_COOKIE_DOMAIN,
    )
    if refresh_token is not None:
        response.set_cookie(
            settings.AUTH_REFRESH_COOKIE_NAME,
            refresh_token,
            max_age=int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()),
            httponly=True,
            secure=settings.AUTH_COOKIE_SECURE,
            samesite=settings.AUTH_COOKIE_SAMESITE,
            path=settings.AUTH_COOKIE_PATH,
            domain=settings.AUTH_COOKIE_DOMAIN,
        )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(
        settings.AUTH_ACCESS_COOKIE_NAME,
        path=settings.AUTH_COOKIE_PATH,
        domain=settings.AUTH_COOKIE_DOMAIN,
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )
    response.delete_cookie(
        settings.AUTH_REFRESH_COOKIE_NAME,
        path=settings.AUTH_COOKIE_PATH,
        domain=settings.AUTH_COOKIE_DOMAIN,
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )


def set_auth_cookies_for_user(response: Response, user) -> tuple[str, str]:
    access_token, refresh_token = issue_token_pair_for_user(user)
    set_auth_cookies(response, access_token=access_token, refresh_token=refresh_token)
    return access_token, refresh_token


def get_refresh_token_from_request(request) -> str:
    token = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME, "")
    if token:
        return token.strip()
    value = request.data.get("refresh_token") or request.data.get("refresh") or ""
    return str(value).strip()


def get_access_token_from_request(request) -> str:
    return str(request.COOKIES.get(settings.AUTH_ACCESS_COOKIE_NAME, "") or "").strip()


def authenticate_user_from_access_token(raw_token: str):
    authenticator = JWTAuthentication()
    validated_token = authenticator.get_validated_token(raw_token)
    user = authenticator.get_user(validated_token)
    return user, validated_token


def get_user_from_refresh_token(raw_token: str):
    refresh = RefreshToken(raw_token)
    user_id = refresh.get(jwt_api_settings.USER_ID_CLAIM)
    if user_id is None:
        raise TokenError("Token contained no recognizable user identification")
    user = UserModel.objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        raise TokenError("User no longer exists or is inactive")
    return user, refresh


def rotate_refresh_token(raw_refresh_token: str):
    user, refresh = get_user_from_refresh_token(raw_refresh_token)
    if settings.SIMPLE_JWT.get("BLACKLIST_AFTER_ROTATION", False):
        refresh.blacklist()
    access_token, refresh_token = issue_token_pair_for_user(user)
    return user, access_token, refresh_token


def revoke_user_refresh_tokens(user) -> None:
    for outstanding in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=outstanding)


def validate_password_or_raise(password: str, *, user=None, field_name: str = "password") -> None:
    try:
        validate_password(password, user=user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({field_name: list(exc.messages)}) from exc
