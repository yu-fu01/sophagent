# Poster Generation Playbook

使用场景：
- 用户确认需要生成海报、封面图、分享图、产品图、攻略图
- 用户要求给文案配图，或要一套多渠道配图包
- 用户提供了商品照片/logo，要求基于实物生成营销海报

## 执行原则

- 文案完成后，先给配图建议，再生成图片
- 画面 prompt 只描述场景、主体、氛围；风格由参数控制
- 不能编造海报上的文字内容；标题、价格、活动信息应来自用户或业务数据
- 用户提供了商品图/logo 时，必须通过 `--reference-image` 传入，让模型基于真实素材生成
- 生成前检查 `references/content-safety.md`

## 海报类型

| 用途 | `--type` | 默认尺寸 | 典型场景 |
|------|----------|---------|---------|
| 朋友圈海报 | `moments` | 1080×1080 (1:1) | 朋友圈促销方图 |
| 小红书封面 | `xiaohongshu` | 1080×1440 (3:4) | 小红书笔记首图 |
| 公众号头图 | `wechat-header` | 900×383 (16:9) | 推文列表大图、文章顶部封面 |
| 公众号方形预览 | `wechat-square` | 200×200 (1:1) | 分享卡片、朋友圈预览 |
| 微信群分享图 | `share-card` | 500×400 (4:3) | 群内分享卡片配图 |
| 产品展示图 | `product` | 1024×1024 (1:1) | 文内配图、产品特写 |
| 攻略图/信息图 | `guide` | 1080×1440 (3:4) | 攻略、知识卡片 |

公众号封面必须成对生成：`wechat-header` + `wechat-square`。

## 风格预设

| `--style-preset` | 效果 | 适用场景 |
|------------------|------|---------|
| `promo` | vivid + flat-vector + bold | 促销、打折、限时活动 |
| `elegant` | earth + hand-drawn + subtle | 高端感、轻奢品牌 |
| `minimal` | mono + flat-vector + subtle | 简约、专业、商务 |
| `festive` | warm + painterly + bold | 节日、庆典、周年 |
| `cozy` | warm + painterly + subtle | 美食、居家、生活方式 |
| `kawaii` | pastel + flat-vector + balanced | 少女风、萌系、甜品 |
| `morandi` | earth + hand-drawn + subtle | 莫兰迪色、高级感 |
| `pop-art` | vivid + flat-vector + bold | 潮流、年轻化 |
| `vintage` | retro + hand-drawn + balanced | 文艺怀旧 |
| `blueprint` | dark + chalk + bold | 技术解析、流程图 |
| `notion` | mono + hand-drawn + subtle | 知识卡片、清单风 |
| `watercolor` | pastel + painterly + subtle | 文艺、淡雅 |
| `corporate` | cool + flat-vector + balanced | 企业宣传、商务 |

## 文字叠加

`--text-overlay` 使用 JSON，字段均可选：

```json
{
  "title": "母亲节特惠",
  "subtitle": "康乃馨花束 限时8折",
  "price": "¥128",
  "footer": "5月10日-12日 到店即享"
}
```

## 参考图（Reference Image）

通过 `--reference-image` 传入用户提供的商品照片、logo 等素材，模型会将素材高保真地融入海报。

**使用规则：**

- 可多次指定，最多 10 张（API 限制）
- 接受本地文件路径，支持 jpg/jpeg/png/webp/gif
- 单张图片不超过 20 MB
- prompt 中应描述参考图在海报中的角色（如"将商品照片作为海报主体"）

**典型场景：**

| 场景 | 参考图 | prompt 要点 |
|------|--------|------------|
| 商品促销海报 | 商品实拍照 | "以参考图中的商品为主体，置于XXX场景" |
| 品牌宣传海报 | 品牌 logo | "将 logo 融入海报顶部/角落" |
| 商品+logo 组合 | 商品照 + logo | "商品居中展示，logo 置于右下角" |

## 常用命令

