import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_operator_token
from usan_api.db.session import get_db
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.elder import ElderCreate, ElderResponse, ElderUpdate

router = APIRouter(prefix="/v1/elders", tags=["elders"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ElderResponse,
    dependencies=[Depends(require_operator_token)],
)
async def create_elder(body: ElderCreate, db: AsyncSession = Depends(get_db)) -> ElderResponse:
    try:
        elder = await elders_repo.create_elder(
            db,
            name=body.name,
            phone_e164=body.phone_e164,
            timezone=body.timezone,
            external_id=body.external_id,
            preferred_voice=body.preferred_voice,
            metadata=body.metadata,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="elder with this phone_e164 or external_id already exists",
        ) from exc
    return ElderResponse.from_model(elder)


@router.put(
    "/{elder_id}",
    response_model=ElderResponse,
    dependencies=[Depends(require_operator_token)],
)
async def update_elder(
    elder_id: uuid.UUID, body: ElderUpdate, db: AsyncSession = Depends(get_db)
) -> ElderResponse:
    try:
        elder = await elders_repo.update_elder(db, elder_id, body.model_dump(exclude_unset=True))
        if elder is None:
            raise HTTPException(status_code=404, detail="elder not found")
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="elder with this phone_e164 or external_id already exists",
        ) from exc
    return ElderResponse.from_model(elder)
