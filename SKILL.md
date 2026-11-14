---
name: figma-native-html-reconstruction
description: 将 Figma 画板高保真重建为可编辑 HTML：通过 Figma MCP/实时节点树读取设计，原生导出独立非文字素材，以真实 DOM 重建文字，在固定画板坐标中还原布局，并用绝对+相对几何、文本审计、同尺寸像素对比和实际截图闭环验收。只要用户提到 Figma 转 HTML、1:1/像素级还原、设计稿转网页、固定画板重建、箭头/连线对齐、DOM 与 Figma 几何核对、reference 像素复验，或要求保留真实文字且禁止整板截图，就应使用本 Skill；即使用户没有明确说出 SOP 或 MCP 也一样。
compatibility: 需要可读取 Figma 的 MCP 或等效设计数据、浏览器/Playwright、Python 3；像素脚本需要 Pillow、NumPy、SciPy，浏览器审计需要 Playwright。
---

# Figma 原生高保真 HTML 重建

把设计稿当作**有坐标、有层级、有连接关系的组件系统**，不要当作一张待描摹的图片。目标是在保留真实文字与独立素材的前提下，建立可复核、可回退的固定画板网页。

## 先锁定交付边界

开始前列出用户硬约束，并逐项验收。若用户未另作说明，采用以下默认：

- 文字是可选择、可审计的真实 DOM；不把文字烘焙成图片。
- 非文字内容使用独立原生导出节点、背景组或合成组；不把整板 PNG/SVG 当网页。
- 保留原画板宽高、原始坐标系和比例；不在画板内部做响应式重排。
- 不删除原稿应显示内容；设计性遮挡、显式换行、可见装饰均须识别后保留。
- 总览可以用等比缩放适应窗口，但缩放只改变显示，不改变原始 stage。
- 字体、图片和组件素材必须有权使用。**缺字体时先去网上把字体下载下来并真正落地生效**（流程见 `references/font-sourcing.md` 与 `scripts/check_fonts.py`），只有在确实找不到、或授权不允许时才披露回退——"留一个 `@font-face` 挂载点等用户自己提供"不算交付；任何情况下都不用文字图片化绕过。
- 缺失或不支持的填充/图片素材如实披露（disclosure），不涂白、不假造颜色或背景。

用户的明确要求优先于这些默认值。若用户要语义化响应式网页而非固定画板复刻，先确认验收目标，不能混用两套构图。

## 按需读取

1. 执行任务前完整读 [references/sop.md](references/sop.md)（含 A–G 阶段门）。
2. 使用 Figwright/Figma MCP 时读 [references/figwright-mcp.md](references/figwright-mcp.md)。
3. 遇到旋转、GROUP、INSTANCE、裁剪、跨容器或箭头连线时读 [references/geometry-and-connectors.md](references/geometry-and-connectors.md)。
4. **只要画板里存在箭头/连线/树状主干，必读 [references/connectors.md](references/connectors.md)**：独立连接器审计 `scripts/audit_connectors.py` 与小节点 SVG 导出 `scripts/export_connector_svg.py` 的契约、verdict 枚举、strict 门与“SVG 真尖端 vs PNG 外框”边界。
5. 每次验收前完整读 [references/verification.md](references/verification.md)。
6. 出现“指标变好但看起来不对”、元素消失、文字重叠或返修时查 [references/qa.md](references/qa.md)。
7. **只要字体清单里有本机没有的字体（含 `mixed` runs 里的字体），先读 [references/font-sourcing.md](references/font-sourcing.md)**：字体怎么找、怎么验、怎么落地、哪些站点是死路。
8. 需要了解本方法的真实来源和局限时读 [references/case-study.md](references/case-study.md)。

## 高效主流程

### 1. 冻结范围与基准

- 从 live Figma 建立 `frame-manifest.json`：画板名称、节点 ID、类型、尺寸、顺序、reference 路径。
- 只把顶层 FRAME/SECTION 等真正画板纳入清单，排除画板外的箭头、注释和辅助矢量。
- 固定浏览器、viewport、DPR、reference 导出时间和像素阈值。静态 PNG 不自动跟随 live Figma，二者冲突必须单列。

