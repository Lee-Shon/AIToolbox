# AIToolbox

**把你自己的本地模型和云厂商接成可调用的 API。**

AIToolbox 负责注册验证、请求中转、结果保存和用量统计。模型、厂商账号与密钥属于使用者。首次启动没有预置厂商、模型或账号；它不是出售模型的服务，也不自动替你选择厂商。

## 普通用户：解压后启动

适用 Windows 10/11 64 位。解压完整 `AIToolbox-Windows-x64.zip`，双击其中的 **AIToolbox.exe**。保留旁边的 `_internal` 文件夹；不要单独移动 exe。

发行包内置 Python、桌面界面、数据库和 CPU 推理引擎，不需要安装 Python、Docker、CUDA 或旧版 AIToolbox。首次启动自动创建自己的数据目录和调用凭据。联网仅用于你主动发起的云厂商调用，不自动下载模型。

窗口有四页：

| 页面 | 怎么用 |
| --- | --- |
| 云厂商 | 新增厂商标识、API 根地址、协议/认证、API Key 和模型名称；可随时修改或删除 |
| 本地模型管理 | 选择已有 GGUF 权重和必要的多模态投影文件，填写模型 ID、实例总上下文与能力，注册、修改并查看状态 |
| 用量 | 查看最近 7 天云端和本地调用次数、输入/输出 token、未计量调用及每日明细 |
| 接入与测试 | 查看接口地址，复制调用凭据到自己的客户端，指定模型发送一次测试请求 |

云厂商标识使用小写英文开头，可含数字、下划线和横线。API 根地址填写厂商文档的完整根地址，通常以 `/v1` 结尾；外部地址必须是 HTTPS，本机地址可用 HTTP。模型名称每行一个，只用于展示目录，不是调用白名单。更换厂商配置立即作用于新请求，旧记录继续保留原配置快照。

选择 OpenAI 兼容时使用 Bearer 认证；Anthropic 使用 `x-api-key` 并传递 `anthropic-version`；其他 API-Key 接口使用 `api-key`。程序按原协议转发，不在 Chat Completions、Responses、Messages 之间自动转换。厂商是否支持某个模型、路径或参数，以厂商实际响应为准。

本地模型先经过文件、内存及所选能力的真实验证，`READY` 后才可调用。注册不会下载或复制权重。内置运行时支持的 GGUF 架构才能启用；多模态还需要匹配的投影文件。取消注册停止新请求并等待已受理请求结束，**不会删除权重或历史记录**。

## 给自己的客户端配置

程序运行时，默认地址如下。凭据从“接入与测试”页复制，不要把云厂商原始 Key 当成 AIToolbox 调用凭据。

| 用途 | Base URL |
| --- | --- |
| 自定义云厂商 | `http://127.0.0.1:49777/p/<厂商标识>/v1` |
| 本地已注册模型 | `http://127.0.0.1:49778/v1` |

Anthropic 客户端若自行追加 `/v1/messages`，Base URL 填云端地址去掉末尾 `/v1`。两个入口使用各自的调用凭据。默认仅接受本机访问，不是公网服务器。

调用前用 `GET /v1/models` 查询目录。本地 Chat Completions 示例正文：

```json
{
  "model": "你注册的模型ID",
  "messages": [{"role": "user", "content": "你好"}],
  "context_tokens": 2048,
  "max_tokens": 128
}
```

POST 到本地 `/v1/chat/completions`，携带 `Authorization: Bearer <本地调用凭据>`。文字生成每次必须填写正整数 `context_tokens` 和 `max_tokens` 或 `max_completion_tokens`。注册时的 `max_context_tokens` 是模型实例总容量；同时调用按各自声明的 `context_tokens` 预留，累计不超过总容量，暂时放不下时等待。服务还会核对真实输入 token 加请求输出上限能否放进本次声明额度。注册总量至少 512 token，实际可用值受模型和本机内存限制。支持 `stream: true`。音频转写使用 `/v1/audio/transcriptions`，multipart 字段包含 `model`、`file`、`context_tokens` 和 `max_tokens`，需先启用音频输入能力。

AI 也可用同一本地凭据管理模型：`POST /admin/models` 提交 `id`、`model_path`、可选 `mmproj_path`、`capabilities` 和 `max_context_tokens`；`GET /admin/models/<id>` 查看状态；`PATCH /admin/models/<id>` 带 `expected_revision` 修改总量、路径、投影文件或能力；`DELETE /admin/models/<id>` 取消注册。修改会等待旧请求结束并验证新配置，失败时保留原配置。能力名为 `text_input`、`image_input`、`audio_input`、`text_output`，当前必须包含文本输入/输出。不支持音频生成。

