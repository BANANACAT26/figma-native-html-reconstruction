# 几何、旋转、裁剪与连接器

## 坐标空间先统一

比较前回答四个问题：

1. Figma 数值相对页面、画板、父 FRAME、GROUP 还是 INSTANCE？
2. DOM 数值相对 document、viewport 还是 stage？
3. 双方是布局盒、注册盒、旋转后 AABB，还是实际渲染边界？
4. 是否包含阴影、描边、蒙版和裁剪？

任一项不同，先归一化再比较，不直接生成缺陷。

## 坐标族（同口径，禁止跨族混算）

每个渲染节点同时携带四个盒，各有固定坐标系，**不得互相推导或角点重投影**：

- `designBox`：未旋转、仅乘祖先矩阵得到的布局盒（CSS left/top/width/height 的依据）。
- `renderAABB`：export 自带的画板坐标渲染真值（来自 `intrinsicRenderBounds`/`relativeToArtboard`）。
- `pngPlacement` + `exportSize`：已烘焙旋转的 PNG 像素落点与像素尺寸。

优先级：定位 DOM 用 `renderAABB`（回退 `designBox`）；资产 `<img>` 平铺在 `pngPlacement`，尺寸取 `exportSize`。**禁止**把父坐标 local 角点乘画板 absoluteTransform 来重算渲染盒（会 double-translate）；**禁止**对 rotation-baked PNG 再套 CSS matrix、再按 designBox 拉伸比例。旋转/翻转节点的 designBox 与 renderAABB 跨族不可直接比，标 `boxComparability.rotated`。plan 层必须透传 exportSize 与 asset 文件名，data_gate 缺这些即阻断。

## 仿射变换

使用节点相对变换并沿祖先链组合，矩阵按列序 `[a,b,c,d,tx,ty]` 复合。四角经过同一矩阵后取 min/max，可得旋转后的轴对齐边界。保存矩阵和中间结果以便复核。

两条序列化约定必须显式处理，否则产生假偏差：

- **GROUP 子节点位于 GROUP 的父空间**：子节点直接继承父仿射，不要再叠加 GROUP 自身位移。先用已知节点验证矩阵口径再推广整页。
- **旋转/嵌套层一次性用全仿射**：输出 HTML 时优先用节点累积的 absoluteTransform 直接生成通用 CSS `matrix(...)`（transform-origin 为 0 0），不用 local 盒再叠 CSS rotate，也不为任何具体角度写特判。缺 absoluteTransform 却带旋转角时按渲染 AABB 放置并披露。

INSTANCE 的 PNG 放置锚定按 PNG 像素尺寸决定：PNG 与子树渲染并集在容差内一致则锚定子树并集，否则锚定节点自身渲染盒；不要在可能很小的 instance 框上做中心锚定，也不写固定补偿偏移。

## 容器与旋转的常见伪偏差

- **容器扁平化**：HTML 只映射叶子，GROUP 无对应 data-n 不一定是内容缺失。
- **旋转外接框**：Figma 未旋转 layout box 与 DOM 旋转后 AABB 不可直接比。
- **位图效果**：阴影/描边让 PNG 大于设计盒，不应把扩大全部判为位置错。
- **文本盒**：table/table-cell 外层布局盒与 Range 行盒承担不同作用。
- **局部坐标**：手机屏、嵌套 FRAME 的坐标要加正确祖先变换，且只加一次。
- **裁剪**：裁掉的部分不可见，raw PNG 尺寸不等于有效可见盒。

无法同口径测量时标 `not_comparable` 或 `needs_visual_review`，不要写成 P0 缺陷。

## 连接器的关系表

每条连接单独记录：

```json
{
  "node_id": "connector-id",
  "source_id": "source-card-id",
  "source_edge": "right | left | top | bottom",
  "target_id": "target-card-id",
  "target_edge": "right | left | top | bottom",
  "source_point": [0, 0],
  "target_point": [0, 0],
  "mid_path": [],
  "arrowhead_at": "target",
  "marker_style": "dot | diamond | none"
}
```

