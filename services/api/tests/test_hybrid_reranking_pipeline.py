"""Pure orchestration coverage for the isolated reranking sub-pipeline."""
from uuid import UUID, uuid4
import pytest
from app.repositories.content_hydration_repository import HydratedContent
from app.repositories.rerank_document_repository import RerankDocumentRecord
from app.services.hybrid_reranking_pipeline import HybridRerankingPipeline, MergedRerankCandidate
from app.services.rerank_document_formatter import RerankDocumentFormatter
from app.services.reranking_contracts import ProviderRerankResult
from app.services.reranking_service import RerankingService

class Eligibility:
 def __init__(self, ids=None): self.ids=ids; self.calls=[]
 def filter_eligible(self, ids): self.calls.append(list(ids)); return list(ids if self.ids is None else self.ids)
class Documents:
 def __init__(self, rows): self.rows=rows; self.calls=[]
 def hydrate(self, ids): self.calls.append(list(ids)); return self.rows
class Public:
 def __init__(self, rows): self.rows=rows; self.calls=[]
 def hydrate(self, ids): self.calls.append(list(ids)); return self.rows
class Provider:
 def __init__(self, scores): self.scores=scores; self.calls=[]
 def rerank(self,q,c): self.calls.append((q,list(c))); return [ProviderRerankResult(x.content_id,self.scores[x.content_id]) for x in reversed(c)]

# Phase 2A observability helpers.  They intentionally delegate or return only
# configured data; later phases will use them to assert behavior.
class EventEligibilitySpy(Eligibility):
 def __init__(self, events, ids=None): super().__init__(ids); self.events=events
 def filter_eligible(self, ids): self.events.append("eligibility"); return super().filter_eligible(ids)
class EventPartialHydrationSpy(Documents):
 def __init__(self, events, rows): super().__init__(rows); self.events=events
 def hydrate(self, ids): self.events.append("partial_hydration"); return super().hydrate(ids)
class EventPublicHydrationSpy(Public):
 def __init__(self, events, rows): super().__init__(rows); self.events=events
 def hydrate(self, ids): self.events.append("public_hydration"); return super().hydrate(ids)
class FormatterDelegator:
 def __init__(self, events, aliases=None): self.events=events; self.calls=[]; self.aliases=aliases or {}; self._real=RerankDocumentFormatter()
 def format(self, document):
  alias=self.aliases.get(document.title, str(len(self.calls)+1)); self.events.append(f"formatter:{alias}"); self.calls.append(document)
  return self._real.format(document)
class RerankingServiceDelegator:
 def __init__(self, events, service): self.events=events; self.calls=[]; self._service=service
 def rerank(self, query, candidates): self.events.append("reranking_service"); self.calls.append((query,list(candidates))); return self._service.rerank(query,candidates)
class EventProvider(Provider):
 def __init__(self, events, scores): super().__init__(scores); self.events=events
 def rerank(self, query, candidates): self.events.append("provider"); return super().rerank(query,candidates)
def rec(i): return RerankDocumentRecord(i,"Title",None,None,(),())
def pub(i): return HydratedContent(i,"Title",f"https://x/{i}",None)

def test_pipeline_reranks_graph_before_seed_and_applies_top_k_last():
 seed, graph = uuid4(),uuid4(); provider=Provider({seed:.1,graph:.9}); e=Eligibility(); d=Documents([rec(seed),rec(graph)]); h=Public([pub(graph)])
 result=HybridRerankingPipeline(e,d,RerankDocumentFormatter(),RerankingService(provider),h).run(" q ",[MergedRerankCandidate(seed,("lexical",)),MergedRerankCandidate(graph,("graph",))],1)
 assert result["items"][0]["content_id"]==graph and result["items"][0]["matched_by"]==["graph"] and result["total"]==1
 assert e.calls==[[seed,graph]] and d.calls==[[seed,graph]] and h.calls==[[graph]] and provider.calls[0][0]=="q"
 assert [x.pre_rerank_rank for x in provider.calls[0][1]]==[1,2]

def test_pool_absence_and_public_absence_recalculate_without_backfill():
 first,missing,last=uuid4(),uuid4(),uuid4(); provider=Provider({first:.1,last:.9}); e=Eligibility(); d=Documents([rec(first),rec(last)]); h=Public([pub(last)])
 result=HybridRerankingPipeline(e,d,RerankDocumentFormatter(),RerankingService(provider),h).run("q",[MergedRerankCandidate(first,("vector",)),MergedRerankCandidate(missing,("graph",)),MergedRerankCandidate(last,("lexical","vector"))],3)
 assert [x.pre_rerank_rank for x in provider.calls[0][1]]==[1,2] and h.calls==[[last,first]] and result["items"][0]["rank"]==1 and result["total"]==1

