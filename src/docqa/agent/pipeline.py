"""Bounded knowledge consumption shared by built-in asks and external draft submission."""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

from docqa.agent.citations import (
    build_citations,
    retain_supported,
    validate_draft,
    validate_support_report,
)
from docqa.agent.config import AgentOptions
from docqa.agent.evidence import build_evidence_items, revalidate_evidence
from docqa.agent.planning import conservative_plan, validate_plan
from docqa.agent.providers import make_chat_clients
from docqa.agent.rendering import render_answer
from docqa.agent.retrieval import CandidateLedger, Subquery, normalize_query
from docqa.agent.sessions import SessionStore
from docqa.doc2md.security import is_within
from docqa.docbase.providers import make_providers
from docqa.docbase.retrieval import generation_directory
from docqa.docbase.storage import load_generation
from docqa.domain.answers import (
    AnswerDraft,
    AnswerResult,
    Assessment,
    CoverageItem,
    HardFilters,
    QueryPlan,
    SupportReport,
)
from docqa.domain.errors import DocQAError


class CallBudget:
    def __init__(self, options):
        self.deadline = time.monotonic() + options.deadline_seconds
        self.maximum = options.max_model_calls
        self.used = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise DocQAError(
                "AGENT_DEADLINE_EXCEEDED", "Answer deadline elapsed; late results are not published"
            )

    def invoke(self, function, *args, **kwargs):
        self.check()
        if self.used >= self.maximum:
            raise DocQAError("AGENT_MODEL_BUDGET_EXCEEDED", "Model task budget exhausted")
        self.used += 1
        result = function(*args, **kwargs, deadline=self.deadline)
        self.check()
        return result


def _store(index, output):
    selected, root = Path(index).resolve(), Path(output).resolve()
    if is_within(root, selected) or is_within(selected, root):
        raise DocQAError(
            "INVALID_OUTPUT", "Agent state and DocBase index must be separate directories"
        )
    return SessionStore(root)


def create_evidence_session(
    index,
    question,
    *,
    output="runtime/agent",
    options=None,
    caller="local-codeagent",
    categories=None,
    doc_ids=None,
    generation_id=None,
    required_topics=None,
):
    options = options or AgentOptions()
    store = _store(index, output)
    return store.create(
        index,
        question,
        caller,
        generation_id=generation_id,
        hard_filters=HardFilters(categories=categories, doc_ids=doc_ids),
        max_queries=options.max_session_queries,
        max_rounds=options.max_rounds,
        max_finalize_attempts=options.max_finalize_attempts,
        max_evidence_bytes=options.max_evidence_bytes,
        ttl_seconds=options.session_ttl_seconds,
        required_topics=conservative_plan(question, required_topics).required_topics,
    )


def retrieve_in_session(
    session_id,
    question,
    *,
    output="runtime/agent",
    options=None,
    caller="local-codeagent",
    round_no=1,
    categories=None,
    doc_ids=None,
    retrieval_providers=None,
):
    options = options or AgentOptions()
    normalize_query(question)
    store = SessionStore(output)
    session = store.get(session_id, caller=caller)
    reservation = store.reserve_query(
        session_id,
        caller=caller,
        filters=HardFilters(categories=categories, doc_ids=doc_ids),
        round_no=round_no,
    )
    try:
        from docqa.docbase.retrieval import query

        filters = reservation["filters"]
        result = query(
            session.index_path,
            question,
            options=options.retrieval,
            generation_id=session.generation_id,
            categories=filters["categories"],
            doc_ids=filters["doc_ids"],
            allow_degraded=options.allow_degraded and options.retrieval.allow_degraded,
            providers=retrieval_providers,
        )
        items = build_evidence_items(session.index_path, session.generation_id, result.hits)
        items.sort(key=lambda item: item.is_heading)
        return store.deliver(
            session_id,
            reservation["reservation_id"],
            items,
            caller=caller,
            query_id=result.query_id,
            degraded=result.status == "degraded",
            warnings=result.warnings,
        )
    except Exception as exc:
        store.query_failed(
            session_id,
            reservation["reservation_id"],
            caller=caller,
            error_code=exc.code if isinstance(exc, DocQAError) else "SESSION_QUERY_FAILED",
        )
        raise