### 2. 一次读树，生成数据清单

- 读取 live 节点树和实际可见性，不以缓存 `_design` 替代 live。
- 产出资产清单、文字清单、容器视觉清单、字体清单、连接关系清单和异常清单。
- 先找最大 TEXT-free 子树；含文字的容器继续向下拆分。不要先逐叶导出再手工拼图。

### 3. 原生导出与 DOM 重建

- 使用 Figma 插件 `exportAsync` 或等效原生导出，保留蒙版、透明度、混合、阴影和复杂路径。
- 仅导出独立非文字节点/子树；整板只可作为验收 reference。
- **连接器（箭头/连线/树状主干）优先用 `scripts/export_connector_svg.py` 导出独立小节点 SVG**（线体+拐点+圆点+箭头头部一体），审计时用 `path.getPointAtLength(totalLength)` 取最长描边路径的真实端点作为箭头尖端。**禁止整板 SVG 导出**——整板 SVG 会把文本和位置烘焙进去，绕过独立 DOM 与增量审计；只对独立矢量/连接器小节点使用 SVG。连接器子树含 TEXT 时拒绝烘焙并披露。
- 用固定尺寸 stage、绝对定位、zIndexChain（可能为 null，按空链处理）、有效裁剪和真实 DOM 文本重建。
- 每个映射元素保留 `data-n="<figma-node-id>"`，让后续几何审计自动化。
- 连接器 SVG/PNG 必须位于承载 fit/scale transform 的同一个 `.stage` 容器内；放在 stage 外（例如 `</section>` 之后）不会随总览缩放，fit 后消失。
- 多层 SOLID 填充按 Porter-Duff over 合成后输出为一层颜色，绝不输出两个裸颜色的逗号列表；半透明纯色压在非纯色层之上时必须作为渐变/颜色层，不能只写裸颜色。
- IMAGE/渐变填充缺失可解析素材时显式披露（image-fill-unresolved），不涂白、不假造黑色背景；文本填充分类为 none/solid/gradient/image，无可见填充或不支持的填充渲染为透明并披露，绝不猜成 `#000`。
- 旋转/嵌套文本用节点累积的全仿射矩阵（absoluteTransform）一次性输出通用 CSS matrix，不用 local 盒再叠 CSS rotate，也不按角度特判。

## Design Compiler 默认流程（唯一构建依据）

默认编排由 `scripts/reconstruct.py` 一条命令串起：

```
extract → normalize → analyze → plan → [P0 data_gate] → build → verify → repair → verify
```

- **SDK/MCP 只是输入**：Figma MCP / 实时节点树导出的 manifest 与 PNG 是 *输入*，不是排版依据。`compile_design.py` 把输入归一化为 design-ir、全关系图（component-relations）和确定性 `layout-plan.json`（含 `planHash` 与 `layout-plan.json.sha256` sidecar）。
- **P0 数据门（compile 之后、build 之前，strict 默认阻断）**：`scripts/data_gate.py` 机读检查 manifest→IR→plan 的契约，失败即非零退出并写 `evidence/data-gate.json`，`reconstruct.py` 在 strict 下**拒绝 build**。检查项：rootFills 链完整、mixed 字号每个 run 有数值（无 16px 冒充）、AUTO line-height 未被盒高/行数伪造（`lineHeightSource='resolved'` 视为伪造）、每个 rendered 节点有画板坐标 render 盒、资产带 pngPlacement+exportSize、无 corner-reprojected 盒标志。`--no-strict` 仅把失败记为 stage 而非阻断。
- **算法 plan 才是构建依据**：`build_fixed_html.py` 只消费 `layout-plan.json`（plan 路径），不再回头读 raw SDK 排版。`reconstruct.py` 导出后**默认**对 export 目录跑 compile；没有 plan 时门 C 直接 `unknown`。
- **Agent 不得截图排版**：Agent 不看 reference PNG 去猜坐标、不手改定位；reference PNG 只在最后做视觉验收（门 G/F）。任何"对着截图挪元素"的动作都违反本契约。
- **确定性一次修复**：门 D 文字失败时跑一次 `repair_text` 确定性 CSS 修复（绝不缩 font-size）后复验；门 E 连接器 strict 失败时跑一次 `repair_connectors` 全 SVG 重建（自带备份/回滚）后复验。
- **strict 默认开启**：任一阶段 `fail` 或 `unknown`（证据未采集）即非零退出；`unknown` 机器可读、单列，**绝不等于 pass**。前置门失败不能用全局像素均值掩盖。

