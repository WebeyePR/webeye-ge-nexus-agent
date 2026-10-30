# GE 图片内联渲染 —— 调研结论与备选方案

> **文档性质**：调研记录 + 备选方案存档。**当前不采用**，原因见第 6 章（影响流式 + 体积膨胀）。
> **核实时间**：2026-10-30
> **当前生产引擎**：`8648928063754600448`
> **相关实现**：`nexus_agent/tools.py` 的 `render_image_as_artifact`

---

## 1. 现象

在 GE 会话里请求「查看文档 + 预览图片」时：

```
Gemini 已回答
image_1793333574786524.jpg          ← 图片作为独立消息，出现在最前面
Gemini 已回答
image_1793333588790970.jpg
image_1793333589227064.jpg
Gemini 已回答
已为您成功下载并注册相关图片...        ← 正文在最后
                                      正文里的 ![孟凡超头像](render_v2_xxx.jpg)
                                      显示为裸文本，不是图片
```

**问题**：图片跑到顶部、正文里的 markdown 图片标签不渲染。

这是**长期遗留问题**，与 ADK 版本升级无关（见 3.2）。

---

## 2. 结论速览

| 通道 | 能否内联渲染 | 依据 | 备注 |
|---|---|---|---|
| ① artifact（`save_artifact`） | ⚠️ **能显示，但独立成消息** | 线上实测 | 位置由 artifact 挂在哪个人物事件决定 |
| ② markdown 外部 URL（GCS 签名 URL） | ❌ **只成可点击链接** | GE 实战文章（Oracle 数据智能体） | `"Images referenced only as GCS URLs appear as clickable links rather than inline renders"` |
| ③ markdown `data:` URI（base64） | ✅ **可内联渲染** | Google Cloud Japan 官方文章（Agent Engine + GE 实测） | 唯一被证实可行的内联方式 |
| ④ A2UI `Image` 原语 | ❌ 不适用 | A2UI 实测报告 | 必须 A2A agent；我们是 ADK 原生 Agent Engine |
| ⑤ 回复里注入 `inline_data` part | ❓ 未验证 | — | GE 可能把它抽成附件，风险未知 |

**一句话**：`data:` URI 是唯一被官方验证过的内联方式，但它有明确代价（第 6 章）。

---

## 3. 根因分析

### 3.1 两条通道错配

我们的实现（`tools.py` → `render_image_as_artifact`）：

```python
artifact_part = types.Part.from_bytes(data=img_bytes, mime_type=mime_type)
version = await tool_context.save_artifact(filename=artifact_filename, artifact=artifact_part)
return {"status": "success", "artifact_name": artifact_filename, ...}
```

| 我们以为 | 实际 |
|---|---|
| `save_artifact` → 图片在文本流原位显示 | artifact 走 `event.actions.artifact_delta` **独立通道**，GE 渲染成独立消息（附件） |
| `![AltText](render_xxx.jpg)` → markdown 解析成图片 | **ADK 未定义任何"用 markdown 引用 artifact"的语法**，GE 不认裸文件名，只能当文本 |

ADK 官方文档（adk.dev/artifacts）原文：

> 保存动作会在 event 里写入 `artifact_delta`（文件名 → 版本号），**"但文档并未说明任何具体客户端会如何使用它"**
> **"文档没有定义任何 Markdown 引用语法"**

→ 我们 prompt 里那条 `MUST KEEP and REPRODUCE ![AltText](lark_doc_xxx_logo.png)` 的写法**没有任何官方语义支撑**，是自创的。

#### 澄清：ADK 2.13 确实存在 `artifact://`，但**不是渲染语法**

二次核验时在源码中发现 `artifact://`（易误判为"有内联引用方案"），实际用途已查清：

```
artifact_util.py:52-58   定义两种 URI 形态：
  artifact://apps/{app}/users/{user}/sessions/{session}/artifacts/{file}/versions/{v}
  artifact://apps/{app}/users/{user}/artifacts/{file}/versions/{v}
```

**仅在三个后端位置被使用**（均已确认）：

| 位置 | 用途 |
|---|---|
| `artifact_util.py:233` | 解析 `file_data.file_uri`，做 scope 校验（`validate_artifact_reference_scope`）+ 递归深度上限（`_MAX_ARTIFACT_REFERENCE_DEPTH=5`，防自引用死循环） |
| `in_memory_artifact_service.py:140` | `load_artifact` 时解析 URI |
| `gcs_artifact_service.py:329` | 同上 |

→ 它是**服务端的 artifact 间引用机制**（artifact 内容里指向另一个 artifact），**不是给前端渲染的 markdown 语法**。GE 前端不认识这个字符串。

