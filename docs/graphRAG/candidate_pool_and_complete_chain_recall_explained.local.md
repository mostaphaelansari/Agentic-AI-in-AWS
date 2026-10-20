# Candidate pools and complete-chain recall, explained from the beginning

A **question** is a request for information; an **answer** is the response to
that request. A **passage** is a piece of source text that the system can search
and return. **Evidence** is information that supports an answer. A **list** is
an ordered collection. A **candidate pool** is a temporary list of passages
available for a later choice. **Recall** measures how much required evidence
was recovered. **Complete-chain recall** measures how often the system recovered
every passage that the reference answer key says a question requires.

This is a detailed companion to
[Candidate pools and complete-chain recall](candidate_pool_and_complete_chain_recall.local.md).
It builds the explanation in dependency order: basic objects first, then the
operations on those objects, then the measurements and their interpretation.
Each specialized term is defined where it enters the explanation. Symbols are
defined before formulas, and code vocabulary is explained before the runnable
example.

A **benchmark** is an evaluation task with specified data and comparison rules.
**2Wiki** is the collection of questions and source evidence used for the main
experiment discussed here. The measurements are taken from the local
[2Wiki benchmark report](2wiki_multihop_benchmark.md), which records the
September 11–13, 2026 experiment. This companion does not report a new experiment.
Examples labeled **illustrative** are teaching examples, not additional measured
results. Implementation details refer to the local source files linked at the
end; the recorded experiment used its own saved copy of the code.

The chapter links provide navigation; each chapter introduces its vocabulary
before developing the explanation.

