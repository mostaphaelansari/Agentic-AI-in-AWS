---
title: "AgentCore Filesystem vs API Storage"
date: 2026-08-11
author: Mostapha EL ANSARI
tags:
  - aws
  - bedrock
  - agentcore
  - storage
status: internal
---

# AgentCore Filesystem vs API Storage

> [!summary]
> API storage addresses remote objects explicitly, while an AgentCore filesystem presents storage as an ordinary directory inside the Runtime. Our current comparison workflow should keep using the API approach; a filesystem becomes useful when the agent needs a resumable workspace.

## The Difference

With **API storage**, application code sends requests to a service such as S3:

```python
s3.put_object(Bucket="agent-data", Key="uploads/data.csv", Body=data)
s3.get_object(Bucket="agent-data", Key="uploads/data.csv")
```

The application controls object names, uploads, downloads, retries, permissions, retention, and deletion. Data is independent of the Runtime session and can be accessed by other authorized services.

With **filesystem configuration**, AgentCore mounts storage at a path inside the Runtime:

```python
with open("/mnt/workspace/data.csv", "wb") as file:
    file.write(data)
```

The agent and ordinary Linux tools can use `open`, `ls`, `git`, pandas, and other file-oriented libraries without writing storage API calls. Enabling a mount does not automatically persist conversation history: the agent must write to the mount, and managed session storage is useful only when later invocations reuse the same Runtime session ID.

| Concern | Storage through an API | Filesystem configuration |
|---|---|---|
| Access model | Bucket/key or database request | Normal file path |
| Persistence | Controlled by the external service | Depends on managed session storage, S3 Files, or EFS |
| Sharing | Explicitly shared through service permissions | Session storage is isolated; S3 Files/EFS can be shared |
| Best fit | Durable records, uploads, offloaded results | Resumable workspaces and file-oriented tools |

## A Simple Mental Model

Imagine that the data is stored in a warehouse.

- An **API** is the service counter. The application gives the clerk an exact identifier, such as `bucket=agent-data` and `key=projects/a/report.pdf`. The clerk returns that object. To save a change, the application gives the object back to the clerk and explicitly asks for it to be stored.
- A **mounted filesystem** is like adding a door from the Runtime into a storage room. The application sees shelves, folders, and files. It can open `projects/a/report.pdf`, change it, rename it, or let another program use it through normal filesystem operations.

The warehouse may still be remote in both cases. A mount does not copy the entire storage service into the Runtime. AgentCore and the operating system translate file operations behind the scenes.

The difference is therefore mainly **who manages the storage interaction**:

```text
Using an API
Application code manages: download -> local temporary file -> upload

Using a mount
Filesystem layer manages: open path -> read/write remote or managed storage
```

### The same task using an API

Suppose the agent must edit a report stored in S3. The application typically does this:

1. Know the bucket name and object key.
2. Call the S3 `GetObject` API.
3. Save the response as a temporary local file.
4. Give that temporary path to the PDF or document tool.
5. Detect the output produced by the tool.
6. Call the S3 `PutObject` API to save the result.
7. Decide what to do with the temporary files and old S3 object.
8. Repeat the download process if a new Runtime later needs the file.

```text
S3 object
   |
   | GetObject
   v
/tmp/report.pdf -> processing tool -> /tmp/result.pdf
                                         |
                                         | PutObject
                                         v
                                  new S3 object
```

This is clear and useful when there are only a few known inputs and outputs.

### The same task using a mount

AgentCore attaches the configured storage at a path, for example `/mnt/workspace`. The application then does this:

1. Open `/mnt/workspace/report.pdf`.
2. Let the document tool read or modify it.
3. Close the file.

```text
/mnt/workspace/report.pdf -> processing tool -> /mnt/workspace/result.pdf
```

The application does not contain S3 download and upload logic. To the application, both files are already part of its working directory. Network access and persistence still happen, but the mounted filesystem layer handles them.

This is useful when many programs must work on the same directory tree or when the Runtime must return to the workspace later.

## Why AWS Added Filesystem Mounting