def _validate_assessment(assessment, plan, items):
    assessment = Assessment.model_validate(assessment)
    expected = {topic.topic_id for topic in plan.required_topics}
    if (
        len(assessment.coverage) != len(expected)
        or {value.topic_id for value in assessment.coverage} != expected
    ):
        raise DocQAError(
            "ASSESSMENT_INVALID", "Coverage must name each required topic exactly once"
        )
    ids = {item.evidence_id for item in items}
    for coverage in assessment.coverage:
        if not set(coverage.evidence_ids).issubset(ids) or (
            coverage.state != "missing" and not coverage.evidence_ids
        ):
            raise DocQAError("ASSESSMENT_INVALID", "Coverage refers to unavailable evidence")
    for query in assessment.followup_queries:
        if not set(query.topic_ids).issubset(expected):
            raise DocQAError("ASSESSMENT_INVALID", "Follow-up query invents a required topic")
    for conflict in assessment.conflicts:
        if not set(conflict.topic_ids).issubset(expected) or not set(
            conflict.evidence_ids
        ).issubset(ids):
            raise DocQAError("ASSESSMENT_INVALID", "Conflict refers to unknown topics or evidence")
    return assessment


def _publish(
    store,
    session,
    draft,
    report,
    topics,
    *,
    synthetic,
    degraded,
    rounds,
    stop_reason,
    trace,
    warnings,
    caller,
    options,
):
    supported_topics = {topic for claim in draft.claims for topic in claim.topic_ids}
    if report is not None:
        supported_topics.intersection_update(
            check.item_id for check in report.topic_checks if check.state == "supported"
        )
    conflict_topics = {topic for value in draft.conflicts for topic in value.topic_ids}
    all_topics = {topic.topic_id for topic in topics}
    missing = sorted(all_topics - supported_topics - conflict_topics)
    if draft.conflicts:
        status = "conflicting_evidence"
    elif not draft.claims:
        status = "insufficient_evidence"
    elif missing:
        status = "partial"
    else:
        status = "answered"
    items = store.items(session.session_id, caller=caller)
    answer_id = uuid4().hex
    semantic = bool(report is not None and not synthetic and (draft.claims or draft.conflicts))
    coverage = [
        CoverageItem(
            topic_id=topic.topic_id,
            state="conflicting"
            if topic.topic_id in conflict_topics
            else ("supported" if topic.topic_id in supported_topics else "missing"),
            evidence_ids=sorted(
                {
                    eid
                    for claim in draft.claims
                    if topic.topic_id in claim.topic_ids
                    for eid in claim.evidence_ids
                }
                | {
                    eid
                    for conflict in draft.conflicts
                    if topic.topic_id in conflict.topic_ids
                    for eid in conflict.evidence_ids
                }
            ),
        )
        for topic in topics
    ]
    current = store.get(session.session_id, caller=caller)
    result = AnswerResult(
        answer_id=answer_id,
        session_id=session.session_id,
        generation_id=session.generation_id,
        question=session.question,
        status=status,
        requested_format=draft.requested_format,
        claims=draft.claims,
        citations=build_citations(draft, items, answer_id),
        unanswered_topics=missing,
        conflicts=draft.conflicts,
        diagram_edges=draft.diagram_edges,
        coverage=coverage,
        support_report=report,
        verification="extractive_demo"
        if synthetic
        else ("semantic_checked" if semantic else "not_applicable"),
        verified=semantic,
        synthetic=synthetic,
        production_ready=semantic and not degraded and status == "answered",
        degraded=degraded,
        rounds_used=rounds,
        queries_used=current.queries_used,
        stop_reason=stop_reason,
        warnings=list(dict.fromkeys(warnings)),
        trace=trace,
    )
    item_map = {item.evidence_id: item for item in items}
    for citation in result.citations:
        reference = revalidate_evidence(session.index_path, item_map[citation.evidence_id])
        citation.markdown_path = reference["markdown_path"]
        citation.snapshot_path = reference["snapshot_path"]
        citation.image_paths = [image["path"] for image in reference["images"]]
    result.answer_markdown = render_answer(
        result, answer_dir=store.root / "answers" / result.answer_id
    )
    if len(result.answer_markdown.encode("utf-8")) > options.max_output_bytes:
        raise DocQAError(
            "ANSWER_OUTPUT_BUDGET_EXCEEDED",
            "Rendered answer exceeds explicit output bytes; no hidden truncation",
        )
    store.save_answer(session.session_id, result, result.answer_markdown, caller=caller)
    return result


