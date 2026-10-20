# Production vLLM Strategies: Model, KV Cache, and Requests

Production inference involves several separate placement decisions. A request
needs model weights to run, space for its growing KV cache, and a worker that
will process each phase. The names TP, PP, DP, CP, DCP, P/D, and EP describe
different ways to make those decisions. They can be combined.

This guide starts with the practical meaning of each strategy, then goes deeper
into how TP and DCP place the KV cache across GPUs. For the mechanics of
physical KV blocks, prefix sharing, and request cleanup, see the
[KV Cache and PagedAttention guide](./KV%20Cache%20and%20PagedAttention%20in%20vLLM%20-%20Illustrated%20Guide.md).

## 1. Four Things to Place

Before choosing a strategy, distinguish these four things:

1. **Model weights** are the parameters used to run each transformer layer.
   They occupy GPU memory even when no request is active.
2. **A model replica** is one complete serving copy of the model. The copy may
   itself span several GPUs through TP or PP.
3. **A request's KV cache** holds keys and values computed for its previous
   tokens. It grows as the request processes more prompt and output tokens.
4. **A request's phases** are **prefill**, which processes the prompt, and
   **decode**, which generates subsequent tokens.

The central questions are: *How many GPUs cooperate on one replica? Which
replica receives each request? Where does that request's KV live? Do prefill
and decode run in the same worker pool?*

## 2. The Strategies at a Glance

| Strategy | What it divides | What happens to a request's KV? | Typical reason to use it |
|---|---|---|---|
| **TP: tensor parallelism** | Work and weights within model layers across GPUs | Attention KV heads can be split across TP GPUs; with too few distinct KV heads, some KV may be duplicated | A replica needs multiple GPUs for memory or compute |
| **PP: pipeline parallelism** | Model layers into successive stages | Each stage stores KV for its own layers | A replica needs more GPUs or nodes, or layer placement suits the hardware |
| **DP: data parallelism** | Independent requests across model replicas | Each replica has its own KV cache for the requests routed to it | Serve more independent traffic |
| **CP: context parallelism** | Work or storage along a request's token sequence | The exact layout depends on the prefill or decode method | Process long contexts across GPUs |
| **DCP: decode context parallelism** | Stored KV positions during decode | KV positions are sharded across participating GPUs, often to remove TP duplication | Reduce per-GPU KV occupancy for long or concurrent requests |
| **P/D disaggregation** | Prefill and decode into separate worker pools | Prefill builds KV and transfers it to decode | Tune first-token and subsequent-token latency separately |
| **EP: expert parallelism** | Experts in a mixture-of-experts (MoE) model | Expert placement is separate from attention KV placement | Distribute MoE expert weights and work |

