"""Pipeline tests (no web server needed). Expected values were verified against data/sales.csv with an
independent csv-module calculation (see test_matches_independent_calculation)."""
import csv
import sqlite3
from pathlib import Path

import pytest

from app import interpreter
from app.db import connect, data_quality, load_csv
from app.queries import _data_note, ask
from app.db import AppError

CSV = Path(__file__).resolve().parent.parent / "data" / "sales.csv"
HEADER = "Transaction_Id,Order_date,Product,Category,Region,Sales_channel,Quantity,Unit_price,Discount_amount,Status\n"


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)      # default: rule-based path, fully offline


@pytest.fixture
def db(tmp_path):
    load_csv(CSV, tmp_path / "sales.db")
    return tmp_path / "sales.db"


def build(tmp_path, *lines):
    (tmp_path / "x.csv").write_text(HEADER + "\n".join(lines) + "\n")
    load_csv(tmp_path / "x.csv", tmp_path / "x.db")
    return tmp_path / "x.db"


def fail(db, question):
    with pytest.raises(AppError) as e:
        ask(question, db)
    return e.value


# ---------- the five required questions ----------
def test_total_revenue(db):
    r = ask("What is the total revenue?", db)
    assert r["rows"][0][:2] == [1439200.0, 49] and "14,39,200" in r["answer"]


def test_top_product(db):
    r = ask("Which product generated the highest revenue?", db)
    assert r["rows"][0][:2] == ["Monitor", 473000.0] and r["answer"].startswith("Monitor")


def test_top_region(db):
    r = ask("Which region generated the highest sales?", db)
    assert r["rows"][0][:2] == ["South", 411200.0]


def test_monthly_trend(db):
    r = ask("What is the monthly revenue trend?", db)
    assert [x[1] for x in r["rows"]] == [210250, 234600, 223900, 240700, 318500, 211250]
    assert r["rows"][0][3] is None                                     # no previous month for January
    assert "highest month was May 2026" in r["answer"] and "lowest was Jan 2026" in r["answer"]
    assert any("partial month" in n and "28 Jun" in n for n in r["notes"])      # data ends mid-June


def test_compare_months(db):
    r = ask("How did revenue change between January and February?", db)
    assert r["rows"][1][3] == 24350 and r["rows"][1][4] == 11.58 and "increased" in r["answer"]


def test_month_filter(db):
    assert ask("What was the total revenue in March?", db)["rows"][0][0] == 223900


def test_matches_independent_calculation(db):
    total = sum(int(r["Quantity"]) * float(r["Unit_price"]) - float(r["Discount_amount"])
                for r in csv.DictReader(open(CSV)) if r["Status"] == "Completed" and r["Unit_price"])
    assert ask("total revenue", db)["rows"][0][0] == total


# ---------- ranking: lowest, worst, second-highest, top/bottom N ----------
def test_lowest_product_and_worst_region(db):
    r = ask("Which product generated the lowest revenue?", db)
    assert r["rows"][0][:2] == ["Laptop Stand", 96000.0] and r["answer"].startswith("Laptop Stand generated the lowest")
    r = ask("Which region is the worst performing?", db)
    assert r["answer"].startswith("West generated the lowest revenue") and r["rows"][0][1] == 296850


def test_second_highest_and_lowest(db):
    r = ask("What is the second-highest revenue product?", db)
    assert r["answer"].startswith("Desk generated the second-highest revenue") and r["rows"][1][4] == 2
    assert ask("Which region is third highest?", db)["answer"].startswith("East generated the third-highest")
    assert ask("second lowest product", db)["answer"].startswith("Keyboard generated the second-lowest")


def test_top_and_bottom_n(db):
    assert "Monitor (₹4,73,000); Desk (₹2,22,500); Office Chair (₹2,10,600)" in ask("top 3 products", db)["answer"]
    r = ask("bottom 2 regions", db)
    assert r["answer"].startswith("Bottom 2 regions") and [x[0] for x in r["rows"][:2]] == ["West", "East"]


def test_ranking_with_month_and_which_month(db):
    assert ask("Which product made the least in May?", db)["rows"][0][0] == "Laptop Stand"
    r = ask("Which month had the lowest revenue?", db)
    assert r["intent"] == "monthly_trend" and "lowest was Jan 2026" in r["answer"]


