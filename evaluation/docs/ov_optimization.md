# ov测试分析

> 实施优先级与生产约束见 **[ov-improvement-roadmap.md](./ov-improvement-roadmap.md)**（不含 eval-only 削弱 assemble 类改动）。

# 问题1：时间等细节信息回答失败

问题描述：目前链路中assemble链路失效，导致autorecall里面收集到的长期记忆内容无法涵盖会话中的细节，完全丢失了相关细节。

问题原因：两条链路+ov tool

1. assemble负责收集lastet_archived_overview，该内容为最近归档的内容（包含了一些细节），在设计上是希望补充相关的历史对话概要，提供较细节的内容。
2. aotorecall链路中，负责收集相关的长期记忆（user、agent）里面的人物事件、爱好、职业等。该链路设计上希望提供角色的形象和事件信息。提供的信息作为补充promt和问题直接合并。
3. autorecall出的内容→长期记忆，都是patch形式，包括search和replace内容，因此只会整篇内容的更新，不是信息追加，因此会很容易混淆重要信息或丢失细节。
4. ov tool 作为工具链补充，由llm自己决定是否调用，目前测试中还没有出现过主动调用的情况。该链路可以锁定到具体的message内容。

问题示例：locomo_0_qa0（见[https://www.notion.so/ov-3672e13230aa80708da1caa256a84738?source=copy_link#3682e13230aa80bea9b1c10afaf6635c](https://www.notion.so/ov-3672e13230aa80708da1caa256a84738?pvs=21)）

解决办法：

1. assemble链路基本失败
2. 修改提示词让llm在autorecall没有找到有用信息之后，主动调用ov tool检索
3. 支持通过配置 **`memory.custom_templates_dir`** 覆盖/追加自定义 schema，让长期记忆生成的时候写明细节，如：注意区分不同事件，并写明事件的发展和时间线（见 `memory_type_registry.py` 和 `openviking_cli/utils/config/memory_confi）`
4. autorecall出的内容→长期记忆，更换patch模式。

# 问题2：events生成的逻辑

问题描述: events这中长期记忆作为一种针对事件的记忆，被设计初衷是为了能够详细的以时间为节点记录事件。但在目前的测试中，没有任何events类型文件生成。

问题原因：初始化+不会被作为context提醒

1. 本身yaml文件规定的该类记忆的生成条件较为严格（需要信息较多）
2. operation_model=”add_only”，只能被叠加，不能被修改（非patch）
3. 在llm通过message.jsonl和系统提示词提取长期记忆的过程中，需要添加context，该部分context是通过一个假的tool调用过程进行的，也就是prefetch()的add_tool_call_pair_to_messages。
    1. add_tool_call_pair_to_messages
        1. 该函数考虑到不同记忆类型（entity\events\preferences）之类的yaml文件，决定不同的operation权限。
        2. 调用假的search通过向量检索找到top10的长期记忆的uri，作为context输给llm。设置eager_prefetch来控制是否在这一步执行read：false默认只返回search得到的uri，无内容；true返回uri对应的内容。
    2. 对于events这种类型，会导致直接add，不进行这个假的tool调用（search/read）。所以llm不会看到任何events类型的历史记忆，提取全靠llm自己创建。

问题实例：[https://www.notion.so/ov-3672e13230aa80708da1caa256a84738?source=copy_link#3682e13230aa8024b1c4cb1c53438f78](https://www.notion.so/ov-3672e13230aa80708da1caa256a84738?pvs=21)

# 问题3. 图片内容丢失

- 图片caption描述不全→ 数据集问题
- 无法读取url图片内容→ 信息接受能力问题、ov侧ingest问题

locomo形式：

- "img_url”图片链接（数据集不打包原图，只给 URL）
- "blip_caption”BLIP 模型提供图片的内容描述，但是并不保证关键信息全面
- query: 构建数据时用 **icrawler** 搜图用的 **检索关键词**
- "text“中可能会提到相关的关键信息，但不能保证全面

locomo原文

```json
{
          "speaker": "Melanie",
          "img_url": [
            "https://www.speakers.co.uk/microsites/tom-oliver/wp-content/uploads/2014/11/Book-Cover-3D1.jpg"
          ],
          "blip_caption": "a photography of a book cover with a gold coin on it",
          "query": "painted canvas follow your dreams",
          "dia_id": "D7:8",
          "re-download": true,
          "text": "Caroline, so glad you got the support! Your experience really brought you to where you need to be. You're gonna make a huge difference! This book I read last year reminds me to always pursue my dreams, just like you are doing!\ud83c\udf1f"
        }
```

```json
{
        "question_id": "locomo_0_qa23",
        "question": "What books has Melanie read?",
        "golden_answer": "\"Nothing is Impossible\", \"Charlotte's Web\"",
        "generated_answer": "There is no specific information available about what books Melanie has read. The records mention Caroline's reading preferences and favorite books, but Melanie's reading habits or specific books are not detailed.",
        "llm_judgments": {
          "judgment_1": false,
          "judgment_2": false,
          "judgment_3": false
        },
        "category": "1"
      }
```

写入ov的messages.jsonl

{"id": "msg_137f305f26a947a4b04dbf4db625614c", "role": "user", "role_id": "locomo_0", "parts": [{"type": "text", "text": "[Melanie]: [Melanie shared an image: a photography of a book cover with a gold coin on it] Caroline, so glad you got the support! Your experience really brought you to where you need to be. You're gonna make a huge difference! This book I read last year reminds me to always pursue my dreams, just like you are doing!🌟"}], "created_at": "2023-07-12T16:36:30"}

## 框架对比

| **路径** | **`img_url`** | **`blip_caption`** | **写入 OV 的形态** |
| --- | --- | --- | --- |
| **EverOS** (`loaders.py` → `ov_ingest.py`) | 丢弃 | 写入 | 纯 text：`[Melanie]: [Melanie shared an image: …] 正文` |
| **openclaw-openviking-eval** (`eval.py` ingest) | **保留为文本行** | 附在 URL 后 | 纯 text：`Melanie: 正文\nhttps://…: blip` |
| **OpenViking 官方 `import_to_ov.py`** | 丢弃 | 丢弃 | 仅 `[speaker]: 正文` |
1. **openclaw-openviking-eval** 

Caroline: The transgender stories were so inspiring! ...
[https://i.redd.it/l7hozpetnhlb1.jpg:](https://i.redd.it/l7hozpetnhlb1.jpg:) a photo of a dog walking past a wall with a painting of a woman

1. **官方 OpenViking 在 OpenClaw 里**

OpenViking 的 **Message 模型**只有三种 part：`text` / `context` / `tool`，**没有 `image` part**

| **用户输入** | **进入 OV session 的方式** |
| --- | --- |
| 纯文字 | 文本 part → 归档、Phase2 抽 memory |
| 带图消息（多模态 block） | **默认不进 session**（除非 OpenClaw 在 text 里另有描述） |
| 工具调用 / 工具结果 | `tool` part |
| 注入的 `<relevant-memories>` | 写入前会被 strip 掉 |

显式把文件/URL 导入 `viking://resources/...`，服务端用 parser 处理 PDF、图片等（README 写明支持 png/jpg/pdf 等）。

![image.png](image.png)

## 图片的caption和query相差很大

```json
{
          "speaker": "Melanie",
          "img_url": [
            "http://candicealexander.com/cdn/shop/products/IMG_7269_a49d5af8-c76c-4ecd-ae20-48c08cb11dec.jpg"
          ],
          "blip_caption": "a photo of a painting of a sunset over a lake",
          "query": "painting sunrise",
          "dia_id": "D1:12",
          "text": "You'd be a great counselor! Your empathy and understanding will really help the people you work with. By the way, take a look at this."
        }
```

## query不代表和图片的贴近

```json
{
          "speaker": "Melanie",
          "img_url": [
            "https://www.speakers.co.uk/microsites/tom-oliver/wp-content/uploads/2014/11/Book-Cover-3D1.jpg"
          ],
          "blip_caption": "a photography of a book cover with a gold coin on it",
          "query": "painted canvas follow your dreams",
          "dia_id": "D7:8",
          "re-download": true,
          "text": "Caroline, so glad you got the support! Your experience really brought you to where you need to be. You're gonna make a huge difference! This book I read last year reminds me to always pursue my dreams, just like you are doing!\ud83c\udf1f"
        }
```

## 统计

【全数据集 10 个 conversation：**910** 张图，**735** 个 QA 的 evidence 命中含图 `dia_id`。】

### **`locomo_0` evidence 与含图 message 的交集**

- 含图 `dia_id`：**77** 个
- QA 总数：**199**（含 cat 5）
- **可能与图片相关**（`evidence ∩ image_dia_ids` ≠ ∅）：**65** 题

评测子集排除 cat 5 后，其中 **57** 题进入 `eval_results.json`。

### **`locomo_0` eval 正确率（152 题，排除 cat 5）**

| **分组** | **题数** | **J1 / J2 / J3** | **Mean** | **Majority** |
| --- | --- | --- | --- | --- |
| **可能与图片相关** | **57** | 36.84% / 35.09% / 36.84% | **36.26%** | 21/57 (36.8%) |
| 非图片相关 | 95 | 44.21% / 43.16% / 41.05% | 42.81% | 41/95 (43.2%) |
| 全部 | 152 | — | 40.35% | 62/152 |

### **图片相关 QA 的 category 分布（57 题）**

| **Cat** | **类型** | **题数** | **Mean 正确率** |
| --- | --- | --- | --- |
| 1 | 事实性问答 | 20 | 38.33% |
| 2 | 时间近似问答 | 16 | 18.75% |
| 3 | 短语抽取问答 | 2 | 100.00% |
| 4 | 总结性问答 | 19 | 42.11% |

对比：非图片相关 cat 2 仅 17.46%；图片相关 cat 2 略高（18.75%）。图片相关整体比非图片相关低约 **6.5** 个百分点。

# 特点

## **locomo0 结果（152 题）**

| **Cat** | **类型（中/英）** | **题数** | **真实问题特征** | **J1** | **J2** | **J3** | **Mean** | **Majority** |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **1** | 事实性问答 / single-hop | 32 | What=25, How=2, When=1 | 43.75% | 37.50% | 40.62% | **40.62%** | 14/32 (43.8%) |
| **2** | 时间近似问答 / temporal | 37 | **When=34**（典型时间题） | 18.92% | 18.92% | 16.22% | **18.02%** | 7/37 (18.9%) |
| **3** | 短语抽取问答 / token-level | 13 | **Would=13**（推断/短答类） | 84.62% | 84.62% | 84.62% | **84.62%** | 11/13 (84.6%) |
| **4** | 总结性问答 / multi-hop | 70 | What=58, How=7, Who=2…（多事实整合） | 44.29% | 44.29% | 42.86% | **43.81%** | 30/70 (42.9%) |

# 实例1. qa0

- llm prompt提取关键词作为URI
- extract memory 过程中patch直接替换URI内容
- llm需要read更改前内容，但分辨力仍让不强，导致事件混淆

updates": [
{
"uri": "viking://user/locomo_0/memories/entities/people/caroline.md",
"before": "PATCH: {\n  \"blocks\": [\n    {\n      \"search\": \"- **Attended LGBTQ support group on 2023-05-07**\\n- Found the experience powerful and inspiring\\n- Feels accepted and courageous after attending\\n- Planning to continue education and explore career options\\n- Interested in counseling or mental health work to support others with similar issues\\n- Values self-expression and relaxation through creative activities\\n- Currently researching adoption agencies with LGBTQ+ inclusivity\\n- Aspires to become a single parent through adoption\\n- Excited about creating a loving home for children in need\",\n      \"replace\": \"- **Attended LGBTQ support group on 2023-05-07**\\n- Found the experience powerful and inspiring\\n- Feels accepted and courageous after attending\\n- Planning to continue education and explore career options\\n- Interested in counseling or mental health work to support others with similar issues\\n- Values self-expression and relaxation through creative activities\\n- Currently researching adoption agencies with LGBTQ+ inclusivity\\n- Aspires to become a single parent through adoption\\n- Excited about creating a loving home for children in need\\n- Attended LGBTQ+ pride parade last week (2023-06-26)\\n- Felt strong sense of belonging and community\\n- Motivated to use personal story to help others\\n- Planning to attend transgender conference this month (2023-07-15)\\n- Excited to meet community members and learn about advocacy\"\n    }\n  ]\n}",
"after": "PATCH: {\n  \"blocks\": [\n    {\n      \"search\": \"- **Attended LGBTQ support group on 2023-05-07**\\n- Found the experience powerful and inspiring\\n- Feels accepted and courageous after attending\\n- Planning to continue education and explore career options\\n- Interested in counseling or mental health work to support others with similar issues\\n- Values self-expression and relaxation through creative activities\\n- Currently researching adoption agencies with LGBTQ+ inclusivity\\n- Aspires to become a single parent through adoption\\n- Excited about creating a loving home for children in need\",\n      \"replace\": \"- **Attended LGBTQ conference on 2023-07-10**\\n- Found the experience welcoming and affirming\\n- Felt totally accepted and connected with people who've gone through similar journeys\\n- Inspired to fight for trans rights and spread awareness\\n- Planning to continue education and explore career options\\n- Interested in counseling or mental health work to support others with similar issues\\n- Values self-expression and relaxation through creative activities\\n- Currently researching adoption agencies with LGBTQ+ inclusivity\\n- Aspires to become a single parent through adoption\\n- Excited about creating a loving home for children in need\\n- Loves reading books that guide and motivate her, especially 'Becoming Nicole' by Amy Ellis Nutt\"\n    }\n  ]\n}"
}

## **archive_007 的对话来源**

`archive_007/messages.jsonl` 里 Caroline 提到 LGBTQ conference（2023-07-10）、心理咨询职业、读书 *Becoming Nicole* 等。ExtractLoop 读这些消息后，LLM 输出类似：

> 
> 
> 
> {
> "entities": [
> {
> "category": "people",
> "name": "caroline",
> "uri": "viking://user/locomo_0/memories/entities/people/caroline.md",
> "content": { "blocks": [{ "search": "...", "replace": "..." }] }
> },
> {
> "category": "hobbies",
> "name": "reading",
> "content": "# Reading\n- Caroline loves reading..."
> }
> ]
> }
> 
- `caroline` → URI 已存在 → **update**
- `reading` → URI 不存在 → **add**

## 解决方法

支持通过配置 **`memory.custom_templates_dir`** 覆盖/追加自定义 schema（见 `memory_type_registry.py` 和 `openviking_cli/utils/config/memory_config.py`）。
sh qa10 

# 实例s2. qa10

- 对话中存在详细的信息内容，overview中也提取出来了，但assembled链路在qa阶段无用

> - Caroline shared a photo of her close-knit friend group, noting they’ve supported her since moving from her home country **four years** ago — especially after a difficult breakup.
> 
- user memories中
    - 信息很小，导致信息缺失
    - 主体性强，导致细节提取被忽视
    - entity和events的混淆

| **维度** | **entities** | **events** |
| --- | --- | --- |
| **核心问题** | 「这是什么**东西**，有哪些稳定属性？」 | 「**什么时候**发生了什么事？」 |
| **测试句** | 描述的是 THING，不是偏好/事件/问题 | 能否说成「XXX 做了/正在做/计划做…」 |
| **典型内容** | 人、项目、组织、爱好主题（`people/caroline`、`hobbies/painting`） | 带时间的决策、参会、事故、计划（应进 `events/2023/07/12/...`） |
| **时间** | 属性里可带日期 bullet，但**文件按实体名组织** | **目录按年/月/日**，强调绝对日期 |
| **更新方式** | `content` 用 **patch**，可反复改 | **`add_only`**，设计上「历史记录，建完不改」 |

`entities` 

1.  schema ：entity应**链接**到 events，而不是把事件全堆在实体里
2. SessionExtractContextProvider.prefetch：对 `memories/entities/` 做 **search**，结果里常有 `caroline.md`、`painting.md` → 模型自然 **PATCH 这些已有文件**。

`events` 

1. schema: 写**叙事型摘要**，并带上对话索引 `ranges`
2. add_only: 跳过对search/列举，只新增，不需要看之前的记忆列表

# 实例3：locomo_0_qa14

> "<relevant-memories>\nThe following OpenViking memories may be relevant:\n- [] PATCH: {\n  \"blocks\": [\n    {\n      \"search\": \"- Caroline is interested in pursuing this field (as of 2023-06-27)\\n- Motivated by desire to support others with similar issues\\n- Sees this as an extension of her personal growth journey\\n- Currently researching career options in this area\\n- Recently attended an LGBTQ+ counseling workshop on 2023-06-23 that was enlightening\\n- Interested in working specifically with trans people to help them accept themselves and support their mental health\",\n      \"replace\": \"- Caroline is interested in pursuing this field (as of 2023-07-15)\\n- Motivated by desire to support others with similar issues\\n- Sees this as an extension of her personal growth journey\\n- Currently researching career options in this area\\n- Recently attended an LGBTQ+ counseling workshop on 2023-06-23 that was enlightening\\n- Interested in working specifically with trans people to help them accept themselves and support their mental health\\n- Inspired by recent pride parade attendance (2023-06-25) to focus on community-based counseling approaches\"\n    }\n  ]\n}\n- [] # Piano\n- Caroline is learning piano\n- Started recently as creative outlet\n- Sees it as way to express emotions and find relaxation\n- Inspired by Melanie's pottery journey\n- Plans to continue developing skills\n- [] # Reading\n- Caroline loves reading books that guide and motivate her\n- Favorite book: 'Becoming Nicole' by Amy Ellis Nutt (inspiring true story about a trans girl and her family)\n- Books help her discover who she is and remind her to keep going and never give up\n- Finds reading therapeutic and empowering\n- [] PATCH: {\n  \"blocks\": [\n    {\n      \"search\": \"- Melanie's creative outlet and way to express feelings\\n- Painted a sunset over a lake scene (created last year)\\n- Helps with relaxation after long days\\n- Caroline appreciates the blending of colors and artistic expression\\n- Both appreciate painting as a form of self-expression and relaxation\\n- Caroline owns a hand-painted bowl from a friend's 18th birthday gift, which represents art and self-expression\",\n      \"replace\": \"- Melanie's creative outlet and way to express feelings\\n- Painted a sunset with palm tree scene with kids on 2023-07-09\\n- Helps with relaxation after long days\\n- Caroline appreciates the blending of colors and artistic expression\\n- Both appreciate painting as a form of self-expression and relaxation\\n- Caroline owns a hand-painted bowl from a friend's 18th birthday gift, which represents art and self-expression\\n- Family painting sessions have become regular bonding activities\\n- Caroline is preparing for her upcoming LGBTQ art show in August 2023\"\n    }\n  ]\n}\n- [] PATCH: {\n  \"blocks\": [\n    {\n      \"search\": \"# LGBTQ Support Group\\n- Caroline attended on 2023-05-07\\n- Provided powerful and inspiring experiences\\n- Shared transgender stories that were particularly moving\\n- Created feelings of acceptance and courage for Caroline\\n- Helped Caroline feel more comfortable embracing herself\",\n      \"replace\": \"# LGBTQ Pride Event\\n- Caroline attended on 2023-06-25\\n- Provided welcoming and affirming experiences\\n- Connected with people who've gone through similar journeys\\n- Created feelings of acceptance and inspiration for Caroline\\n- Helped Caroline feel more confident in fighting for trans rights and spreading awareness\\n- Reinforced her desire to work in mental health counseling specifically with trans people\"\n    }\n  ]\n}\n</relevant-memories>\n\nWould Caroline still want to pursue counseling as a career if she hadn't received support growing up?”
> 

Based on Caroline's expressed motivations and reflections, it seems that the support she received growing up played a significant role in her personal growth and current path. She has acknowledged the invaluable help from friends, family, and mentors who boosted her through tough times and helped her find her true self. This positive influence inspired her to want to provide similar support to others.\n\nIf Caroline had not received such support growing up, it might have made her journey more difficult and potentially affected her outlook. However, given her strong desire to help others and make a difference, it is likely she would still want to pursue counseling as a career, perhaps even more driven by her own struggles to create a better experience for others who lack support. The support she received has reinforced and encouraged her path, but **her core motivation**—to help others on similar journeys—probably would remain strong regardless.