def _failed(store, session, error, *, caller, options, trace, rounds=0):
    current = store.get(session.session_id, caller=caller)
    if current.finalize_attempts_used == 0:
        store.reserve_finalize(session.session_id, caller=caller)
    result = AnswerResult(
        answer_id=uuid4().hex,
        session_id=session.session_id,
        generation_id=session.generation_id,
        question=session.question,
        status="failed",
        requested_format="markdown",
        verification="not_applicable",
        verified=False,
        synthetic=session.synthetic or options.mode == "demo",
        production_ready=False,
        rounds_used=rounds,
        queries_used=current.queries_used,
        stop_reason=error.code,
        warnings=[error.code],
        trace=trace,
    )
    result.answer_markdown = render_answer(result)
    store.save_answer(session.session_id, result, result.answer_markdown, caller=caller)
    return result


def finalize_answer(
    session_id,
    draft,
    *,
    output="runtime/agent",
    options=None,
    caller="local-codeagent",
    verifier=None,
    topics=None,
    trace=None,
    rounds=0,
    degraded=False,
    budget=None,
    warnings=None,
):
    """External drafts and built-in composition pass through the same source and semantic gates."""
    options = options or AgentOptions()
    store = SessionStore(output)
    session = store.reserve_finalize(session_id, caller=caller)
    diagnostics = store.query_diagnostics(session_id, caller=caller)
    if diagnostics["failures"]:
        raise DocQAError(
            "SESSION_RETRIEVAL_FAILED",
            "Session contains failed queries; resolve the service failure in a new session",
        )
    degraded = degraded or diagnostics["degraded"]
    if degraded and not (options.allow_degraded and options.retrieval.allow_degraded):
        raise DocQAError("DEGRADED_NOT_ALLOWED", "Finalization disallows degraded retrieval")
    rounds = max(rounds, diagnostics["rounds"])
    draft = AnswerDraft.model_validate(draft)
    expected_topics = session.required_topics or conservative_plan(session.question).required_topics
    if topics is not None and topics != expected_topics:
        raise DocQAError(
            "ANSWER_INVALID", "Finalization cannot change the session's required topics"
        )
    topics = expected_topics
    draft = draft.model_copy(update={"required_topics": topics})
    items = store.items(session_id, caller=caller)
    validate_draft(draft, items, topics)
    for item in items:
        revalidate_evidence(session.index_path, item)
    budget = budget or CallBudget(options)
    report = None
    if draft.claims or draft.conflicts or draft.diagram_edges:
        if verifier is None:
            from docqa.agent.providers import DemoChatClient, HttpChatClient

            verifier = (
                DemoChatClient(role="verifier")
                if options.mode == "demo"
                else HttpChatClient(options.verifier, role="verifier")
            )
        report = SupportReport.model_validate(
            budget.invoke(verifier.verify, session.question, draft, items)
        )
        validate_support_report(draft, report)
        supported = retain_supported(draft, report)
        if (
            len(supported.claims) != len(draft.claims)
            or len(supported.conflicts) != len(draft.conflicts)
            or len(supported.diagram_edges) != len(draft.diagram_edges)
        ):
            raise DocQAError(
                "ANSWER_UNSUPPORTED",
                "One or more claims, conflicts or diagram relationships lacks semantic support",
            )
    synthetic = bool(
        session.synthetic
        or options.mode == "demo"
        or (verifier and verifier.fingerprint().get("synthetic", False))
    )
    notes = [*(warnings or []), *diagnostics["warnings"]]
    if synthetic:
        notes.append("SYNTHETIC_DEMO: extract display is not real semantic verification")
    budget.check()
    return _publish(
        store,
        session,
        draft,
        report,
        topics,
        synthetic=synthetic,
        degraded=degraded,
        rounds=rounds,
        stop_reason="external_finalized",
        trace=trace or [],
        warnings=notes,
        caller=caller,
        options=options,
    )


