"""Trusted billing domain plugin for the shared four-core PostgreSQL experiment.

No payment, tax or revenue-recognition claims. All monetary calculation uses integer
micro-CNY; an invoice total is rounded once, half-up, to cents.
"""
from __future__ import annotations

import calendar
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from collections.abc import Mapping

MICROS_PER_CENT = 10_000


class BillingError(ValueError):
    pass


def timestamp(value):
    if not isinstance(value, str):
        raise BillingError("timestamp must be an ISO-8601 string with timezone")
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise BillingError("invalid timestamp") from e
    if d.tzinfo is None:
        raise BillingError("timezone is required")
    return d.astimezone(timezone.utc)


def positive_int(value, name, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise BillingError(f"{name} must be {'non-negative' if allow_zero else 'positive'} integer")
    return value


def required(payload, key):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise BillingError(f"invalid {key}")
    return value


def period_bounds(period):
    try:
        year, month = map(int, period.split("-"))
        if period != f"{year:04d}-{month:02d}":
            raise ValueError()
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise BillingError("period must be YYYY-MM")
    return start, end


def rounded_cents(micros):
    return (int(micros) + MICROS_PER_CENT // 2) // MICROS_PER_CENT


def output_row(row):
    return {k: int(v) if isinstance(v, Decimal) else v.isoformat() if isinstance(v, datetime) else v for k, v in row.items()}


def rows(ctx, sql, params=()):
    ctx.cur.execute(sql, params)
    result = ctx.cur.fetchall()
    if not result:
        return []
    names = [d.name for d in ctx.cur.description]
    return [dict(r) if isinstance(r, Mapping) else dict(zip(names, r)) for r in result]


def one(ctx, sql, params=()):
    result = rows(ctx, sql, params)
    return result[0] if result else None


def tenant(ctx):
    return ctx.actor["tenant"]


def insert(ctx, table, values):
    # release_id is provided by the runtime, never by client payload.
    release = getattr(ctx, "release_id", None)
    if release is None:
        raise RuntimeError("shared runtime context must expose release_id")
    values = {"tenant_id": tenant(ctx), "id": str(uuid.uuid4()), "release_id": release, **values}
    columns = list(values)
    ctx.cur.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))}) RETURNING id", list(values.values()))
    row = ctx.cur.fetchone()
    return str(row["id"] if isinstance(row, Mapping) else row[0])


def lock_scope(ctx, key):
    # All scope-changing actions acquire the same tenant-specific advisory lock.
    # Hash collisions serialize extra work but cannot violate correctness.
    ctx.cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant(ctx) + ":billing:" + key,))


def publish_rate(ctx, p):
    product = required(p, "product")
    version = required(p, "version")
    effective = timestamp(p["effective_from"])
    price = positive_int(p["price_micros"], "price_micros", allow_zero=True)
    lock_scope(ctx, "product:" + product)
    existing = one(ctx, "SELECT * FROM billing_rate WHERE tenant_id=%s AND product=%s AND version=%s", (tenant(ctx), product, version))
    if existing:
        if existing["effective_from"] != effective or int(existing["price_micros"]) != price:
            raise BillingError("immutable rate version conflicts")
        return {"rate_id": str(existing["id"]), "replayed": True}
    if one(ctx, "SELECT id FROM billing_rate WHERE tenant_id=%s AND product=%s AND effective_from=%s", (tenant(ctx), product, effective)):
        raise BillingError("another rate starts at the same instant")
    # An append cannot retrospectively reprice already accepted usage.
    if one(ctx, "SELECT id FROM billing_usage WHERE tenant_id=%s AND product=%s AND occurred_at >= %s LIMIT 1", (tenant(ctx), product, effective)):
        raise BillingError("rate would change the history of accepted usage")
    rid = insert(ctx, "billing_rate", {"product": product, "version": version, "effective_from": effective, "price_micros": price, "currency": "CNY"})
    return {"rate_id": rid, "version": version, "price_micros": price}


def import_usage(ctx, p):
    account, product, source = (required(p, k) for k in ("account", "product", "source_key"))
    quantity = positive_int(p["quantity"], "quantity")
    occurred = timestamp(p["occurred_at"])
    if "received_at" in p and ctx.actor["role"] not in ("owner", "finance"):
        raise BillingError("only trusted synthetic ingestion may set received_at")
    received = timestamp(p["received_at"]) if "received_at" in p else datetime.now(timezone.utc)
    if received < occurred:
        raise BillingError("received_at precedes occurred_at")
    lock_scope(ctx, "product:" + product)
    lock_scope(ctx, "usage:" + source)
    existing = one(ctx, "SELECT * FROM billing_usage WHERE tenant_id=%s AND source_key=%s", (tenant(ctx), source))
    if existing:
        same = (existing["account"], existing["product"], existing["occurred_at"], int(existing["quantity"])) == (account, product, occurred, quantity)
        if not same:
            raise BillingError("source_key conflicts with an accepted usage fact")
        return {"usage_id": str(existing["id"]), "charge_micros": int(existing["charge_micros"]), "replayed": True}
    rate = one(ctx, "SELECT * FROM billing_rate WHERE tenant_id=%s AND product=%s AND effective_from<=%s ORDER BY effective_from DESC LIMIT 1", (tenant(ctx), product, occurred))
    if rate is None:
        raise BillingError("no rate covers occurred_at")
    period = occurred.strftime("%Y-%m")
    start, end = period_bounds(period)
    charge = quantity * int(rate["price_micros"])
    uid = insert(ctx, "billing_usage", {"account": account, "product": product, "source_key": source, "quantity": quantity, "occurred_at": occurred, "received_at": received, "period": period, "rate_ref": str(rate["id"]), "rate_version": rate["version"], "charge_micros": charge, "late": received >= end})
    return {"usage_id": uid, "rate_version": rate["version"], "charge_micros": charge, "late": received >= end, "period": period}


