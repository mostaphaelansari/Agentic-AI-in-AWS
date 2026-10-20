# 2Wiki bridge benchmark — 2026-09-11

**Completed September 11:** all six runs evaluated the same 120 questions.
The primary cohort is the 60 `compositional` and `inference` questions.
Graph retrieval's candidate advantage survives final evidence selection on this
sample, using only content-derived edges.

| Complete-chain recall, primary bridge cohort | Flat | Graph |
| --- | ---: | ---: |
| Candidate pool, K=40 | 41/60 (68.3%) | 60/60 (100%) |
| Final evidence, K=4 | 35/60 (58.3%) | 50/60 (83.3%) |
| **Final evidence, K=5 — primary cutoff** | **38/60 (63.3%)** | **50/60 (83.3%)** |

The primary K=5 gain is **20.0 percentage points**: paired bootstrap 95% interval
**+6.7 to +33.3 points**, exact two-sided McNemar `p=0.0118`. There are 16 graph
wins, four losses, and 40 ties. This is a retrieval result on a balanced,
936-passage diagnostic corpus, not a full-corpus reproduction of HippoRAG or a
measurement of answer F1. MuSiQue remains alongside it below.

## Frozen sample and protocol

The source is the April 7, 2021 release linked by the
[official dataset repository](https://github.com/Alab-NII/2wikimultihop).
Its development split contains 12,576 questions. The downloaded `dev.json` has
SHA-256 `79f77ae104088ea8e25b1a65dbece768d45771194663bc5660ec9a98070dadf5`.

Preparation used seed `9112026`, sample size 120, and all four question types.
There are 30 questions per type and 936 deduplicated passages, including each
case's supplied distractors. The primary cohort therefore has 60 cases; the
comparison controls have 60. This balanced diagnostic sample does not represent
the development split's natural type proportions and is not a full-corpus
reproduction of HippoRAG.

| Setting | Value |
| --- | --- |
| Main cohort | `compositional`, `inference` |
| Comparison controls | `comparison`, `bridge_comparison`, also reported separately |
| Hard baseline | Existing 60-case held-out MuSiQue sample |
| Graph policy | `content-v2`, content-derived edges |
| Embeddings | `granite-embedding:278m` through Ollama |
| Search scope | Pooled, same indexed passages for all 2Wiki strategies |
| Candidate limit / seeds / maximum hops | 40 / 3 / 3 |
| Candidate coverage | K=40, no reranking or selector |
| Final evidence | K=4 for the MuSiQue comparison; K=5 additionally |
| Joint selector | `eu.anthropic.claude-sonnet-4-6`, same for flat and graph |
| Isolated index and namespace | `2wiki-content-v2-20260911` |
| Execution load | Two concurrent strategy jobs; sequential cases within each job |
| Selector failure policy | Invalid submissions after three attempts count as empty contexts; service/authentication errors stop the job for resume |

Freeze these settings before measuring retrieval. Do not choose another sample
or tune traversal based on its scores. Keep the oracle, if run later with
`--graph-source gold-evidence`, in a separate result explicitly marked
`oracle_graph: true`.

## Question types and their evidence patterns

The question type describes how the gold passages must be combined to answer a
question. The counts below describe the usual pattern in this benchmark sample;
the dataset records the actual supporting facts for every individual question.

| Question type | What the system must do | Typical gold passages |
| --- | --- | ---: |
| Compositional | Follow one relation and then another relation explicitly requested by the question. | 2 |
| Inference | Combine facts to infer a relationship that is not stated directly in either passage. | 2 |
| Comparison | Retrieve one fact about each of two subjects named in the question, then compare the values. | 2 |
| Bridge comparison | Discover an intermediate subject for each side, retrieve a property of each discovered subject, and then compare those properties. | 4 |

**Compositional example:** “Who is the father of the director of *The Ride*?”
One gold passage says that *The Ride* was directed by Jan Svěrák, and the other
says that Jan Svěrák is the son of Zdeněk Svěrák. The reasoning follows the
explicit sequence `film -> director -> director's father`.

**Inference example:** “Who is the paternal grandfather of Charlotte Bacon?”
One gold passage identifies Edward Harley as Charlotte's father, and another
identifies John Harley as Edward's father. Neither passage needs to state the
final relationship directly: the system infers that `father's father` means
`paternal grandfather`.

**Comparison example:** “Which film was released first, *Arthur 2: On the
Rocks* or *The Opposite Sex and How to Live with Them*?” One gold passage gives
1988 for the first film and another gives 1993 for the second. The system
compares the two values. The passages can be unrelated in the content graph
because the question itself places their subjects into a comparison.

**Bridge-comparison example:** “Which film has the director born first, *The
Irony of Fate* or *Terror of the Bloodhunters*?” The evidence forms two parallel
chains: `The Irony of Fate -> Eldar Ryazanov -> 1927` and `Terror of the
Bloodhunters -> Jerry Warren -> 1925`. The system must retrieve the two film
passages and the two director passages before comparing the directors' birth
years. A complete chain therefore requires all four gold passages.

`Bridge, primary` is a reporting cohort rather than a fifth question type. It
combines the 30 compositional and 30 inference cases. The comparison and
bridge-comparison cases are reported separately as controls.

## Primary final evidence at K=5

| Cohort | Cases | Flat complete chains | Graph complete chains | Flat mean support recall | Graph mean support recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| **Bridge, primary** | **60** | **38/60 (63.3%)** | **50/60 (83.3%)** | **77.5%** | **83.3%** |
| Compositional | 30 | 17/30 (56.7%) | 27/30 (90.0%) | 73.3% | 90.0% |
| Inference | 30 | 21/30 (70.0%) | 23/30 (76.7%) | 81.7% | 76.7% |
| Comparison | 30 | 28/30 (93.3%) | 28/30 (93.3%) | 93.3% | 93.3% |
| Bridge comparison | 30 | 0/30 (0%) | 29/30 (96.7%) | 60.8% | 99.2% |

The combined bridge cohort supports a complete-chain gain. The observed mean
support-recall gain is smaller, +5.8 points, and its paired 95% interval spans
zero (-5.0 to +16.7 points). The gain is strongest on compositional questions.
Inference alone has six graph wins and four losses (`p=0.754`), and its mean
support recall is lower for graph in this run. Do not claim a demonstrated gain
on every question type.

On the primary cohort, support precision@5 is 31.0% for flat and 33.3% for graph.
First-support Hit@5 and MRR@5 are 91.7% for flat and 83.3% for graph; chain MRR@5
is 0.3117 versus 0.4083. Higher first-support scores for flat coexist with lower
complete-chain recall because flat often returns partial evidence, while failed
graph selections deliver no context.

### Where the remaining failures occur

| Primary bridge cases at K=5 | Flat | Graph |
| --- | ---: | ---: |
| Incomplete candidate pool | 19 | 0 |
| Complete candidate pool, incomplete final context | 3 | 10 |
| Complete final context | 38 | 50 |
| Selector validation failures across all 60 cases | 5 | 10 |

The selector-failure row overlaps the failure stages above. Every graph bridge
failure at K=5 is a selector validation failure, despite complete candidate
coverage: eight non-exact-quote failures and two index-structure failures. All
50 successful graph selections retain the complete gold chain. The five flat
selector failures involve non-exact quotes; some occur on already-incomplete
candidate pools. These failures remain in the denominators rather than being
dropped. Across all 120 questions, selector failures are 8/120 for flat and
12/120 for graph.

No selected passage at K=4 or K=5 was absent from its strategy's separately
measured K=40 candidate pool. The observed loss of complete graph chains is
therefore at final selection in this experiment. The same model, prompt rules,
and budget apply to both strategies; graph candidates can additionally carry
content-derived navigation hints, as implemented by the existing selector.

### Observed latency on the primary cohort

| Stage | Flat median | Graph median | Flat p95 | Graph p95 |
| --- | ---: | ---: | ---: | ---: |
| Candidate retrieval, K=40 | 0.60 s | 0.94 s | 0.81 s | 1.24 s |
| Retrieval plus selector, K=4 | 9.98 s | 11.41 s | 15.61 s | 15.23 s |
| Retrieval plus selector, K=5 | 11.18 s | 12.37 s | 16.64 s | 17.40 s |

Strategy jobs were launched concurrently and each processed its cases
sequentially. K=5 resumed on September 13 after AWS access expired, reusing
91 completed flat cases and 87 completed graph cases. These are observed
single-pass latencies across those sessions, not a repeated load test or a
direct latency comparison with the historical MuSiQue run.

All intervals use 20,000 paired case resamples with seed `9112026`. P-values
for secondary cutoffs and individual types are descriptive and unadjusted for
multiple comparisons; the primary final test is the combined bridge cohort at
K=5.

## Live candidate retrieval at K=40

Both strategies completed all 120 cases on the verified 936-passage pooled
corpus. No retrieval errors occurred and no gold-evidence relations were loaded.

| Cohort | Cases | Flat complete chains | Graph complete chains | Flat mean support recall | Graph mean support recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| **Bridge, primary** | **60** | **41/60 (68.3%)** | **60/60 (100%)** | **84.2%** | **100%** |
| Compositional | 30 | 19/30 (63.3%) | 30/30 (100%) | 81.7% | 100% |
| Inference | 30 | 22/30 (73.3%) | 30/30 (100%) | 86.7% | 100% |
| Comparison | 30 | 30/30 (100%) | 30/30 (100%) | 100% | 100% |
| Bridge comparison | 30 | 0/30 (0%) | 29/30 (96.7%) | 63.3% | 99.2% |

### How to read the candidate-retrieval metrics

For each question, the dataset supplies a set of required, or **gold**, support
passages. Write that set as `G = {G1, G2, ..., Gn}`. The number `n` can differ
between questions. At this stage each retrieval strategy returns up to 40
candidate passages, written as `R@40`. Gold labels are used by the evaluator to
score those candidates; they are not given to the retriever.

| Column or term | Definition | Plain interpretation |
| --- | --- | --- |
| Cohort | A group of questions with the same reasoning pattern. | `Bridge, primary` is the 60-question subtotal formed by the 30 compositional and 30 inference cases; it is not an additional set of questions. |
| Cases | Number of questions in the cohort. | A denominator of 60 means that 60 questions were evaluated. |
| Flat | Semantic retrieval from a ranked, linear list of passages, without traversing content-graph edges. | These columns report what the flat retriever found. |
| Graph | Retrieval that can expand from seed passages or entities through content-derived graph edges. | These columns report what the graph retriever found. |
| Matched supports | `|G intersection R@40|`: the number of unique gold passages present among the candidates. | If the question needs `G1` and `G2` and both appear in the candidates, the matched count is 2. |
| Support recall for one question | `matched supports / number of gold supports`, or `|G intersection R@40| / |G|`. | This gives partial credit. Finding two of four gold passages gives 50% support recall. |
| Complete chain for one question | 1 when every gold passage is in `R@40`; otherwise 0. Equivalently, `G` must be a subset of `R@40`. | If even one required passage is missing, the chain is incomplete. Extra distractor passages do not make an otherwise complete chain fail. |
| Flat/Graph complete chains | Number of questions for which that strategy retrieved every gold passage, divided by `Cases`. | `41/60 (68.3%)` means flat retrieval produced a complete chain for 41 questions and an incomplete chain for 19. |
| Flat/Graph mean support recall | Average of the per-question support-recall values across the cohort. | `84.2%` means that flat retrieval recovered 84.2% of the required passages on average. It does not mean that 84.2% of questions had a complete chain. |

For example, suppose a question has `G = {G1, G2, G3, G4}` and the top-40
candidate pool contains `G1`, `G2`, and `G4`, but not `G3`. Its support recall
is `3 / 4 = 75%`, while its complete-chain result is 0. If all four gold
passages occur anywhere within the top 40, its support recall is 100% and its
complete-chain result is 1. Their exact ranks within the 40 do not affect these
two measurements, provided they remain inside the cutoff.

This difference explains how flat retrieval can have 63.3% mean support recall
on the bridge-comparison cohort while completing 0 of 30 chains: it often finds
some of the four required passages, but misses at least one on every question.
Conversely, graph retrieval finds every required passage in 29 of those 30
questions and nearly all of them in the remaining question, producing 96.7%
complete-chain recall and 99.2% mean support recall.

On the primary cohort, graph has 19 complete-chain wins, 41 ties, and no losses.
Its gain is **31.7 percentage points**, with a paired bootstrap 95% interval of
**+20.0 to +43.3 points** and exact two-sided McNemar `p=0.00000381`. These are
20,000 paired case resamples with seed `9112026`. Scores describe this balanced,
small pooled-corpus diagnostic; they do not establish full-corpus performance.

Ordinary Hit@40 is 100% for both strategies on the bridge cohort, and MRR@40 is
0.9917 for both. These first-support metrics miss the gain in retrieving the
second required passage.

The comparison control behaves as expected at the candidate stage: both
strategies retrieve every required passage. **Bridge comparison is a demanding
four-support test**, despite naming both initial entities. Its result does not
support treating it as an easy flat-RAG control; retain its separate breakdown.

Candidate-stage recall must not be presented as delivered-context recall or
answer accuracy.

## Final evidence at K=4

Both strategies completed all 120 cases. The table includes failed selector
submissions as empty contexts, using the frozen failure policy.

| Cohort | Cases | Flat complete chains | Graph complete chains | Flat mean support recall | Graph mean support recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| **Bridge, primary** | **60** | **35/60 (58.3%)** | **50/60 (83.3%)** | **72.5%** | **84.2%** |
| Compositional | 30 | 15/30 (50.0%) | 24/30 (80.0%) | 65.0% | 80.0% |
| Inference | 30 | 20/30 (66.7%) | 26/30 (86.7%) | 80.0% | 88.3% |
| Comparison | 30 | 30/30 (100%) | 29/30 (96.7%) | 100% | 96.7% |
| Bridge comparison | 30 | 0/30 (0%) | 28/30 (93.3%) | 61.7% | 98.3% |

The primary cohort has 19 graph wins, four losses, and 37 ties on complete-chain
recall. The **25.0-point gain** has a paired bootstrap 95% interval of **+11.7 to
+40.0 points**, with exact two-sided McNemar `p=0.00260`. K=4 is the secondary
cutoff retained for comparison with the historical MuSiQue setup.

Final support precision on the bridge cohort is 36.3% for flat and 42.1% for
graph. First-support Hit@4 and MRR@4 are 86.7% for flat and 85.0% for graph,
while chain MRR@4 is 0.2917 versus 0.4111. Once again, finding the first support
does not measure whether the complete chain was retained.

All 60 graph bridge candidate pools contained the complete chain, but ten lost
it at final selection. Nine of those ten are selector validation failures;
only one returned a valid but incomplete evidence set. Flat has 19 incomplete
candidate pools and six further losses from complete pools. Across all bridge
cases, flat has eight selector failures and graph has nine; across all 120 cases,
the counts are nine and ten. Most validation failures involve non-exact quotes;
the model's submitted indices and evidence structure also sometimes fail checks.

For the bridge cohort, observed end-to-end median latency is 9.98 seconds for
flat and 11.41 seconds for graph; p95 is 15.61 and 15.23 seconds respectively.
The jobs were launched concurrently, and these are single-pass measurements.
Successful-case token usage is recorded, including successful cases' correction
attempts. Usage from ultimately failed selections is not retained by the existing
selector, so the records are insufficient for a complete dollar-cost comparison.

## Measured graph connectivity

These are offline checks against the prepared content graph. Gold labels are
used only to select the passages to inspect, after graph construction.

| Cohort | Cases | All supports connected in pooled graph | Connected using only gold paragraphs and their content entities | Connected by explicit support-title/alias mentions alone |
| --- | ---: | ---: | ---: | ---: |
| **2Wiki bridge, primary** | **60** | **59/60 (98.3%)** | **59/60 (98.3%)** | **54/60 (90.0%)** |
| Compositional | 30 | 29/30 | 29/30 | 27/30 |
| Inference | 30 | 30/30 | 30/30 | 27/30 |
| Comparison | 30 | 9/30 | 3/30 | 0/30 |
| Bridge comparison | 30 | 13/30 | 4/30 | 0/30 |
| **MuSiQue held-out, content-v2** | **60** | **45/60 (75.0%)** | **28/60 (46.7%)** | **11/60 (18.3%)** |
| MuSiQue two-hop | 20 | 17/20 | 14/20 | 9/20 |
| MuSiQue three-hop | 20 | 13/20 | 8/20 | 2/20 |
| MuSiQue four-hop | 20 | 15/20 | 6/20 | 0/20 |

“Connected” means membership in one undirected component with no depth or
candidate limit. It does not establish that question-derived seeds reach every
support within three hops, that retrieval retains those passages in 40
candidates, or that the selector retains them at K=4 or K=5. Title matching
includes the graph builder's conservative aliases; 90% is not an exact-title-only
match rate.

The primary cohort's two supports are much more often connected than MuSiQue's
longer chains. That supports testing this dataset, without establishing a
retrieval advantage. Comparison questions can retrieve both named entities
independently. Bridge-comparison questions in this sample have four gold
paragraphs and can require intermediate entities, so their lower whole-set
connectivity must not be interpreted as an automatic flat-RAG win.

Connectivity is also not a strict recall ceiling for a hybrid retriever. The one
disconnected bridge case, `198dc91e0bdd11eba7f7acde48001122`, has both supports
in flat search at ranks 1 and 12, and in graph-augmented retrieval at ranks 1 and
21. Searching multiple components can therefore supply a complete evidence set
without a single graph path connecting all supports.

## Retained MuSiQue retrieval results

The historical content-v2 run is preserved alongside the new dataset. It used
the 60 held-out cases, 1,057 pooled passages, and the settings above at K=40/K=4.
These existing artifacts are historical; they have not been rerun during this
task, and their code provenance must be checked before treating this 2Wiki
run as an exact comparison of implementations.

| MuSiQue metric | Flat | Graph |
| --- | ---: | ---: |
| Complete chain at K=40 | 55.0% | 58.3% |
| Mean support recall at K=40 | 78.8% | 81.7% |
| Complete chain after selector at K=4 | 41.7% | 46.7% |
| Mean support recall after selector at K=4 | 66.4% | 73.3% |
| Selector contract failures, counted as empty contexts | 7/60 | 4/60 |

Sources are the local `policy-comparison/v2-pool-k40.json` and
`policy-comparison/v2-final-k4.json` artifacts under
`var/multihop/musique-heldout-20260910/`. These differ from the older diagnostic
results in [the metrics example](multihop_retrieval_metrics_example.md), which
used a previous graph/selector configuration. Neither table is answer accuracy.

## Reproduce and resume

```bash
./.venv/bin/python scripts/multihop_graph_rag.py prepare \
  --dataset 2wiki \
  --input var/multihop/2wiki-20260911/source/dev.json \
  --output var/multihop/2wiki-20260911/prepared \
  --split dev --sample-size 120 --seed 9112026

./.venv/bin/python var/multihop/2wiki-20260911/analyze_connectivity.py

aws sso login
bash var/multihop/2wiki-20260911/run_benchmark.sh
```

The local run script indexes the isolated stores, verifies counts, and evaluates
flat and graph at K=40, then K=4 and K=5. A local checkpoint runner invokes the
unchanged production retrieval and selector functions and retains each case's
result for resume. It checks code and prepared-data hashes against the frozen
protocol. Invalid selector submissions after the three built-in attempts are
counted as empty contexts, matching the MuSiQue failure policy. Service,
transport, and authentication errors stop the run for resume rather than being
scored as retrieval failures.

Flat and graph jobs run concurrently, each with sequential cases. The recorded
latency includes retrieval and selection under this two-job load and should not
be directly compared with the older MuSiQue timings. Metrics and uncertainty are
assembled by `summarize_results.py` after both strategies finish all 120 cases.

The reusable CLI now accepts repeated `--question-type` on `prepare` and
`evaluate`. On `prepare`, filtering happens before sampling and changes the
prepared corpus. On `evaluate`, filtering happens before `--limit` and retains
the full indexed corpus. To evaluate only the primary cohort, append:

```bash
--question-type compositional --question-type inference
```

Omit the evaluation filter to report all types together. 2Wiki reports identify
`primary_cohort: bridge` and include pooled case-level metrics and paired
flat comparisons under `question_type_cohorts.bridge` and
`question_type_cohorts.comparison_controls`. Per-type results remain under each
strategy's `by_question_type`. The primary measure is complete-chain recall;
mean support recall, precision, latency, and paired wins/losses accompany it.

The archive, source audit, prepared corpus, case-level connectivity output, and
run scripts are local artifacts under `var/multihop/2wiki-20260911/`, which is
ignored by Git. The CLI and tests are reviewable repository changes. Validation:
71 focused tests passed, Ruff passed, and the shell script passed `bash -n`.

Completed artifacts:

- [Consolidated metrics](../../var/multihop/2wiki-20260911/metrics-summary.json)
- [Candidate K=40, including every case](../../var/multihop/2wiki-20260911/pool-pooled-k40.json)
- [Final K=4, including every case](../../var/multihop/2wiki-20260911/final-pooled-k4.json)
- [Final K=5, including every case](../../var/multihop/2wiki-20260911/final-pooled-k5.json)
- [Frozen protocol and hashes](../../var/multihop/2wiki-20260911/protocol.json)
- [Independent result audit](../../var/multihop/2wiki-20260911/result-audit.json)

`code-snapshot/` retains the frozen source files. `jobs/` retains per-case
checkpoints, and `logs-before-resume-20260913/` preserves the logs from the
interrupted run. Infrastructure failures were resumed and were not scored as
retrieval failures. The checkpoint smoke check confirmed that invalid selections
remain in the denominator and cached cases do not issue new model calls.
The final audit independently recomputed recall and precision from returned IDs
for all 720 case evaluations, checked the case sets and cohort aggregates,
confirmed final contexts were contained in the K=40 pools, and reverified the
source and prepared-data hashes. All checks passed.
