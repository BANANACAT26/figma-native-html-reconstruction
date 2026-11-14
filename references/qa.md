# 踩坑 QA 与故障决策

## 路线与边界

### Q1：先做语义化响应式网页，为什么被否决？
它改变了原稿构图。固定画板高保真复刻与响应式重构是不同任务；先确认目标，不能用通用网页经验替代用户要求。

### Q2：整板高清 PNG 很接近原稿，为什么仍不合格？
它没有真实 DOM 文本和独立组件，不是要求的可编辑网页。整板图只能放 `_reference` 做验收。

### Q3：整板 SVG 是否更“可编辑”，能否作为主路线？
本次两次回传超时，而且整板导出仍会绕过独立节点和增量审计。仅对独立矢量/连接器小范围使用。

### Q4：Dev Mode 不能隐藏文字，如何导出纯非文字素材？
遍历 live 树，显式选择最大 TEXT-free 子树并原生导出；不要对原设计做临时写操作。

### Q5：为什么不让 GUI 自动点 Figma？
本次 AX 点击多次未改变真实状态。GUI 返回动作不等于状态改变；结构化 MCP 更稳定、可保存和可复验。

## 节点、坐标与素材

### Q6：页面顶层有 N 项，为什么不是 N 个画板？
其中可能有画布上的 VECTOR 箭头等非画板节点。按节点类型和画板语义过滤，不能按 children 数量交付。

### Q7：live 与旧 reference 不一致，选哪个？
它们回答不同问题：live 管当前结构/几何，reference 管历史像素。标版本冲突，不擅自用修改一个去迎合并另一个。

### Q8：`visible=true` 的元素在画面中看不到，能删吗？
不能仅凭此删除。检查祖先裁剪、z 序遮挡、导出偏移和 reference；live 中应显示的内容必须保留或由等效独立组件替代。

### Q9：GROUP 子节点出现几千像素误差，是页面坏了吗？
可能是坐标口径错误。本次 GROUP 子节点已处父空间，重复加 GROUP 位移会产生假 P0。先用已知节点验证矩阵。

### Q10：旋转节点 bbox 差很多，为什么不直接修？
未旋转布局盒、旋转 AABB、注册框和实际渲染边界不同。先统一口径；不可比时标待复核，不生成假缺陷。

### Q11：PNG 比 Figma 设计盒大，是否尺寸错误？
可能包含阴影、描边或实例渲染溢出。比较实际可视边界和有效裁剪，而不是强拉到 layout box。

### Q12：容器没有对应 DOM，是否内容丢失？
容器可能已被扁平化，后代或背景已承担视觉。检查渲染结果与映射类型，不把每个 Figma 容器都要求一比一 DOM。

### Q13：原生重导出为什么反而变差？
导出新资产可能改变缩放、抗锯齿、色彩或与旧 reference 的版本关系。候选必须 A/B，退化就回退，不能因为“原生”就保留。

### Q14：为何移动烘焙 PNG、扫色彩/缩放/亚像素相位常常失败？
这些试验只能改变表观像素，容易破坏 live 几何，且无法修复素材内部内容差异。先归因结构；多次无收益即停止扫参。

## 文字与字体

### Q15：文本重叠只看 div 位置可以吗？
不行。需检查重复 DOM、隐藏文本误显示、一个文本拆多份、行高/盒高以及真实行盒相交。

### Q16：全局 `white-space:pre` 为什么造成问题？
它禁止必要自然折行，长段落会水平溢出。根据显式换行、自动resize、文本框宽度和实际内容逐类设置。

### Q17：全局 `break-word` 是否安全？
不安全，会把单行标题、短标签或英文拆成原稿没有的多行。用真实行盒审计验证。

### Q18：设计里本来就有重叠/遮挡，审计如何处理？
与 live 设计盒和视觉证据对照，重分类设计性行为；不要为了审计清零删掉真实内容。

### Q19：CSS 写了字体名，为什么字形还不一致？
字体可能未实际加载、字重缺失或回退。检查 `document.fonts`、网络资源和实际字形；记录授权限制和回退，不将文字图片化。

### Q20：OFL 替代字体能否放心用于精修？
授权合法不等于视觉接近。真实 A/B 中候选字体曾使差异升高，应回退；字宽和字重匹配比“可分发”另一个维度。

## 箭头与树状连线

### Q21：两个组件绝对坐标都对，为什么箭头仍错？
连接取决于源/目标边、端点和内部路径。旋转实例的外层注册框不等于箭头像素位置，必须验相对关系。

### Q22：端点差一点，能否在箭头前补一段线？
不能。本次用户明确否决。要延伸/移动完整连接器，让真正的箭头尖端接到目标边；纵向同样处理。

### Q23：整组箭头一起平移是否高效？
只有证明所有节点共享同一误差时才可用。本次不同旋转 PNG 的内部偏移不同，需要逐条校正。

