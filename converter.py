"""Excel -> PDF -> JPEG conversion using headless LibreOffice and Poppler."""

import os
import shutil
import subprocess
from pathlib import Path

DPI = int(os.environ.get("JPEG_DPI", "300"))
LIBREOFFICE_TIMEOUT = int(os.environ.get("LIBREOFFICE_TIMEOUT", "120"))
PDFTOPPM_TIMEOUT = int(os.environ.get("PDFTOPPM_TIMEOUT", "60"))

# File signatures: .xlsx is a ZIP container, legacy .xls is an OLE2 compound file.
XLSX_MAGIC = b"PK\x03\x04"
XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

MACOS_SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"


class ConversionError(Exception):
    """A failure whose message is safe to show to the user."""


def find_libreoffice() -> str:
    candidates = [os.environ.get("LIBREOFFICE_BIN"), "libreoffice", "soffice", MACOS_SOFFICE]
    for candidate in candidates:
        if candidate and shutil.which(candidate):
            return shutil.which(candidate)
    raise ConversionError("LibreOffice is not installed on the server.")


def find_pdftoppm() -> str:
    path = shutil.which(os.environ.get("PDFTOPPM_BIN", "pdftoppm"))
    if not path:
        raise ConversionError("pdftoppm (Poppler) is not installed on the server.")
    return path


def check_signature(header: bytes, suffix: str) -> None:
    """Reject files whose contents don't match their Excel extension."""
    expected = XLSX_MAGIC if suffix == ".xlsx" else XLS_MAGIC
    if not header.startswith(expected):
        raise ConversionError("This file doesn't look like a valid Excel workbook. Is it corrupted or renamed?")


def excel_to_pdf(excel_path: Path, workdir: Path) -> Path:
    profile_dir = workdir / "lo_profile"
    try:
        result = subprocess.run(
            [
                find_libreoffice(),
                f"-env:UserInstallation={profile_dir.as_uri()}",
                "--headless",
                "--invisible",
                "--nodefault",
                "--nofirststartwizard",
                "--nolockcheck",
                "--nologo",
                "--norestore",
                "--convert-to", "pdf",
                "--outdir", str(workdir),
                str(excel_path),
            ],
            capture_output=True,
            timeout=LIBREOFFICE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ConversionError("Converting the spreadsheet took too long. Please try again.")

    pdf_path = workdir / f"{excel_path.stem}.pdf"
    if result.returncode != 0 or not pdf_path.exists():
        raise ConversionError("The spreadsheet could not be converted. Check that it opens correctly in Excel.")
    return pdf_path


def pdf_first_page_to_jpeg(pdf_path: Path, output_path: Path) -> Path:
    prefix = pdf_path.with_name("page")
    try:
        result = subprocess.run(
            [
                find_pdftoppm(), "-jpeg", "-r", str(DPI), "-f", "1", "-l", "1", "-singlefile",
                str(pdf_path), str(prefix),
            ],
            capture_output=True,
            timeout=PDFTOPPM_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ConversionError("Rendering the image took too long. Please try again.")

    rendered = prefix.with_suffix(".jpg")
    if result.returncode != 0 or not rendered.exists():
        raise ConversionError("The converted sheet could not be rendered to JPEG.")
    rendered.rename(output_path)
    return output_path
