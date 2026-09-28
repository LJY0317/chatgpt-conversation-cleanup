from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .desktop_probe import Presence
from .evidence import SAFE_CLEANUP_ERROR_CODES, DesktopFailureEvidence


class ReconcileStatus(str, Enum):
    CONFIRMED = "confirmed"
    SUSPECTED = "suspected"
    UNKNOWN = "unknown"
    PROTECTED = "protected"
    PRESENT = "present"


@dataclass(frozen=True)
class CatalogAssessment:
    row: dict
    status: ReconcileStatus
    reason: str

    @property
    def default_selected(self) -> bool:
        return self.status == ReconcileStatus.CONFIRMED

    @property
    def selectable(self) -> bool:
        return self.status in {
            ReconcileStatus.CONFIRMED,
            ReconcileStatus.SUSPECTED,
            ReconcileStatus.UNKNOWN,
        }

    def selected_row(self, *, manual: bool = False) -> dict:
        selected = dict(self.row)
        if self.status == ReconcileStatus.CONFIRMED:
            selected["classification"] = "confirmed-missing"
        elif manual:
            selected["classification"] = "manual-local-cleanup"
        elif self.status == ReconcileStatus.SUSPECTED:
            selected["classification"] = "missing-chatgpt-entry"
        else:
            selected["classification"] = "manual-local-cleanup"
        return selected


def assess_catalog_rows(
    rows: list[dict],
    failures: dict[str, DesktopFailureEvidence],
    presence: dict[str, Presence] | None,
) -> list[CatalogAssessment]:
    """Classify every local ChatGPT catalog row without hiding uncertainty.

    Automatic cleanup is intentionally narrow. A row is confirmed only when
    Desktop produced terminal deleted/not-found evidence or an optional live
    provider conclusively reports it missing. Rows that cannot be verified stay
    visible as UNKNOWN instead of disappearing from the user's review surface.
    """

    assessments: list[CatalogAssessment] = []
    for row in rows:
        thread_id = row["thread_id"]
        evidence = failures.get(thread_id)
        live = presence.get(thread_id) if presence is not None else None

        if evidence is not None and evidence.error_code == "conversation_inaccessible":
            assessments.append(
                CatalogAssessment(
                    row=row,
                    status=ReconcileStatus.PROTECTED,
                    reason="ChatGPT reported an access problem, not a confirmed deletion.",
                )
            )
            continue

        if live == Presence.PRESENT:
            assessments.append(
                CatalogAssessment(
                    row=row,
                    status=ReconcileStatus.PRESENT,
                    reason="ChatGPT still returned this conversation.",
                )
            )
            continue

        if (
            evidence is not None
            and evidence.observed_at is not None
            and evidence.error_code in SAFE_CLEANUP_ERROR_CODES
        ):
            assessments.append(
                CatalogAssessment(
                    row=row,
                    status=ReconcileStatus.CONFIRMED,
                    reason="Desktop recorded a terminal deleted/not-found result.",
                )
            )
            continue

        if live == Presence.MISSING:
            assessments.append(
                CatalogAssessment(
                    row=row,
                    status=ReconcileStatus.CONFIRMED,
                    reason="The optional live check could not load this conversation twice.",
                )
            )
            continue

        if row.get("missing_candidate") == 1:
            assessments.append(
                CatalogAssessment(
                    row=row,
                    status=ReconcileStatus.SUSPECTED,
                    reason="ChatGPT Desktop marked this local row as a possible leftover.",
                )
            )
            continue

        assessments.append(
            CatalogAssessment(
                row=row,
                status=ReconcileStatus.UNKNOWN,
                reason="No reliable automatic signal is available for this local row.",
            )
        )
    return assessments


def assessment_counts(assessments: list[CatalogAssessment]) -> dict[ReconcileStatus, int]:
    return {
        status: sum(item.status == status for item in assessments)
        for status in ReconcileStatus
    }
