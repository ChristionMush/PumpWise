from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Role


class _HasRole(BasePermission):
    roles: tuple = ()

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.role in self.roles)


class IsPlatformAdmin(_HasRole):
    roles = (Role.PLATFORM_ADMIN,)


class IsOwner(_HasRole):
    roles = (Role.OWNER,)


class OwnerWriteManagerRead(BasePermission):
    """Owners manage users; managers may only look."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return user.role in (Role.OWNER, Role.MANAGER)
        return user.role == Role.OWNER


class IsOwnerOrManager(_HasRole):
    roles = (Role.OWNER, Role.MANAGER)


class IsTenantUser(_HasRole):
    """Any staff member of a tenant (not platform staff)."""

    roles = (Role.OWNER, Role.MANAGER, Role.ATTENDANT)
