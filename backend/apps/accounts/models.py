from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower

from apps.core.models import SoftDeleteModel, SoftDeleteQuerySet, UUIDModel


class Role(models.TextChoices):
    PLATFORM_ADMIN = "platform_admin", "Platform admin"
    OWNER = "owner", "Owner"
    MANAGER = "manager", "Manager"
    ATTENDANT = "attendant", "Attendant"


class UserManager(BaseUserManager.from_queryset(SoftDeleteQuerySet)):
    """Default manager hides soft-deleted users."""

    use_in_migrations = False

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)

    def create_user(self, *, tenant_id, role, password, email=None, phone=None, **extra):
        user = self.model(tenant_id=tenant_id, role=role, email=email, phone=phone, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user


class AllUsersManager(BaseUserManager.from_queryset(SoftDeleteQuerySet)):
    """Includes soft-deleted users."""


class User(UUIDModel, SoftDeleteModel, AbstractBaseUser):
    """Login account. Email and/or phone (phone-first for Zimbabwe), both globally unique
    because login happens before the tenant is known.

    platform_admin  <=>  tenant IS NULL   (enforced by a CHECK constraint, so a tenant
    user can never be created or promoted into a platform admin, even by raw SQL).
    """

    tenant = models.ForeignKey("tenants.Tenant", null=True, blank=True, on_delete=models.PROTECT, related_name="users")
    role = models.CharField(max_length=20, choices=Role.choices)
    full_name = models.CharField(max_length=200)
    email = models.EmailField(null=True, blank=True)
    phone = models.CharField(max_length=20, null=True, blank=True)
    preferred_language = models.CharField(
        max_length=2, default="en", choices=[("en", "English"), ("sn", "Shona"), ("nd", "Ndebele")]
    )
    is_active = models.BooleanField(default=True)
    # Bumped on logout / password change / deactivation: instantly invalidates every issued token.
    token_version = models.PositiveIntegerField(default=0)
    # Set when staff hand out a temporary password; every API call except change-password/me/logout is refused until changed.
    must_change_password = models.BooleanField(default=False)

    objects = UserManager()
    all_objects = AllUsersManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["full_name"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(role="platform_admin", tenant__isnull=True) | (~models.Q(role="platform_admin") & models.Q(tenant__isnull=False))),
                name="user_platform_admin_iff_no_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(email__isnull=False) | models.Q(phone__isnull=False),
                name="user_needs_email_or_phone",
            ),
            models.UniqueConstraint(Lower("email"), name="user_email_unique_ci"),
            models.UniqueConstraint("phone", name="user_phone_unique"),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.role})"

    def revoke_tokens(self):
        self.token_version += 1

    def deactivate(self):
        """Soft delete: keep the row and its history, lock the account, kill tokens."""
        from django.utils import timezone

        self.is_active = False
        self.deleted_at = timezone.now()
        self.revoke_tokens()
        self.save(update_fields=["is_active", "deleted_at", "token_version", "updated_at"])