def test_empty_eligible_pool_skips_reranker_and_public_hydration():
 provider=Provider({}); e=Eligibility([]); d=Documents([]); h=Public([])
 assert HybridRerankingPipeline(e,d,RerankDocumentFormatter(),RerankingService(provider),h).run("q",[],20)=={"items":[],"total":0}
 assert e.calls==[[]] and d.calls==[] and provider.calls==h.calls==[]

def test_errors_propagate_without_partial_result():
 class Broken(Eligibility):
  def filter_eligible(self, ids): raise RuntimeError("broken")
 with pytest.raises(RuntimeError): HybridRerankingPipeline(Broken(),Documents([]),RerankDocumentFormatter(),RerankingService(Provider({})),Public([])).run("q",[MergedRerankCandidate(uuid4(),("lexical",))],1)

def test_candidate_pool_is_exactly_100_and_preserves_order_without_backfill():
 ids=[UUID(int=index + 1) for index in range(101)]; provider=Provider({item:float(index) for index,item in enumerate(ids)})
 e=Eligibility(); d=Documents([rec(item) for item in ids]); h=Public([pub(ids[-2])])
 result=HybridRerankingPipeline(e,d,RerankDocumentFormatter(),RerankingService(provider),h).run("query",[MergedRerankCandidate(item,("lexical",)) for item in ids],100)
 assert d.calls==[ids[:100]] and len(provider.calls)==1 and len(provider.calls[0][1])==100
 assert [item.content_id for item in provider.calls[0][1]]==ids[:100] and ids[100] not in h.calls[0]
 assert result["items"]==[{"rank":1,"content_id":ids[-2],"title":"Title","url":f"https://x/{ids[-2]}","summary":None,"matched_by":["lexical"]}]

@pytest.mark.parametrize("stage",["documents","formatter","reranker","public"])
def test_each_downstream_failure_propagates_once_without_following_calls(stage):
 a=uuid4(); e=Eligibility(); d=Documents([rec(a)]); h=Public([pub(a)]); provider=Provider({a:1.0}); formatter=RerankDocumentFormatter()
 if stage=="documents":
  d.hydrate=lambda ids: (_ for _ in ()).throw(RuntimeError("documents"))
 elif stage=="formatter":
  formatter.format=lambda value: (_ for _ in ()).throw(RuntimeError("formatter"))
 elif stage=="reranker": provider.error=RuntimeError("reranker"); provider.rerank=lambda q,c: (_ for _ in ()).throw(provider.error)
 else: h.hydrate=lambda ids: (_ for _ in ()).throw(RuntimeError("public"))
 with pytest.raises(RuntimeError): HybridRerankingPipeline(e,d,formatter,RerankingService(provider),h).run("q",[MergedRerankCandidate(a,("graph",))],1)
 assert len(e.calls)==1
 if stage in ("documents","formatter","reranker"): assert h.calls==[]

def test_nominal_flow_observes_calls_arguments_and_query_boundaries():
 events=[]; a,b,c=UUID(int=1),UUID(int=2),UUID(int=3); query="  Busca MiSta com Unicode: ação  "
 class ObservableCandidates:
  def __init__(self, values): self.values=values; self.iterations=0
  def __iter__(self): self.iterations+=1; return iter(self.values)
 eligibility=EventEligibilitySpy(events); partial=EventPartialHydrationSpy(events,[RerankDocumentRecord(a,"partial-A","sA","cat",("ta",),("ka",)),RerankDocumentRecord(b,"partial-B","sB","cat",("tb",),("kb",)),RerankDocumentRecord(c,"partial-C","sC","cat",("tc",),("kc",))])
 formatter=FormatterDelegator(events,{"partial-A":"A","partial-B":"B","partial-C":"C"}); provider=EventProvider(events,{a:.8,b:.7,c:.9}); reranker=RerankingServiceDelegator(events,RerankingService(provider)); public=EventPublicHydrationSpy(events,[HydratedContent(c,"public-C","https://public/C","PC"),HydratedContent(a,"public-A","https://public/A","PA"),HydratedContent(b,"public-B","https://public/B","PB")])
 merged=ObservableCandidates([MergedRerankCandidate(a,("lexical",)),MergedRerankCandidate(b,("vector",)),MergedRerankCandidate(c,("graph",))])
 result=HybridRerankingPipeline(eligibility,partial,formatter,reranker,public).run(query,merged,3)
 assert events==["eligibility","partial_hydration","formatter:A","formatter:B","formatter:C","reranking_service","provider","public_hydration"]
 assert [len(x.calls) for x in (eligibility,partial,reranker,provider,public)]==[1,1,1,1,1] and len(formatter.calls)==3
 assert eligibility.calls==[[a,b,c]] and partial.calls==[[a,b,c]] and public.calls==[[c,a,b]]
 assert merged.iterations==1
 received_query,candidates=reranker.calls[0]; assert received_query==query and provider.calls[0][0]==query.strip()
 assert [x.content_id for x in candidates]==[a,b,c] and [x.pre_rerank_rank for x in candidates]==[1,2,3] and [x.matched_by for x in candidates]==[("lexical",),("vector",),("graph",)]
 assert [x.document_text for x in candidates]==["Title: partial-A\nSummary: sA\nCategory: cat\nTopics: ta\nKeywords: ka","Title: partial-B\nSummary: sB\nCategory: cat\nTopics: tb\nKeywords: kb","Title: partial-C\nSummary: sC\nCategory: cat\nTopics: tc\nKeywords: kc"]
 assert [item["content_id"] for item in result["items"]]==[c,a,b] and [item["rank"] for item in result["items"]]==[1,2,3] and result["total"]==3
 assert [(item["title"],item["url"],item["summary"],item["matched_by"]) for item in result["items"]]==[("public-C","https://public/C","PC",["graph"]),("public-A","https://public/A","PA",["lexical"]),("public-B","https://public/B","PB",["vector"])]
 assert all(not ({"pre_rerank_rank","document_text","score","rerank_score","provider"}&set(item)) for item in result["items"])

