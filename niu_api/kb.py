"""
知识库 API 端点
为 Page-Agent 提供本地知识检索和问答服务
"""

from fastapi import APIRouter, Query
from typing import List, Optional
from pydantic import BaseModel

router = APIRouter(prefix="/kb", tags=["knowledge-base"])


class SearchResult(BaseModel):
    """搜索结果"""
    title: str
    content: str
    relevance: float
    source: Optional[str] = None


class SearchResponse(BaseModel):
    """搜索响应"""
    success: bool
    results: List[SearchResult]
    total: int


class AnswerResponse(BaseModel):
    """问答响应"""
    success: bool
    answer: str
    sources: List[str]
    confidence: float


@router.get("/search", response_model=SearchResponse)
async def search_knowledge(
    q: str = Query(..., description="搜索关键词"),
    limit: int = Query(5, ge=1, le=20, description="返回结果数量")
):
    """
    知识库搜索

    TODO: 对接实际的向量检索系统
    当前返回模拟数据用于测试

    Args:
        q: 搜索关键词
        limit: 返回结果数量

    Returns:
        SearchResponse: 搜索结果列表
    """
    # 临时模拟数据 - 后续对接真实知识库
    mock_results = []

    # MBTI 相关
    if "mbti" in q.lower() or "人格测试" in q or "外向" in q or "内向" in q:
        mock_results = [
            SearchResult(
                title="MBTI人格测试简介",
                content="""MBTI（Myers-Briggs Type Indicator）是一种人格类型指标，基于卡尔·荣格的心理类型理论。

MBTI 将人格分为四个维度：
1. 外向(E) vs 内向(I) - 能量来源
   - 外向型：从外部世界获得能量，喜欢社交、表达、行动
   - 内向型：从内心世界获得能量，喜欢独处、思考、深度

2. 感觉(S) vs 直觉(N) - 信息获取方式
   - 感觉型：关注具体细节、现实、当下
   - 直觉型：关注整体模式、可能性、未来

3. 思考(T) vs 情感(F) - 决策方式
   - 思考型：基于逻辑、客观分析
   - 情感型：基于价值观、人际关系

4. 判断(J) vs 知觉(P) - 生活态度
   - 判断型：喜欢计划、组织、决断
   - 知觉型：喜欢灵活、适应、开放""",
                relevance=0.95,
                source="心理学基础知识"
            ),
            SearchResult(
                title="MBTI外向-内向维度详解",
                content="""外向型 (Extraversion) 特征：
- 从外部世界获得能量
- 喜欢社交互动、表达想法
- 行动导向，先做再想
- 在人群中感到兴奋
- 容易被外界刺激吸引

内向型 (Introversion) 特征：
- 从内心世界获得能量
- 喜欢独处思考、深度交流
- 思考导向，先想再做
- 在独处时恢复精力
- 容易被过多刺激耗尽能量

注意：外向/内向不是绝对的，而是偏好倾向。每个人在不同情境下都可能表现出两种特质，但通常有一种更自然、更舒适的偏好。""",
                relevance=0.92,
                source="人格心理学"
            )
        ]

    # 浏览器自动化相关
    elif "浏览器" in q or "browser" in q.lower():
        mock_results = [
            SearchResult(
                title="浏览器自动化工具对比",
                content="主流浏览器自动化工具：Selenium（成熟稳定）、Playwright（现代跨浏览器）、"
                       "Puppeteer（Chrome专用）、Browser-Use（AI驱动）。",
                relevance=0.92,
                source="技术文档"
            )
        ]

    # 默认返回
    if not mock_results:
        mock_results = [
            SearchResult(
                title=f"关于 '{q}' 的知识",
                content=f"这是关于 '{q}' 的模拟搜索结果。"
                       "实际使用时将对接真实的知识库系统。",
                relevance=0.85,
                source="知识库"
            )
        ]

    return SearchResponse(
        success=True,
        results=mock_results[:limit],
        total=len(mock_results)
    )


@router.get("/answer", response_model=AnswerResponse)
async def answer_question(
    context: str = Query(..., description="上下文信息"),
    question: str = Query(..., description="问题")
):
    """
    基于知识库回答问题

    TODO: 对接实际的RAG系统
    当前返回模拟答案用于测试

    Args:
        context: 上下文信息（用于增强答案质量）
        question: 用户问题

    Returns:
        AnswerResponse: 答案及来源
    """
    # 临时模拟回答 - 结合 context 和 question
    answer = f"根据知识库信息，关于 '{question}' 的回答：\n\n"
    if context:
        answer += f"上下文：{context}\n\n"
    answer += "[模拟答案 - 后续对接真实RAG系统]"

    return AnswerResponse(
        success=True,
        answer=answer,
        sources=["知识库文档1", "知识库文档2"],
        confidence=0.85
    )


@router.get("/health")
async def health_check():
    """
    健康检查

    Returns:
        dict: 服务状态
    """
    return {
        "status": "ok",
        "service": "knowledge-base",
        "version": "1.0.0"
    }