| Reading order | Chapters |
| --- | --- |
| Understand the task | [1. Basic objects](#1-the-basic-objects-questions-text-evidence-and-answer-keys) · [2. Multi-hop reasoning](#2-what-multi-hop-reasoning-means) · [3. Question to evidence](#3-from-a-question-to-selected-evidence) |
| Understand the system | [4. Ordinary search](#4-how-ordinary-search-finds-candidate-passages) · [5. Graph search](#5-how-the-graph-creates-another-way-to-find-evidence) · [6. Final selection](#6-how-the-final-evidence-selector-works) · [7. Dataset records](#7-how-a-dataset-record-becomes-a-gold-passage-set) |
| Calculate the measurements | [8. Arithmetic vocabulary](#8-the-arithmetic-vocabulary-for-every-metric) · [9. Recall and precision](#9-recall-precision-and-complete-chain-success) · [10. Ranking measurements](#10-first-support-ranking-versus-whole-chain-ranking) |
| Read the experiment | [11. Measured question](#11-following-one-measured-question-through-both-strategies) · [12. Sample](#12-understanding-the-sample-before-reading-its-percentages) · [13. Results](#13-reading-the-reported-complete-chain-results) · [14. Paired comparisons](#14-paired-comparisons-wins-losses-ties-and-rescued-supports) · [15. Failure stages](#15-locating-where-a-chain-was-lost) |
| Understand the limits | [16. Uncertainty](#16-understanding-the-uncertainty-calculations) · [17. Timing and usage](#17-timing-model-usage-and-diagnostic-fields) · [18. Connectivity and MuSiQue](#18-graph-connectivity-and-the-comparison-with-musique) · [19. Settings](#19-which-settings-can-change-the-result-and-why) |
| Practice and inspect | [20. Runnable program](#20-a-small-program-that-demonstrates-the-mechanism) · [21. Common questions](#21-questions-to-ask-when-interpreting-a-result) · [22. Exercises](#22-small-exercises-with-answers) · [23. Source and field references](#23-finding-these-concepts-in-the-project) |

## 1. The basic objects: questions, text, evidence, and answer keys

### 1.1 A question and an answer are different from supporting evidence

A **question** is the request the system is trying to resolve. An **answer** is
the response to that request. A **supporting fact** is a statement used to
justify the answer.

Consider this question from the report:

> Who is the father of the director of film *The Ride (1994 Film)*?

The report gives two supporting facts:

1. *The Ride* was directed by Jan Svěrák.
2. Jan Svěrák is the son of Zdeněk Svěrák.

Combining those facts supports the answer **Zdeněk Svěrák**.

Merely returning that name is different from returning the text that supports
it. A system might guess the name, remember it from earlier training, or find it
in an unrelated sentence. This experiment measures whether the supporting
passages were returned.

### 1.2 Article, sentence, paragraph, passage, and document

A **record** is one stored unit of information. These words describe different
ways of dividing source text and representing it as records:

| Term | Definition | Meaning in this project |
| --- | --- | --- |
| Article | A source text about a subject, usually identified by a title. | For example, an article titled *Jan Svěrák*. |
| Sentence | A unit of text expressing a statement or related statements. | The source can mark one particular sentence as supporting evidence. |
| Paragraph | A block of text within a source. | The implementation uses “paragraph” in the names of searchable records. |
| Passage | The text unit treated as one search item. | In the prepared 2Wiki data, the sentences supplied together for one article entry are joined into one passage. |
| Document | A stored record containing text and associated information. | A search document can represent one passage rather than an entire article. |

The prepared passage is the unit the measurements count. It should not be assumed
to be a complete original Wikipedia article or exactly one typographic
paragraph. It is the text supplied and prepared for this experiment.

An **identifier**, abbreviated **ID**, is a value used to distinguish one
record from another. A passage ID lets comparison code ask whether the same
passage was returned without comparing long strings of text every time.

**Metadata** means information stored alongside the passage text, such as its
title, source collection, or identifier.

### 1.3 Dataset, case, corpus, and benchmark

A **dataset** is an organized collection of records. In this task, those records
contain questions, source passages, and reference answers or evidence labels.

A **case** is one question together with its associated information. One case
can have several supporting passages.

A **corpus** is the collection of passages that can be searched. The corpus is
the search material; the set of cases is the set of questions being evaluated.
Their sizes measure different things.

A **benchmark** is an evaluation task with specified data, rules, and
measurements for comparing systems. An **evaluator** is the code that calculates
those measurements from the systems' outputs and the reference information.

In the reported 2Wiki experiment:

```text
120 cases            = 120 questions to evaluate
936 corpus passages  = 936 distinct searchable text records
```

These numbers do not imply that every question requires 936 passages, or that
there is one passage per question.

### 1.4 Gold labels, gold passages, and gold answers

A **label** is a reference annotation attached to data. An **annotation** is
information marking how the data should be interpreted or evaluated.

**Gold** means the benchmark treats an annotation as its reference answer key.
It is not a score assigned by the search system.

| Term | Definition | Example from the reported case |
| --- | --- | --- |
| Gold passage | A passage whose ID is labeled as required supporting evidence for the question. | The passage about *The Ride*. |
| Gold supporting fact | A specific sentence labeled as required evidence. | The sentence naming the film's director. |
| Gold relation | A structured expression of a relationship in the reference evidence. | The film's director is Jan Svěrák. |
| Gold answer | The expected final answer. | Zdeněk Svěrák. |

The same passage can be gold for one question and irrelevant to another.
“Gold” is therefore a relationship between a passage and a particular question,
not a permanent quality rating for the passage.

A **distractor** is a passage outside that question's labeled gold passages. It may
share names or vocabulary with the question. It may even contain useful
background. The passage-level measurements here give it no gold-support credit.

This distinction also limits the measurement: if a different passage provides
valid alternative evidence but has no gold label, this evaluator does not
automatically recognize it as an equivalent substitute.

### 1.5 A list preserves order; a set records membership

A **list** is an ordered collection. A **rank** is an item's position in a
ranked list, starting at 1 for the first item.

A **set** is a collection of distinct items whose order does not matter.
**Distinct** and **unique** both mean that duplicates count only once.

We use these conventions throughout:

| Notation | Meaning |
| --- | --- |
| `G1`, `G2`, `G3` | Short names for gold passage IDs in an example. |
| `D1`, `D2`, `D3` | Short names for distractor passage IDs. |
| `[D1, G1, G2]` | A list: D1 is first, G1 second, G2 third. |
| `{G1, G2}` | A set: G1 and G2 are members, with no ranking implied. |
| `[]` | An empty list: no passages were returned. |

A search returns an ordered list. The evaluator uses both its order and its
set of unique IDs, depending on the measurement.

### 1.6 Why the answer key must be kept separate

A **retriever** is the component that finds passages for a question. An
**evidence selector** is the component that chooses which retrieved passages
to retain for use together.

**Label leakage** means reference information that should be reserved for
evaluation influences the system being evaluated. For example, telling a
selector “G1 and G2 are the correct passages” would reveal the answer to the
evidence-selection task.

The intended separation is:

```text
Retriever sees: question and searchable content.
Selector sees: question and candidate passage content.
Evaluator sees: returned IDs and the hidden gold IDs.
```

The useful facts themselves must be present in searchable text. What remains
hidden is the privileged annotation saying which passages and relations are
the reference answer for this question.

## 2. What multi-hop reasoning means

**Reasoning** means combining or interpreting information to reach a conclusion.
A **reasoning hop** is one step in the fact relationship needed to resolve the
question. **Multi-hop** means more than one such step is needed.

An **entity** is an identifiable subject, such as a person, film, organization,
or place. A **relation** is a connection with a particular meaning between
subjects, such as “directed by” or “father of.”

An **intermediate entity**, also called a **bridge entity**, is a subject
discovered along the way that connects one reasoning step to the next.

For the reported film question:

```text
Starting subject:      The Ride
First relation:        director
Intermediate entity:   Jan Svěrák
Second relation:       father
Requested answer:      Zdeněk Svěrák
```

We can draw the reasoning with arrows, where each arrow means “follow the named
relationship”:

```text
The Ride --director--> Jan Svěrák --father--> Zdeněk Svěrák
```

An **evidence chain** is the collection of facts or passages needed to support
those reasoning steps. A **metric** is a precisely defined measurement.
In this report, a **complete chain** has a precise
measurement meaning: every unique gold passage ID is present in the evaluated
result list.

That definition does not require the returned list to be in logical reasoning
order. If both gold passages are returned in reverse order, the chain is still
complete for this metric. Their order can affect the ranking measurements
introduced later.

It also does not require the evaluator to prove that a model understood the
relationships. The evaluator checks passage IDs.

### 2.1 Three different counts that can look similar

| Count | What it counts | Why it can differ from the others |
| --- | --- | --- |
| Reasoning-hop count | Steps needed to combine facts. | Several steps can sometimes be supported by one passage. |
| Supporting-fact count | Sentences labeled as evidence. | Several labeled sentences can occur in the same passage. |
| Gold-passage count | Distinct passage IDs labeled as required. | This is the count used in passage recall. |

**Preparation** means converting source data into the records used by the
project. In this project's 2Wiki preparation, `hop_count` is populated from the
number of unique supporting titles. It is a dataset-preparation convention,
not an independent measurement of the operations needed to reason out an answer.

### 2.2 The four question types in the reported sample

A **question type** is a category describing how evidence must be combined.
A **property** is an attribute of a subject, such as a birth year. A
**paternal grandfather** is a person's father's father.

| Type | Definition | Illustrative pattern |
| --- | --- | --- |
| Compositional | Follow a sequence of relationships requested in the question. | Film → director → director's father. |
| Inference | Combine facts to establish a relationship that the evidence need not name directly. | Person → father → father's father, then infer “paternal grandfather.” |
| Comparison | Retrieve a property of each of two named subjects and compare the values. | Film A → release year; Film B → release year; compare years. |
| Bridge comparison | Discover an intermediate subject for each side, obtain a property of each, and compare the properties. | Film A → director A → birth year; Film B → director B → birth year. |

“Chain” is used broadly in the metric name. The two branches of a
bridge-comparison question do not have to form one linear sequence. Complete-chain
recall still asks whether all required passages from both branches are present.

## 3. From a question to selected evidence

### 3.1 Retrieval and generation

**Retrieval** is finding stored information relevant to a request.
**Generation** is producing new response text.

A **language model** is a trained program that processes and produces language.
The abbreviation **LLM** means **large language model**. An **answering model**
is a language model used to produce the final answer.

**Retrieval-augmented generation**, abbreviated **RAG**, is an approach in which
retrieved information is supplied to a model to help it generate an answer.

The **context** is the information supplied for a particular model task. In
this report, **final context** means the selected passage evidence that could
be supplied to an answering model. It is not the entire searchable corpus.

The measured experiment stops after evidence selection. A language model is
used as the selector, but there is no measured answer-generation stage.

### 3.2 Candidate pool versus final evidence

A candidate passage is a possible choice. It has not necessarily been selected
for the final context.

The two stages serve different purposes:

| Stage | Definition | Question it is intended to resolve |
| --- | --- | --- |
| Candidate retrieval | Produce a wider list of potentially useful passages. | Is the required evidence available for selection? |
| Final evidence selection | Choose a smaller collection from that list. | Which passages should be used together? |

A **budget** is a limit on how much of a resource can be used. Here the passage
budget limits the number of passages, not the number of words or questions.

The measured flow is:

```text
936 searchable passages
          |
          | retrieve separately for each question
          v
up to 40 candidate passages for that question
          |
          | choose evidence together
          v
4 or 5 final passages on a successful full-budget selection
          |
          | compare their IDs with the question's gold IDs
          v
measurements for that question
```

Every question has its own candidate pool. Sixty evaluated questions do not
share one 40-passage pool.

### 3.3 What K and @K mean

**K** is the number of ranked positions allowed at a particular stage. A
**cutoff** is the boundary after which ranked items are ignored.

**Top K** means the first K items in a ranked list. The suffix **@K** means
“calculate this measurement using only those first K items.”

| Expression | Definition |
| --- | --- |
| Candidate `K=40` | Evaluate up to the first 40 candidate passages. |
| Final `K=5` | Evaluate up to the first five final selected passages. |
| `candidate_limit=40` | The configured candidate-pool budget. |
| `top_k=5` | The configured final passage budget. |

K does not determine the number of gold passages. The question's annotations
determine that number.

For a two-gold question:

```text
Gold at candidate ranks 3 and 27: complete at K=40.
Gold at candidate ranks 3 and 41: incomplete at K=40.
Gold only at candidate rank 1:   incomplete at K=40.
```

The second result becomes complete at K=41 only if we are extending that same
ranked list and the other support remains present.

### 3.4 Why a larger working pool is useful

The initial search may rank a passage weakly because it contains an
intermediate person's name that the question does not mention. That passage
can become obviously useful when read alongside another candidate.

For example, the question mentions *The Ride*, but the second required passage
is titled *Jan Svěrák*. A selector that sees both can connect them.

With only five candidates, a passage ranked eighth is unavailable. With 40
candidates, it is available and can be promoted into the final five.

**Promotion** means placing a passage earlier in a later output than it
appeared in the earlier ranking. Candidate rank 8 can become final rank 2.
There is no requirement to preserve candidate order.

### 3.5 Exactly K, fewer than K, and an empty context

The normal reported selector has enough candidates to request exactly four or
five distinct passages. The code requests the smaller of `top_k` and the number
of available candidates.

For example:

```text
40 candidates, top_k=5: request exactly 5 selected passages.
 3 candidates, top_k=5: request exactly 3 selected passages.
 0 candidates:          return no selected passages.
```

An **empty context** is a final list containing no passages. The reported
failure policy also assigns an empty context when a selector submission remains
invalid after its allowed attempts.

Thus “up to five returned passages” describes the outer limit, while “exactly
five” describes a valid selection when five usable candidates are available.

### 3.6 The candidate pool sets a limit on selection

A **subset** is a set whose every member belongs to another set. Because this
selector can only choose candidates, its final passage set is a subset of its
candidate passage set.

Therefore:

```text
A gold passage absent from the candidate pool
cannot be selected into the final context from that pool.
```

A **ceiling** is an upper limit. Candidate complete-chain recall is a ceiling
on final complete-chain recall when the final outputs come from those same
pools and the same cases are scored under the same labels.

This condition matters when candidate and final results were collected in
separate runs. The benchmark report says that the final passage IDs were
checked against the separately measured candidate pools; none fell outside its
strategy's corresponding pool.

## 4. How ordinary search finds candidate passages

### 4.1 Search index and search query

A **search index** is a stored organization of documents that makes searching
them efficient. **Indexing** is creating or updating that organization.

A **query** is the request submitted to a search system. Here the query is
derived from the user's question.

**OpenSearch** is the search software used by this implementation to store and
search passage documents. Both retrieval strategies start with OpenSearch.

### 4.2 Lexical search

**Lexical search**, also called **keyword search**, finds and ranks text using
words appearing in the query and documents.

A **field** is a named part of a stored document, such as `title` or `text`.
A **field weight** adjusts how strongly matches in that field influence the
search calculation.

The implementation searches these fields:

| Field | What it contains | Configured weight |
| --- | --- | ---: |
| `title` | The passage's title. | 3 |
| `about_entity_name` | The entity represented by the title. | 2 |
| `entity_names` | Entity names found in the passage. | 2 |
| `text` | The passage content. | 1 |

The notation `title^3` in the search code specifies the title boost. These
weights influence the scoring calculation; they do not mean a title match has
three times the likelihood of being correct.

Keyword search can be especially useful for exact names such as a film title.
It can still miss a relevant biography if the biography does not repeat the
film's name.

### 4.3 Embeddings, vectors, and semantic search

A **vector** is an ordered list of numbers. For example, `[0.2, -0.1, 0.7]` is
a vector with three numbers. A **dimension** is one position in that vector.

An **embedding** is a numerical representation of an item, such as a passage
or question. An **embedding model** is the trained program that creates it.

**Semantic similarity** means similarity in meaning. **Vector search** compares
embeddings to find nearby representations, which the model is trained to make
useful for finding related text. In this context it is also called **semantic
search**.

The individual vector numbers are not labeled facts such as “is a director.”
Closeness in this representation is a search signal, not proof that a passage
contains the required evidence.

The reported embedding model is `granite-embedding:278m`. **Ollama** is the
software used to run that model in this setup. A **model identifier** names
which model configuration to use. This identifier records the experiment's
configuration; it is not a recommendation about which model to use today.

Passage embeddings and question embeddings need compatible representations.
Changing only the question model while leaving an incompatible passage index
can make their vector comparisons meaningless.

### 4.4 Hybrid search and reciprocal-rank fusion

**Hybrid search** combines more than one search method. Here it combines lexical
search with vector search.

A **retrieval score** is a number used to order passages before evaluation.
It differs from an **evaluation score**, which measures the returned evidence
against the hidden answer key.

**Reciprocal-rank fusion**, abbreviated **RRF**, combines ranked lists by giving
each passage a contribution based on its rank in each list. **Reciprocal**
means one divided by a number. **Fusion** here means combining the contributions.

Before writing the formula, define:

| Symbol | Definition |
| --- | --- |
| `r` | The passage's rank in one search list, starting at 1. |
| `c` | A fixed rank constant that moderates the difference between rank contributions. |
| `+` | Add contributions when more than one search list contains the passage. |

The contribution from one list is:

```text
contribution = 1 / (c + r)
```

The symbol `≈` means “approximately equal to,” allowing rounding.
The implementation uses `c=60`. If a passage is present in both lists, add both
contributions. If it is absent from one returned list, that list contributes
zero.

For an illustrative passage at vector rank 2 and lexical rank 5:

```text
RRF score = 1 / (60 + 2) + 1 / (60 + 5)
          = 1 / 62       + 1 / 65
          ≈ 0.03151
```

Another passage found only at vector rank 1 gets:

```text
RRF score = 1 / 61 ≈ 0.01639
```

The first passage ranks above the second under these illustrative inputs
because two search methods contributed to it.

The RRF constant 60 is unrelated to the 60 questions in the main reported group.
Neither an RRF score nor an embedding similarity establishes that a passage is
gold support.

### 4.5 Flat retrieval

A **graph** is a structure representing things and connections between them.
**Flat retrieval** is the strategy that uses the ordinary search results
without following graph connections between passages. “Flat” describes this
retrieval structure; it does not mean keyword-only search.

In this benchmark, Flat uses hybrid search, creates its candidate pool, and
uses the same type of evidence selector as Graph for the final runs.

Flat can find every required passage if all of them rank inside its pool.
Multi-hop evidence does not automatically require graph retrieval.

## 5. How the graph creates another way to find evidence

### 5.1 Graph, node, edge, and path

A **graph** is a structure consisting of things and connections between them.
A **node** is one of the represented things. An **edge** is one stored
connection between nodes.

A **path** is a sequence of connected nodes and edges. **Traversal** means
following those connections. A **neighbor** is a node connected by an edge.
Two nodes are **connected** if an allowed path exists between them.

**Neptune Analytics** is the graph service used by this implementation to store
these nodes and find paths. It provides another route to passage IDs;
OpenSearch supplies the passage documents used by retrieval.

### 5.2 The three node types

A **node type** identifies what kind of object a node represents.
**Normalization** means applying consistent text transformations so equivalent
forms can be matched more easily. Examples in this project include consistent
letter casing and whitespace handling.

**Pooled search** lets a question search the combined passage corpus.
**Case-controlled search** restricts it to passages supplied with its own
dataset case. Both useful and distractor passages can belong to that context.

| Node type in code | Definition | Example |
| --- | --- | --- |
| `BenchmarkParagraph` | One prepared passage with its ID and title. | The passage titled *The Ride (1994 film)*. |
| `BenchmarkEntity` | One normalized named subject shared across text records. | Jan Svěrák. |
| `BenchmarkCase` | A stored benchmark case and its source-context associations. | The case asking for the director's father. |

Although case nodes are stored, normal pooled retrieval does not begin by
walking from the question's case node. It starts from passages found by search.
The submitted question is not itself a new permanent traversal node.

### 5.3 The edge types

A **directed edge** has a stored source and target. Arrow notation shows that
direction. The notation `-[:ABOUT]->` means an edge labeled `ABOUT` points from
the item on its left to the item on its right.

| Edge | Definition | Use in the reported normal graph |
| --- | --- | --- |
| `Paragraph -[:ABOUT]-> Entity` | The paragraph title identifies its subject entity. | Used. |
| `Paragraph -[:MENTIONS]-> Entity` | The paragraph content contains a recognized mention of an entity. | Used. |
| `Case -[:HAS_PARAGRAPH]-> Paragraph` | The passage belongs to the context originally supplied with that case. | Stored for controlled search scope; it is not a gold-support label. |
| `Entity -[:RELATED_TO]-> Entity` | A separately supplied relationship connects two entities. | Dataset gold-evidence relationships are excluded from the reported normal run. |

These search scopes are different tasks. Restricting the search to one case
reduces the competing passages and cannot be silently substituted for pooled
search when comparing scores.

### 5.4 A graph path is not the same thing as a reasoning relation

For the reported film example, the stored connections allow:

```text
Passage: The Ride
    --MENTIONS--> Entity: Jan Svěrák
    <--ABOUT--    Passage: Jan Svěrák
```

The second arrow points toward the entity because the biography passage is
about that entity.

**Undirected traversal** means following an allowed edge in either direction,
regardless of its stored arrow. This traversal can move from the film passage
to the shared entity and then to the biography passage.

There is no direct passage-to-passage edge here. Two stored edges connect the
passages through an entity node.

The graph path says, approximately, “these texts are connected through this
subject.” The text must still establish “this person directed the film” and
“this other person is his father.” A mention alone does not establish either
relationship.

### 5.5 Content-derived edges and oracle edges

**Content-derived** means built from the searchable passage content and titles,
without consulting the question's gold evidence labels.

An **oracle** in an experiment is a method given privileged correct information
to help investigate behavior that the normal system cannot directly achieve.
The `gold-evidence` graph
mode is an oracle because it can use the dataset's reference relations.

The normal run uses `graph_source=content-derived`. The oracle mode uses
`graph_source=gold-evidence` and must be reported separately.

The normal graph can contain answer-bearing text or an entity whose name is
the eventual answer. That is legitimate searchable content. What it must not
use is a trusted relationship imported from the hidden reference answer key
to solve the ordinary retrieval task.

### 5.6 How content-derived entities and links are extracted

**Entity extraction** is identifying entity names in text. An **alias** is an
alternative written form used to refer to an entity. A **matching rule** is a
specified procedure for deciding whether text contains such a name.
**Disambiguation** means distinguishing subjects with similar names. The
parenthesized text in *The Ride (1994 film)* helps distinguish that film from
other works called *The Ride*.

The construction policy named `content-v2` uses these kinds of rules:

1. Link a passage's title to its subject entity with `ABOUT`.
2. Look for other corpus titles in the passage text.
3. Accept conservative title aliases, such as a title without a final
   parenthesized disambiguation.
4. Connect repeated multi-word named phrases found across passages.
5. Extract some single-word place names when nearby words indicate a location.

These rules are deliberately limited. A **pronoun**, such as “he,” refers to
something without naming it again. A **paraphrase** expresses an idea with
different words. An **implicit relationship** must be inferred rather than
being explicitly stated. Such forms may fail to create the expected content
edge.

Conversely, an ambiguous name may connect unrelated passages. More extracted
connections do not automatically mean more useful connections.

### 5.7 Seeds, depth, and graph distance

A **seed passage** is a passage chosen as a starting point for traversal.
**Expansion** means discovering more passages by traversing from seeds.

The setting `seed_limit=3` means the first three ordinary search passages are
used as starting passages, when three are available.

**Traversal depth** limits how far expansion can move. A **paragraph-level
hop** is one move from a passage through an entity to another passage.

In the normal content graph:

```text
Passage → entity → passage
       one paragraph-level hop
       two stored edges
```

The setting `max_hops=3` allows up to three such passage-to-passage moves,
implemented as a maximum path length of six stored edges.

**Graph distance** is the length of a path. In the current returned
`graph_distance` field, that length counts stored edges. A displayed distance
of 2 can therefore represent one paragraph-level hop.

None of these limits defines the dataset's reasoning-hop count. In the film
example, the question requires two reasoning relationships, while one
paragraph-level graph hop can connect its two evidence passages.

### 5.8 Shortest paths and provenance

A **shortest path** is a path with the fewest stored edges among the paths under
consideration. **Provenance** is the record of where an item came from and how
it was obtained.

When traversal discovers a passage through several routes, the implementation
keeps a shortest discovered route for its graph provenance. “Shortest” does
not mean the passage is more factually relevant; it means fewer graph edges
were crossed.

The retriever asks OpenSearch for the documents corresponding to discovered
IDs. The code sometimes calls this **hydration**, meaning filling an ID-only
search result with the passage's full stored document.

### 5.9 Combining graph passages with ordinary search

A **fallback passage** in this retrieval stage is an ordinary search result
kept as an alternative source of evidence. It does not imply an error occurred.

**Interleaving** means alternating items from two lists. **Pinning** a seed
means reserving its place at the beginning of the candidate pool.

Graph retrieval does the following with the measured settings:

1. Run hybrid search and choose up to three seeds.
2. Traverse from each seed within the depth and result limits.
3. Keep one representative path for each reached passage and fetch its text.
4. Keep the seeds at the beginning of the candidate list.
5. Alternate graph-reached passages with remaining ordinary search passages.
6. Keep each passage once and cut the combined pool to the candidate budget.

**Deduplication** means removing repeated records so one passage does not use
several slots. The graph and ordinary-search lists are combined by passage ID.

Graph candidates are ordered primarily by shorter graph distance; score and ID
help resolve ordering among candidates at the same distance. The whole mixed
pool is not simply every document globally sorted by its ordinary search score.

The result-source labels are:

| Label | Definition |
| --- | --- |
| `seed` | An initial search passage reserved as a traversal starting point. |
| `graph` | A passage found through traversal and absent from the ordinary search candidate list. |
| `fallback` | An ordinary search passage that is not included through the graph-reached branch. |
| `both` | A non-seed passage found by both ordinary search and traversal. |

These labels describe origin, not correctness. A `graph` passage may be a
distractor, and a `fallback` passage may be gold evidence.

### 5.10 Degree, hubs, and navigation hints

The **degree** of a node is the number of stored connections touching it. A
**hub** is a node with many connections compared with other nodes.

A common place or broad subject can become a hub connecting many unrelated
passages. Such paths can be noisy, meaning they frequently lead to information
that does not help answer this question.

A **navigation hint** is information about a route that may help the selector
notice related evidence. The optional `graph_connection` field provides one.

The selector withholds a path hint if supplied entity-degree information shows
a degree above 6. This does not remove the candidate passage, delete the hub,
or stop traversal through it. It limits the hints shown to the selector.

A connection such as `The Ride → Jan Svěrák → Jan Svěrák` can repeat the same
display name because one occurrence represents an entity node and the next a
passage node. They are distinct objects with similar displayed text.

## 6. How the final evidence selector works

### 6.1 Reranking versus joint selection

A **reranker** is a component that assigns a new ordering to retrieved
passages. A typical passage reranker evaluates how relevant individual passages
are to a question.

**Joint evidence selection** means choosing passages as a collection, taking
account of how they complement each other. **Complementary evidence** supplies
different needed facts; **redundant evidence** repeats information already
available from another selected passage.

Five individually relevant passages about a film could all repeat the
director's name while omitting the director's father. A useful joint selection
needs to preserve the biography as well as the film passage.

The reported final runs use the joint selector. Candidate K=40 measurements
were collected without a selector or reranker.

### 6.2 What the model receives

A **prompt** is the input and instructions supplied to a language model for
one task. A **model identifier** specifies which model configuration to call.

The reported selector model identifier is
`eu.anthropic.claude-sonnet-4-6`. **Amazon Bedrock** is the service through which
this implementation calls it.

The selector receives the question, the requested passage budget, passage
titles and text, temporary passage indices, and eligible graph navigation
hints. It does not receive the gold passage IDs, gold answer, or reference
reasoning relations.

An **index**, in this selector context, is a temporary numerical position used
to refer to a candidate. This differs from a search index, which is a stored
search structure.

The code sorts candidates by their IDs before numbering them for the prompt.
These indices start at 0. Therefore:

```text
selector index 0  = first item in the selector's ID-sorted input
retrieval rank 1  = first item in the earlier retrieval ordering
```

They need not refer to the same passage. The selector returns indices ordered
by usefulness, which establishes the final result ordering.

### 6.3 The submission contract

A **contract** is a set of rules a component's output must satisfy. A
**validator** is code that checks those rules. A **valid submission** passes
those checks.

A **character** is one text element counted by the program, such as a letter,
space, or punctuation mark.

For a normal five-passage selection, the contract requires:

| Requirement | Definition and purpose |
| --- | --- |
| Exactly five indices | Fill the requested budget when at least five candidates are available. |
| Distinct indices | Do not select the same passage twice. |
| In-range integer indices | Use whole-number positions that actually exist in the candidate list. |
| One evidence entry per selected index | Associate each chosen passage with its own explanation. |
| An exact quote | Copy a nonempty excerpt of at most 600 characters from the source passage. |
| A nonempty role | Describe the fact or relationship that the passage supports, or a missing link. |
| At most two passages from one article | Limit concentration on one title when the available pool can satisfy that cap. |

The article-diversity check compares titles after normalizing casing and
surrounding spaces. The code relaxes that cap when the available titled pool is
too narrow to satisfy it; the prompt still asks for diversity.

### 6.4 Exact quotation does not prove semantic correctness

**Semantic correctness** means that the claimed meaning is actually supported.

The quote validator checks that the quote occurs exactly in the source text.
It does not independently prove that the role is true, that a pronoun was
resolved correctly, or that the selected collection answers the question.

For example, an exact quote saying that someone attended a film premiere would
not prove that the person directed the film. A supplied role claiming
“identifies the director” could still be semantically wrong even if the quote
passes its exact-text check.

There are therefore three separate checks or outcomes:

```text
Valid submission:      output structure and quotation rules passed.
Complete gold support: evaluator found every required passage ID.
Correct reasoning:    facts were interpreted and combined correctly.
```

The first two are relevant to this experiment. It does not measure the third
by generating and grading final answers.

### 6.5 Attempts, repairs, and failure policy

An **attempt** is one model submission. A **repair attempt**, also called a
**retry**, is an additional attempt after a failure.

For repairable validation failures, the selector permits three attempts in
total:

```text
attempt 1: initial submission
attempt 2: first repair, if needed
attempt 3: second repair, if needed
```

This is not one initial attempt plus three retries. The source document's
“up to 3” should be read as three total attempts.

The validator sends an error description back to the model so it can submit a
corrected set. It does not silently replace duplicate indices or invent quotes.

Under the reported benchmark policy, a submission still invalid after those
attempts becomes an empty final context and is scored as failure. Its question
remains in the evaluated case count.

A **service error** is a failure to use the external service successfully. An
**authentication error** means access could not be verified. A **transport
error** means the request or response could not be communicated successfully.
The reported runner stops for these failures and allows the run to resume;
it does not treat them as ordinary missing-evidence outcomes.

Not every exception is repaired by the three-attempt loop. The loop specifically
repairs checked evidence-set validation errors. The measured policy and the
kind of error both matter when interpreting a failure count.

### 6.6 Why more candidates or more final slots can change selection

More candidates can make a missing support available, but also give the model
more distractors to read. More final slots can make a complete chain easier to
fit, but create more required quotations and structured entries to validate.

A **token** is a model's unit of text, which may be a whole word, part of a
word, or punctuation. **Input tokens** are those supplied to the model;
**output tokens** are those it produces. A **token budget** limits how many
such units can be processed or generated. Tokens do not correspond to a fixed
number of characters or passages.

The current selector rejects a constructed input prompt longer than 160,000
characters and requests at most 2,048 output tokens per call. These are
implementation limits, not evaluation scores.

Increasing a passage count alone does not guarantee that the resulting model
task remains within those limits or that the selected evidence improves.

## 7. How a dataset record becomes a gold passage set

### 7.1 Reading the data notation

**JSON**, short for **JavaScript Object Notation**, is a text format for storing
structured data. An **object** is a collection of named fields and their values.
An **array** is an ordered list of values. A **string** is a text value, written
between quotation marks in JSON.

In JSON, braces `{...}` enclose an object and brackets `[...]` enclose an
array. This is different from our mathematical use of `{G1, G2}` to denote a
set; the surrounding explanation will identify which notation is being used.

For example:

```json
{
  "question": "Who is the father of the director?",
  "supporting_facts": [["Film article", 0], ["Director article", 1]]
}
```

The field `question` has a string value. The field `supporting_facts` has an
array value containing two smaller arrays.

### 7.2 The original 2Wiki fields

**2Wiki**, here, is the question-answering dataset used for the reported
experiment. A **triple** is a group of three values. A **relationship triple**
gives a starting subject, a relation, and the subject or value reached by that
relation. For example, `["The Ride", "director", "Jan Svěrák"]` expresses
the film's director relationship.

Its relevant record fields are:

| Field | Definition | How it is used |
| --- | --- | --- |
| `_id` | A unique identifier for the question. | Associate results with the same case. |
| `type` | The question's reasoning category. | Group results by question type. |
| `question` | The question text. | Input to retrieval and selection. |
| `context` | Article titles paired with supplied sentence arrays. | Prepare the searchable passages, including distractors. |
| `supporting_facts` | Title and sentence-index pairs labeling reference evidence. | Identify the gold supporting passages during preparation. |
| `evidences` | Reference relationship triples. | Retain for evaluation or separately labeled oracle analysis. |
| `answer` | The expected answer text. | Retain as a reference label; not used to select normal evidence. |

The dataset's sentence indices start at zero:

```text
sentence index 0 = first sentence
sentence index 1 = second sentence
sentence index 2 = third sentence
```

The source document's shortened example includes these annotations:

```json
{
  "supporting_facts": [
    ["The Ride (1994 film)", 0],
    ["Jan Svěrák", 1]
  ]
}
```

The first pair identifies the first sentence of the supplied film context.
The second identifies the second sentence of the supplied biography context.
These sentence positions are unrelated to retrieval ranks and selector indices.

### 7.3 Preparation and stable IDs

**Preparation** is the conversion from source dataset records to the project's
searchable passages and evaluation records. A **stable ID** is an identifier
recomputed consistently from the same defining inputs.

The preparation code joins the supplied sentences for each 2Wiki context entry
into a passage. It assigns a passage ID using the dataset name, normalized
title, and passage text.

A **hash** is a computed fingerprint of input data. A **fingerprint** is a
compact value used to detect or identify differences in that data. The code
uses a shortened hash in passage IDs. If the defining passage text changes, its
ID can change; “stable” does not mean invariant under content changes.

The prepared evaluation record stores `supporting_paragraph_ids`: the passage
IDs identified from the supporting-title annotations.

The 2Wiki preparation sorts the unique support IDs. Their stored order is
therefore not a statement of the logical reasoning order. Gold passage
membership, graph traversal order, and the order of reasoning relationships
are separate information.

For the measured film case, those IDs are:

```text
paragraph:4adb89580828fc3d13e919f5
paragraph:ffdaeb31fc2265649a2d4869
```

The source document maps them to the *Jan Svěrák* and *The Ride (1994 film)*
passages. Their complicated spelling is not part of the reasoning; the
evaluator needs consistent identity, not a meaningful-looking number.

### 7.4 What is lost when scoring at passage level

**Passage-level scoring** counts matching passage IDs. **Sentence-level
scoring** would count which labeled sentences were recovered or selected.

If one gold passage contains three supporting sentences, returning that
passage counts as finding one gold passage. The present evaluator does not
give three separate points or check which of those sentences was quoted.

The supporting-fact annotations help identify the gold passages, but the
reported recall and precision are not sentence-level measurements.

Outside a labeled benchmark, an ordinary production question usually has no
provided gold-support set. This exact gold-ID recall calculation cannot be
performed unless someone supplies reference evidence or another evaluation
method is defined.

## 8. The arithmetic vocabulary for every metric

A **metric** is a precisely defined measurement. A **case-level metric** is
calculated for one question. An **aggregate metric** combines results across
several questions.

A **fraction** expresses one quantity divided by another. Its **numerator** is
the quantity above the division line, and its **denominator** is the quantity
below it. A **percentage** is a fraction multiplied by 100.

For example, `2/5` has numerator 2 and denominator 5, equals decimal `0.4`, and
corresponds to `40%`.

The **sum** of values is the result of adding them. The **mean**, or arithmetic
average, is that sum divided by the number of values. A **binary** measurement
has two possible values; here 1 means success and 0 means failure. Averaging
such values gives the fraction of successes. A **rate**, for these case
outcomes, is the proportion of cases with a specified outcome.

The arithmetic symbols `+`, `−`, `×`, and `/` mean add, subtract, multiply, and
divide. The comparison symbols `≤` and `≥` mean “less than or equal to” and
“greater than or equal to.” The meaning of set difference and set inclusion
is specified separately in the notation table below.

### 8.1 Symbols used below

| Symbol or expression | Definition |
| --- | --- |
| `G` | The set of unique gold passage IDs for one question. |
| `R_K` | The ordered list of returned items within the first K positions. |
| `U_K` | The set of unique, nonempty passage IDs in `R_K`. |
| `g` | The number of IDs in `G`: gold count. |
| `n` | The number of IDs in `U_K`: retrieved count. |
| `m` | The number of IDs that belong to both `G` and `U_K`: matched count. |
| `N` | The number of evaluated questions in the group being summarized. |
| `\|A\|` | The number of members in a set named `A`. |
| `G ∩ U_K` | The intersection: IDs belonging to both sets. |
| `G − U_K` | The set difference: gold IDs absent from the retrieved set. |
| `G ⊆ U_K` | The subset condition: every gold ID is retrieved. |
| `min(...)` | The smallest of the listed values. |
| `max(...)` | The largest of the listed values. |

Notice that lowercase `n` counts returned passages for one question, while
uppercase `N` counts evaluated questions.

### 8.2 Cut off first, then count unique IDs

The evaluator first takes the first K returned records. It then forms the set
of their unique, nonempty passage IDs.

For example, with `K=3` and an illustrative result list:

```text
Full result list: [G1, G1, D1, G2]
First 3 records:  [G1, G1, D1]
Unique IDs:       {G1, D1}
```

The retrieved count is 2, and G2 is still outside the cutoff. The evaluator
does not remove the duplicate first and then pull G2 forward to refill a slot.

Ordinary retrieval deduplicates candidates, and the selector requires distinct
indices. This edge case explains the exact evaluation rule when inspecting
unusual or malformed result data.

### 8.3 One example used for all the calculations

Assume the gold set is `{G1, G2}` and the evaluated list at K=5 is:

| Rank | Passage | Gold membership |
| ---: | --- | --- |
| 1 | D1 | Not in the gold set. |
| 2 | G1 | Gold. |
| 3 | D2 | Not in the gold set. |
| 4 | G2 | Gold. |
| 5 | D3 | Not in the gold set. |

The basic counts are:

```text
g = 2
n = 5
m = 2
missing support IDs = G − U_K = no missing IDs
```

All the following case-level calculations refer to this same result unless
another result is explicitly specified.

## 9. Recall, precision, and complete-chain success

### 9.1 Support recall gives partial credit

**Support recall** is the fraction of the question's gold passages that were
retrieved:

```text
support recall = m / g
```

For the worked list:

```text
support recall = 2 / 2 = 1.0 = 100%
```

If G2 were missing, it would become `1/2 = 50%`.

**Mean support recall** averages this fraction across questions, giving each
question equal weight:

```text
mean support recall
    = sum of the questions' support-recall values / N
```

Consider three illustrative questions with recalls 100%, 50%, and 0%:

```text
mean support recall = (1.0 + 0.5 + 0.0) / 3 = 0.5 = 50%
```

This does not say half the questions have complete chains. Only one of those
three does.

### 9.2 Averaging questions is different from pooling supports

A **macro average** gives each question equal weight. A **micro average**
combines the underlying counts before dividing. The reported mean support
recall is a macro average.

For two illustrative questions:

| Question | Gold passages found | Per-question recall |
| --- | ---: | ---: |
| A | 1 of 1 | 100% |
| B | 1 of 4 | 25% |

The two calculations differ:

```text
macro mean = (100% + 25%) / 2 = 62.5%
micro recall = (1 + 1) / (1 + 4) = 2 / 5 = 40%
```

Micro recall gives the four-support question more influence because it has
more supporting passages. Neither calculation should be substituted silently
for the other.

### 9.3 Complete-chain success gives no partial credit

**Complete-chain success**, also called **Chain Hit@K** for one question, is 1
if every gold passage is retrieved within K and 0 otherwise:

```text
complete-chain success = 1 if G is nonempty and G ⊆ U_K
                       = 0 otherwise
```

The implementation's case field is `all_support_recalled`.

For our worked list, both G1 and G2 appear, so the result is 1. With G2 missing,
the result is 0, even though support recall is still 50%.

**Complete-chain recall@K** is the mean of those binary successes:

```text
complete-chain recall@K
    = number of questions with every gold passage within K / N
```

The aggregate field `all_support_recall` and average `chain_hit_at_K` express
this same all-support success rate in the report.

The word “recall” is therefore used at two levels:

| Metric | Unit receiving credit |
| --- | --- |
| Support recall | Each recovered gold passage within a question. |
| Complete-chain recall | Each question whose entire gold set is recovered. |

### 9.4 Why four passages out of five can still be a failure

Suppose a question requires five gold passages and only four are retrieved:

```text
support recall = 4 / 5 = 80%
complete-chain success = 0
```

The last missing passage may contain the very fact needed to reach the answer.
Complete-chain recall deliberately treats the chain as unfinished.

For questions with nonempty gold sets, each complete-chain result is at most
its support recall. Their averages over the same questions must therefore satisfy:

```text
complete-chain recall ≤ mean support recall
```

A reported result violating that relationship would need its definitions or
implementation checked.

### 9.5 Support precision measures the concentration of labeled evidence

**Support precision** is the fraction of unique returned passages that belong
to the gold set:

```text
support precision = m / n
```

For our worked list:

```text
support precision = 2 / 5 = 40%
```

**Mean support precision** is the average of that fraction across questions.

Recall asks, “How much of the required evidence was found?” Precision asks,
“How much of the returned material is labeled required evidence?”

| Returned result | Recall | Precision | Complete chain |
| --- | ---: | ---: | ---: |
| Both gold passages and three distractors | 100% | 40% | 1 |
| One gold passage and four distractors | 50% | 20% | 0 |
| Both gold passages only | 100% | 100% | 1 |
| Five distractors | 0% | 0% | 0 |

The third row is mathematically useful, but does not satisfy the normal
exactly-five selector contract when five candidates are available.

### 9.6 Why perfect retrieval can have only 40% precision

When two gold passages exist and exactly five distinct passages must be
returned, at most two of those five can be gold:

```text
maximum possible precision = 2 / 5 = 40%
```

More generally, when exactly K distinct passages are returned:

```text
maximum possible precision = min(g, K) / K
```

For a two-gold question with 40 candidates, even a complete candidate pool has
precision only `2/40 = 5%`. The wide candidate stage is intentionally giving
selection many possibilities.

Thus 33.3% mean support precision at final K=5 is not an answer-accuracy score
and should be interpreted relative to gold counts and the fixed budget.

### 9.7 Empty outputs and empty gold sets

When no passages are returned, the evaluator assigns support precision 0
instead of dividing by zero. For a question with gold evidence, support recall
and complete-chain success are also 0.

When a case has an empty gold set, the code assigns recall 0 and complete-chain
success false. It does not award automatic success for having no requirements.

That is an explicit software convention. Normal benchmark questions are
expected to have meaningful gold evidence, so empty labels should be understood
as a data condition requiring attention rather than an ordinary easy success.

## 10. First-support ranking versus whole-chain ranking

### 10.1 First-support Hit@K

**First-support Hit@K** is 1 if at least one gold passage appears within the
cutoff, and 0 if none does. The report also calls this simply **Hit@K**.

Our worked list has G1 at rank 2, so Hit@5 is 1.

This result remains 1 if G2 disappears. Hit records the existence of an entry
point into the evidence, not the completeness of the evidence.

The aggregate Hit@K is the fraction of evaluated questions with at least one
such hit.

### 10.2 First relevant rank and reciprocal rank

A **relevant** passage for these metrics means a passage whose ID is in the
gold set. The **first relevant rank** is the smallest rank containing gold
evidence.

**Reciprocal rank** is one divided by that first relevant rank. If no gold
passage occurs within K, the score is 0.

For the worked list:

```text
first relevant rank = 2
reciprocal rank = 1 / 2 = 0.5
```

Earlier first evidence gets more credit:

| First gold rank | Reciprocal rank |
| ---: | ---: |
| 1 | 1.000 |
| 2 | 0.500 |
| 3 | 0.333… |
| 5 | 0.200 |
| No gold within K | 0.000 |

The trailing `…` indicates that the decimal continues.

### 10.3 Mean Reciprocal Rank

**Mean Reciprocal Rank**, abbreviated **MRR**, averages reciprocal rank across
questions. **MRR@K** applies the cutoff before calculating each question's
score. The report's **first-support MRR@K** is the same measurement.

For three illustrative questions with first relevant ranks 1, 2, and no hit:

```text
MRR = (1.0 + 0.5 + 0.0) / 3 = 0.5
```

This is neither the average rank nor the reciprocal of the average rank.
The reciprocal must be calculated for each question before averaging.

MRR of 0.5 does not mean half the answers were correct. It summarizes how early
the first reference passage appeared, with zero credit for questions with none.

### 10.4 Chain completion rank

The **chain completion rank** is the first position by which every gold
passage has appeared. It is the largest first-occurrence rank among the gold
passages, provided all are present.

Our list contains G1 first at rank 2 and G2 first at rank 4:

```text
chain completion rank = max(2, 4) = 4
```

If the list contains duplicates, only the first occurrence of each gold ID
matters. A repeated G1 does not postpone an already completed chain.

If any gold passage is absent, the implementation stores completion rank 0.
Here 0 is a **sentinel value**, meaning a special marker for “not completed,”
rather than a real list position.

### 10.5 Reciprocal chain rank and Chain MRR

**Reciprocal chain rank** is one divided by the chain completion rank, with
zero for an incomplete chain.

```text
reciprocal chain rank = 1 / 4 = 0.25
```

**Chain MRR@K** averages those reciprocal chain ranks across questions. It
rewards both completing chains and completing them early.

The original document sometimes says “Chain MRR” while describing one question.
More precisely, that one-question value is reciprocal chain rank; the mean is
the aggregate Chain MRR.

For three illustrative questions completing at ranks 2, 4, and never:

```text
Chain MRR = (1/2 + 1/4 + 0) / 3 = 0.25
```

For a question requiring two distinct passages, the earliest possible
completion rank is 2, so its maximum reciprocal chain rank is `1/2 = 0.5`.
For four distinct gold passages, the maximum is `1/4 = 0.25`.

Chain MRR therefore depends on how many supporting passages questions require.
A group of four-support questions should not be compared casually with a
group of two-support questions as if their possible best scores were identical.

### 10.6 The same list under every metric

Here is the complete arithmetic for `[D1, G1, D2, G2, D3]` at K=5:

| Measurement | Calculation | Value |
| --- | --- | ---: |
| Gold count | Two unique gold IDs. | 2 |
| Retrieved count | Five unique returned IDs. | 5 |
| Matched count | G1 and G2 both found. | 2 |
| Support recall | `2/2` | 1.0 |
| Support precision | `2/5` | 0.4 |
| First-support Hit@5 | At least one gold ID found. | 1 |
| First relevant rank | G1 first appears at rank 2. | 2 |
| Reciprocal rank | `1/2` | 0.5 |
| Chain Hit@5 | Every gold ID found. | 1 |
| Chain completion rank | G2 completes the set at rank 4. | 4 |
| Reciprocal chain rank | `1/4` | 0.25 |

Now replace G2 with D4:

```text
[D1, G1, D2, D4, D3]
```

Hit@5 remains 1, first relevant rank remains 2, and reciprocal rank remains
0.5. Support recall falls to 0.5, precision falls to 0.2, Chain Hit becomes 0,
and reciprocal chain rank becomes 0.

This is the precise reason a strong first-support result can hide a failed
multi-hop evidence set.

### 10.7 How changing order differs from changing membership

If `[D1, G1, D2, G2, D3]` is reordered to `[G1, G2, D1, D2, D3]` at the same
K=5:

```text
support recall:        unchanged at 100%
support precision:     unchanged at 40%
Chain Hit:             unchanged at 1
reciprocal rank:       improves from 1/2 to 1
reciprocal chain rank: improves from 1/4 to 1/2
```

The set of passages is identical. Only metrics that use rank respond to the
reordering at this cutoff.

### 10.8 Useful consistency relationships

For nonempty gold sets scored at the same cutoff, these relationships hold:

```text
Chain Hit ≤ support recall ≤ first-support Hit
reciprocal chain rank ≤ reciprocal rank ≤ first-support Hit
reciprocal chain rank ≤ Chain Hit
```

They also hold after averaging the corresponding quantities across the same
questions. All values lie between 0 and 1.

For one gold passage, first-support Hit and Chain Hit are identical, and
ordinary MRR and Chain MRR are identical. Their difference becomes useful when
a question requires multiple gold passages.

## 11. Following one measured question through both strategies

The report identifies the case as
`022a3dd40bdd11eba7f7acde48001122`. Its question asks for the father of the
director of *The Ride (1994 Film)*.

The gold passages are the film passage and the Jan Svěrák biography. The report
records this behavior:

| Stage | Flat | Graph |
| --- | --- | --- |
| Candidate evidence | Film passage at rank 1; biography absent from the first 40. | Film passage at rank 1; biography at rank 8. |
| Candidate support recall | `1/2 = 50%`. | `2/2 = 100%`. |
| Candidate Hit@40 | 1. | 1. |
| Candidate reciprocal rank | 1.0. | 1.0. |
| Candidate Chain Hit@40 | 0. | 1. |
| Candidate reciprocal chain rank | 0. | `1/8 = 0.125`. |
| Final evidence at K=5 | Retains only one gold passage. | Retains both gold passages in its first two positions. |
| Final support recall | 50%. | 100%. |
| Final Chain Hit@5 | 0. | 1. |

The reciprocal chain ranks above are calculations from the reported ranks.
The graph final result has reciprocal chain rank `1/2 = 0.5` because the
second selected passage completes its gold set.

The mechanism is visible in three steps:

1. Ordinary search finds a passage that names the director.
2. Traversal follows that named person to the biography passage.
3. Joint selection retains the film and biography passages together.

The Graph candidate result is not already the final answer. It is a collection
containing the required evidence. The model-based selector still has to retain
it, and a later answering system would still have to interpret it correctly.

## 12. Understanding the sample before reading its percentages

### 12.1 Sample, split, cohort, and controls

A **sample** is a selected portion of a larger dataset. A **split** is a
designated dataset partition. A **development split** is a partition commonly
used for development and evaluation decisions; the label `dev` identifies it
in the prepared records.

A **cohort** is a group of cases whose metrics are reported together. A
**subgroup** is a smaller group within a larger cohort.
**Primary** identifies the designated main comparison; a primary cohort is
the group of questions used for that main comparison.

A **control group** here is an additional question group used to help interpret
how the retrieval strategies behave under a different evidence pattern. It
does not mean a medical or randomized-treatment control.

The reported 2Wiki development split has 12,576 questions. The experiment
selected 120, with equal counts across four question types:

| Group | Cases | Role |
| --- | ---: | --- |
| Compositional | 30 | Part of the primary bridge cohort. |
| Inference | 30 | Part of the primary bridge cohort. |
| Comparison | 30 | A separately reported comparison control. |
| Bridge comparison | 30 | A separately reported four-support comparison control. |
| Primary bridge cohort | 60 | The compositional and inference rows combined. |

The final row is a subtotal, not 60 extra questions. The primary bridge cohort
is a reporting choice, not a fifth dataset question type.

### 12.2 Balanced, diagnostic, held-out, and frozen

**Balanced** means the sample deliberately gives the included categories equal
case counts. It does not establish that the full dataset has equal category
frequencies.

**Diagnostic** means designed to help explain system behavior or locate
weaknesses. A **diagnostic corpus** can be smaller and more deliberately
constructed than the source collection used by a production application.

**Tuning** means changing system choices to improve its measured behavior.
**MuSiQue** is another multi-hop question dataset used for comparison here.
**Held-out** describes a sample reserved from a particular development or
tuning process. The MuSiQue comparison is described as a retained held-out
sample; that term should not be used to imply that every design decision in
this project was made without knowledge of any related evaluation.

A **protocol** is the specified procedure and settings for an experiment.
**Frozen** means those choices are fixed for the comparison, so one method is
not given a different task after its results are seen.

A **random sampling seed** is an initial value used to make a random sampling
procedure repeatable. This experiment used `9112026`. It is unrelated to the
three seed passages that start graph traversal.

### 12.3 The recorded protocol in one place

| Choice | Recorded value | What that choice controls |
| --- | --- | --- |
| Dataset sample | 120 2Wiki questions, 30 per type. | Which cases are available for evaluation. |
| Main reported group | 60 compositional and inference questions. | The primary result's denominator. |
| Search corpus | 936 deduplicated passages. | What all sampled questions can search. |
| Search scope | Pooled. | Passages from other sampled cases can compete. |
| Candidate budget | 40. | How much evidence reaches selection. |
| Graph seeds | 3. | How many initial passages start expansion. |
| Graph depth | 3 paragraph-level hops. | How far traversal can expand. |
| Graph construction | `content-v2`, content-derived edges. | Which connections exist without gold relations. |
| Embedding model | `granite-embedding:278m` through Ollama. | The numerical text representation used for vector search. |
| Selector model | `eu.anthropic.claude-sonnet-4-6`. | The model used to choose final evidence. |
| Final passage budgets | K=4 and K=5. | How much selected evidence is delivered. |
| Primary final cutoff | K=5. | The designated main final comparison. |
| Validation failures | Empty context after exhausted allowed attempts. | Failed selections remain in the denominator. |

A **namespace** is a name used to isolate one collection of graph records from
another. The experiment used the isolated index and graph namespace
`2wiki-content-v2-20260911`, avoiding accidental mixing of different prepared
experiments.

### 12.4 What “primary” and “secondary” mean

A **primary result** is the designated main measurement used to assess the
comparison. A **secondary result** is an additional measurement that provides
context or explores another condition.

K=5 on the combined bridge cohort is primary. K=4 is secondary and is retained
in part to relate the setup to the older MuSiQue final budget.

Choosing which result to emphasize only after inspecting many results can
overstate the evidence. The designated primary comparison tells the reader
which result was intended to carry the main conclusion.

## 13. Reading the reported complete-chain results

### 13.1 Counts first, percentages second

| Stage, primary bridge cohort | Flat | Graph |
| --- | ---: | ---: |
| Candidate K=40 | 41/60 = 68.3% | 60/60 = 100.0% |
| Final K=4 | 35/60 = 58.3% | 50/60 = 83.3% |
| Final K=5, primary | 38/60 = 63.3% | 50/60 = 83.3% |

Each numerator counts questions with complete gold-passage sets. Each
denominator counts evaluated questions.

`41/60` does not mean 41 passages were retrieved from a 60-passage corpus. It
means 41 questions had every required passage somewhere in their own
40-candidate lists.

Similarly, `50/60` at final K=5 means the final selected list contained all gold
passages for 50 questions. The remaining 10 questions did not receive complete
gold support.

### 13.2 Percentage points versus relative improvement

A **percentage point** is the unit obtained by subtracting two percentage
rates directly. A **relative improvement** divides the change by the starting
value.

At final K=5:

```text
Graph complete-chain recall = 50 / 60 = 83.333…%
Flat complete-chain recall  = 38 / 60 = 63.333…%
absolute difference         = 12 / 60 = 20.0 percentage points
relative improvement        = 12 / 38 = 31.58…%
```

The 20-point gain and roughly 31.6% relative gain describe the same count
difference with different denominators.

The direct count interpretation is: Graph delivered complete evidence for 12
more questions overall in this 60-case group.

### 13.3 Mean support recall and precision add another view

The primary final K=5 results also report:

| Metric | Flat | Graph |
| --- | ---: | ---: |
| Complete-chain recall | 63.3% | 83.3% |
| Mean support recall | 77.5% | 83.3% |
| Mean support precision | 31.0% | 33.3% |
| First-support Hit@5 | 91.7% | 83.3% |
| First-support MRR@5 | 0.9167 | 0.8333 |
| Chain MRR@5 | 0.3117 | 0.4083 |

The source sometimes displays MRR on a percentage-like scale, such as 91.7%.
Writing 0.9167 makes it clearer that MRR is an average reciprocal-rank score,
not a proportion of correct answers.

### 13.4 Reconstructing why Flat has 77.5% mean support recall

All primary cases have two gold passages. From the reported outcomes, Flat
has 38 complete cases and 55 cases with at least one gold passage. Therefore:

```text
38 cases found both gold passages.
17 cases found exactly one: 55 − 38 = 17.
 5 cases found none:        60 − 55 = 5.
```

This is a reconstruction from the reported two-support case counts, not a new
measurement.

The mean support recall is:

```text
(38 × 1.0 + 17 × 0.5 + 5 × 0.0) / 60
    = 46.5 / 60
    = 77.5%
```

The 17 partial results raise support recall without raising complete-chain
recall.

With five returned passages on valid selections and zero precision for the
empty failures, the mean precision is:

```text
(38 × 0.4 + 17 × 0.2 + 5 × 0.0) / 60 = 31.0%
```

### 13.5 Why Graph has equal complete-chain and mean support recall

In the primary Graph K=5 run:

```text
50 cases returned valid selections containing both gold passages.
10 cases failed validation and were scored as empty contexts.
```

There are no partially supported valid Graph results in this particular
primary run. Consequently:

```text
mean support recall = (50 × 1.0 + 10 × 0.0) / 60 = 83.3%
complete-chain recall = 50 / 60 = 83.3%
mean support precision = (50 × 0.4 + 10 × 0.0) / 60 = 33.3%
```

Equal recall values are a consequence of these particular outcomes. Graph
retrieval does not generally force support recall to equal complete-chain
recall.

### 13.6 Why Flat can win the first-support metric and lose the chain metric

Flat finds at least one gold passage for 55 questions. Graph does so for 50,
because its other 10 final contexts are empty.

But only 38 of Flat's 55 hit cases have both gold passages. All 50 of Graph's
hit cases have both.

```text
First-support question: Which system more often returns anything gold?
Answer for this run:    Flat, 55 versus 50.

Complete-chain question: Which more often returns every required passage?
Answer for this run:     Graph, 50 versus 38.
```

These are compatible findings because the metrics ask different questions.

### 13.7 Separate K=4 and K=5 runs are not nested lists

**Nested lists** here would mean that the first four passages of the K=5 list
are exactly the K=4 list. The experiment does not assume this.

The K=4 and K=5 selectors are separate model tasks with different budgets.
Their choices and validation outcomes can differ. Graph succeeds on 50 cases
in each run, but that does not prove the same 50 cases succeeded.

For a fixed ranked list, increasing the evaluation cutoff cannot lose a gold
passage already inside it. Re-running retrieval or selection with a different
budget changes the list itself, so the same guarantee does not apply.

### 13.8 Subgroups show where the result comes from

| Question type, final K=5 | Flat complete chains | Graph complete chains | Flat mean support recall | Graph mean support recall |
| --- | ---: | ---: | ---: | ---: |
| Compositional, 30 cases | 17/30 | 27/30 | 73.3% | 90.0% |
| Inference, 30 cases | 21/30 | 23/30 | 81.7% | 76.7% |
| Comparison, 30 cases | 28/30 | 28/30 | 93.3% | 93.3% |
| Bridge comparison, 30 cases | 0/30 | 29/30 | 60.8% | 99.2% |

The primary combined gain is strongest in compositional cases. Inference has
more complete Graph chains but lower Graph mean partial-support recall. The
comparison controls tie on the count of complete chains at K=5.

Bridge comparison is a demanding four-support task in this sample. Flat's
0/30 complete chains and 60.8% mean support recall mean it frequently finds
part of the evidence while missing at least one required passage every time.

These rows do not establish an advantage for every question type, and the
60-case primary subtotal should not be added again to the 120-case total.

## 14. Paired comparisons: wins, losses, ties, and rescued supports

### 14.1 Why comparing the same questions matters

A **paired comparison** evaluates two methods on the same individual cases
and keeps each case's two results together. The **baseline** is the reference
method against which the other method is compared. Here Flat is the baseline
and Graph is the alternative strategy.

In a complete-chain comparison:

| Outcome | Definition |
| --- | --- |
| Graph win | Graph's chain is complete and Flat's is incomplete. |
| Graph loss | Flat's chain is complete and Graph's is incomplete. |
| Successful tie | Both return complete chains. |
| Failed tie | Neither returns a complete chain. |

The primary K=5 pairs are:

| Flat | Graph | Cases |
| --- | --- | ---: |
| Complete | Complete | 34 |
| Incomplete | Complete | 16 |
| Complete | Incomplete | 4 |
| Incomplete | Incomplete | 6 |

Check the totals:

```text
all cases = 34 + 16 + 4 + 6 = 60
Flat successes = 34 + 4 = 38
Graph successes = 34 + 16 = 50
ties = 34 + 6 = 40
net Graph advantage = 16 − 4 = 12
```

The 12-question net advantage does not mean Graph wins exactly 12 cases.
It wins 16 and loses 4.

### 14.2 Partial-support wins are a different comparison

The implementation also compares per-question support recall. A Graph
support-recall win can occur when Graph finds three of four gold passages
and Flat finds two of four.

That case is still a complete-chain tie because both chains are incomplete.
Always check which metric a wins/losses table is comparing.

### 14.3 Rescued supports

A **rescued support** is a gold passage Graph retrieves that Flat does not
retrieve for the same question at the compared stage and cutoff.

Define `U_Graph` as Graph's unique returned IDs and `U_Flat` as Flat's. Then:

```text
rescued gold IDs = (G ∩ U_Graph) − U_Flat
```

A **rescue case** is a question with at least one rescued gold ID. A **rate**
is a proportion of cases with a specified outcome.

| Rescue field | Definition |
| --- | --- |
| `cases` | Number of questions with at least one rescued gold passage. |
| `rate` | Rescue cases divided by the number of paired questions. |
| `supports` | Total rescued gold passage IDs, counted within their questions. |
| `by_hop` | Rescue results grouped by the cases' recorded reasoning-hop count. |

`by_hop` groups questions; it does not necessarily identify which numbered
step in a particular chain was rescued.
Inside each hop-count group, `cases` counts all paired questions in that group
and `rescued` counts those with at least one rescued support. Those nested
counts differ from the outer rescue object's `cases`, which counts rescue
cases only.

Rescue is **directional**, meaning it looks at what Graph adds over Flat.
It does not subtract passages Graph loses.

For an illustrative four-gold question:

```text
Gold:  {G1, G2, G3, G4}
Flat:  {G1, G2, G3}
Graph: {G1, G2, G4}
```

Graph rescues G4 but loses G3. Both methods have 75% recall and incomplete
chains. A rescue count alone would hide the loss.

### 14.4 What candidate_minus_baseline means

The comparison report uses `candidate_minus_baseline` for the alternative
strategy's mean metric minus the baseline's mean metric.

In that field name, **candidate** means “the alternative method being
compared.” It does not necessarily mean “candidate-pool stage.” A final K=5
comparison can also have a `candidate_minus_baseline` field.

For recall metrics, a positive Graph-minus-Flat value favors Graph; a negative
value favors Flat. For elapsed time or cost, lower is generally preferable, so the
sign must be interpreted according to the measurement.

## 15. Locating where a chain was lost

**Stage attribution** means assigning an incomplete outcome to the stage where
complete gold coverage was first unavailable or lost.

For each case, compare the candidate-pool result with the final result:

| Bucket | Definition |
| --- | --- |
| `complete` | Final evidence contains every gold passage. |
| `lost_in_selection` | The candidate pool contained every gold passage, but final evidence does not. |
| `never_pooled` | The candidate pool itself lacked at least one gold passage and the final chain is incomplete. |

These are **mutually exclusive** buckets: each case belongs to exactly one.
They cover the paired stage results, so their counts add up to the evaluated
case count.

### 15.1 The primary final K=5 breakdown

| Bucket | Flat | Graph |
| --- | ---: | ---: |
| `never_pooled` | 19 | 0 |
| `lost_in_selection` | 3 | 10 |
| `complete` | 38 | 50 |
| Total | 60 | 60 |

For Flat, 19 cases already lack complete candidate evidence. Of its 41 complete
candidate pools, three do not lead to complete final evidence.

For Graph, all 60 candidate pools are complete. Ten do not produce a complete
final context.

This suggests different immediate areas to investigate: candidate retrieval
for Flat's 19 pool misses, and final selection for Graph's 10 losses.

### 15.2 Validation failures overlap these buckets

A **selector validation failure count** measures cases whose submissions
exhausted the allowed validation attempts. It describes an error type, while
the three buckets describe evidence availability across stages.

For the primary K=5 cohort:

```text
Flat selector validation failures:  5
Graph selector validation failures: 10
```

Do not add those counts to the three bucket counts. A Flat case can have an
incomplete candidate pool and also fail selector validation. It belongs in
`never_pooled` and is also counted as a selector validation failure.

All 10 Graph K=5 failures in the primary cohort were validation failures:
eight involved non-exact quotes and two involved index structure. All 50 valid
Graph selections preserved complete gold support.

The source report distinguishes this from Graph K=4, where nine primary cases
failed validation and one returned a valid but incomplete selection. The same
number of final failures can have different causes.

### 15.3 Reliability and conditional success

**Operational reliability** is how consistently a component completes its
task successfully under its required interface rules.

The **evidence-selector applied rate** is the fraction of cases with a
nonempty result marked as having a valid selector output. For the primary
Graph K=5 run, this is `50/60 = 83.3%`.

A **conditional success rate** measures success only within a specified
subset. Among Graph's 50 valid primary selections, all 50 chains were complete:

```text
complete chains among valid selections = 50 / 50 = 100%
complete chains among all cases        = 50 / 60 = 83.3%
```

The first calculation helps diagnose the selector's successful behavior. The
second is the delivered-evidence result. Replacing the second with the first
would hide the failed cases.

Fixing validation may improve the observed final result, but these counts do
not prove that all future repaired selections would retain every gold passage.
That outcome requires a new measured run.

## 16. Understanding the uncertainty calculations

### 16.1 Why a sample's percentage is not a universal constant

**Sampling uncertainty** is the variation that can arise because a measurement
uses a limited set of cases instead of every case of interest. An **estimate**
is a value calculated from the available data to describe a quantity of
interest.

**Probability** expresses how likely an event is under a specified model, on a
scale from 0 for impossible to 1 for certain. A **probability model** specifies
the possible outcomes and how likelihood is assigned to them. **Statistical**
methods use observed data and such assumptions to quantify patterns or
uncertainty.
A **distribution** describes how values or probabilities are spread across
possible outcomes.

In 60 cases, changing one binary outcome changes the rate by:

```text
1 / 60 × 100 = 1.666… percentage points
```

In a 30-case subgroup, one outcome changes it by 3.333… points. A visible
percentage difference can therefore represent only one or two questions.

The reported +20.0-point advantage is an observed effect on the evaluated
cases. An **effect size** quantifies the magnitude of a difference; here the
effect size is the difference in complete-chain success rates.

### 16.2 Paired bootstrap resampling

**Resampling** means making new samples from observed data. **Sampling with
replacement** means a selected item remains available to be selected again.
One resample can contain the same case several times and omit other cases.

A **bootstrap** repeatedly resamples the observed cases and recalculates a
measurement. A **paired bootstrap** keeps each question's Flat and Graph
results together during that process.

An **interval** gives a lower and an upper value. A **percentile** is a position
in an ordered collection of measurements: the 2.5th percentile marks a value
near the bottom 2.5%, and the 97.5th near the top 2.5%.

The reported procedure uses 20,000 paired resamples:

1. Start with 60 pairs, one Flat/Graph pair per question.
2. Draw 60 pairs with replacement to form a resample.
3. Calculate Graph complete-chain recall minus Flat complete-chain recall in
   that resample.
4. Repeat until 20,000 differences have been calculated.
5. Use the central 95% of those differences to obtain the reported interval.

Keeping pairs together preserves which method did better on the same
question. Separately resampling Flat cases and Graph cases would discard that
relationship and answer a different statistical question.

### 16.3 The reported 95% interval

A **confidence interval** is an interval produced by a statistical procedure
intended to express uncertainty about an estimated quantity. Its confidence
level describes the procedure's coverage under its assumptions, rather than
assigning a probability to a fixed already-computed effect.

For primary complete-chain recall, the report gives:

```text
observed Graph gain: +20.0 percentage points
paired bootstrap 95% interval: +6.7 to +33.3 percentage points
```

The resampling variation remains on the positive side of zero for this
interval. That supports a positive complete-chain advantage under the sampled
case mix and frozen protocol.

It does not mean:

- 95% of questions improved;
- every future run has a 95% chance of falling inside the interval;
- the same interval applies to the full 2Wiki corpus or a different application;
- changing the model, graph policy, or candidate budget preserves the gain.

The bootstrap here resamples the recorded case outcomes. It does not rerun the
selector 20,000 times and does not directly measure variation across repeated
model calls or changes to the corpus.

For mean support recall, the report gives a smaller +5.8-point difference and
an interval of −5.0 to +16.7 points. That interval includes zero and negative
values, so the sample does not establish a positive mean partial-support gain
with the same strength as the complete-chain result.

### 16.4 McNemar's test starts with the disagreements

A **statistical test** compares observed data with what would be expected under
a specified assumption. A **null hypothesis** is the assumption being tested.
Here the relevant null is that the paired binary outcomes have no systematic
directional advantage between methods.

**McNemar's test** is a test for comparing two paired binary outcomes. It uses
the pairs in which the methods disagree.

For primary K=5:

```text
Graph-only complete chains = 16
Flat-only complete chains  =  4
disagreements              = 20
ties                       = 40
```

Conditional on a disagreement under the null, either method is equally likely
to be the successful one. The test asks whether 16 disagreements favoring one
method and 4 favoring the other is unusually imbalanced under that assumption.

### 16.5 Exact, two-sided, and p-value

An **exact test** calculates the relevant probabilities directly from a
specified discrete probability model instead of relying on a large-sample
approximation. **Discrete** means the possible counts are separate whole-number
values, such as 0 through 20.

A **two-sided test** treats an equally extreme advantage in either direction
as evidence against the null. It would also consider 4 Graph wins and 16 Flat
wins an extreme imbalance of the same size.

A **p-value** is the probability, under the null model, of observing a result
at least as extreme as the one being tested.

For this exact two-sided McNemar calculation, the p-value is approximately
`0.0118`, or `1.18%`.

This does not mean there is a 1.18% probability that Graph has no advantage.
It is a probability of data under the null, not a probability assigned to the
null itself.

### 16.6 Working through the exact calculation

A **trial** is one occurrence with a recorded outcome. **Independent trials**
have outcomes that do not change one another's probabilities under the model.
A **binomial model** describes the number of successes in a fixed number of
independent trials with the same success probability. Here, conditional on 20
disagreements under the null, “favors Graph” has probability one half.

Define the notation first:

| Notation | Definition |
| --- | --- |
| `X` | The number of the 20 disagreements favoring Graph under the null model. |
| `P(event)` | The probability of the named event under that model. |
| `C(20, j)` | The number of ways to choose j positions from 20 positions. |
| `2^20` | Two multiplied by itself 20 times: the number of equally likely binary sequences. |

By symmetry, at least 16 Graph wins is as probable as at most 4. The exact
two-sided calculation is:

```text
p = P(X ≤ 4) + P(X ≥ 16)
  = 2 × P(X ≤ 4)
  = 2 × [C(20,0) + C(20,1) + C(20,2) + C(20,3) + C(20,4)] / 2^20
  = 2 × [1 + 20 + 190 + 1140 + 4845] / 1048576
  = 12392 / 1048576
  ≈ 0.0118179
```

The **tails** are the low and high extremes of a distribution of possible
outcomes. Here “at least as extreme” includes both tails: 0–4 Graph wins and
16–20 Graph wins.

The p-value describes the strength of the observed disagreement imbalance
under the null model. The +20.0-point effect size describes how much the
observed success rates differ. These are different quantities.

### 16.7 Why secondary tests need context

**Multiple comparisons** means examining several statistical comparisons, such
as different cutoffs and several question types. The more comparisons examined,
the more opportunities there are to observe an unusual result somewhere.

An **adjustment for multiple comparisons** is a statistical procedure that
accounts for that collection of tests. The report says its secondary and
per-type p-values are descriptive and unadjusted.

The secondary K=4 comparison has 19 Graph wins and 4 losses, giving
`p≈0.00260`. The smaller p-value reflects that particular imbalance. It does
not replace K=5 as the designated primary result or prove K=4 is the better
product setting.

The uncertainty calculations also do not correct for every possible
dependence among cases, for example shared passages or related subjects. They
describe the stated paired case-resampling and test procedures. Generalization
beyond this sample remains a separate empirical question.

## 17. Timing, model usage, and diagnostic fields

### 17.1 Latency, median, p50, and p95

**Latency** is elapsed time to complete an operation. A **millisecond**,
abbreviated **ms**, is one thousandth of a second. A **second** is abbreviated
**s** in the report's timing table.

The **median**, also called **p50**, is a central ordered observation: about
half the measurements are at or below it. **p95** is the 95th percentile:
about 95% of measured operations finish at or below that value.

For a finite sample, percentile implementations select or interpolate between
observations in slightly different ways. The current evaluator sorts latencies
and selects the observation at a rounded fractional index. The report's p50
and p95 describe that convention, not a guaranteed deadline for future calls.

| Primary cohort stage | Flat p50 | Graph p50 | Flat p95 | Graph p95 |
| --- | ---: | ---: | ---: | ---: |
| Candidate K=40 | 0.60 s | 0.94 s | 0.81 s | 1.24 s |
| Retrieval plus selector, K=4 | 9.98 s | 11.41 s | 15.61 s | 15.23 s |
| Retrieval plus selector, K=5 | 11.18 s | 12.37 s | 16.64 s | 17.40 s |

The selector stage accounts for much more elapsed time than candidate
retrieval in these observations. The difference between two medians, however,
is not automatically the median of a separately timed internal component.

### 17.2 What the execution setup permits us to conclude

**Concurrent jobs** are jobs running during overlapping time periods.
**Sequential cases** means each job processes one case after another.
The experiment launched two strategy jobs concurrently, with sequential cases
inside each job.

A **load test** repeatedly measures a service under specified request volumes.
A **service-level objective** is a target for behavior such as latency or
availability in operation. These one-pass benchmark timings do not establish
such an objective.

The final K=5 run resumed after access expired, so its timings span more than
one session. They are recorded observations under that execution setup, not
an isolated repeated experiment proving a universal latency difference.

### 17.3 Characters versus tokens

**Input tokens** are model text units supplied to a call. **Output tokens** are
units the model produces. A **usage record** reports quantities consumed during
a call, such as those token counts.

The evaluator's `context_characters` counts characters in the returned passage
texts at the evaluated cutoff. `mean_context_characters` averages that count
across questions.

At final K=5, this character count measures selected text. It is not the amount
of candidate text read by the selector, which can include all 40 passages plus
the question, instructions, and repair messages.

Therefore:

```text
five final passages
does not imply
the selector was charged only for reading five passages.
```

Character counts and token counts are related to text size but are not
interchangeable through one exact constant.

### 17.4 Why successful-selection token totals are incomplete for cost

The implementation attaches usage records to successful selected passages.
The evaluator retains the shared usage once per case, rather than counting the
same model call once for every selected passage.

Those records can include repairs that eventually led to a successful
selection. Usage from selections that ultimately failed is not fully retained
in the reported output.

**Cost accounting** means attributing consumed resources to monetary charges.
Complete per-question cost accounting would need the failed-call usage as well
as successful-call usage and the relevant infrastructure costs.

The available totals are therefore insufficient for an exact dollar-cost
comparison. This is a limitation of the recorded measurements, not evidence
that failed calls were free.

### 17.5 Reranker and provenance diagnostics

A **diagnostic field** records behavior that helps explain outcomes but is not
itself a gold-evidence quality measurement.

| Field or reported quantity | Definition |
| --- | --- |
| `reranker_applied_rate` | Fraction of cases whose returned results are marked with reranker scores. |
| `reranker_fallback_rate` | Fraction of cases marked as falling back after reranking did not supply its intended result. |
| `evidence_selector_applied_rate` | Fraction with nonempty returned results marked as selected by the evidence selector. |
| `source_counts` | Counts of returned items by origin label such as `seed`, `graph`, `fallback`, and `both`. |
| `evidence_selector_usage` | Recorded model usage associated with a selected result. |
| `selected_evidence` | Selected IDs together with their supplied quotes and roles. |

The `fallback` origin label and `reranker_fallback` error behavior use the same
word for different concepts. An ordinary search fallback passage does not
indicate that a reranker failed.

For the reported selector runs, a low or zero reranker applied rate is not
evidence of a broken selector: the runs use joint selection rather than the
optional reranking path.

## 18. Graph connectivity and the comparison with MuSiQue

### 18.1 Connected components

Two nodes are **connected** if an allowed path exists between them. A
**connected component** is a group in which each node can reach every other
node under the chosen traversal interpretation, with no larger such group
containing it.

An **offline diagnostic** is an analysis performed separately from the live
question-retrieval path. Here it inspects the constructed graph after the gold
labels are used to identify which passages to examine.

The connectivity check does not ask whether the retriever actually found a
path within its time, depth, seed, or candidate limits. It asks whether a path
exists in the inspected graph structure.

### 18.2 Three increasingly restrictive connectivity checks

| Check | Definition | What it tells us |
| --- | --- | --- |
| Same pooled component | All gold passages belong to one connected region of the whole content graph. | There is some route among them, possibly through distractor passages. |
| Gold passages and their content entities only | Connectivity remains when the inspection keeps gold passages and their content-entity connections without using other passages as bridges. | The gold evidence itself has enough extracted connections. |
| Explicit support-title or alias mentions | Connectivity is supported by the title/alias matching rule alone. | The chain is visible to a particularly direct content-matching rule. |

The third check includes the builder's permitted aliases. It is not an
exact-title-only match rate.

Gold annotations are used to choose the nodes inspected after construction.
They are not used to add edges to the normal content graph.

### 18.3 The reported connectivity differences

**MuSiQue** is the other multi-hop question dataset retained for comparison in
this project. Its historical held-out sample has 60 questions: 20 with two
recorded hops, 20 with three, and 20 with four. Its pooled corpus has 1,057
passages.

| Connectivity check | 2Wiki primary bridge, 60 cases | MuSiQue held-out, 60 cases |
| --- | ---: | ---: |
| Same pooled component | 59/60 = 98.3% | 45/60 = 75.0% |
| Gold passages and content entities only | 59/60 = 98.3% | 28/60 = 46.7% |
| Explicit support-title/alias mentions | 54/60 = 90.0% | 11/60 = 18.3% |

The graph construction rules have far more visible routes between the sampled
2Wiki bridge supports. That gives graph expansion more opportunities to find
missing evidence in that sample.

### 18.4 Why longer chains and implicit links matter

A two-support question needs both required passages. A four-support question
needs all four, creating more opportunities for one missing passage to make
the whole chain incomplete.

As an **illustrative probability model**, suppose each required passage is
recovered independently with probability 0.9. **Independently** means one
passage's success does not change another's probability in that simplified
model. Then:

```text
two-support complete probability  = 0.9 × 0.9 = 0.81
four-support complete probability = 0.9 × 0.9 × 0.9 × 0.9 = 0.6561
```

Actual retrieval events are not necessarily independent, and these are not
benchmark estimates. The example explains why requiring every link can make
complete-chain success harder as the number of required supports increases.

The final budget also matters. Four gold passages at final K=4 leave no slot
for a distractor. Two gold passages at final K=5 leave three spare slots.

Entity extraction creates another difference. An explicit mention of the
next passage's title fits this graph policy well. A bridge conveyed through a
pronoun, paraphrase, or unrecognized alias may have no corresponding edge.

Increasing traversal depth cannot create an edge that was never built.

### 18.5 Connectivity is neither a guarantee nor a strict hybrid-recall ceiling

A connected gold set can still fail because:

- the selected seeds start in a different region;
- the required path exceeds `max_hops`;
- the graph's result limits exclude a needed passage;
- interleaving and the 40-passage cutoff omit a needed candidate;
- the selector drops a support or fails validation.

Conversely, hybrid search can retrieve passages from different components.
Multiple seeds can also start in different regions.

The report contains one disconnected primary bridge case in which both gold
supports are nevertheless retrieved. Its ID is
`198dc91e0bdd11eba7f7acde48001122`; Flat places its supports at ranks 1 and 12,
and Graph's mixed pool places them at ranks 1 and 21.

That explains how graph connectivity is 59/60 while Graph candidate complete
recall is 60/60. A graph-augmented retriever can combine independent search
results without requiring one graph path through the entire gold set.

Bridge-comparison questions provide another reason: two separate evidence
branches can be recovered independently even if all four gold passages do not
belong to a single component.

### 18.6 Comparing the observed retrieval results carefully

| Recorded complete-chain result | Flat | Graph | Graph minus Flat |
| --- | ---: | ---: | ---: |
| 2Wiki primary candidate K=40 | 68.3% | 100.0% | +31.7 points |
| 2Wiki primary final K=5 | 63.3% | 83.3% | +20.0 points |
| Historical MuSiQue candidate K=40 | 55.0% | 58.3% | +3.3 points |
| Historical MuSiQue final K=4 | 41.7% | 46.7% | +5.0 points |

The historical MuSiQue mean support recall was 78.8% versus 81.7% at K=40 and
66.4% versus 73.3% at final K=4. Its final selector validation failure counts
were 7 for Flat and 4 for Graph out of 60.

The historical artifacts were retained, not rerun with the 2Wiki experiment.
The sample, support-count mixture, corpus size, final cutoff, and potentially
code details differ. These are observed runs, not a controlled experiment in
which only the dataset changed.

The supported interpretation is that this content-derived graph policy fits
the sampled 2Wiki bridge evidence patterns better. It does not establish that
MuSiQue is a bad dataset or that Graph universally has a 20-point advantage.

## 19. Which settings can change the result, and why

A **parameter** is a configurable value controlling an operation. **Tuning**
means adjusting such values to improve a desired outcome. A **tradeoff** occurs
when a choice helps one property while making another worse.

### 19.1 Candidate and final budgets

| Setting | Meaning | Possible benefit of increasing it | Possible cost or failure mode |
| --- | --- | --- | --- |
| `candidate_limit` | Number of candidates retained for later selection. | Include gold passages that previously fell outside the pool. | More text, distractors, selector work, and potentially different rankings or mixing. |
| `top_k` | Number of final passages requested. | Make room for more required supports. | More selected text and validation entries; lower precision when extra slots are nongold. |

If the gold set contains more than K distinct passages, complete-chain success
within K is impossible under this passage-ID metric. Even perfect selection
cannot fit five distinct required passages into four positions.

If `top_k` exceeds `candidate_limit`, the retriever's current code uses their
maximum for the effective retrieval budget. Thus `candidate_limit` is the
effective pool size in the reported 40-versus-4/5 settings, but should not be
treated as an independent hard cap for every possible configuration.

### 19.2 Seeds and traversal depth

| Setting | Meaning | Possible benefit | Possible cost or failure mode |
| --- | --- | --- | --- |
| `seed_limit` | Number of starting passages from ordinary search. | Reach evidence from more starting regions. | Expand from distractors and reserve more candidate slots for seeds. |
| `max_hops` | Maximum paragraph-level expansion depth. | Reach evidence farther from the seeds. | More path exploration and more unrelated reachable content. |

**Topic drift** means retrieval gradually moves away from the question's
needed subjects. Deep expansion through broad entities can increase it.

Neither setting changes the question's gold set. More seeds cannot guarantee
a missing relation was extracted, and more depth cannot cross disconnected
components from a single seed.

### 19.3 Graph construction and candidate mixing

| Choice | Definition | Why it affects the result |
| --- | --- | --- |
| `graph_source` | Which relationship source traversal is allowed to use. | Gold-evidence oracle relations reveal privileged information and change the task. |
| Construction policy | The rules used to recognize entities and create links. | Determines which passages can be reached and how many noisy connections exist. |
| Namespace | The isolated collection of graph records. | Mixing old and new graph constructions can invalidate a comparison. |
| Mixing policy | How seeds, graph results, and ordinary search share the candidate budget. | More room for one source can exclude useful passages from the other. |
| Path-hint policy | Which graph connections are shown to the selector. | Changes the information available for choosing evidence, even with the same passage pool. |

An **ablation** is an experiment that removes or changes one component to
measure its contribution. For example, comparing the same graph candidate
pools with and without navigation hints would help distinguish passage
availability from hint-assisted selection.

The reported Flat/Graph comparison includes the implemented graph retrieval
and eligible navigation hints. It does not isolate graph traversal as the
only possible contributor to every final-stage difference.

### 19.4 Models, prompts, and validation rules

Changing the embedding model can change ordinary search rankings and graph
seeds. Changing the selector model or its prompt can change which candidate
combinations are chosen. Changing the contract or failure policy can change
how many selections count as valid delivered contexts.

Increasing permitted repair attempts can recover some invalid submissions,
but adds model calls and latency. Relaxing exact-quote checks can increase the
applied rate while allowing unsupported quotations. Those are different system
behaviors and need separately labeled measurements.

There is no mathematical guarantee that a higher applied rate alone increases
complete-chain recall; newly valid results could still omit gold passages.

### 19.5 Corpus, labels, and sample choices

**Distractor density** describes how much competing nongold material is
present relative to the required evidence. Adding more competing passages can
change rankings even when the gold passages themselves are unchanged.

Changing the following can affect every reported rate:

- the sampled questions and their type proportions;
- the total searchable corpus and included distractors;
- passage boundaries and stable IDs;
- gold-support labels and their completeness;
- pooled versus case-controlled search;
- which failed cases remain in the denominator.

A fair comparison states these choices and keeps them fixed across the
strategies being compared. Changing several at once makes it hard to attribute
the result to a specific cause.

## 20. A small program that demonstrates the mechanism

This example is a miniature, **deterministic** program: the same input produces
the same output. It uses made-up passage records to demonstrate graph expansion,
candidate cutoffs, and all the main case-level metrics. It does not contact
OpenSearch, Neptune, or a model service.

### 20.1 Python vocabulary used in the example

**Python** is the programming language used here. Its **standard library** is
the collection of modules supplied with a normal Python installation. A
**module** is a collection of reusable code. This example uses only that
standard library.

A **queue** holds items waiting to be processed. A **breadth-first search**
visits nearer nodes before farther ones by processing a queue in arrival order.
The standard-library `deque` supplies that queue. A `defaultdict(set)` creates
an empty neighbor set when a node is first encountered.

| Code concept | Definition |
| --- | --- |
| Variable | A name referring to a value. |
| Dictionary | A collection mapping keys to values, such as passage IDs to passage records. |
| Function | A named block of code that accepts inputs and returns a result. |
| Argument | An input supplied when calling a function. |
| Loop | Code that repeats an operation over items. |
| Condition | A check that controls whether an operation is performed. |
| Boolean | A true/false value, written `True` or `False` in Python. |
| `len(value)` | Count the elements in a collection. |
| `set(values)` | Create a collection of unique values. |
| `values[:k]` | Take the first k items of a list. |
| `enumerate(values, start=1)` | Visit items together with ranks starting at 1. |
| `&` between sets | Find the intersection of the sets. |
| `<=` between sets | Check whether the left set is a subset of the right. |
| `return` | Send a function's result back to its caller. |
| `print(...)` | Display output. |
| `#` | Start a comment explaining code. |

Some additional Python syntax makes the example compact:

| Syntax or operation | Definition |
| --- | --- |
| `import` and `from ... import ...` | Make named code from another module available. |
| `def` | Begin a function definition; the indented lines form its body. |
| `=` | Assign a value to a variable. |
| `==` | Compare two values for equality. |
| `if`, `else` | Choose which code to run according to a condition. |
| `for`, `while` | Repeat over items, or repeat while a condition is true. |
| `continue` | Skip the rest of the current loop iteration and move to the next. |
| `in`, `not in` | Test whether a collection contains an item or does not contain it. |
| `and`, `not` | Combine true/false conditions, or reverse one. |
| `bool(value)`, `int(value)` | Convert a value to true/false, or to a whole number. |
| `a if condition else b` | Use value `a` when the condition is true and `b` otherwise. |
| `sorted(values)` | Return the values in a consistent sorted order. |
| `dictionary.items()` | Visit the dictionary's key/value pairs. |
| `dictionary.values()` | Obtain the dictionary's values. |
| `collection.add(item)` | Add an item to a set. |
| `collection.append(item)` | Add an item at the end of a list or queue. |
| `queue.popleft()` | Remove and return the oldest item at the front of the queue. |
| `text.startswith(prefix)` | Check whether text begins with the specified characters. |
| `text.split(":", 1)` | Split text at its first colon into at most two parts. |
| `[1]` after a list | Select its second item, since Python positions start at 0. |
| `+` between strings or lists | Join the two values in order. |

A **tuple** is an ordered group of values, written here in parentheses, such
as `(node, distance)`. **Unpacking** assigns those values to separate variables,
as in `node, distance = queue.popleft()`.

A **comprehension** builds a collection by applying a short loop and optional
condition inside its brackets or braces. For example,
`[pid for pid in search_ranking if pid not in reached]` builds a list of IDs
that search found but graph expansion has not already included.

An **f-string** is a Python string prefixed by `f` that inserts values where
braces appear. The formatting suffix `:.2f` displays a number with two digits
after the decimal point. In the result dictionary, `rr` means reciprocal rank
and `chain_rr` means reciprocal chain rank. The short name `pid` means passage
ID. The `ids` variable holds a list of IDs.

The graph is represented with an **adjacency mapping**: each node maps to the
set of neighboring nodes it can reach directly.

The program's node names start with `passage:` or `entity:` to distinguish
passage nodes from entity nodes. The numbered passage IDs `P1` and `P2` are
ordinary passage names; they become gold only in the separate scoring call.

### 20.2 The runnable program

Save the following as `graph_recall_walkthrough.py` and run it with
`python3 graph_recall_walkthrough.py`:

```python
from collections import defaultdict, deque


# Entity names are supplied as if extracted from content, without gold labels.
PASSAGES = {
    "P1": {"entities": ["Film A", "Director B"]},
    "P2": {"entities": ["Director B", "Parent C"]},
    "D1": {"entities": ["Unrelated film"]},
    "D2": {"entities": ["Unrelated director"]},
    "D3": {"entities": ["Unrelated family"]},
}


def build_graph(passages):
    graph = defaultdict(set)
    for passage_id, record in passages.items():
        passage_node = "passage:" + passage_id
        for name in record["entities"]:
            entity_node = "entity:" + name
            graph[passage_node].add(entity_node)
            graph[entity_node].add(passage_node)
    return graph


def expand(graph, seed_id, paragraph_hops):
    start = "passage:" + seed_id
    queue = deque([(start, 0)])
    seen = {start}
    found = [seed_id]
    edge_limit = 2 * paragraph_hops
    while queue:
        node, distance = queue.popleft()
        if distance >= edge_limit:
            continue
        for neighbor in sorted(graph[node]):
            if neighbor in seen:
                continue
            seen.add(neighbor)
            queue.append((neighbor, distance + 1))
            if neighbor.startswith("passage:"):
                found.append(neighbor.split(":", 1)[1])
    return found


def graph_candidates(graph, search_ranking, limit):
    reached = expand(graph, search_ranking[0], paragraph_hops=1)
    remaining = [pid for pid in search_ranking if pid not in reached]
    return (reached + remaining)[:limit]


def score(gold_ids, result_ids, k):
    gold = set(gold_ids)
    ranked = result_ids[:k]
    retrieved = {pid for pid in ranked if pid}
    matched = gold & retrieved
    first_ranks = {}
    for rank, pid in enumerate(ranked, start=1):
        if pid in gold and pid not in first_ranks:
            first_ranks[pid] = rank
    first = min(first_ranks.values()) if first_ranks else 0
    complete = bool(gold) and gold <= retrieved
    completion = max(first_ranks.values()) if complete else 0
    return {
        "recall": len(matched) / len(gold) if gold else 0.0,
        "precision": len(matched) / len(retrieved) if retrieved else 0.0,
        "hit": int(bool(first)),
        "rr": 1 / first if first else 0.0,
        "chain": int(complete),
        "chain_rr": 1 / completion if completion else 0.0,
    }


graph = build_graph(PASSAGES)
# The second support is outside this teaching example's four-item cutoff.
search_ranking = ["P1", "D1", "D2", "D3", "P2"]
flat_pool = search_ranking[:4]
graph_pool = graph_candidates(graph, search_ranking, limit=4)

# These are scripted choices, not a simulated language model.
flat_final = flat_pool[:2]
graph_final = graph_pool[:2]
graph_discards_support = [graph_pool[0], graph_pool[2]]

# The answer key enters only now, for evaluation of already-created results.
gold_ids = {"P1", "P2"}
examples = [
    ("Flat pool", flat_pool, 4),
    ("Graph pool", graph_pool, 4),
    ("Flat final", flat_final, 2),
    ("Graph final", graph_final, 2),
    ("Dropped support", graph_discards_support, 2),
    ("Invalid selection", [], 2),
]
for label, ids, cutoff in examples:
    result = score(gold_ids, ids, cutoff)
    print(
        f"{label}: {ids}; "
        f"recall={result['recall']:.2f}, precision={result['precision']:.2f}, "
        f"hit={result['hit']}, rr={result['rr']:.2f}, "
        f"chain={result['chain']}, chain_rr={result['chain_rr']:.2f}"
    )
```

### 20.3 Expected output

```text
Flat pool: ['P1', 'D1', 'D2', 'D3']; recall=0.50, precision=0.25, hit=1, rr=1.00, chain=0, chain_rr=0.00
Graph pool: ['P1', 'P2', 'D1', 'D2']; recall=1.00, precision=0.50, hit=1, rr=1.00, chain=1, chain_rr=0.50
Flat final: ['P1', 'D1']; recall=0.50, precision=0.50, hit=1, rr=1.00, chain=0, chain_rr=0.00
Graph final: ['P1', 'P2']; recall=1.00, precision=1.00, hit=1, rr=1.00, chain=1, chain_rr=0.50
Dropped support: ['P1', 'D1']; recall=0.50, precision=0.50, hit=1, rr=1.00, chain=0, chain_rr=0.00
Invalid selection: []; recall=0.00, precision=0.00, hit=0, rr=0.00, chain=0, chain_rr=0.00
```

### 20.4 Reading each line of the result

The Flat pool finds P1 but excludes P2 at its four-item boundary. The graph
walk reaches P2 through the shared `Director B` entity and includes it.

The successful graph final list keeps both supports. The scripted
`Dropped support` selection demonstrates a different possible outcome from
that same complete graph pool: a selector can discard an available support.
The `Invalid selection` row shows the effect of scoring an empty context.

Every nonempty row has first-support Hit 1 and reciprocal rank 1. Those
measurements cannot distinguish the complete rows from the partial rows.

The graph builder and candidate function never receive `gold_ids`. Altering
the answer key can change the scores but cannot change those already-built
candidate lists.

### 20.5 What the miniature deliberately simplifies

The program uses one seed, one paragraph-level hop, a four-candidate pool, and
two final slots. The real measured settings are three seeds, three hops,
40 candidates, and four or five final slots.

It supplies entity names directly, rather than extracting them from prose.
It appends graph-reached passages before ordinary search remainders, rather
than reproducing the implementation's full interleaving policy. Its final
choices are scripted and do not simulate model understanding or validation.

Its metric function follows the main evaluator's passage-ID and rank rules.
The example teaches why expansion can make evidence available and why final
selection can still lose it. It is not a performance reproduction of the cloud
benchmark.

## 21. Questions to ask when interpreting a result

### 21.1 “Graph got 100%. Is the task solved?”

Identify the metric and stage. Candidate complete-chain recall of 100% means
every evaluated question had its gold passages inside the candidate pool. It
does not establish successful final selection or correct generated answers.

In the reported primary cohort, candidate Graph recall is 100%, while final
K=5 complete-chain recall is 83.3% because 10 selections fail validation.

### 21.2 “Can a selector find evidence missing from the pool?”

This selector cannot. It only returns indices into the supplied candidate
list. A different system could perform another retrieval call, but that would
add an operation beyond this fixed-pool selection stage.

### 21.3 “Does Graph need a direct edge to the final answer?”

No. The graph can locate a passage whose text contains the needed fact. The
normal content graph links passage subjects and mentions; it need not store a
trusted answer relation such as `father`.

### 21.4 “If all gold passages connect, why can Graph still miss them?”

Connectivity only establishes that some route exists in the inspected graph.
The actual retriever has particular seeds, a depth limit, graph-result limits,
a candidate budget, and a final selector. Any of those can prevent complete
delivery.

### 21.5 “If the evidence chain is complete, must the answer be correct?”

No. A model may overlook a fact, misread a pronoun, compare values incorrectly,
or answer an unintended question even when all reference passages are present.

Conversely, a model could produce a correct answer from memorized knowledge
or alternative evidence when one labeled passage is missing. That still fails
this passage-ID completeness metric.

### 21.6 “What answer metrics would be needed?”

**Answer Exact Match**, often abbreviated **EM**, checks whether a generated
answer matches the reference under a specified text-normalization rule.
**Tokenization** is the procedure for splitting text into the units counted.
An **answer token** is one of those evaluation units, often a word; it need
not be the same unit as a language model's input or output token.

**Answer-token precision** measures the fraction of generated answer tokens
matched by reference answer tokens. **Answer-token recall** measures the
fraction of reference answer tokens matched by the generated answer.
**Answer F1** is a balanced combination of those two values. Calling answer
precision `p` and answer recall `r`, its formula is `2 × p × r / (p + r)`,
with zero when both are zero. The exact normalization, treatment of repeated
tokens, and matching rules must be specified by the evaluation.

**Answer accuracy** is the fraction of generated answers judged correct under
the chosen correctness rule.

**Factuality** asks whether claims are factually supported or correct.
**Citation correctness** asks whether cited sources support the claims they
are attached to. **User satisfaction** asks whether the response meets the
user's needs under a specified feedback or evaluation method.

Those are separate evaluations. The reported benchmark does not provide their
results. Its 83.3% complete-chain recall cannot be relabeled as answer accuracy,
answer F1, or citation correctness.

### 21.7 “Why not compare precision alone?”

A system returning one gold passage and nothing else could have 100% precision
while missing most of the required chain. A fixed five-passage result containing
both required passages has only 40% precision for a two-gold question.

Precision needs recall, gold count, and passage budget beside it.

### 21.8 “Why not compare complete-chain recall alone?”

Complete-chain recall is the primary evidence-coverage outcome, but it gives no
partial credit and does not explain where failures occur. Support recall,
rank metrics, stage attribution, validation failures, and latency provide
different information needed to diagnose the result.

### 21.9 “How would I investigate one failed question?”

For a labeled benchmark case, the useful inspection order is:

1. Identify the unique gold passage IDs and the facts they are meant to support.
2. Inspect the candidate list and record which gold IDs are missing.
3. If candidates are missing, inspect ordinary search ranks, seeds, graph
   reachability, path depth, and the final candidate cutoff.
4. If the candidate pool is complete, inspect the final selected IDs and any
   selector validation error.
5. Distinguish a valid partial selection from an empty failed submission.
6. Recalculate the case metrics from the returned IDs.
7. Compare the same case with the other retrieval strategy at the same stage
   and cutoff.

That order separates missing search evidence, budget effects, and selection
failures before proposing a change.

## 22. Small exercises with answers

These are illustrative checks of the definitions, not benchmark cases.

### Exercise A: complete evidence with late first support

```text
Gold = {G1, G2, G3}
Result at K=5 = [D1, G1, G2, D2, G3]
```

The gold count is 3, retrieved count 5, and matched count 3. Recall is 100%,
precision is 60%, Hit@5 is 1, and reciprocal rank is `1/2 = 0.5`.
The chain completes at rank 5, so Chain Hit is 1 and reciprocal chain rank is
`1/5 = 0.2`.

At K=4 on the same list, G3 is excluded. Recall becomes `2/3 = 66.7%`,
precision becomes `2/4 = 50%`, and the chain measurements become zero. Hit and
reciprocal rank remain unchanged.

### Exercise B: a complete pool followed by an incomplete final list

```text
Gold = {G1, G2}
Candidate pool = [G1, D1, D2, G2]
Final list = [G1, D1]
```

Candidate Chain Hit@4 is 1. Final Chain Hit@2 is 0. The stage bucket is
`lost_in_selection` because G2 was available but did not survive selection.

### Exercise C: compare two methods case by case

```text
Case A: Flat complete,   Graph complete
Case B: Flat incomplete, Graph complete
Case C: Flat complete,   Graph incomplete
Case D: Flat incomplete, Graph complete
```

Graph has two wins, one loss, and one tie. Flat completes `2/4 = 50%` and
Graph completes `3/4 = 75%`. The net difference is one question, or
25 percentage points. The relative gain over Flat is `1/2 = 50%`.

### Exercise D: a larger K does not change the gold count

A question requires two gold passages. Its 40-candidate pool contains both,
and its five-passage final context also contains both.

Both stages have 100% support recall and Chain Hit 1. Assuming full unique
lists, candidate precision is `2/40 = 5%` and final precision is `2/5 = 40%`.
The gold count is 2 at both stages.

### Exercise E: a successful-looking first-support result

```text
Gold = {G1, G2, G3, G4}
Result at K=4 = [G1, D1, D2, D3]
```

Hit@4 is 1 and reciprocal rank is 1.0. Nevertheless, support recall is only
25%, support precision is 25%, and Chain Hit and reciprocal chain rank are
both zero.

## 23. Finding these concepts in the project

### 23.1 Source documents

An **artifact** is a saved output of a process, such as a prepared data file,
result report, log, or source snapshot. A **snapshot** is a saved copy of
something at a particular time.

| Document | What it provides |
| --- | --- |
| [Original candidate-pool document](candidate_pool_and_complete_chain_recall.local.md) | The source explanation expanded by this companion. |
| [2Wiki benchmark report](2wiki_multihop_benchmark.md) | Recorded settings, measured results, uncertainty, failure analysis, and artifact locations. |
| [Multi-hop retrieval metrics example](multihop_retrieval_metrics_example.md) | A separate three-hop teaching example; its older measured configuration should not be mixed with the retained results above. |

The benchmark report links its saved candidate, final, protocol, and audit
artifacts under `var/multihop/2wiki-20260911/`. An **audit** is an independent
check that recorded data and calculations agree with the specified rules.
Those artifacts are local and may not exist in every checkout of the project.

### 23.2 Implementation map

| File | Responsibility |
| --- | --- |
| [datasets.py](../../rag/multihop/datasets.py) | Convert source records into prepared cases, passages, and evaluation labels. |
| [schema.py](../../rag/multihop/schema.py) | Create stable IDs and identify content entities and title mentions. |
| [index.py](../../rag/multihop/index.py) | Put prepared passage and graph records into their stores. |
| [opensearch.py](../../rag/multihop/opensearch.py) | Run lexical/vector search, combine ranks, and fetch passage documents. |
| [neptune.py](../../rag/multihop/neptune.py) | Store graph relationships, expand paths, and inspect graph structure. |
| [retrieve.py](../../rag/multihop/retrieve.py) | Implement retrieval strategies and combine candidates before finalization. |
| [selection.py](../../rag/multihop/selection.py) | Construct the selector prompt, request evidence, and validate indices, quotes, and roles. |
| [evaluate.py](../../rag/multihop/evaluate.py) | Score passage IDs and ranks, aggregate cases, compare strategies, and attribute stage failures. |
| [provenance.py](../../rag/multihop/provenance.py) | Record fingerprints and information identifying the code and graph used. |
| [multihop_graph_rag.py](../../scripts/multihop_graph_rag.py) | Expose preparation, indexing, retrieval, and evaluation operations as commands. |

### 23.3 Field-name reference

The letter `K` below stands for the numeric cutoff in a generated field name;
for example, `hit_at_K` becomes `hit_at_5` when K=5.

| Field | Where to read its meaning in this guide |
| --- | --- |
| `supporting_paragraph_ids` | Gold passage IDs; sections 1 and 7. |
| `gold_count`, `retrieved_count`, `matched_count` | Basic unique-ID counts; section 8. |
| `missing_support_ids` | Gold IDs absent from the evaluated result; section 8. |
| `ranking_cutoff` | The evaluated K boundary; sections 3 and 8. |
| `support_recall`, `mean_support_recall` | Partial support coverage per case and averaged; section 9. |
| `support_precision`, `mean_support_precision` | Concentration of gold evidence in returned passages; section 9. |
| `all_support_recalled` | Binary complete-chain result for a case; section 9. |
| `all_support_recall` | Fraction of cases with complete chains; section 9. |
| `hit_at_K`, `first_support_hit_at_K` | Finding at least one gold passage; section 10. |
| `first_relevant_rank` | First position containing gold evidence; section 10. |
| `reciprocal_rank_at_K`, `first_support_reciprocal_rank_at_K` | Case-level first-support reciprocal rank; section 10. |
| `mrr_at_K`, `first_support_mrr_at_K` | Mean first-support reciprocal rank; section 10. |
| `chain_hit_at_K` | Complete-chain result, or its mean in an aggregate; sections 9–10. |
| `chain_completion_rank` | Rank where all gold IDs have appeared; section 10. |
| `reciprocal_chain_rank_at_K`, `chain_mrr_at_K` | Case-level and mean reciprocal chain rank; section 10. |
| `case_count`, `by_question_type`, `by_hop` | Group size and subgroup summaries; section 12. |
| `paired_all_support`, `paired_support_recall` | Paired wins, ties, and losses for the named metric; section 14. |
| `candidate_minus_baseline` | Alternative method minus baseline, not necessarily candidate-stage results; section 14. |
| `complete`, `lost_in_selection`, `never_pooled` | Mutually exclusive stage-attribution buckets; section 15. |
| `selector_failure_count`, `evidence_selector_applied_rate` | Validation failures and valid applied selections; sections 15 and 17. |
| `latency_ms_p50`, `latency_ms_p95` | Ordered latency percentiles in milliseconds; section 17. |
| `context_characters`, `mean_context_characters` | Returned text size per case and averaged; section 17. |
| `source_counts`, `retrieval_source` | How returned passages were found; sections 5 and 17. |

### 23.4 Reading a new result table in a reliable order

First identify the dataset, searchable corpus, and evaluated case group. Then
identify the stage and K, the gold passage definition, and the failure policy.
Read complete-chain counts together with mean support recall, first-support
metrics, and the paired outcomes. Finally, use stage attribution, timing, and
uncertainty to understand the result's cause and practical limits.

For the main recorded result, the precise claim is: **on the frozen 60-case
2Wiki bridge cohort, Graph delivered every labeled supporting passage at final
K=5 for 50 questions, compared with 38 for Flat.** The additional metrics
explain what that gain means, where incomplete cases occurred, and which
conclusions require further evaluation.
