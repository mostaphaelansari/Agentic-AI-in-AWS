# AI Infrastructure for LLM Inference: GPUs, vLLM, llm-d and Kubernetes

## 1. Introduction

Running large language models in production is not only a machine-learning problem. It is also a hardware, memory-management and distributed-systems problem.

A production LLM stack has to solve several challenges at once:

* fit large model weights into accelerator memory;
* process prompts efficiently;
* generate tokens with low latency;
* serve many users on the same GPUs;
* manage KV-cache memory;
* scale models across multiple GPUs;
* route requests intelligently across multiple model servers;
* orchestrate the complete infrastructure reliably.

A useful way to understand the stack is as a set of layers, each solving a
different class of problem:

```mermaid
flowchart TB
    APP[Applications] --> API[OpenAI-compatible API]
    API --> ROUTE[llm-d<br/>fleet routing]
    ROUTE --> SERVE[vLLM<br/>scheduling, batching, KV cache]
    SERVE --> RUNTIME[PyTorch / accelerator runtime]
    RUNTIME --> GPU[GPUs<br/>compute, memory, bandwidth]
    K8S[Kubernetes<br/>placement, scaling, recovery] -. manages .-> ROUTE
    K8S -. manages .-> SERVE
    K8S -. schedules .-> GPU

    classDef app fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef inference fill:#e8fff3,stroke:#059669,color:#052e16
    classDef hardware fill:#fff4db,stroke:#d97706,color:#451a03
    classDef control fill:#f3e8ff,stroke:#9333ea,color:#3b0764
    class APP,API app
    class ROUTE,SERVE,RUNTIME inference
    class GPU hardware
    class K8S control
```

Each new layer appears because the previous layer alone is insufficient once the system grows.

---

# 2. An LLM Is Fundamentally a Large Collection of Weights

A large language model consists of a model architecture and a very large collection of numerical parameters, or weights.

These weights are stored as files and must be loaded into memory during inference.

A simplified execution flow is:

```mermaid
flowchart LR
    FILES[(Model files)] -->|load once| HBM[GPU memory<br/>resident weights]
    TOKENS[Input tokens] --> OPS[Transformer operations]
    HBM -->|read on every pass| OPS
    OPS --> NEXT[Next-token probabilities]
    NEXT -->|sample one token| TOKENS

    classDef storage fill:#f3f4f6,stroke:#4b5563,color:#111827
    classDef gpu fill:#fff4db,stroke:#d97706,color:#451a03
    classDef data fill:#e8f1ff,stroke:#2563eb,color:#172554
    class FILES storage
    class HBM,OPS gpu
    class TOKENS,NEXT data
```

Large models contain billions of parameters, which creates two immediate infrastructure requirements:

1. enough memory to store the model;
2. enough computational capacity to execute the required matrix operations.

For example, a 70-billion-parameter model requires a very large amount of memory even before accounting for runtime overhead and KV cache.

This makes GPU memory one of the first constraints in LLM infrastructure.

---

# 3. Why GPUs Are Used

Transformer inference involves a huge number of mathematical operations that can be executed in parallel.

GPUs are well suited to this workload because they contain very large numbers of parallel processing units.

A CPU is optimized for relatively few powerful cores, while a GPU provides
many execution units suited to parallel matrix operations:

```mermaid
flowchart LR
    subgraph CPU[CPU: a few general-purpose cores]
        C1[Core] --> T1[Task A]
        C2[Core] --> T2[Task B]
        C3[Core] --> T3[Task C]
    end

    subgraph GPU[GPU: many parallel execution units]
        M1[Matrix tile 1]
        M2[Matrix tile 2]
        M3[Matrix tile 3]
        M4[Matrix tile 4]
        M5[Matrix tile 5]
        M6[Matrix tile 6]
        M1 & M2 & M3 & M4 & M5 & M6 --> MAT[Parallel matrix operation]
    end

    classDef cpu fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef gpu fill:#fff4db,stroke:#d97706,color:#451a03
    class C1,C2,C3,T1,T2,T3 cpu
    class M1,M2,M3,M4,M5,M6,MAT gpu
```

However, raw computational capability is not enough to evaluate an accelerator for LLM inference.

Three hardware characteristics are particularly important.

```mermaid
flowchart TB
    WORKLOAD[LLM inference workload]
    CAP[Memory capacity<br/>Can the model and KV cache fit?]
    BW[Memory bandwidth<br/>How quickly can weights and state move?]
    COMP[Compute<br/>How quickly can parallel math run?]
    RESULT[Usable throughput and latency]

    WORKLOAD --> CAP
    WORKLOAD --> BW
    WORKLOAD --> COMP
    CAP --> RESULT
    BW --> RESULT
    COMP --> RESULT

    classDef input fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef factor fill:#fff4db,stroke:#d97706,color:#451a03
    classDef result fill:#e8fff3,stroke:#059669,color:#052e16
    class WORKLOAD input
    class CAP,BW,COMP factor
    class RESULT result
```