def answer_question(
    index,
    question,
    *,
    output="runtime/agent",
    options=None,
    caller="local-codeagent",
    categories=None,
    doc_ids=None,
    generation_id=None,
    required_topics=None,
    output_format="markdown",
    use_planner=False,
    chat_clients=None,
    retrieval_providers=None,
):
    options = options or AgentOptions()
    plan = conservative_plan(question, required_topics, output_format)
    store = _store(index, output)
    session = create_evidence_session(
        index,
        question,
        output=output,
        options=options,
        caller=caller,
        categories=categories,
        doc_ids=doc_ids,
        generation_id=generation_id,
        required_topics=required_topics,
    )
    budget, trace, warnings, rounds, degraded = CallBudget(options), [], [], 0, False
    try:
        with load_generation(generation_directory(index, session.generation_id)) as generation:
            if generation.profiles["mode"] != options.retrieval.mode:
                raise DocQAError(
                    "PROFILE_MISMATCH", "Retrieval mode differs from the pinned generation"
                )
            eligible = [
                chunk
                for chunk in generation.chunks
                if (categories is None or chunk.category in categories)
                and (doc_ids is None or chunk.doc_id in doc_ids)
            ]
            if not eligible:
                trace.append(
                    {
                        "stage": "plan",
                        "topics": [topic.model_dump() for topic in plan.required_topics],
                    }
                )
                store.reserve_finalize(session.session_id, caller=caller)
                return _publish(
                    store,
                    session,
                    AnswerDraft(requested_format=output_format),
                    None,
                    plan.required_topics,
                    synthetic=session.synthetic or options.mode == "demo",
                    degraded=False,
                    rounds=0,
                    stop_reason="index_empty" if not generation.chunks else "no_eligible_documents",
                    trace=trace,
                    warnings=[],
                    caller=caller,
                    options=options,
                )
            generator, verifier = chat_clients or make_chat_clients(options)
            if use_planner:
                proposed = QueryPlan.model_validate(
                    budget.invoke(
                        generator.plan,
                        question,
                        plan.required_topics,
                        {
                            "categories": sorted({chunk.category for chunk in eligible}),
                            "generation_id": session.generation_id,
                            "output_format": output_format,
                        },
                    )
                )
                validate_plan(proposed, question, output_format=output_format)
                if required_topics is not None and [
                    (topic.topic_id, topic.text) for topic in proposed.required_topics
                ] != [(topic.topic_id, topic.text) for topic in plan.required_topics]:
                    raise DocQAError(
                        "INVALID_PLAN", "Planner changed the explicitly required topics"
                    )
                plan = proposed
                session = store.set_plan_topics(
                    session.session_id, plan.required_topics, caller=caller
                )
            trace.append(
                {
                    "stage": "plan",
                    "topics": [topic.model_dump() for topic in plan.required_topics],
                    "exact_terms": plan.exact_terms,
                    "queries": [value.model_dump() for value in plan.subqueries],
                }
            )
            trace.append(
                {
                    "stage": "model_profiles",
                    "generator": generator.fingerprint(),
                    "verifier": verifier.fingerprint(),
                }
            )
            providers = retrieval_providers or make_providers(options.retrieval)
            ledger = CandidateLedger(
                generation,
                question,
                options=options.retrieval,
                providers=providers,
                topic_order=[topic.topic_id for topic in plan.required_topics],
                categories=categories,
                doc_ids=doc_ids,
                agent_allow_degraded=options.allow_degraded,
                max_pool=options.max_candidates,
                rerank_top_n=options.rerank_top_n,
                max_queries=min(options.max_queries, session.max_queries),
            )
            pending, uncovered, assessment = (
                plan.subqueries,
                [topic.topic_id for topic in plan.required_topics],
                None,
            )
            stop = "round_budget"
            for round_no in range(1, options.max_rounds + 1):
                budget.check()
                pending = [
                    value
                    for value in pending
                    if normalize_query(value.query) not in ledger.seen_queries
                ]
                pending = pending[
                    : min(options.max_subqueries, options.max_queries - ledger.query_count)
                ]
                if not pending:
                    stop = "no_new_query"
                    break
                reservations = [
                    store.reserve_query(session.session_id, caller=caller, round_no=round_no)
                    for _ in pending
                ]
                try:
                    batch = ledger.collect(
                        [Subquery(value.query, tuple(value.topic_ids)) for value in pending],
                        round_index=round_no,
                        uncovered_topics=uncovered,
                    )
                    budget.check()
                except Exception as exc:
                    for reservation in reservations:
                        store.query_failed(
                            session.session_id,
                            reservation["reservation_id"],
                            caller=caller,
                            error_code=exc.code
                            if isinstance(exc, DocQAError)
                            else "RETRIEVAL_FAILED",
                        )
                    raise
                rounds = round_no
                trace.append({"stage": "retrieve", **batch.trace})
                degraded = degraded or batch.degraded
                warnings.extend(batch.warnings)
                items = build_evidence_items(index, session.generation_id, batch.hits)
                items.sort(key=lambda item: item.is_heading)
                pack = store.deliver(
                    session.session_id,
                    reservations[0]["reservation_id"],
                    items,
                    caller=caller,
                    query_id=uuid4().hex,
                    degraded=batch.degraded,
                    warnings=batch.warnings,
                )
                for reservation in reservations[1:]:
                    store.link_query(
                        session.session_id,
                        reservation["reservation_id"],
                        caller=caller,
                        combined_into=pack.query_id,
                    )
                trace.append(
                    {
                        "stage": "evidence",
                        "round_no": rounds,
                        "new_items": len(pack.items),
                        "omitted": pack.omitted_count,
                        "payload_utf8_bytes": pack.payload_utf8_bytes,
                    }
                )
                if not pack.complete:
                    warnings.append("EVIDENCE_OMITTED_BY_BYTE_BUDGET")
                all_items = store.items(session.session_id, caller=caller)
                if not all_items:
                    stop = batch.empty_reason or "evidence_budget"
                    break
                assessment = _validate_assessment(
                    budget.invoke(generator.assess, question, plan.required_topics, all_items),
                    plan,
                    all_items,
                )
                trace.append(
                    {
                        "stage": "assess",
                        "round_no": rounds,
                        "coverage": [value.model_dump() for value in assessment.coverage],
                    }
                )
                uncovered = [
                    value.topic_id for value in assessment.coverage if value.state != "supported"
                ]
                if assessment.conflicts or any(
                    value.state == "conflicting" for value in assessment.coverage
                ):
                    stop = "conflict_observed"
                    break
                if not uncovered:
                    stop = "topics_covered"
                    break
                if round_no > 1 and not pack.items:
                    stop = "no_new_evidence"
                    break
                pending = assessment.followup_queries
                if not pending:
                    pending = [
                        type(plan.subqueries[0])(
                            query=next(
                                topic.text
                                for topic in plan.required_topics
                                if topic.topic_id == topic_id
                            ),
                            topic_ids=[topic_id],
                        )
                        for topic_id in uncovered[: options.max_subqueries]
                    ]
            all_items = store.items(session.session_id, caller=caller)
            if not all_items or assessment is None:
                store.reserve_finalize(session.session_id, caller=caller)
                return _publish(
                    store,
                    session,
                    AnswerDraft(requested_format=output_format),
                    None,
                    plan.required_topics,
                    synthetic=session.synthetic or options.mode == "demo",
                    degraded=degraded,
                    rounds=rounds,
                    stop_reason=stop,
                    trace=trace,
                    warnings=warnings,
                    caller=caller,
                    options=options,
                )
            for attempt in range(options.max_finalize_attempts):
                store.reserve_finalize(session.session_id, caller=caller)
                draft = AnswerDraft.model_validate(
                    budget.invoke(
                        generator.compose,
                        question,
                        plan.required_topics,
                        all_items,
                        assessment,
                        output_format,
                        repair=attempt > 0,
                    )
                )
                draft = draft.model_copy(update={"required_topics": plan.required_topics})
                try:
                    if draft.requested_format != output_format:
                        raise DocQAError(
                            "ANSWER_INVALID", "Composer changed the requested output format"
                        )
                    validate_draft(draft, all_items, plan.required_topics)
                    conflicted = {
                        value.topic_id
                        for value in assessment.coverage
                        if value.state == "conflicting"
                    } | {topic for conflict in assessment.conflicts for topic in conflict.topic_ids}
                    if not conflicted.issubset(
                        {topic for conflict in draft.conflicts for topic in conflict.topic_ids}
                    ):
                        raise DocQAError(
                            "ANSWER_INVALID", "Composer omitted an observed topic conflict"
                        )
                    for item in all_items:
                        revalidate_evidence(index, item)
                    report = SupportReport.model_validate(
                        budget.invoke(verifier.verify, question, draft, all_items)
                    )
                    validate_support_report(draft, report)
                    retained = retain_supported(draft, report)
                    incomplete = (
                        len(retained.claims),
                        len(retained.conflicts),
                        len(retained.diagram_edges),
                    ) != (len(draft.claims), len(draft.conflicts), len(draft.diagram_edges))
                    if incomplete and attempt == 0:
                        trace.append(
                            {
                                "stage": "repair",
                                "reason": "unsupported_items_removed_on_final_attempt",
                            }
                        )
                        continue
                    if incomplete:
                        warnings.append("UNSUPPORTED_ITEMS_REMOVED")
                        stop = "unsupported_items_removed"
                    synthetic = (
                        session.synthetic
                        or options.mode == "demo"
                        or any(
                            client.fingerprint().get("synthetic", False)
                            for client in (generator, verifier)
                        )
                    )
                    budget.check()
                    return _publish(
                        store,
                        session,
                        retained,
                        report,
                        plan.required_topics,
                        synthetic=bool(synthetic),
                        degraded=degraded,
                        rounds=rounds,
                        stop_reason=stop,
                        trace=trace,
                        warnings=warnings
                        + (["SYNTHETIC_DEMO: not real semantic verification"] if synthetic else []),
                        caller=caller,
                        options=options,
                    )
                except DocQAError as exc:
                    if attempt == 0 and exc.code in {
                        "ANSWER_INVALID",
                        "CITATION_INVALID",
                        "QUOTE_NOT_FOUND",
                        "SUPPORT_REPORT_INVALID",
                    }:
                        trace.append({"stage": "repair", "reason": exc.code})
                        continue
                    raise
            raise DocQAError("ANSWER_INVALID", "No publishable answer")
    except (DocQAError, OSError, ValueError, TypeError) as exc:
        error = (
            exc
            if isinstance(exc, DocQAError)
            else DocQAError("AGENT_FAILED", "Agent orchestration failed")
        )
        trace.append({"stage": "failed", "code": error.code, "model_tasks": budget.used})
        return _failed(
            store, session, error, caller=caller, options=options, trace=trace, rounds=rounds
        )
