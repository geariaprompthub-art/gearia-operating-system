"""Embedding domain tests; all use the deterministic network-free provider."""
from datetime import UTC, datetime, timedelta
from sqlalchemy import select
from uuid import UUID
from app.db import Base
from app.models.content import Content
from app.models.content_embedding import ContentEmbedding
from app.models.source import Source
from app.services.embedding_provider import build_content_embedding_text, content_hash
from app.services.embedding_service import EmbeddingService
from app.services.fake_embedding_provider import FakeEmbeddingProvider
from app.services.embedding_dependencies import get_embedding_service
from app.services.embedding_dependencies import get_embedding_provider
from test_main import TestingSessionLocal, test_engine, client

def setup_function(_: object) -> None:
    Base.metadata.drop_all(bind=test_engine); Base.metadata.create_all(bind=test_engine)
def content():
    db=TestingSessionLocal(); source=Source(name="embedding-source",type="manual"); db.add(source); db.commit(); db.refresh(source)
    item=Content(source_id=source.id,title="Automação",url="https://test/embed",fingerprint="embed",summary="ChatGPT workflow",topics=["ChatGPT"],keywords=["OpenAI"],processing_status="processed"); db.add(item); db.commit(); db.refresh(item); return db,item
def test_fake_provider_and_text_hash_are_deterministic() -> None:
    provider=FakeEmbeddingProvider(); first=provider.embed_text("same"); second=provider.embed_text("same")
    assert len(first)==1536 and first==second and first!=provider.embed_text("other")
    db,item=content(); text=build_content_embedding_text(item); assert content_hash(text)==content_hash(text)
    item.title="changed"; assert content_hash(text)!=content_hash(build_content_embedding_text(item)); db.close()
def test_service_idempotency_force_dry_run_failure_and_stale() -> None:
    db,item=content(); provider=FakeEmbeddingProvider(); service=EmbeddingService(db,provider)
    assert service.generate(item.id)["status"]=="completed"; assert service.generate(item.id)["status"]=="skipped"
    calls=provider.calls; assert service.generate(item.id,dry_run=True)["status"]=="skipped" and provider.calls==calls
    assert service.generate(item.id,force=True)["status"]=="completed" and provider.calls==calls+1
    item.title="new title"; db.commit(); assert service.generate(item.id,dry_run=True)["status"]=="would_generate"
    assert service.generate(item.id)["status"]=="completed"; row=db.scalar(select(ContentEmbedding)); assert row.embedding_status=="completed" and row.processing_error is None
    db.close()
def test_provider_failure_is_recorded() -> None:
    db,item=content()
    try: EmbeddingService(db,FakeEmbeddingProvider(fail=True)).generate(item.id)
    except RuntimeError: pass
    row=db.scalar(select(ContentEmbedding)); assert row and row.embedding_status=="failed" and row.processing_error
    db.close()
def _assert_no_vector(value: object) -> None:
    """Reject vector-bearing keys anywhere in a public JSON response."""
    if isinstance(value, dict):
        assert not ({"embedding","vector","embedding_vector","values"} & set(value))
        for nested in value.values(): _assert_no_vector(nested)
    elif isinstance(value, list):
        for nested in value: _assert_no_vector(nested)
def test_public_embedding_endpoints_never_expose_vectors_and_validate_limits() -> None:
    """Public contract remains metadata-only even for errors and empty pages."""
    responses=[client.get("/embeddings"),client.get("/embeddings?limit=100"),client.post("/embeddings/generate",json={"limit":501}),client.get("/embeddings/contents/00000000-0000-0000-0000-000000000000")]
    assert responses[0].status_code==responses[1].status_code==200
    assert responses[2].status_code==422 and responses[3].status_code==404
    for response in responses: _assert_no_vector(response.json())
def test_batch_content_ids_is_deduplicated_ordered_and_metrics_are_coherent() -> None:
    """Missing IDs are intentionally ignored: requested may exceed processed."""
    db,item=content(); second=Content(source_id=item.source_id,title="Second",url="https://test/second",fingerprint="embed-second",processing_status="processed"); db.add(second); db.commit(); db.refresh(second)
    provider=FakeEmbeddingProvider(); missing=UUID("00000000-0000-0000-0000-000000000000")
    result=EmbeddingService(db,provider).generate_batch(content_ids=[second.id,item.id,second.id,missing],dry_run=True)
    assert result["requested"]==3 and result["processed"]==2 and len(result["items"])==2
    assert result["completed"]+result["skipped"]+result["failed"]==result["processed"]
    assert provider.calls==0 and db.scalar(select(ContentEmbedding)) is None
    _assert_no_vector(result); db.close()