The first three rows answer different questions: **TP and PP divide one
replica; DP creates more request-serving replicas.** CP and DCP deal with
context placement. P/D divides a request over time. EP applies when the model
has experts. See vLLM's [parallelism and scaling guide](https://docs.vllm.ai/en/latest/serving/parallelism_scaling/),
[data parallel guide](https://docs.vllm.ai/en/latest/serving/data_parallel_deployment/),
[context parallel guide](https://docs.vllm.ai/en/latest/serving/context_parallel_deployment/),
and [expert parallel guide](https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment/).

## 3. Follow Two Requests Through a Deployment

Suppose a model needs two GPUs to form one usable replica. You have four GPUs
and choose `TP=2, DP=2`:

```text
                   ┌─ Replica 1: GPU 0 + GPU 1 (TP=2) ← Request A
Incoming requests ─┤
                   └─ Replica 2: GPU 2 + GPU 3 (TP=2) ← Request B
```

Request A runs on **both GPUs in replica 1**: they cooperate on the same model
steps. Request B runs on both GPUs in replica 2. The replicas hold separate
copies of the model and maintain separate KV pools. Adding DP capacity helps
with independent requests; it does not reduce the KV memory required by one
request *within* its replica. vLLM documents this combined `DP=2, TP=2`
pattern in its [data parallel guide](https://docs.vllm.ai/en/latest/serving/data_parallel_deployment/).

Now consider a different choice: **P/D disaggregation**. One request visits
two *pools* in sequence:

```text
Request A's prompt → Prefill pool → transfer A's KV → Decode pool → answer
```

Each pool must be able to run the model; each can use its own TP, PP, or other
supported layout. The prefill pool builds the cache for A's prompt. The decode
pool receives that cache and uses it while generating A's output. This is the
answer to **“one worker runs prefill and another runs decode.”** It is a
different decision from DP: in DP, different replicas normally receive
different requests; in P/D, the *same* request moves between phase-specific
pools. vLLM's [disaggregated prefill guide](https://docs.vllm.ai/en/latest/features/disagg_prefill/)
describes separate tuning of time to first token and inter-token latency and
explicitly cautions that disaggregation by itself does not improve throughput.

## 4. What Each Model Layout Means for KV

### TP: divide work within a layer

Imagine an attention layer with eight KV heads and `TP=4`. Conceptually, each
GPU can own two KV heads and their cached K/V values. The GPUs also hold
different weight shards and must combine partial model results. Fast GPU links
matter because this communication repeats throughout inference. vLLM commonly
recommends TP when a model needs multiple GPUs within one node.

If TP exceeds the number of independently shardable KV heads, adding TP GPUs
does not keep reducing KV bytes per GPU proportionally. Some KV heads are
replicated. That is one reason DCP exists; the detailed example below shows how
it uses the sequence dimension after TP reaches this KV-sharding floor.

### PP: divide layers

With `PP=2` and an 80-layer model, one stage might execute the early layers
and the next stage the later layers. Each stage needs the weights and KV cache
for *its* layers. A request's hidden states pass from one stage to the next.
Several requests can be in flight to keep both stages busy. The exact layer
split and performance depend on the model and deployment. vLLM recommends
combining TP within nodes and PP across nodes when a model is too large for one
node; it also describes cases where PP suits GPUs without a fast interconnect.
[vLLM scaling guide](https://docs.vllm.ai/en/latest/serving/parallelism_scaling/).

### CP and DCP: divide the context

**Prefill context parallelism** can distribute tokens from one long prompt so
several GPUs work on different portions. Those portions still need access to
the earlier K/V state required by causal attention, so prefill methods differ
in how they exchange or gather K/V.

**Decode context parallelism** addresses a different point in the request.
During decode, the current query attends to a long stored history. DCP shards
that history by token position and combines partial attention results. In
vLLM, DCP can use GPUs already present in a TP group to reduce duplicated KV
storage; a larger DCP degree also adds communication. It is especially
relevant when TP has more ranks than distinct KV-head partitions. See the
[vLLM context parallel guide](https://docs.vllm.ai/en/latest/serving/context_parallel_deployment/)
for current behavior and constraints.


A **rank** in the discussion below is one GPU process within a model replica. The replica may contain several ranks that cooperate on the same request.

The August 2026 vLLM article
[Efficient Decode Context Parallelism with vLLM for Long Context Workloads](https://vllm.ai/blog/2026-08-07-decode-context-parallelism)
describes **Decode Context Parallelism (DCP)**. DCP shards KV cache by token
position—the sequence or context dimension—across GPUs already participating in
the model replica.

> **Illustration and version note:** the three figures in this section come
> from that official vLLM article and are loaded from the vLLM website rather
> than copied into this repository. DCP constraints, supported attention
> backends, environment variables, and collective implementations are
> version-sensitive. Check the
> [current context-parallel deployment documentation](https://docs.vllm.ai/en/latest/serving/context_parallel_deployment/)
> before configuring a deployment.

![Comparison of tensor-parallel KV duplication and sequence-sharded Decode Context Parallelism](https://vllm.ai/blog-assets/figures/2026-07-27-decode-context-parallelism/kv-parallelism-overview.svg)

*Illustration: [KV parallelism overview](https://vllm.ai/blog-assets/figures/2026-07-27-decode-context-parallelism/kv-parallelism-overview.svg), from the [official vLLM DCP article](https://vllm.ai/blog/2026-08-07-decode-context-parallelism), by Seonghee Lee, Sungsoo Ha, Omri Almog, and Lucas Wilkinson.*

#### Why tensor parallelism reaches a KV-sharding floor

For a request with `T` cached positions and `Hkv` KV heads, its logical cache
has both a head dimension and a sequence dimension:

```text
logical KV state ~= [layers, 2, Hkv, T, head_dimension]
```

Ordinary tensor parallelism can assign different KV heads to different ranks.
That works until each distinct KV head is already owned by one rank. If the TP
degree grows beyond the number of independently shardable KV heads, additional
ranks may hold duplicate cache data instead of a smaller unique slice.

This appears quickly with modern attention designs:

- **GQA** deliberately uses fewer KV heads than query heads.
- **MQA** has only one shared KV head.
- **MLA** stores one compressed latent representation shared across query
  heads, behaving like a single KV-head dimension for this capacity problem.

#### Reading the overview illustration

- In head-sharded TP, colors represent different KV heads assigned to ranks.
- Once there are more ranks than distinct head shards, colors repeat: that is
  KV duplication.
- DCP repurposes those would-be duplicate ranks to hold different token-position
  slices.
- Each rank in a DCP group stores a different portion of the long context,
  lowering per-GPU KV occupancy and leaving room for more concurrent requests.
  Copies may remain across TP ranks if DCP uses less than the available
  duplication factor.

#### PagedAttention and DCP solve different layout levels

| Mechanism | Mapping it manages | Primary benefit |
|---|---|---|
| PagedAttention | Logical request blocks to physical KV blocks | Dynamic allocation with low fragmentation |
| Tensor-parallel KV sharding | KV heads to GPU ranks | Splits head-specific state while distinct heads remain |
| Decode Context Parallelism | Token-position slices to GPU ranks | Reduces KV duplication and per-rank long-context state |

They complement one another. A rank can own a context slice that is itself
stored in paged physical blocks. DCP does not replace the request's logical
block table, and PagedAttention alone does not decide how a distributed replica
partitions cache across GPUs.

#### A four-GPU example

For a 200,000-token context and DCP degree four, a simplified contiguous
explanation is:

```text
GPU 0: positions      0 to  49,999
GPU 1: positions 50,000 to  99,999
GPU 2: positions 100,000 to 149,999
GPU 3: positions 150,000 to 199,999
```

The implementation can interleave physical blocks rather than assigning one
single contiguous range per rank so future tokens distribute naturally as the
sequence grows. Within a DCP group, each rank owns a distinct portion of the
sequence dimension.

For a conceptual block with `B` token slots on each rank, interleaving makes
the corresponding request-wide attention block span `B x DCP degree` token
positions. A request-wide token position must be mapped to its owning rank and
local offset before that rank consults its block table. The single-rank formula
from the companion guide cannot use the physical `B` unchanged here.

Let `head_shard_count` be the number of distinct KV-head partitions across TP
ranks. Ignoring metadata, imbalance, padding, and other caches, ideal balanced
sharding of an unsharded request cache gives the per-GPU estimate:

```text
KV bytes per GPU ~= total unsharded request KV bytes
                    / (head_shard_count x DCP degree)
```

For MLA, `head_shard_count` is effectively one. For the GQA example with four
KV heads, `TP=8`, and `DCP=2`, it is four, so each GPU stores approximately
one-eighth of the unsharded request cache. If the DCP degree is smaller than
the KV-duplication factor, some copies remain across TP ranks. The total
logical K/V state still exists. DCP changes its placement; it is not
compression.

#### How one decode attention step works

![Decode Context Parallelism gathers the query, computes against local KV slices, and combines partial attention results](https://vllm.ai/blog-assets/figures/2026-07-27-decode-context-parallelism/figure-5.png)

*Illustration: [Decode Context Parallelism execution flow](https://vllm.ai/blog-assets/figures/2026-07-27-decode-context-parallelism/figure-5.png), from the [official vLLM DCP article](https://vllm.ai/blog/2026-08-07-decode-context-parallelism), by Seonghee Lee, Sungsoo Ha, Omri Almog, and Lucas Wilkinson.*

The standard process described in the article is:

1. **Assemble the query.** Each rank needs the query representation required to
   score its local keys. The DCP group all-gathers query fragments, unless a
   supported optimized path avoids that collective.
2. **Compute locally.** Every rank applies attention between the query and its
   locally owned K/V context slice.
3. **Merge partial attention.** Ranks exchange partial outputs and log-sum-exp
   normalization state. Online-softmax identities combine the independently
   computed slices into the same logical attention result.
4. **Return the appropriate output shard.** A reduce-scatter or supported
   equivalent leaves each rank with the result needed by following operations.

The merge cannot simply average partial outputs. Each context slice has a
different score maximum and normalization sum, so its contribution must be
reweighted consistently with a softmax over the complete logical history.

#### The communication trade-off

DCP exchanges lower per-rank KV capacity for additional communication:

```text
benefit
  = less duplicated KV per GPU
  + more cache capacity for concurrent requests
  + local attention scans over shorter context slices

cost
  = query collective
  + partial-output/LSE collective
  + synchronization and possible rank imbalance
```

The query is small during ordinary one-token decode, which makes distributing
context more attractive than it would be for a large prompt matrix. The output
merge still needs a high-bandwidth, low-latency interconnect. DCP is therefore
most compelling for long contexts, significant KV duplication, and GPUs joined
by fast links.

#### GQA and MLA have different DCP opportunities

For a GQA model, head-based TP remains useful until the KV heads have been
distributed. DCP can use the remaining duplication factor to split the sequence
dimension. An illustrative layout with four KV heads and `TP=8` has two copies
of each head shard; `DCP=2` can turn those copies into two different context
slices.

For MLA, the compressed latent cache is effectively shared rather than divided
into many independent KV heads. Plain TP can therefore replicate the latent
cache across ranks. DCP can assign each rank a different sequence slice, making
MLA long-context deployments a strong use case.

Exact valid relationships among TP degree, KV-head count, DCP degree, model
architecture, and attention backend are release-specific. Do not infer a valid
production configuration from the simplified arithmetic alone.

#### What the published benchmark demonstrates

![Published vLLM comparison of throughput, interactivity, and KV utilization for baseline TP and DCP](https://vllm.ai/blog-assets/figures/2026-07-27-decode-context-parallelism/figure-3.png)

*Illustration: [DCP scaling and KV-utilization results](https://vllm.ai/blog-assets/figures/2026-07-27-decode-context-parallelism/figure-3.png), from the [official vLLM DCP article](https://vllm.ai/blog/2026-08-07-decode-context-parallelism), by Seonghee Lee, Sungsoo Ha, Omri Almog, and Lucas Wilkinson.*

The article's experiment used one eight-GPU B200 node, Kimi K2.6 in NVFP4, and
a long-context agentic trace. In that specific setup, baseline TP reached full
KV utilization at concurrency 64 and plateaued near 1,863 tokens/s/GPU. The DCP
configuration continued to concurrency 512, reported 82% KV utilization there,
and reached 6,091 tokens/s/GPU.

These numbers demonstrate the capacity mechanism for that model, hardware,
dtype, software revision, and workload. They are not a universal DCP speedup.
A production comparison should hold the following constant:

- model and quantization;
- GPU count and topology;
- prompt/output length distribution;
- prefix-reuse pattern;
- request concurrency or arrival process;
- TTFT/TPOT SLOs;
- attention backend and vLLM revision.

Measure per-rank KV occupancy, collective time, TPOT, total output throughput,
and goodput together. Lower cache duplication is valuable only if communication
does not push user latency beyond the target.

#### Configuration shape

The article demonstrates the feature alongside tensor parallelism:

```bash
vllm serve <model-name> \
  --tensor-parallel-size <tp-size> \
  --decode-context-parallel-size <dcp-size>
```

The DCP degree organizes GPUs already present in the tensor-parallel model
replica; it is not simply an instruction to launch that many additional GPUs.
Confirm the selected model/backend combination and constraints in the current
documentation before deployment.

### DP and EP: distinguish replicas from experts

For a dense model, DP usually means model replicas with independent request
batches and independent local KV caches. If two requests share a prompt but
land on different replicas, a local prefix-cache hit on one replica does not
automatically help the other. Routing can consider queue length and cache
locality. [vLLM data parallel guide](https://docs.vllm.ai/en/latest/serving/data_parallel_deployment/).

EP applies to **MoE models only**. Different GPUs store and execute different
experts. Attention and expert layers can use different placement strategies;
vLLM can combine DP for attention with EP for experts. This is why an MoE
deployment should not be pictured simply as several completely independent
full-model copies. [vLLM expert parallel guide](https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment/).

### Scaling within a replica versus adding replicas

DCP partitions one request's KV history **within one model replica**. Adding
replicas is a different operation: each replica serves separate requests, has
its own GPU memory, and normally has its own local prefix cache. A block cached
only on replica A cannot help a request sent to replica B unless that KV state
is shared or transferred.

Prefix-aware routing can send related requests to the same replica so that they
reuse locally cached blocks. The router must balance that locality against queue
length and free KV capacity. See the [vLLM Production Stack prefix-aware routing
guide](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/prefix-aware-routing.html).

| Scaling choice | What it changes for the KV cache | Main question |
|---|---|---|
| More TP ranks in one replica | May split KV heads, then duplicate them after the head-sharding floor | Are the heads already fully sharded? |
| DCP ranks within that replica | Split token positions across participating ranks | Does the saved memory outweigh the collective cost? |
| More model replicas through DP | Add independent request-serving capacity and local cache pools | Will routing preserve useful prefix locality? |

## 5. KV and Scheduling Techniques That Work Alongside Them

These choices change memory use or scheduling without defining a new TP, PP,
or DP layout:

| Technique | What it does | Question it answers |
|---|---|---|
| **PagedAttention** | Allocates a request's KV in fixed-size physical blocks as the request grows | How do we use each GPU's KV memory without reserving a contiguous maximum-length buffer? |
| **Prefix caching** | Reuses eligible cached KV blocks for an identical prompt prefix | Can we avoid recomputing a prefix already processed here? |
| **KV quantization** | Stores KV values with fewer bytes per element, where supported | Can more KV fit in GPU memory? |
| **KV offloading** | Moves reusable KV state to a larger, slower tier and fetches it when needed | Can we trade transfer time for more cache capacity? |
| **Continuous batching** | Updates the active batch as requests arrive, generate tokens, and finish | Which ready requests should run in the next model step? |
| **Chunked prefill** | Schedules a large prompt in smaller token chunks | How can prefill share scheduling time with other work? |

PagedAttention and prefix caching are explained in the
[KV Cache and PagedAttention guide](./KV%20Cache%20and%20PagedAttention%20in%20vLLM%20-%20Illustrated%20Guide.md).
For current support and options, use vLLM's
[prefix caching design](https://docs.vllm.ai/en/latest/design/prefix_caching/),
[cache configuration](https://docs.vllm.ai/en/latest/api/vllm/config/cache/),
[KV offloading guide](https://docs.vllm.ai/en/latest/features/kv_offloading_usage/),
and [scheduler configuration](https://docs.vllm.ai/en/latest/api/vllm/config/scheduler/).

## 6. Choose by the Actual Bottleneck

| What you observe | Start by considering | Why |
|---|---|---|
| A model replica does not fit on one GPU | TP; PP if layer placement better suits the GPUs | Divide weights and their associated work |
| A model replica does not fit on one node | TP within nodes plus PP across nodes | Divide both within layers and across layers |
| Enough capacity per request, but too many independent requests | DP and request routing | Add independent request-serving capacity |
| Long prompts cause high time to first token | Prefill context parallelism or suitable prefill scheduling | Distribute or schedule prompt work |
| Long active contexts exhaust per-GPU KV memory | Examine KV-head sharding, then DCP where supported | Reduce duplicated or per-GPU KV storage |
| Prefill work disrupts decode latency | Chunked prefill or P/D disaggregation | Control how the phases compete for GPU time |
| MoE experts dominate model placement | EP with the attention layout the model needs | Distribute expert weights and execution |
| Repeated prompt prefixes consume compute | Prefix caching and cache-aware routing | Reuse eligible prompt KV blocks |

These are starting points, not automatic performance promises.

## 7. What to Measure

Compare the same model, GPU count, workload, and latency targets when testing
layouts. Track:

- model-weight memory and KV occupancy **per rank**, including duplicated KV;
- free KV blocks, running and waiting requests, and preemptions;
- prompt and output lengths, request concurrency, and prefix-cache hit rate;
- time to first token (TTFT), time per output token (TPOT), total throughput,
  and goodput at the required latency limits;
- collective latency and GPU-to-GPU communication time for TP, DCP, or EP;
- KV-transfer time when prefill and decode are disaggregated.

Higher admitted concurrency is useful only while requests still meet the
intended latency target. Likewise, lower cache duplication helps only when the
additional communication does not erase the memory benefit.

**Memory aid:** TP splits work *within layers*; PP splits *layers*; DP splits
*requests*; CP/DCP split *context work or KV storage*; P/D splits *phases*; EP
splits *experts*. PagedAttention, caching, and scheduling determine how those
workers use their local memory and time.

## References

- [vLLM Parallelism and Scaling](https://docs.vllm.ai/en/latest/serving/parallelism_scaling/)
- [vLLM Context Parallel Deployment](https://docs.vllm.ai/en/latest/serving/context_parallel_deployment/)
- [vLLM Data Parallel Deployment](https://docs.vllm.ai/en/latest/serving/data_parallel_deployment/)
- [vLLM Expert Parallel Deployment](https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment/)
- [vLLM Disaggregated Prefilling](https://docs.vllm.ai/en/latest/features/disagg_prefill/)
- [Efficient Decode Context Parallelism with vLLM for Long Context Workloads](https://vllm.ai/blog/2026-08-07-decode-context-parallelism)
- [vLLM Production Stack: Prefix-Aware Routing](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/prefix-aware-routing.html)
