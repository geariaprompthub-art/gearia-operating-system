"""Unit tests for pure deterministic Reciprocal Rank Fusion."""

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from app.services.reciprocal_rank_fusion import RankedList, ReciprocalRankFusion


@dataclass(frozen=True)
class Candidate:
    """Test-only candidate implementing only the content_id protocol."""

    content_id: UUID


@dataclass(frozen=True)
class InvalidCandidate:
    """Deliberately invalid protocol implementation for validation coverage."""

    content_id: str


def _list(origin: str, *ids: UUID) -> RankedList:
    return RankedList(origin=origin, candidates=[Candidate(content_id) for content_id in ids])


def test_intersection_scores_exactly_and_out_ranks_single_source_items() -> None:
    shared, lexical_only, vector_only = uuid4(), uuid4(), uuid4()
    result = ReciprocalRankFusion.fuse([_list("lexical", lexical_only, shared), _list("vector", vector_only, shared)], top_k=10)
    by_id = {candidate.content_id: candidate for candidate in result}
    assert by_id[shared].rrf_score == pytest.approx(1 / 62 + 1 / 62)
    assert by_id[shared].matched_by == ("lexical", "vector")
    assert result[0].content_id == shared
    assert by_id[lexical_only].matched_by == ("lexical",)
    assert by_id[vector_only].matched_by == ("vector",)


def test_empty_single_and_three_lists_are_repeatable() -> None:
    first, second, third = uuid4(), uuid4(), uuid4()
    assert ReciprocalRankFusion.fuse([], top_k=10) == []
    assert ReciprocalRankFusion.fuse([_list("lexical")], top_k=10) == []
    single = ReciprocalRankFusion.fuse([_list("lexical", first, second)], top_k=10)
    three = ReciprocalRankFusion.fuse([_list("lexical", first), _list("vector", second), _list("other", third)], top_k=10)
    assert [candidate.content_id for candidate in single] == [first, second]
    assert len(three) == 3
    assert three == ReciprocalRankFusion.fuse([_list("other", third), _list("vector", second), _list("lexical", first)], top_k=10)


def test_positions_duplicates_and_alternate_rrf_k_are_handled_without_compaction() -> None:
    first, second = uuid4(), uuid4()
    result = ReciprocalRankFusion.fuse([_list("lexical", first, first, second)], top_k=10, rrf_k=1)
    by_id = {candidate.content_id: candidate for candidate in result}
    assert by_id[first].rrf_score == pytest.approx(1 / 2)
    assert by_id[second].rrf_score == pytest.approx(1 / 4)
    assert by_id[first].matched_by == ("lexical",)


def test_top_k_ties_and_canonical_origin_order_are_deterministic() -> None:
    low = UUID("00000000-0000-0000-0000-000000000001")
    high = UUID("00000000-0000-0000-0000-000000000002")
    result = ReciprocalRankFusion.fuse([_list("zeta", high), _list("vector", low), _list("lexical", low)], top_k=10)
    assert [candidate.content_id for candidate in result] == [low, high]
    tied = ReciprocalRankFusion.fuse([_list("lexical", high), _list("vector", low)], top_k=10)
    assert [candidate.content_id for candidate in tied] == [low, high]
    assert result[0].matched_by == ("lexical", "vector")
    assert ReciprocalRankFusion.fuse([_list("lexical", low), _list("vector", low), _list("zeta", low)], top_k=10)[0].matched_by == ("lexical", "vector", "zeta")
    assert len(ReciprocalRankFusion.fuse([_list("lexical", low), _list("vector", high)], top_k=1)) == 1
    assert len(ReciprocalRankFusion.fuse([_list("lexical", low), _list("vector", high)], top_k=2)) == 2
    assert len(ReciprocalRankFusion.fuse([_list("lexical", low)], top_k=10)) == 1


@pytest.mark.parametrize("top_k,rrf_k", [(0, 60), (-1, 60), (1, 0), (1, -1), (True, 60), (1, True)])
def test_invalid_limits_are_rejected(top_k: int, rrf_k: int) -> None:
    with pytest.raises(ValueError):
        ReciprocalRankFusion.fuse([], top_k=top_k, rrf_k=rrf_k)


def test_invalid_origin_and_candidate_are_rejected() -> None:
    valid = Candidate(uuid4())
    with pytest.raises(ValueError):
        ReciprocalRankFusion.fuse([RankedList(origin=" ", candidates=[valid])], top_k=1)
    with pytest.raises(ValueError):
        ReciprocalRankFusion.fuse([RankedList(origin=" lexical ", candidates=[valid])], top_k=1)
    with pytest.raises(ValueError):
        ReciprocalRankFusion.fuse([RankedList(origin="lexical", candidates=[InvalidCandidate("not-a-uuid")])], top_k=1)


@pytest.mark.parametrize("origin", ["lexical", "vector"])
def test_duplicate_origins_are_rejected_before_any_fusion(origin: str) -> None:
    """Duplicate origins never create implicit strategy weighting or a partial result."""

    first, second, third = Candidate(uuid4()), Candidate(uuid4()), Candidate(uuid4())
    ranked_lists = [
        RankedList(origin=origin, candidates=[first]),
        RankedList(origin="other", candidates=[second]),
        RankedList(origin=origin, candidates=[third]),
    ]
    with pytest.raises(ValueError, match=f"duplicate origin: {origin}"):
        ReciprocalRankFusion.fuse(ranked_lists, top_k=10)


def test_duplicate_origin_with_empty_list_is_rejected_and_unique_origins_still_work() -> None:
    """Origin validation does not depend on adjacency or candidate count."""

    candidate = Candidate(uuid4())
    with pytest.raises(ValueError, match="duplicate origin: lexical"):
        ReciprocalRankFusion.fuse(
            [RankedList(origin="lexical", candidates=[]), RankedList(origin="lexical", candidates=[candidate])],
            top_k=10,
        )
    result = ReciprocalRankFusion.fuse([RankedList(origin="lexical", candidates=[candidate]), RankedList(origin="vector", candidates=[])], top_k=10)
    assert [item.content_id for item in result] == [candidate.content_id]