## 查询结果与用量

保存响应头里的 `X-AIToolbox-Request-ID`，也可以在请求时自行提供 `X-Request-Id`。在相应的云端或本地端口上：

- `GET /requests/<id>`：查询持久记录；需要原入口凭据。
- 云端 `GET /requests/<id>/result`：读取已完成的原始响应正文。流式结果保留原 SSE 数据。
- 本地查询记录中的 `response.body_base64` 保存完整响应正文，`usage` 保存上游返回的计量。
- 本地 `GET /admin/usage-dashboard?day=YYYY-MM-DD`：查询同一业务数据库的云端和本地日用量，按北京时间。

断线或超时后先查询原 ID。`UNKNOWN` 表示无法确认完整终态，不能当作成功，也不要盲目重发。已受理的同 ID 请求不会自动再次调用模型。用量未知时保持未知；本地用量最多约 5 秒后出现在图表。

## 你的数据存在哪里

默认在 `%LOCALAPPDATA%\AIToolbox`，可从界面打开。程序文件夹可以移动，数据默认留在该使用者的数据目录。通过 `AIToolbox.exe --data-dir "自选目录"` 可以使用另一套独立数据。

密钥由当前 Windows 用户的 DPAPI 加密；调用凭据只在本机数据目录保存。数据库和收据保存请求输入、输出、状态与用量。备份时先关闭窗口，再复制整个数据目录；复制到另一台机器或另一个 Windows 用户后，通常需要重新填写厂商密钥。不要把使用后的数据目录发给别人或上传 GitHub。

关闭窗口会等待当前调用结束、保存记录并停止本产品服务。再次启动恢复目录和记录，不自动加载模型或重发中断请求。模型在实际调用时加载；中断的注册验证需要显式重试。注册验证完成后释放测试实例。同一数据目录只允许一个实例使用。

端口冲突会给出启动错误；在自己的 `settings.json` 修改 `cloud_port` / `local_port` 后重启。没有自动修改系统 PATH、旧客户端或其他 AIToolbox 部署。

## 已知边界

内置推理使用 CPU，一次加载一个模型。较大的权重需要足够可用内存，速度取决于机器与模型；能启动软件不代表任意权重都能运行。产品不附带模型文件、GPU 驱动或 CUDA 库。

音频注册探针由 Windows 在本机临时合成一句自有测试文本，用后删除；没有携带第三方录音。该验证需要 Windows 已安装英语语音。如提示缺少语音，请在 Windows 语言/语音设置安装英语语音后重新注册；文本和图像使用不依赖语音组件。

云调用需要使用者自己的有效账号、余额/权限与网络。目录中的名称是使用者填写的提示，不代表自动购买权限。测试按钮会实际调用所选模型；云端可能计费。

## 开发者：从源码运行和构建

源码使用 Python 3.11 标准库与 Tk，无 pip 运行依赖。Windows 下先准备固定原生运行时，再启动：

```powershell
python -c "import sys; sys.path.insert(0,'tools'); from build import prepare_runtime; prepare_runtime()"
python src/main.py
```

构建独立 Windows 发行包：

```powershell
python -m venv .build/venv
.build/venv/Scripts/python -m pip install -r requirements-build.txt
.build/venv/Scripts/python tools/build.py
```

构建脚本下载并核对 `config/runtime-lock.json` 固定版本/哈希，不下载模型。输出为 `dist/AIToolbox/` 和 `dist/AIToolbox-Windows-x64.zip`，随包 `manifest.json` 列出文件 SHA256。构建产物不进入源码 Git。

运行工程检查：

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m unittest discover -s tests -v
```

自有代码使用 [MIT](LICENSE)。第三方运行组件按[各自许可](THIRD_PARTY_NOTICES.md)分发，不能把权重、云服务或其他用户资产的权限从本项目许可证推导出来。

本目录是 v10.2.0 源码。已从本源码构建 Windows 测试包、核对文件哈希，并从重新解压的 ZIP 在全新数据目录中空白启动；隔离的 0.8B 模型已登记为 `READY`，一次文字请求和原 ID 收据均完成并记录实际用量。用户独立验收尚未开始。**对外公开分发二进制前，还需确认微软运行库的再分发授权**；附上条款不等于取得该授权。待上传源码不含这些 DLL，详见上述第三方说明。