def invoice_scope(ctx, p, *, must_exist=False):
    account, product, period = (required(p, k) for k in ("account", "product", "period"))
    period_bounds(period)
    lock_scope(ctx, "product:" + product)
    lock_scope(ctx, "invoice:" + account + ":" + product + ":" + period)
    inv = one(ctx, "SELECT * FROM billing_invoice WHERE tenant_id=%s AND account=%s AND product=%s AND period=%s FOR UPDATE", (tenant(ctx), account, product, period))
    if must_exist and inv is None:
        raise BillingError("invoice not found")
    return account, product, period, inv


def recalculate(ctx, p):
    account, product, period, inv = invoice_scope(ctx, p)
    if inv and inv["status"] != "draft":
        raise BillingError("frozen invoice cannot be recalculated; create adjustments")
    usage = rows(ctx, "SELECT * FROM billing_usage WHERE tenant_id=%s AND account=%s AND product=%s AND period=%s ORDER BY occurred_at,id", (tenant(ctx), account, product, period))
    total = sum(int(u["charge_micros"]) for u in usage)
    if inv is None:
        iid = insert(ctx, "billing_invoice", {"account": account, "product": product, "period": period, "status": "draft", "total_micros": total, "total_cents": rounded_cents(total), "revision": 1})
        revision = 1
    else:
        iid = str(inv["id"])
        revision = int(inv["revision"]) + 1
        ctx.cur.execute("DELETE FROM billing_invoice_line WHERE tenant_id=%s AND invoice_ref=%s", (tenant(ctx), iid))
        ctx.cur.execute("UPDATE billing_invoice SET total_micros=%s,total_cents=%s,revision=%s WHERE tenant_id=%s AND id=%s", (total, rounded_cents(total), revision, tenant(ctx), iid))
    for u in usage:
        insert(ctx, "billing_invoice_line", {"invoice_ref": iid, "usage_ref": str(u["id"]), "source_key": u["source_key"], "rate_version": u["rate_version"], "quantity": u["quantity"], "charge_micros": u["charge_micros"]})
    return {"invoice_id": iid, "status": "draft", "total_micros": total, "total_cents": rounded_cents(total), "revision": revision, "lines": len(usage)}


def freeze(ctx, p):
    _, _, _, inv = invoice_scope(ctx, p, must_exist=True)
    if inv["status"] == "frozen":
        return {"invoice_id": str(inv["id"]), "status": "frozen", "replayed": True}
    expected = positive_int(p["expected_revision"], "expected_revision")
    if expected != int(inv["revision"]):
        raise BillingError("invoice revision conflict")
    # Reject freezing stale drafts; caller must explicitly recalculate first.
    missing = one(ctx, "SELECT u.id FROM billing_usage u WHERE u.tenant_id=%s AND u.account=%s AND u.product=%s AND u.period=%s AND NOT EXISTS (SELECT 1 FROM billing_invoice_line l WHERE l.tenant_id=u.tenant_id AND l.invoice_ref=%s AND l.usage_ref=u.id) LIMIT 1", (tenant(ctx), inv["account"], inv["product"], inv["period"], str(inv["id"])))
    if missing:
        raise BillingError("draft has new usage; recalculate before freezing")
    ctx.cur.execute("UPDATE billing_invoice SET status='frozen' WHERE tenant_id=%s AND id=%s", (tenant(ctx), str(inv["id"])))
    return {"invoice_id": str(inv["id"]), "status": "frozen", "total_cents": int(inv["total_cents"])}