---

# 4. The Three Core GPU Characteristics

## 4.1 Memory Capacity

The first question is:

> How much VRAM or HBM does the accelerator have?

Memory capacity determines:

* whether a model can fit;
* which precision or quantization can be used;
* how much memory remains for runtime state;
* how much KV cache can be stored;
* how many simultaneous sequences can be active.

At a simplified level:

$$
GPU\ Memory =
Model\ Weights +
KV\ Cache +
Runtime\ Overhead
$$

This makes memory capacity directly related to both model compatibility and concurrency.

---

## 4.2 Memory Bandwidth

Memory bandwidth describes how quickly data can be moved from GPU memory to the compute units.

This is particularly important during token generation.

The model weights remain resident in GPU memory, but generating each new output token requires repeatedly reading large portions of those weights.

As a result, decode performance is often strongly influenced by:

> **memory bandwidth**

rather than only peak compute.

---

## 4.3 Compute Performance

Compute capability is often expressed using metrics such as:

* TFLOPS;
* BF16 performance;
* FP16 performance;
* FP8 performance;
* FP4 performance.

Compute becomes particularly important when processing large input prompts because many prompt tokens can be processed in parallel.

Therefore, a useful GPU comparison should always consider:

> **memory capacity + memory bandwidth + compute**

rather than only theoretical TFLOPS.

---

# 5. Why PyTorch Alone Is Not Enough for Production Serving

At the simplest level, a model can be loaded using PyTorch and executed directly on a GPU.

```text
Model
  ↓
PyTorch
  ↓
GPU
  ↓
Inference
```

This is sufficient for experimentation and development.

Production systems introduce a different challenge:

```text
User 1
User 2
User 3
User 4
...
User N
   ↓
same model
   ↓
same GPU infrastructure
```

The serving layer must now manage:

* many simultaneous requests;
* request queues;
* streaming;
* dynamic batching;
* KV-cache allocation;
* GPU memory;
* scheduling;
* throughput;
* latency.

This creates the need for a dedicated LLM model server.

---

# 6. vLLM as the Model-Serving Layer

vLLM is designed to serve large language models efficiently on accelerators.

It sits between the application and the underlying PyTorch/GPU execution stack.

Conceptually, vLLM turns low-level model execution into a multi-user serving
system:

```mermaid
flowchart TB
    APP[Application] -->|HTTP / streaming| API[OpenAI-compatible API]
    API --> VLLM

    subgraph VLLM[vLLM model server]
        QUEUE[Request queue] --> SCHED[Scheduler]
        SCHED --> BATCH[Continuous batching]
        BATCH --> CACHE[KV-cache manager]
    end

    CACHE --> RUNTIME[PyTorch / GPU runtime]
    RUNTIME --> GPU[GPU]
    GPU -->|generated token stream| APP

    classDef app fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef serve fill:#e8fff3,stroke:#059669,color:#052e16
    classDef gpu fill:#fff4db,stroke:#d97706,color:#451a03
    class APP,API app
    class QUEUE,SCHED,BATCH,CACHE serve
    class RUNTIME,GPU gpu
```

vLLM provides functionality such as:

* persistent model loading;
* request scheduling;
* efficient batching;
* KV-cache management;
* streaming output;
* OpenAI-compatible serving APIs.

Its purpose is not merely to execute a model, but to use expensive accelerator resources efficiently while handling many simultaneous sequences.

---

# 7. Token Generation Creates Two Different Workloads

Large language models generate text autoregressively.

They do not generate the entire response at once.

Instead:

```text
Token 1
   ↓
Token 2
   ↓
Token 3
   ↓
Token 4
   ↓
...
```

This creates two very different phases of inference:

1. **Prefill**
2. **Decode**

These phases stress hardware differently.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Queue as Queue / scheduler
    participant GPU as GPU model server
    participant KV as KV cache

    User->>Queue: Prompt tokens
    Queue->>GPU: Schedule request
    Note over GPU: Prefill: process prompt in parallel<br/>Mostly compute intensive
    GPU->>KV: Store attention keys and values
    GPU-->>User: First output token (TTFT)

    loop One step per output token
        GPU->>KV: Read previous attention state
        Note over GPU: Decode: read weights and generate<br/>Mostly bandwidth intensive
        GPU->>KV: Append new token state
        GPU-->>User: Stream next token (TPOT)
    end