def test_top_k_is_applied_after_reranking_without_duplicate_calls():
 events=[]; a,b,c=UUID(int=11),UUID(int=12),UUID(int=13); e=EventEligibilitySpy(events); d=EventPartialHydrationSpy(events,[rec(a),rec(b),rec(c)]); f=FormatterDelegator(events,{"Title":"x"}); p=EventProvider(events,{a:.1,b:.8,c:.9}); r=RerankingServiceDelegator(events,RerankingService(p)); h=EventPublicHydrationSpy(events,[pub(c),pub(b)])
 out=HybridRerankingPipeline(e,d,f,r,h).run("q",[MergedRerankCandidate(a,("lexical",)),MergedRerankCandidate(b,("vector",)),MergedRerankCandidate(c,("graph",))],2)
 assert h.calls==[[c,b]] and [x["content_id"] for x in out["items"]]==[c,b] and [x["rank"] for x in out["items"]]==[1,2] and out["total"]==2
 assert [len(x.calls) for x in (e,d,r,p,h)]==[1,1,1,1,1] and len(f.calls)==3 and a not in h.calls[0]
 assert events.count("eligibility")==events.count("partial_hydration")==events.count("reranking_service")==events.count("provider")==events.count("public_hydration")==1
 assert h.calls[0]==[c,b] and a not in [item["content_id"] for item in out["items"]] and len(out["items"])==2

def test_graph_can_surpass_seed_through_the_same_reranking_call():
 events=[]; s,g=UUID(int=21),UUID(int=22); e=EventEligibilitySpy(events); d=EventPartialHydrationSpy(events,[rec(s),rec(g)]); f=FormatterDelegator(events); p=EventProvider(events,{s:.1,g:.9}); r=RerankingServiceDelegator(events,RerankingService(p)); h=EventPublicHydrationSpy(events,[pub(g),pub(s)])
 out=HybridRerankingPipeline(e,d,f,r,h).run("q",[MergedRerankCandidate(s,("lexical",)),MergedRerankCandidate(g,("graph",))],2)
 assert h.calls==[[g,s]] and [(x["content_id"],x["matched_by"]) for x in out["items"]]==[(g,["graph"]),(s,["lexical"])]
 assert [len(x.calls) for x in (e,d,r,p,h)]==[1,1,1,1,1] and [x.content_id for x in p.calls[0][1]]==[s,g]

def test_no_eligible_candidates_stop_all_downstream_calls():
 events=[]; a,b=UUID(int=31),UUID(int=32); e=EventEligibilitySpy(events,[]); d=EventPartialHydrationSpy(events,[]); f=FormatterDelegator(events); p=EventProvider(events,{}); r=RerankingServiceDelegator(events,RerankingService(p)); h=EventPublicHydrationSpy(events,[])
 assert HybridRerankingPipeline(e,d,f,r,h).run("q",[MergedRerankCandidate(a,("lexical",)),MergedRerankCandidate(b,("vector",))],2)=={"items":[],"total":0}
 assert e.calls==[[a,b]] and events==["eligibility"] and d.calls==f.calls==r.calls==p.calls==h.calls==[]
