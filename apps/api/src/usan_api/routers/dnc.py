from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_operator_token
from usan_api.db.session import get_db
from usan_api.repositories import dnc as dnc_repo
from usan_api.schemas.dnc import DNCCreate, DNCResponse

router = APIRouter(prefix="/v1/dnc", tags=["dnc"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=DNCResponse,
    dependencies=[Depends(require_operator_token)],
)
async def add_dnc(body: DNCCreate, db: AsyncSession = Depends(get_db)) -> DNCResponse:
    # Serialize against a concurrent call-enqueue gate for the same number so a
    # number cannot be dialed in the window between the gate check and this add.
    await dnc_repo.lock_phone(db, body.phone_e164)
    entry = await dnc_repo.add_entry(db, body.phone_e164, body.reason)
    await db.commit()
    return DNCResponse.from_model(entry)


@router.delete(
    "/{phone_e164}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_operator_token)],
)
async def remove_dnc(phone_e164: str, db: AsyncSession = Depends(get_db)) -> None:
    removed = await dnc_repo.remove_entry(db, phone_e164)
    if not removed:
        raise HTTPException(status_code=404, detail="not on DNC list")
    await db.commit()
