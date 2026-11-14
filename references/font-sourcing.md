# 字体获取与落地（缺字体先下载）

用户明确要求过："字体不对啊，如果没有对应字体应该去下载"。因此**缺字体是待办，不是结论**。本文件是 3.6 的可执行细则：怎么确认缺什么、去哪里找、怎么验、怎么让它真正生效、哪些路已经走不通。

## 1. 先确认"缺什么"，不要凭 CSS 名字判断

字体缺口的唯一可信来源是 per-run 的数据，不是页面上的 `font-family` 字符串。

```python
# text-manifest.json -> 每个 run 的 fontName（family / style / PostScript）
{"nodeId": "1:17", "runs": [{"fontName": {"family": "FZXS15", "style": "Regular"},
                             "fontSize": 82, "characters": "A Creative ..."}]}
```

要点：

- **`fontName='mixed'` 必须逐 run 展开**。一个节点里可能同时出现两款字体（本次案例：标题 FZXS15、导航 HK Grotesk Wide，导航节点的 `fontName` 字段是 `mixed`，只有展开 runs 才看得到）。
- **记录三重身份**：`family`（CSS 里要写的）、`style`（字重/斜体）、PostScript 名（nameID 6，搜文件时最准）。
- 与 `audit_text.py` 的 `fontsMissing` 交叉核对；`fontsVerified` 是"真的加载了"，不是"写了 `@font-face`"。

## 2. 先查本机（命中率不低）

```bash
# 用 nameID 1/4/6 匹配，而不是文件名匹配（字体文件名常被发行方改过）
python3 - <<'PY'
import pathlib
from fontTools.ttLib import TTFont
roots = ["/System/Library/Fonts", "/Library/Fonts", str(pathlib.Path.home()/ "Library/Fonts"),
         "/Applications/Figma.app/Contents/Resources"]        # 按平台调整
keys = ["FZXS", "像素", "HK Grotesk", "HKGrotesk"]
for p in pathlib.Path(roots[0]).rglob("*"): pass            # 遍历各 root，逐个字体读 name 表
PY
```

命中就**复制进项目的 `fonts/` 并改成 `@font-face` 里的文件名**，不要装进系统（避免污染用户环境，也让交付自包含）。

顺带说明：Figma 的 `font_cache.json` 只缓存**用户实际用过的**字体，缺字体在里面查不到属正常，不能据此断定"世上没有这款字体"。

## 3. 上网找：正文级检索 + 按确切文件名搜

搜索摘要常常只给站点首页，必须**进入页面读正文和直链**（浏览器带渲染，比 curl 更稳）。

有效做法（按优先级）：

1. **按 PostScript / 完整文件名搜**：`"FZXS15--GB1-0.ttf"`、`"HKGroteskWide-Regular.otf"`、`"HK Grotesk Wide ExtraBold.otf"`。字体分发站会把文件名当索引，命中率远高于搜中文名。
2. **`site:` 限定分发站**：`site:zhaozi.org "HK Grotesk Wide"`。
3. **进入字体详情页取"文件区"直链**：找形如 `/download/<hash>?filename=X.otf` 的链接，**用直链下载**，不要点"官网下载 / 下载客户端"。本次两款字体都是这样拿到的：
   - `https://down-c7.zhaozi.org/download/c/7/<md5>?filename=FZXS15--GB1-0.ttf`
   - `https://down-b8.zhaozi.org/download/b/8/<md5>?filename=HKGroteskWide-Regular.otf`
4. **同款字体多镜像交叉验证**：详情页会同时给出 `文件名 / MD5 / 大小`。同一 MD5 在多站出现，即同一份文件被多处分发，可信度显著提高。

## 4. 必须校验，再落地

```bash
md5 -q fonts/FZXS15--GB1-0.ttf     # 与站点公布的 MD5 逐字符比对，不是"差不多"
python3 -c "
from fontTools.ttLib import TTFont
f=TTFont('fonts/FZXS15--GB1-0.ttf', lazy=True)
for n in f['name'].names:
    if n.nameID in (1,2,4,6): print(n.nameID, n.toUnicode())
print('unitsPerEm', f['head'].unitsPerEm, 'glyphs', len(f.getGlyphOrder()))
print('cover A-Z', all(ord(c) in f.getBestCmap() for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'))
"
```

落地清单（缺一条就可能"下载了但不生效"）：

