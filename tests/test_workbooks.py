from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook, load_workbook

from main import main
from Record import Record, record
from workbooks import export_master, import_workbooks


class WorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def template(self, filename="seller.xlsx", seller="Alex Sales 1", rows=()):
        book = Workbook()
        sheet = book.active
        sheet.title = "Sheet1"
        sheet["A1"] = seller
        for col, header in enumerate(["Buyers Name", "Phone #", "email", "Street", "City", "# Bought", "$ Bought", "Payment Type"], 1):
            sheet.cell(3, col, header)
        sheet["I3"] = '=IF(F105=I105,"Ball Numbers","Please Enter Ball Numbers")'
        sheet["N3"] = sheet["S3"] = "Ball Numbers"
        sheet["X3"] = "Notes"
        sheet["AH3"] = "Payment Type"
        for r in range(4, 105):
            sheet.cell(r, 7, f'=_xlfn.IFS(F{r}="","",F{r}=1,20,F{r}=2,40,F{r}>2,(F{r}/3)*50)')
            sheet.cell(r, 26, r - 3)  # Inventory must not become a purchase.
        for r, values in enumerate(rows, 4):
            for col, value in values.items():
                sheet[f"{col}{r}"] = value
        sheet["E105"] = "Balls Sold"
        sheet["F105"] = "=SUM(F4:F104)"
        sheet["G105"] = "=SUM(G4:G104)"
        sheet["A106"] = "Bookkeeping, not a buyer"
        book.create_sheet("Assignments")["A1"] = "Ticket Assignments"
        path = self.folder / filename
        book.save(path)
        book.close()
        return path

    def simple(self, filename, rows, title="Sales"):
        book = Workbook()
        sheet = book.active
        sheet.title = title
        sheet.append(["Seller", "Buyer Name", "Phone", "Quantity", "Amount", "Ticket Numbers", "Notes"])
        for row in rows:
            sheet.append(row)
        path = self.folder / filename
        book.save(path)
        book.close()
        return path

    def test_template_all_ball_columns_price_and_footer(self):
        path = self.template(rows=[{"A": "Buyer", "B": "0012345", "F": 15,
                                    **{chr(73 + i): i + 1 for i in range(15)}, "X": "Note"}])
        result = import_workbooks([path])
        self.assertEqual(len(result.records), 1)
        rec = result.records[0]
        self.assertEqual(rec.numbers, list(range(1, 16)))
        self.assertEqual(rec.dollarBought, Decimal("250.00"))
        self.assertEqual(rec.phoneNumber, "0012345")
        self.assertEqual(rec.seller, "Alex")
        self.assertEqual(rec.issues, [])
        self.assertTrue(any("Skipped" in item[3] for item in result.log))

    def test_combine_sellers_and_multiple_sales_sheets(self):
        one = self.template("one.xlsx", rows=[{"A": "Buyer One", "F": 3, "I": 1, "J": 2, "K": 3}])
        book = load_workbook(one)
        second = book.copy_worksheet(book["Sheet1"])
        second["A1"] = "Alex Sales 2"
        second["I4"], second["J4"], second["K4"] = 4, 5, 6
        book.save(one)
        book.close()
        two = self.template("two.xlsx", "Jamie Sales 1", [{"A": "Buyer Two", "F": 1, "I": 7, "G": 18}])
        result = import_workbooks([one, two])
        target = export_master(result, self.folder / "master.xlsx")
        book = load_workbook(target, data_only=True)
        self.addCleanup(book.close)
        self.assertEqual(book.sheetnames, ["Seller Summary", "Master Sales", "Import Issues", "Import Log"])
        rows = list(book["Seller Summary"].iter_rows(min_row=6, values_only=True))
        self.assertEqual(rows[0][:5], ("Alex", 2, 6, 6, 100))
        self.assertEqual(rows[1][:5], ("Jamie", 1, 1, 1, 18))
        self.assertEqual(rows[-1][1:5], (3, 7, 7, 118))
        self.assertEqual(book["Master Sales"].max_row, 4)
        self.assertEqual(book["Master Sales"]["P2"].value, 4)

    def test_duplicate_file_not_imported_twice(self):
        path = self.template(rows=[{"A": "Buyer", "F": 1, "I": 1}])
        result = import_workbooks([path, path])
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(result.sources), 1)

    def test_conflicting_balls_and_duplicates_within_row(self):
        a = self.simple("a.xlsx", [["Alex", "One", "", 3, 50, "1, 1, 2", ""]])
        b = self.simple("b.xlsx", [["Jamie", "Two", "", 1, 20, 2, ""]])
        result = import_workbooks([a, b])
        self.assertEqual(len(result.records), 2)
        self.assertTrue(any("Ball 1 appears 2 times" in issue for issue in result.records[0].issues))
        for rec in result.records:
            self.assertTrue(any("Ball 2 is assigned" in issue for issue in rec.issues))
        book = load_workbook(export_master(result, self.folder / "master.xlsx"), data_only=True)
        self.addCleanup(book.close)
        summary = book["Seller Summary"]
        self.assertEqual(summary.cell(summary.max_row, 4).value, 2)

    def test_incomplete_sample_is_preserved_and_flagged(self):
        path = self.template(seller="Name Sales 1", rows=[{"F": 0}, {"F": 0}, {"F": 3, "I": 880, "J": 881, "K": 882, "L": 884}])
        result = import_workbooks([path])
        self.assertEqual(len(result.records), 1)
        rec = result.records[0]
        self.assertEqual(rec.sourceRow, 6)
        self.assertEqual(rec.dollarBought, Decimal("50.00"))
        self.assertTrue(any("Buyer name" in issue for issue in rec.issues))
        self.assertTrue(any("Seller name" in issue for issue in rec.issues))
        self.assertTrue(any("does not match" in issue for issue in rec.issues))

    def test_invalid_values_preserve_raw_data_and_valid_balls(self):
        path = self.simple("bad.xlsx", [["Alex", "Buyer", "", "two", "unknown", "1; 2.5; -3; bad; 4-6", ""]])
        result = import_workbooks([path])
        rec = result.records[0]
        self.assertIsNone(rec.numberBought)
        self.assertIsNone(rec.dollarBought)
        self.assertEqual(rec.numbers, [1, 4, 5, 6])
        self.assertEqual(rec.rawQuantity, "two")
        self.assertIn("bad", rec.rawNumbers)
        self.assertGreaterEqual(len(rec.issues), 5)

    def test_exact_formula_and_explicit_amounts(self):
        path = self.template(rows=[{"A": "One", "F": 4, "I": "1-4"},
                                   {"A": "Two", "F": 1, "I": 5, "G": 10},
                                   {"A": "Three", "F": 1, "I": 6, "G": "=F6*99"}])
        result = import_workbooks([path])
        self.assertEqual([r.dollarBought for r in result.records], [Decimal("66.67"), Decimal("10.00"), None])
        self.assertTrue(result.records[2].issues)

    def test_external_formulas_get_notice_even_with_empty_cached_values(self):
        path = self.template(rows=[{"A": "='[other.xlsx]Sales'!A4"}])
        result = import_workbooks([path])
        self.assertEqual(result.records, [])
        self.assertTrue(any("External workbook links" in n[2] for n in result.notices))

    def test_empty_named_seller_has_zero_summary(self):
        path = self.template()
        result = import_workbooks([path])
        self.assertEqual(result.records, [])
        book = load_workbook(export_master(result, self.folder / "master.xlsx"), data_only=True)
        self.addCleanup(book.close)
        self.assertEqual(book["Seller Summary"]["A6"].value, "Alex")
        self.assertEqual(book["Seller Summary"]["E6"].value, 0)

    def test_text_is_not_exported_as_executable_formula(self):
        path = self.simple("text.xlsx", [["Alex", "Buyer", "", 1, 20, 1, "safe"]])
        result = import_workbooks([path])
        result.records[0].notes = '=HYPERLINK("https://example.invalid","test")'
        book = load_workbook(export_master(result, self.folder / "master.xlsx"))
        self.addCleanup(book.close)
        self.assertEqual(book["Master Sales"]["K2"].data_type, "s")

    def test_output_cannot_overwrite_input_or_existing_file(self):
        path = self.template()
        original = path.read_bytes()
        result = import_workbooks([path])
        with self.assertRaises(ValueError):
            export_master(result, path, overwrite=True)
        target = export_master(result, self.folder / "master.xlsx")
        with self.assertRaises(FileExistsError):
            export_master(result, target)
        self.assertEqual(path.read_bytes(), original)

    def test_unsupported_workbook_stops_without_partial_export(self):
        good = self.template()
        bad = self.folder / "bad.xlsx"
        book = Workbook()
        book.save(bad)
        book.close()
        target = self.folder / "out.xlsx"
        with redirect_stderr(StringIO()):
            code = main([str(good), str(bad), "-o", str(target)])
        self.assertEqual(code, 1)
        self.assertFalse(target.exists())

    def test_cli_folder_excludes_locks_and_selected_output(self):
        self.template(rows=[{"A": "Buyer", "F": 1, "I": 1}])
        (self.folder / "~$seller.xlsx").write_text("Excel lock")
        target = self.folder / "master.xlsx"
        with redirect_stdout(StringIO()):
            self.assertEqual(main([str(self.folder), "-o", str(target)]), 0)
            self.assertEqual(main([str(self.folder), "-o", str(target), "--overwrite"]), 0)
        book = load_workbook(target)
        self.addCleanup(book.close)
        self.assertEqual(book["Master Sales"].max_row, 2)

    def test_original_record_constructor_works(self):
        rec = record("Buyer", "555", "", "", "", 1, 20, [1])
        self.assertIsInstance(rec, Record)
        self.assertEqual(rec.numberBought, 1)

    def test_buyer_named_total_is_not_mistaken_for_footer(self):
        path = self.simple("total.xlsx", [["Alex", "Total", "", 1, 20, 1, ""]])
        result = import_workbooks([path])
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].name, "Total")

    def test_1800_tickets_aggregate_without_losing_purchases(self):
        rows = [[f"Seller {i % 10}", f"Buyer {i}", "", 3, 50,
                 f"{3 * i + 1}-{3 * i + 3}", ""] for i in range(600)]
        result = import_workbooks([self.simple("annual.xlsx", rows)])
        book = load_workbook(export_master(result, self.folder / "annual_master.xlsx"), data_only=True)
        self.addCleanup(book.close)
        summary = book["Seller Summary"]
        self.assertEqual(book["Master Sales"].max_row, 601)
        self.assertEqual([c.value for c in summary[summary.max_row]][1:6], [600, 1800, 1800, 30000, 0])


if __name__ == "__main__":
    unittest.main()
