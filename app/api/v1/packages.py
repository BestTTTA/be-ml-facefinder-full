"""Public package catalogue (active packages only)."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.responses import ok
from app.schemas.common import SuccessResponse
from app.schemas.models import PackageOut
from app.services import package_service

router = APIRouter(prefix="/packages", tags=["packages"])


@router.get("", response_model=SuccessResponse[list[PackageOut]], summary="List available packages",
            description="Public. Returns active packages and their limits.")
async def list_packages(db: AsyncSession = Depends(get_db)):
    return ok([PackageOut.model_validate(p).model_dump() for p in await package_service.list_packages(db)])
