---
name: vibe-rss
description: "RSS阅读周报生成器。读取sources.txt中的URL和兴趣标签，抓取最新内容，筛选值得阅读的文章，生成中文周报markdown。当用户提到'周报'、'阅读推荐'、'vibe-rss'、'生成周报'时触发。"
---

# vibe-rss 阅读周报生成器

## 工作流程

```
sources.txt --> fetch_urls.py --> Claude筛选+摘要 --> weekly-YYYY-WNN.md
```

## 步骤

### 1. 读取sources.txt

读取 `~/.claude/skills/vibe-rss/sources.txt`，解析:
- `>` 前缀行 → 兴趣标签列表
- `#` 行 → 分类(忽略)
- 裸URL → 待抓取源列表

### 2. 抓取内容

运行抓取脚本:

```bash
python3 ~/.claude/skills/vibe-rss/scripts/fetch_urls.py ~/.claude/skills/vibe-rss/sources.txt
```

脚本输出JSON数组，每个元素包含:
- url: 源URL
- title: 页面标题
- articles: 该源最近的文章列表(标题+链接+摘要片段)
- error: 抓取失败时的错误信息

### 3. 筛选文章

根据兴趣标签和AI判断，从抓取结果中筛选值得推荐的文章。

筛选标准(优先级从高到低):
1. 与兴趣标签的匹配度
2. 文章的深度和原创性(优先技术深度文、经验总结、独到见解)
3. 时效性(优先最近一周发布)
4. 实用性(能学到新东西或改变认知)

不设数量上限。宁缺毋滥，但好文章不遗漏。

### 4. 对筛选出的文章，逐篇用WebFetch读取全文

对每篇被选中的文章，使用WebFetch获取全文内容，用于生成准确的摘要。

### 5. 生成周报

输出文件: `~/note/proj/vibe-rss/weekly-YYYY-WNN.md`

格式:

```markdown
# 阅读周报 YYYY-WNN

> 本周从 N 个源中筛选出 M 篇推荐文章。

## 1. 中文标题
- 原文标题: Original Title Here
- 来源: example.com
- 链接: https://example.com/article
- 预估阅读时间: 15 min
- 难度: 初级/中级/高级
- 一句话摘要: 一行说清楚文章讲了什么
- 推荐理由: 为什么值得读，与哪个兴趣方向相关
- 摘要: 3-5句话展开核心内容

## 2. ...
```

## 约束

- 全中文输出(原文标题保留原文)
- 难度分三级: 初级、中级、高级
- 预估阅读时间基于文章长度估算
- 推荐理由要关联到具体的兴趣标签
- 摘要要提炼核心观点，不是简单翻译开头段落
