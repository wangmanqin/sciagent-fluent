"""
agents/rag_advisor.py — W4 RAG 知识库接口 (精简版)
============================================================
工程定位: 给 iter_planner 的 plan 输出附 1-2 段"为什么这么调"的工程引文.

为什么是精简版而不是 Chroma + bge-large-zh:
  本期 demo 知识体量小 (5-10 段工程经验), 用 全量 embedding + 向量库的工程开销
  大于价值. 选**关键词 + frontmatter 元数据**的 lite 检索:
    - 知识库本身是 markdown (knowledge/cfd_snippets.md), 人能读/能改
    - 检索 = 关键词命中 (子串匹配, 不分词不去停用词)
    - 得分 = 命中关键词数 * 段落标签权重
  接口跟向量库版本完全兼容: retrieve(query) → list[Snippet], 升级到 Chroma 时
  只换实现, iter_planner 调用方不动.

什么时候升级到向量库:
  - 知识体量超过 50 段 (人工阅读成本上来)
  - 用户 query 自由度高 (关键词命中率掉到 < 50%)
  - 答辩需要 demo "向量检索" 这个能力本身
  W4 演示路径: "lite 已就位, Chroma 接口 1 行切换" — 这是降低风险的好策略.

接口:
  >>> from agents.rag_advisor import retrieve
  >>> snippets = retrieve("风速 多少合适")
  >>> for s in snippets:
  ...     print(s.topic, s.score, s.preview)

测试入口:
  python -m agents.rag_advisor "风速翻倍温度变化"
"""
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional


_KNOWLEDGE_FILE = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "knowledge", "cfd_snippets.md"
))


@dataclass
class Snippet:
    """单条知识片段."""
    topic: str
    keywords: List[str]
    source: str
    body: str
    score: float = 0.0

    @property
    def preview(self) -> str:
        """前 80 字符预览, 给 iter_planner reasoning 拼一行用."""
        body = self.body.strip().replace("\n", " ")
        return body[:80] + ("…" if len(body) > 80 else "")


def _parse_snippets(path: str) -> List[Snippet]:
    """
    把 markdown 解析成 Snippet 列表.

    格式约定 (cfd_snippets.md):
        ## 标题
        - topic: xxx
        - keywords: a,b,c
        - source: yyy

        正文段落...
        ---  (分隔)
    """
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        text = f.read()

    # 按 --- 切块
    blocks = [b.strip() for b in text.split("\n---") if b.strip()]
    snippets: List[Snippet] = []
    for blk in blocks:
        topic_match = re.search(r"-\s*topic:\s*(.+)", blk)
        keywords_match = re.search(r"-\s*keywords:\s*(.+)", blk)
        source_match = re.search(r"-\s*source:\s*(.+)", blk)
        if not topic_match:
            continue
        topic = topic_match.group(1).strip()
        keywords = []
        if keywords_match:
            keywords = [k.strip() for k in keywords_match.group(1).split(",") if k.strip()]
        source = source_match.group(1).strip() if source_match else ""

        # body = 去掉 frontmatter 后的正文
        body_lines = []
        for line in blk.split("\n"):
            stripped = line.strip()
            if stripped.startswith(("- topic:", "- keywords:", "- source:")):
                continue
            if stripped.startswith("##") or stripped.startswith("# "):
                continue
            body_lines.append(line)
        body = "\n".join(body_lines).strip()

        snippets.append(Snippet(
            topic=topic, keywords=keywords, source=source, body=body,
        ))
    return snippets


# 模块级缓存 — 知识库不会变, 解析一次就行
_SNIPPETS_CACHE: Optional[List[Snippet]] = None


def _get_snippets() -> List[Snippet]:
    global _SNIPPETS_CACHE
    if _SNIPPETS_CACHE is None:
        _SNIPPETS_CACHE = _parse_snippets(_KNOWLEDGE_FILE)
    return _SNIPPETS_CACHE


# ============================================================
# 公开 API: retrieve
# ============================================================

def retrieve(query: str, top_k: int = 2) -> List[Snippet]:
    """
    用关键词命中算简单得分, 返回 top_k 个相关片段.

    Args:
        query: 用户中文/英文 query (iter_planner 的 reasoning 一句话也行)
        top_k: 返回多少条 (默认 2, 报告里多一两段引文够了, 多了反而冗杂)

    Returns:
        list[Snippet], 按 score 降序. score=0 的不返回.
    """
    snippets = _get_snippets()
    if not snippets:
        return []
    q_lower = query.lower()
    scored: List[Snippet] = []
    for s in snippets:
        score = 0.0
        for kw in s.keywords:
            if kw.lower() in q_lower:
                score += 1.0
        # topic 名出现在 query 里也加分 (帮"convection_velocity" 这种英文 topic 命中)
        if s.topic.lower() in q_lower:
            score += 0.5
        if score > 0:
            scored.append(Snippet(
                topic=s.topic, keywords=s.keywords, source=s.source,
                body=s.body, score=score,
            ))
    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[:top_k]


def cite_for_plan(plan_reasoning: str, top_k: int = 1) -> Optional[str]:
    """
    给 iter_planner 的 reasoning 一句话, 返回 1 段相关引文 (markdown 格式).
    集成入口: 让 graph report 节点 / PDF 报告能 append 一段"为什么这么调".

    Returns:
        引文字符串 (含 source 标注). 没命中就返回 None.
    """
    snippets = retrieve(plan_reasoning, top_k=top_k)
    if not snippets:
        return None
    s = snippets[0]
    return (
        f"[工程引文] topic={s.topic}, source={s.source}\n"
        f"  {s.body[:200]}{'…' if len(s.body) > 200 else ''}"
    )


# ============================================================
# CLI / 自测试
# ============================================================

def _selftest() -> int:
    """跑 5 个 query 验关键词检索覆盖率."""
    cases = [
        ("风速调多少温度才会真降", "convection_velocity_temperature"),
        ("CHT 网格策略", "cht_mesh"),
        ("chip_power 物理映射", "chip_power_mapping"),
        ("迭代怎么终止防死循环", "iteration_termination"),
        ("什么风速是工程上典型", "typical_velocity_range"),
    ]
    print("=" * 60)
    print("rag_advisor 自测试")
    print("=" * 60)
    n_pass = 0
    n_fail = 0
    for q, expected_topic in cases:
        results = retrieve(q, top_k=2)
        if not results:
            print(f"[FAIL] '{q}' 检索 0 条")
            n_fail += 1
            continue
        topics = [r.topic for r in results]
        if expected_topic in topics:
            print(f"[OK]   '{q}' → {topics}")
            n_pass += 1
        else:
            print(f"[FAIL] '{q}' 未命中 {expected_topic}, 实得 {topics}")
            n_fail += 1
    print("\n" + "=" * 60)
    print(f"Summary: {n_pass} PASS / {n_fail} FAIL (共 {len(cases)})")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--selftest":
        # CLI 模式: python -m agents.rag_advisor "..."
        for s in retrieve(sys.argv[1]):
            print(f"\n[topic={s.topic}, score={s.score}]")
            print(s.body[:300])
        sys.exit(0)
    sys.exit(_selftest())