```

---

# 8. Prefill

During prefill, the model processes the full input prompt.

The prompt may contain:

```text
System prompt
+
Conversation history
+
RAG context
+
Documents
+
User question
```

Many input tokens can be processed in parallel.

Prefill is therefore generally more:

> **compute intensive**

than decode.

At the end of prefill, the model produces the first output token.

This corresponds to the main user-visible metric:

## TTFT — Time to First Token

$$
TTFT =
Time\ of\ first\ generated\ token
-
Request\ start
$$

TTFT includes more than pure prefill compute in a production system. It can also include:

* queue time;
* routing time;
* scheduling;
* network latency.

But prompt processing is a major component.

Long prompts therefore tend to increase TTFT.

---

# 9. Decode

After the first token is generated, the model enters the decode phase.

The output is produced sequentially:

```text
token 1
→ token 2
→ token 3
→ token 4
→ ...
```

Each generation step requires another pass through the model.

The model weights do not need to be reloaded from storage; they remain in GPU memory.

However, the GPU must repeatedly access those model weights from HBM or VRAM.

For a 200-token response, the model performs roughly 200 sequential generation steps.

This is why decode can become strongly:

> **memory-bandwidth bound**

The key metric is:

## TPOT — Time Per Output Token

A TPOT of:

$$
20ms
$$

corresponds approximately to:

$$
1000/20 = 50
$$

or around:

> **50 generated tokens per second**

The distinction can therefore be summarized as:

| Phase   | Main activity          | Main hardware pressure | Main UX metric |
| ------- | ---------------------- | ---------------------- | -------------- |
| Prefill | Process prompt         | Compute                | TTFT           |
| Decode  | Generate output tokens | Memory bandwidth       | TPOT           |

Initially both phases normally run on the same GPU resources.

```mermaid
flowchart LR
    PROMPT[Prompt<br/>many input tokens] --> PREFILL[Prefill<br/>parallel processing]
    PREFILL -->|first token| DECODE[Decode<br/>sequential generation]
    DECODE --> OUTPUT[Completed response]

    PREFILL -. primary pressure .-> COMPUTE[Compute]
    DECODE -. primary pressure .-> BANDWIDTH[Memory bandwidth]
    PREFILL -. measured by .-> TTFT[TTFT]
    DECODE -. measured by .-> TPOT[TPOT]

    classDef data fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef phase fill:#e8fff3,stroke:#059669,color:#052e16
    classDef pressure fill:#fff4db,stroke:#d97706,color:#451a03
    classDef metric fill:#f3e8ff,stroke:#9333ea,color:#3b0764
    class PROMPT,OUTPUT data
    class PREFILL,DECODE phase
    class COMPUTE,BANDWIDTH pressure
    class TTFT,TPOT metric
```

---

# 10. Why KV Cache Is Necessary

Without caching, each newly generated token would require recomputing information about all previous input and output tokens.

That would be extremely inefficient.

During transformer execution, the model calculates Key and Value representations for attention.

These representations can be saved.

The saved state is called:

> **KV cache**

Conceptually, the cache is reused and extended on every decode step:

```mermaid
flowchart LR
    PROMPT[Prompt tokens] --> PREFILL[Prefill]
    PREFILL --> KV0[(KV cache<br/>prompt state)]
    KV0 --> T1[Generate token 1]
    T1 --> KV1[(KV cache<br/>prompt + token 1)]
    KV1 --> T2[Generate token 2]
    T2 --> KV2[(KV cache<br/>prompt + tokens 1-2)]
    KV2 --> MORE[Continue decoding]

    classDef phase fill:#e8fff3,stroke:#059669,color:#052e16
    classDef cache fill:#fff4db,stroke:#d97706,color:#451a03
    classDef token fill:#e8f1ff,stroke:#2563eb,color:#172554
    class PREFILL,T1,T2,MORE phase
    class KV0,KV1,KV2 cache
    class PROMPT token
```

The KV cache eliminates large amounts of repeated computation.

But it introduces another constraint:

> **KV cache consumes GPU memory.**

The cache is created during prefill and grows as additional output tokens are generated.

---

# 11. KV Cache Connects Context Length to Concurrency

Longer contexts require larger KV caches.

Therefore:

```mermaid
flowchart LR
    A[Longer prompt or output] --> B[Larger KV cache per request]
    B --> C[More GPU memory consumed]
    C --> D[Fewer simultaneous sequences]
    D --> E[Lower concurrency ceiling]

    classDef cause fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef memory fill:#fff4db,stroke:#d97706,color:#451a03
    classDef impact fill:#feecec,stroke:#dc2626,color:#450a0a
    class A cause
    class B,C memory
    class D,E impact
```

This creates an important relationship:

> **Context length ↔ GPU memory ↔ concurrent serving capacity**

For example, 100 requests with short 2K contexts are a very different workload from 100 requests with 50K contexts.

This is why “number of users” cannot be evaluated without understanding prompt and context lengths.

---

# 12. Prefix Caching

Many production requests share a common beginning.

For example:

```text
Corporate system prompt
+
security instructions
+
same large knowledge document
+
user-specific question
```

A second request may contain the same prefix but a different question.

Without prefix caching, the shared portion is processed repeatedly.

With prefix caching:

```mermaid
flowchart TB
    PREFIX[Shared prefix<br/>system prompt + common documents]
    PREFIX --> ONCE[Process once]
    ONCE --> CACHE[(Cached prefix state)]
    QA[User A question] --> RA[Request A]
    QB[User B question] --> RB[Request B]
    CACHE --> RA
    CACHE --> RB
    RA --> OA[Response A]
    RB --> OB[Response B]

    classDef shared fill:#fff4db,stroke:#d97706,color:#451a03
    classDef request fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef result fill:#e8fff3,stroke:#059669,color:#052e16
    class PREFIX,ONCE,CACHE shared
    class QA,QB,RA,RB request
    class OA,OB result
