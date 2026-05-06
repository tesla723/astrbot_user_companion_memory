# User Companion Memory

一个面向 AstrBot 的“用户分类记忆”插件。

![logo](./logo.png)

它不追求把整段聊天都做成长篇总结，而是专门维护那些真正会长期影响陪伴体验的短记忆：用户偏好、双方约定、近期事件、可复用事实，以及“知识放在哪”这类索引信息。插件会在每回合请求前按类别注入这些记忆，让 Agent 更稳定地“记得这个用户是谁”。

## 适合解决什么问题

- 想让 Bot 长期记住某个用户的偏好、禁忌、习惯和项目背景
- 想维护双方之间的约定，例如称呼、交流方式、禁令、承诺
- 想记住短期事件，但又不希望它们永远堆积
- 想让 Agent 记住“某类信息在哪个文件、目录、插件、知识库里”
- 想给 AstrBot 做一个比通用长期记忆更轻、更稳定、更可控的“陪伴型记忆层”

## 核心特性

- 分类记忆：`profile` / `agreement` / `event` / `fact` / `knowledge_ref`
- 每回合分类注入：按类别分别检索、分别限流，而不是混成一大坨
- 轮数总结抽取：每隔固定轮数，从最近对话窗口里抽取新记忆
- 纯向量检索：使用 Embedding 检索记忆，不再混用文本兜底搜索
- 向量模型自修复：切换 Embedding 模型后，会按需重建旧向量
- 遗忘机制：`active -> stale -> archived`
- 细粒度遗忘配置：每个分类都能单独设置 stale / archive / TTL 规则
- Agent 主动写入：提供 `add_user_memory` 和 `search_user_memory`
- WebUI 管理：查看、编辑、归档、搜索、注入预览、配置修改
- 注入 Debug：可看到每轮注入候选、分类命中、最终注入内容长度

## 记忆分类说明

### `profile`

适合记录稳定画像：

- 喜好和禁忌
- 设备和环境
- 项目背景
- 说话偏好
- 作息、习惯、长期兴趣

例子：

- 用户不喜欢吃辣
- 用户偏好白天拍照
- 用户主要在 AstrBot 和 Keil 项目之间切换

### `agreement`

适合记录双方约定和明确规则：

- 称呼规则
- 回复风格要求
- 不允许做什么
- 用户承诺过什么

例子：

- 主人承诺之后不会乱买玩具
- 用户要求助手不要主动安排太多任务

### `event`

适合记录短期事件：

- 今天在测试什么
- 最近刚去了哪
- 某个当下状态变化

例子：

- 今天在调试用户记忆插件
- 刚刚重置了当前会话

### `fact`

适合记录和用户直接相关、可复用的小事实：

- 某种偏好事实
- 某个稳定判断
- 某个上下文结论

### `knowledge_ref`

适合记录“知识存放位置”：

- 文件路径
- 知识库入口
- 插件或目录位置

例子：

- 用户位置记录在 `knowledge/user-location.md`
- 摄影偏好记录在某个工作区文档

## 工作流程

### 1. 每轮请求前

插件会：

- 记录当前用户消息
- 判断是否到达总结轮数
- 判断是否到达遗忘扫描时间
- 根据当前用户输入做向量检索
- 按分类拼接注入块并追加到 `system_prompt`

### 2. 每隔若干轮总结

插件会从最近 `max_buffer_turns` 轮对话中抽取新记忆，并写入记忆库。

### 3. 每隔若干小时做遗忘扫描

插件按系统时间检查各分类是否需要：

- 从 `active` 变成 `stale`
- 从 `active/stale` 变成 `archived`

### 4. Agent 主动写入

Agent 可以在对话中主动调用工具，把用户相关短信息当场写入记忆库。

## 安装方式

### 方式一：AstrBot WebUI 上传 ZIP

1. 打开 AstrBot WebUI
2. 进入插件管理
3. 上传本项目打包好的 ZIP
4. 启用插件

注意：

- AstrBot 对 ZIP 目录结构比较敏感，压缩包第一层必须是插件目录本身
- 本仓库已经按这个要求打包

### 方式二：手动放入插件目录

把整个插件目录放到：

```text
AstrBot/data/plugins/user_companion_memory
```

然后重载或重启 AstrBot。

## 目录结构

```text
user_companion_memory/
├─ main.py
├─ metadata.yaml
├─ _conf_schema.json
├─ requirements.txt
├─ README.md
├─ logo.png
├─ engine/
│  ├─ analyzer.py
│  └─ sanitizer.py
├─ storage/
│  └─ repository.py
├─ webui/
│  └─ server.py
└─ static/
   └─ index.html
```

## 配置说明

AstrBot 会读取 `_conf_schema.json`，并在插件配置页中生成可视化配置项。

### `memory_settings`

- `summary_model`
  - 轮数总结时调用的模型 Provider ID
- `summary_every_turns`
  - 每隔多少轮做一次记忆抽取
- `max_buffer_turns`
  - 总结时最多参考最近多少轮对话
- `embedding_enabled`
  - 是否启用向量检索
- `embedding_model`
  - 使用哪个 Embedding Provider
- `embedding_threshold`
  - 向量检索最低相似度阈值
- `pinned_limit_per_category`
  - 每个分类最多允许多少条置顶记忆

### `injection_settings`

- `reference_header`
  - 注入块头部文案
- `max_injection_chars`
  - 总注入字符上限
- `agreement_limit`
  - 每回合最多注入多少条 `agreement`