def test_http_batch_preserves_dry_run_contract_without_vectors() -> None:
    """The public batch envelope retains dry-run metadata while hiding vectors."""
    db, item = content(); db.close()
    fake = FakeEmbeddingProvider()
    from app.main import app
    app.dependency_overrides[get_embedding_service] = lambda: EmbeddingService(TestingSessionLocal(), fake)
    try:
        response = client.post("/embeddings/generate", json={"content_ids": [str(item.id)], "dry_run": True})
    finally:
        app.dependency_overrides.pop(get_embedding_service, None)
    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True and payload["completed"] == payload["processed"] == 1
    assert payload["items"][0]["status"] == "would_generate" and fake.calls == 0
    _assert_no_vector(payload)
def test_state_transitions_force_skip_failure_recovery_and_dry_run_are_safe() -> None:
    """Core state transitions use only the fake provider and preserve dry-run state."""
    db,item=content(); good=FakeEmbeddingProvider(); service=EmbeddingService(db,good)
    created=service.generate(item.id); row=db.scalar(select(ContentEmbedding)); created_at=row.embedded_at
    assert created["status"]=="completed" and service.generate(item.id)["status"]=="skipped"
    assert db.scalar(select(ContentEmbedding)).embedded_at==created_at
    assert service.generate(item.id,force=True)["status"]=="completed"
    try: EmbeddingService(db,FakeEmbeddingProvider(fail=True)).generate(item.id,force=True)
    except RuntimeError: pass
    row=db.scalar(select(ContentEmbedding)); assert row.embedding_status=="failed" and row.processing_error=="Embedding provider failed"
    assert service.generate(item.id,force=True)["status"]=="completed"; row=db.scalar(select(ContentEmbedding)); assert row.processing_error is None
    before=(row.embedding_status,row.content_hash,row.updated_at,row.embedded_at)
    item.title="changed"; db.commit(); service.generate(item.id,dry_run=True)
    row=db.scalar(select(ContentEmbedding)); assert (row.embedding_status,row.content_hash,row.updated_at,row.embedded_at)==before
    db.close()
def test_http_generation_uses_overridden_fake_and_get_does_not_need_provider() -> None:
    """HTTP generation is injectable; read endpoints never resolve generation dependencies."""
    db,item=content(); db.close(); fake=FakeEmbeddingProvider()
    def override_service(): return EmbeddingService(TestingSessionLocal(),fake)
    from app.main import app
    app.dependency_overrides[get_embedding_service]=override_service
    try:
        first=client.post(f"/embeddings/contents/{item.id}/generate"); skipped=client.post(f"/embeddings/contents/{item.id}/generate")
        read=client.get(f"/embeddings/contents/{item.id}"); missing=client.post("/embeddings/contents/00000000-0000-0000-0000-000000000000/generate")
        assert first.status_code==200 and skipped.status_code==200 and read.status_code==200 and missing.status_code==404
        assert fake.calls==1 and first.json()["status"]=="completed" and skipped.json()["status"]=="skipped"
        for response in (first,skipped,read,missing): _assert_no_vector(response.json())
    finally: app.dependency_overrides.pop(get_embedding_service,None)
def test_http_generation_failure_is_sanitized_and_recovers_with_override() -> None:
    db,item=content(); db.close()
    from app.main import app
    app.dependency_overrides[get_embedding_service]=lambda: EmbeddingService(TestingSessionLocal(),FakeEmbeddingProvider(fail=True))
    try: failed=client.post(f"/embeddings/contents/{item.id}/generate?force=true")
    finally: app.dependency_overrides.pop(get_embedding_service,None)
    assert failed.status_code==503; _assert_no_vector(failed.json())
    good=FakeEmbeddingProvider(); app.dependency_overrides[get_embedding_service]=lambda: EmbeddingService(TestingSessionLocal(),good)
    try: recovered=client.post(f"/embeddings/contents/{item.id}/generate?force=true")
    finally: app.dependency_overrides.pop(get_embedding_service,None)
    assert recovered.status_code==200 and recovered.json()["status"]=="completed" and good.calls==1
def test_batch_failure_in_middle_isolated_and_metrics_remain_coherent() -> None:
    db,first=content()
    for number in range(2): db.add(Content(source_id=first.source_id,title=f"Batch {number}",url=f"https://test/batch/{number}",fingerprint=f"batch-{number}",processing_status="processed"))
    db.commit(); contents=list(db.scalars(select(Content).order_by(Content.created_at.asc(),Content.id.asc())))
    fake=FakeEmbeddingProvider(fail_indices={1}); result=EmbeddingService(db,fake).generate_batch(content_ids=[item.id for item in contents])
    assert result["processed"]==3 and result["completed"]==2 and result["failed"]==1
    assert result["completed"]+result["skipped"]+result["failed"]==result["processed"]
    assert len(result["items"])==3 and fake.failed_indices==[1]; _assert_no_vector(result)
    db.close()