```

This can reduce repeated prefill computation and improve TTFT.

However, it introduces a distributed-inference problem:

> **cache locality matters.**

If one model server already contains the relevant cached prefix, routing the related request to another server may force the system to recompute the same work.

---

# 13. One GPU Serving Many Users

Serving requests sequentially wastes GPU parallelism.

Instead of:

```text
User A
  ↓
GPU
  ↓
finish

User B
  ↓
GPU
```

multiple active sequences can be processed together:

```mermaid
flowchart LR
    A[Sequence A] --> SCHED[Continuous batching scheduler]
    B[Sequence B] --> SCHED
    C[Sequence C] --> SCHED
    D[Sequence D] --> SCHED
    SCHED --> BATCH[One decode-step batch]
    BATCH --> GPU[GPU]
    GPU -->|one token| A
    GPU -->|one token| B
    GPU -->|one token| C
    GPU -->|one token| D

    classDef seq fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef serve fill:#e8fff3,stroke:#059669,color:#052e16
    classDef gpu fill:#fff4db,stroke:#d97706,color:#451a03
    class A,B,C,D seq
    class SCHED,BATCH serve
    class GPU gpu
```

This is batching.

Batching increases:

> **throughput**

because one model execution step can contribute to multiple users simultaneously.

This is a major reason why dedicated inference servers such as vLLM outperform simplistic one-request-at-a-time model execution.

---

# 14. Why Batching Eventually Stops Scaling

Batching improves throughput, but it cannot scale indefinitely.

Every additional active sequence consumes KV-cache memory.

A GPU's memory therefore contains something approximately like:

```mermaid
flowchart TB
    subgraph HBM[GPU HBM / VRAM]
        direction TB
        WEIGHTS[Model weights<br/>fixed while serving]
        KV1[User 1 KV cache]
        KV2[User 2 KV cache]
        KV3[User 3 KV cache]
        KVN[More active-sequence KV caches]
        WORK[Runtime workspace<br/>and temporary buffers]
        FREE[Free capacity<br/>for new requests]
        WEIGHTS ~~~ KV1
        KV1 ~~~ KV2
        KV2 ~~~ KV3
        KV3 ~~~ KVN
        KVN ~~~ WORK
        WORK ~~~ FREE
    end

    classDef weights fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef cache fill:#fff4db,stroke:#d97706,color:#451a03
    classDef runtime fill:#f3e8ff,stroke:#9333ea,color:#3b0764
    classDef free fill:#e8fff3,stroke:#059669,color:#052e16
    class WEIGHTS weights
    class KV1,KV2,KV3,KVN cache
    class WORK runtime
    class FREE free
```

Eventually the remaining memory is exhausted.

This means a GPU can become capacity constrained by memory even if it still has theoretical compute capacity available.

Therefore:

> **Memory capacity determines how many active sequences can coexist.**

This is one of the most important practical limits in LLM serving.

---

# 15. Scaling Beyond One GPU

Large models may not fit into one accelerator.

The model then needs to be distributed across several GPUs.

Conceptually, a single logical model server can shard one model over several
tightly connected GPUs:

```mermaid
flowchart TB
    VLLM[vLLM model server] --> MODEL[Logical model]
    MODEL --> S1[Shard 1]
    MODEL --> S2[Shard 2]
    MODEL --> S3[Shard 3]
    S1 --> G1[GPU 1]
    S2 --> G2[GPU 2]
    S3 --> G3[GPU 3]
    G1 <-->|NVLink / NVSwitch| G2
    G2 <-->|NVLink / NVSwitch| G3

    classDef serve fill:#e8fff3,stroke:#059669,color:#052e16
    classDef model fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef gpu fill:#fff4db,stroke:#d97706,color:#451a03
    class VLLM serve
    class MODEL,S1,S2,S3 model
    class G1,G2,G3 gpu
```

This is commonly implemented using different forms of parallelism or sharding.

Within a single server, technologies such as:

> **NVLink / NVSwitch**

provide high-bandwidth GPU-to-GPU communication.

This makes tightly coupled multi-GPU inference possible.

However, distributed execution introduces communication overhead.

The faster the model requires GPUs to exchange data, the more important the accelerator interconnect becomes.

---

# 16. Scaling Across Servers

When the model or workload extends across several physical machines, the problem becomes even more complex.

```mermaid
flowchart LR
    subgraph A[Server A]
        VA[vLLM worker A]
        A1[GPU A1]
        A2[GPU A2]
        A3[GPU A3]
        VA --> A1 & A2 & A3
    end

    subgraph B[Server B]
        VB[vLLM worker B]
        B1[GPU B1]
        B2[GPU B2]
        B3[GPU B3]
        VB --> B1 & B2 & B3
    end

    VA <-->|cluster network:<br/>state, synchronization, tensors| VB

    classDef worker fill:#e8fff3,stroke:#059669,color:#052e16
    classDef gpu fill:#fff4db,stroke:#d97706,color:#451a03
    class VA,VB worker
    class A1,A2,A3,B1,B2,B3 gpu
