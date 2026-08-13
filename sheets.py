"""
Reads daily/weekly/monthly/custom-range report data out of a Mens Cave filial spreadsheet.
Assumes the same template layout used across filials:
  row4  = date per day-column
  row5  = clients total, row6 = repeat, row7 = new
  row9  = revenue total, row10 = terminal, row11 = cash, row12 = transfer
  row13 = goods sold (count), row14 = goods revenue, row15 = goods revenue (cash)
  row16 = 2GIS reviews, row17 = Yandex reviews
  row21 = kassa start, row22 = kassa end, row23 = discrepancy
  row25 = admin name
  DAY_BLOCKS: B:H, J:P, R:X, Z:AF, AH:AN, AP:AV — 7 day-columns each, followed by
  a totals column (I, Q, Y, AG, AO, AW) with a week SUM already computed, and row1 of
  each block holds a merged "dd.mm - dd.mm" label. AZ5..AZ20 holds the whole-month totals.
KASSA sheet ("КАССА <Месяц> <Год>"): day rows starting at row6, columns H(date)..O(status).
"""
from google.oauth2 import service_account
from googleapiclient.discovery import build
import os
import json
import datetime as dt

SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']

MONTHS_RU = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль',
             'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']

DAY_BLOCK_BOUNDS = [(2, 8), (10, 16), (18, 24), (26, 32), (34, 40), (42, 48)]

ROW_CLIENTS_TOTAL, ROW_CLIENTS_REPEAT, ROW_CLIENTS_NEW = 5, 6, 7
ROW_REVENUE_TOTAL, ROW_TERMINAL, ROW_CASH, ROW_TRANSFER = 9, 10, 11, 12
ROW_GOODS_COUNT, ROW_GOODS_TOTAL, ROW_GOODS_CASH = 13, 14, 15
ROW_REVIEWS_2GIS, ROW_REVIEWS_YANDEX = 16, 17
ROW_KASSA_START, ROW_KASSA_END, ROW_DISCREPANCY = 21, 22, 23
ROW_ADMIN = 25

SUMMARY_ROWS = [ROW_CLIENTS_TOTAL, ROW_CLIENTS_REPEAT, ROW_CLIENTS_NEW,
                ROW_REVENUE_TOTAL, ROW_TERMINAL, ROW_CASH, ROW_TRANSFER,
                ROW_GOODS_COUNT, ROW_GOODS_TOTAL, ROW_GOODS_CASH,
                ROW_REVIEWS_2GIS, ROW_REVIEWS_YANDEX]

_service = None


def get_service():
    global _service
    if _service:
        return _service
    creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    if creds_json:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds_file = os.environ['GOOGLE_CREDENTIALS_FILE']
        creds = service_account.Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    _service = build('sheets', 'v4', credentials=creds)
    return _service


def _sheet_name_for(date):
    return f'{MONTHS_RU[date.month - 1]} {date.year}'


def _col_letter(n):
    s = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _fetch_grid(spreadsheet_id, sheet_name, a1_range):
    service = get_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"'{sheet_name}'!{a1_range}"
    ).execute()
    return resp.get('values', [])


def _num(v):
    if v in (None, ''):
        return 0
    if isinstance(v, (int, float)):
        return v
    s = str(v).replace('\xa0', '').replace(' ', '').replace('₽', '').replace(',', '.')
    try:
        return float(s) if '.' in s else int(s)
    except ValueError:
        return 0


def find_day_column(spreadsheet_id, sheet_name, date):
    """Returns the 1-indexed column number whose row4 matches `date`, or None."""
    target = date.strftime('%d.%m.%Y')
    rows = _fetch_grid(spreadsheet_id, sheet_name, 'A4:AW4')
    if not rows:
        return None
    row = rows[0]
    for i, v in enumerate(row):
        if v == target:
            return i + 1
    return None


def _week_block_bounds(day_col):
    for start, end in DAY_BLOCK_BOUNDS:
        if start <= day_col <= end:
            return start, end
    return None


def find_week_total_column(spreadsheet_id, sheet_name, date):
    """Returns the 1-indexed total column for the week block containing `date`."""
    day_col = find_day_column(spreadsheet_id, sheet_name, date)
    if day_col is None:
        return None
    bounds = _week_block_bounds(day_col)
    if not bounds:
        return None
    start, _ = bounds
    return start + 7  # 7 day-columns then the total column


def get_week_label(spreadsheet_id, sheet_name, date):
    """Reads the merged 'dd.mm - dd.mm' header text for the week block containing `date`."""
    day_col = find_day_column(spreadsheet_id, sheet_name, date)
    if day_col is None:
        return ''
    bounds = _week_block_bounds(day_col)
    if not bounds:
        return ''
    start, _ = bounds
    letter = _col_letter(start)
    vals = _fetch_grid(spreadsheet_id, sheet_name, f'{letter}1')
    return vals[0][0] if vals and vals[0] else ''


def _read_column_by_letter(spreadsheet_id, sheet_name, letter, rows):
    a1 = f'{letter}{min(rows)}:{letter}{max(rows)}'
    values = _fetch_grid(spreadsheet_id, sheet_name, a1)
    out = {}
    row_start = min(rows)
    for r in rows:
        idx = r - row_start
        out[r] = values[idx][0] if idx < len(values) and values[idx] else ''
    return out


def _read_column(spreadsheet_id, sheet_name, col, rows):
    return _read_column_by_letter(spreadsheet_id, sheet_name, _col_letter(col), rows)