### Q24：连线混乱是否一定是重复 DOM？
不是。先审计重复、旧层+新层，再检查每条 PNG 内部旋转后的路径偏移。本次主要根因是各导出单元内部可视路径不同。

### Q25：树状架构能由多个小箭头拼出来吗？
不应。目标是共享主干、正交分支和节点标记；逐标签孤立拼接容易断线、重线和错连。

### Q26：SVG 替换 PNG 后为何可能双显或消失？
隐藏旧层和插入新层是同一替代事务。新 SVG 必须同时存在、位于正确 stage、采用相同坐标系并同步到总览。

## 总览与同步

### Q27：独立页有箭头，连续总览为什么没有？
本次一次是同步只带入了隐藏旧 PNG 的状态，漏带新 SVG；另一次 SVG 被插到 board 容器外。DOM 总数不能证明可见。

### Q28：canonical 截图正确，为什么总览仍不合格？
总览有 CSS 作用域、资源路径、fit transform 和额外裁剪。必须在实际 fit 尺寸下看图并抽样比较归一化几何。

### Q29：为什么重跑旧总览生成器后修复又丢了？
旧生成器会覆盖手工同步和显示适配。生成器也应成为验收对象；先升级生成器或用只替换目标 stage 的同步器，保留当前 fit 逻辑。

### Q30：总览太大能否直接改画板尺寸？
不能改原 stage 或内部布局。只对总览显示做等比缩放，同时设置 viewport 宽高；必要时按窗口宽和高共同计算 scale。

## 指标与交付

### Q31：mismatch 下降是否等于修复成功？
不等于。曾出现像素改善但 live 几何恶化的候选，已回退。还要看相对几何、文本和真实视觉。

### Q32：文本审计 0 错误是否等于全部画板达标？
不等于。文本与像素是不同门；最近一次全量口径只有部分页面达到 1%。汇总文件还要检查是否只覆盖单页。

### Q33：为什么 metrics 与当前 HTML 对不上？
可能截图或报告没重跑，也可能映射记录误写。每次重新截图并保存 HTML/reference/browser/metrics 哈希，以 manifest 和哈希为权威。

### Q34：能否承诺这套 SOP 保证 1%？
不能。精确字体、烘焙位图内容/色彩/抗锯齿及版本冲突会形成下限。SOP保证的是高效定位、可审计验收和错误可回退，不是未经证据的数值承诺。

### Q35：已经“不错”后还要做什么？
从全页扫参转为用户点名的局部组件，逐组件原生导出/几何/像素/视觉验收，保护已认可区域零变化，避免全站反复回归。

## 构建管线与连接环境（通用规则）

以下条目来自实跑复盘暴露的系统性缺陷，已在通用脚本中修复；遇到同类现象按此排查，不依赖任何画板、字体或角度特例。

### Q36：背景写了两层颜色，为什么整层背景消失了？
把两个裸颜色用逗号写进一个 CSS `background` 是非法语法，浏览器会整条丢弃该背景声明，画面变成一片底层色。正确做法：多个 SOLID 填充按 Figma 顺序做 Porter-Duff source-over 合成，最后只输出**一个**颜色层；若半透明纯色压在 IMAGE/渐变之上，该纯色必须再输出成一个平层（如 `linear-gradient(c,c)`），不能只写裸颜色。排查：发现整面背景消失/热点覆盖整板时，先看 builder 输出的 background CSS 是否为多裸颜色逗号列表。

### Q37：IMAGE 背景或图片填充缺素材，能不能先涂白或留空？
不能。IMAGE 填充没有可解析素材时必须显式披露 `image-fill-unresolved` 并在该层留空，而不是涂白、猜黑色或用占位色掩盖。不支持的 fill 类型同样披露后跳过。披露清单要随 manifest 落盘，交付时单列。

### Q38：文本节点没有 fills 就写 `color:#000` 有什么问题？
在深色画板上黑字几乎不可见，text-region 差异会很高，但行盒审计完全正常。规则：文本填充分类渲染——无可见填充 → `color:transparent` 并披露；多 SOLID → over 合成；渐变 → `background-clip:text`；IMAGE 填充披露。**任何分支都不得猜 `#000`**。排查：`audit_text.py` 的 colorAudit 会报"深底深字嫌疑"和"manifest 无 fills"嫌疑。

### Q39：文本 wrap/overflow=0 是否就代表文字视觉通过？
不代表。折行/溢出为 0 只说明没有新增行和盒溢出，颜色对不对、字看不看得见是另一道门。文本审计必须同时输出 colorAudit（深底深字、缺失填充嫌疑）；wrap=0 而 colorAudit 有嫌疑，仍判文字门未过。