```

The system must now manage:

* network communication;
* model placement;
* distributed state;
* GPU topology;
* synchronization;
* routing;
* failures.

The logical model server may therefore represent multiple GPUs and multiple physical nodes.

At this point, conventional web-serving assumptions begin to break down.

---

# 17. Why Traditional Load Balancing Is Insufficient

A conventional load balancer may distribute requests using:

```text
Round robin
```

For example:

```text
Request 1 → Server A
Request 2 → Server B
Request 3 → Server C
Request 4 → Server A
```

This works reasonably well when application instances are stateless and equivalent.

LLM servers are not equivalent at every moment.

Each server may have:

* different queue depth;
* different active batches;
* different available KV-cache capacity;
* different prefixes already cached;
* different current GPU utilization;
* different memory pressure.

A routing decision therefore directly affects infrastructure efficiency.

---

# 18. Wrong Routing Can Waste Expensive GPU Capacity

Several important consequences follow.

## Wrong routing wastes GPU work

If a server already contains useful cached context but the next related request is sent somewhere else, the computation may need to be repeated.

## Memory caps the number of users

Even if compute remains available, KV-cache pressure can prevent additional requests from being admitted.

## Cached work creates locality

A follow-up request may be cheaper on a server that already contains relevant cached state.

## Every generated token repeatedly accesses model weights

Decode performance therefore remains heavily dependent on memory bandwidth.

## Model weights already consume a large part of memory

For large models there may be relatively little memory left for concurrent sequences.

These properties make routing part of the inference optimization problem rather than just a networking function.

---

# 19. llm-d as the Distributed Inference Layer

vLLM primarily solves:

> **How do we efficiently serve a model within an inference server?**

llm-d addresses the next problem:

> **How do we efficiently operate and route requests across a distributed fleet of LLM servers?**

Conceptually:

```mermaid
flowchart TB
    REQ[Incoming requests] --> GATEWAY[Gateway]
    GATEWAY --> ROUTER[llm-d inference-aware routing]
    SIGNALS[(Queue, cache, load,<br/>KV capacity signals)] --> ROUTER

    ROUTER --> A[vLLM A]
    ROUTER --> B[vLLM B]
    ROUTER --> C[vLLM C]
    A --> GA[GPU pool A]
    B --> GB[GPU pool B]
    C --> GC[GPU pool C]
    A & B & C --> SIGNALS

    classDef request fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef route fill:#f3e8ff,stroke:#9333ea,color:#3b0764
    classDef serve fill:#e8fff3,stroke:#059669,color:#052e16
    classDef gpu fill:#fff4db,stroke:#d97706,color:#451a03
    class REQ,GATEWAY request
    class ROUTER,SIGNALS route
    class A,B,C serve
    class GA,GB,GC gpu
```

llm-d provides LLM-specific orchestration and routing intelligence beyond what a conventional HTTP load balancer normally understands.

---

# 20. What an LLM-Aware Router Can Consider

Instead of only asking:

> Which server is next?

an inference-aware router can consider several signals.

### Cache locality

Does a backend already contain a relevant prefix or cached state?

### Queue depth

How much work is already waiting?

### Current load

Which model server is less saturated?

### KV-cache pressure

Does the backend have enough remaining cache capacity?

### Expected request cost

Is this a short conversational request or a very large prompt?

Routing can therefore conceptually work like:

```mermaid
flowchart TD
    REQ[Incoming request] --> PREFIX{Reusable prefix<br/>already cached?}
    PREFIX -->|Yes| LOCAL[Prefer matching backends]
    PREFIX -->|No| ALL[Consider all healthy backends]
    LOCAL --> CAP{Enough KV-cache<br/>capacity?}
    ALL --> CAP
    CAP -->|No| OTHER[Try another backend<br/>or queue request]
    CAP -->|Yes| LOAD{Queue depth and<br/>GPU load acceptable?}
    OTHER --> CAP
    LOAD -->|No| OTHER
    LOAD -->|Yes| COST[Estimate prompt and<br/>generation cost]
    COST --> SELECT[Select best backend]

    classDef decision fill:#fff4db,stroke:#d97706,color:#451a03
    classDef action fill:#e8fff3,stroke:#059669,color:#052e16
    classDef request fill:#e8f1ff,stroke:#2563eb,color:#172554
    class PREFIX,CAP,LOAD decision
    class LOCAL,ALL,OTHER,COST,SELECT action
    class REQ request
