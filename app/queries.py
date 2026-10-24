"""Deterministic analytics: every number comes from a fixed, parameterized SQL template."""
import calendar
import re
from datetime import date

from .db import AppError, connect, data_quality
from .interpreter import MONTHS, interpret

MAX_QUESTION = 300
# Revenue base: completed orders whose revenue is known. Returned orders and unknown-price rows never count.
VALID = "status = 'Completed' AND revenue IS NOT NULL AND (? IS NULL OR month = ?)"
_TOP = ("SELECT {col} AS name, ROUND(SUM(revenue),2) AS revenue, SUM(quantity) AS units FROM sales WHERE "
        + VALID + " GROUP BY {col} ORDER BY revenue {order}, name")
_TXN_COLS = "transaction_id, order_date, product, region, quantity, unit_price, discount_amount, status"
SQL = {
    "total_revenue": ("SELECT ROUND(SUM(revenue),2) AS revenue, COUNT(*) AS transactions, SUM(quantity) AS units "
                      "FROM sales WHERE " + VALID),
    "top_product": _TOP.format(col="product", order="DESC"),
    "top_product_asc": _TOP.format(col="product", order="ASC"),
    "top_region": _TOP.format(col="region", order="DESC"),
    "top_region_asc": _TOP.format(col="region", order="ASC"),
    "monthly_trend": ("SELECT month, ROUND(SUM(revenue),2) AS revenue, COUNT(*) AS transactions FROM sales "
                      "WHERE status = 'Completed' AND revenue IS NOT NULL GROUP BY month ORDER BY month"),
    "compare_months": ("SELECT month, ROUND(SUM(revenue),2) AS revenue, COUNT(*) AS transactions FROM sales "
                       "WHERE status = 'Completed' AND revenue IS NOT NULL AND month IN (?, ?) "
                       "GROUP BY month ORDER BY month"),
    "order_count": ("SELECT COUNT(*), SUM(status = 'Completed'), SUM(status = 'Returned'), "
                    "SUM(status NOT IN ('Completed', 'Returned')), SUM(status = 'Completed' AND revenue IS NOT NULL), "
                    "SUM(revenue_status = 'zero_quantity'), SUM(revenue_status = 'unknown_price') "
                    "FROM sales WHERE (? IS NULL OR month = ?)"),
    "returned_orders": ("SELECT transaction_id, order_date, product, region, quantity, unit_price, "
                        "discount_amount, revenue AS sales_value FROM sales "
                        "WHERE status = 'Returned' AND (? IS NULL OR month = ?) ORDER BY order_date, transaction_id"),
    "data_issues": ("SELECT transaction_id, order_date, product, quantity, unit_price, status, revenue, "
                    "revenue_status FROM sales WHERE revenue_status != 'valid' ORDER BY transaction_id"),
    "transaction_lookup": ("SELECT " + _TXN_COLS + ", revenue, revenue_status FROM sales "
                           "WHERE upper(transaction_id) = upper(?)"),
}
ORDINAL = {2: "second-", 3: "third-", 4: "fourth-", 5: "fifth-"}


def inr(n):
    """Indian digit grouping: 1439200 -> ₹14,39,200."""
    s = f"{abs(round(n)):d}"
    if len(s) > 3:
        s = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", s[:-3]) + "," + s[-3:]
    return ("-" if n < 0 else "") + "₹" + s


def fmt_month(m):
    return f"{calendar.month_abbr[int(m[5:])]} {m[:4]}"


def _q(conn, key, args=()):
    cur = conn.execute(SQL[key], args)
    return [c[0] for c in cur.description], [list(r) for r in cur.fetchall()]