def _summary_dict(vals, extra=None):
    d = {
        'clients_total': _num(vals[ROW_CLIENTS_TOTAL]),
        'clients_repeat': _num(vals[ROW_CLIENTS_REPEAT]),
        'clients_new': _num(vals[ROW_CLIENTS_NEW]),
        'revenue_total': _num(vals[ROW_REVENUE_TOTAL]),
        'revenue_terminal': _num(vals[ROW_TERMINAL]),
        'revenue_cash': _num(vals[ROW_CASH]),
        'revenue_transfer': _num(vals[ROW_TRANSFER]),
        'goods_count': _num(vals[ROW_GOODS_COUNT]),
        'goods_total': _num(vals[ROW_GOODS_TOTAL]),
        'goods_cash': _num(vals[ROW_GOODS_CASH]),
        'reviews_2gis': _num(vals[ROW_REVIEWS_2GIS]),
        'reviews_yandex': _num(vals[ROW_REVIEWS_YANDEX]),
    }
    if extra:
        d.update(extra)
    return d


def get_day_report(spreadsheet_id, date):
    sheet_name = _sheet_name_for(date)
    col = find_day_column(spreadsheet_id, sheet_name, date)
    if col is None:
        return None
    rows_needed = SUMMARY_ROWS + [ROW_KASSA_START, ROW_KASSA_END, ROW_DISCREPANCY, ROW_ADMIN]
    vals = _read_column(spreadsheet_id, sheet_name, col, rows_needed)
    kassa_status = get_kassa_status(spreadsheet_id, sheet_name, date)
    return _summary_dict(vals, {
        'date': date, 'sheet_name': sheet_name, 'period': 'day',
        'kassa_start': _num(vals[ROW_KASSA_START]),
        'kassa_end': _num(vals[ROW_KASSA_END]),
        'discrepancy': _num(vals[ROW_DISCREPANCY]),
        'admin': vals[ROW_ADMIN] or '—',
        'kassa_status': kassa_status,
    })


def get_week_report(spreadsheet_id, date):
    sheet_name = _sheet_name_for(date)
    col = find_week_total_column(spreadsheet_id, sheet_name, date)
    if col is None:
        return None
    vals = _read_column(spreadsheet_id, sheet_name, col, SUMMARY_ROWS)
    label = get_week_label(spreadsheet_id, sheet_name, date)
    return _summary_dict(vals, {'date': date, 'sheet_name': sheet_name, 'period': 'week', 'label': label})


def get_month_report(spreadsheet_id, date):
    sheet_name = _sheet_name_for(date)
    vals = _read_column_by_letter(spreadsheet_id, sheet_name, 'AZ', SUMMARY_ROWS)
    return _summary_dict(vals, {'date': date, 'sheet_name': sheet_name, 'period': 'month'})


def get_range_report(spreadsheet_id, start_date, end_date):
    """Sums day-by-day across an arbitrary (possibly cross-month) range."""
    totals = {k: 0 for k in [
        'clients_total', 'clients_repeat', 'clients_new',
        'revenue_total', 'revenue_terminal', 'revenue_cash', 'revenue_transfer',
        'goods_count', 'goods_total', 'goods_cash', 'reviews_2gis', 'reviews_yandex']}
    found_any = False
    d = start_date
    while d <= end_date:
        day = get_day_report(spreadsheet_id, d)
        if day:
            found_any = True
            for k in totals:
                totals[k] += day.get(k, 0)
        d += dt.timedelta(days=1)
    if not found_any:
        return None
    totals.update({'date': start_date, 'date_end': end_date, 'period': 'range'})
    return totals


def get_kassa_status(spreadsheet_id, report_sheet_name, date):
    """Reads the precomputed Статус cell from 'КАССА <Месяц> <Год>' for this date."""
    kassa_sheet = f'КАССА {report_sheet_name}'
    target = date.strftime('%d.%m.%Y')
    try:
        rows = _fetch_grid(spreadsheet_id, kassa_sheet, 'H6:H36')
    except Exception:
        return None
    for i, row in enumerate(rows):
        if row and row[0] == target:
            status_rows = _fetch_grid(spreadsheet_id, kassa_sheet, f'O{6+i}')
            return status_rows[0][0] if status_rows and status_rows[0] else None
    return None


def get_kassa_day_detail(spreadsheet_id, date):
    """Full picture for 'КАССА <Месяц> <Год>' on this date: the auto-summary row
    (H:O) plus any movements the admin logged (A:F) for the same date."""
    sheet_name = _sheet_name_for(date)
    kassa_sheet = f'КАССА {sheet_name}'
    target = date.strftime('%d.%m.%Y')

    try:
        date_rows = _fetch_grid(spreadsheet_id, kassa_sheet, 'H6:H36')
    except Exception:
        return None

    summary = None
    for i, row in enumerate(date_rows):
        if row and row[0] == target:
            r = 6 + i
            vals = _fetch_grid(spreadsheet_id, kassa_sheet, f'H{r}:O{r}')
            v = vals[0] if vals else []
            v = v + [''] * (8 - len(v))
            summary = {
                'start': _num(v[1]), 'revenue_cash': _num(v[2]),
                'other_income': _num(v[3]), 'expenses': _num(v[4]),
                'expected_end': _num(v[5]), 'actual_end': _num(v[6]),
                'status': v[7] or '',
            }
            break
    if summary is None:
        return None

    entries = []
    for row in _fetch_grid(spreadsheet_id, kassa_sheet, 'A6:F1000'):
        if row and row[0] == target:
            row = row + [''] * (6 - len(row))
            entries.append({
                'admin': row[1], 'category': row[2], 'type': row[3],
                'amount': _num(row[4]), 'comment': row[5],
            })

    summary['entries'] = entries
    summary['date'] = date
    return summary
