from rest_framework.permissions import BasePermission

from api.models import User


def _has_authenticated_active_user(request) -> bool:
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and user.is_active)


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        if not _has_authenticated_active_user(request):
            return False
        return bool(
            request.user.is_superuser
            or (request.user.role == User.Role.ADMIN and request.user.organization_id is not None)
        )


class IsSuperAdminRole(BasePermission):
    def has_permission(self, request, view):
        return bool(_has_authenticated_active_user(request) and request.user.is_superuser)


class IsProfessorRole(BasePermission):
    def has_permission(self, request, view):
        if not _has_authenticated_active_user(request):
            return False
        return bool(
            request.user.role == User.Role.PROFESSOR
            and request.user.organization_id is not None
        )


class IsStudentRole(BasePermission):
    def has_permission(self, request, view):
        if not _has_authenticated_active_user(request):
            return False
        return bool(
            request.user.role == User.Role.STUDENT
            and request.user.organization_id is not None
        )
