# ov.conf 模板逐项说明

`ov.conf.example` 是**严格 JSON**，没有注释（OpenViking 用 `json.loads` 读它，注释会让它直接报
`Invalid JSON in config file`）。所以逐项说明放这里，键的注释以本表为准。

- 用法：复制成 `D:\AI\openviking\ov.conf`（守护脚本默认位置，`memory/openviking-recover.ps1:10-12`、
  `memory/openviking-observe.ps1:9`、`memory/openviking-memory-health.py:18`），或放
  `~/.openviking/ov.conf`；`openviking-server --config ov.conf` 启动（`memory/openviking-recover.ps1:232-233`）。
- **不用把 key 写进文件**：加载时会先做 `os.path.expandvars()`（`openviking_cli/utils/config/config_loader.py:87-88`），
  所以模板里的 `$OV_EMBEDDING_API_KEY` 这类占位会从环境变量取值；变量没设时保持原样，服务会因为 key 无效而报错——
  这比把明文写进文件安全，出问题时也看得见是哪一项没配。
- 字段来源：先按真实 `ov.conf` 的键结构对齐（只读，未复制任何值），再用已安装的
  `openviking 0.4.17.1` 的 pydantic 模型逐项校验过；行号对应该版本在 `venv/Lib/site-packages` 下的源码。
- 校验结果：删掉 `server` 段后 `OpenVikingConfig.model_validate()` 通过，`server` 段交给
  `ServerConfig.model_validate()` 通过（两段是分开校验的，`server` 段里不能放别的段）。

## storage

| 键 | 模板值 | 说明 | 出处 |
|---|---|---|---|
| `storage.workspace` | `D:/AI/openviking/data` | 数据目录（记忆、session、向量库都在这下面）。不填默认 `./data`，即相对启动时的 cwd | `openviking_cli/utils/config/storage_config.py:25` |
| `storage.vectordb.name` | `context` | 向量集合名。换名字等于换一个空库 | `vectordb_config.py:9` |
| `storage.vectordb.backend` | `local` | `local` / `cuvs` / `http` / `volcengine` / `vikingdb`；单机用 `local`。指向别的后端时 `backend` 要与 `url`、凭据一起改 | `vectordb_config.py:199` |
| `storage.agfs.backend` | `local` | 文件层：`local` / `s3` / `memory` | `agfs_config.py` 的 `AGFSConfig.backend` |

## embedding（召回用；维度必须与所选模型一致）

| 键 | 模板值 | 说明 | 出处 |
|---|---|---|---|
| `embedding.dense.provider` | `openai` | 允许值：`openai` `azure` `volcengine` `vikingdb` `jina` `ollama` `gemini` `voyage` `dashscope` `minimax` `cohere` `litellm` `local`。整段都不填时服务会自己用 `local` + `bge-small-zh-v1.5-f16` | `embedding_config.py` 的 `_VALID_PROVIDERS`、`apply_default_local_dense` |
| `embedding.dense.api_key` | `$OV_EMBEDDING_API_KEY` | 换成你的环境变量名；`local` provider 不需要 | 同上 |
| `embedding.dense.api_base` | `https://api.example.com/v1` | 示例地址，填服务商实际地址 | 同上 |
| `embedding.dense.model` | `<embedding-model>` | 占位，填模型名 | `embedding_config.py:45` 附近 |
| `embedding.dense.dimension` | `1024` | **示例值**，必须等于你模型的输出维度；和库里已有集合的维度不一致会启动失败 | `embedding_config.py:45` |
| `embedding.dense.encoding_format` | `float` | `float` 或 `base64`；不确定就别改 | `embedding_config.py:93` |
| `embedding.max_concurrent` | `10` | 默认值，并发太高会被上游限流 | `embedding_config.py:641` |
| `embedding.max_retries` | `3` | 默认值，0 表示不重试 | `embedding_config.py:644` |
| `embedding.allow_metadata_override` | `false` | 只在「换 embedding 模型但想保留旧向量」时才开，语义可能漂移 | `embedding_config.py:657` |

## vlm（抽取用；会话 commit 后异步跑）

| 键 | 模板值 | 说明 | 出处 |
|---|---|---|---|
| `vlm.provider` / `api_key` / `api_base` / `model` | `openai` / `$OV_VLM_API_KEY` / 示例地址 / `<chat-model>` | 抽取的记忆质量取决于这个模型；同一套 OpenAI 兼容形状 | `vlm_config.py` 的 `VLMConfig` |
| `vlm.max_concurrent` | `32` | 默认值 | `vlm_config.py:140` |
| `vlm.max_retries` | `3` | 默认值 | `vlm_config.py:110` |
| `vlm.credentials[]` | 一条 `backup-1` | 备用凭据，按数组顺序失败后往后跳（0 优先级最高）。**失效的凭据要删掉**，否则每次抽取都白跳 | `vlm_config.py:176` |

- 2026-11-08 生产实配：`vlm` 段改指本机 Magpie 网关（`api_base` = `http://127.0.0.1:3425/v1`，
  `model` = `antigravity/gemini-3.8-flash`）。Magpie 对本机回环不校验 key，`api_key` 填占位串即可；
  `credentials` 留空数组时 OpenViking 会把主配置镜像成一条 `default` 凭据（`_normalize_credentials`），
  不会引入别的上游。这一段只动 `vlm`，`embedding`（SiliconFlow bge-m3，dimension 1024）保持原样。

## server

| 键 | 模板值 | 说明 | 出处 |
|---|---|---|---|
| `server.host` / `server.port` | `127.0.0.1` / `1933` | 只本机访问就别改成 `0.0.0.0` | `openviking/server/config.py:323-324` |
| `server.auth_mode` | `api_key` | 不填时按有没有 `root_api_key` 自动判定（有 ⇒ `api_key`，没有 ⇒ `dev`）；`dev` 不鉴权，别对外 | `server/config.py:335`、`:369-381` |
| `server.root_api_key` | `$OV_ROOT_API_KEY` | 守护脚本和脑的 `OPENVIKING_API_KEY` 用的是同一把；换 key 要一次改全（教程 07「服务和守护」踩过） | `server/config.py:336`、`memory/openviking-memory-health.py:29-30` |
| `server.cors_origins` | `["http://127.0.0.1:5173"]` | 只在浏览器里直接调 API 才需要；默认是 `["*"]`，本机自用别放宽 | `server/config.py:341` |

## memory / retrieval

| 键 | 模板值 | 说明 | 出处 |
|---|---|---|---|
| `memory.custom_templates_dir` | `""` | 自定义记忆模板目录；空 = 用内置模板。参考部署用 `D:\AI\openviking\custom-memory-templates`，不熟就别开 | `memory_config.py:31` |
| `retrieval.enable_intent` | `false` | 检索前的意图分析，会多花一次模型调用；默认关 | `retrieval_config.py:39` |

## 起来之后验什么

```powershell
curl.exe -s http://127.0.0.1:1933/health   # 期望 healthy: true
```

`healthy: true` 只说明服务活着，不说明 embedding 能用；真正的验收是教程 07 的召回自检
（`python3 bin/memory.py --selftest`）和数据目录下 `sessions/pi-<id>/messages.jsonl` 有没有新文件。
