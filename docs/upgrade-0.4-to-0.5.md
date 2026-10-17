# 从 0.4.x 升级到 0.5.0（完整迁移步骤）

> 本文是原 README「全流程升级总清单 / 从 0.4.x 升级 / 已在 0.5.0 上迁移过」三节内容的独立成文版。
> 适用对象：从 **0.4.x（或更早）** 升级到 **0.5.0** 的用户；以及已在 0.5.0 上迁移过一次、
> 但因编码口径变化需要**再迁一次**的用户。README 只保留一句话指引，具体操作全部在这里。

## 全流程升级总清单（0.4.x → 0.5.0，照着做即可）

> 这一节把散在各处的内容**按顺序串成一条线**，Windows 与 macOS 都适用。
> 想理解每一步为什么，看对应小节的详细说明。

| # | 步骤 | Windows 怎么做 | macOS 怎么做 | 参考 |
|---|---|---|---|---|
| 0 | **完全退出旧版 Niu** | 任务管理器确认没有 `niu.exe` / `niu_api` 残留 | 活动监视器确认 `niu` 与 `niu_api` 已退出 | — |
| 1 | **删掉 3 个向量索引文件** | PowerShell `Remove-Item "$env:USERPROFILE\.niu\lightrag_storage\vdb_entities.json"`（另两个同目录：`vdb_relationships.json`、`vdb_chunks.json`） | `rm ~/.niu/lightrag_storage/vdb_entities.json`（另两个同目录） | [从 0.4.x 升级](#从-04x-升级必做) |
| 2 | **装新包（内含模型与依赖，无需另装）** | 解压 `Niu-0.5.0-win-x64.7z` 到任意目录，双击 `niu.exe` | 挂载 `Niu-0.5.0-mac-intel.dmg`，拖 `niu.app` 进 `Applications` | [下载安装](../README.md#下载安装) |
| 3 | **启动，点是「尝试修复」** | 弹「检测到数据一致性问题」→ 点「尝试修复」→ 用新模型重建索引（1 万向量量级实测**二十几分钟**） | 同左 | [从 0.4.x 升级](#从-04x-升级必做) |
| 4 | 若提示「**向量模型产物缺失**」 | 极少见（0.5.0 包内自带）；按[安装向量模型产物](../README.md#安装向量模型产物embeddinggemma-2--llamacpp)补 | 同左，macOS 还须补可执行位并重签名 | [安装向量模型产物](../README.md#安装向量模型产物embeddinggemma-2--llamacpp) |
| 5 | **可选清理**：旧模型权重目录 | 删除 `models/bge-base-zh-v1.5/`（约 400 MB，已不用） | 同左 | [从 0.4.x 升级](#从-04x-升级必做) |
| 6 | 确认收尾：`pgrep`/任务管理器无 `llama-server` 残留，端口空闲（缺省 19878，以你配置行里的 `--port` 为准） | `tasklist \| findstr llama-server` 应为空 | `pgrep -fl llama-server` 应为空 | — |

> ⛔ **不要删** `~/.niu/lightrag_storage/` 下的 `graph_chunk_entity_relation.graphml`、
> `kv_store_full_docs.json`、`kv_store_llm_response_cache.json` —— 这是知识图谱的**原始真相源**，
> 索引重建全靠它们。删了就是真丢数据。
>
> ⛔ **不要**整目录 `rm -rf ~/.niu/lightrag_storage/`。

## 从 0.4.x 升级（必做）

0.5.0 把向量模型从 BAAI/bge-base-zh-v1.5 换成了 EmbeddingGemma 2（GGUF，经 llama.cpp 推理）。
新旧模型产生的向量**不兼容**，升级后**必须先删掉旧索引，再启动**。

> ⚠️ **这一步不能跳，也没有任何程序会提醒你。** 新旧模型的向量维度都是 768，
> 所以程序无法从文件里分辨索引是新模型还是旧模型编码的——它只会照常用。
> 跳过这一步的后果是：新旧两套向量被混在同一个索引里，**不会报错、不会弹窗**，
> 但检索结果会明显变差，且没有任何提示。请务必按下面 **1–3 步**做完再启动（第 4 步是可选清理）。

1. **退出 Niu**（完全退出，不是关窗口）
2. **删掉 3 个向量索引文件**：
   - macOS 或 Linux：
     ```bash
     rm ~/.niu/lightrag_storage/vdb_entities.json \
        ~/.niu/lightrag_storage/vdb_relationships.json \
        ~/.niu/lightrag_storage/vdb_chunks.json
     ```
   - Windows（PowerShell，路径按你的用户目录）：
     ```powershell
     Remove-Item "$env:USERPROFILE\.niu\lightrag_storage\vdb_entities.json",
                   "$env:USERPROFILE\.niu\lightrag_storage\vdb_relationships.json",
                   "$env:USERPROFILE\.niu\lightrag_storage\vdb_chunks.json"
     ```
3. **启动 Niu**。首次启动会弹「检测到数据一致性问题」——这是预期的，点是「尝试修复」，
   程序会用新模型从知识图谱重建索引（节点/关系越多越久：1 万向量量级实测约 **二十几分钟**，
   期间请勿强制退出——退出会让重建半途而废、下次重启再触发）。
4. 旧模型权重目录 `models/bge-base-zh-v1.5/`（约 400 MB）已不再需要，可手动删除。

> ⚠️ **这一步会丢失一部分块的原文，请先读下面「迁移前请先选一条路」。**

> 不要删 `~/.niu/lightrag_storage/` 下的另外三个文件 —— `graph_chunk_entity_relation.graphml`、
> `kv_store_full_docs.json`、`kv_store_llm_response_cache.json`。它们是知识图谱的原始真相源，
> 上面的重建全靠它们。

### 迁移前请先选一条路

删除 3 个 VDB 文件后，程序是**从知识图谱反向重建索引**。但图谱里引用的块分为两类，
只有一类有原文可重建：

| 块的来源 | 有原文副本吗 | 删 VDB 后 |
|---|---|---|
| **文档入库**（你用「入库文档」功能加的文件） | 有，在 `kv_store_full_docs.json` | ✅ 可完整重建 |
| **自动入库**（脑区、技能、对话精炼、梦境进化等由 Agent 写入的内容） | **没有副本，原文只存在于向量索引里** | ❌ **无法重建，会从索引中消失** |

**所以请按下面选一条：**

**路线 A —— 自动入库的内容不重要（大多数个人用户选这条）**
直接按上面 1–4 步删 VDB 重建。代价是脑区摘要、技能描述、精炼笔记这类块的索引会消失；
它们的同步进程会在之后自动重新写入一部分，但**历史精炼内容的检索面会变少**。
重启后可在修复报告里看到具体丢了多少。

**路线 B —— 自动入库的内容重要，要求一条不丢**
本程序**不提供**向量库格式转换工具，请自行编写。旧的 3 个 `vdb_*.json` 里每条记录都带
`content` 字段（原始文本），所以做法是：读取每条记录的 `content`，用**新模型**重新编码，
**只替换 `vector` 字段、保持 `__id__` 不变**，写回同名文件 —— 图谱引用一条都不用动，
旧模型权重不需要保留（旧向量是直接被替换掉的，不读取）。
**注意程序不会自动做这件事**：两种口径产出的向量维度都是 768、文件格式相同，
程序无法从文件判断当前索引是旧口径还是新口径编码的，自动转换有混用风险。
转换前**先备份整个 `~/.niu/lightrag_storage/` 目录**。

## 已在 0.5.0 上迁移过？需要再迁一次（必做）

**为什么又要迁：编码口径又变了一次（这次统一加了任务前缀）。** 上面那次迁移解决的是
「换模型」，而向量模型不变、只是编码时统一加上了 EmbeddingGemma 2 的任务前缀
（`task: search result | query: `）。带前缀和裸编码算出来的向量**同样互不兼容**，
所以已经按上面步骤迁过一次的用户，索引仍然需要重建一次。

> ⚠️ **这一步不能跳，也没有任何程序会提醒你。** 两种口径产物的向量维度都是 768，
> 所以程序无法从文件里分辨索引是哪种口径编码的——它只会照常用。
> 跳过这一步的后果是：新旧两套向量被混在同一个索引里，**不会报错、不会弹窗**，
> 但检索结果会明显变差，且没有任何提示。请务必按下面 3 步做完再启动。

1. **退出 Niu**（完全退出，不是关窗口）
2. **删掉 3 个向量索引文件**（⚠️ 会丢失「自动入库」类块的索引，迁移前请先读上面
   [迁移前请先选一条路](#迁移前请先选一条路)，重要数据请走自行转换那条路）：
   - macOS 或 Linux：
     ```bash
     rm ~/.niu/lightrag_storage/vdb_entities.json \
        ~/.niu/lightrag_storage/vdb_relationships.json \
        ~/.niu/lightrag_storage/vdb_chunks.json
     ```
   - Windows（PowerShell，路径按你的用户目录）：
     ```powershell
     Remove-Item "$env:USERPROFILE\.niu\lightrag_storage\vdb_entities.json",
                  "$env:USERPROFILE\.niu\lightrag_storage\vdb_relationships.json",
                  "$env:USERPROFILE\.niu\lightrag_storage\vdb_chunks.json"
     ```
3. **启动 Niu**。首次启动会弹「检测到数据一致性问题」——这是预期的，点是「尝试修复」，
   程序会用新口径从知识图谱重建索引（实测约 **二十几分钟**，期间请勿强制退出）。

> 不要删 `~/.niu/lightrag_storage/` 下的另外三个文件 —— `graph_chunk_entity_relation.graphml`、
> `kv_store_full_docs.json`、`kv_store_llm_response_cache.json`。它们是知识图谱的原始真相源，
> 上面的重建全靠它们。