def test_rank_beyond_available_levels_is_explained(db):
    assert "no rank" in fail(db, "What is the fifth highest region?").message
    assert "Nth-ranked months" in fail(db, "second highest month").message


def test_tied_ranking_is_reported(tmp_path):
    db = build(tmp_path, "T1,03-01-2026,A,C,North,Online,1,100,0,Completed",
               "T2,04-01-2026,B,C,South,Online,1,100,0,Completed", "T3,05-01-2026,C,C,South,Online,1,50,0,Completed")
    assert "A and B (tied)" in ask("Which product generated the highest revenue?", db)["answer"]
    assert ask("lowest product", db)["answer"].startswith("C generated the lowest")
    assert ask("second highest product", db)["answer"].startswith("C generated the second-highest")  # dense ranking


# ---------- loading: every readable row is retained ----------
def test_all_51_transactions_are_retained(db):
    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 51
    assert con.execute("SELECT COUNT(*) FROM rejected_rows").fetchone()[0] == 0
    zero = con.execute("SELECT quantity, revenue, revenue_status FROM sales WHERE transaction_id='TXN0034'").fetchone()
    assert zero == (0, 0.0, "zero_quantity")                                   # revenue is 0, not unknown
    blank = con.execute("SELECT quantity, unit_price, revenue, revenue_status FROM sales "
                        "WHERE transaction_id='TXN0051'").fetchone()
    assert blank == (5, None, None, "unknown_price")                           # revenue is unknown, not 0, not guessed
    assert con.execute("SELECT status FROM sales WHERE transaction_id='TXN0033'").fetchone()[0] == "Returned"


def test_unknown_revenue_never_leaks_into_totals(db):
    con = connect(db)
    assert con.execute("SELECT ROUND(SUM(revenue),2) FROM sales WHERE status='Completed'").fetchone()[0] == 1439200
    assert ask("total revenue in June", db)["rows"][0][:2] == [211250.0, 8]      # TXN0051 (June) not counted


def test_loader_handles_dirty_data(tmp_path):
    db = build(tmp_path,
               "T1,03-01-2026,A,C,North,Online,2,100,0,completed",       # valid; status is normalised
               "T2,31-02-2026,A,C,North,Online,1,100,0,Completed",       # impossible date -> rejected
               "T1,04-01-2026,A,C,North,Online,1,100,0,Completed",       # duplicate id -> rejected
               "T3,05-01-2026,A,C,North,Online,1,100,-5,Completed",      # negative discount -> rejected
               "T4,05-01-2026,A,C,North,Online,abc,100,0,Completed",     # non-numeric quantity -> rejected
               "T5,05-01-2026,A,C,North,Online,1,100,500,Completed",     # discount > total -> rejected
               "T6,05-01-2026,A,C,North,Online,1,100,,Completed",        # blank discount = 0 -> valid
               "T7,05-01-2026,,C,North,Online,1,100,0,Completed",        # missing product -> rejected
               "T8,05-01-2026,A,C,North,Online,3,,0,Completed",          # blank price -> kept, unknown
               "T9,05-01-2026,A,C,North,Online,0,100,0,Completed",       # zero quantity -> kept, 0
               "T10,05-01-2026,A,C,North,Online,-1,100,0,Completed",     # negative quantity -> rejected
               "T11,05-01-2026,A,C,North,Online,2,abc,0,Completed")      # garbage price -> kept, unknown
    con = connect(db)
    assert dict(con.execute("SELECT transaction_id, revenue_status FROM sales")) == {
        "T1": "valid", "T6": "valid", "T8": "unknown_price", "T9": "zero_quantity", "T11": "unknown_price"}
    assert dict(con.execute("SELECT transaction_id, reason FROM rejected_rows")) == {
        "T2": "invalid_date", "T1": "duplicate_transaction_id", "T3": "invalid_discount", "T4": "invalid_quantity",
        "T5": "discount_exceeds_total", "T7": "missing_product", "T10": "negative_quantity"}
    assert con.execute("SELECT status FROM sales WHERE transaction_id='T1'").fetchone()[0] == "Completed"
    assert ask("total revenue", db)["rows"][0][0] == 200 + 100                  # T1 (200) + T6 (100)


