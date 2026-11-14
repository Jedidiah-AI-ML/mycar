"""Register, log in, refresh, log out."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SessionDep
from app.core import security
from app.models import RefreshToken, User
from app.schemas.auth import LoginIn, RefreshIn, RegisterIn, TokenPair, UserOut
from app.services import audit, consents

router = APIRouter(prefix="/auth", tags=["auth"])

_BAD_LOGIN = HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
_BAD_REFRESH = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")


async def find_user_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(
        select(User).where(func.lower(User.email) == email.lower(), User.deleted_at.is_(None))
    )


async def _issue_tokens(
    session: AsyncSession, user: User, request: Request
) -> tuple[TokenPair, RefreshToken]:
    access, expires_in = security.create_access_token(user.id)
    raw, digest = security.new_refresh_token()
    row = RefreshToken(
        user_id=user.id,
        token_hash=digest,
        expires_at=security.refresh_expiry(),
        user_agent=(request.headers.get("user-agent") or "")[:255] or None,
    )
    session.add(row)
    await session.flush()
    return TokenPair(access_token=access, refresh_token=raw, expires_in=expires_in), row


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, session: SessionDep) -> User:
    missing = consents.required_purposes() - set(body.consents)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Required consent missing: {', '.join(sorted(missing))}",
        )
    if await find_user_by_email(session, body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")

    user = User(
        email=body.email.strip().lower(),
        password_hash=security.hash_password(body.password),
        display_name=body.display_name.strip(),
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:  # lost a race with an identical registration
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An account with this email already exists"
        ) from None

    for purpose in set(body.consents):
        await consents.grant(session, user.id, purpose)
    await audit.record(session, user.id, "account.created", "user", user.id)
    await session.commit()
    return user


@router.post("/login", response_model=TokenPair)
async def login(body: LoginIn, request: Request, session: SessionDep) -> TokenPair:
    user = await find_user_by_email(session, body.email)
    if not security.verify_password(body.password, user.password_hash if user else None):
        raise _BAD_LOGIN
    assert user is not None
    pair, _ = await _issue_tokens(session, user, request)
    await session.commit()
    return pair


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshIn, request: Request, session: SessionDep) -> TokenPair:
    # FOR UPDATE: two simultaneous refreshes with the same token can't both succeed.
    row = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == security.hash_refresh_token(body.refresh_token))
        .with_for_update()
    )
    if row is None:
        raise _BAD_REFRESH

    now = datetime.now(UTC)
    if row.revoked_at is not None:
        # Reuse of a rotated-out token: someone else has a copy. End every session.
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == row.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await audit.record(session, row.user_id, "auth.refresh_reuse", "user", row.user_id)
        await session.commit()
        raise _BAD_REFRESH

    user = await session.get(User, row.user_id)
    if row.expires_at <= now or user is None or user.deleted_at is not None:
        raise _BAD_REFRESH

    pair, new_row = await _issue_tokens(session, user, request)
    row.revoked_at = now
    row.replaced_by = new_row.id
    await session.commit()
    return pair


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshIn, session: SessionDep) -> Response:
    """Revokes the given refresh token. Always 204, so it reveals nothing about the token."""
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == security.hash_refresh_token(body.refresh_token),
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