def test_batch_skip_force_and_dry_run_contract() -> None:
    db,first=content(); second=Content(source_id=first.source_id,title="Second",url="https://test/force",fingerprint="force-second",processing_status="processed"); db.add(second); db.commit(); db.refresh(second)
    fake=FakeEmbeddingProvider(); service=EmbeddingService(db,fake); ids=[first.id,second.id]
    initial=service.generate_batch(content_ids=ids); calls=fake.calls
    skipped=service.generate_batch(content_ids=ids); forced=service.generate_batch(content_ids=ids,force=True)
    first.title="changed first"; second.title="changed second"; db.commit(); dry=service.generate_batch(content_ids=ids,dry_run=True)
    assert initial["completed"]==2 and skipped["skipped"]==2 and fake.calls==calls+2
    assert forced["completed"]==2 and dry["completed"]==2 and dry["dry_run"] is True
    assert all(item["status"]=="would_generate" for item in dry["items"])
    assert dry["completed"]+dry["skipped"]+dry["failed"]==dry["processed"]; _assert_no_vector(dry); db.close()
def test_batch_first_last_and_total_failures_do_not_break_metrics() -> None:
    for failure in ({0},{2},{0,1,2}):
        Base.metadata.drop_all(bind=test_engine); Base.metadata.create_all(bind=test_engine)
        db,first=content()
        for number in range(2): db.add(Content(source_id=first.source_id,title=f"Failure {number}",url=f"https://test/failure/{number}",fingerprint=f"failure-{number}",processing_status="processed"))
        db.commit(); ids=[item.id for item in db.scalars(select(Content).order_by(Content.created_at.asc(),Content.id.asc()))]
        result=EmbeddingService(db,FakeEmbeddingProvider(fail_indices=failure)).generate_batch(content_ids=ids)
        assert result["failed"]==len(failure) and result["completed"]==3-len(failure)
        assert result["completed"]+result["skipped"]+result["failed"]==result["processed"]==3; _assert_no_vector(result); db.close()
def test_listing_filters_stale_before_pagination_without_side_effects() -> None:
    db,first=content(); entries=[first]
    for number in range(4):
        item=Content(source_id=first.source_id,title=f"List {number}",url=f"https://test/list/{number}",fingerprint=f"list-{number}",processing_status="processed"); db.add(item); db.commit(); db.refresh(item); entries.append(item)
    service=EmbeddingService(db,FakeEmbeddingProvider())
    for item in entries: service.generate(item.id)
    # Interleave stale records in the base created_at order.
    entries[1].title="stale one"; entries[3].title="stale two"; db.commit()
    before=[(row.embedding_status,row.content_hash,row.updated_at) for row in db.scalars(select(ContentEmbedding).order_by(ContentEmbedding.id))]
    first_page=client.get("/embeddings?stale=true&page=1&page_size=1"); second_page=client.get("/embeddings?stale=true&page=2&page_size=1"); beyond=client.get("/embeddings?stale=true&page=3&page_size=1")
    assert first_page.json()["total"]==second_page.json()["total"]==2
    assert len(first_page.json()["items"])==len(second_page.json()["items"])==1 and beyond.json()["items"]==[]
    assert first_page.json()["items"][0]["content_id"]!=second_page.json()["items"][0]["content_id"]
    for response in (first_page,second_page,beyond): _assert_no_vector(response.json())
    after=[(row.embedding_status,row.content_hash,row.updated_at) for row in db.scalars(select(ContentEmbedding).order_by(ContentEmbedding.id))]
    assert before==after; db.close()

def _listing_fixture() -> tuple[object, list[Content]]:
    """Persist an ordered mix of usable, stale, failed, and processing metadata."""
    db, first = content()
    records = [first]
    for number in range(5):
        record = Content(
            source_id=first.source_id,
            title=f"Listing {number}",
            url=f"https://test/listing/{number}",
            fingerprint=f"listing-{number}",
            processing_status="processed",
        )
        db.add(record)
        db.flush()
        records.append(record)
    base = datetime(2026, 7, 22, tzinfo=UTC)
    states = ["completed", "completed", "failed", "processing", "completed", "completed"]
    for number, record in enumerate(records):
        digest = content_hash(build_content_embedding_text(record))
        if number in {1, 4}:  # completed but stale
            digest = "0" * 64
        db.add(ContentEmbedding(
            content_id=record.id,
            embedding=[0.1] * 1536 if states[number] == "completed" else None,
            content_hash=digest,
            embedding_status=states[number],
            processing_error="safe failure" if states[number] == "failed" else None,
            embedded_at=base + timedelta(seconds=number) if states[number] == "completed" else None,
            created_at=base if number in {2, 3} else base + timedelta(seconds=number),
            updated_at=base + timedelta(seconds=number),
        ))
    db.commit()
    return db, records

