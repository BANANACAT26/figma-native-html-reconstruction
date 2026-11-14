# 连接器（connector）独立模块：契约、验收与历史教训

本文件描述独立连接器审计模块 `scripts/audit_connectors.py` 与小节点 SVG 导出 `scripts/export_connector_svg.py` 的通用契约。模块不绑定任何具体画板、节点 ID、颜色或文本；所有几何数来自 spec 与 DOM 实测。

## 1. 历史证据概括

以下教训来自多轮真实重建回归，已抽象为通用规则；不绑定具体画板名、节点 ID、颜色或像素值。

### 教训 A：位图连接器末端“补直线”不是连接器

- **被否决做法**：在已放置的 PNG 连接器末端用 SVG 短线段“延长”到目标边（每条补若干十像素级的短段，颜色硬编码为项目主色/辅色）。问题：PNG 内部路径已旋转，注册盒与实际渲染内容相差很大；补线只在外框端点接了一段直线，**不是**真正的连接器。
- **最终做法**：删除这些 PNG，用**一个**完整 SVG 重画每条连接器——线体（rounded S-curve，水平→垂直→水平，Q 曲线拐角）、拐点、圆点（外环描边 + 内环填充）、箭头头部作为同一组件。横向与纵向同一规则：圆点端贴 source 边中点，箭头尖端贴 target 边中点，端点误差控制在像素级公差内。
- 教训：**不得**在原资产与目标之间另补普通直线；**不得**整组平移；不同导出单元内部偏移不同，必须逐条算。

### 教训 B：独立 SVG 必须落在 stage 容器内

- 上一轮把连接器 SVG 插到某画板 `</section>` 之后（下一个画板开头之前），导致 SVG **在 stage 容器之外**，不参与 fit 缩放 transform，总览里不可见。
- 修法：把同一 SVG 移回该画板的 `.stage` 内部，与同画板隐藏 PNG 同坐标系。修后实测：`svgInDom=true`、路径数非零、stageTransform 生效、computed stroke 非空、strokeWidth 非零、首条线 rect 非零。
- **抽象为通用验收**（不绑具体画板名）：
  1. 独立 SVG 必须位于正确的 stage 容器内（即承载 fit/scale transform 的同一个祖先元素）；
  2. 必须参与 stage transform（bounding rect 非零、与 stage matrix 一致）；
  3. marker/stroke 在总览实际可见（computed stroke-width > 0、路径数 > 0、非 display:none）。

## 2. 连接器记录结构（每条独立）

```json
{
  "id": "conn-1",
  "selector": "#conn-1",
  "sourceSelector": "#card-a",
  "sourceEdge": "right",
  "targetSelector": "#card-b",
  "targetEdge": "left",
  "arrowheadAt": "target",
  "tolerancePx": 2
}
```

- `sourceEdge` / `targetEdge` ∈ `left|right|top|bottom`，显式指定，不按左右/上下猜。
- `arrowheadAt` ∈ `source|target|none`。圆点端与箭头端由该字段区分，不按颜色猜。
- 每条连接器单独判定，不整组平移、不跨条取平均。

## 3. 资产优先级

1. **原生独立小节点 SVG**（优先）：`export_connector_svg.py` 对单个连接器节点导出 SVG，线体/拐点/圆点/箭头头部一体。审计时按以下顺序解析箭头尖端：
   - **chevron（通用）**：短的 3 点 stroked 折线 `M p0 L p1 L p2`，p1 为顶点（非共线、长度相对主线 < 50%）。p1 即箭头尖端（Figma 原生 stroked chevron 箭头）。排除 `<marker>`/`<defs>` 内部的 path。
   - **marker-end/marker-start**：在主 path 上检测到 CSS marker，用 `getPointAtLength(end|0)` 取路径端点（marker ref 点与尖端对齐）。
   - **filled triangle**：无 stroke 的小填充 path/polygon。
   - 尖端通过 `getScreenCTM()` 映射到视口坐标。
2. **PNG 回退**：只有外框可测。允许 `degraded-bbox` 结论（外框到达两侧边），但**绝不能**把 PNG 外框冒充为箭头尖端；tipVerified=false。
3. **禁止**整板 SVG 导出、禁止把文本画进连接器 SVG。

## 3a. 边线（edge-line）契约，不是边中点

- 一条边是一条**直线段**（如 target left edge = x=const，y 范围 [top, bottom]）。
- 连接器外框/尖端只要在**垂直于边的方向上到达该直线**（open gap = 0，允许穿入卡片），并且沿边方向与卡片跨度有重叠，就算到达。
- **尖端在沿边方向的坐标不要求等于边中点**。S 曲线连接器的出口方向决定尖端落在沿边的哪个位置；强制中点会把正确的连接器误判为 edge-gap。
- open gap 只算"开口缝"：外框/尖端已经越过边线进入卡片时不算失败。

