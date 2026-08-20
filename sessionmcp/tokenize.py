"""中文 bigram 预分词。

FTS5 自带的 trigram tokenizer 对两字中文词无法匹配——它要求查询串至少 3 个字符，
实测「部署」「打标」「指纹」全部返回空。unicode61 则完全不切分连续中文，整段中文
会变成一个 token。

解决办法是在写入前把中文串展开成相邻二字组（bigram），查询时对查询串做同样展开。
两字词因此能精确命中，且仍走 FTS5 索引，保留 BM25 排序，没有 LIKE 全表扫描的
退化路径。

    "部署流程"  ->  "部署 署流 流程"
    查询 "部署"  ->  "部署"          命中
    查询 "流程"  ->  "流程"          命中

单字中文查询（如「卡」）无法用 bigram 表达，由 build_query 单独拆出来交给调用方
做 LIKE 兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# U+4E00–U+9FFF 基本汉字，U+3400–U+4DBF 扩展 A，U+F900–U+FAFF 兼容汉字。
# 日文假名一并纳入——session 里可能混有日文内容。
_CJK = r"㐀-䶿一-鿿豈-﫿぀-ヿ"

# 中文串 / 拉丁数字串（保留下划线、连字符、点，以便 DATABASE_URL、api.example.com 完整成词）
_RUN = re.compile(rf"[{_CJK}]+|[A-Za-z0-9][A-Za-z0-9_.\-]*")
_IS_CJK = re.compile(rf"^[{_CJK}]")


def _expand(run: str) -> list[str]:
    """把一段连续中文展开成相邻二字组；单字原样返回。"""
    if len(run) == 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def tokenize(text: str) -> str:
    """把原文转成供 FTS5 索引的 token 串（空格分隔）。"""
    tokens: list[str] = []
    for run in _RUN.findall(text):
        if _IS_CJK.match(run):
            tokens.extend(_expand(run))
        else:
            tokens.append(run.lower())
    return " ".join(tokens)


@dataclass(frozen=True)
class Query:
    """拆解后的查询。

    match:      可直接喂给 FTS5 MATCH 的表达式，空串表示没有可索引的词
    like_terms: bigram 表达不了的单字中文，需要调用方用 LIKE 兜底
    """

    match: str
    like_terms: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.match and not self.like_terms


def build_query(text: str) -> Query:
    """把用户查询串转成 FTS5 MATCH 表达式。

    多个词之间是 AND 关系。每个词自身展开成的 bigram 序列用 NEAR 无法表达顺序，
    这里用引号短语串联——"部署流程" 展开为 "部署" "署流" "流程"，三个 token 全部
    命中才算命中，等价于子串匹配，不会把「部署」和无关的「流程」凑成一条。
    """
    groups: list[str] = []
    like_terms: list[str] = []

    for raw in text.split():
        parts: list[str] = []
        for run in _RUN.findall(raw):
            if _IS_CJK.match(run):
                if len(run) == 1:
                    like_terms.append(run)
                    continue
                parts.extend(_expand(run))
            else:
                parts.append(run.lower())
        if parts:
            groups.append(" ".join(f'"{p}"' for p in parts))

    return Query(match=" ".join(groups), like_terms=tuple(like_terms))


def snippet_around(text: str, terms: list[str], width: int = 220) -> str:
    """截取包含查询词的片段，用于检索结果展示。

    找不到任何词时退回开头 width 个字符。
    """
    lowered = text.lower()
    hit = -1
    for term in terms:
        pos = lowered.find(term.lower())
        if pos >= 0 and (hit < 0 or pos < hit):
            hit = pos

    if hit < 0:
        return text[:width] + ("…" if len(text) > width else "")

    start = max(0, hit - width // 3)
    end = min(len(text), start + width)
    out = text[start:end]
    if start > 0:
        out = "…" + out
    if end < len(text):
        out = out + "…"
    return out