def test_csv_with_missing_columns_fails_fast(tmp_path):
    (tmp_path / "bad.csv").write_text("a,b\n1,2\n")
    with pytest.raises(ValueError):
        load_csv(tmp_path / "bad.csv", tmp_path / "bad.db")


def test_connection_is_read_only(db):
    with pytest.raises(sqlite3.OperationalError):
        connect(db).execute("DELETE FROM sales")


def test_database_rejects_inconsistent_revenue(db, tmp_path):
    con = sqlite3.connect(db)                                                  # writable, to test the constraints
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE sales SET revenue = NULL WHERE transaction_id='TXN0001'")   # NULL revenue needs unknown_price


# ---------- missing prices, zero quantity, transaction lookup ----------
def test_missing_price_question_is_explained_not_answered_with_total(db):
    for q in ("What is the revenue of the transaction with a missing unit price?",
              "Which transactions have a missing unit price?", "revenue of transactions without a price"):
        r = ask(q, db)
        assert r["intent"] == "missing_price" and "TXN0051" in r["answer"] and "cannot be calculated" in r["answer"]
        assert "14,39,200" not in r["answer"] and "TXN0034" not in r["answer"]


def test_zero_quantity_question(db):
    r = ask("What is the revenue of the zero quantity transaction?", db)
    assert r["intent"] == "data_issues" and "TXN0034" in r["answer"] and "₹0" in r["answer"]


def test_transaction_lookup(db):
    assert "cannot be calculated" in ask("What is the revenue of TXN0051?", db)["answer"]
    assert "revenue is ₹0" in ask("revenue of txn0034", db)["answer"]
    assert "excluded from revenue" in ask("Show TXN0033", db)["answer"]
    assert "has revenue of ₹19,000" not in ask("Show TXN0033", db)["answer"]
    assert "revenue of ₹12,000" in ask("TXN0001", db)["answer"]
    assert "No transaction 'TXN9999'" in fail(db, "Show TXN9999").message


def test_month_with_only_unknown_revenue_is_explained(tmp_path):
    db = build(tmp_path, "T1,03-01-2026,A,C,North,Online,1,100,0,Completed",
               "T2,03-02-2026,A,C,North,Online,5,,0,Completed")
    assert "known price" in fail(db, "total revenue in February").message


# ---------- order counts ----------
def test_order_count(db):
    r = ask("How many orders are there?", db)
    assert dict(r["rows"]) == {"All transactions recorded": 51, "Completed": 50, "Returned": 1,
                               "Completed, revenue counted": 49, "Zero quantity (revenue ₹0)": 1,
                               "Missing unit price (revenue unknown)": 1}
    assert r["answer"].startswith("There are 51 transactions")


def test_order_count_for_a_month(db):
    r = ask("how many transactions in April", db)
    assert dict(r["rows"])["All transactions recorded"] == 10 and dict(r["rows"])["Returned"] == 1
    assert dict(r["rows"])["Completed"] == 9 and dict(r["rows"])["Zero quantity (revenue ₹0)"] == 1


def test_order_count_breakdowns_are_explained_not_faked(db):
    assert "not broken down" in fail(db, "how many orders per region").message
    assert "can't rank orders" in fail(db, "which product has the most orders").message
    assert "not compared across months" in fail(db, "how many orders in March vs April").message


# ---------- returned orders: value, not "loss" ----------
def test_returned_orders_report_value_without_claiming_a_loss(db):
    r = ask("How much revenue did returned orders lose?", db)
    assert r["intent"] == "returned_orders" and r["rows"][0][0] == "TXN0033" and r["rows"][0][7] == 19000
    assert "₹19,000" in r["answer"] and "not a measured financial loss" in r["answer"] and "1.32%" in r["answer"]
    assert "refund" in r["answer"]
    assert "7.89%" in ask("returned orders in April", db)["answer"]
    assert "no returned orders" in ask("returned orders in March", db)["answer"]


# ---------- time periods and month comparisons ----------
def test_relative_dates_quarters_and_weeks_are_explained(db):
    assert "fixed dataset" in fail(db, "revenue last month").message and "June" in fail(db, "revenue this year").message
    assert "Quarters" in fail(db, "What was revenue in Q1?").message
    assert "Quarters" in fail(db, "total revenue in the second quarter").message
    assert "whole calendar month" in fail(db, "revenue for the week of 5 Jan").message
    assert "whole calendar month" in fail(db, "revenue today").message


