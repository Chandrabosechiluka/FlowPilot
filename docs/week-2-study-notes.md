# Week 2 study notes: retrieval (RAG), log search, legacy migration

Goal: explain each part aloud without reading the code, run the eval with two embedders and explain
the difference, and add a second legacy connector yourself.

## Read in this order

**Retrieval**
1. `knowledge/README.md`, one runbook and one ticket: what is being searched.
2. `knowledge/documents.py`: front matter, content hash, why duplicate ids are an error.
3. `knowledge/chunking.py`: headings, heading path, contextual headers, size cap with overlap.
4. `knowledge/text.py` and `knowledge/embeddings.py`: tokens; the `Embedder` interface; the hashing trick.
5. `knowledge/stores.py`: cosine similarity with numpy, BM25 by hand.
6. `knowledge/base.py`: idempotent ingest and Reciprocal Rank Fusion. The heart of the week.
7. `knowledge/evaluation.py` and `evals/retrieval.json`: hit@k and MRR.
8. `knowledge/pgvector_store.py`: the same interface in SQL (HNSW and GIN indexes).

**Logs**
9. `logs/parser.py`: GarageFlow's SimpleConsole format; `docker logs -t`; scopes.
10. `logs/store.py`: search, message templates, grouping. `sandbox/logs.py`: the planted incident.

**Migration**
11. `samples/legacy/autoledger/`: read both CSVs and list every problem before reading the code.
12. `migration/connectors.py` (factory), `normalise.py` (rules), `planner.py`, `applier.py`.

**Wiring**
13. `mcp_server/knowledge_tools.py` and the new lifespan in `server.py`.

## Day plan

| Day | Do |
|---|---|
| 1 | Read 1–4. Run `uv run flowpilot-kb search "..."` with five questions of your own. Explain why each top result came first (vector rank, keyword rank). |
| 2 | Read 5–7. Run `uv run flowpilot-kb eval`. Work out by hand the RRF score of one result from its two ranks. |
| 3 | Install Ollama, `ollama pull nomic-embed-text`, set `EMBEDDINGS_PROVIDER=ollama`, run the eval again. Write down both tables. Which questions improved, which got worse, and why? |
| 4 | Logs: start the sandbox, open `sandbox-logs\garageflow.log`, find the incident by eye, then with `summarize_log_errors` in Claude Desktop. Then check your **real** GarageFlow logs (task 1 below). |
| 5 | Migration: run `plan` and read the report line by line against the CSVs. Run `apply`, run it again, delete the ledger and run it again. Explain each result. |
| Weekend | pgvector (optional, Docker): run the live test. Self-test questions aloud. Do-it-yourself tasks. |

## Concepts to be able to explain

**RAG retrieval**
- **What RAG is**: retrieve relevant passages, then give them to the model with the question, so it
  answers from the shop's own documents instead of guessing. Week 2 builds the *R*; the agent in
  Week 3 does the *G*.
- **Why chunk**, why on headings, why keep the heading path, why overlap, why tickets stay whole.
- **Embeddings**: text → vector; similar meaning → small angle; cosine similarity = dot product of
  unit vectors. Why vectors from two models cannot be mixed (so the index records its embedder).
- **The hashing trick**: what it can and cannot match, and why it is still useful (offline, free,
  deterministic tests, a baseline to beat).
- **BM25**: term frequency with diminishing returns, IDF (rare words count more), length normalisation.
- **Hybrid search and RRF**: why both searches; why fuse by rank, not score; the constant 60.
- **hit@k and MRR**; why split "wording" and "paraphrase" questions; why a quality gate in tests.
- **Idempotent ingest**: content hash per document; what it saves with a paid embedding API.
- **pgvector**: `vector(N)` column, `<=>` cosine distance, HNSW (approximate nearest neighbour, fast,
  small chance of missing a match), GIN index over a generated `tsvector`; when you would move from
  memory to Postgres (corpus size, several instances, persistence).

**Logs**
- Why logs are filtered and grouped, not embedded.
- The SimpleConsole single-line format, scopes, `docker logs -t` UTC timestamps.
- Message templates: replacing ids and numbers to group "the same error".
- Following one request across FlowPilot and GarageFlow by correlation ID.

