"""
Notes API - Sticky notes CRUD endpoints
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from loguru import logger

from niu_api.notes import create_note, update_note, delete_note, list_notes, get_note

router = APIRouter(prefix="/api", tags=["notes"])


class NoteCreateRequest(BaseModel):
    id: str
    content: str
    createdAt: float  # 前端传 ms 时间戳


class NoteUpdateRequest(BaseModel):
    id: str
    content: str
    updatedAt: float  # 前端传 ms 时间戳


@router.post("/notes")
async def api_create_note(request: NoteCreateRequest):
    """Create a new sticky note"""
    try:
        from datetime import datetime
        created_at = datetime.fromtimestamp(request.createdAt / 1000).isoformat()

        result = await create_note(
            note_id=request.id,
            content=request.content,
            created_at=created_at,
        )

        # KG 写入（异步，不阻塞响应）
        try:
            sync_note_to_kg(request.id, request.content)
        except Exception as e:
            logger.warning(f"[Notes] KG sync failed for note {request.id}: {e}")

        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"[Notes] Create failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/notes")
async def api_list_notes():
    """List all sticky notes"""
    try:
        notes = await list_notes()
        return {"status": "ok", "notes": notes}
    except Exception as e:
        logger.error(f"[Notes] List failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/notes/{note_id}")
async def api_get_note(note_id: str):
    """Get a single note"""
    note = await get_note(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"status": "ok", "note": note}


@router.put("/notes/{note_id}")
async def api_update_note(note_id: str, request: NoteUpdateRequest):
    """Update a sticky note"""
    try:
        result = await update_note(note_id=note_id, content=request.content)

        # KG 写入（更新 Document 内容）
        try:
            sync_note_to_kg(note_id, request.content)
        except Exception as e:
            logger.warning(f"[Notes] KG sync failed for note {note_id}: {e}")

        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"[Notes] Update failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/notes/{note_id}")
async def api_delete_note(note_id: str):
    """Delete a sticky note"""
    try:
        result = await delete_note(note_id=note_id)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"[Notes] Delete failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def sync_note_to_kg(note_id: str, content: str):
    """便利贴写入KG：Document节点 + 实体提取"""
    from niu_kg_server import (
        create_document,
        create_entity,
        link_document_entity,
    )

    uri = f"note://{note_id}"

    # 1. 创建/更新 Document 节点
    create_document(
        uri=uri,
        title=content[:50],
        content=content,
        source="note",
    )

    # 2. 简单实体提取（正则，不调LLM）
    entities = _extract_entities(content)
    for entity_id, name, entity_type in entities:
        create_entity(id=entity_id, name=name, entity_type=entity_type)
        link_document_entity(doc_uri=uri, entity_id=entity_id, confidence=1.0)

    logger.info(f"[Notes] KG sync: {uri}, {len(entities)} entities")


def _extract_entities(text: str) -> list:
    """从便利贴文本中提取实体（简单正则规则）

    返回: [(entity_id, name, entity_type), ...]
    """
    import re
    entities = []

    # 技术关键词（编程语言、框架、工具）
    tech_pattern = r'\b(Python|Java|JavaScript|TypeScript|Go|Golang|Rust|C\+\+|React|Vue|Next\.js|FastAPI|Flask|Django|SQLite|PostgreSQL|Redis|Docker|Kubernetes|Git|Node\.js|Electron|Playwright)\b'
    for match in re.finditer(tech_pattern, text, re.IGNORECASE):
        name = match.group(1)
        # 统一大小写
        name_normalized = name.capitalize() if name.lower() not in {"c++", "next.js", "node.js"} else name
        entity_id = f"technology:{name_normalized}"
        if entity_id not in [e[0] for e in entities]:
            entities.append((entity_id, name_normalized, "technology"))

    # 中文人名（2-3个汉字，常见姓氏开头）
    cn_surnames = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳邴松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴郁胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东殴殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
    cn_name_pattern = f'[{cn_surnames}][\u4e00-\u9fff]{{1,2}}'
    for match in re.finditer(cn_name_pattern, text):
        name = match.group(0)
        if len(name) >= 2:
            entity_id = f"person:{name}"
            if entity_id not in [e[0] for e in entities]:
                entities.append((entity_id, name, "person"))

    return entities