- `@font-face` 的 `font-family` 字符串**与 Figma `fontName.family` 逐字一致**（含空格与大小写：`'HK Grotesk Wide'` 不是 `'HKGrotesk-Wide'`）。
- **`src` 的 URL 相对谁**：`@font-face` 写在**独立 CSS 文件**里时，相对 CSS 文件所在目录；被**内联进 HTML** 时相对 HTML 所在目录。用 `fonts/xxx.ttf` 这种前缀时，这两种情况解析出的路径不同——本次踩过（`fonts/fonts.css` 里的 `fonts/FZXS15…` 会解析成 `fonts/fonts/…`）。
- 字重要对上：`font-weight: 400` 的 `@font-face` 配 Regular 文件；**ExtraBold 不能顶替 Regular**。
- 落地后必须复验：`check_fonts.py` + `audit_text.py` 的 `fontsMissing` 为空 + 浏览器 `document.fonts.check()` 为真。
- 字体改完要**重新 build**，别在 `--reuse-export-dir` 的空转里"复验"旧页面（`grep -c "@font-face" page.html` 应为非 0）。

## 5. 高频坑（已走过，勿重试）

| 现象 | 实情 |
| --- | --- |
| 详情页只有"官网下载""下载客户端" | 官方渠道只推自有客户端，不给单文件；直接去找同款文件的直链镜像。 |
| 页面写着"免费下载"但点了要登录/扫码/SVIP | 该站不提供匿名下载；换站，不要卡在登录上消磨轮次。 |
| GitHub 仓库里有同家族字体，但没有需要的变体 | 免费开源版常常只发标准宽度/部分字重；**"Wide/Oblique/Pro"等变体往往是独立家族或商业版**，用仓库里有没有 `wide` 目录判断，不要看仓库 star。 |
| 千 1 字体站（1001fonts）页面出现 "Wide" | 大概率是**站内导航分类**（Wide / Extra Wide 分类菜单），不是变体。 |
| 字体名里有空格（"HK Grotesk Wide"） | 它可能是一个**独立的 family**，不是 HK Grotesk 的宽度轴；搜"HK Grotesk"只会找到标准宽度那套。 |
| 找到 ExtraBold/Black 就以为够了 | 原稿 run 是 Regular；字重不同 → 字面宽度与换行全变，等于没修。 |
| 摘要里"有"但页面 404 / 加载超时 | 搜索摘要会复述站点旧页；以页面正文和实际下载结果为准。 |

## 6. 授权口径（要如实写进交付）

- **方正 / 汉仪等商业字库的"非商免"**：个人非商业可用，**商业用途需向字库取得书面授权**。交付里必须写清这一点，并提示用户正采授权后再商用。
- **OFL 开源字体**（如 HK Grotesk 家族）：可自由使用，但仍要写清来源文件。
- 无论哪种来源，都保留"把同名文件替换成自有授权版本即可重新构建"的路径——`@font-face` 已内联，替换文件后重跑 build 即生效。

## 7. 自检脚本

```bash
python3 scripts/check_fonts.py \
  --fonts-dir layout-01/fonts --css layout-01/fonts/fonts.css \
  --html layout-01/page.html \
  --plan layout-01/export/artboard/layout-plan.json \
  --text-manifest layout-01/export/artboard/text-manifest.json \
  --glyphs "ABCabc0123" --out evidence/fonts.json
```

输出逐字体报告：src 是否解析到文件（按页面内联与 CSS 外链两种上下文各试一次，能直接抓出 `fonts/fonts/…` 双前缀）、字节数、MD5、nameID 1/2/4/6、判定字重、cmap 覆盖。

判定语义：

- `OK`：文件在、family 对、字重覆盖该 style。
- `SYS`：该 family 由操作系统提供（如 macOS 的 Zapfino），不算缺口，但会提示"未随交付打包，换机器可能回退"。
- `GAP`：没声明、文件缺失、字重不对（拿 ExtraBold 顶 Regular 属于此列）、name 表不一致、cmap 缺字 → `exit 1`。

**必须带 `--plan`**：`text-manifest.json` 里 `fontName='mixed'` 的节点没有 per-run 字体，per-run 真值在 `layout-plan.json` 的 `textPlans[].runs[].fontName`；不传会把这些节点误报为 gap。
**必须带 `--html`**：构建器会把 `--font-css` 内联进页面，src 就相对 HTML 目录解析；只按 CSS 目录解析会把正常落地的字体误报为 MISSING。

`--strict` 下只要有一项 `GAP` 就非零退出——此时回到第 3 步继续找，不要直接进"披露回退"。