- `profile_limit`
  - 每回合最多注入多少条 `profile`
- `event_limit`
  - 每回合最多注入多少条 `event`
- `fact_limit`
  - 每回合最多注入多少条 `fact`
- `knowledge_ref_limit`
  - 每回合最多注入多少条 `knowledge_ref`

### `forgetting_settings`

#### 全局

- `run_interval_hours`
  - 每隔多少小时跑一次遗忘扫描
- `protect_pinned`
  - 置顶条目是否免于自动遗忘

#### 分类别规则

每个分类都可以分别配置以下三项：

- `*_stale_after_days`
  - 多少天后转为 `stale`
- `*_archive_after_days`
  - 多少天后转为 `archived`
- `*_ttl_days`
  - 默认 TTL，适合短期类记忆

当前支持：

- `profile_*`
- `agreement_*`
- `event_*`
- `fact_*`
- `knowledge_ref_*`

建议：

- `profile/agreement` 可以设得更保守，甚至关闭自动遗忘
- `event` 建议设置更短
- `fact/knowledge_ref` 可以居中

### `tool_settings`

- `allow_agent_add`
  - 是否允许 Agent 主动写记忆
- `default_tool_priority`
  - Agent 主动写入时的默认重要度

### `prompt_settings`

- `round_summary_prompt`
  - 自定义轮数总结 Prompt

### `debug_settings`

- `debug_level`
  - `off` / `basic` / `verbose` / `trace`
- `log_injection_detail`
  - 是否记录详细注入内容

### `webui_settings`

- `enabled`
- `host`
- `port`
- `access_password`
- `session_timeout`

## WebUI 使用说明

默认会启动一个本地 WebUI，用于查看和维护记忆。

### 功能页

#### 概览

- 记忆总数
- Active / Stale / Archived 数量
- 分类分布
- 最近事件
- 手动执行遗忘扫描

#### 记忆库

- 按分类筛选
- 按状态筛选
- 新增记忆
- 编辑记忆
- 归档记忆
- 查看遗忘倒计时

#### 注入预览

输入一句模拟用户消息，可以查看：

- 这一轮会注入什么
- 哪些分类被命中
- 注入文本长什么样

#### 配置

直接修改运行时配置，并立即生效。

## Agent 工具

### `add_user_memory`

用途：

- 主动把与用户直接相关的短记忆写入记忆库

参数：

- `category`
- `content`
- `priority`
- `pinned`
- `ttl_days`
- `note`

适合场景：

- 用户刚明确说了某个偏好
- 出现了新的约定
- 想把某个近期事件立即记住
- 想把某类知识位置立即入库

### `search_user_memory`

用途：

- 主动搜索当前用户的记忆

说明：

- 当前版本只走向量检索
- 如果未配置 Embedding Provider，则不会使用文本兜底搜索

## 向量检索说明

当前版本的记忆搜索是纯向量检索。

也就是说：

- 不再使用“关键词兜底”
- 不再使用“哈希伪向量”
- 必须有可用的 Embedding Provider

### 为什么这样做

这样可以避免：

- 文本规则和向量排序互相打架
- 不同检索路径结果不一致
- 精确文本命中和语义命中混杂导致行为不稳定

### 注意事项

如果你切换了 `embedding_model`：

- 插件会按需重建旧向量
- 只会重建“不是当前模型生成”的条目
- 不会每次都全量重建

## 遗忘策略说明

每条记忆都有状态：

- `active`
- `stale`
- `archived`

### 触发方式

遗忘不是按聊天轮数触发，而是按系统时间：

- 每隔 `run_interval_hours` 小时检查一次

### 判定基准

主要基于“活动时间”：

- 优先用 `last_used_at`
- 若没有，再退到 `updated_at`
- 再退到 `created_at`

### 额外到期机制

如果某条记忆本身设置了 `ttl_days`，会优先形成自己的到期时间。

### 置顶保护

当 `protect_pinned = true` 时：

- 置顶记忆不会被自动遗忘

## 开源发布建议

建议在公开仓库中一并提供：

- `README.md`
- `metadata.yaml`
- `_conf_schema.json`
- `requirements.txt`
- `logo.png`

建议仓库名和插件名保持一致，例如：

```text
astrbot_user_companion_memory
```

## 兼容性建议

根据 AstrBot 官方插件文档：

- `metadata.yaml` 是插件识别入口
- `_conf_schema.json` 会被自动解析成配置界面
- 插件目录下可添加 `logo.png` 作为 1:1 Logo，推荐 256x256 或更高

## 开发与调试

常用调试观察点：

- 注入候选日志
- 分类命中日志
- 最终注入长度
- 向量检索命中数与阈值
- 向量重建数量
- 遗忘扫描结果

如果需要更详细日志，把：

```text
debug_settings.debug_level = trace
debug_settings.log_injection_detail = true
```

## License

如果你准备开源，建议补充一个明确的开源许可证，例如：

- MIT
- Apache-2.0
- GPL-3.0

当前仓库还未附带许可证文件，你发布前最好补上。

## 发布到 GitHub

最简单的流程就是：

1. 在 GitHub 新建一个公开仓库
2. 把当前项目整个目录上传进去
3. 确认仓库里至少有：
   - `README.md`
   - `metadata.yaml`
   - `_conf_schema.json`
   - `requirements.txt`
   - `logo.png`
4. 补一个 `LICENSE`
5. 如果你要给别人直接安装，再把打包好的 ZIP 一并作为 Release 附件上传
