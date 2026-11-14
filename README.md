# figma-native-html-reconstruction

**Rebuild Figma artboards as editable HTML through a deterministic Design Compiler: real DOM text, independent assets, fixed canvas coordinates, and strict A–G verification.**

**将 Figma 画板重建为可编辑 HTML：通过确定性 Design Compiler 生成固定画板页面，保留真实 DOM 文字与独立素材，并使用严格验收门核对内容、几何、文字、连接器和像素。**

![figma](https://img.shields.io/badge/Figma-MCP%20%2F%20Figwright-6CAAFF?style=flat-square)
![architecture](https://img.shields.io/badge/architecture-design%20compiler-38E07B?style=flat-square)
![text](https://img.shields.io/badge/text-real%20DOM-FFBF47?style=flat-square)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![license](https://img.shields.io/badge/license-MIT-9AA3AD?style=flat-square)

## 交付内容与边界

输入一个 Figma 画板及其同尺寸参考图，输出可维护的 HTML、独立素材、确定性布局计划和机器可读验收证据。

| 项目 | 实现方式 |
|---|---|
| 文字 | 真实 DOM 节点，保留逐 run 字体、字号、样式和颜色，不把文字烘焙成图片 |
| 非文字内容 | 按最大 TEXT-free 子树原生导出，保留复杂路径、蒙版、透明度、阴影与混合效果 |
| 布局 | 固定画板 stage、绝对定位、原始坐标系；总览缩放只影响显示，不触发画板内部响应式重排 |
| 构建依据 | 唯一 `layout-plan.json`，构建器不根据截图猜位置，也不读取 raw SDK 自由排版 |
| 验收 | P0 + A–G 机读质量门；`pass`、`fail`、`unknown` 三态，证据缺失不等于通过 |
| 交付物 | `page.html`、素材目录、design IR、组件关系、布局计划、截图、指标和各门报告 |

> 本项目面向**固定画板高保真重建**，不是自动生成语义化响应式网站的通用工具，也不承诺任意 Figma 文件都能一键达到指定像素差异。

## 当前实现路径

![Design Compiler pipeline](docs/pipeline.png)

完整流程由 `scripts/reconstruct.py` 串联：

```text
extract → normalize → analyze → plan → P0 data gate
        → build → verify → one deterministic repair → re-verify
```

### 1. 读取设计并导出素材

`export_artboard.py` 通过 Figwright / Figma MCP 读取 live 节点树，收集：

- 画板、节点类型、可见性、层级与组件关系；
- 文字内容、逐 run 样式、坐标、累积仿射矩阵和祖先裁剪；
- 最大 TEXT-free 子树及其原生导出素材；
- 独立连接器、reference、字体清单与异常清单。

整板图片只作为最终验收 reference，不作为网页主体；整板 SVG 也不作为构建输入。

### 2. 编译为唯一布局计划

`compile_design.py` 将导出数据编译为：

```text
manifest
  → design-ir.json
  → component-relations.json
  → layout-plan.json
  → layout-plan.json.sha256
```

计划中包含画板背景、节点渲染盒、素材落点、文字 runs、行计划、有效裁剪、层级和关系图。`planHash` 用于确认布局计划的版本与可追溯性。

### 3. 构建前执行 P0 数据门

`data_gate.py` 在生成 HTML 前检查：

- `rootFills` 是否完整透传；
- mixed 字号是否每个 run 都有数值；
- AUTO line-height 是否被伪造为像素值；
- rendered 节点是否有合法渲染盒；
- 素材是否包含 `pngPlacement` 与 `exportSize`；
- 是否存在坐标族混用或 corner-reprojected 盒。

默认 strict 模式下，契约失败会阻断构建，而不是用默认字号、默认颜色或猜测坐标继续生成页面。

### 4. 只根据计划生成 HTML

`build_fixed_html.py` 只消费 `layout-plan.json`，输出固定尺寸 stage 和绝对定位图层。

每个映射节点保留 `data-n="<figma-node-id>"`，供浏览器审计脚本重新定位。旋转与嵌套节点使用累积仿射矩阵；祖先裁剪沿节点链计算；文字和素材分别生成，不用整板截图替代页面。

### 5. 浏览器验收

验收脚本在 Chromium 中读取真实 DOM，而不是只检查 HTML 字符串：

| 门 | 检查内容 |
|---|---|
| A | reference、viewport、DPR、阈值和运行清单是否冻结 |
| B | 内容覆盖、有效可见性、祖先裁剪、隐藏或透明节点是否被错误渲染 |
| C | 父子 inset、同级间距与顺序、对齐轴、基线、图标中心和接边关系 |
| D | 逐 TEXT Node Range 行盒、字体加载、额外折行、溢出、颜色与行计划 |
| E | 独立连接器、SVG 路径真实端点、箭头尖端与目标边 |
| F | canonical 原尺寸截图及总览 fit 后的真实可见性 |
| G | 同 viewport、同 DPR、同 reference 下的像素回归 |

各门报告写入 `evidence/`。任一 `fail` 或 `unknown` 在 strict 模式下都会产生非零退出，并汇总到 `summary.json`。

### 6. 单次确定性修复与复验

文字或连接器失败时，管线最多执行一次对应修复：

- `repair_text`：只修正确定性 CSS 声明，不通过缩小字号消除溢出；
- `repair_connectors`：重建独立连接器 SVG，并保留备份与回滚；
- 修复后重新采集浏览器截图、文字、连接器和像素证据。

不会为了让指标通过而隐藏内容、缩放参考图或无限扫描参数。

## 关键实现

### 文字布局

- 保留每个 run 的 `characters`、`fontSize`、`fontName`、`fills`、`lineHeight` 和 `letterSpacing`。
- AUTO line-height 没有实测数据时保留 `normal` 并标记 unresolved，不用盒高除以行数伪造。
- 可通过独立 Figma 校准节点和原生 PDF spans 生成 `text-calibration.json`；只有拼接后的行文本与原节点完全一致时，才写入 `linePlan`。
- 浏览器逐 TEXT Node 建立 Range，使用真实行盒核对换行和行计划。
- 固定文本盒的纵向溢出单列为 `designBoxOverflow`；横向溢出、内容尺寸模式溢出和行计划不一致仍属于硬失败。

### 几何与裁剪

计划层区分不同坐标口径，避免跨族混算：

| 字段 | 含义 |
|---|---|
| `designBox` | 未旋转、仅乘祖先矩阵的布局盒 |
| `renderAABB` | 原生导出提供的画板坐标渲染边界 |
| `pngPlacement` / `exportSize` | 已烘焙旋转的 PNG 在画板中的落点与像素尺寸 |
| `effectiveClipRect` | 沿祖先链计算后的有效裁剪区域 |

### 字体

`check_fonts.py` 检查字体文件是否真实落地，包括 src 路径、MD5、nameID、字重和 cmap 覆盖；浏览器审计再确认字体实际加载。

缺少字体时先按 [`references/font-sourcing.md`](references/font-sourcing.md) 获取并验证，不把仅写一个 `@font-face` 挂载点视为完成。

## 安装与配置

### 1. 准备运行环境

需要：

- **Node.js 20.19+ 或 22.12+**：运行 Figwright MCP；
- **Figma**：免费账户即可；导入开发插件需要 Figma 桌面端；
- **Python 3.10+**；
- **Playwright + Chromium**：执行真实浏览器审计；
- 一个支持 MCP 的客户端，用于连接 Figwright。

### 2. 配置 Figwright MCP

在 MCP 客户端的 `.mcp.json` 中加入：

```json
{
  "mcpServers": {
    "figwright": {
      "command": "npx",
      "args": ["-y", "@figwright/mcp@latest"]
    }
  }
}
```

`npx` 会获取并运行已发布的服务，不需要全局安装。其他 MCP 客户端可使用相同结构。

### 3. 安装并连接 Figma 插件

1. 从 [Figwright 最新 Release](https://github.com/awdr74100/figwright/releases/latest) 下载插件 ZIP 并解压。
2. 在 Figma 桌面端选择 **Menu → Plugins → Development → Import plugin from manifest…**。
3. 选择解压目录中的 `manifest.json`。
4. 打开 **Plugins → Development → Figwright**。
5. 确认插件显示 **Connected**，并在 MCP 客户端执行 `ping` 验证链路。

Figwright 服务端和插件需要配套更新。详细说明以 [Figwright 官方 README](https://github.com/awdr74100/figwright) 为准；本仓库不需要修改版 Figwright。

### 4. 安装本仓库与 Python 依赖

```bash
git clone https://github.com/BANANACAT26/figma-native-html-reconstruction.git
cd figma-native-html-reconstruction

python3 -m pip install pillow numpy scipy playwright fonttools
python3 -m playwright install chromium
```

### 5. 配置本仓库环境变量

```bash
export FIGWRIGHT_SERVER='npx -y @figwright/mcp@latest'
export FIGWRIGHT_SESSION_ID='<figma-file-session>'
```

`FIGWRIGHT_SESSION_ID` 是 Figwright `list_files` 返回的文件会话 ID。设置后，客户端会通过 `use_file` 绑定目标 Figma 文件。

可选配置：

```bash
# 编辑器侧 tools/list 卡住时跳过能力发现，直接按契约工具名调用
export FIGWRIGHT_SKIP_TOOLS_LIST=1

# 能力发现的单次超时与有界重试
export FIGWRIGHT_TOOLS_LIST_TIMEOUT=15
export FIGWRIGHT_TOOLS_LIST_RETRIES=2
```

如果本机使用已安装的 Figwright 包，也可以直接指定入口：

```bash
export FIGWRIGHT_SERVER='node /absolute/path/to/@figwright/mcp/dist/index.mjs'
```

### 6. 运行完整重建

先从 Figma 导出目标画板的同尺寸 `reference.png`。它只用于最终视觉与像素验收，不参与排版。

```bash
python3 scripts/reconstruct.py \
  --node-id '<artboard-node-id>' \
  --reference reference.png \
  --workdir run-01
```

如果需要验收总览 fit 后的真实可见性，额外提供总览证据：

```bash
python3 scripts/reconstruct.py \
  --node-id '<artboard-node-id>' \
  --reference reference.png \
  --workdir run-01 \
  --overview-evidence overview-fit.png
```

### 7. 离线复验

已有导出目录时，可以不再读取 live Figma：

```bash
python3 scripts/reconstruct.py \
  --reference reference.png \
  --workdir rerun \
  --reuse-export-dir run-01/export/artboard \
  --html run-01/page.html
```

如果修改了导出数据或编译器，需要重新生成 `layout-plan.json`，不要沿用旧计划验证新页面。

## 输出目录

一次完整运行主要产生：

```text
run-01/
  page.html                         可编辑固定画板网页
  export/artboard/
    *.png                           独立非文字原生导出素材
    design-ir.json                  归一化设计数据
    component-relations.json        组件与相对关系图
    layout-plan.json                唯一布局计划
    layout-plan.json.sha256         计划哈希
  evidence/
    data-gate.json                  P0 数据契约结果
    content-ledger.json             B 内容可见性
    relations-audit.json            C 相对关系
    text-layout.json                D 文字行盒与字体
    connectors.json                 E 连接器
    browser.png                     Chromium 实际截图
    metrics.json                    G 像素指标
    run.json                        外部产物哈希与运行清单
  summary.json                      各门状态、失败项与未知项汇总
```

`browser.png` 只是验收截图；真正的网页是包含真实 DOM 和独立素材的 `page.html`。

## 常见问题

### MCP 客户端提示 `npx: command not found`

MCP 客户端直接启动进程时，可能不会读取交互式 shell 的 PATH。在终端执行 `which npx`，然后将 `.mcp.json` 的 `command` 改为返回的绝对路径。

### 插件一直显示 Waiting

确认 MCP 客户端正在运行、Figwright 服务已配置、插件在同一台机器的 Figma 中保持打开，并检查本机 loopback 连接是否被安全软件阻止。

### 字体已配置但页面仍使用旧结果

确认构建步骤确实重新执行，并检查 `page.html` 中的 `@font-face`。使用 `scripts/check_fonts.py` 验证字体文件和命名，再用浏览器审计确认 `document.fonts.check()`。

### live 读取或复验卡住

同一个 `FIGWRIGHT_SESSION_ID` 的 live 读取、导出和复验必须串行执行，不要同时启动两路任务访问同一 Figma 会话。

## 已知限制

- 没有校准 sidecar 时，AUTO line-height 仍可能只能使用浏览器 `normal` 并保持 unresolved。
- 字体、烘焙位图内容、色彩管理和抗锯齿会形成像素还原下限。
- 非 PNG 旋转容器、复杂裁剪、live/reference 版本冲突需要逐例复验。
- 内容已烘焙进祖先 PNG 或没有 DOM 盒的节点，部分关系边只能标记 `unknown`。
- 验收流程保证问题可定位、证据可复核、修改可回退，不保证所有输入自动通过所有质量门。

## 目录结构

```text
SKILL.md                     Skill 使用合同、默认流程和验收规则
references/
  sop.md                     完整执行 SOP
  figwright-mcp.md           Figwright 配置、串行会话与降级策略
  geometry-and-connectors.md 坐标、旋转、GROUP/INSTANCE 与裁剪
  connectors.md              独立连接器与箭头尖端验收
  verification.md            浏览器、几何、文字与像素验收
  font-sourcing.md           字体获取、验证与落地
  qa.md                      返修与回归排查
scripts/
  reconstruct.py             一条命令运行完整 strict 管线
  export_artboard.py         live 读取、原生导出与几何采集
  compile_design.py          manifest → IR → relations → layout plan
  data_gate.py               构建前 P0 数据门
  build_fixed_html.py        只根据 plan 构建 HTML
  check_fonts.py             字体文件与生效检查
  audit_*.py / repair_*.py   内容、关系、文字、连接器审计与修复
  verify.py / pixel_diff.py  截图、像素对比与证据哈希
tests/                       构建器、编译器和质量门回归测试
docs/                        README 配图
```

## License

代码与 Skill 文本采用 MIT。示例字体不随仓库分发，授权说明见 [`references/font-sourcing.md`](references/font-sourcing.md)。Figwright 及其插件遵循原项目许可。
