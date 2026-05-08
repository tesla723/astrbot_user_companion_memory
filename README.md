# User Companion Memory

<p align="center">
  <img src="./logo.png" alt="User Companion Memory logo" width="140" />
</p>

一个面向 AstrBot 的“用户分类记忆层”插件。

它不追求保存整段长历史，也不替代大容量长期记忆系统，而是专门维护那些会长期影响陪伴体验的短记忆：用户偏好、双方约定、个人黑话、近期事件、可复用事实，以及“知识放在哪”这类索引信息。插件会在每回合请求前按类别注入这些记忆，让 Agent 更稳定地记住“这个用户是谁、和我是什么关系、哪些规则不能忘”。

## 这个插件适合做什么

- 稳定记住用户偏好、禁忌、习惯、设备和项目背景
- 维护用户和助手之间的约定、称呼、规则、禁令、承诺
- 记录短期事件，但避免事件类记忆无限堆积
- 记录用户个人黑话、圈内简称、群友梗和特殊语义映射
- 记录“某类知识在哪个文件、目录、插件、页面里”

## 它和大容量记忆插件的区别

大容量记忆插件通常更擅长：

- 保存长聊天历史
- 做大段总结
- 维护更广义的长期记忆库

这个插件更擅长：

- 维护短、小、稳定、可复用的用户相关记忆
- 按类别精细注入，而不是一次塞一大段总结
- 把“约定”“偏好”“黑话”这类高价值记忆固定下来
- 用时间衰减和整理机制控制遗忘，避免记忆库越来越脏

一句话说：

- 大容量记忆插件更像“长档案”
- 这个插件更像“用户档案卡 + 约定簿 + 黑话词典 + 近期状态卡”

## 是否可以配合其他记忆插件使用

可以，而且很适合配合使用。

推荐分工：

- 其他大容量记忆插件负责：
  - 长对话总结
  - 大块历史沉淀
  - 更广的长期上下文
- User Companion Memory 负责：
  - 用户偏好
  - 双方约定
  - 黑话词典
  - 短事件
  - 小事实
  - 知识索引

## 核心特性

- 分类记忆：`profile` / `agreement` / `slang` / `event` / `fact` / `knowledge_ref`
- 每回合分类注入：按类别分别检索、分别限流
- 轮数总结抽取：从最近对话窗口中抽取新记忆
- 黑话长文本导入：可一次导入多条 `slang`
- 纯向量检索：只走 Embedding 检索，不混文本兜底
- 向量模型自修复：切换 Embedding 模型后按需重建旧向量
- 遗忘整理：先用 LLM 做相似合并/更新，再按时间衰减优先级
- 每个分类单独配置置顶上限、衰减方式、衰减间隔、归档阈值
- Agent 主动写入：提供 `add_user_memory` 和 `search_user_memory`
- WebUI 管理：查看、筛选、编辑、归档、预览注入、批量导入黑话、手动整理和遗忘

## 记忆分类

- `profile`
  - 用户稳定画像，例如喜好、禁忌、习惯、设备、项目背景
- `agreement`
  - 双方约定和明确规则，例如称呼、禁令、承诺
- `slang`
  - 用户个人黑话、圈内简称、群友梗、特殊语义映射、说法习惯
- `event`
  - 短期事件，例如今天在做什么、刚刚发生了什么
- `fact`
  - 和用户有关、可复用的小事实
- `knowledge_ref`
  - 知识位置索引，例如某类信息在哪个文件、目录、插件或页面里

## 工作方式

### 每回合请求前

插件会：

- 记录当前用户消息
- 判断是否到达总结轮数
- 判断是否到达遗忘扫描时间
- 用当前用户输入做向量检索
- 按类别生成注入块并追加到 `system_prompt`

### 每隔若干轮

插件会从最近 `max_buffer_turns` 轮对话中抽取新记忆并写入记忆库。

轮数总结 Prompt 可单独控制：

- 从库里先取多少条记忆做 `{existing}` 候选
- 真正塞进 `{existing}` 的上限
- `{conversation}` 最多保留多少字符

### 每隔若干小时

插件会按系统时间执行“整理 + 遗忘扫描”：

1. 先用 `forgetting_organizer_prompt` 对现有记忆做相似合并、更新和归档建议
2. 再按分类规则执行时间衰减

当前遗忘策略不是旧式 TTL，而是：

- 记忆超过 `*_stale_after_days` 后进入衰减期
- 每隔 `*_decay_interval_hours` 执行一次优先级衰减
- 衰减方式由 `*_decay_mode` 控制：
  - `multiply`
  - `subtract`
- 当优先级低于等于 `*_archive_below_priority` 时自动归档

## 安装

### 方式一：AstrBot WebUI 上传 ZIP

1. 打开 AstrBot WebUI
2. 进入插件管理
3. 上传打包好的 ZIP
4. 启用插件

注意：

- AstrBot 对 ZIP 目录结构比较敏感
- 压缩包第一层必须是插件目录本身

### 方式二：手动放入插件目录

把整个目录放到：

```text
AstrBot/data/plugins/astrbot_plugin_user_companion_memory
```

然后重载或重启 AstrBot。

## 配置

AstrBot 会读取 `_conf_schema.json` 并自动生成配置界面。

### `memory_settings`

- `summary_model`
  - 轮数总结模型
- `summary_every_turns`
  - 每隔多少轮做一次记忆抽取