```

This can reduce unnecessary recomputation and improve effective accelerator utilization.

---

# 21. Well-Lit Paths

llm-d uses the concept of **well-lit paths** for common distributed inference architectures.

The aim is to provide tested patterns for known optimization problems rather than requiring every organization to assemble a distributed serving architecture from scratch.

Relevant examples include:

* intelligent inference routing;
* prefix-cache-aware routing;
* prefill/decode disaggregation;
* distributed inference;
* KV-cache optimization;
* multi-GPU serving.

The idea is to provide a known architectural path for recurring infrastructure problems.

---

# 22. Prefill/Decode Disaggregation

Prefill and decode have different hardware characteristics:

```text
Prefill
→ compute intensive

Decode
→ memory-bandwidth intensive
```

A distributed infrastructure can therefore separate them.

Instead of:

```text
Same GPU pool
├── prefill
└── decode
```

the architecture can become:

```mermaid
sequenceDiagram
    actor Client
    participant Router as llm-d router
    participant P as Prefill pool<br/>(compute optimized)
    participant K as KV transfer / shared tier
    participant D as Decode pool<br/>(bandwidth optimized)

    Client->>Router: Prompt
    Router->>P: Schedule prefill
    P->>P: Process all input tokens
    P->>K: Publish KV state
    K->>D: Transfer / expose KV state
    D-->>Client: First token
    loop Remaining output tokens
        D-->>Client: Next streamed token
    end
```

This makes it possible to scale the two phases independently.

For workloads with very large prompts and short answers:

> more prefill capacity may be required.

For workloads with long generated answers:

> more decode capacity may be required.

This is particularly useful when workloads have strongly different prefill/decode characteristics.

---

# 23. Why Kubernetes Becomes Necessary

Once a system contains:

* many GPU nodes;
* many model servers;
* distributed models;
* routing;
* caches;
* scaling;
* failures;
* resource allocation;

it becomes a cluster-management problem.

Kubernetes acts as the infrastructure control plane.

Conceptually:

```mermaid
flowchart TB
    K8S[Kubernetes control plane]
    K8S -->|place, scale, recover| ROUTER[llm-d routing components]
    K8S -->|schedule GPU workloads| A
    K8S -->|schedule GPU workloads| B
    K8S -->|schedule GPU workloads| C
    ROUTER --> A
    ROUTER --> B
    ROUTER --> C

    subgraph A[GPU node A]
        VA[vLLM pod A] --> GA[GPUs]
    end
    subgraph B[GPU node B]
        VB[vLLM pod B] --> GB[GPUs]
    end
    subgraph C[GPU node C]
        VC[vLLM pod C] --> GC[GPUs]
    end

    classDef control fill:#f3e8ff,stroke:#9333ea,color:#3b0764
    classDef serve fill:#e8fff3,stroke:#059669,color:#052e16
    classDef gpu fill:#fff4db,stroke:#d97706,color:#451a03
    class K8S,ROUTER control
    class VA,VB,VC serve
    class GA,GB,GC gpu
```

Kubernetes provides capabilities such as:

* scheduling;
* deployment;
* restart and recovery;
* resource allocation;
* scaling;
* lifecycle management.

The responsibilities become:

> **vLLM:** efficient inference inside the model server.

> **llm-d:** intelligent distributed inference across model servers.

> **Kubernetes:** orchestration and management of the underlying infrastructure.

---

# 24. Complete Infrastructure Progression

The overall architecture can be understood as a progression in which each
step exposes the next constraint:

```mermaid
flowchart LR
    M[Large model] -->|does not fit or run fast enough| G[GPU acceleration]
    G -->|many simultaneous users| V[vLLM serving]
    V -->|active sequences consume memory| K[KV-cache pressure]
    K -->|one GPU is insufficient| MG[Multi-GPU parallelism]
    MG -->|one server is insufficient| F[Fleet of model servers]
    F -->|backends have different state| L[llm-d routing]
    L -->|fleet needs lifecycle management| O[Kubernetes orchestration]

    classDef model fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef hardware fill:#fff4db,stroke:#d97706,color:#451a03
    classDef serve fill:#e8fff3,stroke:#059669,color:#052e16
    classDef control fill:#f3e8ff,stroke:#9333ea,color:#3b0764
    class M model
    class G,MG hardware
    class V,K,F serve
    class L,O control
```

Each layer solves a limitation created by the previous stage.

---

# 25. The Four Main Bottlenecks

The system can be summarized through four main bottlenecks.

```mermaid
flowchart TB
    WORK[Inference workload]
    WORK --> CB[Compute bottleneck<br/>prefill and parallel math]
    WORK --> BW[Bandwidth bottleneck<br/>decode and repeated weight reads]
    WORK --> MC[Capacity bottleneck<br/>weights + KV cache + runtime]
    WORK --> DS[Distributed-system bottleneck<br/>interconnect + routing + synchronization]
    CB --> SLO[Throughput and latency SLOs]
    BW --> SLO
    MC --> SLO
    DS --> SLO

    classDef workload fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef bottleneck fill:#fff4db,stroke:#d97706,color:#451a03
    classDef outcome fill:#feecec,stroke:#dc2626,color:#450a0a
    class WORK workload
    class CB,BW,MC,DS bottleneck
    class SLO outcome