The main reason is compatibility with existing software. Many programs are designed to work with normal directories and file paths, not storage APIs. Git, compilers, package managers, SQLite, document-processing libraries, and command-line tools expect to be able to open, rename, search, and delete files in a directory.

A mount is not a new place where data magically exists, and it does not mean that APIs are obsolete. It is an operating-system interface placed in front of persistent storage. The storage remains remote or AgentCore-managed, but the Runtime sees it at a local-looking path such as `/mnt/workspace`.

The practical choice is therefore:

```text
API:   application -> storage request using bucket + object key
Mount: application -> ordinary file path -> mounted persistent storage
```

Use the API when the application understands the storage service and only needs to move identifiable objects. Mount the storage when the application or one of its tools expects a working directory and should not need storage-specific integration code.

Without a mount, application code must act as the storage coordinator:

```python
s3.download_file(bucket, "input/report.pdf", "/tmp/report.pdf")
# Process the local copy.
s3.upload_file("/tmp/result.pdf", bucket, "output/result.pdf")
```

It must download objects, reconstruct their directories, detect changes, upload the results, process deletions, and handle retries or conflicts. This works well when a workflow reads or writes a few independent objects, but it becomes cumbersome for a workspace containing many related files.

With a mount, remote or managed persistent storage looks like an ordinary Linux disk:

```python
with open("/mnt/workspace/report.pdf", "rb") as file:
    process(file)
```

Existing programs can then work without being rewritten to call S3 or another storage API:

```bash
git status
find /mnt/workspace
npm install
python script.py
```

A coding agent illustrates the difference. If its repository contains 10,000 files, an API-only implementation must list and download the objects, rebuild the directory tree, run development tools, determine which files changed or were deleted, and synchronize those changes back to storage. With a mounted filesystem, the agent opens `/mnt/workspace/project` and uses Git and development tools normally. The workspace can also be restored when the session resumes.

AWS therefore added filesystem configuration to give arbitrary Runtime containers and existing Linux tools a persistent working disk without requiring every developer to build custom download/upload synchronization code. It is not a replacement for storage APIs; it solves a different problem:

> **API storage explicitly saves and retrieves objects. A mounted filesystem gives the agent a persistent working disk.**

For example, saving one uploaded CSV is naturally an API operation: upload `input.csv`, process it, and store `result.csv`. Developing a software project is naturally a filesystem operation because Git, the compiler, tests, and package manager all need to navigate and modify the same directory tree. Implementing that project workspace entirely through object API calls would add synchronization code without improving the agent's work.

Use an API for uploads, generated reports, images, durable records, and other independent objects. Use a mount for repositories, multi-file projects, long-running work, existing file-oriented tools, or a workspace that must survive stop-and-resume cycles.

### What a mount gives the application

A mounted filesystem provides behaviors that file-oriented software already understands:

- Paths and nested directories such as `/mnt/workspace/src/main.py`
- Listing and searching directories
- Reading or changing part of a file
- Renaming or moving files
- Letting multiple libraries and command-line programs use the same paths
- With EFS, shared POSIX permissions and concurrent access
- With managed session storage, restoration of a session workspace after the Runtime stops

Reproducing all of those behaviors directly with an object API requires additional application code. For example, S3 object keys can look like directory paths, but S3 is still an object store: an application must translate file operations into object requests and manage synchronization.

### What a mount does not do

A mount does not automatically:

- Remember the conversation or model state
- Decide which information the agent should save
- Make every other AWS service see the files
- Remove the need for IAM and network permissions
- Make session storage permanent

The agent must still write useful state to the mounted path. Other services need access to the same backing storage or must receive the data through an API.

## Quick Decision Rule

Ask one question: **Is this a stored object, or is it a working directory?**

| Situation | Better starting choice | Reason |
|---|---|---|
| Upload one CSV and return one result | API | The inputs and outputs are explicit objects |
| Save generated images or reports | API | They are independent durable objects |
| Keep a Git repository for the agent | Mount | Git expects a directory tree and normal file operations |
| Run compilers, tests, or package managers | Mount | Existing tools expect filesystem paths |
| Resume a user's multi-file workspace later | Mount | The whole working directory can persist |
| Share a live directory across authorized applications | EFS mount | EFS supports shared POSIX filesystem access |

