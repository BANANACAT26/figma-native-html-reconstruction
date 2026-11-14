# 验收与证据口径

## 验收是矩阵，不是一个百分比

| 维度 | 必查内容 | 不能替代它的指标 |
|---|---|---|
| 内容完整性 | live 可见节点、文本、素材、替代关系 | mismatch 低 |
| 绝对几何 | 位置、尺寸、裁剪、坐标空间 | 截图“看着差不多” |
| 相对几何 | 源/目标边、端点、中心偏移、间距 | 各自绝对坐标合格 |
| 文本 | 字符、行盒、折行、重叠、overflow、字体实际加载 | outside-text |
| 像素 | raw、outside、text-region、SSIM、热点 | 单次旧截图 |
| 实际视觉 | 整页、局部、独立页、总览 fit | DOM 节点存在 |
| 追溯 | 输入/输出哈希、版本、候选与回退 | 无时间戳的 metrics |

## Canonical 截图

1. 读取 reference 物理宽高并作为 viewport。
2. DPR=1，禁用移动仿真，浏览器与项目基线一致。
3. 等网络、图片、字体稳定。
4. 验证 stage origin=(0,0)、尺寸=(W,H)、scale=1。
5. 验证 document 没有超出画板的意外滚动区域。
6. 截图后再次检查 PNG 物理尺寸。

不要用缩放后的 reference 或 browser 图“对齐”。尺寸不一致表示基准错误。

## 像素指标

本次实践的主口径：

```text
mismatch pixel = max(abs(deltaR), abs(deltaG), abs(deltaB)) > 20
mismatch fraction = mismatch pixels / region pixels
```

- `raw`：全图，不被 mask 修改。
- `text_region`：真实 DOM 文本 mask 内，用于观察字体/抗锯齿。
- `outside_text`：mask 外，用于观察结构与非文字素材。
- `SSIM`：感知结构相似性，需与像素指标和视觉一起读。
- `hotspots`：连通差异常块，用来定位组件，不用来替代组件理解。

20/255 是本次项目选择，不是所有任务的通用标准。1%、2% 也只能在相同口径下比较。

## 文本审计

使用真实行盒而非只看外层 div：

- `unexpectedWrap`：原设计无此换行，浏览器新增视觉行。
- `nonDesignOverlap`：不同文本的真实行盒发生原设计没有的明显相交。
- `overflow`：scroll 尺寸超过 client 尺寸；固定文本盒的纵向溢出可能是设计允许的行盒超出，单列 `designBoxOverflow`，最终可见性看祖先 `clipsContent` 与有效 clip。横向 overflow、内容尺寸模式 overflow 仍按硬错误处理。
- `textErrors`：字符缺失、错误、重复或错误映射。
- `colorAudit`：在 stage 背景亮度下报深底深字嫌疑、manifest 无 fills 嫌疑。

同一视觉行内多个 run 先聚类；同源行盒不互相判重叠。设计内显式重叠、nowrap 和裁切要有证据后排除，不能一律报错。

**逐 run 字号审计**：每个 run 渲染为 `<span data-run-index>`，审计按 index 把 DOM computed font-size 与期望 run 字号逐 run 对比（偏差 >2px 才报 `fontSizeMismatch`）。mixed 节点逐 run 正确时不得误报（不能拿容器字号去比 run 列表）；任一 run 错仍杀。`fontSize='mixed'` 只在每个 run 都带数值字号时才可消费；缺数值/非数值一律硬失败，**不回退 16px 默认**。`transparentButSolid` 单独 bucket：design 有可见 SOLID fill 但 DOM computed color 透明即报。

**AUTO line-height 与校准行计划**：AUTO 是合法 live 值。无实测时输出 `line-height:normal` 并标 unresolved，禁止盒高/行数/倍率伪造。若 `layout-plan.json` 带原生 PDF 校准 `linePlan`，审计必须同时比对 `.cal-line` 的逐行文本与 TEXT Node Range 行盒数；不一致进 `linePlanMismatch` 硬失败。行计划只证明断行与基线，不替代有效裁剪验证。

**wrap/overflow=0 不等于文字视觉通过**：折行和盒溢出为 0 时仍须检查 colorAudit——黑字落在深色画板上、文本缺 fills 被猜成颜色，都是 near-invisible 缺陷，行盒审计完全正常也会漏。文本填充分类 none/solid/gradient/image，无可见填充渲染透明并披露，绝不猜 `#000`。

## 保留/回退判定

一个候选只有在以下条件同时成立时才保留：

- 用户指出的目标缺陷已闭合。
- live 几何未出现未批准恶化。
- 同基准像素未出现不可接受回退。
- 文本没有新增错误。
- 独立页和总览实际画面都没有回归。

若 live 与旧 reference 冲突，单列版本冲突并请求/遵循用户选择，不能称为普通通过。