```

## Compute Bottleneck

Most visible during prefill.

Relevant characteristics:

* Tensor Core performance;
* BF16/FP8/FP4 compute;
* batching efficiency.

---

## Memory-Bandwidth Bottleneck

Particularly important during decode.

Relevant characteristic:

* HBM/GDDR bandwidth.

---

## Memory-Capacity Bottleneck

Driven by:

```text
Model weights
+
KV caches
+
runtime overhead
```

Relevant characteristic:

* total accelerator memory.

---

## Distributed-System Bottleneck

Appears when scaling across:

* multiple GPUs;
* multiple nodes;
* multiple model servers.

Relevant factors:

* interconnect;
* network;
* cache locality;
* routing;
* scheduling;
* synchronization.

---

# 26. Important Metrics

The architecture naturally suggests several benchmark metrics.

## User Experience

### TTFT

Time until the first generated token.

### TPOT

Time required for each generated output token.

### End-to-end latency

Total time until the response is complete.

---

## Capacity

### Output tokens per second

Aggregate generation capacity.

### Concurrent sequences

Number of active inference sequences.

### Requests per second

Useful, but should never be interpreted without token lengths.

---

## Memory

### Model memory

Memory occupied by weights.

### KV-cache utilization

Memory consumed by active inference state.

### Prefix-cache hit rate

Percentage of reusable prefixes successfully reused.

---

## Infrastructure

### GPU utilization

Percentage of accelerator compute being used.

### GPU memory utilization

Pressure on VRAM/HBM.

### Queue depth

Number of requests waiting.

### Routing efficiency

Whether requests are sent to appropriate model servers.

---

# 27. Capacity Is More Than GPU Compute

A useful conceptual equation is:

$$
UsefulCapacity
=
f(
Compute,
MemoryCapacity,
MemoryBandwidth,
ModelSize,
ContextLength,
KVCache,
Batching,
Routing,
Caching,
Interconnect
)
$$

Therefore:

$$
UsefulCapacity \neq PeakTFLOPS
$$

A GPU with higher theoretical compute can still perform poorly for a particular LLM workload if:

* memory bandwidth is limiting;
* the model barely fits;
* KV cache is too small;
* interconnect is slow;
* batching is inefficient;
* routing destroys cache locality.

---

# 28. Implications for User Capacity

The number of users a system can support depends on more than model size.

A simplified memory limit is:

$$
AvailableMemory =
TotalGPUmemory -
ModelWeights -
RuntimeOverhead
$$

The remaining space determines possible KV-cache capacity.

Then:

$$
MemoryConcurrency
\approx
\frac{AvailableKVCapacity}
{KVRequirementPerActiveSequence}
$$

But this is only a theoretical maximum.

Production capacity must also satisfy latency targets.

Therefore:

$$
ProductionConcurrency
=
min(
MemoryLimit,
ComputeLimit,
LatencyLimit
)
$$

A system may technically fit 200 active sequences into memory but only support 80 while maintaining acceptable TTFT and TPOT.

---

# 29. Context Length Must Be Included in Capacity Planning

Two deployments with the same number of users can produce completely different load.

Example:

```text
100 users
×
2K-token context
```

is very different from:

```text
100 users
×
50K-token context
```

Therefore a capacity estimate should always include at least:

* model;
* precision;
* input-token distribution;
* output-token distribution;
* concurrency;
* request rate;
* TTFT requirement;
* TPOT requirement.

Without these variables, “number of supported users” is not a meaningful infrastructure metric.

---

# 30. Practical Benchmarking Approach

A practical benchmark derived from this architecture should progress in stages.

```mermaid
flowchart LR
    S1[1. Single GPU<br/>establish baseline] --> S2[2. Raise concurrency<br/>find saturation]
    S2 --> S3[3. Raise context length<br/>measure KV pressure]
    S3 --> S4[4. Multi-GPU<br/>measure scaling efficiency]
    S4 --> S5[5. Multiple servers<br/>measure fleet throughput]
    S5 --> S6[6. Round robin<br/>routing baseline]
    S6 --> S7[7. LLM-aware routing<br/>measure cache and load gains]
    S7 --> S8[8. Disaggregate<br/>test independent scaling]

    classDef baseline fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef scale fill:#fff4db,stroke:#d97706,color:#451a03
    classDef optimize fill:#e8fff3,stroke:#059669,color:#052e16
    class S1,S2,S3 baseline
    class S4,S5 scale
    class S6,S7,S8 optimize
