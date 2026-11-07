from django.conf import settings
from django.db import models
from django.utils import timezone


class RefreshToken(models.Model):
    """One issued family refresh token, so it can be rotated and revoked.

    A refresh token is used once: refreshing revokes it and records its
    successor. Presenting a revoked one again means it leaked, and every live
    refresh token of that identity is revoked.
    """

    jti = models.CharField(max_length=64, unique=True)
    identity = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="family_refresh_tokens",
    )
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    replaced_by = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["identity", "revoked_at"])]

    def __str__(self):
        return f"{self.identity_id}:{self.jti}"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.expires_at > timezone.now()
