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

Весь месячный лист читается ОДНИМ запросом (A1:AZ25) — из этой сетки достаём
день/неделю/месяц/период. Так отчёт укладывается в 1-2 запроса к API вместо
десятков и не упирается в квоту Google (60 чтений в минуту).
"""
from google.oauth2 import service_account
from googleapiclient.discovery import build
import google_auth_httplib2
import httplib2
import os
import json
import time
import threading
import datetime as dt

HTTP_TIMEOUT_SECONDS = 8  # держим короче, чтобы даже с повтором укладываться в общий бюджет времени на отчёт

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

GRID_RANGE = 'A1:AZ25'   # весь месячный лист одним чтением
MONTH_TOTAL_COL = 52     # столбец AZ — готовые итоги за месяц

# httplib2.Http не потокобезопасен: два одновременных запроса через один объект
# ломают друг другу соединение (зависания, битые ответы). Отчёты запускаются в
# отдельных потоках (asyncio.to_thread), поэтому держим свой service на каждый поток.
_local = threading.local()


def get_service():
    service = getattr(_local, 'service', None)
    if service:
        return service
    creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    if creds_json:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds_file = os.environ['GOOGLE_CREDENTIALS_FILE']
        creds = service_account.Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    authed_http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT_SECONDS))
    service = build('sheets', 'v4', http=authed_http, cache_discovery=False)
    _local.service = service
    return service


def _sheet_name_for(date):
    return f'{MONTHS_RU[date.month - 1]} {date.year}'


def _fetch_grid(spreadsheet_id, sheet_name, a1_range, _retries=1):
    service = get_service()
    last_err = None
    for attempt in range(_retries + 1):
        try:
            resp = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=f"'{sheet_name}'!{a1_range}"
            ).execute()
            return resp.get('values', [])
        except Exception as e:
            last_err = e
            if attempt < _retries:
                time.sleep(1)  # короткая пауза — почти все временные сбои Google/сети проходят сами
    raise last_err


def _month_grid(spreadsheet_id, sheet_name):
    return _fetch_grid(spreadsheet_id, sheet_name, GRID_RANGE)


def _cell(grid, row, col):
    """Значение ячейки из сетки (1-based row/col); '' если за пределами данных."""
    r = grid[row - 1] if len(grid) >= row else []
    return r[col - 1] if len(r) >= col else ''


def _col_vals(grid, col, rows):
    return {r: _cell(grid, r, col) for r in rows}


def _grid_day_col(grid, date):
    """1-based номер столбца, где в строке 4 стоит нужная дата, или None."""
    target = date.strftime('%d.%m.%Y')
    dates_row = grid[3] if len(grid) > 3 else []
    for i, v in enumerate(dates_row):
        if v == target:
            return i + 1
    return None


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


def _week_block_bounds(day_col):
    for start, end in DAY_BLOCK_BOUNDS:
        if start <= day_col <= end:
            return start, end
    return None


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
    grid = _month_grid(spreadsheet_id, sheet_name)
    col = _grid_day_col(grid, date)
    if col is None:
        return None
    rows_needed = SUMMARY_ROWS + [ROW_KASSA_START, ROW_KASSA_END, ROW_DISCREPANCY, ROW_ADMIN]
    vals = _col_vals(grid, col, rows_needed)
    return _summary_dict(vals, {
        'date': date, 'sheet_name': sheet_name, 'period': 'day',
        'kassa_start': _num(vals[ROW_KASSA_START]),
        'kassa_end': _num(vals[ROW_KASSA_END]),
        'discrepancy': _num(vals[ROW_DISCREPANCY]),
        'admin': vals[ROW_ADMIN] or '—',
    })


def get_week_report(spreadsheet_id, date):
    sheet_name = _sheet_name_for(date)
    grid = _month_grid(spreadsheet_id, sheet_name)
    day_col = _grid_day_col(grid, date)
    if day_col is None:
        return None
    bounds = _week_block_bounds(day_col)
    if not bounds:
        return None
    start, _ = bounds
    vals = _col_vals(grid, start + 7, SUMMARY_ROWS)  # 7 дневных столбцов, затем итог недели
    label = _cell(grid, 1, start)  # объединённая шапка блока "dd.mm - dd.mm"
    return _summary_dict(vals, {'date': date, 'sheet_name': sheet_name, 'period': 'week', 'label': label})


def get_month_report(spreadsheet_id, date):
    sheet_name = _sheet_name_for(date)
    grid = _month_grid(spreadsheet_id, sheet_name)
    vals = _col_vals(grid, MONTH_TOTAL_COL, SUMMARY_ROWS)
    return _summary_dict(vals, {'date': date, 'sheet_name': sheet_name, 'period': 'month'})


def get_range_report(spreadsheet_id, start_date, end_date):
    """Sums day-by-day across an arbitrary (possibly cross-month) range.
    Один запрос к API на каждый задетый месяц, а не на каждый день."""
    totals = {k: 0 for k in [
        'clients_total', 'clients_repeat', 'clients_new',
        'revenue_total', 'revenue_terminal', 'revenue_cash', 'revenue_transfer',
        'goods_count', 'goods_total', 'goods_cash', 'reviews_2gis', 'reviews_yandex']}
    row_by_key = {
        'clients_total': ROW_CLIENTS_TOTAL, 'clients_repeat': ROW_CLIENTS_REPEAT, 'clients_new': ROW_CLIENTS_NEW,
        'revenue_total': ROW_REVENUE_TOTAL, 'revenue_terminal': ROW_TERMINAL,
        'revenue_cash': ROW_CASH, 'revenue_transfer': ROW_TRANSFER,
        'goods_count': ROW_GOODS_COUNT, 'goods_total': ROW_GOODS_TOTAL, 'goods_cash': ROW_GOODS_CASH,
        'reviews_2gis': ROW_REVIEWS_2GIS, 'reviews_yandex': ROW_REVIEWS_YANDEX}
    wanted = {(start_date + dt.timedelta(days=i)).strftime('%d.%m.%Y')
              for i in range((end_date - start_date).days + 1)}
    found_any = False
    month = dt.date(start_date.year, start_date.month, 1)
    while month <= end_date:
        try:
            grid = _month_grid(spreadsheet_id, _sheet_name_for(month))
        except Exception:
            grid = []  # листа за этот месяц нет — просто пропускаем
        dates_row = grid[3] if len(grid) > 3 else []
        for i, v in enumerate(dates_row):
            if v in wanted:
                found_any = True
                col = i + 1
                for k, row in row_by_key.items():
                    totals[k] += _num(_cell(grid, row, col))
        month = (month + dt.timedelta(days=32)).replace(day=1)
    if not found_any:
        return None
    totals.update({'date': start_date, 'date_end': end_date, 'period': 'range'})
    return totals


def get_kassa_day_detail(spreadsheet_id, date):
    """Full picture for 'КАССА <Месяц> <Год>' on this date: the auto-summary row
    (H:O) plus any movements the admin logged (A:F) for the same date."""
    sheet_name = _sheet_name_for(date)
    kassa_sheet = f'КАССА {sheet_name}'
    target = date.strftime('%d.%m.%Y')

    try:
        rows = _fetch_grid(spreadsheet_id, kassa_sheet, 'H6:O36')
    except Exception:
        return None

    summary = None
    for row in rows:
        if row and row[0] == target:
            v = row + [''] * (8 - len(row))
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
