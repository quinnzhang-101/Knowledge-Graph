# Book-Scribe Phase 3 — 增量生成设计

状态：待拍板（2026-09-10）
前置：Phase 1 `epub_normalizer.py` ✅ / Phase 2 `validate_package.py` ✅

---

## 0. 已拍板的 4 项

| # | 决策 | 结果 |
|---|---|---|
| 1 | entity dedup 强度 | `canonical_name` 完全相等 |
| 2 | 增量写入方式 | 先 staging → merge → 校验 |
| 3 | Markdown Projector | 单独 Phase 4，不与生成器耦合 |
| 4 | 单章 LLM 调用 | 按对象类型分批（multi-pass） |

---

## 1. 五条核心架构决策

### 决策 1 — LLM 不接触 ID，不接触 offset

**LLM 只输出"内容 + 原文摘录字符串"。所有 ID 和 offset 由脚本生成。**

理由：
- LLM 数字符偏移必然错（已验证思路：脚本 find 100% 准）
- LLM 起 slug 必然漂移（你第 4 条已确认）
- 反过来，脚本无法判断"这一段是不是一个事件"——判断归 LLM，确定性归脚本

这条是整个 Phase 3 的地基。

### 决策 2 — local_key 引用机制

LLM 输出的对象之间用**章内临时 key**互相引用，脚本 merge 时翻译成真实 ID。

```jsonc
// LLM 输出（staging）
{ "local_key": "ev1", "claim": "...", "original_text": "兵者，国之大事..." }
{ "local_key": "p1", "canonical_name": "孙武", "aliases": ["孙子"] }
{ "local_key": "e1", "title": "孙子论兵", "participants": ["孙武"], "evidence_refs": ["ev1"] }
```

脚本 merge 后（Package）：
```jsonc
{ "evidence_id": "evidence:sunzi-bingfa:0007", ... }
{ "entity_id": "entity:book:sunzi-bingfa:person:sun-wu", ... }
{ "event_id": "event:book:sunzi-bingfa:sunzi-on-war",
  "participants": ["entity:book:sunzi-bingfa:person:sun-wu"],
  "evidence_refs": [{"id": "evidence:sunzi-bingfa:0007"}] }
```

引用规则：
| 引用目标 | LLM 写什么 | 脚本翻译成 |
|---|---|---|
| evidence | `ev1`（local_key） | `evidence:<slug>:<NNNN>` |
| entity | `孙武`（canonical_name，原文） | `entity:book:<slug>:person:<pyslug>` |
| event | `e1`（local_key） | `event:book:<slug>:<slug>` |
| topic | `庙算`（name，原文） | `topic:<slug>` |

entity / topic 用**原文名字**引用，不用 local_key——因为跨章引用已有 entity 时，LLM 手里只有名字。

### 决策 3 — offset 由脚本回查

算法（`evidence_locator.py`）：

```
1. 精确 find：source_text.find(original_text, ch_start, ch_end)
2. 0 命中 → 清洗后 find（见下）
3. 1 命中 → 写入 char_start/char_end
4. >1 命中 → 报错 ambiguous_evidence_anchor，要求 LLM 加长摘录
5. 仍 0 命中 → 报错 evidence_text_not_found，要求 LLM 逐字重抄
```

**清洗匹配**（应对 LLM 改标点 / 全半角 / 繁简混排）：
- 删除所有空白 + 中文标点 `，。；：！？、""''（）《》〈〉—…·`
- 保留汉字 / 拉丁字母 / 数字
- 建「清洗串 → 原串」位置映射数组，在清洗串上 find，再用映射还原真实 offset

这一层是必要的：LLM 摘录时把 `「」` 改成 `""` 是高频行为，不降级会导致大量误报。

### 决策 4 — dedup = canonical_name 完全相等

merge 时对每个新 entity：

| 情况 | 动作 |
|---|---|
| `canonical_name` 与已有 entity **完全相等** | 合并：`aliases` 取并集、`evidence_refs` 取并集，保留原 `entity_id` |
| `canonical_name` 命中已有 entity 的 `aliases` | **不合并**（按你的选择），报 warning `potential_duplicate_entity` 待人工审 |
| 都不命中 | 新建 |

