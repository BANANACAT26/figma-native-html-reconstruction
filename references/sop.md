# 完整制作 SOP

## 目录

1. 输入与验收合同
2. 阶段门总览（A–G，机读 summary）
3. 画板清单与版本冻结（门 A）
4. 节点树与导出单元（门 B/C 输入）
5. 固定画板 HTML 构建
6. 门 B：内容可见性与覆盖
7. 门 C：层级/几何
8. 门 D：文字/素材
9. 门 E：独立连接器
10. 门 F：canonical + 总览视觉
11. 门 G：像素回归与修复闭环
12. 推荐目录和记录模板

## 1. 输入与验收合同

先写出一页任务合同，避免做到一半改变网页形态：

- Figma 文件、页面、画板范围与目标 HTML 数量。
- 每个画板的物理尺寸和 origin；是否允许不同尺寸。
- 真实 DOM 文本、独立非文字素材、可编辑程度。
- 是否禁止整板图像、是否禁止响应式重排。
- 字体授权与可用字体来源（缺字体怎么办见第 4.3 节与 `references/font-sourcing.md`：**先去下载落地，不承诺"留挂载点"**）。
- 用户数值目标及其口径，例如 outside-text mismatch ≤2%；不要自行把 raw 与 outside 混用。
- 独立页、连续总览、目标浏览器、DPR、截图环境。
- 明确哪些用户截图只是证据，不得作为网页内容。

若用户否决整板图或响应式重构，后续候选方案不得偷偷回到这些路线。

## 2. 阶段门总览（A–G，机读 summary）

每道门产出一份机读报告 JSON，写入 `evidence/`。每道门状态取三值之一：

- `pass`：该类证据采集齐全且未发现硬失败。
- `fail`：该类发现硬失败（具体字段在报告里列出）。
- `unknown`：该类证据未采集（缺 spec、缺素材、未做人工视觉），**绝不等于 pass**。

前置门 `fail` 时先修对应类别，不得进入下一道门凑指标。**禁止用全局像素均值/raw 百分比掩盖某一类局部缺失**：内容少渲染、连接器缺尖端、文字近不可见、关系边错位，都不能被 raw 好看抵消。`reconstruct.py` **默认 strict**：任一门 `fail` 或 `unknown` 都非零退出并在 `summary.json` 列出 `failedStages` / `unknownStages`；`--no-strict` 仅放松退出码。

默认管线（`reconstruct.py` 一条命令串起）：`extract → normalize → analyze → plan → [P0 data_gate] → build → verify → repair → verify`。SDK/MCP 导出的 manifest 与 PNG 只是输入；`compile_design.py` 产出的 `layout-plan.json`（含 planHash sidecar）是唯一构建依据，`build_fixed_html.py` 只消费 plan。Agent 不对图排版，reference PNG 只做最终视觉验收。

**P0 数据门（compile 后、build 前，先于 A–G）**：`data_gate.py` 在 build 之前机读检查 manifest→IR→plan 契约——rootFills 链完整且分类正确（null=未知阻断 / []=显式无填充 / [...] =合成）、mixed 字号每个 run 有数值（无 16px 默认冒充）、AUTO line-height 未用盒高/行数伪造（`lineHeightSource='resolved'` 即伪造）、每个 rendered 节点有画板坐标 render 盒、资产带 pngPlacement+exportSize、无 corner-reprojected 盒。失败写 `evidence/data-gate.json` 并在 strict 下非零退出、拒绝 build。这是构建前硬门，不通过就不进入 A–G。

| 门 | 名称 | 主脚本 | 硬失败判据 |
|---|---|---|---|
| P0 | 数据契约（build 前） | data_gate.py | rootFills 未知/缺字段、mixed 缺数值字号、AUTO 伪造、render 盒/资产字段缺失 |
| A | 基准/版本 | verify.py | reference 缺失、viewport 与 reference 物理尺寸不一致 |
| B | 内容可见性与覆盖（浏览器实测） | audit_content.py --browser-audit | ledger unresolved、effective-visible 节点未渲染、hidden/transparent 多渲染、真实 computed 可见性未全过 |
| C | 全关系图（默认） | audit_relations.py | 任一关系边 fail；strict 下 unknown 也不算过 |
| D | 文字/素材（失败自动确定性修一次） | audit_text.py + repair_text.py | 真实行盒新增折行/重叠/overflow、字体未实际加载、colorAudit 硬嫌疑；一次确定性修复（不缩 font-size）后复验仍 fail |
| E | 独立连接器（strict 失败自动 fallback 一次） | audit_connectors.py --strict + repair_connectors.py | 非 tip-verified SVG pass；unverifiable 阻断；一次全 SVG 重建（备份/回滚）后复验仍 fail |
| F | canonical+总览视觉（要真实证据） | verify.py + --overview-evidence | 总览 fit 后 stage/fit/marker/stroke 不可见；未提供记录证据则 unknown |
| G | 像素回归 | verify.py / pixel_diff.py | 同 DPR/viewport 下 raw/outside/text-region 超合同阈值 |

