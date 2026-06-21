"""Knowledge base endpoints — read/write per-practice clinic information.

The knowledge base lives in the ``practices.knowledge_base`` JSONB column and
captures the information the AI agent needs to behave as *this clinic's* agent:
providers, appointment types, accepted insurances, policies, and emergency
protocol.

  GET  /api/knowledge-base  — return the current knowledge base (or null)
  PUT  /api/knowledge-base  — replace/merge knowledge base for this practice

AUTHZ: GET is readable by any authenticated user; PUT requires MANAGE_SETTINGS
(owner / manager role).

Registration: add the following line to app/main.py (after the other imports and
include_router calls):

    from app.routes import knowledge_base  # in the import block at the top
    app.include_router(knowledge_base.router)  # next to the other include_router() calls
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import MANAGE_SETTINGS, require_permission
from app.dependencies import get_current_practice, get_tenant_db
from app.models.audit_log import AuditLog
from app.models.practice import Practice
from app.models.user import User
from app.schemas.knowledge_base import KnowledgeBase, KnowledgeBaseResponse

router = APIRouter(prefix="/api/knowledge-base", tags=["knowledge_base"])


async def _load(db: AsyncSession, practice_id: uuid.UUID) -> Practice:
    """Re-fetch the practice inside the tenant-bound session for mutation."""
    return (
        await db.execute(select(Practice).where(Practice.id == practice_id))
    ).scalar_one()


@router.get("", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    practice: Practice = Depends(get_current_practice),
) -> KnowledgeBaseResponse:
    """Return the current knowledge base for this practice.

    Returns ``{"knowledge_base": null}`` for a practice that has not yet
    filled in their knowledge base — the frontend should treat null and an
    empty object identically (show the empty form).
    """
    kb_raw = practice.knowledge_base
    if not kb_raw:
        return KnowledgeBaseResponse(knowledge_base=None)
    return KnowledgeBaseResponse(knowledge_base=KnowledgeBase(**kb_raw))


@router.put("", response_model=KnowledgeBaseResponse)
async def put_knowledge_base(
    payload: KnowledgeBase,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> KnowledgeBaseResponse:
    """Save (full replace) the knowledge base for this practice.

    The entire payload is written as-is; ``None`` fields are excluded so the
    stored JSON stays compact (omitting a section does not wipe it — use an
    explicit empty list/object to clear a section). To clear the whole
    knowledge base, send an empty JSON body ``{}``.
    """
    p = await _load(db, practice.id)

    # Serialise: exclude_none keeps the JSON compact; existing keys not in the
    # payload are NOT preserved (this is a full replace, not a deep merge).
    p.knowledge_base = payload.model_dump(exclude_none=True) or None

    db.add(
        AuditLog(
            id=uuid.uuid4(),
            practice_id=p.id,
            user_id=user.id,
            action="knowledge_base_updated",
            resource_type="practice",
            resource_id=p.id,
            audit_metadata={
                "providers_count": len(payload.providers or []),
                "appointment_types_count": len(payload.appointment_types or []),
                "insurances_count": len(payload.insurances or []),
            },
        )
    )

    await db.commit()
    await db.refresh(p)

    kb_raw = p.knowledge_base
    return KnowledgeBaseResponse(
        knowledge_base=KnowledgeBase(**kb_raw) if kb_raw else None
    )
