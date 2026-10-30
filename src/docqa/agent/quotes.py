"""Select server-owned exact source spans instead of asking a model to retype them."""

from hashlib import sha256

from docqa.agent.citations import DraftValidationError
from docqa.domain.answers import AnswerDraft


def quote_catalog(items):
    """Overlapping bounded windows, copied without normalizing source text."""
    catalog = {}
    for item in items:
        # Both typed evidence and its compact model projection are supported.
        eid = item["evidence_id"] if isinstance(item, dict) else item.evidence_id
        text = item["text"] if isinstance(item, dict) else item.text
        values = []
        for start in range(0, len(text), 520):
            end = min(start + 600, len(text))
            quote = text[start:end].strip()
            if quote:
                token = sha256(f"{eid}\0{start}\0{quote}".encode("utf-8")).hexdigest()[:20]
                values.append({"quote_id": "Q" + token, "text": quote})
            if end == len(text):
                break
        catalog[eid] = values
    return catalog


def bind_quote_selections(draft: AnswerDraft, items):
    """Only typed selectors resolve; invented IDs/contradictory text still fail."""
    if not any(claim.quote_refs for claim in draft.claims):
        return draft, 0
    catalog = {
        eid: {entry["quote_id"]: entry["text"] for entry in values}
        for eid, values in quote_catalog(items).items()
    }
    claims, count = [], 0
    for claim in draft.claims:
        if not claim.quote_refs:
            claims.append(claim)
            continue
        if set(claim.quote_refs) != set(claim.evidence_ids) or len(claim.evidence_ids) != len(
            set(claim.evidence_ids)
        ):
            raise DraftValidationError(
                "CITATION_INVALID",
                "Every selected source requires one quote selector",
                "quote_selection_sources_mismatch",
            )
        selected = {}
        for eid, token in claim.quote_refs.items():
            if eid not in catalog:
                raise DraftValidationError(
                    "CITATION_INVALID",
                    "Selected evidence was not delivered",
                    "evidence_not_delivered",
                )
            if token not in catalog[eid]:
                raise DraftValidationError(
                    "CITATION_INVALID",
                    "Quote selector is outside this source",
                    "quote_selection_unknown",
                )
            selected[eid] = catalog[eid][token]
        if claim.quotes and claim.quotes != selected:
            raise DraftValidationError(
                "QUOTE_NOT_FOUND",
                "Raw quotes contradict selected source spans",
                "quote_selection_conflict",
            )
        claims.append(claim.model_copy(update={"quotes": selected, "quote_refs": {}}))
        count += len(selected)
    return draft.model_copy(update={"claims": claims}), count