## 3. 画板清单与版本冻结（门 A）

### 2.1 live 清点

从 live 页面读取顶层 children，按类型过滤真正画板。记录：

```json
{
  "name": "<画板序号>",
  "node_id": "<figma-node-id>",
  "type": "FRAME",
  "x": 0,
  "y": "<同排 y>",
  "width": "<项目合同宽度>",
  "height": "<项目合同高度>",
  "reference_path": "_reference/<node-id-slug>.png",
  "design_data_path": "_design/<frame-slug>.json"
}
```

名称与顺序分开保存。真实案例中顶层对象数常多于真正画板数，多余对象是画布箭头等非画板节点；画板编号也不一定从 1 开始，不能凭序号猜节点。

### 2.2 双源校验

用两种信息核对清单，例如 `get_document` 与 `get_nodes_info`。核对数量、ID、名称、尺寸和参考图文件哈希。所有生成器只消费这一份 manifest，禁止在多个脚本各写一份映射。

### 2.3 版本规则

- live Figma：当前结构、可见性、几何与组件关系的事实来源。
- 静态 reference：某一时刻的像素基准，不自动更新。
- 缓存设计数据：仅用于辅助分析，不覆盖 live。

当 live 与 reference 不一致，标记 `version_conflict`，分别记录“对 live 几何的影响”和“对旧 reference 的像素影响”，由用户决定目标；不要悄悄选一个。

## 3. 节点树与导出单元

### 3.1 一次读取，五份清单

遍历每幅画板并生成：

1. `assets`：独立非文字导出单元、z 顺序、透明度、渲染边界、裁剪。
2. `texts`：字符、runs、字体、字号、行高、字距、对齐、自动resize、显式换行。
3. `containerVisuals`：含文字容器自身的填充、描边、圆角、阴影、背景图。
4. `connections`：源组件、目标组件、连接边、路径、箭头头部、节点标记。
5. `anomalies`：旋转、实例溢出、复杂蒙版、字体缺失、版本冲突。

所有清单字段都要按 null 安全消费：`zIndexChain`、`fills`、`strokes`、`effects`、`lineHeight`、`letterSpacing` 等可能为 `null`/缺省，缺失一律按空链/无填充/默认值处理，绝不把 `None` 塞进元组或算术运算导致构建崩溃。同时把每条导出单元的披露项（unresolved IMAGE、不支持填充、未验证旋转）单独累积成 `disclosures`，随 manifest 落盘。

### 3.2 最大 TEXT-free 子树

从画板根部向下：

- 遇到 TEXT：交给文字清单，不导出为资产。
- 节点子树不含 TEXT：尽量整体原生导出。
- 节点子树含 TEXT：保留容器自身视觉记录，继续拆子节点。
- 任一级 `visible=false`：按继承可见性排除，但记录；live 可见却被遮挡的节点不能仅凭属性删除。

这样既保留复杂合成效果，又减少数百个无意义碎片。

### 3.3 原生导出

优先 `exportAsync` 小批量导出 PNG/SVG。位图适合复杂光影和合成组；SVG适合路径、箭头、简单连线。保留原始导出尺寸与文件哈希。2x 素材只能按原 CSS 尺寸显示，不得改变几何。

只读 Dev Mode 下不要依赖临时隐藏文字、clone 或修改原稿；显式选择 TEXT-free 节点导出即可。

导出阶段沿祖先链累积仿射矩阵（local-to-parent，列序 `[a,b,c,d,tx,ty]`），再对四角取 min/max 得旋转 AABB。两条序列化约定必须显式处理：