**结论不变**：要让 GE 在正文里内联显示图片，仍然只有 `data:` URI 一条路。

### 3.2 为什么图片在"顶部"而不是"后面"

ADK 模板的 `_convert_response_events`：

```python
result = _StreamingRunResponse(events=events, artifacts=[], session_id=...)
for event in events:
    if event.actions and event.actions.artifact_delta:
        ...   # 把本事件的 artifact 收集进这个响应包
```

**artifact 被打包在"它所属的那个 SSE 响应"里发给 GE**。

我们的 `render_image_as_artifact` 在**工具执行时**就 `save_artifact`，所以：

```
工具响应事件（带 artifact_delta）→ GE 渲染成图片消息  ← 时间上更早
       ↓
模型最终文本事件                → GE 渲染成文字消息  ← 时间上更晚
```

→ 图片自然排在文字**前面**。

**推论**：图片位置**跟着 artifact 所属事件走**，因此顺序理论可调（把 artifact 挂到最终文本事件，可变成"文字→图片"），但**无法做到段落间交错**。

### 3.3 为什么升级 ADK 解决不了

- artifact 机制从 ADK 1.x 到 2.13 **未变**（一直是 `Part` + `inline_data` + `artifact_delta`）
- GE 把 artifact 渲染到哪儿，是 **GE 前端的决策**，在服务端代码之外
- A2UI 实测报告旁证：*"Without A2UI, an agent in Gemini Enterprise has very limited UI capabilities (**it can't even display an image natively**)"*（指文本回复内不能内联）

---

## 4. 官方可行方案（Google Cloud Japan，2026-02-16）

**来源**：<https://zenn.dev/google_cloud_jp/articles/a6c9ba66c06b65>
作者在 **Agent Engine + Gemini Enterprise** 上实测通过，是当时唯一完整的官方实现范例。

### 4.1 三步机制

**① 工具层**：保存 artifact（`user:` 作用域，跨轮可读），返回带文件名的 markdown

```python
image_artifact = Part.from_bytes(data=img_bytes, mime_type='image/png')
filename = f'user:{tool_context.invocation_id}_ref_{ref_id}.png'
await tool_context.save_artifact(filename=filename, artifact=image_artifact)
# 工具返回的 markdown 里写：![图1](user:xxx_ref_1.png)
```

**② `after_model_callback`：文件名 → base64 data URI（关键一步）**

```python
async def replace_filename_with_base64(markdown_text, quality=80, max_width=350, callback_context=None):
    pattern = r'!\[(.*?)\]\((.*?)\)'

    async def replacer(match):
        alt_text, filename = match.group(1), match.group(2)
        image_artifact = await callback_context.load_artifact(filename=filename)
        image_bytes = image_artifact.inline_data.data
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            if max_width and img.width > max_width:      # 限制宽度
                ratio = max_width / img.width
                img = img.resize((max_width, int(img.height * ratio)), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=quality, optimize=True)   # 重压为 JPEG
        b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        # 把原文件名藏在 alt 里，供下一轮还原
        return f'![{alt_text}#{filename}](data:image/jpeg;base64,{b64})'

    return await async_sub(pattern, replacer, markdown_text, flags=re.MULTILINE)


async def callback_load_images(callback_context, llm_response):
    if not (llm_response.content and llm_response.content.parts):
        return None
    for part in llm_response.content.parts:
        if part.text:
            part.text = await replace_filename_with_base64(
                part.text, quality=80, max_width=350, callback_context=callback_context)
    return None
```

**③ `before_model_callback`：下一轮把 data URI 还原成文件名（防历史累积）**

```python
def replace_base64_with_filename(markdown_text):
    pattern = r'!\[(.*?)\]\(data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=]+\)'

    def replacer(match):
        original_alt = match.group(1)
        if '#' not in original_alt:
            return match.group(0)
        parts = original_alt.split('#')
        filename = parts[-1]
        clean_alt = '#'.join(parts[:-1])
        return f'![{clean_alt}]({filename})'

    return re.sub(pattern, replacer, markdown_text, flags=re.MULTILINE)


async def callback_remove_base64(callback_context, llm_request):
    for content in (llm_request.contents or []):
        for part in (content.parts or []):
            if part.text:
                part.text = replace_base64_with_filename(part.text)
    return None
```

**④ 挂到 Agent 上**

```python
root_agent = LlmAgent(
    ...,
    before_model_callback=callback_remove_base64,
    after_model_callback=callback_load_images,
)
```

**⑤ artifact 服务**