### 3.5 数据契约（P0 硬门，data_gate 机读）

构建器只信 plan，下列契约在 compile→build 之间由 `data_gate.py` 强制；缺必需字段、用默认值冒充、几何口径错配一律非零阻断，**不得为了过而放松**。

- **rootFills（画板背景唯一来源）**：`manifest.rootFills` 原样透传到 IR source 与 `plan.artboard.rootFills`。分类：键缺失或 `null` = 未知 → **阻断**（绝不默默 transparent）；`[]` = 显式无填充 → transparent + disclosure；`[...]` → 多层合成。构建器只消费 plan，不回头读 manifest 重推背景。artboard 根行无自身几何，其 `designBox/renderAABB` 补为舞台盒 `(0,0,w,h)`。
- **坐标族（禁止跨族混算）**：`designBox` = 未旋转、仅乘祖先矩阵的布局盒；`renderAABB` = export 自带的画板坐标渲染真值（`intrinsicRenderBounds`/`relativeToArtboard`）；`pngPlacement`+`exportSize` = 已烘焙旋转的 PNG 像素落点与尺寸。**不得**把父坐标 local 角点乘画板 absoluteTransform 来重算渲染盒。rotation-baked PNG 一律以 `<img>` 平铺在 pngPlacement、按 exportSize 像素尺寸输出，**不再套 CSS matrix、不拉成 designBox 比例、不二次绘制容器**。plan 层必须透传 exportSize/asset 文件名。
- **文字 runs 与字号（禁止 16px 冒充）**：per-run 样式（characters/fontSize/fontName/fills/lineHeight/letterSpacing）原样透传。`fontSize='mixed'` 仅在**每个 run 都带数值字号**时才可消费；任何 run 缺数值/非数值一律硬失败，不回退 16px。审计按 run `<span data-run-index>` 的 computed font-size **逐 run 对比**期望字号（偏差 >2px 才报）；mixed 节点逐 run 正确时不得误报，错误时仍杀。
- **AUTO line-height 与原生行计划（合法 live 值，禁止猜测像素）**：AUTO 是合法 live 值；没有实测时输出 `line-height:normal` 并标 unresolved，禁止用盒高/行数/倍率伪造 px。可在独立 Figma 文件建立同字符、同字体、同宽度校准节点，用原生矢量 PDF 的文本 spans 提取真实行文本和基线，写入可选 `text-calibration.json`。`compile_design.py` 只有在所有 PDF 行拼接后与 live `characters` 完全一致时才附加 `linePlan`，并用真实相邻基线差解析行高；构建器把每行编译为真实 DOM block/run 切片，`white-space:pre` 只阻止浏览器再次断行，不把文字图片化。字符不一致、缺行或混合 runs 无法一一映射时硬失败。
- **有效裁剪**：导出器原样保存容器 `clipsContent`；编译器沿祖先链求有效裁剪交集，文本自身固定盒高度不是可见性边界。构建器用 `clip-path` 裁剪到祖先 Frame，不删除后续行，也不把文字硬裁到自身盒高。
- **合成回归**：`tests/test_iteration4_gates.py` 用纯合成 fixture（无具体画板节点 ID/项目数值）证明：改输入字号、框宽、旋转、父子累积矩阵、hidden 会自动反映到 plan **与**构建后 HTML；缺字段/mixed 缺数值/伪造 AUTO/缺 rootFills 必须非零阻断。改算法时同步跑该套件并做 mutation 证明测试会杀回归。

### 3.6 字体获取（缺字体先下载，不得只"留挂载点"）

字体不是可选的美化项：字重、字宽、字形直接决定换行位置与像素结果。缺字体时的正确顺序是**先获取、再落地、最后才披露**。