- **GROUP 子节点位于 GROUP 的父空间**，不要再给子节点叠加 GROUP 自身位移，否则产生巨大假偏差；子节点直接继承父仿射。
- **INSTANCE 的导出 PNG 可能覆盖子树渲染并集**：当 PNG 像素尺寸与子树渲染并集在容差内一致时，按子树渲染并集锚定放置；否则按节点自身渲染盒锚定。不要在可能很小的 instance 框上做中心锚定，也不要写任何固定补偿偏移。
- 旋转节点的 CSS 盒用未旋转 layout box，PNG 内容已烘焙旋转；二者通过各自口径分别记录，不混用。

## 4. 固定画板 HTML 构建

### 4.1 stage

```html
<div id="viewport">
  <div id="stage"><!-- absolute layers --></div>
</div>
```

```css
#stage{position:relative;width:var(--artboard-w);height:var(--artboard-h);overflow:hidden;transform-origin:0 0}
.layer{position:absolute;display:block;max-width:none}
```

stage 在 canonical 验收时必须 scale=1。展示页可以整体 transform scale，但 viewport 的宽高必须同步为 `W*scale`、`H*scale`。

### 4.2 几何映射

分别保存而不是混用：

- `designBox/cssRect`：未旋转布局盒。
- `renderAABB`：变换后的轴对齐渲染边界。
- `absoluteRenderBounds`：包含实际渲染内容时的边界。
- `pngPlacement`：导出图片内容应放置的位置。
- `exportSize`：图片实际像素对应的 CSS 显示尺寸。
- `effectiveClipRect`：所有有效祖先裁剪的交集。

旋转 PNG 已把旋转烘入内容时，不要再次 CSS rotate。亚像素位置可用整数 left/top + translate 小数，或保留小数定位，但同项目统一。

所有旋转/嵌套层优先使用节点累积的全仿射矩阵（absoluteTransform）一次性输出通用 CSS `matrix(...)`，transform-origin 设为 (0,0)，让局部 (0,0) 精确映射到画板坐标。不要退化成"local 盒 + CSS rotate"——后者在任意非零旋转角下都会产生局部漂移，也不要为某个具体角度写特判。清单里缺 absoluteTransform 却带旋转角的文本，只能按渲染 AABB 放置并记 `rotation-unverified` 披露。

### 4.3 文本

- 每个文本节点保留 `data-n`、原始字符和样式 runs。
- 行高按 AUTO/PERCENT/PIXELS 映射；字距按 em/px 映射。AUTO 无实测时保持 normal/unresolved；若提供独立 Figma 校准节点的原生 PDF 行计划，编译器先验证行文本拼接与 live 字符完全一致，再以真实基线差确定行高，并逐行生成真实 DOM。
- 导出保留 `clipsContent`，编译器向所有后代传播有效裁剪；文本可超出自身固定盒，但不得越过祖先 Frame。不得通过删文本或写死高度模拟裁剪。
- `textAutoResize`、显式换行与文本框宽度共同决定 white-space：含 `\n` 用 `pre`、WIDTH_AND_HEIGHT 用 `nowrap`、其余 `pre-wrap`；不用全局 `pre` 或 `break-word` 粗暴处理所有文本。
- 文本填充分类渲染：无可见填充 → `color:transparent` 并披露；多 SOLID → Porter-Duff over 合成成一个颜色；GRADIENT → `background-clip:text`；IMAGE 填充无法重建为真实 DOM 文字时披露并渲染透明。**任何分支都不得猜成 `#000`**——深底黑字是近不可见缺陷。
- 容器自身的多 SOLID 背景先合成成一层颜色；半透明纯色压在 IMAGE/渐变之上时，该纯色必须输出成一层（可用 `linear-gradient(c,c)` 平层），不能只写一个裸颜色，否则非法逗号列表会让整层背景被 CSS 静默丢弃。
- 垂直对齐可用 table/table-cell 等固定盒方案，但审计时区分外层布局盒与真实行盒。
- 字体先检查实际 `document.fonts` 与渲染回退，不以 CSS 写名字视为已加载。
- **缺字体先获取，不是先披露**：从 `text-manifest.json` / `layout-plan.json` 的 per-run `fontName` 取真实需求（`fontName='mixed'` 必须展开 runs），扫本机 → 上网按确切文件名/PostScript 名搜 → MD5 与 nameID 校验 → 落进 `fonts/` 并把 `@font-face` 的 family 写成与 Figma `fontName.family` 逐字一致 → 用 `scripts/check_fonts.py` 与浏览器 `document.fonts.check()` 证明生效。流程与站点坑见 `references/font-sourcing.md`。
- 两个会让"字体已下载却未生效"的错误：① 复用导出目录时跳过 build，导致复验的是旧 HTML（`@font-face` 计数为 0）；② 改了 export/compiler 却不删 `layout-plan.json`，plan 未刷新。字体改完必须重 build 并复核 `@font-face` 条数。

