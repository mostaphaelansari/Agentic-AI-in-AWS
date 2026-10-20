# Production LLM Inference Parallelism: A Practical Guide

Production inference involves several independent placement decisions: where
model weights live, which workers cooperate on one request, where the KV cache
lives, and how incoming requests are distributed. Tensor, pipeline, data,
context, decode-context, and expert parallelism answer different parts of that
problem. They can be combined.

This guide develops a practical mental model for those strategies and shows
the layouts commonly used in production. For a deeper treatment of KV
placement and DCP, see
[Production vLLM Strategies: Model, KV Cache, and Requests](./Production%20vLLM%20Strategies%20-%20Model%20KV%20Cache%20and%20Requests.md).

> **Version note:** parallel-group constraints, supported model and attention
> backends, and command-line flags change across vLLM releases. Verify a
> deployment against the documentation for the installed vLLM version.

---

## 1. GPU, Node, and Model Replica

A **GPU** is one accelerator with its own compute cores and memory. A **node**
is one server containing CPUs, system RAM, network interfaces, and one or more
GPUs:

```text
Node
├── CPU and system RAM
├── GPU 0
├── GPU 1
├── GPU 2
└── GPU 3
```

A **model replica** is one complete, usable instance of the model. A replica
can occupy one GPU, several GPUs in one node, or several GPUs across multiple
nodes.

A model may fail to fit on one GPU because GPU memory must hold more than its
weights:

- model weights;
- KV cache;
- activations;
- communication buffers;
- CUDA graphs;
- kernel workspaces.

Two GPUs that each hold part of a tensor-parallel model do not form two
replicas. Together they form one usable replica.

---

## 2. Tensor Parallelism: TP

Tensor parallelism divides calculations and weights **inside each transformer
layer** across GPUs:

```text
One transformer layer
        │
   ┌────┴────┐
   ▼         ▼
 GPU 0     GPU 1
 part A     part B
   └────┬────┘
        ▼
 combine results
```

The GPUs process the **same request concurrently** and synchronize within
most layers.

### Main purpose

- Make one model replica fit across several GPUs.
- Add compute to each layer.

### Main cost

- Frequent GPU communication.
- Synchronization at layer operations.
- Sensitivity to the bandwidth and latency of the GPU interconnect.

Fast NVLink or NVSwitch is especially valuable for TP.

### When should TP be used?

The primary reason to use TP is that one GPU cannot provide enough model
memory or compute for one replica. The number of KV heads does not determine
whether TP is needed; it determines how efficiently attention KV can be
distributed after a TP size is selected.

Let:

```text
Hkv = number of KV heads
TP  = number of tensor-parallel ranks
```

A simple, efficient KV layout is possible when `Hkv` is at least as large as
`TP` and divides evenly by it:

```text
Hkv >= TP
Hkv % TP = 0
```

For example, with eight KV heads and four TP GPUs:

```text
Hkv=8
TP=4

GPU 0: KV heads 0, 1
GPU 1: KV heads 2, 3
GPU 2: KV heads 4, 5
GPU 3: KV heads 6, 7
```

Every GPU stores two distinct KV heads, so the KV-head dimension is sharded
without duplication.

Now consider two KV heads and four TP GPUs:

```text
Hkv=2
TP=4

GPU 0: KV head 0
GPU 1: KV head 1
GPU 2: duplicate of KV head 0
GPU 3: duplicate of KV head 1
```

The KV cache is duplicated two times. GPUs 2 and 3 are still useful: they own
different model-weight shards and calculate different query heads, matrix
projections, and MLP work. Only their duplicated KV storage fails to add new
cache capacity.

This does not mean `TP=4` is wrong. If four GPUs are required to hold or run
the model, TP is still necessary. DCP can then divide cached token positions
among those ranks to reduce the KV duplication.

### Practical TP selection order

1. Find the smallest TP size that fits the model weights, target KV capacity,
   activations, CUDA graphs, and workspaces.
