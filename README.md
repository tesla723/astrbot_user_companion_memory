# User Companion Memory

<p align="center">
  <img src="./logo.png" alt="User Companion Memory logo" width="180" />
</p>

一个面向 AstrBot 的“用户分类记忆层”插件。

它的目标不是保存整段大对话，也不是替代通用长期记忆系统，而是专门维护那些会长期影响陪伴体验的短记忆：用户偏好、双方约定、近期事件、可复用事实，以及“知识放在哪”这类索引信息。插件会在每回合请求前按类别注入这些记忆，让 Agent 更稳定地记住“这个用户是谁、和我是什么关系、哪些规则不能忘”。

## 这个插件解决什么问题

- 让 Bot 稳定记住某个用户的偏好、禁忌、习惯和项目背景
- 维护双方之间的约定，例如称呼、说话方式、禁令、承诺
- 记录短期事件，但避免事件类记忆无限堆积
- 记录“某类知识在哪个文件、目录、插件、知识库里”

## 它和大容量记忆插件的区别

大容量记忆插件通常更擅长：

- 保存长聊天历史
- 做大段总结
- 维护更广义的长期记忆库

这个插件更擅长：

- 维护短、小、稳定、可复用的用户相关记忆
- 按类别精细注入，而不是一次塞一大段总结
- 把“约定”和“偏好”这类高价值记忆固定下来
- 控制遗忘节奏，避免记忆越来越脏

一句话说：

- 大容量记忆插件更像“长档案”
- 这个插件更像“用户档案卡 + 约定簿 + 近期状态卡”

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
  - 短事件
  - 小事实
  - 知识索引

比较理想的组合方式是：

- 让大容量记忆插件负责“记很多”
- 让这个插件负责“记得准、注得稳”

## 核心特性

- 分类记忆：`profile` / `agreement` / `event` / `fact` / `knowledge_ref`
- 每回合分类注入：按类别分别检索、分别限流
- 轮数总结抽取：从最近对话窗口中抽取新记忆
- 纯向量检索：只走 Embedding 检索，不混文本兜底
- 向量模型自修复：切换 Embedding 模型后按需重建旧向量
- 遗忘机制：`active -> stale -> archived`
- 每类遗忘规则可单独配置
- Agent 主动写入：提供 `add_user_memory` 和 `search_user_memory`
- WebUI 管理：查看、编辑、归档、搜索、注入预览、配置修改

## 记忆分类

- `profile`
  - 用户稳定画像，例如喜好、禁忌、习惯、设备、项目背景
- `agreement`
  - 双方约定和明确规则，例如称呼、禁令、承诺
- `event`
  - 短期事件，例如今天在做什么、刚刚发生了什么
- `fact`
  - 和用户有关、可复用的小事实
- `knowledge_ref`
  - 知识位置索引，例如某类信息在哪个文件或目录里

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

### 每隔若干小时

插件会按系统时间执行遗忘扫描，把记忆从：

- `active`
- `stale`
- `archived`

逐步推进。

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
- `embedding_enabled`
  - 是否启用向量检索
- `embedding_model`
  - Embedding Provider ID
- `embedding_threshold`
  - 向量检索阈值
- `pinned_limit_per_category`
  - 每个分类最多允许多少条置顶记忆

### `injection_settings`

- `reference_header`
  - 注入块头部文案
- `max_injection_chars`
  - 总注入字符上限
- `agreement_limit`
- `profile_limit`
- `event_limit`
- `fact_limit`
- `knowledge_ref_limit`

这些项分别控制每回合各分类最多注入多少条记忆。

### `forgetting_settings`

全局项：

- `run_interval_hours`
  - 每隔多少小时跑一次遗忘扫描
- `protect_pinned`
  - 置顶条目是否免于自动遗忘

每个分类都可以单独配置：

- `*_stale_after_days`
- `*_archive_after_days`
- `*_ttl_days`

建议：

- `profile/agreement` 配得保守一些，甚至关闭自动遗忘
- `event` 配得更短
- `fact/knowledge_ref` 放中间

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
- 新增、编辑、归档记忆
- 预览某条用户输入会触发哪些注入
- 查看每条记忆距离 `stale` / `archived` 还有多久
- 在线修改插件配置

## Agent 工具

### `add_user_memory`

主动把用户相关短记忆写入记忆库。

适合：

- 用户刚明确说了一个偏好
- 出现了新的约定
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

## 调试建议

如果要看更详细日志，建议打开：

```text
debug_settings.debug_level = trace
debug_settings.log_injection_detail = true
```

重点观察：

- 注入候选日志
- 分类命中日志
- 向量检索命中数和阈值
- 向量重建数量
- 遗忘扫描结果

## 开源建议

建议仓库里至少包含：

- `README.md`
- `metadata.yaml`
- `_conf_schema.json`
- `requirements.txt`
- `logo.png`
- `LICENSE`

如果你要给别人直接安装，建议再发布一个 Release，并附上打包好的 ZIP。
