"""Checks for the shared Forge parse stage (scripts/parse.py and parse.sql, identical in every skill that parses)."""

import argparse
import io

import pytest
from conftest import SKILLS_DIR, load_script

parse = load_script("forge-unstructured-doc-extraction", "parse")


def args(**overrides) -> argparse.Namespace:
    values = dict(catalog="main", schema="docs", prefix="filings", description_element_types="*",
                  page_images_path="/Volumes/main/docs/images", split_pages="folder")
    return argparse.Namespace(**{**values, **overrides})


def doc(page_ids: list[int], element_ids: list[int] | None = None, errors=None) -> dict:
    """A minimal ai_parse_document output: one element per page unless element_ids says otherwise."""
    element_ids = element_ids if element_ids is not None else list(range(len(page_ids)))
    return {
        "document": {
            "pages": [{"id": p, "image_uri": f"/Volumes/x/p{p}.png"} for p in page_ids],
            "elements": [{"id": e, "type": "text", "content": f"e{e}",
                          "bbox": [{"coord": [0, 0, 1, 1], "page_id": page_ids[min(i, len(page_ids) - 1)]}]}
                         for i, e in enumerate(element_ids)],
        },
        "metadata": {"id": "doc", "version": "2.0"},
        "error_status": errors or [],
    }


def test_every_sql_placeholder_has_a_value():
    params = parse.build_params(args())
    extra = parse.slice_params(args(), 1, "501-1000")
    for sql in parse.load_sections(SKILLS_DIR / "forge-unstructured-doc-extraction" / "scripts" / "parse.sql").values():
        parse.render(sql, {**params, **extra})  # KeyError on a placeholder nothing fills


def test_split_pages_uses_a_files_table():
    assert parse.build_params(args(split_pages="none"))["files_table"].endswith("`filings_parsed`")
    assert parse.build_params(args())["files_table"].endswith("`filings_page_files`")


def test_slice_options_carry_a_literal_page_range_and_their_own_image_folder():
    extra = parse.slice_params(args(), 2, "1001-1203")
    assert extra["start_page"] == "1000"
    assert "'pageRange', '1001-1203'" in extra["slice_options"]
    assert "/Volumes/main/docs/images/slice_2/" in extra["slice_options"]


def test_page_ranges():
    assert parse.page_ranges(500) == ["1-500"]
    assert parse.page_ranges(1203) == ["1-500", "501-1000", "1001-1203"]


@pytest.mark.parametrize("second_slice_ids", [[0, 1, 2], [500, 501, 502]])
def test_stitch_renumbers_slices_numbered_either_way(second_slice_ids):
    first = doc(list(range(500)))
    second = doc(second_slice_ids, [0, 1, 2])
    stitched = parse.stitch([(500, second), (0, first)], expected_pages=503)
    pages = [p["id"] for p in stitched["document"]["pages"]]
    assert pages == list(range(503))
    elements = stitched["document"]["elements"]
    assert len({e["id"] for e in elements}) == len(elements) == 503
    assert [e["bbox"][0]["page_id"] for e in elements[-3:]] == [500, 501, 502]
    assert stitched["metadata"] == first["metadata"] and stitched["error_status"] == []


def test_stitch_reports_missing_pages_and_failed_slices():
    stitched = parse.stitch([(0, doc(list(range(500)))), (500, None)], expected_pages=700)
    messages = [e["error_message"] for e in stitched["error_status"]]
    assert any("no output" in m for m in messages)
    assert any("200 of 700 pages missing" in m for m in messages)


def test_stitch_shifts_error_page_ids():
    second = doc([0], errors=[{"page_id": 0, "error_message": "blurry"}])
    stitched = parse.stitch([(0, doc([0])), (1, second)])
    assert stitched["error_status"] == [{"page_id": 1, "error_message": "blurry"}]


def test_assemble_orders_by_file_name_number():
    files = [(f"page_{n}.pdf", doc([0]), None) for n in (10, 2, 1)]
    stitched, ordering = parse.assemble(files)
    assert ordering == "file_name_number"
    assert [e["content"] for e in stitched["document"]["elements"]] == ["e0"] * 3
    assert [p["id"] for p in stitched["document"]["pages"]] == [0, 1, 2]


def test_assemble_falls_back_to_printed_page_numbers():
    def printed(number):
        page = doc([0])
        page["document"]["elements"].append({"id": 1, "type": "page_number", "content": f"Page {number}",
                                             "bbox": [{"coord": [0, 0, 1, 1], "page_id": 0}]})
        return page

    files = [("b.pdf", printed(2), None), ("a.pdf", printed(3), None), ("c.pdf", printed(1), None)]
    stitched, ordering = parse.assemble(files)
    assert ordering == "printed_page_number"
    numbers = [e["content"] for e in stitched["document"]["elements"] if e["type"] == "page_number"]
    assert numbers == ["Page 1", "Page 2", "Page 3"]


def test_assemble_records_pages_that_failed_to_parse():
    files = [("page_1.pdf", doc([0]), None), ("page_2.pdf", None, "skipped: file larger than 100 MB")]
    stitched, _ = parse.assemble(files)
    assert stitched["error_status"] == [{"page_id": 1, "error_message": "page_2.pdf: skipped: file larger than 100 MB"}]


def test_pdf_page_count():
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    assert parse.pdf_page_count(buffer.getvalue()) == 3
    assert parse.pdf_page_count(b"not a pdf") == -1
