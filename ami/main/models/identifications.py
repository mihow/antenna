import functools
import logging
import typing
from typing import final

from django.conf import settings
from django.contrib.auth.models import AbstractUser, AnonymousUser
from django.db import models

from ami.base.models import BaseModel
from ami.users.models import User

if typing.TYPE_CHECKING:
    from .occurrences import Occurrence
    from .taxonomy import Taxon

logger = logging.getLogger(__name__)


@functools.cache
def user_agrees_with_identification(user: "User", occurrence: "Occurrence", taxon: "Taxon") -> bool | None:
    """
    Determine if a user has made an identification of an occurrence that agrees with the given taxon.

    If a user has identified the same occurrence with the same taxon, then they "agree".

    @TODO if we want to reduce this to one query per request, we can accept just User & Occurrence
    then return the list of Taxon IDs that the user has added to that occurrence.
    then the view functions can check if the given taxon is in that list. Or check the list of identifications
    already retrieved by the view.
    """

    # Anonymous users don't have a primary key and will throw an error when used in a query.
    if not user or not user.pk or not taxon or not occurrence:
        return None

    return Identification.objects.filter(
        occurrence=occurrence,
        user=user,
        taxon=taxon,
        withdrawn=False,
    ).exists()


@final
class Identification(BaseModel):
    """A classification of an occurrence by a human."""

    project_accessor = "occurrence__project"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="identifications",
    )
    taxon = models.ForeignKey(
        "Taxon",
        on_delete=models.SET_NULL,
        null=True,
        related_name="identifications",
    )
    occurrence = models.ForeignKey(
        "Occurrence",
        on_delete=models.CASCADE,
        related_name="identifications",
    )
    withdrawn = models.BooleanField(default=False)
    agreed_with_identification = models.ForeignKey(
        "Identification",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agreed_identifications",
    )
    agreed_with_prediction = models.ForeignKey(
        "Classification",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agreed_identifications",
    )
    score = 1.0  # Always 1 for humans, at this time
    comment = models.TextField(blank=True)

    class Meta:
        ordering = [
            "-created_at",
        ]

    def save(self, *args, **kwargs):
        """
        If this is a new identification:
        - Set previous identifications by this user to withdrawn
        - Set the determination of the occurrence to the taxon of this identification if it is primary

        # @TODO Add tests
        """
        from ami.main.models import update_occurrence_determination

        if not self.pk and not self.withdrawn and self.user:
            # This is a new identification and it has not been explicitly withdrawn
            # so set all other identifications of this user to withdrawn.
            Identification.objects.filter(
                occurrence=self.occurrence,
                user=self.user,
            ).exclude(
                pk=self.pk
            ).update(withdrawn=True)

        super().save(*args, **kwargs)

        update_occurrence_determination(self.occurrence)

    def delete(self, *args, **kwargs):
        """
        If this is the current identification for the occurrence, set the determination to the next best ID.
        and un-withdraw the previous ID by the same user.

        @TODO Add tests
        """
        from ami.main.models import update_occurrence_determination

        current_best: Identification | None = self.occurrence.best_identification
        if current_best and current_best == self:
            # Invalidate the cached property so it will be re-calculated
            del self.occurrence.best_identification
            if self.user:
                previous_id = (
                    Identification.objects.filter(
                        occurrence=self.occurrence,
                        user=self.user,
                        withdrawn=True,
                    )
                    .exclude(pk=self.pk)
                    # The "next best" ID by the same user is just the most recent one.
                    # but this could be more complex in the future.
                    .order_by("-created_at")
                    .first()
                )
                if previous_id:
                    previous_id.withdrawn = False
                    previous_id.save()

        super().delete(*args, **kwargs)

        # Allow the update_occurrence_determination to determine the next best ID
        update_occurrence_determination(self.occurrence, current_determination=self.taxon)

    def check_permission(self, user: AbstractUser | AnonymousUser, action: str) -> bool:
        """Custom permission check logic for Identification model."""
        import ami.users.roles as roles

        project = self.get_project()
        if not project:
            return False

        if action == "destroy":
            # Allow if user is superuser, project manager, or owner of the identification
            return user.is_superuser or roles.ProjectManager.has_role(user, project) or self.user == user

        # Fallback to base class permission checks
        return super().check_permission(user, action)