1. **建清单**：从 `text-manifest.json` 按 per-run 取 `fontName`（family + style + PostScript 名），与 `fontsMissing` 合并；`fontName='mixed'` 的节点必须逐 run 展开（同一行可能是两款字体）。
2. **先查本机**：扫系统字体目录与 Figma 字体缓存，用 `fontTools` 按 nameID 1/4/6 匹配；命中即复制进项目 `fonts/` 并改名，不装系统（避免污染用户环境）。
3. **再上网找**：用带渲染的浏览器做正文级检索（不要只读搜索摘要），优先按**确切文件名/PostScript 名**搜（例如 `FZXS15--GB1-0.ttf`、`HKGroteskWide-Regular.otf`），命中后**用站点自己的直链下载**，不要点"官网下载""客户端下载"。
4. **必须校验再落地**：MD5 与站点公布值一致 → `fontTools` 校验 nameID（family/style/PostScript）与字重 → 确认 cmap 含所需字符 → 放进 `fonts/`，`@font-face` 的 `font-family` 字符串**与 Figma `fontName.family` 逐字一致**。
5. **必须证明生效**：`audit_text.py` 的 `fontsVerified`/`fontsMissing`，以及浏览器 `document.fonts.check('16px "<family>"')`；只写名字不算加载。
6. **只有找不到或授权不允许**才披露回退，并写清尝试过的来源与失败原因。

细则（站点黑名单、MD5 交叉验证、变体陷阱、"Wide"这类独立家族、"非商免"授权口径）见 `references/font-sourcing.md`；落地后自检用：

```bash
python3 <SKILL_DIR>/scripts/check_fonts.py \
  --fonts-dir layout-01/fonts --css layout-01/fonts/fonts.css \
  --html layout-01/page.html \
  --plan layout-01/export/artboard/layout-plan.json \
  --text-manifest layout-01/export/artboard/text-manifest.json \
  --glyphs "ABCabc0123"
#   -> 逐条报告：src 是否解析到文件（按 HTML 内联 / CSS 外链两种上下文各试一次）、
#      字节数/MD5、nameID 1/2/4/6、字重是否覆盖该 style、cmap 覆盖
#   exit 0 = 每个 family+style 都已落地；exit 1 = 有缺口，去下载，不要交回退
#   * 必须带 --plan：text-manifest 里 fontName='mixed' 的节点没有 per-run 字体，
#     per-run 真值在 layout-plan.json 的 textPlans[].runs[].fontName
#   * 必须带 --html：构建器会把 --font-css 内联进页面，src 就相对 HTML 目录解析
```

两个会让"字体明明下载了却不生效"的管线坑（真踩过，务必检查）：

- **离线复用会跳过 build**：`--reuse-export-dir` 分支若不重建 HTML，带 `--font-css` 重跑实际验的是旧页面（`@font-face` 计数为 0），summary 却显示 fresh evidence。改字体后必须确认 `grep -c @font-face page.html > 0` 且 build 真的跑过。
- **改了 export/compiler 不重编**：复用导出目录时 `layout-plan.json` 已存在会跳过 compile；改输入后必须删 `layout-plan.json` 与 `.sha256`，并对比 planHash 确认刷新。

### 4. 阶段门 A–G（机读 summary，禁止用全局像素均值掩盖局部缺失）

每道门产出一份机器可读报告 JSON，状态取 `pass` / `fail` / `unknown`；`unknown` 表示证据未采集，**绝不等于 pass**。前置门 `fail` 时先修对应类别，不得进入下一道门凑指标。

