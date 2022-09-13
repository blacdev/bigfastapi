from datetime import datetime
import os
from uuid import uuid4

import sqlalchemy.orm as orm
from bigfastapi.utils.utils import convert_template_to_html
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import and_

from bigfastapi.db.database import get_db

from .auth_api import is_authenticated
from .core import messages
from .core.helpers import Helpers
from .services import email_services
from .models import organization_models
from .models.organization_models import Organization
from .models.receipt_models import Receipt
from .schemas import pdf_schema, receipt_schemas, users_schemas
from .services import receipts_services
from .utils import paginator

app = APIRouter(tags=["Receipts"])


@app.post(
    "/receipts", status_code=201, response_model=receipt_schemas.SendReceiptResponse
)
async def send_receipt(
    payload: receipt_schemas.attributes,
    background_tasks: BackgroundTasks,
    create_file: bool = False,
    db: orm.Session = Depends(get_db),
    user: users_schemas.User = Depends(is_authenticated),
):

    """
    An endpoint to send receipts.

    ReturnDesc-

        On sucessful request, it returns

        returnBody-
            an object with a key `message` with a string value - `receipt sent` and a key `data` with the created receipt details.
    Raises -

        HTTP_404_NOT_FOUND: object does not exist in db
        HTTP_401_UNAUTHORIZED: Not Authenticated
        HTTP_403_FORBIDDEN: User is not a member of organization
        HTTP_422_UNPROCESSABLE_ENTITY: Request validation error
    """
    try:
        organization = await organization_models.fetchOrganization(
            orgId=payload.organization_id, db=db
        )
        if not organization:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=messages.INVALID_ORGANIZATION,
            )

        is_valid_member = await Helpers.is_organization_member(
            user_id=user.id, organization_id=organization.id, db=db
        )
        if is_valid_member == False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=messages.NOT_ORGANIZATION_MEMBER,
            )
        
        custom_template_dir = payload.custom_template_dir
        create_file = payload.create_file if payload.create_file else create_file
        
        if not custom_template_dir:
            template_dir_path = os.path.abspath(
                os.environ.get("TEMPLATES_DIR", payload.template))
            if not os.path.exists(template_dir_path):
                raise HTTPException(
                    status_code=404, detail=f"Template: {payload.template} does not exist")
            custom_template_dir = "/".join(template_dir_path.split("/")[:-1])
            template = payload.template.split("/")[-1]

        html_string = convert_template_to_html(
                template_dir=custom_template_dir,
                template_file=template,
                template_data=payload.data
            )

        receipt = Receipt(
            id=uuid4().hex,
            sender_email=payload.sender_email,
            recipient=payload.recipients[0],
            subject=payload.subject,
            message=html_string,
            organization_id=payload.organization_id,
        )

        if create_file == True:
            pdf_name = (payload.subject) + str(uuid4().hex) + ".pdf"

            schema = {"htmlString": html_string, "pdfName": pdf_name}
            file = receipts_services.convert_to_pdf(pdf_schema.Format(**schema), db=db)
            receipt.file_id = file.id

            await email_services.send_email(
                title=payload.subject,
                recipients=payload.recipients,
                template=template if template else "mail_receipt.html",
                template_body=payload.data,
                custom_template_dir=custom_template_dir,
                background_tasks=background_tasks,
                db=db,
                file="./filestorage/pdfs/" + pdf_name,
            )
        else:
            await email_services.send_email(
                title=payload.subject,
                recipients=payload.recipients,
                template=template if template else "mail_receipt.html",
                template_body=payload.data,
                custom_template_dir=custom_template_dir,
                background_tasks=background_tasks,
                db=db,
            )

        db.add(receipt)
        db.commit()
        db.refresh(receipt)

        return JSONResponse(
            {"message": "receipt sent", "data": jsonable_encoder(receipt)},
            status_code=201,
        )

    except Exception as ex:
        db.rollback()
        if type(ex) == HTTPException:
            raise ex
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(ex)
        )


