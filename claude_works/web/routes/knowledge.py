from fastapi import APIRouter, Depends, HTTPException

from ...knowledge import store as knowledge_store
from ..deps import get_conn, verify_token

router = APIRouter()


@router.get("/api/messages", dependencies=[Depends(verify_token)])
async def get_messages(chat_id: int | None = None, limit: int = 50):
    conn = await get_conn()
    if chat_id:
        async with conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with conn.execute(
            "SELECT * FROM messages ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    await conn.close()
    return [dict(r) for r in rows]


@router.get("/api/knowledge", dependencies=[Depends(verify_token)])
async def get_knowledge(q: str | None = None, type: str | None = None, page: int = 1, page_size: int = 25):
    page_size = max(1, min(page_size, 200))
    page = max(1, page)
    conn = await get_conn()
    if q:
        items = await knowledge_store.search(conn, q, limit=page_size, include_quarantined=True)
        total = len(items)
    else:
        total = await knowledge_store.count(conn, type=type, include_quarantined=True)
        offset = (page - 1) * page_size
        items = await knowledge_store.list_all(conn, type=type, limit=page_size, offset=offset, include_quarantined=True)
    await conn.close()
    pages = max(1, (total + page_size - 1) // page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages}


@router.post("/api/knowledge", dependencies=[Depends(verify_token)])
async def add_knowledge(body: dict):
    title = body.get("title", "").strip()
    content = body.get("content", "").strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="title and content required")
    conn = await get_conn()
    visibility = body.get("visibility", 0)
    if visibility not in (0, 1, 2, 3):
        await conn.close()
        raise HTTPException(status_code=400, detail="Invalid visibility (0-3)")
    entry_id = await knowledge_store.add(
        conn,
        title=title,
        content=content,
        type=body.get("type", "note"),
        tags=body.get("tags"),
        source="admin",
        visibility=visibility,
    )
    await conn.close()
    return {"id": entry_id}


@router.put("/api/knowledge/{entry_id}", dependencies=[Depends(verify_token)])
async def update_knowledge(entry_id: int, body: dict):
    conn = await get_conn()
    raw_tags = body.get("tags")
    tags = raw_tags if isinstance(raw_tags, list) else (
        [t.strip() for t in raw_tags.split(",") if t.strip()] if isinstance(raw_tags, str) else None
    )
    visibility = body.get("visibility")
    if visibility is not None and visibility not in (0, 1, 2, 3):
        await conn.close()
        raise HTTPException(status_code=400, detail="Invalid visibility (0-3)")
    ok = await knowledge_store.update(
        conn,
        entry_id,
        title=body.get("title") or None,
        content=body.get("content") or None,
        type=body.get("type") or None,
        tags=tags,
        visibility=visibility,
    )
    await conn.close()
    if not ok:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"ok": True}


@router.delete("/api/knowledge/{entry_id}", dependencies=[Depends(verify_token)])
async def delete_knowledge(entry_id: int):
    conn = await get_conn()
    ok = await knowledge_store.delete(conn, entry_id)
    await conn.close()
    if not ok:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"ok": True}