```bash
# 纯文字生图：朋友圈促销海报 + 文字叠加
uv run --project {baseDir} \
  python {baseDir}/scripts/generate_poster.py \
  --type moments --style-preset promo \
  --prompt "bouquet of red carnations on a warm-lit wooden table, Mother's Day atmosphere" \
  --text-overlay '{"title":"母亲节特惠","price":"¥128","footer":"5.10-5.12 到店即享"}'

# 商品图 + 文字 → 营销海报
uv run --project {baseDir} \
  python {baseDir}/scripts/generate_poster.py \
  --type moments --style-preset promo \
  --reference-image /path/to/product-photo.jpg \
  --prompt "以参考图中的商品为主体，置于干净的浅色背景上，周围点缀节日装饰元素，营造促销氛围" \
  --text-overlay '{"title":"母亲节特惠","price":"¥128"}'

# 多张参考图：商品 + logo
uv run --project {baseDir} \
  python {baseDir}/scripts/generate_poster.py \
  --type xiaohongshu --style-preset elegant \
  --reference-image /path/to/product.jpg \
  --reference-image /path/to/logo.png \
  --prompt "以参考图中的商品为主体，logo 置于海报右下角，整体风格高级简约"

# 小红书封面（纯生图）
uv run --project {baseDir} \
  python {baseDir}/scripts/generate_poster.py \
  --type xiaohongshu --style-preset kawaii \
  --prompt "a slice of strawberry cake on pastel pink plate, soft natural light, minimalist"

# 公众号封面（成对生成）
uv run --project {baseDir} \
  python {baseDir}/scripts/generate_poster.py \
  --type wechat-header --style-preset cozy \
  --prompt "aerial view of a flower shop with colorful bouquets, warm golden light"

uv run --project {baseDir} \
  python {baseDir}/scripts/generate_poster.py \
  --type wechat-square --style-preset cozy \
  --prompt "colorful flower bouquet, warm tone, simple composition"
```

## 输出处理

脚本输出 `KEY=VALUE`。必须提取并展示给用户，不能原样丢 stdout：

```text
POSTER_TYPE=moments
POSTER_SIZE=1080*1080
STYLE_PRESET=promo
REFERENCE_IMAGES=1
STATUS=succeeded
IMAGE_URL=https://example.com/poster.png
```

`REFERENCE_IMAGES` 仅在使用了参考图时出现。

## 生成失败处理

如果脚本执行失败（exit code 非 0），检查 stderr 输出中的 `STATUS` 行：

| stderr STATUS | 原因 | 处理方式 |
|---------------|------|---------|
| `permission_denied` | 用户账户未开通所调用图片生成模型权限 | **立即告知用户**："图片生成功能需要开通权限，请联系 Sophclaw 平台客服处理。"不要重试、不要换参数。 |
| `api_error` | API 调用失败（网络、限流、服务端错误等） | 可重试 1 次；仍失败则告知用户稍后再试，并展示错误信息。 |
| 无 STATUS 行 | 参数错误、文件不存在等本地问题 | 检查命令参数是否正确。 |

展示格式：

```text
### 朋友圈促销海报
- 内容：[图片画面描述]
- 尺寸：1080×1080
- URL：https://example.com/poster.png

![朋友圈海报](https://example.com/poster.png)
```

## Prompt 编写要点

| 需求 | 不要写 | 要写 |
|------|--------|------|
| 花店促销海报 | "好看的花" | "bouquet of red carnations on warm-lit wooden table, Mother's Day atmosphere" |
| 蛋糕店宣传 | "蛋糕图片" | "a three-layer strawberry cream cake with fresh berries, soft bakery window light" |
| 美容院活动 | "护肤品" | "luxury skincare set on marble surface, rose petals, golden hour light" |
| 保险宣传 | "专业感图片" | "family silhouette under protective umbrella, warm sunset sky, calm and secure" |

## 构图原则

- 留白 40-60%，尤其是带文字叠加时
- 主要元素居中或偏左，形成明确视觉锚点
- 人物用剪影或卡通形象，不用写实人像
- 方形预览图主体必须居中，保证 200×200 也清楚可辨

## 收尾检查

- 尺寸是否符合目标平台
- 公众号是否成对生成
- 小红书封面是否为 3:4 竖图
- 文字叠加是否清晰可读
- 最终结果是否通过 `references/quality-checklist.md` 的海报检查项