从 live 设计和实际画面共同确定关系。`visible=true` 只说明属性，不足以证明在参考画面中可见；也不能因此删除看似多余的真实路径。

## 连接器逐条验收门

扁平化导出的连接器 PNG 内部看不到箭头尖端像素，因此外框级合同是**外框到达指定边**，而非"尖端像素命中"：

- 每条连接器给出 source 边、target 边及两侧选择器；测量连接器外框到 source 边中点、target 边中点的 gap（横向验右/左边，纵向验下/上边，轴显式指定）。
- gap ≤ 公差（默认 2px）该条 `pass`；任一边 gap 超公差该条 `edge-gap` 失败，附 sourceGap/targetGap/公差。
- 缺 source 或 target 选择器/证据只能标 `unverifiable`——**绝不静默判通过，也不把多条 gap 取平均后整体判过**；某一条仍错位时不能被其余条的平均误差掩盖。
- asset 缺失标 `missing-asset`。

**外框级之外还有独立门 E（真尖端审计）**，契约见 [connectors.md](connectors.md) 与脚本 `scripts/audit_connectors.py`：

- 优先用 `export_connector_svg.py` 导出独立小节点 SVG（线体+拐点+圆点+箭头一体），审计用 `path.getPointAtLength(totalLength)` 取最长描边路径的真实端点，经 `getScreenCTM()` 映射到视口坐标，与 target/source 边中点比距离。这才是 tip-verified `pass`。
- PNG 外框到达边只是 `degraded-bbox`（`tipVerified=false`），strict 门下不算过；PNG 外框不能冒充 SVG 真尖端。
- 连接器必须位于承载 fit/scale transform 的同一个 `.stage` 容器内（否则总览 fit 后消失，判 `stage-outside`），且 computed stroke-width > 0、非 display:none。
- **禁止整板 SVG**（会烘焙文本/位置）；连接器子树含 TEXT 时拒绝烘焙。
- 画板无连接器时必须用 `connectors.manifest.json` 证明 `connectorCount == 0`，不能跳过本门。

## 箭头修复规则

- 先确定源/目标组件与连接边，再定位箭头，不根据热点猜位置。
- 比较两端点、中段路径和箭头头部，不能只看外层 img 的 left/top。
- 旋转实例的注册框可能远离内部实际路径；按实际渲染内容或原生矢量路径计算。
- 要让**真正的箭头尖端**到达目标边，横向与纵向使用同一验收规则。
- 不在原箭头与目标之间另补一段普通线来伪装连接。
- 不把整组统一平移当作逐条修复；不同导出单元的内部偏移可能不同。
- 不用平均残余误差掩盖某一条仍错位。

优先原生导出完整连接器；若必须重绘 SVG，把线体、拐点、圆点和箭头头部作为一个组件处理，并隐藏被替代的旧资产，防止双显。

## 树状架构连线

- 一级分支共享连续主干，不为每个标签拼一根孤立折线。
- 水平分支到卡片实际连接边；竖向主干覆盖最上到最下分支中心。
- 保留菱形、圆点等节点标记和真实条目数。
- 新旧线层只能有一个可见；隐藏旧 PNG 时检查替代 SVG 已同步。
- 总览 fit 后仍需看见线体与箭头头部，不能只验原尺寸 canonical。

## 相对定位验收

绝对位置和尺寸通过后，再验：

- 左右连接：source 右边到 target 左边的水平/垂直差。
- 上下连接：source 下边到 target 上边的水平/垂直差。
- 图标：图标中心相对所属 pill/card 中心。
- 文本：文本行盒相对父容器的间距。
- 同级：间距、顺序、基线和共享对齐轴。

相对关系能发现“两个元素分别看似接近，但连接方向/所属关系错误”的问题。
