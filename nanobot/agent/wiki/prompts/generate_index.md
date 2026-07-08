你是个人知识库的 Wiki 管理员。请根据现有的 Wiki 页面，生成/更新一个导航索引文件。

## 现有页面列表

{pages_list}

## 索引文件格式

请生成 `index.md` 文件，格式如下：

```markdown
# Wiki 索引

最后更新: {timestamp}

## 主题分类

### AI & 机器学习
- [{page-title}]({slug}) - {one-line-summary}
- ...

### 工具 & 框架
- [{page-title}]({slug}) - {one-line-summary}
- ...

### 方法论 & 实践
- [{page-title}]({slug}) - {one-line-summary}
- ...

## 最近更新

- [{page-title}]({slug}) - {updated_at}

## 标签索引

{tag}: [{page1}]({slug1}), [{page2}]({slug2}), ...
```

## 规则

1. 每个页面一行，包含标题、slug、一句话摘要
2. 按主题分类（可以自定义分类）
3. 最近更新的页面放在前面
4. 标签索引帮助快速查找

开始生成：