For this repository today, each comparison request creates a fresh session and processes explicit uploads and results. The API approach remains simpler. A filesystem mount becomes valuable if the application evolves into a long-lived workspace where the same user and agent repeatedly modify many related files.

## Who Can Access the Mounted Files?

`filesystemConfigurations` covers three storage options with different access boundaries.

### Managed session storage

Managed session storage is a private AgentCore Runtime workspace. Application code accesses it as an ordinary directory such as `/mnt/workspace`. AgentCore manages the backing storage; it is not exposed as a customer-owned S3 bucket or as an API that another application can browse.

Each `runtimeSessionId` receives an isolated directory. A different session ID—even on the same agent—cannot read it. Stopping and later invoking the same Runtime version with the same session ID restores the files. AWS resets the workspace after 14 days without invocation and when the Runtime version changes, so it should not hold permanent business records.

```text
AgentCore session A -> /mnt/workspace for session A
AgentCore session B -> separate /mnt/workspace for session B
External service    -> no direct access
```

### Customer-owned S3 Files or EFS

S3 Files and EFS are bring-your-own filesystems. AgentCore mounts their access points into Runtime sessions, but the data remains in the customer's AWS account. Other authorized agents and applications can access the same storage. Both options require VPC networking plus explicit IAM and network permissions.

S3 Files synchronizes with its backing S3 bucket, so applications can use normal file operations inside the Runtime and S3 APIs outside it. EFS is a better fit when multiple applications need shared POSIX behavior and concurrent reads and writes.

| Filesystem option | Accessible from | Data lifecycle |
|---|---|---|
| Managed session storage | One AgentCore Runtime session ID | Reset after 14 idle days or a Runtime version update |
| S3 Files | Mounted Runtime sessions and authorized S3 clients | Customer-managed until deleted |
| EFS | Mounted Runtime sessions and authorized EFS clients | Customer-managed until deleted |

## What Enabling the Setting Actually Does

Filesystem configuration mounts storage when AgentCore starts a Runtime session. It does not automatically move existing S3 objects, save model messages, or change application variables. The agent must use the configured path in its code.

For example, this configuration:

```json
{
  "sessionStorage": {
    "mountPath": "/mnt/workspace"
  }
}
```

makes `/mnt/workspace` available inside the Runtime. The application must then write something there:

```python
from pathlib import Path

Path("/mnt/workspace/session.json").write_text(serialized_history)
```

The practical flows are:

```text
Current API flow
UI -> S3 API -> S3 object -> agent downloads object

Managed filesystem flow
UI -> AgentCore invocation -> agent reads/writes /mnt/workspace
                              -> AgentCore restores it for the same session ID

Bring-your-own filesystem flow
External application -> S3 Files or EFS <- mounted AgentCore Runtime
```

The mount is available to the Runtime container, not automatically to every service the agent calls. If Code Interpreter or a Gateway tool needs a file, the Runtime must send the data through that tool's supported input mechanism or place it in mutually accessible storage such as S3.

## Choice for This Repository

The current comparison app creates a fresh AgentCore session for every answer. It temporarily stages CSV/mmCIF uploads in S3, deletes them after processing, and offloads large tool results to S3. This is intentionally stateless, so adding a filesystem mount alone provides no benefit.

Managed session storage would make sense if the app becomes a multi-turn workspace. We would mount a directory such as `/mnt/sessions`, configure a file-backed Strands session manager to use it, and reuse the same `runtimeSessionId` across turns.

The Runtime filesystem is separate from the managed AgentCore Code Interpreter sandbox. Mounting storage on the Runtime does not automatically expose it to Code Interpreter, Browser, Gateway targets, or Lambda functions. Data must be transferred explicitly through an API or mutually accessible storage when those components need it.

See [File system configurations for AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html).
