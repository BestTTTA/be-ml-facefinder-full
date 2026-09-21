from fastapi import APIRouter

from app.api.v1 import admin, api_keys, auth, faces, me, packages
from app.api.v1.me import usage as _usage_handler  # noqa: F401

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(me.router)
router.include_router(api_keys.router, prefix="/me/api-keys")
router.include_router(api_keys.router, prefix="/api-keys", include_in_schema=False)  # alias
router.include_router(faces.router)
router.include_router(packages.router)
router.include_router(admin.router)

# /api/v1/usage alias → same handler as /api/v1/me/usage
router.add_api_route("/usage", me.usage, methods=["GET"], tags=["me"], include_in_schema=False)
