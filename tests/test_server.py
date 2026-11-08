"""Exercise uploads and CSV downloads through the dashboard's HTTP endpoints."""

import csv
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import io
import json
import threading
import unittest
from urllib.parse import quote

from openpyxl import Workbook

from server import CSV_COLUMNS, Store, make_handler


def workbook_bytes(rows):
    book = Workbook()
    sheet = book.worksheets[0]
    sheet.append(["Seller", "Buyer Name", "Phone", "Email", "Street", "City",
                  "Quantity", "Amount", "Payment Type", "Ball Numbers", "Notes"])
    for row in rows:
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def purchase(name="Buyer", numbers="101-103", **changes):
    values = dict(seller="Alex", name=name, phone="0012345678", email="buyer@example.test",
                  street="10 Example St", city="Waterloo", quantity=3, amount=50,
                  payment="Cash", numbers=numbers, notes="")
    values.update(changes)
    return list(values.values())


class CsvDownloadTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.folder.cleanup)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, data=None, headers=None):
        connection = HTTPConnection(*self.server.server_address, timeout=10)
        try:
            connection.request(method, path, body=data, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def upload(self, filename, rows):
        status, _, body = self.request("POST", "/api/files", workbook_bytes(rows),
                                       {"X-Filename": quote(filename)})
        self.assertEqual(status, 201, body)

    def download(self):
        status, headers, body = self.request("GET", "/api/export.csv")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn('filename="master_sales.csv"', headers["Content-Disposition"])
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
        self.assertEqual(reader.fieldnames, CSV_COLUMNS)
        return list(reader)

    def test_download_combines_files_and_preserves_csv_text(self):
        name = 'Élodie, "Lee"'
        notes = 'First line, with comma\nSecond line "quoted"'
        self.upload("Alex sales.xlsx", [purchase(name, notes=notes)])
        self.upload("Jamie sales.xlsx", [purchase("Other buyer", "104", seller="Jamie", quantity=1, amount=20)])
        rows = self.download()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Buyers Name"], name)
        self.assertEqual(rows[0]["Notes"], notes)
        self.assertEqual(rows[0]["Phone #"], "0012345678")
        self.assertEqual(rows[0]["Ball Numbers"], "101, 102, 103")
        self.assertEqual(rows[0]["$ Bought"], "50.00")
        self.assertEqual(rows[0]["Source File"], "Alex sales.xlsx")
        self.assertEqual(rows[0]["Source Row"], "2")
        self.assertTrue(all(row["Review Status"] == "OK" for row in rows))

    def test_download_retains_review_rows_and_protects_formula_text(self):
        self.upload("Review.xlsx", [purchase("", "101", quantity=2, amount=None, notes="=1+1"),
                                    purchase("Second", "101", quantity=1, amount=20)])
        rows = self.download()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["Review Status"] == "Needs review" for row in rows))
        self.assertEqual(rows[0]["$ Bought"], "")
        self.assertEqual(rows[0]["Notes"], "'=1+1")
        self.assertIn("Buyer name is missing", rows[0]["Issues"])
        self.assertIn("does not match", rows[0]["Issues"])
        self.assertIn("Ball 101 is assigned", rows[1]["Issues"])

    def test_replacement_and_removal_change_the_next_download(self):
        self.upload("Sales.xlsx", [purchase("Old buyer")])
        self.upload("Sales.xlsx", [purchase("Updated buyer")])
        rows = self.download()
        self.assertEqual([r["Buyers Name"] for r in rows], ["Updated buyer"])
        status, _, _ = self.request("DELETE", "/api/files/Sales.xlsx")
        self.assertEqual(status, 200)
        self.assertEqual(self.download(), [])

    def test_invalid_replacement_preserves_previous_export(self):
        self.upload("Sales.xlsx", [purchase("Original buyer")])
        status, _, _ = self.request("POST", "/api/files", b"not an Excel file", {"X-Filename": "Sales.xlsx"})
        self.assertEqual(status, 400)
        self.assertEqual(self.download()[0]["Buyers Name"], "Original buyer")

    def test_empty_export_has_headers_and_zero_purchase_summary(self):
        self.assertEqual(self.download(), [])
        status, _, body = self.request("GET", "/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["totals"]["purchases"], 0)


if __name__ == "__main__":
    unittest.main()