```python
def artifact_builder():
    return GcsArtifactService(bucket_name=BUCKET_NAME)
adk_app = AdkApp(agent=root_agent, app_name=..., artifact_service_builder=artifact_builder)
```

> 另有一个社区 Gist 指出：GE 下 `artifact_service` 需要在 `before_agent_callback` 里再次注入才能稳定生效（否则图片会显示两次）。属实现细节，未验证。
> 来源：<https://gist.github.com/enakai00/a80bf3a8106a17cafeecd93881ce4784>

### 4.2 为什么这条路能通，而 ①② 不行

| 通道 | 本质 | 结果 |
|---|---|---|
| GCS URL | 依赖**外部请求**才能取到图片 → 受 CSP / 权限 / 有效期限制 | 只渲染成链接 |
| artifact | 走**独立字段** `artifacts`，与文本流分离 | 独立消息 |
| **`data:` URI** | **图片字节直接内嵌在 markdown 文本里**，渲染器本地解码，零外部依赖 | **内联显示** ✅ |

---

## 5. 实测：base64 体积膨胀（决定不采用的关键数据）

样本取自本项目 `docs/imgs/*.png`，按官方参数（`max_width=350`, JPEG `quality=80`）压缩：

| 原图 | 原大小 | 压缩后 JPEG | base64 后 | 约合 tokens |
|---|---|---|---|---|
| 1280×655 | 161 KB | 7.2 KB | **9.6 KB**（9,848 字符） | ≈ 2,813 |
| 3827×1028 | 365 KB | 4.1 KB | **5.5 KB**（5,632 字符） | ≈ 1,609 |
| 1814×1180 | 110 KB | 8.4 KB | **11.2 KB**（11,472 字符） | ≈ 3,277 |

- **单张平均 ≈ 2,500 tokens 等价、≈ 9 KB 文本**
- **10 张图 ≈ 25,000 tokens、≈ 90 KB**
- base64 相对二进制固定膨胀 **≈ 33%**

> token 数为按 3.5 字符/token 的粗估，用于量级判断。

---

## 6. 为什么当前不采用（决策记录）

| # | 问题 | 说明 |
|---|---|---|
| 1 | **与流式冲突** | 模型输出是碎片（partial），`![图1](文件名)` 可能被切到多个事件，**碎片内无法可靠替换**；必须在 final 事件做完整替换。而我们的去重逻辑会把 final 文本清空 → 冲突。要让图片生效，得让「含图回复」整轮放弃逐字流式（一次性返回），牺牲打字机效果 |
| 2 | **体积膨胀** | 单张 ≈ 9 KB / 2,500 tokens；10 张 ≈ 90 KB。响应体积与传输成本显著上升 |
| 3 | **历史污染风险** | 若 `before_model_callback` 还原逻辑出任何纰漏，base64 会进入会话历史，**下一轮 prompt 成倍膨胀**，且难以事后清理 |
| 4 | **多一处失败点** | 压缩/编码/还原三处都要正确，任一环节异常就可能：图片不显示、正文缺字、或 token 暴涨 |
| 5 | **收益有限** | 图片本身**已经能显示**（artifact 附件），只是位置不理想 |

**结论**：当前收益（位置美学）与代价（流式、体积、稳定性）不成比例。**暂不采用。**

---

## 7. 若将来启用：实施清单

### 7.1 必须先验证（否则不要动工）

1. **GE 当前版本是否仍渲染 `data:` URI** —— 官方文章是 2026-02 的，GE 前端可能变过。用一个最小探针实测。
2. **data URI 长度上限** —— 单条消息含多张图（如 10 张 ≈ 90 KB）是否会被截断。
3. **单条消息内多个 data URI 的渲染** —— 是否都显示、顺序是否正确。

### 7.2 实施要点

- 新增 `nexus_agent/callbacks.py`（当前项目**没有**任何 model callback）
- `after_model_callback`：只在 `llm_response.partial is False` 时替换（避开流式碎片）
- `before_model_callback`：无条件还原（哪怕只漏一次也会污染历史）
- 压缩参数：`max_width=350`、`quality=80`、转 RGB、`optimize=True`
- 加硬上限：单张 > N KB 则放弃内联，退回附件 + 文字说明
- 存量 artifact：`filename` 需带 `user:` 前缀才能跨轮读取
- 降级策略：任何异常都回退到「纯文件名 + 附件」原行为，绝不阻断回复

### 7.3 与现有逻辑的耦合点（改动前必读）

- **`_dedupe_final_text`**（`streaming_app.py`）：final 文本被清空 → 图片替换必须放在清空**之前**，或让含图回复走独立路径
- **`render_image_as_artifact`**：需保证 artifact 以 `temp:`/`user:` 正确作用域保存
- **prompt 的 GOLDEN RULE**：必须同步改写（见第 8 章），否则模型会继续输出裸文件名