def test_listing_pagination_status_stale_ordering_and_read_contract() -> None:
    """GET filters the complete ordered metadata set without provider resolution or writes."""
    db, _ = _listing_fixture()
    before = [
        (row.id, row.embedding_status, row.content_hash, row.processing_error, row.embedded_at, row.updated_at)
        for row in db.scalars(select(ContentEmbedding).order_by(ContentEmbedding.created_at, ContentEmbedding.id))
    ]
    expected_order = [
        str(row.content_id)
        for row in db.scalars(select(ContentEmbedding).order_by(ContentEmbedding.created_at, ContentEmbedding.id))
    ]
    db.close()
    from app.main import app
    def provider_must_not_resolve() -> object:
        raise AssertionError("GET /embeddings must not resolve an embedding provider")
    app.dependency_overrides[get_embedding_provider] = provider_must_not_resolve
    try:
        defaults = client.get("/embeddings")
        page_one = client.get("/embeddings?page=1&page_size=1")
        page_two = client.get("/embeddings?page=2&page_size=1")
        maximum = client.get("/embeddings?page=1&page_size=100")
        beyond = client.get("/embeddings?page=99&page_size=1")
        completed = client.get("/embeddings?status=completed&page_size=100")
        failed = client.get("/embeddings?status=failed&page_size=100")
        processing = client.get("/embeddings?status=processing&page_size=100")
        stale_true = client.get("/embeddings?stale=true&page_size=1&page=1")
        stale_false = client.get("/embeddings?stale=false&page_size=1&page=1")
        combined = client.get("/embeddings?status=completed&stale=true&page_size=100")
        no_results = client.get("/embeddings?status=pending")
        invalids = [
            client.get("/embeddings?page=0"), client.get("/embeddings?page=-1"),
            client.get("/embeddings?page_size=0"), client.get("/embeddings?page_size=101"),
            client.get("/embeddings?page=one"), client.get("/embeddings?page_size=one"),
            client.get("/embeddings?status=unknown"),
        ]
    finally:
        app.dependency_overrides.pop(get_embedding_provider, None)
    assert defaults.status_code == page_one.status_code == page_two.status_code == maximum.status_code == beyond.status_code == 200
    assert defaults.json()["page"] == 1 and defaults.json()["page_size"] == 20 and defaults.json()["total"] == 6
    assert page_one.json()["total"] == page_two.json()["total"] == maximum.json()["total"] == beyond.json()["total"] == 6
    assert len(page_one.json()["items"]) == len(page_two.json()["items"]) == 1
    assert page_one.json()["items"][0]["content_id"] != page_two.json()["items"][0]["content_id"]
    assert len(maximum.json()["items"]) == 6 and beyond.json()["items"] == []
    assert completed.json()["total"] == 4 and all(item["status"] == "completed" for item in completed.json()["items"])
    assert failed.json()["total"] == 1 and failed.json()["items"][0]["is_stale"] is True
    assert processing.json()["total"] == 1 and processing.json()["items"][0]["is_stale"] is True
    # Only matching completed records are usable: stale=true includes stale completed, failed and processing.
    assert stale_true.json()["total"] == 4 and stale_false.json()["total"] == 2
    assert combined.json()["total"] == 2 and all(item["status"] == "completed" and item["is_stale"] for item in combined.json()["items"])
    assert no_results.json()["total"] == 0 and no_results.json()["items"] == []
    assert all(response.status_code == 422 for response in invalids)
    ordered = maximum.json()["items"]
    assert [item["content_id"] for item in ordered] == [item["content_id"] for item in client.get("/embeddings?page_size=100").json()["items"]]
    assert [item["content_id"] for item in ordered] == expected_order
    for response in [defaults, page_one, page_two, maximum, beyond, completed, failed, processing, stale_true, stale_false, combined, no_results, *invalids]:
        _assert_no_vector(response.json())
    db = TestingSessionLocal()
    after = [
        (row.id, row.embedding_status, row.content_hash, row.processing_error, row.embedded_at, row.updated_at)
        for row in db.scalars(select(ContentEmbedding).order_by(ContentEmbedding.created_at, ContentEmbedding.id))
    ]
    assert before == after
    db.close()