2. Benchmark whether that size satisfies latency and throughput objectives.
3. Keep the TP group within the fastest practical GPU interconnect domain.
4. Check the model's MHA, GQA, MQA, or MLA layout and how its KV heads divide
   across the chosen TP size.
5. If TP exceeds the independently shardable KV state, measure the duplication
   and consider DCP.
6. Use remaining GPUs for additional replicas when traffic throughput matters
   more than increasing TP further.

For example, if a model fits and performs well with `TP=2` on an eight-GPU
node, four `TP=2` replicas may serve more independent traffic than one `TP=8`
replica.

> Choose the smallest TP size that makes the model fit and meets performance
> requirements. Then evaluate how efficiently its KV heads divide across that
> TP group.

> **TP means several GPUs cooperate on the same layer and request.**

---

## 3. Data Parallelism: DP

Data parallelism creates multiple model replicas that process different
requests:

```text
Request A → Replica 0
Request B → Replica 1
```

Each replica normally has its own scheduler, active request batch, KV cache,
and local prefix cache.

### Main purpose

- Serve more concurrent users.
- Increase aggregate throughput.
- Isolate queues and failures between replicas.

### Main cost

- Model weights are repeated for every replica.
- Prefix caches are normally local to their replica.

For dense models, production DP is often implemented as independent vLLM
instances behind a load balancer. vLLM also provides built-in DP deployment
options. See the
[vLLM data-parallel deployment guide](https://docs.vllm.ai/en/latest/serving/data_parallel_deployment/).

> **DP means different replicas handle different requests.**

---

## 4. TP Versus DP

| Question | TP | DP |
|---|---|---|
| What is divided? | Work inside the model | Incoming requests |
| Same request on several GPUs? | Yes | Usually no |
| What happens to model weights? | Sharded inside a replica | Repeated across replicas |
| Main reason | Fit or accelerate one replica | Serve more traffic |
| Communication | Frequent | Usually much less between replicas |

For four GPUs with `TP=2, DP=2`:

```text
Request A → Replica 0 → GPU 0 + GPU 1
Request B → Replica 1 → GPU 2 + GPU 3
```

Each pair of TP GPUs forms one usable replica. DP distributes requests between
the two pairs.

---

## 5. Pipeline Parallelism: PP

Pipeline parallelism divides the model by transformer layers:

```text
GPU 0: layers 0–19
GPU 1: layers 20–39
```

For one request:

```text
GPU 0 executes early layers
          │
          │ hidden states
          ▼
GPU 1 executes later layers
```

PP is sequential across stages for one unit of work. Multiple requests or
microbatches can overlap:

```text
Time 1: Stage 0 runs A
Time 2: Stage 0 runs B | Stage 1 runs A
Time 3: Stage 0 runs C | Stage 1 runs B
```

### Main purpose

- Fit one replica across more GPUs or nodes.
- Divide model weights by layer.

### Main cost

- Pipeline bubbles.
- Stage imbalance.
- Activation transfers between stages.

### KV placement

Each pipeline stage stores KV state only for the layers it owns:

```text
Stage 0: early-layer weights and early-layer KV
Stage 1: later-layer weights and later-layer KV
```

TP and PP divide a replica differently:

```text
TP: several GPUs calculate one layer together.

PP: one GPU group completes early layers,
    then passes hidden states to the next group.
```

---

## 6. Combining TP and PP

For `TP=4, PP=2`:

```text
Pipeline stage 0: four TP GPUs for early layers
Pipeline stage 1: four TP GPUs for later layers
```

The GPU count for one replica is:

```text
TP × PP = 4 × 2 = 8 GPUs
```

This layout is common when a model must span multiple nodes:

```text
Node 0: TP=8 for early layers
Node 1: TP=8 for later layers
PP=2 across nodes
```

TP remains inside each node's fast GPU fabric, while PP transfers hidden
states between the nodes. The
[vLLM parallelism and scaling guide](https://docs.vllm.ai/en/latest/serving/parallelism_scaling/)
recommends this general arrangement when a model exceeds one node.

---

## 7. Context Parallelism: CP

Context parallelism is the general idea of dividing one request along its
**token-position dimension**:

```text
One long context
      │
      ├── GPU 0: some positions
      ├── GPU 1: some positions
      ├── GPU 2: some positions
      └── GPU 3: some positions
```

CP has two phase-specific forms:

```text
Context Parallelism
├── PCP: Prefill Context Parallelism
└── DCP: Decode Context Parallelism
```

---

## 8. Prefill Context Parallelism: PCP

PCP divides long-prompt computation across GPUs. For a 16,000-token prompt:

```text
GPU 0: prompt positions     0–3,999
GPU 1: prompt positions 4,000–7,999
GPU 2: prompt positions 8,000–11,999
GPU 3: prompt positions 12,000–15,999
```

### Main purpose

- Reduce TTFT for extremely long prompts.
- Divide prefill computation and temporary memory.

### Main cost

- Distributed causal attention requires communication.
- Backend and model support is more specialized than TP or DP.

### Why prefill must exchange or gather K/V

Every prompt position produces a query, key, and value at each attention
layer. Causal attention allows position `i` to read positions `0` through `i`:

```text
position 0 reads: 0
position 1 reads: 0, 1
position 2 reads: 0, 1, 2
...
position 7 reads: 0, 1, 2, 3, 4, 5, 6, 7
```

Suppose PCP divides an eight-token prompt across two GPUs:

```text
GPU 0 owns positions 0, 1, 2, 3
GPU 1 owns positions 4, 5, 6, 7
```

Each GPU can compute Q, K, and V for its own positions:

```text
GPU 0:
Q0 Q1 Q2 Q3
K0 K1 K2 K3
V0 V1 V2 V3

GPU 1:
Q4 Q5 Q6 Q7
K4 K5 K6 K7
V4 V5 V6 V7
```

GPU 0 has everything needed for its local causal attention. For example, `Q3`
attends only to `K0` through `K3`.

GPU 1 does not initially have everything its queries require:

```text
Q4 attends to K0 K1 K2 K3 K4
Q5 attends to K0 K1 K2 K3 K4 K5
Q6 attends to K0 K1 K2 K3 K4 K5 K6
Q7 attends to K0 K1 K2 K3 K4 K5 K6 K7
```

The missing `K0–K3` and `V0–V3` reside on GPU 0. A distributed prefill
method must make that remote information available to GPU 1. Two broad
approaches are possible.

#### Method 1: partial Q, full K/V

Each GPU remains responsible for the attention outputs of its local query
positions, but the GPUs gather the K/V shards so every GPU can access the
complete sequence:

```text
Before gathering:

GPU 0: K/V 0–3
GPU 1: K/V 4–7


After gathering:

GPU 0: K/V 0–7
GPU 1: K/V 0–7
```

This is conceptually an all-gather:

```text
GPU 0 ── K/V 0–3 ──┐
                    ├── complete K/V on each GPU
GPU 1 ── K/V 4–7 ──┘
```

Each GPU then calculates only its assigned output rows:

```text
GPU 0: Q0–Q3 × complete K/V → outputs 0–3
GPU 1: Q4–Q7 × complete K/V → outputs 4–7
```

The causal mask prevents a query from reading future positions.

This approach is relatively simple and can work well for moderately long
prompts. Its limitation is that every participating GPU temporarily holds the
complete K/V tensors. Query work is divided, but K/V memory is replicated.

#### Method 2: partial Q, partial K/V

For a context too large to replicate, each GPU retains only its local Q, K,
and V chunk. The GPUs exchange K/V chunks or combine partial attention
results without constructing a complete K/V copy on every rank.

Ring attention is one possible pattern. With four GPUs:

```text
GPU 0 owns K/V chunk 0
GPU 1 owns K/V chunk 1
GPU 2 owns K/V chunk 2
GPU 3 owns K/V chunk 3
```

Each GPU first computes against its local chunk. The chunks then circulate:

```text
KV0: GPU 0 → GPU 1
KV1: GPU 1 → GPU 2
KV2: GPU 2 → GPU 3
KV3: GPU 3 → GPU 0
```

The GPUs process each received chunk and continue until every query shard has
seen all causally valid K/V shards it needs:

```text
              K/V chunks move around the ring

        ┌────────────────────────────┐
        ▼                            │
      GPU 0 → GPU 1 → GPU 2 → GPU 3
        │                            ▲
        └────────────────────────────┘
```

Causal structure can avoid useless work. Queries in the first sequence chunk
do not need K/V chunks containing only future positions.

#### Combining partial attention correctly

Attention includes a softmax over all valid keys. Partial attention outputs
from separate K/V chunks therefore cannot simply be added. A distributed
algorithm combines the information needed for one global normalization:

```text
partial maximum scores
partial exponential sums
partial weighted-value sums
              │
              ▼
combine global softmax statistics
              │
              ▼
correct complete attention output
```

This is related to the online-softmax technique used by memory-efficient
attention kernels, extended across devices.

#### Comparing the approaches

| Question | Partial Q, full K/V | Partial Q, partial K/V |
|---|---|---|
| Query positions | Divided across GPUs | Divided across GPUs |
| K/V available per GPU | Complete K/V | A local or currently received shard |
| Main communication | Gather K/V | Exchange K/V chunks or partial results |
| Temporary K/V memory per GPU | Higher | Lower |
| Implementation | Simpler | More complicated |
| Typical fit | Moderately long prompts | Extremely long prompts |
| Main risk | K/V replication | Communication overhead |

Here, **gather** means collecting all distributed pieces on each participating
GPU:

```text
Before: GPU 0=A, GPU 1=B, GPU 2=C, GPU 3=D
After:  every GPU has A+B+C+D
```

**Exchange** means sending smaller pieces between GPUs as computation
progresses:

```text
GPU 0 sends A to GPU 1
GPU 1 sends B to GPU 2
GPU 2 sends C to GPU 3
GPU 3 sends D to GPU 0
```

Gathering uses more temporary memory. Exchanging uses more communication steps
and more complicated attention bookkeeping.

#### Temporary prefill tensors versus retained KV

The K/V used while calculating distributed prefill attention and the KV cache
retained for future decoding are related but distinct:

```text
During prefill:
  GPUs gather or exchange K/V needed for attention computation.

After prefill:
  the engine retains K/V according to its persistent cache layout.
```

The gather-or-exchange decision does not by itself mean the final KV cache is
fully copied on every PCP GPU. In current vLLM terminology, PCP expands the
process world size but does not itself increase the persistent KV-cache shard
count. DCP separately controls how decode KV positions are sharded.

This also explains why prefill and decode use different context-parallel
methods. Prefill has many new query positions that PCP can divide. Decode
normally has only one new query position per active sequence, so DCP instead
divides the large stored history that this query reads.

PCP adds workers to the replica:

```text
GPUs per replica = TP × PP × PCP
```

The vLLM context-parallel documentation describes multiple prefill approaches
and currently characterizes them as under active development. See the
[vLLM context-parallel deployment guide](https://docs.vllm.ai/en/latest/serving/context_parallel_deployment/).

---

## 9. Decode Context Parallelism: DCP

DCP divides stored historical KV positions during decoding:

```text
One new query
    ├── attends to a KV shard on GPU 0
    ├── attends to a KV shard on GPU 1
    ├── attends to a KV shard on GPU 2
    └── attends to a KV shard on GPU 3
```

Each GPU calculates a partial attention result, and the results are combined.

### Main purpose

- Reduce duplicated KV storage.
- Increase effective KV capacity.
- Support longer contexts or more concurrent requests.

### Main cost

- Communication during decode attention.

DCP normally reuses existing TP or PCP ranks:

```text
DCP does not multiply the worker count.
```

Without PCP, current vLLM requires:

```text
TP % DCP = 0
```

With PCP enabled, current vLLM accepts:

```text
DCP=1
DCP=PCP
DCP=TP × PCP
```

These are implementation constraints and may change between releases. See the
[vLLM parallel configuration](https://docs.vllm.ai/en/latest/api/vllm/config/parallel/).

---

## 10. CP Versus PCP Versus DCP

- **CP** is the general category.
- **PCP** applies context parallelism while processing the prompt.
- **DCP** applies context parallelism while decoding.

```text
PCP divides the work of building a long context.

DCP divides the stored context used during generation.
```

| Property | PCP | DCP |
|---|---|---|
| Phase | Prefill | Decode |
| Divides | New prompt positions | Stored KV positions |
| Main metric | TTFT | ITL, TPOT, and decode capacity |
| Adds worker ranks | Yes | Usually no |
| Main benefit | Long-prompt processing | KV capacity |

---

## 11. How the Attention Architecture Affects TP

The examples in [Section 2](#2-tensor-parallelism-tp) show that a chosen TP
size may exceed the number of independently shardable KV heads. How quickly
this happens depends on the attention architecture:

| Architecture | Typical cached structure | Consequence for TP |
|---|---|---|
| **MHA** | Usually one KV head for each query head | KV often divides cleanly across several TP ranks |
| **GQA** | Several query heads share fewer KV heads | Large TP sizes may duplicate KV heads |
| **MQA** | Query heads share one KV head | The single KV head is normally repeated across TP ranks |
| **MLA** | Heads use a shared compressed latent cache representation | The cached latent state may be repeated across TP ranks |

For an illustrative 32-query-head model:

```text
MHA: 32 query heads, 32 KV heads, TP=8
     → 4 distinct KV heads per GPU

GQA: 32 query heads, 8 KV heads, TP=8
     → 1 distinct KV head per GPU

GQA: 32 query heads, 2 KV heads, TP=8
     → each KV head is represented on 4 GPUs

MQA or an MLA-style shared cache, TP=8
     → the cached representation may be repeated across all TP ranks
```

TP can remain necessary for model weights and compute in all four cases. DCP
addresses the separate KV-capacity problem by sharding cached token positions
after head-based sharding stops scaling efficiently.

---

## 12. Multi-head Latent Attention: MLA

MLA compresses K/V information into a smaller shared latent representation:

```text
Standard attention:
token → several complete K and V vectors

MLA:
token → compressed KV latent + required positional state
```

Every token still has its own latent state:

```text
Token 0 → latent 0
Token 1 → latent 1
Token 2 → latent 2
```

The representation is shared across attention heads, not across tokens.

With TP, MLA may have no useful KV-head dimension to divide:

```text
GPU 0: complete latent cache
GPU 1: duplicate
GPU 2: duplicate
GPU 3: duplicate
```

For models such as DeepSeek-R1, the vLLM guide uses `TP=8, DCP=8` as an
example that divides cached positions among the same eight GPUs and reduces
MLA cache duplication.

---

## 13. Expert Parallelism: EP

EP applies only to Mixture of Experts models. An MoE layer contains multiple
feed-forward experts:

```text
Token
  │
  ▼
Router
  ├── Expert 1
  └── Expert 4
```

EP places different experts on different GPUs:

```text
GPU 0: Experts 0–1
GPU 1: Experts 2–3
GPU 2: Experts 4–5
GPU 3: Experts 6–7
```

Token hidden states are routed to the selected experts and returned afterward.

### Main purpose

- Distribute very large expert weights.
- Execute selected experts across GPUs.

### Main cost

- All-to-all communication.
- Expert load imbalance.

EP does not primarily manage the KV cache. Attention layers create KV state;
MoE feed-forward layers route tokens through experts.

Current vLLM derives the expert-parallel group from the product of TP and DP
when EP is enabled. See the
[vLLM expert-parallel deployment guide](https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment/).

---

## 14. Ray's Role

Ray is an optional distributed runtime used to:

- discover nodes and GPUs;
- launch remote vLLM workers;
- assign processes to GPUs;
- place TP and PP ranks;
- coordinate process lifecycle;
- provide distributed observability and management.

Ray does not normally perform the heavy GPU tensor collectives:

```text
Ray:
  launches and manages workers

NCCL:
  moves tensors and performs GPU collectives
```

Typical use:

- one node: native multiprocessing is usually sufficient;
- multiple nodes: Ray is convenient but optional;
- Ray Serve deployment: Ray is required;
- manually managed multi-node vLLM: use the supported multiprocessing path.

The distinction is:

> **Ray assigns workers to stations; NCCL moves the tensors between them.**

See [What is Ray? in the vLLM scaling guide](https://docs.vllm.ai/en/latest/serving/parallelism_scaling/#what-is-ray).

---

## 15. Role of the Interconnect

Different parallel strategies have different communication needs:

| Technology | Typical role |
|---|---|
| NVLink | Fast GPU-to-GPU communication inside a node |
| NVSwitch | Connects many GPUs in one high-bandwidth fabric |
| PCIe | General device interconnect, usually slower for collectives |
| InfiniBand | High-speed communication across nodes |
| RoCE | RDMA over Ethernet across nodes |
| Ethernet | Routing, APIs, control traffic, and less intensive communication |
| NVLink-C2C | High-bandwidth CPU-to-GPU connection on supported platforms |

A practical placement guideline is:

```text
TP and DCP → preferably within NVLink/NVSwitch domains
EP         → fast all-to-all fabric
PP         → more reasonable across nodes
DP         → can operate through a normal load balancer
```

---

## 16. CPU and RAM Offloading

“CPU offloading” normally means placing data in the node's system RAM. Model
weights and KV blocks use different mechanisms.

### Model-weight offloading

```bash
--cpu-offload-gb <GiB>
```

- The value is per GPU.
- Offloaded weights are needed during every forward pass.
- It is most attractive when the model only slightly exceeds GPU capacity.
- Extensive offloading can reduce throughput.

For four GPUs with 10 GiB per GPU:

```text
4 GPUs × 10 GiB = up to 40 GiB of host RAM
```

### KV offloading

```bash
--kv-offloading-size <GiB>
```

- The value is total across TP ranks.
- The documented offload cache extends reusable prefix-cache capacity.
- Completed cacheable prefix blocks can be retained in host memory.
- A later matching request can promote them back to the GPU.

It should not be described as demand paging an active request's complete KV
history on every decode step. See the
[vLLM model-weight offload configuration](https://docs.vllm.ai/en/latest/api/vllm/config/offload/),
[cache configuration](https://docs.vllm.ai/en/latest/api/vllm/config/cache/),
and [KV offloading guide](https://docs.vllm.ai/en/latest/features/kv_offloading_usage/).

---

## 17. Combining the Strategies

Conceptually, these strategies can coexist:

```text
TP=4
PP=2
PCP=2
DP=2
DCP=2
```

The physical worker count is:

```text
TP × PP × PCP × DP
= 4 × 2 × 2 × 2
= 32 GPUs
```

DCP is excluded because it reuses existing ranks:

```text
GPUs per DP replica = TP × PP × PCP

Total worker GPUs = TP × PP × PCP × DP
```

Different mechanisms become relevant at different stages:

```text
Request routing:
  DP chooses a replica

Prefill:
  PP divides layers
  TP divides layer operations
  PCP divides prompt positions

Decode:
  PP divides layers
  TP divides layer operations
  DCP divides stored KV positions
```

Although a configuration may be conceptually valid, the exact combination
must be supported by the model, attention backend, hardware platform, and
installed vLLM version.

---

## 18. Common Production Layouts

Production teams generally start with the simplest layout that satisfies
memory, throughput, and latency requirements.

### Model fits on one GPU

```text
TP=1
PP=1
many independent replicas
```

This avoids model-parallel communication and is usually the simplest layout.

### Model needs several GPUs in one node

```text
TP=2, TP=4, or TP=8
multiple replicas if capacity permits
```

For an eight-GPU node where one replica needs four GPUs:

```text
Replica 0: GPUs 0–3, TP=4
Replica 1: GPUs 4–7, TP=4
```

Conceptually, this is `TP=4, DP=2`.

### Model fills an eight-GPU node

```text
One node: TP=8, one replica
```

Additional nodes usually host additional replicas behind a load balancer.

### Model needs multiple nodes

```text
TP = GPUs per node
PP = nodes per replica
```

For two eight-GPU nodes:

```text
TP=8
PP=2
```

### MLA or GQA model with KV duplication

```text
TP + DCP
```

Add DCP after measurements show that duplicated KV, long contexts, or decode
concurrency are limiting capacity.

### MoE model

```text
TP/DP + EP
```

EP distributes expert weights and work; TP or DP handles the non-expert parts
according to the selected layout.

### Extremely long prompt prefill

```text
First: tune token budgets and chunked prefill
Then: consider PCP
```

### Different prefill and decode SLOs

Consider prefill/decode disaggregation:

```text
Request → prefill pool → transfer KV → decode pool
```

This permits independent scaling of prefill and decode, but adds KV-transfer,
routing, networking, and failure-handling complexity.

---

## 19. Techniques Commonly Used Alongside Parallelism

These are not parallelism types:

- **PagedAttention:** manages KV memory in blocks.
- **Continuous batching:** changes which requests run together each engine
  step.
- **Chunked prefill:** divides large prompt work into schedulable chunks.
- **Prefix caching:** reuses KV blocks for repeated prefixes.
- **KV quantization:** stores each KV scalar with fewer bytes.
- **KV offloading:** extends reusable cache into another memory tier.
- **Admission control:** limits work before queues or KV memory become
  unstable.
- **Cache-aware routing:** sends requests to replicas likely to have matching
  prefixes.

These mechanisms often matter as much as the parallel layout. Parallelism
controls placement; scheduling and caching determine how efficiently the
deployment uses that placement.

---

## 20. Production Selection Guide

Choose a strategy to solve a measured problem:

| Problem | Likely strategy |
|---|---|
| Model does not fit one GPU | TP |
| Model does not fit one node | TP + PP |
| Need more independent traffic capacity | DP or replicated instances |
| TP duplicates too much KV | DCP |
| Very long prompt TTFT | Chunked prefill, then consider PCP |
| Large MoE expert weights | EP |
| Prefill disrupts decode latency | Chunked prefill or P/D disaggregation |
| Model barely exceeds GPU capacity | Weight quantization or limited CPU offloading |
| Repeated prompts | Prefix caching and cache-aware routing |

The most common dense-model production arrangement is:

```text
TP inside each model replica
            +
multiple replicas behind a load balancer
```

PP is added when one replica exceeds one node. DCP is added when KV
duplication or long-context capacity becomes a problem. PCP, EP, and
prefill/decode disaggregation solve more specialized bottlenecks.

---

## 21. Compact Mental Model

```text
TP:  You calculate part of this layer.
PP:  You own these complete layers.
DP:  You handle different requests.
PCP: You process this part of the prompt.
DCP: You store this part of the decode history.
EP:  You own these MoE experts.
```

For GPU arithmetic:

```text
GPUs per replica = TP × PP × PCP

Total worker GPUs = TP × PP × PCP × DP

DCP reuses existing ranks and does not multiply this count.
```

## References

- [vLLM parallelism and scaling](https://docs.vllm.ai/en/latest/serving/parallelism_scaling/)
- [vLLM data-parallel deployment](https://docs.vllm.ai/en/latest/serving/data_parallel_deployment/)
- [vLLM context-parallel deployment](https://docs.vllm.ai/en/latest/serving/context_parallel_deployment/)
- [vLLM expert-parallel deployment](https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment/)
- [vLLM parallel configuration](https://docs.vllm.ai/en/latest/api/vllm/config/parallel/)
- [vLLM model-weight offload configuration](https://docs.vllm.ai/en/latest/api/vllm/config/offload/)
- [vLLM KV-cache configuration](https://docs.vllm.ai/en/latest/api/vllm/config/cache/)
- [vLLM KV offloading usage guide](https://docs.vllm.ai/en/latest/features/kv_offloading_usage/)
