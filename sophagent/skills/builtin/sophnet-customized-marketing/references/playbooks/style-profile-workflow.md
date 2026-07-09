# Style Profile Workflow

使用场景：
- 首次使用本 skill，需要建立老板风格档案
- 已有风格档案，需要读取后用于本轮写作
- 用户对文案提出风格反馈，需要更新档案

## 核心规则

- 用户的风格偏好、表达习惯、品牌调性等信息，只允许通过 `style_profile.py` 写入 `user-style-profile.md`
- 严禁把风格信息写入 SophAgent memory SophAgent user profile；只读取其中与表达风格相关的信息

## 存储位置

- 档案目录：`{workingDirectory}/sophnet-customized-marketing`
- 档案文件：`user-style-profile.md`

## 标准流程

### 1. 先读取现有档案

```bash
uv run --project {baseDir} \
  python {baseDir}/scripts/style_profile.py read \
  --profile-dir {workingDirectory}/sophnet-customized-marketing
```

- `STATUS=ok`：后续文案按档案风格生成
- `STATUS=not_found`：进入首次建档流程

### 2. 首次建档

依次执行：

1. 读取 SophAgent 已注入的 MEMORY / USER PROFILE，提取表达习惯、品牌调性、常用语、禁忌偏好
2. 若未提取到无相关信息，主动询问用户：

```text
在为你写文案之前，想先了解你的风格偏好。
你平时发朋友圈/群消息是什么风格？
A. 热情活泼，喜欢用 emoji 和感叹号
B. 简洁专业，不废话
C. 温馨亲切，像跟朋友聊天
D. 其他（请描述）
```

3. 获取到信息后，通过 `write` 建档：

```bash
uv run --project {baseDir} \
  python {baseDir}/scripts/style_profile.py write \
  --profile-dir {workingDirectory}/sophnet-customized-marketing \
  --content "..."
```

### 3. 风格学习与更新

当用户给出此类反馈时，更新档案：

- “太正式了”
- “再活泼点”
- “别用 emoji”
- “更像我平时发朋友圈的语气”

```bash
uv run --project {baseDir} \
  python {baseDir}/scripts/style_profile.py update \
  --profile-dir {workingDirectory}/sophnet-customized-marketing \
  --key "表达风格" \
  --value "用户反馈希望更活泼，之前太正式"
```

## 命令索引

| 操作 | 命令 |
|------|------|
| 读取 | `uv run --project {baseDir} python {baseDir}/scripts/style_profile.py read --profile-dir {workingDirectory}/sophnet-customized-marketing` |
| 首次写入 | `uv run --project {baseDir} python {baseDir}/scripts/style_profile.py write --profile-dir {workingDirectory}/sophnet-customized-marketing --content "..."` |
| 更新单项 | `uv run --project {baseDir} python {baseDir}/scripts/style_profile.py update --profile-dir {workingDirectory}/sophnet-customized-marketing --key "表达风格" --value "..."` |
| JSON 格式读取 | `uv run --project {baseDir} python {baseDir}/scripts/style_profile.py read --profile-dir {workingDirectory}/sophnet-customized-marketing --format json` |

## 档案结构示例

```markdown
# 用户营销风格档案

## 表达风格
热情外放，喜欢用感叹号和 emoji，口语化

## 品牌调性
温馨、亲切、接地气

## 常用语/口头禅
"姐妹们"、"真心推荐"、"用过都说好"

## 禁忌偏好
不喜欢太正式的书面语，不用"尊敬的客户"

## 偏好渠道
主要发朋友圈和微信群，偶尔发小红书
```

维度名称不固定，可根据对话积累的信息扩展。