## 6. 门 B：内容可见性与覆盖

`audit_content.py` 交叉核对三份输入：`manifest.json`（assets / containerVisuals / contentLedger）、`text-manifest.json`（effective-visible TEXT）、构建出的 HTML（实际出现的 `data-n`）。

机读报告写入 `evidence/content-ledger.json`，硬失败包括：

1. ledger 里任何 `disposition == "unresolved"`（未解析 IMAGE / 不支持填充未披露）。
2. effective-visible 的 asset / container / TEXT 节点在 HTML 里**缺 `data-n`**（构建器静默丢节点）。
3. effective-visible TEXT 在 HTML 里缺失（text coverage 漏映射）。
4. 被标记 `effectiveVisible=false`（invisible / transparent）的节点**却在 HTML 里多渲染**（hidden over-render）。

`--strict` 下 `hardFailures > 0` 即非零退出；默认还带 `--browser-audit`：在真实 Chromium 里逐节点取 computed display/visibility/opacity、祖先 clip/mask、stage 相交与可见像素，任一类不过即 blocked。HTML 里有 manifest 未声明的 `data-n` 只算 soft warning（遗留 fixture 装饰节点可能存在）。

**前置门失败先修对应类别**：内容门挂了不要去调像素，也不要用 raw 好看掩盖"某节点没渲染"。

## 7. 门 C：全关系图（默认 audit_relations，不再要求手工 geometry-spec）

默认门 C 不再要求手写 `geometry-spec`。`reconstruct.py` 先在真实 Chromium 里跑一次 MEASURE_JS，把所有 `[data-n]` 节点的 `getBoundingClientRect()`（相对 stage）、computed style、matrix、Range 行盒/基线采集成 `evidence/measured.json`，再由 `audit_relations.py --export-dir <export> --measured measured.json` 与 `layout-plan.json` 的计划边逐条 diff。

覆盖的关系边（与编译器 contract 一致）：

- `parentChildInset`：子渲染盒相对父容器四边的 inset。
- `siblingGap` / `siblingOrder`：同级间距与 z/顺序。
- `alignmentAxis`：同级轴线对齐。
- `baseline` / `iconHostCenter` / `textLineInset`。
- `connectorEdge`：连接器接边。

绝对坐标各自正确而相对关系错误，仍不通过。不可比情形（旋转 vs 未旋转盒族、选择器缺失、Range 行盒缺失、autoLayout 字段缺失）一律 `unknown`——**strict 下 unknown 不是 pass**，且机器可读地落在 `relations-audit.json` 的 `edges[].reason`。没有 `layout-plan.json`（compile 被跳过或未产出）时，门 C 直接 `unknown`，不进入"凭两个绝对位置就算过"。

`--geometry-spec` 仅作为遗留补充证据运行 `audit_geometry.py`，结果挂在门 C 的 `geometrySupplement` 字段，不替代关系图判定。

## 8. 门 D：文字/素材

`audit_text.py` 用 TreeWalker 逐 TEXT Node 建 Range，用 `Range.getClientRects()` 读取真实文字行盒（不选取整个外层 block，避免把多行合并成一个大盒），聚合同一视觉行内不同 run 的 rect，再检查：

- 与原始显式换行相比是否新增行（`unexpectedWrap`）。
- scroll/client 是否 overflow；固定盒纵向溢出记录为 `designBoxOverflow`，可见性由祖先裁剪决定；横向或内容尺寸模式 overflow 为硬失败。校准 `linePlan` 还要逐行核对文本与行盒数。
- 不同 TEXT 的真实行盒是否新增相交（`nonDesignOverlap`）。
- DOM 是否重复、原稿隐藏文本是否误显示、一个文本是否被拆成多份。
- coverage：每个 effective-visible TEXT 必须映射到 DOM 节点；缺了进 `missingInDom`。