@app.get(
    "/receipts", status_code=200, response_model=receipt_schemas.FetchReceiptsResponse
)
async def get_receipts(
    organization_id: str,
    search_value: str = None,
    sorting_key: str = None,
    datetime_constraint: datetime = None,
    reverse_sort: bool = True,
    page: int = 1,
    size: int = 50,
    db: orm.Session = Depends(get_db),
    user: users_schemas.User = Depends(is_authenticated),
):

    """
    An endpoint to fetch all receipts.

    Intro -

        This endpoint retrieves all the receipts in an organization and can be used to synchronize the receipts created offline.

    ParamDesc -

        reqQuery-organization_id: This is the id of the organization sending the receipt.
        reqQuery-search_value(optional): This is a string used to filter the receipts.
        reqQuery-sorting_key(optional): This is a string used to sort the receipts.
        reqQuery-datetime_constraint: This is the key used for synchronization. If provided, the receipts created after the specified time are returned.
        reqQuery-reverse_sort(optional): This is a boolean specifying the order of the returned data.
        reqQuery-page: This is an integer specifying the page to display. The default value is `1`.
        reqQuery-size: This is an integer used to specify the volume of data to be retrieved in numbers.

    returnDesc-

    On sucessful request, it returns
        returnBody- an object with a key `data` containing a paginated response for the list of receipts.
    """
    try:
        organization = await organization_models.fetchOrganization(
            orgId=organization_id, db=db
        )
        if not organization:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=messages.INVALID_ORGANIZATION,
            )

        is_valid_member = await Helpers.is_organization_member(
            user_id=user.id, organization_id=organization.id, db=db
        )
        if is_valid_member == False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=messages.NOT_ORGANIZATION_MEMBER,
            )

        sort_dir = "asc" if reverse_sort == True else "desc"
        page_size = 50 if size < 1 or size > 100 else size
        page_number = 1 if page <= 0 else page
        offset = await paginator.off_set(page=page_number, size=page_size)

        organization = (
            db.query(Organization).filter(Organization.id == organization_id).first()
        )
        if not organization:
            return JSONResponse(
                {"message": "Organization does not exist"},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if search_value:
            receipts, total_items = await receipts_services.search_receipts(
                organization_id=organization_id,
                search_value=search_value,
                offset=offset,
                size=page_size,
                db=db,
            )
        else:
            receipts, total_items = await receipts_services.get_receipts(
                organization_id=organization_id,
                offset=offset,
                size=page_size,
                sort_dir=sort_dir,
                sorting_key=sorting_key,
                db=db,
                datetime_constraint=datetime_constraint,
            )

        pointers = await paginator.page_urls(
            page=page, size=page_size, count=total_items, endpoint=f"/receipts"
        )
        response = {
            "page": page_number,
            "size": page_size,
            "total": total_items,
            "previous_page": pointers["previous"],
            "next_page": pointers["next"],
            "items": receipts,
        }
        return JSONResponse({"data": jsonable_encoder(response)}, status_code=200)
    except Exception as ex:
        if type(ex) == HTTPException:
            raise ex
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(ex)
        )


@app.get(
    "/receipts/{receipt_id}",
    status_code=200,
    response_model=receipt_schemas.SingleReceiptResponse,
)
async def get_receipt(
    organization_id: str,
    receipt_id: str,
    db: orm.Session = Depends(get_db),
    user: users_schemas.User = Depends(is_authenticated),
):
    """
    An endpoint to get a single receipt.
    Intro -
        This endpoint returns a receipt that matches the receipt id specified in the route.

    ParamDesc -

        reqParam-receipt_id: This is the id of the receipt to be fetched.
        reqQuery-organization_id: This is the id of the organization.

    returnDesc-

        On sucessful request, it returns an object with the key `data` containing the receipt details.
    """
    try:
        organization = await organization_models.fetchOrganization(
            orgId=organization_id, db=db
        )
        if not organization:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=messages.INVALID_ORGANIZATION,
            )

        is_valid_member = await Helpers.is_organization_member(
            user_id=user.id, organization_id=organization.id, db=db
        )
        if is_valid_member == False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=messages.NOT_ORGANIZATION_MEMBER,
            )

        receipt = await receipts_services.get_receipt_by_id(
            receipt_id=receipt_id, org_id=organization_id, db=db
        )

        return JSONResponse({"data": jsonable_encoder(receipt)}, status_code=200)
    except Exception as ex:
        if type(ex) == HTTPException:
            raise ex
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(ex)
        )


@app.delete("/receipts/selected/delete", status_code=status.HTTP_200_OK)
async def delete_selected_receipts(
    receipts: receipt_schemas.DeleteSelectedReceipts,
    db: orm.Session = Depends(get_db),
    user: users_schemas.User = Depends(is_authenticated),
):
    """
    intro-This endpoint allows you to delete selected receipts.

    paramDesc-On delete request the url takes no parameter

    returnDesc-On sucessful request, it returns a `message`
    returnBody- "successfully deleted receipts"
    """

    user_status = await Helpers.is_organization_member(
        user_id=user.id, organization_id=receipts.organization_id, db=db
    )
    if user_status == False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not allowed to delete receipts for this business",
        )

    for receipt_id in receipts.receipt_id_list:
        receipt = (
            db.query(Receipt)
            .filter(
                and_(
                    Receipt.id == receipt_id,
                    Receipt.organization_id == receipts.organization_id,
                )
            )
            .first()
        )
        if receipt is not None:
            receipt.is_deleted = True
            db.commit()
            db.refresh(receipt)

    return {"message": "Successfully Deleted Receipts"}


@app.get("/receipts/{receipt_id}/download", status_code=200)
async def download_receipt(
    organization_id: str,
    receipt_id: str,
    db: orm.Session = Depends(get_db),
    user: users_schemas.User = Depends(is_authenticated),
):
    """
    An endpoint to download a receipt.
    Intro -
        This endpoint returns the generated file for the receipt that matches the receipt id specified in the route.

    ParamDesc -

        reqParam-receipt_id: This is the id of the receipt to be fetched.
        reqQuery-organization_id: This is the id of the organization.

    returnDesc-

        On sucessful request, it returns an object with the receipt details.
    """
    try:
        organization = await organization_models.fetchOrganization(
            orgId=organization_id, db=db
        )
        if not organization:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=messages.INVALID_ORGANIZATION,
            )

        is_valid_member = await Helpers.is_organization_member(
            user_id=user.id, organization_id=organization.id, db=db
        )
        if is_valid_member == False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=messages.NOT_ORGANIZATION_MEMBER,
            )

        receipt = await receipts_services.get_receipt_by_id(
            receipt_id=receipt_id, org_id=organization_id, db=db
        )
        file = receipts_services.get_file(
            file_id=receipt.file_id, db=db, bucket_name="pdfs"
        )

        return file
    except Exception as ex:
        if type(ex) == HTTPException:
            raise ex
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(ex)
        )