- `max_buffer_turns`
  - 总结时最多参考最近多少轮对话
- `round_summary_fetch_limit`
  - 轮数总结前最多从记忆库取多少条给 `{existing}` 做候选，`0 = 不设上限`
- `round_summary_existing_limit`
  - 轮数总结 Prompt 中 `{existing}` 最多放多少条，`0 = 不放`
- `round_summary_conversation_chars`
  - 轮数总结 Prompt 中 `{conversation}` 最多保留多少字符，`0 = 不截断`
- `embedding_enabled`
  - 是否启用向量检索
- `embedding_model`
  - Embedding Provider ID
- `embedding_threshold`
  - 向量检索阈值
- `profile_pinned_limit` / `agreement_pinned_limit` / `slang_pinned_limit` / `event_pinned_limit` / `fact_pinned_limit` / `knowledge_ref_pinned_limit`
  - 各分类置顶上限

### `slang_settings`

- `enabled`
  - 是否启用黑话词典
- `auto_extract`
  - 轮数总结时是否自动抽取 `slang`
- `slang_model`
  - 黑话导入使用的模型
- `dedupe_limit`
  - 黑话导入前最多取多少条已有 `slang` 做去重候选
- `import_existing_limit`
  - 黑话导入 Prompt 中 `{existing}` 最多放多少条已有 `slang`
- `import_text_chars`
  - 黑话长文本导入时原始文本最多保留多少字符
- `batch_import_default_priority`
  - 批量导入黑话默认优先级
- `batch_import_default_pinned`
  - 批量导入黑话默认是否置顶

### `injection_settings`

- `reference_header`
  - 注入块头部文案
- `max_injection_chars`
  - 总注入字符上限
- `agreement_limit`
- `profile_limit`
- `slang_limit`
- `event_limit`
- `fact_limit`
- `knowledge_ref_limit`

这些项分别控制每回合各分类最多注入多少条记忆。

### `forgetting_settings`

全局项：

- `run_interval_hours`
  - 每隔多少小时跑一次遗忘扫描
- `organizer_model`
  - 遗忘整理使用的模型
- `organizer_memory_limit`
  - 遗忘整理最多选取多少条记忆，`0 = 不设上限`
- `protect_pinned`
  - 置顶条目是否免于自动衰减和自动归档

每个分类都可以单独配置：

- `*_stale_after_days`
- `*_decay_mode`
- `*_decay_value`
- `*_decay_interval_hours`
- `*_archive_below_priority`

建议：

- `profile/agreement/slang` 配得保守一些
- `event` 配得更短
- `fact/knowledge_ref` 放中间

### `prompt_settings`

- `round_summary_prompt`
  - 轮数总结提取 Prompt，可用 `{existing}` `{conversation}`
- `slang_import_prompt`
  - 黑话长文本导入 Prompt，可用 `{existing}` `{conversation}`
- `forgetting_organizer_prompt`
  - 遗忘整理 Prompt，用于合并、更新和归档建议

### `tool_settings`

- `allow_agent_add`
  - 是否允许 Agent 主动写记忆
- `default_tool_priority`
  - Agent 主动写入默认重要度

### `debug_settings`

- `debug_level`
  - `off` / `basic` / `verbose` / `trace`
- `log_injection_detail`
  - 是否记录详细注入内容

## WebUI

默认会启动本地 WebUI，用来：

- 查看记忆总览
- 多条件筛选、排序、折叠筛选区域
- 新增、编辑、归档记忆
- 预览某条用户输入会触发哪些注入
- 查看每条记忆的衰减解释和预计归档时间
- 手动执行遗忘整理
- 查看整理结果和参与记忆原文
- 批量导入黑话
- 在线修改插件配置

## Agent 工具

### `add_user_memory`

主动把用户相关短记忆写入记忆库。

适合：

- 用户刚明确说了一个偏好
- 出现了新的约定
- 黑话需要立即记住
- 想立即记录某个事件或知识位置

### `search_user_memory`

主动搜索当前用户的记忆。

当前版本说明：

- 只走向量检索
- 不再混用文本兜底搜索

## 向量检索说明

当前版本是纯向量检索。

也就是说：

- 不再使用关键词兜底
- 不再使用伪向量降级
- 必须有可用的 Embedding Provider

如果你切换了 `embedding_model`：

- 插件会按需重建旧向量
- 只会重建不是当前模型生成的条目
- 不会每次都全量重建

嵌入式模型的消耗主要和两件事有关：

- 当前查询文本有多长
- 新增或更新的记忆内容有多长

平时不会因为记忆库条数变多就每轮全量重建向量；只有换模型或补齐旧数据时，才会和库条数明显相关。

## 调试建议

如果要看更详细日志，建议打开：

```text
debug_settings.debug_level = trace
debug_settings.log_injection_detail = true
```

重点观察：

- 当前轮数、待总结轮数、距离下次总结还差几轮
- 注入检索 query
- 注入构建完成和最终使用的 id 列表
- 向量检索命中数和阈值
- 黑话导入结果
- 遗忘整理结果
- 遗忘扫描结果

## 仓库建议包含

- `README.md`
- `metadata.yaml`
- `_conf_schema.json`
- `requirements.txt`
- `logo.png`
- `slang_2026_seed.txt`
- `LICENSE`

如果你要给别人直接安装，建议再发布一个 Release，并附上打包好的 ZIP。
