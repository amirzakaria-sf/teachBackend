from __future__ import annotations

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import parse_cookie
from rest_framework_simplejwt.authentication import JWTAuthentication


class JwtCookieAuthMiddleware:
    def __init__(self, inner):
        self.inner = inner
        self.authenticator = JWTAuthentication()

    async def __call__(self, scope, receive, send):
        scope["user"] = await self._get_user(scope)
        return await self.inner(scope, receive, send)

    @database_sync_to_async
    def _get_user(self, scope):
        headers = dict(scope.get("headers") or [])
        raw_cookie = headers.get(b"cookie", b"").decode("latin1")
        cookies = parse_cookie(raw_cookie)
        token = str(cookies.get(settings.AUTH_ACCESS_COOKIE_NAME, "") or "").strip()
        if not token:
            return AnonymousUser()
        try:
            validated_token = self.authenticator.get_validated_token(token)
            user = self.authenticator.get_user(validated_token)
            if not getattr(user, "is_active", False):
                return AnonymousUser()
            return user
        except Exception:
            return AnonymousUser()
