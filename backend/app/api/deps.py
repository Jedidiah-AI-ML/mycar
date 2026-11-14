"""Shared FastAPI dependencies: current user and per-vehicle authorisation."""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import InvalidToken, decode_access_token
from app.db.session import get_session
from app.models import User

_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _unauthorised(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


async def current_user(
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if creds is None:
        raise _unauthorised()
    try:
        user_id = decode_access_token(creds.credentials)
    except InvalidToken:
        raise _unauthorised("Invalid or expired token") from None
    user = await session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise _unauthorised("Invalid or expired token")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