colorAudit 规则（**gradient 透明填充不误报**）：

- 设计 manifest 说 GRADIENT 的文本，DOM 用 `color:transparent` + `background-clip:text` 是**正确**渲染，不是近不可见嫌疑；若 DOM 没有 `background-clip:text` 才报嫌疑。
- IMAGE 填充、无可见填充（`fills` 为空）的文本无法在无 reference 像素时判色，报 `unknowns`，不猜 `#000`。
- SOLID 文本才在 stage 背景亮度下报"深底深字"嫌疑。
- **wrap/overflow=0 不等于视觉通过**：行盒干净但 colorAudit 有硬嫌疑，本门仍 fail。

**默认一次确定性修复**：门 D 失败时，`reconstruct.py` 自动用 `repair_text.build_repair_plan(plan.textPlans, lineAudit, fontsMissing)` 生成机读修复计划（`evidence/text-repair-plan.json`），只把 `autoApplicable` 的 `setTextLayout` 声明（white-space / line-height / width 等）确定性合并进对应 `[data-n]` 的 inline style——**绝不重写 font-size**（缩字消 overflow 被 contract 明令禁止）——备份原 HTML 后重跑一次 `audit_text.py` 复验。修一次仍 fail 就如实 fail，不第二轮扫参。缺字体（R-FONT-1）**先按 3.6 / font-sourcing 下载并落地字体再复验**，落不了才进 blocked 并披露来源尝试与回退字体，不静默回退系统字体。

## 9. 门 E：独立连接器

门 E 用独立模块 `audit_connectors.py`（契约见 `connectors.md`），与门 C 的外框初筛互补——门 E 验证**真正的箭头尖端**。

前置导出：连接器节点用 `export_connector_svg.py` 导出为独立小节点 SVG（线体+拐点+圆点+箭头头部一体）。**禁止整板 SVG**（会烘焙文本/位置）；连接器子树含 TEXT 时拒绝烘焙并披露。

审计规则（逐条，不整组平移、不跨条平均）：

- 优先 SVG：用 `path.getPointAtLength(totalLength)` 取最长描边路径的真实端点，经 `getScreenCTM()` 映射到视口坐标，与 target 边中点比距离。箭头端在 `arrowheadAt` 指定的那一侧。
- PNG 回退只能测外框到两侧边中点的 gap，结论为 `degraded-bbox`，**tipVerified=false，strict 下不算 pass**——PNG 外框不能冒充 SVG 真尖端。
- 连接器必须位于承载 fit/scale transform 的同一个 `.stage` 容器内；放在 stage 外（如 `</section>` 之后）不随总览缩放，fit 后消失，判 `stage-outside`。
- 必须可见：非零 bbox、computed stroke-width > 0（或有填充箭头头部）、非 display:none / visibility:hidden。
- 缺 source/target 选择器或证据 → `unverifiable`，**绝不静默判过**。
- verdict 枚举：`pass`（SVG 尖端接边，tipVerified=true）/ `degraded-bbox` / `edge-gap` / `tip-miss` / `stage-outside` / `invisible` / `missing-asset` / `unverifiable`。

strict 门（`--strict`，默认从严）：只要不是 tip-verified SVG pass，全部阻塞；`--allow-degraded-bbox` 显式放行 PNG-only（其余硬失败仍阻塞）。spec 结构错误（缺 selector/边、边名非法）退出码 3。

**默认一次 fallback 重建**：门 E `--strict` 失败时，`reconstruct.py` 自动跑一次 `repair_connectors.py`（以 `layout-plan.json` 为 connector plan）做全 stage 坐标 SVG 重建、源/真尖端吸附到计划边、水平/竖直/共享主干、消除新旧双显；它自带备份与回滚——修复后不能全边通过或引入其他回归则回滚原 HTML。重建后自动复验一次门 E。`unverifiable`（缺 source/target）被 repair 跳过并阻断，**绝不平均成 pass**；一轮后仍 fail 就如实 fail。

**画板无连接器时**：必须用 `export_connector_svg.py` 落盘的 `connectors.manifest.json` 证明 `connectorCount == 0`；没有该 sidecar 也没有 `--connectors-spec`，本门标 `unknown`（操作员须显式确认清单为空），不能跳过。

## 10. 门 F：canonical + 总览视觉

连续总览是第二个真实产品入口，不是独立页的附属品：