- **A 基准/版本**：reference 存在、viewport/DPR/阈值冻结、run 清单落盘。
- **B 内容可见性与覆盖（默认浏览器实测）**：`audit_content.py --browser-audit` 在真实 Chromium 里逐节点取 computed display/visibility/opacity、祖先 clip/mask、stage 相交与可见像素；再核对 effective-visibility ledger——每个 effective-visible 节点必须在 HTML 里以 `data-n` 出现；被跳过的 invisible/transparent 节点**不得**多渲染；ledger 中 `unresolved` 是硬失败。
- **C 关系图（默认全量，无需手工 geometry-spec）**：`audit_relations.py` 对 `layout-plan.json` 的全关系边（parentChildInset / siblingGap / siblingOrder / alignmentAxis / baseline / iconHostCenter / textLineInset / connectorEdge）与真实 DOM 测量（MEASURE_JS）逐条 diff。每条边 pass/fail/unknown；strict 下 unknown 不算过。不再要求手工写 geometry-spec；`--geometry-spec` 仅作遗留补充证据。
- **D 文字/素材（失败自动确定性修一次）**：`audit_text.py` 逐 TEXT Node 的 Range 测真实行盒，避免把外层 block 盒合并成行；测字体实际加载、新增换行、overflow、colorAudit，并对校准 `linePlan` 逐行比对文本与行盒数。固定文本盒的纵向 scroll overflow 单列为 `designBoxOverflow`，可见性由祖先裁剪判定；横向 overflow、内容尺寸模式 overflow、行计划不一致仍是硬失败。失败时自动跑一次 `repair_text`（仅合并 white-space / line-height / width 等声明，**绝不缩 font-size**）并复验一次。`fontsMissing` 非空时先按 3.6 去下载并落地字体再复验，**不得把"字体缺失"当成终态披露收工**。gradient 透明填充（`color:transparent` + `background-clip:text`）不是近不可见嫌疑；image/无填充填充分类为 unknown，不猜 `#000`。wrap/overflow=0 不等于视觉通过。
- **E 独立连接器（strict 失败自动 fallback 重建一次）**：`audit_connectors.py --strict` 逐条判 source/target/边/端点/mid_path/箭头端；SVG 真尖端（`getPointAtLength`）接目标边才是 `pass`，PNG 外框到达边只是 `degraded-bbox`（strict 下不算过）；strict 失败时自动跑一次 `repair_connectors.py` 全 SVG 重建（自带备份与回滚：修复后不能全边通过或引入回归则回滚）并复验。缺 source/target 为 `unverifiable`，**阻断交付、绝不平均成 pass**；连接器必须在 stage 内、fit 后可见。**画板无连接器时必须用 `connectors.manifest.json` 证明清单为空**。
- **F canonical+总览视觉（要真实证据）**：canonical 截图由 `verify.py` 捕获；总览 fit 后 stage/fit/marker/stroke 的真实可见性必须由 `--overview-evidence` 提供记录证据，否则本门 `unknown`（DOM 存在不等于视觉可见）。
- **G 像素回归**：同 DPR、同 viewport、同 reference 下的 raw/outside-text/text-region 指标；修复轮后自动重截一次刷新证据。

`reconstruct.py` **默认 strict**：任一阶段 `fail` 或 `unknown` 都非零退出并在 summary 里列出 `failedStages` / `unknownStages`；`--no-strict` 仅放松退出码。live 复验对同一 Figma session 必须串行，禁止并发两路 live 读取打同一 session。

### 5. 同基准复验与小步修复

- 每次候选修改只改一个变量，先备份，使用独立输出目录。
- 重新截图并计算 raw、outside-text、text-region、SSIM、热图和文本审计。
- 同一 Figma 编辑器会话按串行处理 tools/call；对同一 session 的 live 复验必须串行，绝不开两个 FW 实例或两路 live 复验并发打同一 session，否则 get_node 会排队阻塞。
- 同时检查独立页 canonical 原尺寸图与连续总览 fit 后的真实可见画面。
- 只有目标缺陷闭合且未造成其他回归才保留；否则回退并记录原因。

## 验收门

只有以下各项均有证据才可宣称完成；未达用户数值目标时如实列出，不把“已优化”写成“已达标”。阶段门 A–G 的机读 summary 是权威记录（见 `references/sop.md`），任一门 `fail` 先修对应类别，不得用全局像素均值掩盖局部缺失；未采集证据的门标 `unknown`，不算过。

