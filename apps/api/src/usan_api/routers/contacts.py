import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_operator_token
from usan_api.db.session import get_db
from usan_api.repositories import contacts as contacts_repo
from usan_api.schemas.contact import ContactCreate, ContactResponse, ContactUpdate

router = APIRouter(prefix="/v1/contacts", tags=["contacts"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ContactResponse,
    dependencies=[Depends(require_operator_token)],
)
async def create_contact(
    body: ContactCreate, db: AsyncSession = Depends(get_db)
) -> ContactResponse:
    try:
        contact = await contacts_repo.create_contact(
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
            detail="contact with this phone_e164 or external_id already exists",
        ) from exc
    return ContactResponse.from_model(contact)


@router.put(
    "/{contact_id}",
    response_model=ContactResponse,
    dependencies=[Depends(require_operator_token)],
)
async def update_contact(
    contact_id: uuid.UUID, body: ContactUpdate, db: AsyncSession = Depends(get_db)
) -> ContactResponse:
    try:
        contact = await contacts_repo.update_contact(
            db, contact_id, body.model_dump(exclude_unset=True)
        )
        if contact is None:
            raise HTTPException(status_code=404, detail="contact not found")
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="contact with this phone_e164 or external_id already exists",
        ) from exc
    return ContactResponse.from_model(contact)