**Migration**
- Factory pattern for vendors (your Solera DMS integrations, rebuilt).
- Plan → review → apply; why apply checks the fingerprint.
- Idempotency: ledger + atomic write + GarageFlow's unique email (409 → link). Why POSTs are not retried.
- Why one bad row does not stop the run but an outage does.
- "Not migrated" fields and cross-customer registrations as **product feedback**.

## Interview story (60 seconds)

> "Week 2 gave FlowPilot memory and hands. Memory: the shop's runbooks and past tickets are chunked on
> headings and indexed for hybrid search, vector similarity for meaning plus BM25 for exact error text,
> fused with Reciprocal Rank Fusion. The embedding model is pluggable, and I measure retrieval with an
> eval set: hit@3 and MRR, split into questions that share words with the answer and paraphrases, so I
> can show what a real embedding model buys over a free baseline. It runs in memory or on Postgres with
> pgvector. Logs are parsed and grouped instead of embedded, so the agent can see that the no-shows job
> has failed every minute for three hours. Hands: a migration tool for onboarding a shop from its old
> DMS, like the 340-dealership CDK-to-Fortellis migration I worked on at Solera: a connector per vendor,
> a reviewable plan, and an apply step that is safe to re-run."

## Self-test questions

1. A query's top chunk has vector rank 3 and keyword rank 1. Another has vector rank 1 and no keyword
   rank. Which wins under RRF (k = 60)? Show the arithmetic.
2. You edit one sentence in `time-zones.md`. What does the next ingest embed, and how does it know?
3. You switch from `hash` to `ollama`. What happens to a pgvector index built yesterday, and why?
4. Why is "the DB is down and nothing works" hard for the hashing embedder?
5. Why does the knowledge search tool return `citation`, and what should the agent do with it?
6. GarageFlow's log line has no date. How does FlowPilot know when it happened?
7. Why are the `no-shows` errors grouped into one pattern although every line differs?
8. The plan says C1006 was merged into C1002. What happens to C1006's car, and to its phone number?
9. Apply crashes after creating customer C1003 but before saving the ledger. What happens on the next run?
10. Why does apply refuse a plan if `customers.csv` changed, even by one row?
11. A shop insists the same car belongs to two customers. What does GarageFlow allow, and what does the plan do?
12. When would you choose pgvector over the in-memory store? When would you not?

## Do it yourself

1. **Check your real GarageFlow logs.** Start GarageFlow in Docker, make a few requests, then run
   `docker logs garageflow-api --tail 20`. Look at a request line: does it contain
   `CorrelationId:...`, or something like ``System.Collections.Generic.Dictionary`2[...]``?
   GarageFlow's `CorrelationIdMiddleware` passes a `Dictionary` to `BeginScope`, and the SimpleConsole
   logger may print a scope with `ToString()`, which for a dictionary is just its type name. If you see
   the type name, that is a real bug in GarageFlow's logging. Fix it in C#:
   `logger.BeginScope("CorrelationId:{CorrelationId}", id)` (a message-template scope prints as text
   and stays structured). Add a test, commit it to GarageFlow, and write down the story: a log
   analysis tool found a bug in the system it monitors.
2. **Second legacy vendor.** Add a connector `servicebook-json` for a JSON export shaped like
   `{"clients": [{"ref": "...", "name": "...", "contact": {"email": "...", "mobile": "..."}}],
   "cars": [{"ref": "...", "client_ref": "...", "plate": "...", "brand": "...", "model": "...", "year": 2020}]}`.
   Register it with `@register("servicebook-json")`, write a sample file and tests. You should not need
   to change the planner at all: if you do, ask yourself why.
3. **New eval cases.** Write five questions of your own into `evals/retrieval.json`, at least two
   paraphrases. Run the eval with both embedders.
4. **A runbook of your own.** Write `knowledge/runbooks/inspection-items.md` (adding items, Critical
   condition, completing an appointment). Ingest; check that only it was embedded; add an eval case for it.

Commit style: `feat(migration): add servicebook-json connector`, `test(kb): add paraphrase eval cases`.
