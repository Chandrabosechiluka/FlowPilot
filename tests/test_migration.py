"""Legacy DMS migration: normalisation, connector factory, plan, apply, re-runs and interruptions."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone

import pytest

from flowpilot.migration import apply_plan, build_plan, create_connector
from flowpilot.migration.applier import Ledger, PlanMismatch
from flowpilot.migration.connectors import ConnectorError, available_connectors
from flowpilot.migration.normalise import (
    Rejected,
    clean_email,
    clean_name,
    clean_phone,
    clean_registration,
    clean_year,
    expand_make,
)
from flowpilot.migration.planner import MigrationPlan
from tests.conftest import ROOT

pytestmark = pytest.mark.anyio

SAMPLE = ROOT / "samples" / "legacy" / "autoledger"
NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


# ------------------------------------------------------------- normalisation


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("9000011111", "+91 90000 11111"), ("+91-98480-12345", "+91 98480 12345"), ("09848012346", "+91 98480 12346"),
     ("91 98480 12345", "+91 98480 12345")],
)
def test_indian_phone_numbers_are_normalised(raw, expected):
    assert clean_phone(raw) == (expected, [])


def test_odd_phone_is_kept_with_a_warning_and_nonsense_is_rejected():
    value, warnings = clean_phone("040-2345678")
    assert value == "040-2345678" and warnings
    with pytest.raises(Rejected):
        clean_phone("12345")


def test_names_emails_registrations_years_makes():
    assert clean_name("  RAVI   KUMAR ") == ("Ravi Kumar", ["name 'RAVI KUMAR' changed to 'Ravi Kumar'"])
    assert clean_name("Anitha Rao") == ("Anitha Rao", [])
    assert clean_email(" Ravi@Example.COM ") == ("ravi@example.com", [])
    assert clean_email("a@b")[1]  # accepted, with a warning
    with pytest.raises(Rejected, match="missing"):
        clean_email("")
    with pytest.raises(Rejected, match="not valid"):
        clean_email("a@@b.com")
    assert clean_registration("ts-09 ex 1234") == "TS09EX1234"  # same rule as GarageFlow
    assert clean_year("21", now=NOW) == (2021, ["two-digit year '21' read as 2021"])
    assert clean_year("95", now=NOW)[0] == 1995
    with pytest.raises(Rejected, match="outside"):
        clean_year("1975", now=NOW)
    codes = {"MSIL": "Maruti Suzuki"}
    assert expand_make("msil", codes) == ("Maruti Suzuki", [])
    assert expand_make("BAJ", codes)[1]
    assert expand_make("Bajaj", codes) == ("Bajaj", [])


# ---------------------------------------------------------------- connectors


def test_connector_factory():
    assert "autoledger-csv" in available_connectors()
    assert create_connector("autoledger-csv", path=SAMPLE).kind == "autoledger-csv"
    with pytest.raises(ConnectorError, match="Unknown source"):
        create_connector("cdk-xml", path=SAMPLE)


def test_missing_columns_are_reported(tmp_path):
    (tmp_path / "customers.csv").write_text("CUST_NO,NAME\nC1,x\n", encoding="utf-8")
    (tmp_path / "vehicles.csv").write_text("VEH_ID\n", encoding="utf-8")
    with pytest.raises(ConnectorError, match="missing columns"):
        create_connector("autoledger-csv", path=tmp_path).customers()


def test_excel_byte_order_mark_is_handled(tmp_path):
    shutil.copytree(SAMPLE, tmp_path / "x")
    path = tmp_path / "x" / "customers.csv"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert create_connector("autoledger-csv", path=tmp_path / "x").customers()[0].legacy_id == "C1001"


# ---------------------------------------------------------------------- plan


@pytest.fixture
async def plan(gf) -> MigrationPlan:
    connector = create_connector("autoledger-csv", path=SAMPLE)
    return await build_plan(connector, gf, source_options={"path": str(SAMPLE)}, target="http://garageflow.test", now=NOW)


async def test_plan_summary(plan):
    s = plan.summary
    assert (s.customers_in_source, s.customers_to_create, s.customers_to_link, s.duplicates_merged) == (8, 4, 1, 1)
    assert (s.vehicles_in_source, s.vehicles_to_add, s.vehicles_to_skip) == (11, 7, 1)
    assert s.rejected == 5


async def test_plan_links_existing_customer_and_merges_duplicates(plan):
    by_key = {c.key: c for c in plan.customers}
    assert by_key["C1001"].action == "link" and by_key["C1001"].garageflow_id
    assert by_key["C1002"].legacy_ids == ["C1002", "C1006"]
    assert "phone +91 98480 99999 was not kept" in by_key["C1002"].warnings[0]
    seltos = next(v for v in plan.vehicles if v.legacy_id == "V007")
    assert seltos.owner_key == "C1002"  # the merged duplicate's car goes to the surviving customer


async def test_plan_rejections_explain_why(plan):
    reasons = {r.legacy_id: r.reasons for r in plan.rejected}
    assert "email is missing" in reasons["C1004"][0]
    assert "C1004 was rejected" in reasons["V006"][0]
    assert "1975" in reasons["V008"][0]
    assert "not in the customer export" in reasons["V010"][0]


async def test_plan_warns_about_a_registration_owned_by_someone_else(plan):
    city = next(v for v in plan.vehicles if v.legacy_id == "V005")
    assert city.action == "add" and "Sanjay Reddy" in city.warnings[0]
    swift = next(v for v in plan.vehicles if v.legacy_id == "V001")
    assert swift.action == "skip"


async def test_plan_lists_fields_that_cannot_be_migrated(plan):
    assert "LOYALTY_POINTS" in plan.not_migrated_fields["customers"]
    assert "ODOMETER_KM" in plan.not_migrated_fields["vehicles"]


async def test_plan_round_trips_as_json(plan):
    again = MigrationPlan.model_validate_json(plan.model_dump_json())
    assert again == plan
    assert "Rejected" in plan.report()


async def test_planning_writes_nothing(plan, sandbox_state):
    assert not [p for m, p, _ in sandbox_state.requests_seen if m == "POST" and p != "/api/auth/login"]


# --------------------------------------------------------------------- apply


async def test_apply_creates_links_and_adds(plan, gf, tmp_path, sandbox_state):
    connector = create_connector("autoledger-csv", path=SAMPLE)
    report = await apply_plan(plan, connector, gf, Ledger(tmp_path / "ledger.json"))
    assert (len(report.customers_created), len(report.customers_linked), len(report.vehicles_added)) == (4, 1, 7)
    assert not report.failed and not report.stopped_early

    anitha = await gf.find_customer_by_email("anitha.rao@example.com")
    assert anitha.phone == "+91 98480 12345"
    assert sorted(v.registration_number for v in anitha.vehicles) == ["TS08AB7788", "TS10XY5555"]
    ravi = await gf.find_customer_by_email("ravi.kumar@example.com")
    assert sorted(v.registration_number for v in ravi.vehicles) == ["TS09EX1234", "TS09GH4321"]


async def test_second_run_with_the_ledger_writes_nothing(plan, gf, tmp_path, sandbox_state):
    connector = create_connector("autoledger-csv", path=SAMPLE)
    ledger_path = tmp_path / "ledger.json"
    await apply_plan(plan, connector, gf, Ledger(ledger_path))
    posts_before = sum(1 for m, p, _ in sandbox_state.requests_seen if m == "POST")
    again = await apply_plan(plan, connector, gf, Ledger.load(ledger_path))
    posts_after = sum(1 for m, p, _ in sandbox_state.requests_seen if m == "POST")
    assert again.already_done == 12 and posts_after == posts_before


async def test_lost_ledger_still_does_not_duplicate(plan, gf, tmp_path):
    connector = create_connector("autoledger-csv", path=SAMPLE)
    await apply_plan(plan, connector, gf, Ledger(tmp_path / "a.json"))
    report = await apply_plan(plan, connector, gf, Ledger(tmp_path / "b.json"))  # fresh ledger
    assert report.customers_created == [] and len(report.customers_linked) == 5  # 409 → linked
    assert report.vehicles_added == []
    page = await gf.list_customers(search="anitha", page_size=100)
    assert page.total_count == 1


async def test_outage_stops_the_run_and_a_rerun_finishes_it(plan, gf, tmp_path, sandbox_state):
    connector = create_connector("autoledger-csv", path=SAMPLE)
    ledger_path = tmp_path / "ledger.json"
    await gf.list_bays()  # sign in first
    sandbox_state.faults.fail_next = 1  # the first create fails, and POSTs are never retried automatically
    first = await apply_plan(plan, connector, gf, Ledger(ledger_path))
    assert first.stopped_early and "HTTP 503" in first.stopped_early
    second = await apply_plan(plan, connector, gf, Ledger.load(ledger_path))
    assert not second.stopped_early and not second.failed
    total_customers = len(first.customers_created) + len(second.customers_created)
    assert total_customers == 4


async def test_changed_export_is_refused(plan, gf, tmp_path):
    folder = tmp_path / "export"
    shutil.copytree(SAMPLE, folder)
    plan = plan.model_copy(update={"source_options": {"path": str(folder)}})
    with (folder / "customers.csv").open("a", encoding="utf-8") as handle:
        handle.write("C2000,New Person,new@example.com,9000000000,,,,,\n")
    with pytest.raises(PlanMismatch):
        await apply_plan(plan, create_connector("autoledger-csv", path=folder), gf, Ledger(tmp_path / "l.json"))


async def test_ledger_file_is_written_atomically(plan, gf, tmp_path):
    ledger_path = tmp_path / "ledger.json"
    await apply_plan(plan, create_connector("autoledger-csv", path=SAMPLE), gf, Ledger(ledger_path))
    assert ledger_path.exists()
    assert not list(tmp_path.glob(".ledger-*"))  # no temp files left behind
    assert len(Ledger.load(ledger_path).customers) == 5
