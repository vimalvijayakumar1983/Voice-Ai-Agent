"""Small assertion harness for deterministic Tier-1 knowledge cases."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.exact_fact_retrieval import (
    ExactFactIndex,
    ExactFactResolution,
    ExactFactResponseAction,
    ExactFactType,
    resolve_exact_fact,
)


@dataclass(frozen=True)
class Tier1QualityCase:
    case_id: str
    query: str
    expected_action: ExactFactResponseAction
    expected_intents: tuple[ExactFactType, ...]
    expected_evidence_ids: tuple[str, ...] = ()
    paraphrases: tuple[str, ...] = ()
    query_variants: tuple[str, ...] = ()
    forbidden_values: tuple[str, ...] = ()

    @property
    def utterances(self) -> tuple[str, ...]:
        return (self.query, *self.paraphrases)


@dataclass(frozen=True)
class Tier1CaseRun:
    case_id: str
    utterance: str
    result: ExactFactResolution


class Tier1QualityHarness:
    """Execute utterance families and enforce the caller-facing contract."""

    def __init__(self, index: ExactFactIndex) -> None:
        self.index = index

    def run(self, case: Tier1QualityCase) -> tuple[Tier1CaseRun, ...]:
        return tuple(
            Tier1CaseRun(
                case_id=case.case_id,
                utterance=utterance,
                result=resolve_exact_fact(
                    self.index,
                    query=utterance,
                    query_variants=case.query_variants,
                ),
            )
            for utterance in case.utterances
        )

    def assert_case(self, case: Tier1QualityCase) -> tuple[Tier1CaseRun, ...]:
        runs = self.run(case)
        for run in runs:
            result = run.result
            assert result.response_action == case.expected_action, (
                f"{case.case_id!r} / {run.utterance!r}: expected action "
                f"{case.expected_action.value}, got {result.response_action.value} "
                f"({result.reason})"
            )
            assert result.intents == case.expected_intents, (
                f"{case.case_id!r} / {run.utterance!r}: expected intents "
                f"{case.expected_intents!r}, got {result.intents!r}"
            )
            assert result.evidence_ids == case.expected_evidence_ids, (
                f"{case.case_id!r} / {run.utterance!r}: expected evidence "
                f"{case.expected_evidence_ids!r}, got {result.evidence_ids!r}"
            )
            context = result.evidence_context or ""
            for forbidden in case.forbidden_values:
                assert forbidden.casefold() not in context.casefold(), (
                    f"{case.case_id!r} / {run.utterance!r}: leaked forbidden value {forbidden!r}"
                )
        return runs
