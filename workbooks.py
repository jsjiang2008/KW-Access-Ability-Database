"""Import seller workbooks and export a traceable, values-only master workbook."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import re
import tempfile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Color, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from Record import Record


def text(value):
    return "" if value is None else str(value).strip()


def label(value):
    return re.sub(r"[^a-z0-9#$]", "", text(value).casefold())


ALIASES = {
    "name": {"buyersname", "buyername", "name", "customername"},
    "phoneNumber": {"phone#", "phone", "phonenumber", "telephone"},
    "email": {"email", "emailaddress"},
    "street": {"street", "address", "streetaddress"},
    "city": {"city", "town"},
    "numberBought": {"#bought", "numberbought", "quantity", "ticketssold", "ballssold"},
    "dollarBought": {"$bought", "dollarbought", "amount", "amountpaid", "salesamount"},
    "paymentType": {"paymenttype", "paymentmethod"},
    "notes": {"notes", "comments"},
    "seller": {"seller", "salesperson", "soldby"},
}
BALL_LABELS = {"ballnumbers", "ballnumber", "ticketnumbers", "ticketnumber", "numbers",
               "pleaseenterballnumbers", "toomanynumbers"}


@dataclass
class ImportResult:
    records: list[Record] = field(default_factory=list)
    sellers: set[str] = field(default_factory=set)
    notices: list[tuple[str, str, str]] = field(default_factory=list)
    log: list[tuple[str, str, int, str]] = field(default_factory=list)
    sources: set[Path] = field(default_factory=set)


def find_header(sheet, formulas):
    """Find a buyer table without treating side panels as additional records."""
    for row in sheet.iter_rows(max_row=min(sheet.max_row, 30)):
        mapping, balls = {}, []
        for cell in row:
            key = label(cell.value)
            for field_name, names in ALIASES.items():
                if key in names:
                    mapping.setdefault(field_name, cell.column)
            if key in BALL_LABELS or re.fullmatch(r"(?:ball|ticket)(?:number)?\d+", key):
                balls.append(cell.column)
        if "name" not in mapping or "numberBought" not in mapping:
            continue
        # The supplied form has three labels spanning I:M, N:R and S:W.
        # I3 is a formula, so its cached label may be missing or show a warning.
        template = (mapping.get("name") == 1 and mapping.get("numberBought") == 6
                    and mapping.get("dollarBought") == 7 and mapping.get("notes") == 24
                    and 14 in balls and 19 in balls)
        if template:
            balls = list(range(9, 24))
        elif balls:
            for merged in formulas.merged_cells.ranges:
                if merged.min_row == row[0].row and merged.min_col in balls:
                    balls.extend(range(merged.min_col, merged.max_col + 1))
        if balls:
            return row[0].row, mapping, sorted(set(balls)), template
    return None


def seller_for(sheet, path, header_row):
    for row in sheet.iter_rows(max_row=header_row - 1) if header_row > 1 else []:
        for cell in row:
            value = text(cell.value)
            match = re.fullmatch(r"(.+?)\s+Sales(?:\s+\d+)?", value, re.I)
            if match and match[1].strip().casefold() not in {"name", "seller", "salesperson"}:
                return match[1].strip(), False
            if label(value) in {"seller", "salesperson", "soldby"}:
                name = text(sheet.cell(cell.row, cell.column + 1).value)
                if name:
                    return name, False
    if not re.fullmatch(r"Sheet\d*", sheet.title, re.I):
        return re.sub(r"\s+(?:Sales\s+)?\d+$", "", sheet.title, flags=re.I), False
    return f"{path.stem} / {sheet.title}", True


def decimal_value(value):
    if isinstance(value, bool):
        raise ValueError("expected a number")
    try:
        result = Decimal(text(value).replace("$", "").replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("expected a number") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("expected a non-negative number")
    return result


def positive_integer(value):
    result = decimal_value(value)
    if result != result.to_integral_value() or result < 1:
        raise ValueError("expected a positive whole number")
    return int(result)


def parse_numbers(values):
    numbers, errors = [], []
    for value in values:
        if not text(value):
            continue
        parts = re.split(r"[,;\s]+", re.sub(r"\s*[-–]\s*", "-", text(value)))
        for part in parts:
            try:
                if re.fullmatch(r"\d+-\d+", part):
                    start, end = map(int, part.split("-"))
                    if start < 1 or end < start or end - start > 10000:
                        raise ValueError("invalid or excessively large range")
                    numbers.extend(range(start, end + 1))
                else:
                    numbers.append(positive_integer(part))
            except ValueError:
                errors.append(f"Invalid ball number: {part!r}.")
    return numbers, errors


def template_amount(formula, row, quantity):
    """Evaluate ONLY the exact pricing expression found in the supplied forms."""
    expected = f'=IFS(F{row}="","",F{row}=1,20,F{row}=2,40,F{row}>2,(F{row}/3)*50)'
    normalized = text(formula).replace("_xlfn.", "").replace(" ", "").upper()
    if normalized != expected or quantity is None:
        return None
    amount = Decimal(quantity * 20) if quantity < 3 else Decimal(quantity) / 3 * 50
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def read_sheet(sheet, formulas, path, result):
    header = find_header(sheet, formulas)
    if not header:
        result.log.append((str(path), sheet.title, 0, "Skipped: no supported buyer table"))
        return False
    header_row, mapping, ball_columns, template = header
    seller, unnamed = seller_for(sheet, path, header_row)
    if not unnamed and "seller" not in mapping:
        result.sellers.add(seller)
    count = 0
    formula_notices = set()
    for row_num in range(header_row + 1, sheet.max_row + 1):
        # Stop at the form's totals; never read its ticket inventory as purchases.
        row_labels = {label(sheet.cell(row_num, c).value) for c in range(1, mapping["numberBought"])}
        quantity_formula = text(formulas.cell(row_num, mapping["numberBought"]).value).upper()
        has_ball_entries = any(text(sheet.cell(row_num, c).value) for c in ball_columns)
        if (row_labels & {"total", "totals", "grandtotal", "ballssold", "totalsales"}
                and not has_ball_entries
                and (quantity_formula.startswith("=SUM(")
                     or label(sheet.cell(row_num, mapping["name"]).value) in {"total", "totals", "grandtotal"})):
            break
        cells = {key: sheet.cell(row_num, col) for key, col in mapping.items()}
        ball_cells = [sheet.cell(row_num, col) for col in ball_columns]
        for cell in [*cells.values(), *ball_cells]:
            raw = formulas[cell.coordinate]
            if raw.data_type == "f" and "[" in str(raw.value):
                formula_notices.add("External workbook links detected. Values are the last saved Excel results; refresh links and save the source before importing.")
            elif raw.data_type == "f" and cell.value is None and not (template and cell.column == 7):
                formula_notices.add("Some formulas have no saved result. Open, recalculate and save the source in Excel, then re-import.")

        # Blank and zero-only template rows are not sales. Ignore their price formulas.
        meaningful = []
        for key, cell in cells.items():
            if key == "seller" or (key == "dollarBought" and formulas[cell.coordinate].data_type == "f"):
                continue
            meaningful.append(cell.value)
        meaningful.extend(cell.value for cell in ball_cells)
        if not any(value not in (None, "", 0, "0") and text(value) for value in meaningful):
            continue

        def value(key):
            return cells[key].value if key in cells else None

        row_seller = text(value("seller")) or seller
        result.sellers.add(row_seller)
        numbers, issues = parse_numbers([cell.value for cell in ball_cells])
        if unnamed and not text(value("seller")):
            issues.append("Seller name is missing; using the file and sheet name. Replace the template title with the seller's name.")
        if not text(value("name")):
            issues.append("Buyer name is missing.")
        try:
            quantity = positive_integer(value("numberBought"))
        except ValueError:
            quantity = None
            issues.append("Quantity is missing or is not a positive whole number.")
        if quantity is not None and quantity != len(numbers):
            issues.append(f"Quantity ({quantity}) does not match ball numbers entered ({len(numbers)}).")
        if not numbers:
            issues.append("No valid ball numbers entered.")

        amount_cell = cells.get("dollarBought")
        raw_amount = formulas[amount_cell.coordinate].value if amount_cell else None
        amount = template_amount(raw_amount, row_num, quantity) if template else None
        if amount is None:
            try:
                amount = decimal_value(value("dollarBought")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except (ValueError, InvalidOperation):
                amount = None
                issues.append("Sales amount is missing or invalid; excluded from dollar totals.")
        for key, cell in cells.items():
            if key == "dollarBought" and amount is not None:
                continue
            if cell.data_type == "e" or (formulas[cell.coordinate].data_type == "f" and cell.value is None):
                issues.append(f"{cell.coordinate} has an Excel error or no saved formula result.")
        result.records.append(Record(
            name=text(value("name")), phoneNumber=text(value("phoneNumber")),
            email=text(value("email")), street=text(value("street")), city=text(value("city")),
            numberBought=quantity, dollarBought=amount, numbers=numbers,
            seller=row_seller, paymentType=text(value("paymentType")), notes=text(value("notes")),
            sourceFile=str(path), sourceSheet=sheet.title, sourceRow=row_num, issues=issues,
            rawQuantity=text(value("numberBought")), rawAmount=text(value("dollarBought")),
            rawNumbers="; ".join(text(cell.value) for cell in ball_cells if text(cell.value)),
        ))
        count += 1
    result.notices.extend((str(path), sheet.title, message) for message in sorted(formula_notices))
    result.log.append((str(path), sheet.title, count, "Imported" if count else "Buyer table is empty"))
    return True


def import_workbooks(paths):
    result = ImportResult()
    for source in paths:
        path = Path(source).expanduser().resolve()
        if path in result.sources:
            result.log.append((str(path), "", 0, "Skipped: same input file supplied again"))
            continue
        if path.suffix.lower() != ".xlsx":
            raise ValueError(f"Expected an .xlsx file: {path}")
        result.sources.add(path)
        values = load_workbook(path, data_only=True, keep_links=False)
        try:
            formulas = load_workbook(path, data_only=False, keep_links=False)
            try:
                recognized = [read_sheet(sheet, formulas[sheet.title], path, result) for sheet in values]
                if not any(recognized):
                    raise ValueError(f"No supported buyer table found in {path.name}. Expected buyer name, quantity and ball-number headers.")
            finally:
                formulas.close()
        finally:
            values.close()
    if not result.sources:
        raise ValueError("Choose at least one input workbook.")

    owners = defaultdict(list)
    for rec in result.records:
        for number, occurrences in Counter(rec.numbers).items():
            owners[number].append(rec)
            if occurrences > 1:
                rec.issues.append(f"Ball {number} appears {occurrences} times in this row.")
    for number, records in owners.items():
        if len(records) > 1:
            for rec in records:
                rec.issues.append(f"Ball {number} is assigned in {len(records)} purchase rows; see Master Sales for the other sources.")
    return result


def append_values(sheet, values):
    """Keep imported text as text, including names/notes beginning with '='."""
    sheet.append(values)
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str):
            cell.data_type = "s"


def format_sheet(sheet, header_row=1, currency_columns=()):
    sheet.freeze_panes = f"B{header_row + 1}"
    sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(sheet.max_column)}{sheet.max_row}"
    sheet.sheet_view.showGridLines = False
    for cell in sheet[header_row]:
        cell.fill = PatternFill("solid", fgColor=Color(rgb="173D4B"))
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    sheet.row_dimensions[header_row].height = 32
    for row in sheet.iter_rows(min_row=header_row + 1):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if cell.row % 2 == 0:
                cell.fill = PatternFill("solid", fgColor=Color(rgb="EDF5F5"))
            if cell.column in currency_columns:
                cell.number_format = '"$"#,##0.00'
    for col in sheet.columns:
        width = max((min(len(text(cell.value)), 60) for cell in col), default=12)
        sheet.column_dimensions[get_column_letter(col[0].column)].width = min(max(width + 2, 14), 62)
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.print_title_rows = f"{header_row}:{header_row}"


def export_master(result, destination, overwrite=False):
    path = Path(destination).expanduser().resolve()
    if path.suffix.lower() != ".xlsx":
        raise ValueError("The output filename must end in .xlsx.")
    if path in result.sources:
        raise ValueError("The output must be different from every input workbook.")
    if path.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {path}. Choose another name or use --overwrite.")

    workbook = Workbook()
    # A new workbook always contains a worksheet; active can also be None or a chart.
    summary = workbook.worksheets[0]
    if not isinstance(summary, Worksheet):
        raise TypeError("The summary must be a writable worksheet.")
    summary.title = "Seller Summary"
    summary.append(["KW AccessAbility — Ball Drop Sales"])
    summary.append(["Snapshot of imported files. Re-run after sellers update their spreadsheets."])
    summary.append(["Totals include rows needing review. Dollar totals exclude missing/invalid amounts. Unique balls are not proof of valid sales."])
    summary.append([f"{len(result.records)} purchase rows; {sum(bool(r.issues) for r in result.records)} rows need review; {len(result.notices)} source notices. See Import Issues."])
    for row in range(1, 5):
        summary.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
        summary.cell(row, 1).alignment = Alignment(wrap_text=True, vertical="center")
        summary.row_dimensions[row].height = 30
    summary.cell(1, 1).font = Font(size=20, bold=True, color="173D4B")
    summary.append(["Seller", "Purchase Rows", "Tickets Reported", "Unique Ball Numbers", "Sales Amount", "Rows to Review", "Missing Quantities", "Missing Amounts"])
    groups = defaultdict(list)
    display_names = {}
    for seller in sorted(result.sellers):
        display_names.setdefault(seller.casefold(), seller)
        groups[seller.casefold()]
    for rec in result.records:
        groups[rec.seller.casefold()].append(rec)

    def totals(name, records):
        return [name, len(records), sum(r.numberBought or 0 for r in records),
                len({n for r in records for n in r.numbers}),
                sum((r.dollarBought for r in records if r.dollarBought is not None), Decimal(0)),
                sum(bool(r.issues) for r in records), sum(r.numberBought is None for r in records),
                sum(r.dollarBought is None for r in records)]

    for key, records in sorted(groups.items()):
        append_values(summary, totals(display_names[key], records))
    append_values(summary, totals("TOTAL (unique balls across all sellers)", result.records))
    format_sheet(summary, 5, (5,))
    summary.auto_filter.ref = f"A5:H{max(5, summary.max_row - 1)}"
    for column in range(1, summary.max_column + 1):
        summary.cell(summary.max_row, column).font = Font(bold=True)
    for col in "ABCDEFGH":
        summary.column_dimensions[col].width = 24 if col == "A" else 19

    master = workbook.create_sheet("Master Sales")
    master.append(["Seller", "Buyers Name", "Phone #", "email", "Street", "City", "# Bought", "$ Bought", "Payment Type", "Ball Numbers", "Notes", "Review Status", "Issues", "Source File", "Source Sheet", "Source Row", "Original Quantity", "Original Amount", "Original Ball Numbers"])
    issues = workbook.create_sheet("Import Issues")
    issues.append(["Source File", "Source Sheet", "Source Row", "Seller", "Buyer", "Issue"])
    for rec in result.records:
        append_values(master, [rec.seller, rec.name, rec.phoneNumber, rec.email, rec.street, rec.city,
                              rec.numberBought, rec.dollarBought, rec.paymentType,
                              ", ".join(map(str, rec.numbers)), rec.notes,
                              "Needs review" if rec.issues else "OK", " ".join(rec.issues),
                              rec.sourceFile, rec.sourceSheet, rec.sourceRow,
                              rec.rawQuantity, rec.rawAmount, rec.rawNumbers])
        for issue in rec.issues:
            append_values(issues, [rec.sourceFile, rec.sourceSheet, rec.sourceRow, rec.seller, rec.name, issue])
    for file, sheet, notice in result.notices:
        append_values(issues, [file, sheet, None, "", "", notice])
    log = workbook.create_sheet("Import Log")
    log.append(["Source File", "Source Sheet", "Imported Purchase Rows", "Result"])
    for entry in result.log:
        append_values(log, entry)
    format_sheet(master, currency_columns=(8,))
    format_sheet(issues)
    format_sheet(log)
    # Save completely before replacing an existing export; never modify the inputs.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".xlsx", delete=False) as file:
            temporary = Path(file.name)
        workbook.save(temporary)
        temporary.replace(path)
    finally:
        workbook.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path