def adjust_frozen(ctx, p):
    account, product, period, inv = invoice_scope(ctx, p, must_exist=True)
    if inv["status"] != "frozen":
        raise BillingError("only frozen invoices receive adjustments")
    iid = str(inv["id"])
    existing = rows(ctx, "SELECT * FROM billing_adjustment WHERE tenant_id=%s AND invoice_ref=%s", (tenant(ctx), iid))
    current_micros = int(inv["total_micros"]) + sum(int(a["charge_micros"]) for a in existing)
    billed_cents = int(inv["total_cents"]) + sum(int(a["delta_cents"]) for a in existing)
    late = rows(ctx, "SELECT u.* FROM billing_usage u WHERE u.tenant_id=%s AND u.account=%s AND u.product=%s AND u.period=%s AND NOT EXISTS (SELECT 1 FROM billing_invoice_line l WHERE l.tenant_id=u.tenant_id AND l.invoice_ref=%s AND l.usage_ref=u.id) AND NOT EXISTS (SELECT 1 FROM billing_adjustment a WHERE a.tenant_id=u.tenant_id AND a.invoice_ref=%s AND a.usage_ref=u.id) ORDER BY u.occurred_at,u.id", (tenant(ctx), account, product, period, iid, iid))
    ids = []
    for u in late:
        current_micros += int(u["charge_micros"])
        delta = rounded_cents(current_micros) - billed_cents
        billed_cents += delta
        ids.append(insert(ctx, "billing_adjustment", {"invoice_ref": iid, "usage_ref": str(u["id"]), "source_key": u["source_key"], "charge_micros": u["charge_micros"], "delta_cents": delta, "reason": "usage_received_after_freeze"}))
    return {"invoice_id": iid, "new_adjustments": len(ids), "adjustment_ids": ids, "frozen_total_cents": int(inv["total_cents"]), "adjusted_total_cents": billed_cents}


def statement(ctx, p):
    account, product, period = (required(p, k) for k in ("account", "product", "period"))
    period_bounds(period)
    inv = one(ctx, "SELECT * FROM billing_invoice WHERE tenant_id=%s AND account=%s AND product=%s AND period=%s", (tenant(ctx), account, product, period))
    if inv is None:
        return {"found": False}
    iid = str(inv["id"])
    lines = rows(ctx, "SELECT source_key,rate_version,quantity,charge_micros FROM billing_invoice_line WHERE tenant_id=%s AND invoice_ref=%s ORDER BY source_key", (tenant(ctx), iid))
    adjustments = rows(ctx, "SELECT source_key,charge_micros,delta_cents,reason FROM billing_adjustment WHERE tenant_id=%s AND invoice_ref=%s ORDER BY source_key", (tenant(ctx), iid))
    return {"found": True, "invoice": output_row(inv), "lines": [output_row(l) for l in lines], "adjustments": [output_row(a) for a in adjustments], "adjusted_total_cents": int(inv["total_cents"]) + sum(int(a["delta_cents"]) for a in adjustments), "currency": "CNY", "period_timezone": "UTC", "rounding": "sum integer micro-CNY, round total half-up once to cents; adjustment is cumulative rounded difference", "scope": "usage billing record; no payment, tax or revenue recognition"}


HANDLERS = {"billing.publish_rate": publish_rate, "billing.import_usage": import_usage, "billing.recalculate": recalculate, "billing.freeze": freeze, "billing.adjust_frozen": adjust_frozen}
QUERIES = {"billing.statement": statement}


def definition():
    objects = {
        "billing_rate": {"product":"text","version":"text","effective_from":"timestamptz","price_micros":"numeric","currency":"text"},
        "billing_usage": {"account":"text","product":"text","source_key":"text","quantity":"numeric","occurred_at":"timestamptz","received_at":"timestamptz","period":"text","rate_ref":"text","rate_version":"text","charge_micros":"numeric","late":"boolean"},
        "billing_invoice": {"account":"text","product":"text","period":"text","status":"text","total_micros":"numeric","total_cents":"numeric","revision":"integer"},
        "billing_invoice_line": {"invoice_ref":"text","usage_ref":"text","source_key":"text","rate_version":"text","quantity":"numeric","charge_micros":"numeric"},
        "billing_adjustment": {"invoice_ref":"text","usage_ref":"text","source_key":"text","charge_micros":"numeric","delta_cents":"numeric","reason":"text"},
    }
    actions = {name:{"handler":name,"roles":["owner","finance"]} for name in HANDLERS}
    actions["billing.import_usage"]["roles"].append("billing_ingestor")
    return {"objects": {name:{"columns": cols} for name,cols in objects.items()}, "actions":actions, "queries":{name:{"handler":name,"roles":["owner","finance","billing_reader"]} for name in QUERIES}}


def register(runtime):
    for name, fn in HANDLERS.items():
        runtime.register_handler(name, fn)
    for name, fn in QUERIES.items():
        runtime.register_query(name, fn)


def install(runtime, owner_token):
    register(runtime)
    manifest = runtime.current_definition(runtime.actor(owner_token)["tenant"])
    manifest["release_id"] = "billing-" + uuid.uuid4().hex
    manifest["version"] = int(manifest.get("version", 0)) + 1
    addition = definition()
    for category in ("objects", "actions", "queries"):
        manifest.setdefault(category, {}).update(addition[category])
    runtime.publish(manifest, owner_token)
    return manifest["release_id"]