1. **A 基准**：reference 与 viewport/DPR/阈值已冻结，run 清单（外部产物哈希 + sidecar 自摘要）已落盘。
2. **B 内容**：effective-visible 内容未丢，hidden/transparent 元素未多渲染，ledger 无 unresolved；文字仍是 DOM；未解析 IMAGE/不支持填充已披露而非涂白。
3. **C 几何**：绝对位置和尺寸通过，组件间相对位置通过；测量伪偏差已重分类。
4. **D 文字**：文本内容正确，无浏览器新增重叠、意外折行和未解释的 overflow；wrap/overflow=0 不等于视觉通过，还须查 colorAudit；gradient 透明填充有 `background-clip:text` 证据，不误报。**`fontsMissing` 必须先通过联网获取字体闭合**（`document.fonts.check()` 为 true 且字体文件已落进项目），"字体缺失所以回退系统字体"只能作为已尝试并失败的披露项。
5. **E 连接器**：逐条 source/target/边/端点/mid_path/箭头端通过；SVG 真尖端接目标边才是 pass，PNG 外框只是 degraded-bbox（strict 下不算过）；连接器在 stage 内且总览 fit 后 marker/stroke 可见；无连接器时证明清单为空。
6. **F 视觉**：实际查看整页与关键局部；独立页和总览 fit 后均可见，箭头头部与树状主干没有缺失。
7. **G 像素**：在同一物理尺寸、同一 DPR、同一 reference 下复验；不得缩放图片凑指标。
8. **追溯**：保存 HTML/reference/browser/metrics 哈希及修前修后证据；run.json 只绑定外部产物哈希，自身摘要写在 sidecar（run.json.sha256），不内嵌自指哈希。
9. **不承诺 1%**：精确字体、烘焙位图内容/色彩/抗锯齿、live/reference 版本冲突会形成下限；SOP 保证可定位、可审计、可回退，不承诺任意 Figma 文件自动达到 1%。

## 已知限制（如实披露，不得当 pass）

- **AUTO line-height 外部数据缺口**：没有校准 sidecar 时仍只能落到浏览器 `line-height:normal`，保持 unresolved。原生 PDF 行计划目前属于显式校准输入，不是原节点自动测量通道；混合 runs、更复杂段落与 live/reference 版本冲突必须逐例复验，不能从一个样本推广为全部文本已闭合。
- **字体**：缺字体不再是"已知限制"，而是待办——按 3.6 下载落地后 D 门才会真正闭合；确实取不到（无处可下载、授权不允许、商业字体无免费渠道）时，才在交付里写明"已尝试的来源 + 回退到哪个字体 + 由此产生的像素下限"，并保留 `@font-face` 以便用户后续替换同名文件。
- **非 PNG 旋转容器**：已烘焙为 PNG 的旋转节点平铺不二次变换；未导出 PNG 的旋转/翻转容器仍用累积仿射 matrix 渲染，复杂裁剪下的像素贴合需逐例核验。
- **关系审计 baked/box-family 口径**：内容烘焙进祖先 PNG、或本身无 DOM 盒的节点，关系边只能记 unknown（不判 pass）；修复后旋转 PNG 按平面平铺，而 plan 的 rotated 标志仍标 rotated，box-family 口径会产生少量 unknown，属口径差而非布局错误。
- 以上均为已知未决项；strict 下它们表现为 fail/unknown，不得用全局像素均值或"视觉基本一致"抹平。

## 使用随附脚本

把 `<SKILL_DIR>` 替换为本 Skill 的真实目录。

