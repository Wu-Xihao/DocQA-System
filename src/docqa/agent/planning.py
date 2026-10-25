"""Conservative original-question planning with protected technical identifiers."""

from __future__ import annotations

import re

from docqa.agent.retrieval import normalize_query
from docqa.domain.answers import PlannedQuery, QueryPlan, QuestionTopic
from docqa.domain.errors import DocQAError

_TECHNICAL = re.compile(
    r"(?<![A-Za-z0-9_])(?:--?[A-Za-z][A-Za-z0-9_-]*(?:=[A-Za-z0-9_.:/\\-]+)?|"
    r"[A-Za-z0-9_]+(?:[.:/\\@-][A-Za-z0-9_]+)*)(?![A-Za-z0-9_])"
)


def extract_exact_terms(question: str) -> list[str]:
    """Preserve identifiers/flags/versions/acronyms, including their exact case."""
    normalize_query(question)
    terms = []
    for match in _TECHNICAL.finditer(question):
        value = match.group()
        if not (
            any(char.isdigit() or char in "_./\\@-:" for char in value)
            or (len(value) >= 2 and value.isupper())
        ):
            continue
        if value not in terms:
            terms.append(value)
    if len(terms) > 32:
        raise DocQAError("INVALID_REQUEST", "Question exceeds 32 protected technical terms")
    return terms


def conservative_plan(
    question: str,
    required_topics: list[str] | None = None,
    output_format: str = "markdown",
) -> QueryPlan:
    """One unmodified original query; topics are explicit obligations, not guesses.

    Complex semantic decomposition may be supplied by a bounded planner in the
    orchestration layer. This default does not claim that punctuation reliably
    identifies all comparison sides or subquestions.
    """
    normalize_query(question)
    if output_format not in {"markdown", "table", "mermaid"}:
        raise DocQAError("INVALID_REQUEST", "Unknown requested answer format")
    values = [question] if required_topics is None else required_topics
    if (
        not isinstance(values, list)
        or not 1 <= len(values) <= 12
        or any(
            not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in values
        )
        or len(set(values)) != len(values)
    ):
        raise DocQAError(
            "INVALID_REQUEST", "Required topics must be 1..12 distinct bounded strings"
        )
    topics = [
        QuestionTopic(topic_id=f"T{number:02d}", text=value)
        for number, value in enumerate(values, 1)
    ]
    intent = "process" if output_format == "mermaid" else "fact"
    return QueryPlan(
        intent=intent,
        required_topics=topics,
        exact_terms=extract_exact_terms(question),
        subqueries=[PlannedQuery(query=question, topic_ids=[topic.topic_id for topic in topics])],
        output_format=output_format,
    )


def validate_plan(
    plan: QueryPlan,
    question: str,
    *,
    output_format: str | None = None,
) -> QueryPlan:
    """Reject dropped obligations and identifiers in provider-produced plans.

    Plans have no fields for source paths, hard filters, generation selection or
    tools. Their strict domain schema therefore cannot authorize a larger scope.
    """
    if not isinstance(plan, QueryPlan):
        raise DocQAError("INVALID_PLAN", "Planner must return the QueryPlan contract")
    expected = extract_exact_terms(question)
    if plan.exact_terms != expected:
        raise DocQAError("INVALID_PLAN", "Planner changed or dropped protected technical terms")
    if output_format is not None and plan.output_format != output_format:
        raise DocQAError("INVALID_PLAN", "Planner changed the requested output format")
    covered = {topic for query in plan.subqueries for topic in query.topic_ids}
    if covered != {topic.topic_id for topic in plan.required_topics}:
        raise DocQAError("INVALID_PLAN", "Plan leaves a required topic without any query")
    queries = [normalize_query(query.query) for query in plan.subqueries]
    if len(set(queries)) != len(queries):
        raise DocQAError("INVALID_PLAN", "Plan repeats the same normalized subquery")
    for term in expected:
        # Exact spelling matters: FE_HEART_Start is not FE_HEART_START. A token
        # cannot be 'preserved' by inserting it inside an unrelated identifier.
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])"
        if not any(re.search(pattern, query.query) for query in plan.subqueries):
            raise DocQAError("INVALID_PLAN", "Subqueries omit a protected technical identifier")
    return plan


default_plan = conservative_plan