```

### Stage 1 — Single Model, Single GPU

Measure:

```text
TTFT
TPOT
output tokens/sec
GPU utilization
VRAM utilization
```

### Stage 2 — Increase Concurrency

Run:

```text
1
2
4
8
16
32
64
128
...
```

Observe where:

* throughput saturates;
* KV cache fills;
* TTFT increases;
* TPOT deteriorates.

### Stage 3 — Increase Context Length

Test:

```text
2K
8K
32K
128K
```

Measure the impact on:

* TTFT;
* KV-cache use;
* concurrency.

### Stage 4 — Multi-GPU

Measure:

* scaling efficiency;
* interconnect overhead;
* throughput.

### Stage 5 — Multiple Model Servers

Establish total cluster throughput.

### Stage 6 — Conventional Routing

Use round-robin as a baseline.

### Stage 7 — LLM-Aware Routing

Introduce:

* cache awareness;
* queue awareness;
* load awareness;
* KV-capacity awareness.

### Stage 8 — Prefill/Decode Disaggregation

Evaluate whether separating the two workloads increases usable throughput or improves latency for the target workload.

---

# 31. Division of Responsibilities

| Infrastructure problem             | Main component      |
| ---------------------------------- | ------------------- |
| Load transformer/model             | PyTorch/runtime     |
| Keep model resident                | vLLM                |
| Expose inference API               | vLLM                |
| Dynamic batching                   | vLLM                |
| KV-cache management                | vLLM                |
| Prefix reuse                       | inference runtime   |
| Model distribution across GPUs     | distributed runtime |
| Manage model-server fleet          | Kubernetes          |
| LLM-aware routing                  | llm-d               |
| Preserve cache locality            | llm-d               |
| Distributed inference optimization | llm-d               |
| Infrastructure lifecycle           | Kubernetes          |
| Restart / rescheduling             | Kubernetes          |

A useful simplification is:

> **vLLM optimizes inference inside a model server.**

> **llm-d optimizes inference across model servers.**

> **Kubernetes manages the infrastructure on which those servers run.**

---

# 32. Final Architectural View

The complete stack can be represented as:

```mermaid
flowchart TB
    USERS[Applications and users] --> API[OpenAI-compatible API / gateway]
    API --> ROUTER[llm-d inference-aware router]

    ROUTER --> A
    ROUTER --> B
    ROUTER --> C

    subgraph FLEET[Distributed inference fleet]
        direction LR
        subgraph A[vLLM server A]
            QA[Scheduler + batching] --> KA[(KV cache A)]
            KA --> GA[GPU group A]
        end
        subgraph B[vLLM server B]
            QB[Scheduler + batching] --> KB[(KV cache B)]
            KB --> GB[GPU group B]
        end
        subgraph C[vLLM server C]
            QC[Scheduler + batching] --> KC[(KV cache C)]
            KC --> GC[GPU group C]
        end
    end

    A & B & C -. queue, load, cache,<br/>and capacity signals .-> ROUTER
    K8S[Kubernetes control plane] -. deploys, schedules,<br/>scales, and recovers .-> ROUTER
    K8S -. manages .-> FLEET

    subgraph HARDWARE[What constrains each GPU group]
        COMP[Compute<br/>prefill]
        MEM[Memory capacity<br/>weights + KV cache]
        BW[Memory bandwidth<br/>decode]
    end
    HARDWARE -. determines usable capacity .-> FLEET

    classDef edge fill:#e8f1ff,stroke:#2563eb,color:#172554
    classDef route fill:#f3e8ff,stroke:#9333ea,color:#3b0764
    classDef serve fill:#e8fff3,stroke:#059669,color:#052e16
    classDef gpu fill:#fff4db,stroke:#d97706,color:#451a03
    class USERS,API edge
    class ROUTER,K8S route
    class QA,QB,QC,KA,KB,KC serve
    class GA,GB,GC,COMP,MEM,BW gpu
```

---

# Conclusion

Efficient LLM infrastructure depends on much more than selecting a powerful GPU.

The complete problem combines:

* accelerator compute;
* memory capacity;
* memory bandwidth;
* model size;
* prompt length;
* KV-cache management;
* batching;
* multi-GPU communication;
* cache locality;
* distributed routing;
* Kubernetes orchestration.

The progression from PyTorch to vLLM, and from vLLM to llm-d, reflects increasing infrastructure complexity.

A simple model can be loaded directly with a machine-learning framework.

A production model requires an inference server.

A large multi-user deployment requires batching and efficient KV-cache management.

A large model may require multiple GPUs.

A fleet of model servers requires intelligent LLM-aware routing.

At cluster scale, Kubernetes provides the orchestration layer underneath the complete system.

The most important practical lesson is therefore:

> **Production LLM performance is determined by how efficiently the infrastructure moves model weights, manages inference state, shares GPU resources and routes work across the cluster—not merely by the theoretical compute performance of the accelerator.**

## Source

KodeKloud, **“AI Infrastructure Explained (GPUs, vLLM, and LLM-D)”**
YouTube, 2026.
https://www.youtube.com/watch?v=hBzUokVYQkI