### Q40：旋转文本为什么倒置/漂在画板外？
用"未旋转 local 盒 + CSS rotate"渲染旋转文本时，旋转中心和原点口径不一致会导致任意非零角度都漂移（倒置文本是典型症状）。正确做法：用节点累积的全仿射矩阵（absoluteTransform）一次性输出通用 `matrix(...)`、transform-origin 为 (0,0)，把整棵祖先树的旋转平移一次 reproduce。清单里有旋转角却没有 absoluteTransform 的文本，只能按渲染 AABB 放置并记 `rotation-unverified` 披露，不写任何角度特判。

### Q41：manifest 里 zIndexChain/fills 为 null，构建就崩，为什么？
序列化字段可能为 null 或缺省。所有清单消费必须 null 安全：zIndexChain 为 null/非列表 → 空链；fills/strokes/effects 非列表 → 空；fontSize/字号非法 → 默认值。任何 `tuple(None)`、`float(None)` 类崩溃都是未做 null 防护，不是数据异常。

### Q42：连接器每条外框 left/top 都对，为什么尖端没接上目标卡？
扁平化 PNG 里看不到箭头尖端像素，只看 img 外框位置无法证明尖端接触。验收合同是：逐条测量连接器外框到 source 边、target 边的 gap（横向验右/左边、纵向验下/上边），gap 超公差即该条 `edge-gap` 失败。缺 source 或 target 选择器只能标 `unverifiable`——绝不静默判过，也不用多条 gap 的平均值掩盖某一条仍错位。修复时逐条处理连接器，不做整组统一平移。

### Q43：tools/list 反复超时导致整条流水线跑不动，怎么办？
tools/list 只是可选的能力发现元数据；实际工具名由 SOP 契约固定。客户端对 tools/list 做有界重试（可配超时与重试次数），失败后退化为 degraded 并继续按契约工具名调用；编辑器侧卡住时设 `FIGWRIGHT_SKIP_TOOLS_LIST=1` 直接跳过。注意一个曾出现的实现 bug：请求必须先 send 再等响应，"超时"可能只是请求根本没发出去。

### Q44：并发两路 live 读取，为什么 get_node 卡几十分钟无返回？
同一 Figma 编辑器会话（session id 绑定）串行处理 tools/call。对同一 session 的 live 读取/导出/复验必须串行排队，禁止两个 FW 实例或两路 live 复验并发打同一 session。多画板 live 复核时复用会话逐画板串行，或换独立 session。

### Q45：run 报告里三个哈希互相矛盾、验签失败，为什么？
把 run 自身的哈希在写完文件后回写进同一文件，内嵌值指向回写前的中间态。契约：run.json 只绑定外部产物（HTML/reference/browser/metrics）的 SHA-256 并列出 `hash_inputs`；run.json 自己的摘要写到 sidecar `run.json.sha256`，使 `sha256(run.json)` 与 sidecar 内容一致。任何把自哈希内嵌进自身的做法都会自指不自洽。

### Q46：首轮数字和修复后数字、不同运行之间的数字能直接比较吗？
不能。修复后的漂亮指标是单变量修出来的，不是首轮能力；跨运行若用不同阈值（mismatch@20 vs MAE vs bad%）、不同区域（全图 vs text-mask 外），数字不可比。报告时分清 first_pass 与 final、同口径再比较；修复方向为负（指标反而变差）时如实写明回退，不用平均分/通过率掩盖某条 assertion 失败或未达标。

### Q47：content ledger 里 effectiveVisible=false 的节点，HTML 里没出现算漏吗？
不算漏，反而正确。`audit_content.py` 的规则是双向的：effective-visible 节点必须在 HTML 里以 `data-n` 出现（漏了是 hard failure）；被标记 invisible/transparent 而**却**在 HTML 里多渲染（hidden over-render）也是 hard failure。每个节点在 ledger 里都要有 disposition：`rendered-*` / `skipped-invisible` / `skipped-transparent` / `unresolved`。`unresolved` 是硬失败（未解析 IMAGE / 不支持填充未披露）。

### Q48：gradient 文字在 CSS 里是 `color:transparent`，审计会不会误报成近不可见？
会被正确识别为"有证据的透明"而不是嫌疑。`audit_text.py` 从 design manifest 读 fill mode：GRADIENT 文本要求 DOM 有 `background-clip:text`（或 `-webkit-background-clip:text`），这是 gradient 文字的标准渲染方式；DOM 缺这个 class 才报嫌疑。IMAGE 填充和无可见填充（fills 为空）的文本无法在无 reference 像素时判色，报 `unknowns`，**绝不猜 `#000`**。SOLID 文本才在 stage 背景亮度下报"深底深字"嫌疑。

### Q49：wrap/overflow=0、行盒审计全绿，文字门就过了吗？
没过。行盒审计只查布局（折行、重叠、overflow），颜色/可见性是 colorAudit 另查。wrap=0 但 colorAudit 有硬嫌疑（深底深字、gradient 文本缺 background-clip），门 D 仍 fail。

