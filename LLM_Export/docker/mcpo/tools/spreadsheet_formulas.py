"""Relocate formulas together with the supplied A1-based data grid."""
import re

from openpyxl.formula.tokenizer import Tokenizer, TokenizerError
from openpyxl.utils.cell import column_index_from_string, get_column_letter

from .file_security import FilePolicyError


def relocate_formula(formula, row_delta, column_delta, sheet_name, sheet_names):
    """Move same-sheet A1 references, retaining $ markers for subsequent edits.

    The tool supplies one grid. Existing other sheets are not moved. Named and
    structured references, external workbooks and unsupported reference syntax
    fail explicitly instead of producing unverifiable spreadsheet results.
    """
    try:
        tokens = Tokenizer(formula).items
    except TokenizerError as exc:
        raise FilePolicyError("Unsupported spreadsheet formula syntax; use A1 cell or range references.") from exc

    def coordinate(value):
        cell = re.fullmatch(r'(\$?)([A-Za-z]{1,3})(\$?)([1-9][0-9]*)', value)
        column = re.fullmatch(r'(\$?)([A-Za-z]{1,3})', value)
        row_only = re.fullmatch(r'(\$?)([1-9][0-9]*)', value)
        if cell:
            col_anchor, col_name, row_anchor, row_number = cell.groups()
        elif column:
            col_anchor, col_name = column.groups()
            row_anchor, row_number = '', None
        elif row_only:
            row_anchor, row_number = row_only.groups()
            col_anchor, col_name = '', None
        else:
            raise FilePolicyError("Named ranges, structured references and non-A1 spreadsheet references are not supported.")
        col = column_index_from_string(col_name) + column_delta if col_name else None
        row = int(row_number) + row_delta if row_number else None
        if (col is not None and not 1 <= col <= 16384) or (row is not None and not 1 <= row <= 1048576):
            raise FilePolicyError("A relocated spreadsheet reference exceeds Excel's row or column limits.")
        return (col_anchor + get_column_letter(col) if col else '') + (row_anchor + str(row) if row else '')

    for token in tokens:
        if token.type == 'FUNC' and token.subtype == 'OPEN' and token.value.rstrip('(').upper() in ('INDIRECT', '_XLFN.INDIRECT'):
            raise FilePolicyError("INDIRECT text-based spreadsheet references cannot be relocated safely and are not supported.")
        if token.type != 'OPERAND' or token.subtype != 'RANGE':
            continue
        value = token.value
        qualifier, separator, reference = value.rpartition('!')
        other_sheet = False
        if separator:
            unquoted = qualifier[1:-1].replace("''", "'") if qualifier.startswith("'") and qualifier.endswith("'") else qualifier
            if '[' in unquoted or ']' in unquoted or ':' in unquoted:
                raise FilePolicyError("External workbook and multi-sheet range references are not supported.")
            if unquoted.casefold() != sheet_name.casefold():
                if unquoted.casefold() not in {name.casefold() for name in sheet_names}:
                    raise FilePolicyError("The spreadsheet formula refers to a sheet that does not exist in the output workbook.")
                # This tool changes only the active sheet's supplied grid.
                other_sheet = True
        else:
            reference = value
        parts = reference.split(':')
        if len(parts) > 2 or (len(parts) == 1 and not re.fullmatch(r'\$?[A-Za-z]{1,3}\$?[1-9][0-9]*', reference)):
            raise FilePolicyError("Named ranges, structured references and non-A1 spreadsheet references are not supported.")
        patterns = [r'\$?[A-Za-z]{1,3}\$?[1-9][0-9]*', r'\$?[A-Za-z]{1,3}', r'\$?[1-9][0-9]*']
        if not any(all(re.fullmatch(pattern, part) for part in parts) for pattern in patterns):
            raise FilePolicyError("The spreadsheet formula contains an unsupported A1 range.")
        if other_sheet:
            continue
        token.value = (qualifier + '!' if separator else '') + ':'.join(coordinate(part) for part in parts)
    return '=' + ''.join(token.value for token in tokens)