- 只替换目标画板 stage 内容，保留其他画板、导航、样式和 fit 逻辑。
- 所有新增 SVG/脚本生成的节点也必须同步，不能只同步图片隐藏状态。
- 新增节点必须位于目标 `.stage` 内，参与同一 transform 和 clipping。
- CSS 按 board 作用域隔离，资源路径重定根目录。
- 对独立页与总览抽样比较归一化几何，并**实际查看 fit 后局部截图**：stage transform 生效、marker/stroke 可见（computed stroke-width > 0、路径数 > 0、非 display:none）、箭头头部与树状主干没有缺失。
- 重新运行旧总览生成器前检查它是否会覆盖手工加入的缩放或同步修复。

本门的 canonical 截图由 `verify.py` 捕获（`evidence/browser.png`）；总览 fit 后真实可见性是人工/证据项，未记录则 `unknown`。DOM 节点存在不等于视觉可见。

## 11. 门 G：像素回归与修复闭环

### 11.1 canonical

- 同一 Chrome/浏览器版本。
- viewport 与 reference 物理尺寸相同，DPR=1。
- stage=(0,0,W,H)，页面无额外滚动溢出。
- 等字体和图片稳定后截图。
- browser.png 与 reference.png 物理尺寸完全一致。
- **同一 Figma 编辑器会话的 tools/call 是串行处理的**：live 复验对同一 session id 必须串行排队执行，禁止两路 live 读取/导出并发打同一 session，否则 get_node 会在插件端排队数十分钟无返回。多画板需要 live 复核时复用同一会话、逐画板串行，或换独立 session。

### 11.2 指标

主口径示例：任一 RGB 通道 `abs(candidate-reference)>20/255` 为 mismatch。保存 raw、outside-text、text-region、SSIM、热图、overlay、hotspots。阈值是项目合同，不是跨项目自然常数。

### 11.3 单变量实验

1. 备份正式 HTML 与当前证据。
2. 写一个明确假设和只改一个变量的候选。
3. 输出到独立 candidate 目录，不覆盖 baseline。
4. 重跑所有阶段门（A–G），不只是像素。
5. 目标闭合且无回归才提升为正式版；否则回退。

不要因为 outside 下降就接受 live 几何恶化，也不要因为 live 坐标一致就接受 reference 画面明显错误。冲突时记录，不算普通修复。**首轮数字与修复后数字分开报告，修复方向为负时如实写明回退；不得用平均分/通过率掩盖某条 assertion 失败或未达标。**

### 11.4 热点归因顺序

热图 → 局部 overlay → live 节点 → DOM 几何 → 素材内容 → 字体/抗锯齿。先找结构根因，再考虑像素级调整。大面积背景或已烘焙截图的内容差异，通常不能靠移动其他 DOM 修复。

## 12. 推荐目录和记录模板

```text
project/
  frame-manifest.json
  frame-XX/page.html
  _reference/                 # 仅验收，不进入网页
  _design/                    # 缓存，live 优先
  _native_export/frame-XX/    # 独立非文字资产与manifest
  _diff/frame-XX/             # browser/diff/heatmap/overlay/metrics/text/run
  _repair_evidence/           # 每轮before/after/result/rollback
  _backup/
```

每轮 `repair-result.json` 至少写：

```json
{
  "target": "缺陷与验收口径",
  "changed_nodes": [],
  "hypothesis": "单变量根因假设",
  "before": {},
  "after": {},
  "relative_geometry": {},
  "text_regression": {},
  "visual_evidence": [],
  "decision": "retained | rolled_back | version_conflict | blocked",
  "remaining": []
}
```

### 8.1 run 清单的哈希契约

每轮 `run.json` 只绑定外部产物的 SHA-256：HTML、reference、browser.png、metrics.json；并显式列出 `hash_inputs`。**绝不把 run.json 自己的哈希写进 run.json 自身**（写完再回写会让内嵌哈希指向中间态，造成自举不自洽）。run.json 自身的摘要写到 sidecar 文件 `run.json.sha256`，使 `sha256(run.json) == run.json.sha256 内容` 可独立验签。跨 run 比较指标时必须同口径（阈值、区域定义、mismatch 算法），不同口径的 raw/MAE/bad% 数字不能直接相减；首轮数字与修复后数字分开报告，修复方向为负时如实写明，不得用平均分掩盖未达标或回退。