def _month(v, months):
    """Normalise 'March', 'mar 2026' or '2026-03' to 'YYYY-MM' and make sure the data covers it."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", s):
        y, mo = int(m[1]), int(m[2])
    elif (m := re.fullmatch(r"([a-z]+)\.?(?:\s+(\d{4}))?", s)) and m[1] in MONTHS:
        y, mo = int(m[2] or max(months)[:4]), MONTHS[m[1]]
    else:
        raise AppError(400, f"Could not understand the month '{v}'. Try 'January' or '2026-01'.")
    key = f"{y}-{mo:02d}"
    if key not in months:
        raise AppError(400, f"No sales data for '{v}'. Available months: {', '.join(map(fmt_month, months))}.")
    return key


def _partial(conn, months):
    """Warn when a month in the answer is incomplete (the data ends mid-month)."""
    last_value = conn.execute("SELECT MAX(order_date) FROM sales").fetchone()[0]
    if not last_value:
        return []
    last = date.fromisoformat(last_value)
    key = last.strftime("%Y-%m")
    if key in months and last.day < calendar.monthrange(last.year, last.month)[1]:
        return [f"{fmt_month(key)} has data only through {last:%d %b}, so it is a partial month."]
    return []


def total_revenue(conn, p, months):
    m = _month(p.get("month"), months)
    cols, rows = _q(conn, "total_revenue", (m, m))
    rev, n, units = rows[0]
    if not n:
        raise AppError(400, f"No revenue can be calculated for {fmt_month(m)}: it has no completed transactions "
                            f"with a known price.")
    scope = f"in {fmt_month(m)}" if m else "(all time)"
    return dict(answer=f"Total revenue {scope} is {inr(rev)} from {n} completed transactions ({units} units).",
                columns=cols, rows=rows, chart=None, notes=_partial(conn, [m] if m else []), sql=SQL["total_revenue"])


def _top(intent, noun):
    """Ranking by revenue: highest or lowest, Nth position, or a top/bottom-N list."""
    def run(conn, p, months):
        m = _month(p.get("month"), months)
        low = p.get("direction") == "lowest"
        key = intent + ("_asc" if low else "")
        cols, rows = _q(conn, key, (m, m))
        if not rows:
            raise AppError(400, f"No revenue can be calculated for {noun}s {('in ' + fmt_month(m)) if m else ''}.")
        total = sum(r[1] for r in rows) or 1
        levels = list(dict.fromkeys(r[1] for r in rows))              # distinct revenues in ranking order
        position = {v: i + 1 for i, v in enumerate(levels)}           # equal revenue = equal rank (ties visible)
        for r in rows:
            r += [round(r[1] / total * 100, 1), position[r[1]]]
        scope = f"in {fmt_month(m)}" if m else "overall"
        label = fmt_month(m) if m else "total"
        word = "lowest" if low else "highest"
        if p.get("limit"):
            picks = rows[:p["limit"]]
            ans = (f"{'Bottom' if low else 'Top'} {len(picks)} {noun}s by revenue {scope}: "
                   + "; ".join(f"{r[0]} ({inr(r[1])})" for r in picks) + ".")
        else:
            rank = p.get("rank", 1)
            if rank > len(levels):
                raise AppError(400, f"Only {len(levels)} {noun}s have different revenue levels {scope}, "
                                    f"so there is no rank {rank}.")
            hit = [r for r in rows if r[4] == rank]
            who = " and ".join(r[0] for r in hit) + (" (tied)" if len(hit) > 1 else "")
            ans = (f"{who} generated the {ORDINAL.get(rank, '')}{word} revenue {scope}: {inr(hit[0][1])} "
                   f"({hit[0][3]}% of {label} revenue).")
        return dict(answer=ans, columns=cols + ["share_pct", "rank"], rows=rows,
                    notes=_partial(conn, [m] if m else []), sql=SQL[key],
                    chart={"type": "bar", "labels": [r[0] for r in rows], "values": [r[1] for r in rows],
                           "label": "Revenue (₹)"})
    return run


def monthly_trend(conn, p, months):
    cols, rows = _q(conn, "monthly_trend")
    if not rows:
        raise AppError(400, "No monthly revenue data is available.")
    prev = None
    for r in rows:
        r.append(round((r[1] - prev) / prev * 100, 1) if prev else None)     # None instead of /0
        prev = r[1]
    peak, low = max(rows, key=lambda r: r[1]), min(rows, key=lambda r: r[1])
    return dict(answer=f"Monthly revenue went from {inr(rows[0][1])} in {fmt_month(rows[0][0])} to "
                       f"{inr(rows[-1][1])} in {fmt_month(rows[-1][0])}; the highest month was {fmt_month(peak[0])} "
                       f"({inr(peak[1])}) and the lowest was {fmt_month(low[0])} ({inr(low[1])}).",
                columns=cols + ["mom_change_pct"], rows=rows, notes=_partial(conn, months), sql=SQL["monthly_trend"],
                chart={"type": "line", "labels": [fmt_month(r[0]) for r in rows], "values": [r[1] for r in rows],
                       "label": "Revenue (₹)"})


def compare_months(conn, p, months):
    a, b = _month(p["month_a"], months), _month(p["month_b"], months)
    if a == b:
        raise AppError(400, "Please choose two different months to compare.")
    rev = {r[0]: r for r in _q(conn, "compare_months", (a, b))[1]}
    if a not in rev or b not in rev:
        raise AppError(400, "Could not find revenue for both months.")
    (_, ra, ta), (_, rb, tb) = rev[a], rev[b]
    # Change is measured from the first-named month (base) to the second.
    diff, fa, fb = rb - ra, fmt_month(a), fmt_month(b)
    pct = round(diff / ra * 100, 2) if ra else None                           # undefined if base is 0
    wording = p.get("comparison_wording", "normal")
    pct_text = f"{abs(pct):.2f}%" if pct is not None else "percentage undefined"
    if diff == 0:
        if wording == "same_question":
            ans = f"Yes, revenue was the same in {fa} and {fb}: {inr(ra)} in both months."
        elif wording in {"higher_question", "lower_question", "increase_question", "decrease_question"}:
            ans = f"No, revenue was unchanged between {fa} and {fb}: {inr(ra)} in both months."
        else:
            ans = f"Revenue was unchanged between {fa} and {fb}: {inr(ra)}."
    elif wording == "same_question":
        ans = (f"No, revenue was not the same between {fa} and {fb}. It {'increased' if diff > 0 else 'decreased'} "
               f"by {inr(abs(diff))} ({pct_text}), from {inr(ra)} in {fa} to {inr(rb)} in {fb}.")
    elif wording in {"higher_question", "lower_question"}:
        (hm, hv), (lm, lv) = sorted([(fa, ra), (fb, rb)], key=lambda x: -x[1])
        ans = (f"{hm} revenue ({inr(hv)}) was higher than {lm} ({inr(lv)}), a gap of {inr(abs(diff))}."
               if wording == "higher_question"
               else f"{lm} revenue ({inr(lv)}) was lower than {hm} ({inr(hv)}), a gap of {inr(abs(diff))}.")
    elif wording in {"increase_question", "decrease_question"}:
        want_up = wording == "increase_question"
        ok = (diff > 0) == want_up
        ans = (f"{'Yes' if ok else 'No'}, revenue {'did' if ok else 'did not'} "
               f"{'increase' if want_up else 'decrease'} from {fa} to {fb}. It "
               f"{'increased' if diff > 0 else 'decreased'} by {inr(abs(diff))} ({pct_text}), "
               f"from {inr(ra)} to {inr(rb)}.")
    else:
        pct_suffix = (f" ({abs(pct):.2f}%)" if pct is not None
                      else " (percentage undefined: starting revenue is zero)")
        ans = (f"Revenue {'increased' if diff > 0 else 'decreased'} by {inr(abs(diff))}{pct_suffix} "
               f"from {fa} ({inr(ra)}) to {fb} ({inr(rb)}).")
    chart_months = sorted([(a, ra), (b, rb)])                                 # chart is always chronological
    return dict(answer=ans, columns=["month", "revenue", "transactions", "change_vs_first", "change_pct"],
                rows=[[a, ra, ta, None, None], [b, rb, tb, diff, pct]], notes=_partial(conn, [a, b]),
                sql=SQL["compare_months"],
                chart={"type": "bar", "labels": [fmt_month(k) for k, _ in chart_months],
                       "values": [v for _, v in chart_months], "label": "Revenue (₹)"})


def order_count(conn, p, months):
    m = _month(p.get("month"), months)
    total, completed, returned, other, counted, zero, unknown = (v or 0 for v in conn.execute(SQL["order_count"], (m, m)).fetchone())
    rows = [["All transactions recorded", total], ["Completed", completed], ["Returned", returned]]
    if other:
        rows.append(["Other status", other])
    rows += [["Completed, revenue counted", counted], ["Zero quantity (revenue ₹0)", zero],
             ["Missing unit price (revenue unknown)", unknown]]
    scope = f"in {fmt_month(m)}" if m else "in the data"
    ans = (f"There are {total} transactions {scope}: {completed} completed and {returned} returned"
           f"{f' and {other} with another status' if other else ''}. Revenue is counted for {counted} of the "
           f"completed ones. Data issues among all transactions: {zero} with zero quantity (₹0 revenue) and "
           f"{unknown} with a missing unit price (revenue unknown).")
    return dict(answer=ans, columns=["measure", "transactions"], rows=rows, chart=None,
                notes=_partial(conn, [m] if m else []), sql=SQL["order_count"])


def returned_orders(conn, p, months):
    m = _month(p.get("month"), months)
    cols, rows = _q(conn, "returned_orders", (m, m))
    scope = f"in {fmt_month(m)}" if m else "in the data"
    caution = ("This is the recorded sales value of orders that were reversed, which the revenue figures exclude. It is "
               "not a measured financial loss: the data has no refund amounts, restocking, shipping or cost "
               "information, and the goods may have been resold.")
    if not rows:
        return dict(answer=f"There are no returned orders {scope}.", columns=cols, rows=rows, chart=None,
                    notes=[], sql=SQL["returned_orders"])
    known = [r[7] for r in rows if r[7] is not None]
    unknown = len(rows) - len(known)
    reported = _q(conn, "total_revenue", (m, m))[1][0][0]
    share = f" That equals {round(sum(known) / reported * 100, 2)}% of the revenue reported {scope}." if known and reported else ""
    ids = ", ".join(r[0] for r in rows)
    ans = (f"{len(rows)} returned order(s) {scope} ({ids}) with a recorded sales value of {inr(sum(known))}"
           f"{f', plus {unknown} with an unknown value (missing price)' if unknown else ''}.{share} {caution}")
    return dict(answer=ans, columns=cols, rows=rows, chart=None, notes=_partial(conn, [m] if m else []),
                sql=SQL["returned_orders"])


def data_issues(conn, p, months):
    cols, rows = _q(conn, "data_issues")
    unknown = [r[0] for r in rows if r[7] == "unknown_price"]
    zero = [r[0] for r in rows if r[7] == "zero_quantity"]
    dq = data_quality(conn)
    parts = []
    if unknown:
        parts.append(f"{len(unknown)} transaction(s) ({', '.join(unknown)}) have no valid unit price, so their revenue "
                     f"cannot be calculated. It is kept as unknown and left out of every revenue total instead of "
                     f"being guessed.")
    if zero:
        parts.append(f"{len(zero)} transaction(s) ({', '.join(zero)}) have quantity 0, so their revenue is ₹0.")
    if not parts:
        parts.append("No transactions have a missing unit price or zero quantity.")
    if dq["by_status"].get("Returned"):
        parts.append(f"{dq['by_status']['Returned']} returned order(s) are also excluded from revenue.")
    rej = len(dq["rejected"])
    parts.append(f"{rej} row(s) were rejected as unreadable." if rej else "No rows were rejected as unreadable.")
    return dict(answer=" ".join(parts), columns=cols, rows=rows, chart=None, notes=[], sql=SQL["data_issues"])


def missing_price(conn, p, months):
    """Show only transactions whose price is missing/invalid; do not mix in zero-quantity rows."""
    sql = ("SELECT transaction_id, order_date, product, quantity, unit_price, status, revenue, revenue_status "
           "FROM sales WHERE revenue_status = 'unknown_price' ORDER BY transaction_id")
    cur = conn.execute(sql)
    cols, rows = [c[0] for c in cur.description], [list(r) for r in cur.fetchall()]
    if rows:
        ids = ", ".join(r[0] for r in rows)
        ans = (f"{len(rows)} transaction(s) ({ids}) have a missing or invalid unit price. Their revenue is unknown, "
               "cannot be calculated, and is excluded from revenue totals rather than treated as ₹0 or guessed.")
    else:
        ans = "No transactions have a missing or invalid unit price."
    return dict(answer=ans, columns=cols, rows=rows, chart=None, notes=[], sql=sql)


def transaction_lookup(conn, p, months):
    tid = p["transaction_id"]
    cols, rows = _q(conn, "transaction_lookup", (tid,))
    if not rows:
        example = conn.execute("SELECT MIN(transaction_id) FROM sales").fetchone()[0]
        raise AppError(400, f"No transaction '{tid}' was found. IDs look like {example}.")
    t = dict(zip(cols, rows[0]))
    head = f"{t['transaction_id']} ({t['product']}, {t['region']}, quantity {t['quantity']}, {t['status']})"
    if t["revenue_status"] == "unknown_price":
        ans = (f"{head} has no valid unit price, so its revenue cannot be calculated. It is recorded as unknown and "
               f"excluded from all revenue totals rather than guessed.")
    elif t["revenue_status"] == "zero_quantity":
        ans = f"{head} has quantity 0, so its revenue is ₹0."
    elif t["status"] != "Completed":
        ans = (f"{head} would be worth {inr(t['revenue'])} (quantity x unit price - discount), but its status is "
               f"{t['status']}, so it is excluded from revenue.")
    else:
        ans = f"{head} has revenue of {inr(t['revenue'])} (quantity x unit price - discount)."
    return dict(answer=ans, columns=cols, rows=rows, chart=None, notes=[], sql=SQL["transaction_lookup"])


HANDLERS = {
    "total_revenue": total_revenue,
    "top_product": _top("top_product", "product"),
    "top_region": _top("top_region", "region"),
    "monthly_trend": monthly_trend,
    "compare_months": compare_months,
    "order_count": order_count,
    "returned_orders": returned_orders,
    "data_issues": data_issues,
    "missing_price": missing_price,
    "transaction_lookup": transaction_lookup,
}


def _guard(conn, q, months):
    """Refuse (with an explanation) questions that name a filter or period we do not implement,
    instead of silently ignoring it. Runs for the LLM path and the rule path alike."""
    q = q.lower()
    first, last = fmt_month(months[0]), fmt_month(months[-1])
    forecast = re.search(r"forecast|predict|\bwhat will\b", q)          # refused later with the forecasting reason
    if not forecast and (m := re.search(r"\b(?:last|this|next|previous|current|past) (?:month|year|quarter|week)\b|"
                                        r"\brecent(?:ly)?\b|\blatest month\b", q)):
        raise AppError(400, f"'{m[0]}' depends on today's date, but this is a fixed dataset ({first} to {last}). "
                            f"Please name the month, e.g. '{calendar.month_name[int(months[-1][5:])]}' "
                            f"(the latest month with data).")
    if m := re.search(r"\b(q[1-4]|quarters?|quarterly|h[12]|(?:first|second) half|half[- ]year)\b", q):
        raise AppError(400, f"Quarters and half-years ('{m[0]}') aren't supported. Ask about one calendar month, "
                            f"or compare two, e.g. Q1 is January-March: try 'total revenue in March' or "
                            f"'compare January and March'.")
    if m := re.search(r"\b(weeks?|weekly|daily|days?|today|yesterday|tomorrow|ytd|mtd)\b", q):
        raise AppError(400, f"Revenue is only available by whole calendar month, not by '{m[0]}'.")
    terms = {r[0].lower() for r in conn.execute(
        "SELECT product FROM sales UNION SELECT category FROM sales UNION SELECT region FROM sales "
        "UNION SELECT sales_channel FROM sales")}
    hit = sorted(t for t in terms if re.search(rf"\b{re.escape(t)}\b", q))
    if hit:
        raise AppError(400, f"Filtering by {', '.join(hit)} isn't supported yet; I answer overall questions "
                            f"(optionally for one month) and rank products and regions.")
    for y in re.findall(r"\b(?:19|20)\d{2}\b", q):
        if y not in {m[:4] for m in months}:
            raise AppError(400, f"No sales data for {y}. Available months: {', '.join(map(fmt_month, months))}.")


def _data_note(conn):
    """Plain-English data-quality summary; every number comes straight from the database."""
    dq = data_quality(conn)
    loaded, st, rs = dq["loaded"], dq["by_status"], dq["revenue_status"]
    counted = conn.execute("SELECT COUNT(*) FROM sales WHERE status='Completed' AND revenue IS NOT NULL").fetchone()[0]
    unknown = conn.execute("SELECT COUNT(*) FROM sales WHERE status='Completed' AND revenue IS NULL").fetchone()[0]
    not_counted = [f"{n} {s.lower()} order(s)" for s, n in st.items() if s != "Completed"]
    if unknown:
        not_counted.append(f"{unknown} completed with a missing unit price (revenue unknown)")
    note = f"Revenue is based on {counted} of {loaded} loaded transactions (completed, revenue known)."
    if not_counted:
        note += f" Not counted: {', '.join(not_counted)}."
    zero = conn.execute("SELECT COUNT(*) FROM sales WHERE status='Completed' AND revenue_status='zero_quantity'").fetchone()[0]
    if zero:
        note += f" Counted at ₹0: {zero} zero-quantity transaction(s)."
    if dq["rejected"]:
        note += f" Rejected as unreadable: {len(dq['rejected'])}."
    return note


def ask(question, db_path, interpret_fn=interpret):
    """Full pipeline: validate input -> scope guard -> interpret -> run SQL -> structured answer."""
    if not isinstance(question, str) or not question.strip():
        raise AppError(422, "Please enter a question.")
    question = question.strip()
    if len(question) > MAX_QUESTION:
        raise AppError(422, f"Question is too long (max {MAX_QUESTION} characters).")
    conn = connect(db_path)
    try:
        months = [r[0] for r in conn.execute("SELECT DISTINCT month FROM sales ORDER BY month")]
        if not months:
            raise AppError(503, "No valid sales data is loaded.")
        _guard(conn, question, months)
        plan = interpret_fn(question)
        if plan.get("intent") not in HANDLERS:
            raise AppError(400, "Sorry, I can't answer that from the sales data.")
        res = HANDLERS[plan["intent"]](conn, plan["params"], months)
        res["notes"] = res["notes"] + [_data_note(conn)]
    finally:
        conn.close()
    return {"status": "ok", "question": question, **plan, **res}