### Q50：PNG 连接器外框贴到了目标边中点，能算箭头接到位吗？
不能。PNG 是扁平栅格，内部看不到箭头尖端像素；外框到达边只能给 `degraded-bbox` 结论（诚实报告外框位置），`tipVerified=false`，在 `audit_connectors.py --strict` 下**不算 pass**。真要 pass 必须用独立小节点 SVG 导出（`export_connector_svg.py`），审计用 `path.getPointAtLength(totalLength)` 取最长描边路径的真实端点，经 `getScreenCTM()` 映射到视口，与目标边中点比距离。PNG 外框不能冒充 SVG 真尖端。

### Q51：独立页 canonical 截图里箭头看得见，总览 fit 后不见了，为什么？
典型原因是连接器 SVG 被插到了 board 容器外（`</section>` 之后），不参与 `.stage` 上的 `transform: matrix(...)` fit 缩放。`audit_connectors.py` 会判 `stage-outside`。修法：把 SVG 移回承载 fit/scale transform 的同一个 `.stage` 容器内，与其他图层同坐标系；修后还要在总览实际 fit 尺寸下确认 computed stroke-width > 0、路径数 > 0、marker/箭头头部可见。DOM 节点存在不等于总览视觉可见。

### Q52：这版画板里没有箭头，门 E 怎么过？
不能跳过。必须证明清单为空：用 `export_connector_svg.py` 落盘的 `connectors.manifest.json` 确认 `connectorCount == 0`；没有该 sidecar 也没传 `--connectors-spec`，`reconstruct.py` 把门将 E 标为 `unknown`，由操作员显式确认"本画板确实无连接器"。无证据就当空，等于把可能漏画的箭头静默放过。

### Q53：能不能用一个整板 SVG 一次导出全部箭头，省事？
不能。整板 SVG 会把文本和位置一起烘焙进去，绕过独立 DOM 文字与增量审计；历史上整板回传还超时。连接器必须**逐节点**导出小 SVG（线体+拐点+圆点+箭头一体），且子树含 TEXT 时 `export_connector_svg.py` 会拒绝烘焙。

### Q54：raw mismatch 已经很低，能不能宣称达标 1%？
不能承诺。精确字体、烘焙位图内容/色彩/抗锯齿、live/reference 版本冲突会形成下限；SOP 保证的是可定位、可审计、可回退，不是未经证据的数值达标。报告里未达用户目标时如实列出差距，不把"已优化"写成"已达标"。

## Design Compiler 默认流程

### Q55：为什么构建必须走 compile_design 产出的 layout-plan，而不是直接读 SDK 数据排版？
SDK/MCP 节点树只是输入。compile_design 把它归一化为 design-ir、全关系图和带 planHash 的确定性 layout-plan；builder 只消费 plan。这样排版规则集中在编译器一处、可复现（同输入同 planHash）、可逐条审计关系边；直接在 Agent 手里按 SDK 字段摆元素会把排版规则散落在不可复验的临时判断里。

### Q56：Agent 能不能对着 reference PNG 截图微调坐标？
不能。reference PNG 只在最后做视觉验收（门 G/F）。Agent 截图排版等于绕开 plan 与关系审计，引入不可复验的人工判断；坐标问题应回到 plan/关系门定位，而不是肉眼挪像素。

### Q57：门 C 为什么不再要手工 geometry-spec？
手工 spec 只覆盖人想到的少数边，容易漏掉关系。默认流程用 audit_relations 对 plan 全量关系边（父子 inset、同级间距/顺序、对齐轴、基线、图标中心、文字行 inset、连接器边）与真实 DOM 测量逐条 diff，不需要手写 spec；没 plan 时门 C 直接 unknown。

### Q58：strict 为什么 unknown 也算不过？
unknown 表示证据没采到（没 plan、没测量、没总览 fit 证据），不是"没问题"。strict 默认下 fail 与 unknown 都非零退出并机器可读单列；`--no-strict` 只放松退出码，不能把 unknown 洗成 pass。

### Q59：文字门失败为什么只自动修一次？
repair_text 是确定性规则器（只合并 white-space/line-height/width 等声明，绝不缩 font-size）。修一次后复验：过了就留，仍 fail 就如实 fail——第二轮扫参会引入不可复验的试错。缺字体进 blocked（R-FONT-1），不静默回退。

### Q60：连接器 strict 失败自动重建后，为什么成功还要保留失败回滚能力？
repair_connectors 自带备份与回滚：重建后若不能全部适用边通过或引入回归，就回滚原 HTML。unverifiable（缺 source/target）被跳过并阻断，不用多条边的平均残余把某条错位洗成 pass。