**已知风险**：完全相等策略下，书中出现两个同名但不同人（如"王大人"）会被错误合并为同一 entity。
缓解：warning 记录 + 后续人工订正；schema 不加唯一约束以外的东西。

### 决策 5 — merge 失败 = Package 零变更

```
staging/ch-XXX/          ← 唯一写入目标
   ↓  merge_package.py
[内存构建新 Package]  ← 不落盘
   ↓  validate_package
通过 → 原子替换 Package + 归档 staging → _merged/ch-XXX-<ts>/
失败 → Package 一动不动，staging 保留，打印错误清单
```

「内存构建 + 校验通过才落盘」保证 Package 任何时刻都是 valid 状态。

---

## 2. 流水线（4 pass + 脚本缝合）

```
                    ┌─────────────────── 脚本 ───────────────────┐
source_normalized ──┤ 切片：章节文本 + 已有 entity 清单（prompt 上下文）
                    └───────────────────┬───────────────────────┘
                                        ↓
  Pass 1  evidence        输入：章节文本
          (LLM)           输出：01_evidence.json
                                        ↓ 脚本：offset 回查 + 分配 evidence_id
  Pass 2  entity + event  输入：章节文本 + pass1 结果摘要
          (LLM)           输出：02_entities.json / 03_events.json
                                        ↓ 脚本：slug 生成 + dedup + 翻译 participants
  Pass 3  rel + causal    输入：pass1+2 结果
        + context         输出：04_relations.json
          (LLM)
                                        ↓
  Pass 4  topic + mirror  输入：全部结果
          (LLM)           输出：05_topics_mirrors.json
                                        ↓
                    ┌─────────────────── 脚本 ───────────────────┐
                    │ merge → validate → 通过则落盘 / 失败则回滚 │
                    └────────────────────────────────────────────┘
```

**分批的价值不只是 token budget**——更在于**局部失败局部重跑**。
pass 3 引错了 event key，只需要重跑 pass 3，不用把整章 40 条 evidence 重新生成一遍。

**超长章切块**：单章 > 8000 字符时，按段落切成 chunk，每个 chunk 独立跑 pass 1+2；pass 3+4 用整章汇总跑（causal / mirror 需要跨 chunk 视野）。

---

## 3. Slug 规则（实测后修正）

实测 pypinyin 结果：

| canonical_name | lazy_pinyin | 按字分隔 slug |
|---|---|---|
| 张居正 | zhang / ju / zheng | `zhang-ju-zheng` |
| 万历皇帝 | wan / li / huang / di | `wan-li-huang-di` |
| 孙武 | sun / wu | `sun-wu` |
| Sun Tzu | （无汉字） | **空串 ← 坑** |

两个需要定的点：

**3a. 分隔方式**
你 spec 示例写的是 `zhang-juzheng`（姓-名连写），但确认的规则是"转拼音、小写、连字符分隔" → 得到 `zhang-ju-zheng`。
二者不符。**建议按字分隔**（`zhang-ju-zheng`）：规则简单无歧义，且你已确认"Global Resolver 不依赖 slug 一致"，好看与否无功能影响。

**3b. 无汉字名字的 fallback**
`Sun Tzu` 过滤汉字后为空。规则改为：
- 汉字部分 → pypinyin 按字分隔
- 非汉字部分 → 小写化，非 `[a-z0-9]` 转 `-`，连续 `-` 合并，首尾 `-` 去掉
- 两部分拼接

| 输入 | slug |
|---|---|
| 孙武 | `sun-wu` |
| Sun Tzu | `sun-tzu` |
| 张居正 (Zhang Juzheng) | `zhang-ju-zheng` |
| 孙子兵法 | `sun-zi-bing-fa` |

**3c. 冲突处理**：slug 已存在且 canonical_name 不同 → 追加 `-2` / `-3`。
**3d. 长度截断**：> 6 段时取前 6 段 + 4 位 hash（`md5(canonical_name)[:4]`）。

