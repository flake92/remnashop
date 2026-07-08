from fastapi import APIRouter

from src.core.constants import API_V1

from .users import router as users_router

router = APIRouter(prefix=API_V1 + "/admin")
router.include_router(users_router)

__all__ = ["router"]
