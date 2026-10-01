# AIToolbox · V11 独立版

把自己的本地模型和云厂商接成 API，保存结果、原请求身份与用量。首次启动没有预置厂商、模型或账号；模型文件、账号和密钥由使用者提供。

**当前源码版本 11.15.0，保留 v11.7.0 的 LocalAI 升级，并同步主线 v11.13.0 的请求与取消修复。** 本地服务、两层资源安排、批次连续段停止、LocalAI 原生扩展沿用该版本；独立版保留自己的窗口、云厂商配置、凭据、数据库和端口。旧 v10.3.0 CPU 版可从 Git 历史恢复。

11.15.0 修复不完整请求仍执行、提交前错误状态、流式结束标记误判，以及 vLLM 取消后的迟到提交和响应超时。部分结果、原请求身份和真实停止证明继续保存。此版本发布源码，Windows 二进制未更新；ComfyUI 由独立项目维护。

后端扩展也有修改，须准备包含当前 `native/extensions/backend/python/vllm_execution.py` 的 11.15.0 镜像。现有 11.8.0 镜像不会自动更新；先关闭应用并排空请求，运行 `tools/localai.py stop`，移除该工具准确提示的自有旧容器，再以 `tools/localai.py start --image aitoolbox-localai:11.15.0` 启动，沿用原数据目录和模型根。数据和权重保留在宿主机。

## 运行环境与启动

Windows 10/11 x64；桌面源码使用 Python 3.11 和 Tk。**本地模型运行改用 WSL2、Docker、NVIDIA GPU 与匹配的 NVIDIA Container Toolkit，原先“无需 Docker、内置 CPU 单模型运行时”的说明已失效。** 云端入口可以在本地后端尚未准备时独立使用。不会自动下载模型。

本仓库发布源码，不附带模型、现用配置、私有数据、Docker 镜像或 Windows 二进制发行包。先按下面准备 V11 后端；普通未打补丁的 LocalAI 镜像不具备本产品所需的原生计数、资源画像和停止确认协议。

1. 在自己的 WSL2 发行版中准备 Docker 和 NVIDIA Container Toolkit，确认 Docker 能访问显卡。以下示例发行版名为 `Ubuntu`，可以换成自己的名称。
2. 在 WSL 中准备固定上游源码并构建后端。`/path/to/AIToolbox` 指本仓库在 WSL 中的路径；构建需要网络、足够磁盘空间和时间，后端依赖为数 GB 级。

   ```bash
   python3 /path/to/AIToolbox/tools/prepare_backend.py --output "$HOME/aitoolbox-build"
   docker build -t aitoolbox-localai:11.15.0 "$HOME/aitoolbox-build"
   ```

   **已有匹配当前 helper 的 V11 部署可迁移，跳过上述从零构建：**

   ```bash
   python3 /path/to/AIToolbox/tools/migrate_backend.py --backends /your/existing/backends
   ```

   工具先核对已安装 helper 与当前源码完全相同；旧 helper 不匹配时明确拒绝，须先更新它或使用上述从零准备方法。默认读取已验证镜像 `aitoolbox-localai:v4.10.0-contract-v4`，也可用 `--source-image` 指定。工具核对适配器后，将原镜像与两个已安装后端完整复制进独立镜像，不重新编译、不修改旧后端，也不复制模型、配置、凭据或数据卷。独立容器自带 `/backends/cuda12-llama-cpp` 和 `/backends/cuda13-vllm`，运行时不依赖旧部署的后端目录。固定来源见 `config/runtime-lock.json`。

3. 回到 Windows，在仓库目录中建立自己的后端。`--asset-root` 可重复指定，目录中的文件只读挂载；登记模型时选择这些目录内的文件或模型目录。省略时使用数据目录下的 `models`。

   ```powershell
   python tools/localai.py start --distribution Ubuntu --asset-root "D:\MyModels"
   python src/main.py
   ```

   使用自选数据目录时，上述两个命令都添加 `--data-dir "D:\MyAIToolboxData"`。准备工具生成独立密钥、配置和容器，不导入旧部署的厂商或模型。后续可用同一工具的 `status` / `stop` 操作查看或停止自己的后端；执行前关闭桌面程序。新增模型根时，需要在关闭程序后停止并移除提示中准确命名的自有容器，再重新 `start`；数据和权重保留在宿主机。