---

## 4. 脚本清单

| 脚本 | 职责 | 依赖 |
|---|---|---|
| `bookscribe/slugify.py` | canonical_name → slug（3a-3d 规则） | pypinyin |
| `bookscribe/evidence_locator.py` | original_text → char offset（精确 + 清洗降级 + 歧义检测） | — |
| `bookscribe/init_package.py` | 建 manifest / book.json / build_state.json 骨架 | — |
| `bookscribe/merge_package.py` | staging → Package：ID 分配、slug、dedup、offset 填充、manifest 更新、validate | jsonschema |
| `bookscribe/prompts/pass1..4.md` | 4 个 pass 的 LLM 输出契约（agent 读） | — |
| `bookscribe/test_merge_package.py` | merge 单测 + 失败回滚测试 | — |

**重要**：脚本**不调 LLM API**。
LLM 环节由 agent（我）在对话中执行，产出 staging JSON，脚本只负责 merge。
理由：无需 API key 配置，且把"判断"留在 agent 侧便于你中途干预和调 prompt。

---

## 5. staging 协议

```
books/<slug>/staging/ch-002/
├── 01_evidence.json
├── 02_entities.json
├── 03_events.json
├── 04_relations.json
├── 05_topics_mirrors.json
├── context.json          # 本次 pass 的输入快照（章节文本范围、已有 entity 清单）
└── merge_report.json     # merge 结果：新增/合并/警告/错误
```

merge 成功后整个目录移到 `staging/_merged/ch-002-20260910-013000/`，保留可追溯。

**幂等 / 断点续跑**：`build_state.json`（不污染 manifest，manifest schema 已锁）

```json
{
  "book_slug": "wanli-fifteen-years",
  "schema_version": "1.0",
  "chapters": {
    "ch-002": {"status": "merged", "at": "2026-09-10T01:30:00",
               "evidence": 12, "characters": 5, "events": 4},
    "ch-003": {"status": "failed", "at": "...", "error": "evidence_text_not_found x3"}
  },
  "next_evidence_seq": 25
}
```

`next_evidence_seq` 保证 evidence_id 全局递增不冲突。

---

## 6. 需要你拍板的 5 个问题

| # | 问题 | 选项 | 我的倾向 |
|---|---|---|---|
| **Q1** | slug 分隔方式 | A. 按字 `zhang-ju-zheng` / B. 姓-名连写 `zhang-juzheng`（需姓氏表或固定首字规则） | **A**，规则无歧义 |
| **Q2** | evidence 摘录多处命中 | A. 报错要求 LLM 加长摘录 / B. 取第一处 + warning | **A**，宁可多一轮不要错锚 |
| **Q3** | 单章 evidence 上限 | 建议 40 条/章，单条 ≤ 300 字 | 防 LLM 摘录整章 |
| **Q4** | Topic 悬空问题 | A. v1 就加 `topic_refs` 到 character/event（需改 schema）/ B. v1 不动，Phase 4 Projector 里再处理 | **A**，否则 topic 在 Package 里没有任何连接 |
| **Q5** | merge 失败后 | A. 停下拉人工 / B. 自动重跑该 pass 最多 2 次再停 | **B**，但失败清单要打全 |

---

## 7. 执行顺序（拍板后）

1. `slugify.py` + `evidence_locator.py`（两个纯函数，先写先测）
2. `init_package.py` + `merge_package.py`
3. `test_merge_package.py`（含回滚测试）
4. `prompts/pass1..4.md`（LLM 契约）
5. 拿《孙子兵法》ch-002「计篇」做真实端到端：agent 产 staging → merge → validate
6. 通过后再扩到多章，验证跨章 dedup 与增量 merge

---

## 8. 不在 Phase 3 范围

- Markdown Projector（Phase 4，单独）
- Global Entity Resolution（v2）
- Obsidian / SQLite projection（消费侧，v2+）
- Topic 层级体系（v1.1）
