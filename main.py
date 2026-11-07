"""Run with workbook paths, or with no arguments for interactive prompts."""

import argparse
from pathlib import Path
import sys
from zipfile import BadZipFile

from workbooks import export_master, import_workbooks


def main(argv=None):
    parser = argparse.ArgumentParser(description="Combine ball-drop seller spreadsheets into a master workbook with seller totals.")
    parser.add_argument("inputs", nargs="*", help="Input .xlsx files or folders containing .xlsx files")
    parser.add_argument("-o", "--output", default="master_sales.xlsx", help="Output workbook (default: master_sales.xlsx)")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output workbook")
    args = parser.parse_args(argv)
    if not args.inputs:
        print("Enter each seller workbook or folder, one at a time. Press Enter on a blank line to finish.")
        while True:
            value = input("Input path: ").strip().strip('"')
            if not value:
                break
            args.inputs.append(value)
        if not args.inputs:
            print("No input files selected.")
            return 1
        args.output = input(f"Output workbook [{args.output}]: ").strip().strip('"') or args.output

    output = Path(args.output).expanduser().resolve()
    inputs = []
    for entry in args.inputs:
        path = Path(entry).expanduser()
        if path.is_dir():
            inputs.extend(sorted(p for p in path.iterdir() if p.is_file()
                                 and p.suffix.lower() == ".xlsx" and not p.name.startswith("~$")
                                 and p.resolve() != output))
        else:
            inputs.append(path)
    try:
        if output in {p.resolve() for p in inputs}:
            raise ValueError("The output must be different from every input workbook.")
        if output.exists() and not args.overwrite:
            raise FileExistsError(f"Output already exists: {output}. Choose another name or use --overwrite.")
        result = import_workbooks(inputs)
        export_master(result, output, overwrite=args.overwrite)
    except (OSError, ValueError, BadZipFile, KeyError) as exc:
        print(f"Could not create the master workbook: {exc}", file=sys.stderr)
        return 1
    print(f"Created {output}")
    print(f"Imported {len(result.records)} purchase rows from {len(result.sources)} workbook(s).")
    review = sum(bool(record.issues) for record in result.records)
    print(f"Rows needing review: {review}. Source notices: {len(result.notices)}. See Import Issues in the workbook.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        sys.exit(1)
