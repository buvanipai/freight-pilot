# tests/test_tableau_export.py
import csv
import os

import pytest

REQUIRED_COLUMNS = {
    "id", "mode", "status", "freight_value_usd",
    "origin_state", "destination_state", "created_at",
}

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set",
)


def test_tableau_export_file_exists_with_data(tmp_path):
    from db.export_tableau import export

    out = tmp_path / "shipments_export.csv"
    row_count = export(output_file=out)

    assert out.exists(), "export file not created"
    assert row_count > 0, "export is empty — pipeline may not have run"


def test_tableau_export_required_columns(tmp_path):
    from db.export_tableau import export

    out = tmp_path / "shipments_export.csv"
    export(output_file=out)

    with open(out) as f:
        reader = csv.DictReader(f)
        columns = set(reader.fieldnames or [])

    missing = REQUIRED_COLUMNS - columns
    assert not missing, f"missing columns: {missing}"


def test_tableau_export_row_count_matches(tmp_path):
    from db.export_tableau import export

    out = tmp_path / "shipments_export.csv"
    row_count = export(output_file=out)

    with open(out) as f:
        file_rows = sum(1 for _ in csv.DictReader(f))

    assert file_rows == row_count, (
        f"return value {row_count} != rows in file {file_rows}"
    )
