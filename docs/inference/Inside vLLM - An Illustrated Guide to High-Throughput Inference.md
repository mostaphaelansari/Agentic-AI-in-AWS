# Inside vLLM: An Illustrated Guide to High-Throughput Inference


This guide explains how vLLM turns a transformer model into a high-throughput
inference system. It is an original companion to Aleksa Gordic's article,
[Inside vLLM: Anatomy of a High-Throughput LLM Inference System](https://www.aleksagordic.com/blog/vllm).
It follows the same visual progression while focusing on the mental models
needed to reason about scheduling, GPU memory, latency, and scale.

> **Version note:** the source article examines vLLM V1 at commit `42172ad`
> from August 9, 2025. The architecture and performance concepts remain useful,
> but class names, defaults, flags, and process topology can change. Current
> behavior should always be checked against the
> [official vLLM documentation](https://docs.vllm.ai/en/latest/).

> **Diagram attribution:** every bitmap diagram below was created by Aleksa
> Gordic for the source article and is loaded from the author's website. The
> images are referenced, not copied into this repository.

## 1. The Mental Model

vLLM is not merely a faster implementation of `model.forward()`. It is a
serving engine built around several cooperating systems:

| System | Responsibility |
|---|---|
| Input processor | Validates requests, tokenizes prompts, and creates internal request objects |
| Scheduler | Chooses which prompt and generation tokens enter the next GPU step |
| KV-cache manager | Allocates and releases token-state memory in fixed-size blocks |
| Model executor | Coordinates workers and launches model computation |
| GPU worker | Holds model weights and executes forward passes |
| Output processor | Detokenizes generated IDs, applies stopping rules, and streams results |
| Serving layer | Accepts concurrent network requests and routes them to engine cores |

The most important loop is:

1. Admit new requests.
2. Select tokens that fit the current scheduling budget.
3. Reserve KV-cache blocks for those tokens.
4. Run one model step over the combined batch.
5. Sample and return new tokens.
6. Release completed requests and repeat.

Everything else either makes this loop more memory-efficient, reduces the work
inside it, or distributes it across more hardware.

---

## 2. Constructing the Engine

The engine is divided into a user-facing layer and an execution core. The
outer layer converts between API-friendly objects and internal engine objects.
The core owns scheduling, GPU execution, structured-output state, and KV-cache
management.

![vLLM engine constructor and its principal components](https://www.aleksagordic.com/blog/vllm/engine_constructor.png)

*Diagram: [LLM engine constructor](https://www.aleksagordic.com/blog/vllm/engine_constructor.png), by Aleksa Gordic.*

### How to read the diagram

Start on the left with `LLMEngine`:

- The **processor** turns prompts, token IDs, embeddings, sampling parameters,
  and priorities into engine requests.
- The **engine core client** is the boundary between the frontend and the core.
  In a simple offline process it can be direct; in a distributed deployment it
  becomes an inter-process or network client.
- The **output processor** converts internal token outputs into the response
  object consumed by the application.

Inside `EngineCore`:

- The **scheduler** owns waiting and running requests.
- The **KV-cache manager** tracks which physical cache blocks belong to which
  requests.
- The **model executor** asks one or more workers to execute the selected step.
- The **structured-output manager** maintains grammar constraints used during
  guided decoding.

### Is `vLLM config` a YAML manifest?

In the diagram, `vLLM config` means the engine's complete internal
configuration object. It is a concept, not necessarily one specific file.
vLLM can construct it from several inputs:

- CLI arguments passed to `vllm serve`;
- arguments passed to the Python `LLM(...)` API;
- an optional vLLM YAML configuration file;
- environment variables, model metadata, and vLLM defaults.

For example, these CLI arguments contribute to the internal configuration:

```bash
vllm serve <model-name> \
  --dtype bfloat16 \
  --max-model-len 8192 \
  --tensor-parallel-size 4
```

A Kubernetes manifest is one level outside vLLM. It describes the container,
GPUs, replicas, volumes, networking, and environment, and usually passes the
vLLM arguments to the container:

```text
Kubernetes YAML
  -> container command, arguments, and environment
  -> vLLM internal config
  -> configured engine
```

Therefore, the box can be backed by YAML, but it should not be read as “this is
always a Kubernetes manifest” or “vLLM requires one YAML file.”

### Why tokenize when the model is already installed?

Installing or downloading a model makes its files available: primarily its
weights, architecture configuration, and tokenizer files. It does not convert
future user prompts into tokens ahead of time because those prompts do not
exist yet.

The model's mathematical layers accept numeric token IDs and embeddings, not
raw text. Every incoming text prompt must therefore follow this path:

```text
"The cat sat"
  -> tokenizer loaded for this model
  -> numeric token IDs
  -> embedding vectors on the GPU
  -> transformer layers
```

The tokenizer itself is normally loaded once when the server starts, just like
the model. The **mapping operation** still runs for each new prompt. Different
models can split the same sentence into different tokens, so vLLM must use the
tokenizer associated with the served model. A client may sometimes submit token
IDs directly, but ordinary chat requests contain text and need tokenization.

### Worker initialization

A GPU worker generally performs three expensive setup phases:

1. **Initialize the device.** Select the CUDA device, validate the requested
   dtype, initialize distributed groups, and create runtime buffers.
2. **Load the model.** Construct the architecture, load weight tensors, switch
   to inference mode, and optionally compile parts of the model.
3. **Initialize inference memory.** Profile memory usage, determine how many KV
   blocks fit, allocate cache tensors, select attention kernels, and warm up
   execution shapes.

CUDA graphs can capture repeated GPU work and replay it with less CPU launch
overhead. They improve steady-state latency, but require compatible shapes and
consume memory for captured graph state.

### The memory decision made at startup

The worker cannot give all VRAM to the KV cache. A rough budget is:

```text
available KV memory
  = usable GPU memory
  - model weight shard
  - activations and temporary tensors
  - CUDA graph and kernel workspace
  - communication buffers
  - runtime safety margin
```

Profiling converts the remaining bytes into a number of allocatable KV blocks.
That number later limits the combined context of all active requests.

---

## 3. The Engine Step Loop

Once requests exist, the engine repeatedly performs **schedule, execute, and
postprocess**.

![vLLM engine scheduling and execution loop](https://www.aleksagordic.com/blog/vllm/engine_loop.png)

*Diagram: [Engine loop](https://www.aleksagordic.com/blog/vllm/engine_loop.png), by Aleksa Gordic.*

### How to read the diagram

Follow one request clockwise:

1. `add_request` places it in the scheduler's waiting queue.
2. `schedule` chooses some of its tokens for the next step.
3. `execute_model` performs the GPU forward pass.
4. Engine output is applied to the request.
5. If the request is unfinished, it participates in another step.
6. If a stopping condition is met, its cache blocks are released and its final
   result leaves the engine.

Common stop conditions include:

- the configured output-token or model-length limit;
- an end-of-sequence token;
- a configured stop token;
- a configured stop string;
- explicit cancellation by the client.

### Offline versus online execution

An offline synchronous call begins with a known collection of prompts and
blocks until they finish. An online asynchronous server can receive new prompts
while earlier requests are decoding. The core step is similar, but the online
frontend continuously feeds and drains it.

This is the foundation of **continuous batching**: the batch is reconsidered
after each engine step instead of remaining fixed until every original member
finishes.

---

## 4. What the Scheduler Actually Schedules

The scheduler does not simply choose a list of requests. It chooses a number
of **tokens** from each request while respecting several constraints:

- maximum batched tokens for the step;
- maximum active sequences;
- available KV-cache blocks;
- request priority or arrival order;
- cached prompt blocks that do not need recomputation;
- speculative tokens that need verification;
- encoder or multimodal budgets where applicable.

### Prefill work

During prefill, the model processes prompt tokens and creates their KV states.
Long prompts expose substantial matrix multiplication parallelism and are often
compute-intensive. Prefill largely determines **time to first token (TTFT)**.

### Decode work

During ordinary decode, each active request contributes one new token to the
step. Previous token states are read from the KV cache. The model weights still
have to be read for every step, so decode at small batch sizes is commonly
limited by memory bandwidth. Decode largely determines **inter-token latency
(ITL)** and **time per output token (TPOT)**.

### Block allocation

The scheduler asks the KV-cache manager to reserve enough blocks for scheduled
tokens. For a block size of 16 tokens:

```text
required blocks = ceil(tokens needing new KV state / 16)
```

If there are not enough free blocks, the scheduler must delay work or preempt a
lower-priority request. A preempted request may later recompute evicted state,
so memory pressure can turn directly into additional GPU work and latency.

![A request-to-block mapping and the free KV block queue](https://www.aleksagordic.com/blog/vllm/kv_cache_blocks.png)

*Diagram: [KV-cache blocks](https://www.aleksagordic.com/blog/vllm/kv_cache_blocks.png), by Aleksa Gordic.*

### How to read the diagram

- Each request owns a logical sequence of block IDs.
- The blocks do not need to be contiguous in physical GPU memory.
- The free-block queue contains blocks available for new tokens.
- Finishing or preempting a request returns its blocks to that pool.
- A block table translates logical token positions into physical blocks during
  attention.

This indirection is the essence of PagedAttention. It avoids reserving one
large contiguous region for every request and reduces fragmentation when
sequence lengths differ.

### KV bytes per block

For a standard attention layer, the approximate storage for one block is:

```text
2 x block_tokens x KV_heads x head_dimension x bytes_per_element
```

The factor `2` represents keys and values. Multiply by the number of layers to
estimate the full-model block size. GQA reduces the number of KV heads, while a
smaller KV dtype reduces bytes per element.

---

## 5. Forward Pass, PagedAttention, and Continuous Batching

After scheduling, the model runner constructs GPU inputs for the selected
tokens. Requests of different lengths are flattened into a combined token
layout. Position information, block tables, and attention metadata keep the
sequences logically separate.

![Continuous batching and paged attention during a forward pass](https://www.aleksagordic.com/blog/vllm/fwd_pass.png)

*Diagram: [Forward pass with continuous batching and PagedAttention](https://www.aleksagordic.com/blog/vllm/fwd_pass.png), by Aleksa Gordic.*

### How to read the diagram

The illustration follows three independent requests through prefill and their
first decode step. It uses a KV block size of four token positions.

#### 1. Tokenize each prompt

The prompts become three token sequences:

```text
Request A: [1, 2, 3, 4, 5]          -> 5 tokens
Request B: [1, 6, 5, 7, 8, 9, 10]   -> 7 tokens
Request C: [1, 12, 13]               -> 3 tokens
```

The repeated `1` is the simplified beginning-of-sequence token in this
example. The other numbers are token IDs; their values are unrelated to the
block size.

#### 2. Allocate physical KV blocks

Because each block has four slots, the requests need `ceil(tokens / 4)` blocks:

| Request | Tokens | Blocks needed | Physical blocks in the illustration |
|---|---:|---:|---|
| A | 5 | 2 | 1 and 2 |
| B | 7 | 2 | 3 and 4 |
| C | 3 | 1 | 5 |

`block_id` identifies a physical KV block. `ref_cnt = 1` means that one
sequence currently references it. `block_hash` is metadata used for prefix
matching when prefix caching applies.

#### 3. Pack the prefill work

The model runner represents the three requests as one packed batch:

```text
input_ids = [1,2,3,4,5, 1,6,5,7,8,9,10, 1,12,13]
positions = [0,1,2,3,4, 0,1,2,3,4,5,6, 0,1,2]
```

Positions restart at zero for each request. This is a packed execution layout,
not one semantic sequence: attention metadata preserves the request boundaries,
so tokens from one request cannot attend to tokens from another.

#### 4. Map prompt tokens to KV-cache slots

With four slots per block, the first slot of physical block `b` is `b * 4` in
the numbering used by the illustration. Therefore:

| Request | Physical blocks | Slots used during prefill |
|---|---|---|
| A | 1 and 2 | `4, 5, 6, 7, 8` |
| B | 3 and 4 | `12, 13, 14, 15, 16, 17, 18` |
| C | 5 | `20, 21, 22` |

The combined mapping is:

```text
slot_mapping = [4,5,6,7,8, 12,13,14,15,16,17,18, 20,21,22]
```

For every prompt token, the attention layer computes K and V vectors and uses
this mapping to write them into paged GPU memory. After prefill, request A has
five occupied slots, B has seven, and C has three. The gray parts of the final
blocks are still empty.

#### 5. Move to the first decode step

Suppose prefill samples one continuation token for each request:

```text
Request A -> token 14
Request B -> token 15
Request C -> token 16
```

Conceptually, the updated sequences are:

```text
A: [1, 2, 3, 4, 5, 14]
B: [1, 6, 5, 7, 8, 9, 10, 15]
C: [1, 12, 13, 16]
```

Their new lengths are `seq_lens = [6, 8, 4]`. The long arrays shown in the
figure describe this updated state, but the decode forward pass does not
recompute the prompt tokens. It processes only the three new tokens:

```text
input_ids = [14, 15, 16]
positions = [5, 7, 3]
```

The old K/V vectors are reused from the cache. Each new query reads the history
of its own request, and only the new token's K/V vectors are added.

#### 6. Place the new K/V vectors

The next free slot for each request is:

| Request | New token | Physical block and offset | New KV slot |
|---|---:|---|---:|
| A | 14 | block 2, offset 1 | `2 * 4 + 1 = 9` |
| B | 15 | block 4, offset 3 | `4 * 4 + 3 = 19` |
| C | 16 | block 5, offset 3 | `5 * 4 + 3 = 23` |

For this decode work, the useful mapping is therefore `[9, 19, 23]`. The
figure's full updated mapping also includes all previously filled prompt slots.

The decode metadata explains how the packed work remains separate:

- `query_start_loc = [0, 1, 2, 3]` marks three query segments, each containing
  one new token;
- `seq_lens = [6, 8, 4]` gives the history length visible to each new query;
- `num_actual_tokens = 3` says that this forward pass computes three new token
  positions in total.

After the pass, A has six cached positions, B has eight, and C has four. The
engine samples the next token for each unfinished request and repeats the decode
process.

### Two concepts that are easy to confuse

| Concept | What it changes |
|---|---|
| Continuous batching | Which requests participate in each engine step |
| PagedAttention | How their KV states are allocated and addressed in memory |

They complement each other. Continuous batching keeps useful work flowing into
the GPU; PagedAttention makes the changing group of variable-length requests
memory-manageable.

---

## 6. Chunked Prefill

A single long prompt can consume most or all of a step's token budget. That
delays decode work for existing users and short prompts waiting behind it.
Chunked prefill splits the long prompt across multiple scheduling steps.

![Long prompt split into multiple chunked-prefill steps](https://www.aleksagordic.com/blog/vllm/chunked_pt1.png)

*Diagram: [Chunked prefill](https://www.aleksagordic.com/blog/vllm/chunked_pt1.png), by Aleksa Gordic.*

### How to read the diagram

- The long prompt is divided into token chunks.
- Only the chunks that fit the current budget are scheduled.
- Decode tokens from active requests can share intervening steps.
- The first output token is produced only after the prompt's final chunk has
  been processed.

Chunking therefore trades one kind of latency for another:

| Benefit | Cost |
|---|---|
| Prevents one long prompt from monopolizing a step | The long prompt may finish prefill later |
| Reduces ITL spikes for active decoders | More scheduling boundaries and metadata work |
| Improves fairness under mixed prompt lengths | Earlier KV blocks may be read across several chunks |

The useful tuning variable is not merely “chunking on or off.” It is the token
budget per step relative to prompt lengths, decode concurrency, and the desired
TTFT/ITL SLOs.

---

## 7. Automatic Prefix Caching

Many workloads repeat large prompt prefixes: system messages, tool schemas,
few-shot examples, or shared document context. Prefix caching reuses already
computed KV blocks instead of prefilling identical blocks again.

### Stage 1: hash complete prefix blocks

![Prefix tokens divided into hashable cache blocks](https://www.aleksagordic.com/blog/vllm/prefix_pt1.png)

*Diagram: [Prefix caching, stage 1](https://www.aleksagordic.com/blog/vllm/prefix_pt1.png), by Aleksa Gordic.*

Tokens are divided into complete blocks. A block hash includes the block's
tokens and the hash of its preceding prefix, so identical text in a different
prefix context does not accidentally refer to the wrong KV state. Optional
metadata can isolate caches across modalities, adapters, or tenants.

### Stage 2: associate hashes with computed blocks

![Computed prefix hashes associated with allocated KV blocks](https://www.aleksagordic.com/blog/vllm/prefix_pt2.png)

*Diagram: [Prefix caching, stage 2](https://www.aleksagordic.com/blog/vllm/prefix_pt2.png), by Aleksa Gordic.*

The first request receives normal KV blocks. Once the forward pass computes
their contents, the cache manager can associate the complete prefix-block
hashes with those physical blocks.

### Stage 3: reuse the longest valid cached prefix

![A later request reuses matching prefix blocks](https://www.aleksagordic.com/blog/vllm/prefix_pt3.png)

*Diagram: [Prefix caching, stage 3](https://www.aleksagordic.com/blog/vllm/prefix_pt3.png), by Aleksa Gordic.*

A later request hashes its own prefix and looks for the longest sequence of
matching cached blocks. It reuses those blocks and computes only the unmatched
suffix.

### Reading the two-request example

The illustration uses four token positions per KV block. Both requests begin
with the same eight-token prefix but have different three-token suffixes:

```text
Request A: [shared prefix: 8] + [A suffix: 3] = 11 prompt tokens
Request B: [shared prefix: 8] + [B suffix: 3] = 11 prompt tokens
```

They branch after the shared prefix; Request B does not append its suffix after
Request A's suffix:

```text
                         +-- A1 A2 A3   Request A
S1 S2 S3 S4 S5 S6 S7 S8 |
                         +-- B1 B2 B3   Request B
```

During Request A's prefill, its eleven prompt positions occupy two full blocks
and three slots of a third block:

```text
block 1: [S1 S2 S3 S4]   full
block 2: [S5 S6 S7 S8]   full
block 3: [A1 A2 A3 --]   75% full

11 positions / 4 positions per block = 2.75 blocks
```

Request A then continues decoding. In the diagram it eventually has seventeen
processed positions, including generated-token positions, so its state grows
to four full blocks plus one slot of block 5:

```text
17 positions / 4 positions per block = 4.25 blocks
```

Those additional six positions are generated decode tokens. They are not a
third part of either request's prompt.

When Request B arrives, only blocks 1 and 2 match its prefix. Its block table
points directly to those existing physical blocks, avoiding computation for
the first eight positions. Request A's suffix and generated continuation in
blocks 3 through 5 do not belong to Request B and cannot be reused. Request B's
three new suffix positions are written to a newly allocated physical block 6:

```text
Request B block table: [1, 2, 6]

blocks 1 and 2: 8 cached prefix positions
block 6:         3 newly computed suffix positions, 75% full
```

The choice of physical block 6 does not mean blocks 3 through 5 belong to
Request B. The illustration assumes block 6 is at the front of the allocator's
free-block queue; physical IDs do not need to be consecutive or allocated in
numeric order.

The expression `8 + 3 + 3` counts prompt computation across both requests:

```text
shared prefix computed once:       8
Request A's private suffix:       +3
Request B's private suffix:       +3
                                  --
total prompt positions computed:  14
```

Without prefix caching, both eleven-token prompts would be computed in full,
for `11 + 11 = 22` prompt-position computations. For Request B alone, the
logical prompt length remains `8 + 3 = 11`; it never becomes `8 + 3 + 3`.

### What prefix caching does and does not improve

Prefix caching can reduce:

- prefill GPU work;
- TTFT for repeated long prefixes;
- temporary pressure created by redundant prompt computation.

It does not directly accelerate decode after the prompts diverge. It also needs
cache locality: a request routed to a replica that does not hold the prefix will
miss even if another replica has it.

Only complete blocks are reusable. A non-aligned final fragment normally has
to be recomputed because its KV block can later contain different continuation
tokens.

---

## 8. Guided Decoding

Guided decoding restricts the next-token choices so generated text follows a
schema, regular expression, grammar, or fixed set of choices.

![Finite-state machine constraining Positive or Negative output](https://www.aleksagordic.com/blog/vllm/fsm.png)

*Diagram: [Guided-decoding finite-state machine](https://www.aleksagordic.com/blog/vllm/fsm.png), by Aleksa Gordic.*

### How to read the finite-state machine

Each circle is a generation state. Each edge is a token or character allowed
from that state. If the current state allows only `P` or `N`, every other token
must be made impossible before sampling. After one is selected, the state
advances and a new allowed set is produced.

![Grammar bitmask expanded and applied to model logits](https://www.aleksagordic.com/blog/vllm/fsm2.png)

*Diagram: [Guided-decoding logit mask](https://www.aleksagordic.com/blog/vllm/fsm2.png), by Aleksa Gordic.*

### Runtime sequence

1. Compile the requested grammar into an efficient state machine.
2. Determine the token IDs allowed by the current state.
3. Represent that allowed set as a compact bitmask.
4. Expand or apply the mask to the vocabulary logits.
5. Set forbidden-token logits to negative infinity.
6. Sample from the remaining tokens.
7. Advance the grammar state and repeat.

The model still supplies probabilities among valid choices. Guided decoding
does not teach the model facts; it constrains the shape of its output.

---

## 9. Speculative Decoding

Ordinary autoregressive decoding obtains one token from each target-model step.
Speculative decoding tries to obtain several by proposing tokens cheaply and
checking them in parallel with the target model.

### Draft stage

![A fast proposer drafts several candidate tokens](https://www.aleksagordic.com/blog/vllm/specdec_pt1.png)

*Diagram: [Speculative decoding draft stage](https://www.aleksagordic.com/blog/vllm/specdec_pt1.png), by Aleksa Gordic.*

A proposer generates `k` likely continuation tokens. The proposer may be an
n-gram lookup, a learned multi-token prediction head, or another supported
method. Its purpose is speed, not final authority.

### Verify and accept stage

![The target model verifies draft tokens and rejection sampling chooses output](https://www.aleksagordic.com/blog/vllm/specdec_pt2.png)

*Diagram: [Speculative decoding verification](https://www.aleksagordic.com/blog/vllm/specdec_pt2.png), by Aleksa Gordic.*

The target model evaluates the drafted positions in one pass. An acceptance
procedure scans from left to right:

- accepted draft tokens become output without separate target-model steps;
- verification stops at the first rejection;
- a replacement token is sampled from an adjusted target distribution;
- if all drafts are accepted, one additional target token may already be
  available from the verification pass.

### When it helps

Speculative decoding is useful when:

- draft generation is much cheaper than target decoding;
- the acceptance rate is high;
- verification kernels use the added parallel work efficiently;
- the extra cache and bookkeeping do not dominate.

It can hurt when draft acceptance is low or ordinary decode is already highly
batched. Always compare TPOT and throughput with representative prompts rather
than assuming a larger speculative-token count is better.

---

## 10. Disaggregated Prefill and Decode

Prefill and decode compete when they share a GPU even though their resource
profiles differ. Disaggregation assigns them to separate worker pools and
transfers the prompt's KV state between those pools.

![Separate prefill and decode workers exchange KV-cache state](https://www.aleksagordic.com/blog/vllm/pd.png)

*Diagram: [Disaggregated prefill/decode](https://www.aleksagordic.com/blog/vllm/pd.png), by Aleksa Gordic.*

### How to read the diagram

1. A request is sent to a prefill worker.
2. That worker processes the prompt and produces the first-token state.
3. Its KV blocks are exported through a connector.
4. A decode worker imports the KV blocks into its own paged cache.
5. Decode continues locally and streams subsequent tokens.

The transfer can use shared storage for demonstration, but production systems
typically need a high-bandwidth path such as a purpose-built KV connector over
fast host or accelerator networking.

### Why separate the phases?

| Goal | Prefill pool | Decode pool |
|---|---|---|
| Main SLO | TTFT | ITL / TPOT |
| Typical work | Many prompt tokens | One or several new tokens per sequence |
| Common bottleneck | Compute at sufficient prompt size | HBM bandwidth at modest batches |
| Scaling signal | Waiting prompt tokens | Active sequences and decode backlog |

### The cost

Disaggregation removes direct prefill/decode interference but introduces:

- KV-transfer latency and fabric bandwidth consumption;
- routing and ownership coordination;
- more components and failure modes;
- the need to size prefill and decode pools independently;
- cache cleanup when one side fails or a request is cancelled.

The transfer must be faster than the interference it removes. This is an
architectural choice to benchmark, not a universal upgrade.

---

## 11. Scaling a Model Across GPUs

There are two fundamentally different reasons to use more GPUs:

1. **The model does not fit or one model step is too slow.** Split one model
   replica across GPUs with tensor or pipeline parallelism.
2. **One replica cannot serve enough independent work.** Create more replicas
   with data parallelism.

| Strategy | What is split or copied | Main use | Main communication |
|---|---|---|---|
| Tensor parallelism (TP) | Tensors within layers | Fast multi-GPU model execution, usually within a node | Collectives during many layers |
| Pipeline parallelism (PP) | Consecutive layer groups | Model spans devices or nodes | Activations between stages |
| Data parallelism (DP) | Whole model replica | More independent batches | Routing and limited coordination |
| Expert parallelism (EP) | MoE experts | Distribute expert weights and work | Token all-to-all communication |

### Multi-process execution

![MultiProcExecutor coordinating eight tensor-parallel workers](https://www.aleksagordic.com/blog/vllm/multiprocexecutor.png)

*Diagram: [MultiProcExecutor with TP=8](https://www.aleksagordic.com/blog/vllm/multiprocexecutor.png), by Aleksa Gordic.*

### How to read the diagram

- The executor creates one worker process per GPU rank.
- Each worker loads only the model shard assigned to that rank.
- A broadcast path sends the same execution command to all participating
  workers.
- Workers run their local portion and synchronize through the configured
  distributed collectives.
- The designated output rank returns the step result to the engine core.

The engine core still calls a model-executor interface. Whether the executor
uses one worker or many is hidden behind that boundary.

### Choosing TP and PP

Prefer keeping communication on the fastest available fabric. TP frequently
communicates within layers, so it benefits strongly from NVLink-class bandwidth.
PP communicates less often but introduces stage boundaries and pipeline
balancing concerns. A common multi-node layout uses TP within each node and PP
across nodes when the model cannot otherwise fit.

---

## 12. Scaling Replicas Across Nodes

Data parallelism creates several engine cores, each with its own scheduler and
KV cache. Requests must be routed to a replica, while every replica may itself
use TP or PP.

![Four TP=4 data-parallel engines placed across two eight-GPU nodes](https://www.aleksagordic.com/blog/vllm/server_setup.png)

*Diagram: [Two-node server configuration](https://www.aleksagordic.com/blog/vllm/server_setup.png), by Aleksa Gordic.*

### How to read the layout

The example has:

- two nodes with eight GPUs each;
- four data-parallel model replicas;
- four GPUs per replica because `TP=4`;
- two replicas on each node;
- an API/frontend role on one node and headless engine roles on the other.

The arithmetic is:

```text
total GPU workers = DP x TP x PP
                  = 4 x 4 x 1
                  = 16 GPUs
```

Current vLLM supports multi-node DP deployments with node-local DP ranks and a
shared frontend configuration. Exact launch flags and supported load-balancing
modes should be taken from the version-matched data-parallel documentation.

---

## 13. Engine Processes Behind the API

A production server separates HTTP work, engine coordination, and GPU work into
processes so slow tokenization or network clients do not directly block model
execution.

![Four data-parallel engine core processes and their internal threads](https://www.aleksagordic.com/blog/vllm/dpenginecoreproc.png)

*Diagram: [Distributed DPEngineCoreProc layout](https://www.aleksagordic.com/blog/vllm/dpenginecoreproc.png), by Aleksa Gordic.*

### How to read the diagram

Each data-parallel rank owns an engine-core process. In the article's snapshot,
that process has three steady-state activities:

- an **input path** receives and deserializes requests;
- the **main engine loop** schedules work and drives the executor;
- an **output path** returns intermediate or final results.

The frontend communicates with engine cores using message queues and sockets.
A data-parallel coordinator exchanges load information and, for configurations
that require synchronized participation, coordinates progress across ranks.

### End-to-end request path

Read the whole server as the following chain:

```text
Client
  -> HTTP API server
  -> input validation and tokenization
  -> replica selection
  -> engine-core input channel
  -> scheduler
  -> model executor
  -> GPU workers
  -> engine-core output channel
  -> asynchronous output handling
  -> streamed HTTP response
```

The API server is concurrent: many request coroutines can be waiting for engine
output without requiring one operating-system thread per client. GPU execution,
however, occurs in worker processes and is parallel hardware work rather than
Python `asyncio` concurrency.

### Routing matters

The least-busy replica is not always the best replica. Each DP rank has its own
KV cache. A router may need to balance:

- waiting and running request counts;
- estimated prompt and output sizes;
- free KV-cache capacity;
- prefix-cache locality;
- adapter or model availability;
- SLO class and request priority.

Queue-aware routing prevents obvious overload. Cache-aware routing can also
avoid recomputing a large shared prefix on another replica.

---

## 14. Latency Metrics

Inference latency is not one number. Different phases affect different parts
of what a user experiences.

![Timeline showing TTFT, ITL, and end-to-end latency](https://www.aleksagordic.com/blog/vllm/latency_diagram.png)

*Diagram: [TTFT, ITL, and end-to-end latency](https://www.aleksagordic.com/blog/vllm/latency_diagram.png), by Aleksa Gordic.*

### How to read the timeline

- The request starts at submission.
- **TTFT** ends when the first output token reaches the client.
- Each gap between later streamed outputs is an **ITL** sample.
- **E2E latency** ends when the final response arrives.
- **TPOT** amortizes the decode portion over output tokens.

For ordinary one-token-at-a-time streaming:

```text
TPOT = (E2E latency - TTFT) / (output tokens - 1)

E2E latency ~= TTFT + (output tokens - 1) x TPOT
```

With speculative decoding, one streamed event can contain several tokens.
Therefore ITL, which measures gaps between stream events, and TPOT, which
amortizes time per token, can differ materially.

| Metric | What it reveals | Common causes of regression |
|---|---|---|
| TTFT | Queueing plus prompt processing | Long prompts, prefill backlog, cache misses |
| ITL | Smoothness of token delivery | Large decode batches, prefill interference, synchronization |
| TPOT | Average decode cost per output token | Bandwidth pressure, batch shape, long contexts |
| E2E | Complete user wait | TTFT, output length, TPOT, network overhead |
| Throughput | Total work completed per second | GPU utilization, batching, parallelism |
| Goodput | Work completed while satisfying SLOs | Tail latency and overload behavior |

An **SLO**, or service level objective, is an internal measurable target such
as `p99 TTFT < 800 ms` and `p99 TPOT < 60 ms/token`. Goodput excludes work that
violates those targets, so it is often more useful than raw throughput.

---

## 15. Why Batching Trades Latency for Throughput

Decode repeatedly reads a large amount of model state to produce relatively
little output. Adding sequences to a batch lets those weight reads serve more
tokens at once.

![Roofline-style relationship among batch size, latency, and throughput](https://www.aleksagordic.com/blog/vllm/roofline.png)

*Diagram: [Simplified roofline performance model](https://www.aleksagordic.com/blog/vllm/roofline.png), by Aleksa Gordic.*

### How to read the graph

The horizontal axis represents increasing batch size or work per step. The
roofline separates two operating regions:

1. **Bandwidth-bound region.** The GPU spends most of the step moving model
   weights from HBM. Adding some tokens reuses those loaded weights, so total
   throughput rises with only a small increase in step time.
2. **Compute-bound region.** The GPU's arithmetic units become saturated.
   Adding more tokens now increases work and step latency more directly, while
   throughput approaches a ceiling.

The transition is the **saturation batch**. The best production operating point
is often near this knee, not at the largest possible batch.

### Arithmetic-intensity view

The roofline model compares:

```text
arithmetic intensity = floating-point operations / bytes moved

achievable performance
  = min(peak compute,
        memory bandwidth x arithmetic intensity)
```

Prefill has many tokens and larger matrix multiplications, so its arithmetic
intensity can be high. Small-batch decode performs limited arithmetic after
loading the weights, so it is commonly bandwidth-bound.

Real kernels make the curve less tidy. Kernel selection, quantization, context
length, KV traffic, attention backends, and CUDA-graph coverage can all shift
the knee.

---

## 16. Benchmarking vLLM Correctly

Current vLLM releases expose several benchmark modes:

| Command | Question answered |
|---|---|
| `vllm bench latency` | How long does a controlled offline batch take? |
| `vllm bench throughput` | What is maximum offline processing throughput? |
| `vllm bench serve` | How does a live endpoint behave under an arrival pattern? |
| `vllm bench startup` | How long does initialization take? |
| `vllm bench sweep` | How do results change across a parameter grid? |

A controlled latency example is:

```bash
vllm bench latency \
  --model <model-name> \
  --input-len 32 \
  --output-len 128 \
  --batch-size 8
```

For an online serving benchmark, vary at least:

- request arrival rate;
- maximum concurrency;
- prompt-length distribution;
- output-length distribution;
- repeated-prefix frequency;
- model and quantization;
- SLO thresholds;
- warmup duration and measurement duration.

### A defensible experiment sequence

1. **Freeze the workload.** Keep prompt/output distributions, dataset, sampling
   settings, and random seed stable.
2. **Warm the engine.** Exclude model loading, compilation, and first-use kernel
   costs unless startup behavior is the subject of the test.
3. **Establish low-load latency.** Measure TTFT and TPOT without queueing.
4. **Increase offered load.** Sweep request rate or concurrency until throughput
   plateaus and tail latency rises.
5. **Locate the knee.** Find the highest load that still meets the SLOs.
6. **Change one knob.** Compare batching, cache, parallelism, or speculative
   settings without changing the workload.
7. **Report distributions.** Include p50, p95, and p99, not only averages.
8. **Record failures.** Timeouts, rejected requests, preemptions, and OOMs are
   part of the result.

### Avoid misleading comparisons

- Repeating the same prompts can warm the prefix cache and inflate later runs.
- Infinite-rate throughput tests do not represent an interactive workload.
- Equal request counts are not equal work when token lengths differ.
- A higher token/s number is not an improvement if TTFT or TPOT violates the
  product SLO.
- Comparing vLLM versions without recording defaults can silently compare
  different schedulers, kernels, or graph modes.

---

## 17. Symptom-to-Subsystem Map

| Symptom | First places to investigate |
|---|---|
| High TTFT at low load | Tokenization, prompt length, cold prefix cache, prefill kernels |
| High TTFT only under load | Waiting queue, token budget, long-prefill interference, admission control |
| High or uneven ITL | Decode batch size, mixed prefill, synchronization, network streaming |
| Frequent preemption | KV capacity, context lengths, `max_num_seqs`, block pressure |
| Good cache hit rate but little speedup | Prefixes are short, decode dominates, or routing breaks locality |
| Speculative decoding reduces throughput | Low acceptance, costly proposer, or already-efficient large batches |
| More GPUs do not reduce latency | Communication dominates or parallel groups cross slow links |
| High raw throughput but poor goodput | Operating beyond the latency knee |
| One DP rank is overloaded | Load-balancing signal is stale or routing is overly sticky |
| P/D split underperforms | KV-transfer cost exceeds removed interference |

---

## 18. What to Master

To reason confidently about vLLM, understand these concepts in order:

1. **Autoregressive inference:** prefill once, then decode repeatedly.
2. **GPU memory:** weights, activations, graphs, buffers, and KV cache share VRAM.
3. **KV-cache growth:** active sequence length consumes memory over time.
4. **PagedAttention:** logical token history maps to non-contiguous physical blocks.
5. **Continuous batching:** the active batch changes after every engine step.
6. **Token-budget scheduling:** work is admitted by tokens and cache capacity,
   not only by request count.
7. **Latency metrics:** TTFT, ITL, TPOT, E2E, throughput, and goodput describe
   different behavior.
8. **Advanced optimizations:** chunked prefill, prefix caching, guided decoding,
   and speculative decoding solve different problems.
9. **Parallelism:** TP and PP split one replica; DP creates more replicas; EP
   distributes MoE experts.
10. **Distributed serving:** API processes, engine cores, GPU workers, routing,
    cache locality, and failure handling form one system.

The unifying idea is simple: vLLM keeps the GPU productive by deciding which
tokens to compute, ensuring their state fits in memory, and reorganizing the
work as requests arrive and finish.

---

## References

- [Inside vLLM: Anatomy of a High-Throughput LLM Inference System](https://www.aleksagordic.com/blog/vllm)
- [vLLM documentation](https://docs.vllm.ai/en/latest/)
- [vLLM V1 process architecture](https://docs.vllm.ai/en/latest/design/arch_overview.html)
- [Automatic Prefix Caching](https://docs.vllm.ai/en/latest/design/prefix_caching/)
- [Data Parallel Deployment](https://docs.vllm.ai/en/latest/serving/data_parallel_deployment/)
- [Disaggregated Serving](https://docs.vllm.ai/en/latest/examples/disaggregated/disaggregated_serving/)
- [Benchmark CLI](https://docs.vllm.ai/en/latest/benchmarking/cli/)
- [PagedAttention paper](https://arxiv.org/abs/2309.06180)
- [Orca continuous batching paper](https://www.usenix.org/conference/osdi22/presentation/yu)
- [Speculative sampling paper](https://arxiv.org/abs/2302.01318)