首次启动默认使用 `%LOCALAPPDATA%\AIToolbox`。窗口仍有“云厂商”“本地模型管理”“用量”“接入与测试”四页。后端未准备时，本地健康检查或登记返回错误；云厂商配置与历史用量仍可使用。

## 模型登记与生命周期

在“本地模型管理”选择 GGUF 文件及需要的多模态投影，或选择含 `config.json`、safetensors 的模型目录。勾选真实输入能力，填写实例总上下文 `max_context_tokens`；文本输出必选，文本输入可不选，因此可登记只接受音频的 ASR 或只接受图片的 OCR。支持范围以对应后端和真实能力验证为准，不能把“发现文件”当作可用。

登记会真实加载、验证所选能力并取得原生资源画像，成功才进入 `READY`；验证结束释放测试实例。调用时按需加载，最后一个使用者结束后卸载。`READY` 表示验证过的绑定，并不表示一直占用显卡。修改先排空旧请求、验证新绑定，失败保留原配置；取消登记停止接新请求、排空并释放实例，不删除模型文件或历史结果。相同 ID 重新登记建立新修订。

旧 CPU 版使用原数据目录升级时，云厂商、DPAPI 凭据、调用 token、收据和数据库保留。旧 CPU `READY` 会显示 `legacy_cpu_registration_requires_update`，请选中模型后点“修改注册”，用现有字段重新验证；成功后形成新修订。不会在启动时偷偷加载旧模型、重放请求，或把旧运行时的证明当作 V11 验证结果。

## 两层资源安排

第一层按登记的模型、精度、**完整实例总上下文 T** 计算显存占位，从大到小尝试；大项放不下仍继续检查小项。同模型先复用已有实例，需要且能放下时再建副本。并发实例数由实际预算决定，不固定为 1 或 4，也不缩小 T 或降低精度来凑空间。

第二层扫描完整批次，同模型请求即使被其他模型隔开，也继续向后收集。实例内部按到达顺序检查可放入的请求；暂时放不下的等待，后面能放下的可以先执行。例如剩余 3000，先来的 4000 等待，后来的 2000 可以进入。

每条请求预留它声明的完整 `context_tokens = C`；多候选 `n` 占用 `C × n`。原生实际输入计数只用于核对“输入 + 最大输出 ≤ C”，输入短不会少占 C。T 是实例总池，C 是本次请求的完整额度，`max_tokens` / `max_completion_tokens` 是最大输出，三者不能互换。

## 接口与批次

| 用途 | 默认地址 |
| --- | --- |
| 自定义云厂商 | `http://127.0.0.1:49777/p/<厂商标识>/v1` |
| 本地模型 | `http://127.0.0.1:49778/v1` |
| 私有 LocalAI 后端 | `http://127.0.0.1:49779`，由产品内部使用 |

客户端从“接入与测试”复制相应入口的调用凭据，发送 `Authorization: Bearer <凭据>`；不要使用云厂商原始 Key 或后端密钥代替。各端口仅绑定本机。`settings.json` 的 `cloud_port` / `local_port` 可在关闭应用后修改；后端端口通过准备工具 `--port` 设置，不能与客户端端口重叠。

本地 `GET /v1/models` 查询已就绪模型；`POST /v1/chat/completions` 示例：

```json
{"model":"my-model","messages":[{"role":"user","content":"你好"}],"context_tokens":2048,"max_tokens":128}
```

支持文本、所登记的图像/音频输入、流式响应和多候选。`POST /v1/audio/transcriptions` 使用 multipart 的 `model`、WAV `file`、`context_tokens`、`max_tokens`；当前不提供音频生成。

需表达请求关联时，向 `POST /v1/batches` 提交一次完整、不可变的有序清单（目前只接受非流式 Chat Completions 正文）：

```json
{
  "object_id":"document-1",
  "batch_id":"round-1",
  "requests":[
    {"request_id":"A1","body":{"model":"A","messages":[{"role":"user","content":"第一段"}],"context_tokens":2048,"max_tokens":128}},
    {"request_id":"A2","body":{"model":"A","messages":[{"role":"user","content":"第二段"}],"context_tokens":2048,"max_tokens":128}},
    {"request_id":"B1","body":{"model":"B","messages":[{"role":"user","content":"另一模型"}],"context_tokens":2048,"max_tokens":128}},
    {"request_id":"A3","body":{"model":"A","messages":[{"role":"user","content":"下一连续段"}],"context_tokens":2048,"max_tokens":128}}
  ]
}
```