## 证据完整性

`run.json` 至少包含：

- HTML、reference、browser、metrics 的 SHA-256，以及显式 `hash_inputs` 列表。
- viewport、DPR、浏览器信息、时间。
- raw 与 outside 指标。
- 输入文件路径和输出目录。

哈希契约为**外部产物绑定 + sidecar 自摘要**：run.json 只绑定外部文件摘要，绝不把自己的哈希内嵌进自身（写完再回写会让内嵌值指向中间态、自举不自洽）。run.json 自身摘要写到 sidecar 文件 `run.json.sha256`，使 `sha256(run.json) == sidecar 内容` 可独立验签；manifest 里声明 `self_digest_file` 指向该 sidecar。

每次正式复验都重新截图，避免 metrics 指向旧 browser.png。汇总文件也要标范围：只跑了一页时，不得把它表述成全量 0 错误。跨 run 比较必须同口径（阈值、区域、mismatch 算法），首轮与修复后数字分开报告。

## 连接器验收（独立门 E）

连接器逐条量外框与真尖端，由 `audit_connectors.py` 机读判定（契约见 `connectors.md`）：

| verdict | 含义 | tipVerified | strict 下是否过 |
|---|---|---|---|
| `pass` | SVG 用 `path.getPointAtLength(totalLength)` 测得的真实端点落在 target/source 边中点（≤ 公差），线体框也到位 | true | 过 |
| `degraded-bbox` | PNG 外框到达两侧边中点，但尖端不可见 | false | **不过**（须显式 `--allow-degraded-bbox` 才放行） |
| `edge-gap` | 连接器外框未到达指定边 | false | 不过 |
| `tip-miss` | SVG 尖端测得但偏离目标边 | false | 不过 |
| `stage-outside` | 连接器不在承载 fit/scale 的 `.stage` 容器内 | false | 不过 |
| `invisible` | 零尺寸 / stroke-width=0 / display:none / 无描边路径 | false | 不过 |
| `missing-asset` | selector 找不到元素 | false | 不过 |
| `unverifiable` | source/target 选择器缺失或不匹配 | false | 不过 |

要点：

- **SVG 真尖端 vs PNG 外框**：SVG 用 `getPointAtLength` + `getScreenCTM()` 测真实箭头端点；PNG 只能测外框，外框到达边**不能**冒充尖端验证。
- **stage/fit/marker/stroke 总览可见**：连接器必须在 `.stage` 内参与 fit transform；computed stroke-width > 0、路径数 > 0、非 display:none，总览 fit 后箭头头部与线体仍可见。
- 缺 source/target **不判过**，直接 `unverifiable`；未知证据一律 `unverifiable`，不静默通过。
- 禁止用多条 gap 的平均残余误差掩盖某一条仍错位。
- **画板无连接器时**：必须用 `connectors.manifest.json` 证明 `connectorCount == 0`；没有该 sidecar 也没有 spec，本门为 `unknown`（操作员须显式确认清单为空），不能跳过。

## live 复验串行

同一 Figma 编辑器会话串行处理 tools/call。live 复验（读取节点树、导出、截图）对同一 session id 必须串行排队，禁止两路并发；并发会让 get_node 在插件端排队数十分钟无返回并污染耗时指标。多画板 live 复核复用会话逐板串行，或换独立 session。

## 阶段门状态契约

每道门（A–G）机读状态取 `pass | fail | unknown`：

- `unknown` 表示证据未采集（缺 plan、缺素材、未做人工视觉），**绝不等于 pass**。
- `reconstruct.py` **默认 strict**：任一门 `fail` **或** `unknown` 都非零退出，summary 列出 `failedStages` 与 `unknownStages`；`--no-strict` 只放松退出码。
- 前置门 `fail` 先修对应类别，不得用后续门的好看指标抵消。
- **禁止用全局像素均值掩盖局部缺失**：内容少渲染、连接器缺尖端、文字近不可见、关系边错位，都不能被 raw 百分比好看抵消。
- 门 D/E 的默认一次确定性修复（text repair / connector fallback）与复验在 summary `repairs` 字段留痕；修后数字与首轮数字分开报告。

## 停止条件

达到用户目标且所有验收门通过即可停止。若目标未达：

- 列出剩余差距与主导热点。
- 说明是字体、位图内容、抗锯齿、版本冲突还是测量限制。
- 已回退的失败实验不计为正式改善；修复方向为负时如实写明。
- 不用整板图、文字图片化或隐藏内容凑过门槛。
- **不承诺任意 Figma 文件必然达到 1%**：精确字体、烘焙位图内容/色彩/抗锯齿、live/reference 版本冲突会形成下限；SOP 保证的是可定位、可审计、可回退，不是未经证据的数值达标。