## 3b. 缺 source/target

仍 `unverifiable`，不判过。禁止平移、固定补偿、跨条平均。

## 3c. 构建期自动接入

`build_fixed_html.py` 启动时自动查找 `<export_dir>/connectors/connectors.manifest.json`（或同目录），把清单中带独立小节点 SVG 的连接器层从 `<img>` 替换为同 CSS 盒的 inline `<svg>`（保留 `data-n`、`preserveAspectRatio="none"`、stage 层级、clip），不再需要 run 内 wire 脚本。含 `<text>` 的 SVG 或缺失文件自动回退 PNG，并在 build report 中披露 `connector-svg-refused/unavailable`（auditor 按 degraded-bbox 处理）。整板 SVG 不在清单里（`export_connector_svg.py` 拒绝）。

## 4. 判定机读 verdict

| verdict | 含义 | tipVerified |
|---|---|---|
| `pass` | SVG 真实尖端到达 target/source 边中点（≤ 公差），线体框也到位 | true |
| `degraded-bbox` | PNG 外框到达两侧边中点，但尖端不可见 | false |
| `edge-gap` | 连接器外框未到达指定边 | false |
| `tip-miss` | SVG 尖端测得但偏离目标边 | false |
| `stage-outside` | 连接器不在 stage 容器内（见教训 B） | false |
| `invisible` | 零尺寸 / stroke-width=0 / display:none / 无描边路径 | false |
| `missing-asset` | selector 找不到元素 | false |
| `unverifiable` | source/target 选择器缺失或不匹配 | false |

- 缺 source/target **不判过**，直接 `unverifiable`。
- 未知证据一律 `unverifiable`，不静默通过。

## 5. 严格退出门（"未测不能给通过"）

`audit_connectors.py --strict` 在以下任一存在时退出码 2，**默认从严**——只要不是 tipVerified=true 的 `pass`，全部阻塞：
`degraded-bbox`、`edge-gap`、`tip-miss`、`stage-outside`、`invisible`、`missing-asset`、`unverifiable`。

PNG 外框到达边虽然在 summary 里单独列为 `degraded-bbox`（诚实报告外框结果），但在 strict 门里**不算通过**，因为没有真正验证箭头尖端。

显式宽松模式：`--allow-degraded-bbox` 把 `degraded-bbox` 从 strictFail 里剔除（其余硬失败仍然阻塞）。只有当某条连接器暂时只能拿到 PNG、且上游已计划补 SVG 时，才显式打开这个开关；不要默认使用。

spec 结构错误（缺 selector/边、边名非法）退出码 3。

**空 spec 契约**：`connectors: []`（或缺省）是合法状态——对应 `connectors.manifest.json` 中 `connectorCount=0` 的画板。此时 validator 不报错，summary 标 `empty=true`、`strictFail=0`，即使 `--strict` 也退出 0；stage probe 仍会报告 stage 元素是否存在、transform 是否生效。source/target 字段校验只对实际列出的连接器生效，不对空清单做检查。这与 reconstruct 端空清单证明契约对齐。

## 5.1 reconstruct 默认 fallback 循环

门 E `--strict` 失败时，`reconstruct.py` 自动跑一次 `repair_connectors.py`（以 `layout-plan.json` 为 connector plan）：全 stage 坐标 SVG 重建、source 与真尖端吸附到计划边、水平/竖直/共享主干、消除新旧双显；自带备份与回滚——修复后不能全部适用边通过或引入回归则回滚原 HTML。重建后自动复验一次门 E。`unverifiable`（缺 source/target）被跳过并阻断，**绝不平均成 pass**；一轮后仍 fail 如实 fail，不第二轮扫参。

## 6. 测试覆盖（tests/test_connectors.py）

合成回归，不依赖真实画板：
- 横向 marker-end SVG：pass，tipVerified=true。
- 纵向 marker-end SVG：同规则 pass。
- 横向 stroked chevron、off-midpoint 尖端（尖端 y 不在边中点）：chevronDetected=true、tipKind=chevron、pass + tipVerified。
- SVG 移出 stage：stage-outside。
- stroke-width=0 / 隐藏：invisible。
- PNG 外框到达边：degraded-bbox，tipVerified=false（不能冒充 pass），strict 下阻塞；`--allow-degraded-bbox` 显式放行。
- 缺 source/target：unverifiable。
- 尖端 open gap 超差：tip-miss；外框 open gap 超差：edge-gap。
- 空 connectors 列表：validator 不报错、summary.empty=true、strictFail=0、CLI `--strict` 退出 0。
- summary 计数与 strictFail 正确（含宽松模式）。