```bash
# 通用 MCP 客户端从环境读取服务与文件会话，不内置本机路径
export FIGWRIGHT_SERVER='node /path/to/@figwright/mcp/dist/index.mjs'
export FIGWRIGHT_SESSION_ID='your-figma-file-session'
# tools/list 是可选能力发现；编辑器侧卡住时可跳过，按 SOP 契约工具名直接调用
export FIGWRIGHT_SKIP_TOOLS_LIST=1        # 跳过 tools/list
export FIGWRIGHT_TOOLS_LIST_TIMEOUT=15    # 单次超时
export FIGWRIGHT_TOOLS_LIST_RETRIES=2     # 有界重试后退化为 degraded

# 一条命令完成默认 Design Compiler 流程：
# live 导出 -> compile(plan) -> build(只走 plan) -> 阶段门 A–G -> 确定性修复 -> 复验
# 默认 strict：任一 fail/unknown 非零退出
python3 <SKILL_DIR>/scripts/reconstruct.py \
  --node-id '<artboard-node-id>' --reference reference.png --workdir fresh-run \
  [--connectors-spec connectors.json] [--overview-evidence fit.png]

# 离线复验（不打 live）：复用已有 export；layout-plan.json 已存在则不重复 compile
python3 <SKILL_DIR>/scripts/reconstruct.py \
  --reference reference.png --workdir rerun --reuse-export-dir fresh-run/export/artboard \
  --html fresh-run/page.html

# 编译器（reconstruct 默认自动调用；单独调用时把产物写回 export 目录）
python3 <SKILL_DIR>/scripts/compile_design.py --export-dir fresh-export/sample
#   -> design-ir.json / component-relations.json / layout-plan.json
#      layout-plan.json.sha256（planHash sidecar，可复现验签）

# manifest -> 固定画板 HTML；构建器只消费 layout-plan.json，不回头读 raw SDK
python3 <SKILL_DIR>/scripts/build_fixed_html.py \
  --export-dir fresh-export/sample --output output/page.html

# 原尺寸截图、DOM 文本 mask 与环境硬校验
python3 <SKILL_DIR>/scripts/capture.py \
  --html page.html --reference reference.png --out evidence/run-01

# 同尺寸像素对比；文本 mask 仅用于拆分指标，不修改 raw diff
python3 <SKILL_DIR>/scripts/pixel_diff.py \
  --browser evidence/run-01/browser.png \
  --reference reference.png --outdir evidence/run-01 \
  --text-mask evidence/run-01/text-mask.png

# 一键截图+对比+HTML/reference/browser/metrics 哈希
python3 <SKILL_DIR>/scripts/verify.py \
  --html page.html --reference reference.png --out evidence/run-01

# 阶段门 B：effective-visibility / content-ledger / text coverage / 防多渲染
python3 <SKILL_DIR>/scripts/audit_content.py \
  --export-dir fresh-export/sample --html page.html \
  --out evidence/run-01/content-ledger.json --strict

# 阶段门 D：真实 DOM 行盒、额外折行、overflow、colorAudit（gradient 透明填充不误报）
python3 <SKILL_DIR>/scripts/audit_text.py \
  --html page.html --text-manifest fresh-export/sample/text-manifest.json \
  --out evidence/run-01/text-layout.json

# 字体落地自检：src 是否解析到文件 / MD5 / nameID 1-4-6 / 字重 / 是否覆盖每个 family+style
python3 <SKILL_DIR>/scripts/check_fonts.py \
  --fonts-dir fonts --css fonts/fonts.css --html page.html \
  --plan fresh-export/sample/layout-plan.json \
  --text-manifest fresh-export/sample/text-manifest.json --strict
#   exit 0 = 清单里每个字体都已落地且 name 表自洽；
#   exit 1 = 有 family/style 缺文件、字重或家族对不上（此时先去下载，不要直接披露回退）

# 阶段门 E：连接器逐条审计（SVG 真尖端 vs PNG 外框；strict 默认从严）
python3 <SKILL_DIR>/scripts/audit_connectors.py \
  --html page.html --spec connectors.json \
  --out evidence/run-01/connectors.json --strict
#   PNG-only 连接器外框到达边只是 degraded-bbox，strict 下不算过；
#   只有上游计划补 SVG 时才显式 --allow-degraded-bbox。
```

输入两张图物理尺寸不一致时脚本会失败，这是正确行为：先修正渲染基准，不要自动缩放对齐。

## 效率原则

- 并行处理彼此独立的画板，但统一 manifest、指标口径和输出 schema。
- 优先修一类根因，不逐像素扫参；连续两次同类试验无收益就停止并换证据通道。
- 设计只读时不依赖 clone/hide 等写操作；用显式节点列表导出。
- 不把 GUI 点击、整板 SVG 回传或超长 base64 当作主路线；以结构化 MCP 数据和小批量原生导出为主。
- 不用降低文字质量、隐藏内容、整板截图或伪造指标换取“通过”。

## 交付

交付当前 HTML 与必要素材、画板清单、验收清单、几何/文本/像素证据及未解决项。说明实测覆盖范围和仍存在的字体、位图内容、版本冲突或阈值缺口；不得承诺任意 Figma 文件都能自动达到 1%。