---

## 8. 零风险改善：已落地（2026-10-30）

不启用 data URI 的前提下，已按本节第 1、3 条完成改动（**只改文案，不动渲染机制**）：

### 8.1 已完成

1. **prompt 金科玉律已替换**（`nexus_agent/agent.py` 第 3 节）
   - 旧：`MUST KEEP and REPRODUCE the exact same inline image tags (![AltText](lark_doc_xxx_logo.png))` + 每图一条斜体脚注
   - 新：正文用 `（图1）`、`（图2）` **编号占位**（位置仍在原段落），文末一行
     `🖼️ 图片对照：图1 = lark_doc_xxx.png；图2 = …（可在右侧「产物 / Artifacts」面板点击查看原图）`
   - 并**明令禁止**输出任何指向 artifact 文件名的 markdown 图片语法（GE 会渲染成裸文件名）
   - 保留原有约束：不得丢弃图片、不得把所有占位堆到开头/结尾
2. **工具输出同步修正**（`get_lark_document_markdown` 的 `download_images=True` 分支）
   - 旧：每张成功图被替换成 `![alt](lark_xxx.png)` + 一段脚注 —— 与新 prompt 规则**自相矛盾**
   - 新：替换为 `（图N）`，文末追加对照表；**仅成功下载的图参与编号**，失败图不占号（避免「图1、图3」断号）
3. **话术校正**：主动邀请语从「原地内联排版」改为「右侧「产物 / Artifacts」面板逐一呈现、正文按（图N）标注」

**验证**：新增 2 项测试（全成功 / 部分失败），断言「无 `![` 语法」「编号连续且与原文顺序一致」「失败图保留原始直链」。全套 185 passed + 1 skipped。

### 8.2 明确未做（附原因）

| 项 | 原因 |
|---|---|
| 把 `save_artifact` 从工具执行时推迟到最终回复阶段（原第 2 条） | `artifact_delta` 只能挂在产生它的工具事件上，延后等于改工具契约；且「文字先于图片」是否成立需 GE 真机验证，**不属于零风险** |
| 下载失败兜底仍输出 `![alt](原始URL)` | GE 对原始飞书直链大概率同样渲染为裸文本，改成普通链接 `[图](url)` 更佳；但这属另一条通道，需先确认外部链接在 GE 的渲染行为，不在本次范围 |
| `download_images=False`（默认模式）原文中的 `![]()` 标签 | 该模式本就不替换图片，模型可能照抄。当前 prompt 的禁止条款只针对 artifact 文件名，未扩展到原始 URL —— 避免在无证据时过度约束 |

---

## 9. 参考资料

| 来源 | 内容 | 可靠性 |
|---|---|---|
| <https://zenn.dev/google_cloud_jp/articles/a6c9ba66c06b65> | **Google Cloud Japan 官方**：base64 data URI 图文交错完整实现（Agent Engine + GE 实测） | 高（官方 + 实测） |
| <https://adk.dev/artifacts/> | ADK 官方：artifact 存储机制；**无 markdown 引用语法**；客户端自行决定渲染 | 高（官方） |
| <https://gist.github.com/enakai00/a80bf3a8106a17cafeecd93881ce4784> | GE 下图片显示两次的规避（`artifact_service` 插件注入） | 中（个人 Gist） |
| <https://a2uicatalog.ai/blog/002-gemini-enterprise-field-report-a2a-a2ui-part1> | A2UI 实测：GE 只认 v0.8 的 18 个原语；**A2UI 需 A2A agent**；无 A2UI 不能原生显示图片 | 中高（实测报告） |
| <https://social.ora.cl/6040BBZC1M> | Oracle 数据智能体在 GE 的实战：**GCS URL 只成链接**，binary blob 才渲染 | 中（厂商方案文） |
| <https://github.com/google/adk-python/discussions/4404> | GE 对 `text/html` artifact 报 `Unsupported attachment`（1.21.0） | 中（issue） |
| <https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/image-generation> | Google 官方：Gemini 支持 `responseModalities=["TEXT","IMAGE"]` 图文交错输出 | 高（官方） |

---

## 10. 一句话总结

> 图片能在 GE 显示，但**只能以 artifact 附件形态**；要真正图文交错，唯一被官方验证的路径是**把图片 base64 内嵌进 markdown**——代价是牺牲流式并让响应膨胀约 9 KB/张。
> 当前判断：**不值得**。先按第 8 章做零风险改善。