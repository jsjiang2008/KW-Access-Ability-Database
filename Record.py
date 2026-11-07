"""A buyer's purchase, including the seller and original spreadsheet row."""

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Record:
    # Keep the original eight fields and positional constructor arguments.
    name: str
    phoneNumber: str
    email: str
    street: str
    city: str
    numberBought: int | None
    dollarBought: Decimal | None
    numbers: list[int]
    seller: str = ""
    paymentType: str = ""
    notes: str = ""
    sourceFile: str = ""
    sourceSheet: str = ""
    sourceRow: int = 0
    issues: list[str] = field(default_factory=list)
    rawQuantity: str = ""
    rawAmount: str = ""
    rawNumbers: str = ""


# Existing code using `from Record import record` continues to work.
record = Record

