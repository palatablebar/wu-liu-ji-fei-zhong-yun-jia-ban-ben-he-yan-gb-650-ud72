"""M8 入口：结算裁决页、裁决查询、重算、人工裁决与账单查询。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import store, web
from app.modules.m8_settlement import service

router = APIRouter(tags=["settlement"])


@router.get("/settlement", response_class=HTMLResponse)
def page_settlement(request: Request):
    context = web.shell(request, active="settlement")
    run_id = context["run_id"]
    context.update({
        "summary": service.summary(run_id) if run_id else {},
        "decisions": service.decisions(run_id) if run_id else [],
        "pending": service.pending(run_id) if run_id else [],
        "bills": service.bills(run_id) if run_id else [],
    })
    return web.page("settlement.html", context)


@router.get("/api/settlement")
def api_settlement(run: str = ""):
    run_id = run or store.latest_run_id()
    return {
        "ok": True,
        "run_id": run_id,
        "summary": service.summary(run_id) if run_id else {},
        "decisions": service.decisions(run_id) if run_id else [],
        "bills": service.bills(run_id) if run_id else [],
    }


@router.post("/api/settlement/run")
def api_settle(run: str = ""):
    run_id = run or store.latest_run_id()
    return {"ok": True, "summary": service.settle(run_id)}


@router.post("/api/settlement/resolve")
def api_resolve(run: str = "", waybill_no: str = Form(...), kind: str = Form(...),
                rule_id: str = Form(...), operator: str = Form(default="settlement-desk")):
    run_id = run or store.latest_run_id()
    return {"ok": True, "result": service.resolve_pending(
        run_id, waybill_no, kind, rule_id, operator)}


@router.get("/api/bills")
def api_bills(run: str = ""):
    run_id = run or store.latest_run_id()
    return {"ok": True, "run_id": run_id, "bills": service.bills(run_id)}