def test_ambiguous_phrasings_never_answer_a_different_question(db):
    assert ask("lowest revenue month", db)["intent"] == "monthly_trend"
    assert ask("month with the highest sales", db)["intent"] == "monthly_trend"
    assert ask("Is revenue growing?", db)["intent"] == "monthly_trend"
    assert ask("total revenue excluding returned orders", db)["rows"][0][0] == 1439200      # already how totals work
    assert "always exclude returned orders" in fail(db, "revenue including returned orders").message
    assert "one transaction at a time" in fail(db, "revenue of TXN0001 and TXN0002").message
    assert "not broken down" in fail(db, "revenue trend by region").message
    assert "can't rank orders" in fail(db, "Which transaction had the highest revenue?").message


def test_two_months_without_comparison_wording_are_explained(db):
    assert "compare two months" in fail(db, "revenue in January and February").message
    assert "range of months" in fail(db, "total revenue from January to March").message
    assert "two months at a time" in fail(db, "compare Jan Feb Mar").message
    assert "two months" in fail(db, "How did revenue change in March?").message
    assert "No sales data" in fail(db, "How did revenue change between January and July?").message


def test_compared_to_makes_the_second_month_the_base(db):
    r = ask("How did revenue change in March compared to February?", db)
    assert "decreased by ₹10,700" in r["answer"] and "from Feb 2026 (₹2,34,600) to Mar 2026 (₹2,23,900)" in r["answer"]
    assert ask("Did revenue increase in March compared to February?", db)["answer"].startswith("No, revenue did not increase")
    assert "from Jan 2026" in ask("Compare January with February", db)["answer"]      # plain 'compare': order as written


def test_revenue_increase_wording(db):
    r = ask("Did revenue increase from January to February?", db)
    assert r["intent"] == "compare_months" and r["rows"][1][3] == 24350 and r["answer"].startswith("Yes")
    assert ask("Did revenue fall between March and April?", db)["answer"].startswith("No, revenue did not decrease")


def test_higher_wording_names_the_winner_whatever_the_order(db):
    for q in ("Was February revenue higher than January?", "Did revenue go higher from January to February?",
              "Which month had higher revenue, January or February?"):
        assert ask(q, db)["answer"].startswith("Feb 2026 revenue (₹2,34,600) was higher than Jan 2026")


def test_trend_question_with_comparison_words_is_still_a_trend(db):
    assert ask("Did revenue rise or fall month by month?", db)["intent"] == "monthly_trend"
    assert ask("Is revenue increasing over the months?", db)["intent"] == "monthly_trend"
    assert ask("Which month had the highest revenue?", db)["intent"] == "monthly_trend"


def test_same_month_twice_and_zero_base(tmp_path, db):
    assert "two different months" in fail(db, "Compare January with January").message
    zero = build(tmp_path, "T1,03-01-2026,A,C,North,Online,1,100,100,Completed",          # fully discounted: revenue 0
                 "T2,03-02-2026,A,C,North,Online,5,100,0,Completed")
    r = ask("Compare January vs February", zero)
    assert r["rows"][1][4] is None and "undefined" in r["answer"]                         # no division by zero


# ---------- unsupported questions get a reason ----------
def test_unsupported_questions_explain_why(db):
    assert "sales channel" in fail(db, "revenue by sales channel").message
    assert "no cost information" in fail(db, "profit by product").message
    assert "Averages" in fail(db, "average order value").message
    assert "not units" in fail(db, "how many units were sold").message
    assert "Forecasting" in fail(db, "forecast next year revenue").message
    assert "Cancellations" in fail(db, "cancelled orders").message
    assert "monitor" in fail(db, "What is the revenue for Monitor?").message.lower()
    assert "No sales data for 2025" in fail(db, "Total revenue in 2025").message
    assert "I can answer questions about revenue" in fail(db, "What's the weather in Mumbai?").message


def test_empty_or_oversized_question(db):
    for q in ("", "   ", None, "x" * 301):
        assert fail(db, q).status == 422


def test_sql_injection_text_is_inert(db):
    assert fail(db, "'; DROP TABLE sales; --").status == 400
    assert "read-only" in fail(db, "total revenue; DROP TABLE sales; --").message
    assert ask("total revenue", db)["rows"][0][0] == 1439200                    # table intact