返回批次 `key`，用 `GET /v1/batches/<key>` 查询。在上述原顺序中，资源安排可以收集 A1、A2、A3；但 A1 有原生证据的超限只传播到同一连续段的 A2。已运行的相关请求会收到定向停止并记录原生确认；B1、A3、其他对象/批次继续，已完成结果保留。生成前拒绝不算这套处理已经完成。**对象、批次和原序共同确定关联范围，单个请求 ID 只负责该请求身份。**

模型管理：`POST /admin/models` 登记；`GET /admin/models/<id>` 查询；`PATCH /admin/models/<id>` 带 `expected_revision` 修改；`DELETE /admin/models/<id>` 排空并注销。登记字段为 `id`、绝对 `model_path`、可选 `mmproj_path`、`capabilities`、`max_context_tokens`。`GET /admin/scheduling` 查看实例、完整额度占用与安排事件。

## 云厂商、结果与数据

厂商配置由使用者填写。标识以小写英文开头，可含数字、下划线和横线；API 根地址一般以 `/v1` 结尾，外网需 HTTPS、本机可 HTTP。OpenAI 兼容使用 Bearer；Anthropic 使用 `x-api-key` 和 `anthropic-version`；其他接口可使用 `api-key`。按厂商原协议转发，不自动转换 Chat Completions、Responses、Messages。Anthropic 客户端若自行追加 `/v1/messages`，Base URL 去掉末尾 `/v1`。模型目录是用户填写的提示，实际权限以厂商响应为准。

保存响应头 `X-AIToolbox-Request-ID` 或自行提供 `X-Request-ID`。在原入口 `GET /requests/<id>` 查询持久收据；云端 `/requests/<id>/result` 返回原响应，流式保留 SSE；本地收据 `response.body_base64` 保存完整正文。断线先查原 ID；已受理同 ID 不再次调用，`UNKNOWN` 不能当成功或盲目重发。用量来自原生返回，未知不估算，本地约 5 秒后同步到业务数据库。

关闭窗口停止新调用、排空已受理工作、保存记录并释放模型；独立 LocalAI 容器可保持空闲，用准备工具 `stop` 停止。备份前关闭应用并停止自己的后端，复制完整数据目录。密钥使用当前 Windows 用户的 DPAPI；换电脑或用户通常需重填云厂商 Key。不要上传使用后的数据目录。

## 构建与验证范围

Windows 桌面打包（与上面的后端镜像构建分开）：

```powershell
python -m venv .build/venv
.build/venv/Scripts/python -m pip install -r requirements-build.txt
.build/venv/Scripts/python tools/build.py
$env:PYTHONPATH = "$PWD/src"
python -m unittest discover -s tests -v
```

输出 `dist/AIToolbox/`、`dist/AIToolbox-Windows-x64.zip` 和 SHA256 清单。桌面包包含 Python/Tk，不再包含旧 CPU llama-server；后端工具脚本仍需 Python，WSL 镜像构建需要 Python 3。完整保留 `_internal` 和配套资源。

迁移后的独立源码通过 60 项工程检查，Windows 打包程序完成空白启动与 22 条真实请求：文本生命周期、三个实例并发、新 ASR/OCR 登记、7 条贪婪安排/完整额度补位，以及原连续段定向停止。调度文件、批次受理/请求执行和原生补丁与 V11.7 基线一致，6 个已部署原生二进制/适配器哈希完全相同。V11.7 的 Omni 兼容证据作为继承基线保留。原生上下文溢出和显存 OOM 未主动制造，Omni 小字 OCR 质量限制保留。新的公开干净构建配方与“完整迁移已验证的后端”是不同验证范围，不能把后者当作所有新机器从零安装都验证过。

音频能力探针沿用独立版的自有文本，通过 Windows 英语语音临时合成，用后删除；不携带第三方录音。缺少英语语音时须安装后重试。自有代码使用 [MIT](LICENSE)，第三方来源及分发边界见[许可说明](THIRD_PARTY_NOTICES.md)。
