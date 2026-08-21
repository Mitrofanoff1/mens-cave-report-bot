"""
Reads daily/weekly/monthly/custom-range report data out of a Mens Cave filial spreadsheet.

СТРОКИ ИЩУТСЯ ПО ПОДПИСЯМ В СТОЛБЦЕ A, а не по жёстким номерам. Владелец
периодически переставляет строки в шаблоне (в августе 2026 так добавились
7 строк источников трафика, и всё ниже уехало на 3-10 строк). При жёстких
номерах бот молча показывал бы чужие цифры; поиск по подписи переживает
перестановку и позволяет одному коду читать и старые листы, и новые.
Столбцы листа КАССА так же ищутся по заголовкам в строке 5.

  row4  = date per day-column (это единственная опора на номер строки:
          строка дат стоит над всеми блоками и не двигается)
  DAY_BLOCKS: B:H, J:P, R:X, Z:AF, AH:AN, AP:AV — 7 day-columns each, followed by
  a totals column (I, Q, Y, AG, AO, AW) with a week SUM already computed, and row1 of
  each block holds a merged "dd.mm - dd.mm" label. AZ holds the whole-month totals.

Весь месячный лист читается ОДНИМ запросом — из этой сетки достаём
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

DATES_ROW = 4            # строка с датами — единственная опора на номер строки

# Подписи из столбца A. Первый вариант — как сейчас в шаблоне, остальные —
# запасные написания (в таблице живёт опечатка «Налчиные», её тоже ловим).
ROW_LABELS = {
    'clients_total': ['количество клиентов за день'],
    'clients_repeat': ['повторных клиентов'],
    'clients_new': ['новых клиентов'],
    'revenue_total': ['выручка с услуг за день'],
    'revenue_terminal': ['оплата по терминалу'],
    'revenue_cash': ['наличными'],
    # «Переводом» переименовали в «Оплата по QR-коду» 21.08.2026 —
    # старое написание оставляем, чтобы читались листы за прошлые месяцы
    'revenue_qr': ['оплата по qr-коду', 'переводом'],
    'goods_count': ['количество проданных товаров'],
    'goods_total': ['выручка с проданных товаров'],
    'goods_cash': ['из них наличными'],
    'reviews_2gis': ['получено отзывов на 2гис'],
    'reviews_yandex': ['получено отзывов на яндекс.картах', 'получено отзывов на яндекс картах'],
    'kassa_start': ['налчиные в кассе: на начало смены', 'наличные в кассе: на начало смены'],
    'kassa_end': ['налчиные в кассе: на конец смены', 'наличные в кассе: на конец смены'],
    'admin': ['администратор на смене'],
}

# Источники трафика новых клиентов. НЕОБЯЗАТЕЛЬНЫЕ строки: появились в августе 2026,
# в листах за прошлые месяцы их нет — если не нашлись, отчёт просто строится без них.
# Порядок здесь = порядок строк в таблице и порядок вывода в отчёте.
SOURCE_LABELS = [
    ('src_yandex_maps', ['яндекс.карты', 'яндекс карты']),
    ('src_2gis', ['2гис']),
    ('src_street', ['с улицы']),
    ('src_referral', ['рекомендация']),
    ('src_vk', ['vk группа']),
    ('src_tg', ['тг группа']),
    ('src_unknown', ['неопределенно', 'другое / не указан']),
]
SOURCE_KEYS = [key for key, _ in SOURCE_LABELS]

# Что показываем в сводке за день/неделю/месяц/период.
SUMMARY_KEYS = ['clients_total', 'clients_repeat', 'clients_new',
                'revenue_total', 'revenue_terminal', 'revenue_cash', 'revenue_qr',
                'goods_count', 'goods_total', 'goods_cash',
                'reviews_2gis', 'reviews_yandex']

# Заголовки листа КАССА (строка 5) — по ним ищем столбцы сводки.
KASSA_HEADER_ROW = 5
KASSA_COLS = {
    'date': ['дата'],
    'start': ['касса на начало смены'],
    'revenue_cash': ['выручка наличными за день'],
    'other_income': ['другие приходы (кроме выручки)'],
    'expenses': ['расходы'],
    'expected_end': ['ожидаемый конец в кассе'],
    'actual_end': ['факт на конец смены'],
    'status': ['статус'],
}

GRID_RANGE = 'A1:AZ40'   # весь месячный лист одним чтением, с запасом на новые строки
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


def _norm(text):
    """Подпись к сравнимому виду: без регистра, ё=е, без маркеров списка и лишних пробелов."""
    s = str(text or '').replace('\xa0', ' ').lower().replace('ё', 'е')
    s = s.lstrip(' —–-•*').strip()
    return ' '.join(s.split())


def _row_map(grid):
    """Где какая строка на этом листе: {ключ: номер строки}. Ищем по подписи в столбце A.
    Строки источников необязательны — в старых листах их нет."""
    wanted = list(ROW_LABELS.items()) + SOURCE_LABELS
    found = {}
    for i, row in enumerate(grid, 1):
        label = _norm(row[0] if row else '')
        if not label:
            continue
        for key, variants in wanted:
            if key not in found and label in variants:
                found[key] = i
    missing = [k for k in ROW_LABELS if k not in found]
    if missing:
        raise ValueError('в листе не нашлись строки: ' + ', '.join(missing))
    return found


def _col_vals(grid, col, rows_by_key):
    """Значения одного столбца по ключам: {ключ: значение ячейки}."""
    return {key: _cell(grid, row, col) for key, row in rows_by_key.items()}


def _grid_day_col(grid, date):
    """1-based номер столбца, где в строке дат стоит нужная дата, или None."""
    target = date.strftime('%d.%m.%Y')
    dates_row = grid[DATES_ROW - 1] if len(grid) >= DATES_ROW else []
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
    d = {key: _num(vals.get(key)) for key in SUMMARY_KEYS}
    # источников может не быть (старые листы) — тогда просто пустая разбивка
    d['sources'] = {key: _num(vals[key]) for key in SOURCE_KEYS if key in vals}
    if extra:
        d.update(extra)
    return d


def get_day_report(spreadsheet_id, date):
    sheet_name = _sheet_name_for(date)
    grid = _month_grid(spreadsheet_id, sheet_name)
    col = _grid_day_col(grid, date)
    if col is None:
        return None
    rows = _row_map(grid)
    vals = _col_vals(grid, col, rows)
    return _summary_dict(vals, {
        'date': date, 'sheet_name': sheet_name, 'period': 'day',
        'kassa_start': _num(vals['kassa_start']),
        'kassa_end': _num(vals['kassa_end']),
        'admin': vals['admin'] or '—',
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
    summary_rows = {k: r for k, r in _row_map(grid).items() if k in SUMMARY_KEYS or k in SOURCE_KEYS}
    vals = _col_vals(grid, start + 7, summary_rows)  # 7 дневных столбцов, затем итог недели
    label = _cell(grid, 1, start)  # объединённая шапка блока "dd.mm - dd.mm"
    return _summary_dict(vals, {'date': date, 'sheet_name': sheet_name, 'period': 'week', 'label': label})


def get_month_report(spreadsheet_id, date):
    sheet_name = _sheet_name_for(date)
    grid = _month_grid(spreadsheet_id, sheet_name)
    summary_rows = {k: r for k, r in _row_map(grid).items() if k in SUMMARY_KEYS or k in SOURCE_KEYS}
    vals = _col_vals(grid, MONTH_TOTAL_COL, summary_rows)
    return _summary_dict(vals, {'date': date, 'sheet_name': sheet_name, 'period': 'month'})


def get_range_report(spreadsheet_id, start_date, end_date):
    """Sums day-by-day across an arbitrary (possibly cross-month) range.
    Один запрос к API на каждый задетый месяц, а не на каждый день."""
    totals = {k: 0 for k in SUMMARY_KEYS + SOURCE_KEYS}
    wanted = {(start_date + dt.timedelta(days=i)).strftime('%d.%m.%Y')
              for i in range((end_date - start_date).days + 1)}
    found_any = False
    month = dt.date(start_date.year, start_date.month, 1)
    while month <= end_date:
        try:
            grid = _month_grid(spreadsheet_id, _sheet_name_for(month))
            # у каждого месяца своя разметка: старые листы и новые лежат по-разному
            row_by_key = {k: r for k, r in _row_map(grid).items() if k in SUMMARY_KEYS or k in SOURCE_KEYS}
        except Exception:
            grid, row_by_key = [], {}  # листа за этот месяц нет — просто пропускаем
        dates_row = grid[DATES_ROW - 1] if len(grid) >= DATES_ROW else []
        for i, v in enumerate(dates_row):
            if v in wanted:
                found_any = True
                col = i + 1
                for k, row in row_by_key.items():
                    totals[k] += _num(_cell(grid, row, col))
        month = (month + dt.timedelta(days=32)).replace(day=1)
    if not found_any:
        return None
    sources = {k: totals.pop(k) for k in SOURCE_KEYS}
    totals.update({'sources': sources, 'date': start_date,
                   'date_end': end_date, 'period': 'range'})
    return totals


def get_kassa_day_detail(spreadsheet_id, date):
    """Full picture for 'КАССА <Месяц> <Год>' on this date: the auto-summary row
    (H:O) plus any movements the admin logged (A:F) for the same date."""
    sheet_name = _sheet_name_for(date)
    kassa_sheet = f'КАССА {sheet_name}'
    target = date.strftime('%d.%m.%Y')

    try:
        # с заголовками: столбцы сводки ищем по подписям, а не по буквам —
        # в августе 2026 перед «Статусом» вклинился «Расхождение за день»
        rows = _fetch_grid(spreadsheet_id, kassa_sheet, f'H{KASSA_HEADER_ROW}:R36')
    except Exception:
        return None
    if not rows:
        return None

    header = [_norm(c) for c in rows[0]]
    idx = {}
    for key, variants in KASSA_COLS.items():
        for i, h in enumerate(header):
            if h in variants:
                idx[key] = i
                break
    if 'date' not in idx:
        return None

    def pick(row, key, default=''):
        i = idx.get(key)
        return row[i] if i is not None and len(row) > i else default

    summary = None
    for row in rows[1:]:
        if row and pick(row, 'date') == target:
            summary = {
                'start': _num(pick(row, 'start')),
                'revenue_cash': _num(pick(row, 'revenue_cash')),
                'other_income': _num(pick(row, 'other_income')),
                'expenses': _num(pick(row, 'expenses')),
                'expected_end': _num(pick(row, 'expected_end')),
                'actual_end': _num(pick(row, 'actual_end')),
                'status': pick(row, 'status') or '',
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