# ---------- data-quality figures always match the database ----------
def test_data_quality_numbers_match_the_database(db):
    con = connect(db)
    dq = data_quality(con)
    assert dq["loaded"] == con.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 51
    assert dq["by_status"] == {"Completed": 50, "Returned": 1}
    assert dq["revenue_status"] == {"valid": 49, "zero_quantity": 1, "unknown_price": 1}
    assert sum(dq["revenue_status"].values()) == dq["loaded"] and len(dq["rejected"]) == 0
    assert [f["transaction_id"] for f in dq["flagged"]] == ["TXN0034", "TXN0051"]
    counted = con.execute("SELECT COUNT(*) FROM sales WHERE status='Completed' AND revenue IS NOT NULL").fetchone()[0]
    note = _data_note(con)
    assert f"{counted} of 51 loaded" in note and "1 returned order(s)" in note
    assert "1 completed with a missing unit price" in note and "1 zero-quantity" in note
    assert ask("total revenue", db)["notes"][-1] == note                        # same note in every answer


def test_data_quality_partition_adds_up_on_dirty_data(tmp_path):
    db = build(tmp_path, "T1,03-01-2026,A,C,N,O,2,100,0,Completed", "T2,03-01-2026,A,C,N,O,1,,0,Completed",
               "T3,03-01-2026,A,C,N,O,0,100,0,Completed", "T4,03-01-2026,A,C,N,O,1,100,0,Returned",
               "T5,xx,A,C,N,O,1,100,0,Completed")
    con = connect(db)
    dq = data_quality(con)
    counted = con.execute("SELECT COUNT(*) FROM sales WHERE status='Completed' AND revenue IS NOT NULL").fetchone()[0]
    unknown = con.execute("SELECT COUNT(*) FROM sales WHERE status='Completed' AND revenue IS NULL").fetchone()[0]
    assert counted + unknown + dq["by_status"]["Returned"] == dq["loaded"] == 4 and len(dq["rejected"]) == 1
    assert "Rejected as unreadable: 1" in _data_note(con)


# ---------- AI layer ----------
def test_llm_routing_is_used_when_key_present(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(interpreter, "_llm", lambda q: {"intent": "top_region"})
    r = ask("Where do we sell the most?", db)
    assert r["interpreted_by"] == "llm" and r["rows"][0][0] == "South"


def test_llm_can_request_lowest_and_nth_rank(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(interpreter, "_llm", lambda q: {"intent": "top_product", "direction": "lowest", "rank": 2})
    assert ask("Which item sells worst after the laggard?", db)["answer"].startswith("Keyboard")


def test_llm_failure_falls_back_to_rules(db, monkeypatch):
    def boom(q):
        raise TimeoutError("LLM down")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(interpreter, "_llm", boom)
    r = ask("What is the total revenue?", db)
    assert (r["interpreted_by"], r["fallback_reason"], r["rows"][0][0]) == ("rules", "llm_error", 1439200)


def test_llm_output_is_validated_never_trusted(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    for bad in ({"intent": "DROP TABLE sales"}, {"intent": "monthly_trend", "month": "March"},
                {"intent": "top_product", "rank": 0}, {"intent": "top_product", "rank": "x"},
                {"intent": "top_product", "direction": "sideways"}, {"intent": "top_product", "rank": 2, "limit": 2},
                {"intent": "transaction_lookup", "transaction_id": "x; DROP TABLE sales"},
                {"intent": "compare_months", "month_a": "January"}):
        monkeypatch.setattr(interpreter, "_llm", lambda q, bad=bad: bad)
        assert fail(db, "Show me something").status == 400


def test_missing_price_question_shows_only_unknown_price_rows(db):
    r = ask("What is the revenue for the transaction with a missing unit price?", db)
    assert r["intent"] == "missing_price"
    assert [row[0] for row in r["rows"]] == ["TXN0051"]
    row = r["rows"][0]
    assert row[4] is None and row[6] is None and row[7] == "unknown_price"
    assert "excluded from revenue totals" in r["answer"]
    assert "rather than treated as ₹0" in r["answer"]


def test_missing_price_plural_question_shows_only_unknown_price_rows(db):
    r = ask("Which transactions have a missing unit price?", db)
    assert [row[0] for row in r["rows"]] == ["TXN0051"]
    assert "TXN0034" not in r["answer"]
