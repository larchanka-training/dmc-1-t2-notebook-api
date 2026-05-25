from fastapi import APIRouter, Depends

from app.modules.auth.dependencies import get_current_user
from app.modules.auth.schemas.user_schemas import CurrentUser

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.get(
    "/me",
    response_model=CurrentUser,
    summary="Get current user",
    description="Returns the current placeholder user for local development.",
)
def get_me(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    return current_user
