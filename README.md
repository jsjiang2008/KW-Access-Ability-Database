# KW-Access-Ability-Database
Communihacks 2026 project by me, Tim Xia, Chester Wang, and Jason Qian!

Combine ball-drop seller spreadsheets into one Excel workbook. The first tab
summarizes each seller's ticket quantities and dollar sales; the next tab contains
every populated buyer row with its seller and original file, sheet and row.

## Run

Requires Python 3.10 or newer. From this project folder:

```sh
python -m pip install -r requirements.txt
python main.py
```

Enter a file or folder at each prompt, then enter a blank line to finish selecting
inputs. You will then be prompted for the output filename. Paths containing spaces
can be pasted with or without surrounding double quotes. On Linux, use `python3`
if `python` is unavailable; on Windows, `py` also works.

You can also provide all inputs on the command line:

```sh
python main.py "Edward Sales.xlsx" "Brett Sales.xlsx" -o "Master Sales.xlsx"
python main.py "seller_forms" -o "Master Sales.xlsx"
```

Folder input reads `.xlsx` files directly inside that folder, excluding Excel lock
files (`~$...`) and the selected output file. Keep previous exports out of input
folders. Use `--overwrite` to replace a previous export. Input workbooks are never
modified, and an output path cannot also be an input path. Selecting the same file
twice imports it only once. A missing, unreadable or unsupported workbook stops
the run without writing a partial export.

## Web dashboard

```sh
python server.py
```

Open http://127.0.0.1:8000 (use `--port` to change it). Upload seller `.xlsx`
files with the button or by dragging them in. The page shows the uploaded files,
total tickets and revenue, a seller leaderboard, and a ball-number search that
returns the buyer's name and contact details. **Export CSV** downloads every
purchase row with its review status and issues.

The dashboard uses the same import rules as `main.py`. It only listens on this
computer, and uploaded files are kept in a temporary folder that is deleted when
the server stops, so re-upload them after a restart.

## Output tabs

- **Seller Summary:** purchase rows, tickets reported, unique ball numbers, sales
  amounts, and counts of records needing review or missing quantities/amounts.
  Named sellers with empty forms appear with zero sales.
- **Master Sales:** combined buyer/contact/address details, quantities, amounts,
  payment types, ball numbers, notes, review status, and source information.
  Original quantity, amount and ball-number text is kept for checking invalid data.
- **Import Issues:** missing names, unknown sellers, invalid values, mismatched
  quantities, duplicate ball numbers, and notices about source formulas.
- **Import Log:** imported and skipped sheets and their record counts.

Totals include records needing review. Missing/invalid quantities and amounts are
excluded from the corresponding numeric total and counted in separate columns.
Dollar totals are **sales amounts**, not confirmation that money was received;
payment types such as `Unpaid` are retained on Master Sales. Duplicate ball numbers
are flagged on every affected row, including duplicates within a purchase. Rows
are retained so someone can resolve conflicts without losing buyer information.
The overall unique-ball count counts each number once across all sellers.

This is a file-based snapshot: re-run after sellers update their files. It does not
reserve numbers or provide live availability. The output's totals are saved values;
fix source spreadsheets and regenerate instead of editing the export.

## Supported spreadsheet layout

The supplied 2026 and blank salesperson workbooks are supported, including all
seller sheets in one workbook. Headers can be within the first 30 rows. Required
headers identify buyer name, quantity and ball numbers. Common alternatives such
as `Buyer Name`, `Quantity`, `Amount`, `Seller`, and `Ticket Numbers` are accepted.
Buyer tables end at their total row. Assignment, inventory and bookkeeping sheets
without buyer headers are skipped and listed in Import Log.

The supplied forms store buyer details in A:H, **all 15 ball-number columns in
I:W**, and notes in X. Other supported layouts can use one ball-number column,
multiple labeled columns, or a merged ball-number header. Ball cells may contain
individual positive integers, comma/semicolon/space-separated lists, or inclusive
ranges such as `101-103`. Decimal ticket numbers, negative numbers and invalid
text are flagged; valid numbers from the same row are retained.

Set the supplied form's A1 title to a plain seller name such as `Edward F`, or
to `Edward F Sales 1`. This takes precedence over a copied worksheet tab name.
Multiple sheets titled
`Edward F Sales 1` and `Edward F Sales 2` are grouped under `Edward F`. A `Seller`
column can instead specify the seller on each row. Otherwise a named worksheet is
used. Generic sheets with an unchanged `Name Sales 1` title use the file/sheet name
and get a review flag when they contain a purchase. Seller grouping ignores case.

The program recognizes and calculates the **exact pricing formula in the supplied
forms**: 1 ticket costs $20, 2 cost $40, and quantities above 2 use `quantity / 3 *
50`, rounded to cents per purchase. This deliberately matches the source formula
(e.g. 4 costs $66.67); it does not assume a different bundle policy. Explicit
amounts are preserved. Other formulas use their last saved Excel results; missing
or erroneous amounts are flagged rather than guessed. External workbook links
produce a notice: open Excel, refresh/recalculate and save before importing if
those links are used. The importer does not execute Excel formulas or refresh
external links.

The provided samples contain mostly empty seller tables. The file called
`Blank Salesperson Form.xlsx` includes an unnamed test purchase for 3 tickets
with 4 ball numbers; it is retained and marked for review.

## Code and tests

`Record.py` keeps the original eight constructor fields and the lowercase `record`
alias, adding seller, payment, notes, source and validation information.
`workbooks.py` handles import, validation, aggregation and export. `main.py` provides
interactive and command-line entry points. No database or internet connection is
needed during use.

```sh
python -m unittest discover -s tests -v
```

Tests generate temporary spreadsheets with fictional data; no buyer workbooks are
committed to the repository.
