import logging
import typing
from typing import final

from django.contrib.postgres.fields import ArrayField
from django.db import models

from ami.base.models import BaseModel, BaseQuerySet

if typing.TYPE_CHECKING:
    from .taxonomy import Taxon

logger = logging.getLogger(__name__)


@final
class ClassificationResult(BaseModel):
    """A classification result from a model"""


class ClassificationQuerySet(BaseQuerySet):
    def find_duplicates(self, project_id: int | None = None) -> models.QuerySet:
        # Find the oldest classification for each unique combination
        if project_id:
            self = self.filter(detection__source_image__project_id=project_id)
        unique_oldest = (
            self.values("detection", "taxon", "algorithm", "score", "softmax_output", "raw_output")
            .annotate(min_id=models.Min("id"))
            .distinct()
        )

        # Keep only the oldest classifications
        return self.exclude(id__in=[item["min_id"] for item in unique_oldest])


class ClassificationManager(models.Manager.from_queryset(ClassificationQuerySet)):
    pass


@final
class Classification(BaseModel):
    """The output of a classifier"""

    project_accessor = "detection__source_image__project"
    detection = models.ForeignKey(
        "Detection",
        on_delete=models.SET_NULL,
        null=True,
        related_name="classifications",
    )

    taxon = models.ForeignKey("Taxon", on_delete=models.SET_NULL, null=True, related_name="classifications")
    score = models.FloatField(null=True)
    timestamp = models.DateTimeField()  # Is this to represent when classification was made? why not use created_at?
    terminal = models.BooleanField(
        default=True, help_text="Is this the final classification from a series of classifiers in a pipeline?"
    )
    logits = ArrayField(
        models.FloatField(), null=True, help_text="The raw output of the last fully connected layer of the model"
    )
    scores = ArrayField(
        models.FloatField(),
        null=True,
        help_text="The probabilities the model, calibrated by the model maker, likely the softmax output",
    )
    category_map = models.ForeignKey("ml.AlgorithmCategoryMap", on_delete=models.PROTECT, null=True)

    algorithm = models.ForeignKey(
        "ml.Algorithm",
        on_delete=models.SET_NULL,
        null=True,
        related_name="classifications",
    )
    # job = models.CharField(max_length=255, null=True)
    applied_to = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="derived_classifications",
        help_text=(
            "If this classification was produced by a post-processing algorithm, "
            "this field references the original classification it was applied to."
        ),
    )
    objects = ClassificationManager()

    # Type hints for auto-generated fields
    taxon_id: int
    algorithm_id: int

    class Meta:
        ordering = ["-created_at", "-score"]

    def __str__(self) -> str:
        terminal = "Terminal" if self.terminal else "Intermediate"
        if logger.getEffectiveLevel() == logging.DEBUG:
            # Query the related objects to get the names
            return f"#{self.pk} to Taxon {self.taxon} ({self.score:.2f}) by Algorithm {self.algorithm} ({terminal})"
        return (
            f"#{self.pk} to Taxon #{self.taxon_id} ({self.score:.2f}) by Algorithm #{self.algorithm_id} ({terminal})"
        )

    def top_scores_with_index(self, n: int | None = None) -> typing.Iterable[tuple[int, float]]:
        """
        Return the scores with their index, but sorted by score.
        """
        if self.scores:
            top_scores_by_index = sorted(enumerate(self.scores), key=lambda x: x[1], reverse=True)[:n]
            return top_scores_by_index
        else:
            return []

    def predictions(self, sort=True) -> typing.Iterable[tuple[str, float]]:
        """
        Return all label-score pairs for this classification using the category map.
        """
        if not self.category_map:
            raise ValueError("Classification must have a category map to get predictions.")
        scores = self.scores or []
        preds = zip(self.category_map.labels, scores)
        if sort:
            return sorted(preds, key=lambda x: x[1], reverse=True)
        else:
            return preds

    def predictions_with_taxa(self, sort=True) -> typing.Iterable[tuple["Taxon", float]]:
        """
        Return taxa objects and their scores for this classification using the category map.

        @TODO make this more efficient with numpy and/or postgres array functions. especially when we only need
        the top N out of thousands of taxa.
        """
        if not self.category_map:
            raise ValueError("Classification must have a category map to get predictions.")
        scores = self.scores or []
        category_data_with_taxa = self.category_map.with_taxa()
        taxa_sorted_by_index = [cat["taxon"] for cat in sorted(category_data_with_taxa, key=lambda cat: cat["index"])]
        preds = zip(taxa_sorted_by_index, scores)
        if sort:
            return sorted(preds, key=lambda x: x[1], reverse=True)
        else:
            return preds

    def taxa(self) -> typing.Iterable["Taxon"]:
        """
        Return the taxa objects for this classification using the category map.
        """
        if not self.category_map:
            return []
        category_data_with_taxa = self.category_map.with_taxa()
        taxa_sorted_by_index = [cat["taxon"] for cat in sorted(category_data_with_taxa, key=lambda cat: cat["index"])]
        return taxa_sorted_by_index

    def top_n(self, n: int = 3) -> list[dict[str, "Taxon | float | None"]]:
        """Return top N taxa and scores for this classification."""
        if not self.category_map:
            logger.warning(
                f"Classification {self.pk}'s algorithm ({self.algorithm_id}) has no category map, "
                "can't get top N predictions."
            )
            return []

        top_scored = self.top_scores_with_index(n)  # (index, score) pairs
        indexes = [idx for idx, _ in top_scored]
        category_data: list[dict] = self.category_map.with_taxa(only_indexes=indexes)
        assert category_data is not None
        index_to_taxon = {cat["index"]: cat["taxon"] for cat in category_data}

        return [
            {
                "taxon": index_to_taxon[i],
                "score": s,
                "logit": self.logits[i] if self.logits else None,
            }
            for i, s in top_scored
        ]

    def save(self, *args, **kwargs):
        """
        Set the category map based on the algorithm.
        """
        if self.algorithm and not self.category_map:
            self.category_map = self.algorithm.category_map
        super().save(*args, **kwargs)
