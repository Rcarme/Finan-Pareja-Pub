"""Vista del negocio sobre los mismos movimientos del libro contable."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.formatters import parse_period, today_ar
from app.models.ledger import Account, Transaction
from app.services.business_service import get_business_summary
from app.services.category_service import canonical_categories

business_router = APIRouter(tags=["business"])


@business_router.get("/business", response_class=HTMLResponse)
def business_dashboard(
    request: Request, period: str | None = None, db: Session = Depends(get_db)
) -> HTMLResponse:
    from app.web.app import templates

    try:
        month = parse_period(period) if period else today_ar().replace(day=1)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    rows = db.scalars(
        select(Transaction)
        .where(
            Transaction.activity == "business",
            Transaction.period == month,
            Transaction.status == "active",
        )
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    ).all()
    accounts = {account.id: account.name for account in db.scalars(select(Account))}
    categories = {key: category.name for key, category in canonical_categories(db).items()}
    return templates.TemplateResponse(
        request=request,
        name="business/index.html",
        context={
            "title": "Negocio",
            "user": getattr(request.state, "user", None),
            "summary": get_business_summary(db, month),
            "period": month.strftime("%Y-%m"),
            "transactions": rows,
            "accounts": accounts,
            "categories": categories,
        },
    )
