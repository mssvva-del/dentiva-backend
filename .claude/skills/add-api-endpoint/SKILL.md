---
name: add-api-endpoint
description: Add a new HTTP endpoint to the Dentovox backend the project's way. Use when adding any /api/* route — creating a router, request/response schema, tenant-scoped DB access, tests, and wiring it into the app.
---

# Add an API endpoint

Follow the existing shape (see `app/routes/reactivation.py` for a minimal example,
`app/routes/patients.py` for a richer one).

1. **Schema** — add Pydantic request/response models in `app/schemas/<area>.py`.
   Validate at the boundary (enums, ranges) so bad input 4xx's here.
2. **Router** — in `app/routes/<area>.py`:
   ```python
   router = APIRouter(prefix="/api/<area>", tags=["<area>"])

   @router.get("/thing", response_model=ThingOut)
   async def get_thing(
       practice: Practice = Depends(get_current_practice),   # auth + tenant
       db: AsyncSession = Depends(get_tenant_db),             # RLS-bound session
   ) -> ThingOut: ...
   ```
   - Mutating routes: gate with `Depends(require_permission(<PERM>))` (RBAC) and
     write an `AuditLog` row for changes.
   - PHI queries: the `get_tenant_db` session is already RLS-bound to the practice —
     never query PHI without it. Look patients up by `phone_hmac`, never by scanning.
3. **Register** — `app.include_router(<area>.router)` in `app/main.py` (add the import
   to the `from app.routes import (...)` block).
4. **Tests** — `tests/test_routes/test_<area>.py`, minimum three:
   - happy path (200 + shape), **auth failure** (401 without dev header),
     **tenant isolation** (practice A can't see practice B's rows).
   Use `seed_practice` + the `client` fixture (RLS-enforced) and `_hdr(org, user)`.
5. **Verify** — `ruff check app/ tests/` clean and `pytest -q` green (db up).

Do NOT put auth logic inside the route body — it belongs in the dependency.
Do NOT log PHI. Keep response shapes consistent with the other routers.
