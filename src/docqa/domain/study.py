"""Editorial plans reference checked claims; they cannot supply new factual bodies."""

from typing import Annotated

from pydantic import Field

from docqa.domain.documents import Contract


class StudySection(Contract):
    title: str = Field(min_length=1, max_length=80)
    claim_ids: list[str] = Field(min_length=1, max_length=100)


class StudyPlan(Contract):
    sections: list[StudySection] = Field(min_length=1, max_length=40)
    claim_labels: dict[str, Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=dict, max_length=100
    )

    def check_claims(self, allowed):
        from docqa.domain.errors import DocQAError

        supplied = [identity for section in self.sections for identity in section.claim_ids]
        allowed = set(allowed)
        if (
            len(supplied) != len(set(supplied))
            or set(supplied) != allowed
            or not set(self.claim_labels).issubset(allowed)
        ):
            raise DocQAError(
                "ARTIFACT_PLAN_INVALID",
                "Editorial plan must organize every checked claim exactly once",
            )
        return self
