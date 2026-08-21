"""
Telegram bot "Гарик — Отчёты": shows owner-facing daily/weekly report summaries
pulled live from the Mens Cave Google Sheets (per filial), via inline buttons.

Runs in webhook mode (aiohttp), meant for Render.com — it binds to $PORT and expects
$RENDER_EXTERNAL_URL to be set automatically by Render for the public webhook URL.
An external cron ping (e.g. cron-job.org) hitting GET /health every ~10 min keeps the
free-tier instance from spinning down.
"""
import os
import asyncio
import logging
import datetime as dt
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.error import RetryAfter
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

import sheets

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('garik-bot')

BOT_TOKEN = os.environ['BOT_TOKEN']
WEBHOOK_SECRET = BOT_TOKEN.replace(':', '')  # ':' в пути ломает роутинг на Render — используем без него
ALLOWED_USER_IDS = {int(x) for x in os.environ.get('ALLOWED_USER_IDS', '').split(',') if x.strip()}
PORT = int(os.environ.get('PORT', '10000'))
EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL', '').rstrip('/')

FILIALS = {
    'murino': {'title': 'Мурино', 'sheet_id': os.environ.get('SHEET_ID_MURINO', '')},
    'bugry': {'title': 'Бугры', 'sheet_id': os.environ.get('SHEET_ID_BUGRY', '')},
}

WEEKDAYS_RU_LOWER = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']


def _is_allowed(user_id):
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


# постоянная клавиатура внизу экрана — видна всегда, это и есть меню
BTN_MURINO = f"Отчет по {FILIALS['murino']['title']}"
BTN_BUGRY = f"Отчет по {FILIALS['bugry']['title']}"
BTN_BOTH = 'Общий отчет за 2 филиала'

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_MURINO], [BTN_BUGRY], [BTN_BOTH]],
    resize_keyboard=True, is_persistent=True
)

BUTTON_TO_FILIAL_KEY = {BTN_MURINO: 'murino', BTN_BUGRY: 'bugry', BTN_BOTH: 'both'}


DAY_OFFSETS = {'today': 0, 'yesterday': 1, 'before_yesterday': 2}

# Render живёт по UTC: без явного пояса с 00:00 до 03:00 по Москве
# «сегодня» считалось бы вчерашним днём и отчёты ехали на день назад.
MSK = dt.timezone(dt.timedelta(hours=3))


def _today():
    return dt.datetime.now(MSK).date()

COMBINED_TITLE = 'Мурино + Бугры'
SUM_KEYS = ['clients_total', 'clients_repeat', 'clients_new',
            'revenue_total', 'revenue_terminal', 'revenue_cash', 'revenue_transfer',
            'goods_count', 'goods_total', 'goods_cash', 'reviews_2gis', 'reviews_yandex']


def _merge_reports(a, b):
    if a is None and b is None:
        return None
    a = a or {}
    b = b or {}
    merged = {k: (a.get(k, 0) or 0) + (b.get(k, 0) or 0) for k in SUM_KEYS}
    # источники лежат вложенным словарём — складываем отдельно, по каждому каналу
    sa, sb = a.get('sources') or {}, b.get('sources') or {}
    merged['sources'] = {k: (sa.get(k, 0) or 0) + (sb.get(k, 0) or 0)
                         for k in sheets.SOURCE_KEYS if k in sa or k in sb}
    return merged


def _day_button_label(date):
    """«20.08 (чт)» — так сразу видно, какой именно день откроется."""
    return f'{date.strftime("%d.%m")} ({WEEKDAYS_RU_LOWER[date.weekday()]})'


def _period_menu(filial_key, today=None):
    today = today or _today()
    yesterday = today - dt.timedelta(days=1)
    before = today - dt.timedelta(days=2)
    buttons = [
        [InlineKeyboardButton('📅 Сегодня', callback_data=f'report:{filial_key}:today')],
        [InlineKeyboardButton(_day_button_label(yesterday), callback_data=f'report:{filial_key}:yesterday'),
         InlineKeyboardButton(_day_button_label(before), callback_data=f'report:{filial_key}:before_yesterday')],
        [InlineKeyboardButton('Прошлая неделя', callback_data=f'report:{filial_key}:last_week'),
         InlineKeyboardButton('Этот месяц', callback_data=f'report:{filial_key}:month')],
        [InlineKeyboardButton('✏️ Свой период', callback_data=f'range:{filial_key}')],
    ]
    return InlineKeyboardMarkup(buttons)


def _other_filial(key):
    return {'murino': 'bugry', 'bugry': 'murino'}.get(key)


def _report_footer(filial_key, period, today, start=None, end=None):
    buttons = []
    if filial_key != 'both':
        if period in DAY_OFFSETS:
            kassa_date = today - dt.timedelta(days=DAY_OFFSETS[period])
        elif period == 'range' and start and start == end:
            kassa_date = start   # выбран один конкретный день — касса за него же
        else:
            kassa_date = today
        if period == 'range' and start and end:
            back = f'range:{start.strftime("%Y%m%d")}:{end.strftime("%Y%m%d")}'
        else:
            back = period
        buttons.append([InlineKeyboardButton(
            '💰 Что по кассе за этот день?',
            callback_data=f'kassa:{filial_key}:{kassa_date.strftime("%Y%m%d")}:{back}'
        )])
        sheet_id = FILIALS[filial_key]['sheet_id']
        if sheet_id:
            buttons.append([InlineKeyboardButton(
                '📊 Перейти в таблицу', url=f'https://docs.google.com/spreadsheets/d/{sheet_id}/edit'
            )])
        buttons.append([InlineKeyboardButton(
            '📈 Статистика за месяц', callback_data=f'report:{filial_key}:month'
        )])
    else:
        # общий отчёт за 2 филиала: быстрые кнопки недели/месяца
        # (кнопку текущего периода не дублируем)
        if period != 'week':
            buttons.append([InlineKeyboardButton('📊 Статистика за неделю', callback_data='report:both:week')])
        if period != 'last_week':
            buttons.append([InlineKeyboardButton('📅 Статистика за прошлую неделю', callback_data='report:both:last_week')])
        if period != 'month':
            buttons.append([InlineKeyboardButton('📈 Статистика за месяц', callback_data='report:both:month')])

    other = _other_filial(filial_key)
    if other:
        if period == 'range' and start and end:
            cb = f'rrange:{other}:{start.strftime("%Y%m%d")}:{end.strftime("%Y%m%d")}'
        else:
            cb = f'report:{other}:{period}'
        buttons.append([InlineKeyboardButton(f"🔀 Другой филиал ({FILIALS[other]['title']})", callback_data=cb)])

    buttons.append([InlineKeyboardButton('« Другой период', callback_data=f'filial:{filial_key}')])
    return InlineKeyboardMarkup(buttons)


def _fmt_money(n):
    return f'{n:,.0f}'.replace(',', ' ') + ' ₽'


def _goods_line(data):
    count = data.get('goods_count', 0)
    if not count:
        return '🛍 Товаров не продано :('
    return f'🛍 Товаров продано: {count}'


def _header(filial_title, second_line):
    return f"<b>Отчет Men's Cave: {filial_title}.</b>\n<b>{second_line}</b>"


# Источники трафика: значок и подпись для отчёта. Порядок и ключи — из sheets.py,
# там же порядок строк в таблице.
SOURCE_VIEW = {
    'src_yandex_maps': ('🔴', 'Яндекс.Карты'),
    'src_2gis': ('🟢', '2ГИС'),
    'src_street': ('🟠', 'С улицы'),
    'src_referral': ('🟡', 'Рекомендация'),
    'src_vk': ('🟣', 'VK группа'),
    'src_tg': ('🔵', 'ТГ группа'),
    'src_unknown': ('⭕', 'Неопределили'),
}


def _sources_lines(data):
    """Разбивка новых клиентов по источникам. Показываем только те, где есть хоть один
    человек — нули в отчёте только зашумляют. Если разбивки нет вовсе (старый лист
    или все нули), возвращаем пусто, и строка «Новых» остаётся как была."""
    sources = data.get('sources') or {}
    lines = []
    for key in sheets.SOURCE_KEYS:
        count = sources.get(key, 0)
        if not count:
            continue
        icon, title = SOURCE_VIEW[key]
        lines.append(f'{icon} {title} - {count}')
    return lines


def _format_body(data, period_word):
    sources = _sources_lines(data)
    new_line = f"• Новых: {data['clients_new']}"
    if sources:
        new_line += '. Из них:'
    return [
        f"👥 <b>Клиентов всего {period_word}: {data['clients_total']}</b>",
        f"• Повторных: {data['clients_repeat']}",
        new_line,
        *sources,
        '',
        f"💸 <b>Выручка {period_word}: {_fmt_money(data['revenue_total'])}</b>",
        f"• Прошло по терминалу: {_fmt_money(data['revenue_terminal'])}",
        f"• Залетело наличными: {_fmt_money(data['revenue_cash'])}",
        f"• Переводом: {_fmt_money(data['revenue_transfer'])}",
        '',
        f"🔴 <b>Отзывы на Яндексе {period_word}: {data['reviews_yandex']}</b>",
        f"🟢 <b>Отзывы на 2ГИС {period_word}: {data['reviews_2gis']}</b>",
        '',
        _goods_line(data),
    ]


def _format_day(filial_title, data, include_kassa=True):
    d = data['date']
    header = _header(filial_title, f"За {d.strftime('%d.%m.%Y')} ({WEEKDAYS_RU_LOWER[d.weekday()]})")
    lines = [header, ''] + _format_body(data, 'за день')
    if include_kassa:
        lines += [
            '',
            f"💵 <b>В кассе на конец смены: {_fmt_money(data['kassa_end'])}</b>",
            f"🧑‍💼 <b>Администратор на смене: {data['admin']}</b>",
        ]
    return '\n'.join(lines)


def _kassa_now_tail(data):
    v = data.get('kassa_now')
    if v is None:
        return []
    return ['', f"💵 <b>В кассе сейчас: {_fmt_money(v)}</b>"]


def _format_week(filial_title, data, title_word='За неделю'):
    label = data.get('label') or ''
    second = title_word + (f' ({label})' if label else '')
    header = _header(filial_title, second)
    lines = [header, ''] + _format_body(data, 'за неделю') + _kassa_now_tail(data)
    return '\n'.join(lines)


def _format_month(filial_title, data):
    header = _header(filial_title, f"За {data['sheet_name']}")
    lines = [header, ''] + _format_body(data, 'за месяц') + _kassa_now_tail(data)
    return '\n'.join(lines)


def _format_range(filial_title, data):
    second = f"За период {data['date'].strftime('%d.%m.%Y')}–{data['date_end'].strftime('%d.%m.%Y')}"
    header = _header(filial_title, second)
    lines = [header, ''] + _format_body(data, 'за период') + _kassa_now_tail(data)
    return '\n'.join(lines)


def _format_kassa_detail(filial_title, data):
    d = data['date']
    header = _header(filial_title, f"Касса на {d.strftime('%d.%m.%Y')} ({WEEKDAYS_RU_LOWER[d.weekday()]})")
    lines = [
        header, '',
        f"💰 <b>В кассе с утра: {_fmt_money(data['start'])}</b>",
        f"💵 Выручка наличными: {_fmt_money(data['revenue_cash'])}",
        f"➕ Другие приходы: {_fmt_money(data['other_income'])}",
        f"➖ Расходы: {_fmt_money(data['expenses'])}",
        '',
        f"💵 <b>Остаток в кассе: {_fmt_money(data['actual_end'])}</b>",
    ]
    status = data.get('status')
    if status:
        icon = '✅' if status == 'Сходится' else '⚠️'
        lines.append(f"<b>{icon} {status}</b>")
    entries = data.get('entries') or []
    if entries:
        lines.append('')
        lines.append('Движения за день:')
        for e in entries:
            sign = '+' if e['type'] == 'Приход' else '-'
            desc = e['comment'] or e['category']
            lines.append(f"{sign} {_fmt_money(e['amount'])}: {desc} ({e['admin']})")
    return '\n'.join(lines)


REPORT_TIMEOUT = 45  # секунд — с запасом на автоповтор внутри sheets.py при временных сбоях


async def _show_loading(query):
    try:
        await query.edit_message_text('⏳ Секунду, собираю данные…')
    except Exception:
        pass  # не критично, если не удалось — итоговый текст всё равно попробуем показать


async def _edit_quiet(query, *args, **kwargs):
    """Правка сообщения, где сбой не критичен (повторное нажатие той же кнопки
    даёт от Telegram "message is not modified" — это не ошибка)."""
    try:
        await query.edit_message_text(*args, **kwargs)
    except Exception:
        log.warning('edit_message_text failed (скорее всего, текст не изменился)')


async def _finish_report(query, text, reply_markup):
    try:
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
        return
    except RetryAfter as e:
        # Flood control: Telegram просит подождать. Ждём и повторяем, иначе отчёт молча пропадёт.
        log.warning('flood control on edit, retrying in %s s', e.retry_after)
        await asyncio.sleep(e.retry_after + 1)
        try:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
            return
        except Exception:
            log.exception('edit retry after flood control failed')
    except Exception:
        log.exception('failed to edit final report message, sending as a new message instead')
    try:
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
    except Exception:
        log.exception('fallback send also failed')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data.pop('awaiting_range_filial', None)
    if not _is_allowed(user_id):
        await update.message.reply_text(
            f'Доступ закрыт. Ваш Telegram ID: {user_id}\nПередайте его владельцу, чтобы вас добавили.'
        )
        return
    await update.message.reply_text('Выберите филиал кнопкой внизу 👇', reply_markup=MAIN_KEYBOARD)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not _is_allowed(user_id):
        await query.answer('Доступ закрыт', show_alert=True)
        return
    try:
        await query.answer()
    except Exception:
        # Callback пришёл с опозданием (например, инстанс Render просыпался) —
        # Telegram отвечает "query is too old". Отчёт всё равно можно показать.
        log.warning('callback answer failed (stale query) — continuing anyway')

    data = query.data
    if data == 'menu':
        await _edit_quiet(query, 'Выберите филиал кнопкой внизу 👇')
        return

    if data.startswith('filial:'):
        filial_key = data.split(':')[1]
        context.user_data.pop('awaiting_range_filial', None)
        title = COMBINED_TITLE if filial_key == 'both' else FILIALS[filial_key]['title']
        await _edit_quiet(query, f'{title} — выберите период:', reply_markup=_period_menu(filial_key))
        return

    if data.startswith('range:'):
        filial_key = data.split(':')[1]
        context.user_data['awaiting_range_filial'] = filial_key
        cancel_menu = InlineKeyboardMarkup([[InlineKeyboardButton('« Отмена', callback_data=f'filial:{filial_key}')]])
        await _edit_quiet(
            query,
            'Напишите одним сообщением:\n\n'
            '• <b>один день</b> — 20.08.2026\n'
            '   (можно короче: 20.08 — возьму текущий год)\n\n'
            '• <b>период</b> — 01.08.2026-10.08.2026',
            parse_mode='HTML',
            reply_markup=cancel_menu
        )
        return

    if data.startswith('report:'):
        if context.user_data.get('report_busy'):
            return  # двойной тап — предыдущий отчёт ещё собирается, не дублируем
        context.user_data['report_busy'] = True
        try:
            _, filial_key, period = data.split(':')
            today = _today()
            await _show_loading(query)
            try:
                text = await asyncio.wait_for(asyncio.to_thread(_report_for, filial_key, period, today), REPORT_TIMEOUT)
            except asyncio.TimeoutError:
                log.error('report generation timed out')
                text = None
            except Exception:
                log.exception('report generation failed')
                text = None

            if text is None:
                text = 'Не удалось получить данные (таблица долго не отвечает). Попробуйте еще раз через минуту.'
            await _finish_report(query, text, _report_footer(filial_key, period, today))
        finally:
            context.user_data.pop('report_busy', None)
        return

    if data.startswith('rrange:'):
        if context.user_data.get('report_busy'):
            return  # двойной тап — предыдущий отчёт ещё собирается, не дублируем
        context.user_data['report_busy'] = True
        try:
            _, filial_key, start_s, end_s = data.split(':')
            start = dt.datetime.strptime(start_s, '%Y%m%d').date()
            end = dt.datetime.strptime(end_s, '%Y%m%d').date()
            await _show_loading(query)
            try:
                text = await asyncio.wait_for(asyncio.to_thread(_range_report_for, filial_key, start, end), REPORT_TIMEOUT)
            except asyncio.TimeoutError:
                log.error('range report generation timed out')
                text = None
            except Exception:
                log.exception('range report failed')
                text = None
            if text is None:
                text = 'Не удалось получить данные (таблица долго не отвечает). Попробуйте еще раз через минуту.'
            today = _today()
            await _finish_report(query, text, _report_footer(filial_key, 'range', today, start, end))
        finally:
            context.user_data.pop('report_busy', None)
        return

    if data.startswith('kassa:'):
        if context.user_data.get('report_busy'):
            return  # двойной тап — предыдущий отчёт ещё собирается, не дублируем
        context.user_data['report_busy'] = True
        try:
            parts = data.split(':')
            filial_key, date_s = parts[1], parts[2]
            back_period = parts[3] if len(parts) > 3 else 'today'
            kassa_date = dt.datetime.strptime(date_s, '%Y%m%d').date()
            filial = FILIALS.get(filial_key)
            await _show_loading(query)
            try:
                if filial and filial['sheet_id']:
                    detail = await asyncio.wait_for(
                        asyncio.to_thread(sheets.get_kassa_day_detail, filial['sheet_id'], kassa_date), REPORT_TIMEOUT
                    )
                else:
                    detail = None
                text = _format_kassa_detail(filial['title'], detail) if detail else None
            except asyncio.TimeoutError:
                log.error('kassa detail timed out')
                text = None
            except Exception:
                log.exception('kassa detail failed')
                text = None
            if text is None:
                text = f"Не нашел данные по кассе за {kassa_date.strftime('%d.%m.%Y')} (возможно, день еще не заполнен, либо таблица долго не отвечает)."

            if back_period == 'range' and len(parts) > 5:
                back_cb = f'rrange:{filial_key}:{parts[4]}:{parts[5]}'
            else:
                back_cb = f'report:{filial_key}:{back_period}'
            back = InlineKeyboardMarkup([
                [InlineKeyboardButton('← Назад к отчету', callback_data=back_cb)],
                [InlineKeyboardButton('« Другой период', callback_data=f'filial:{filial_key}')],
            ])
            await _finish_report(query, text, back)
        finally:
            context.user_data.pop('report_busy', None)
        return


def _today_kassa_end(sheet_id, today):
    if not sheet_id:
        return None
    try:
        d = sheets.get_day_report(sheet_id, today)
        return d['kassa_end'] if d else None
    except Exception:
        return None


def _safe(fn, *args):
    """Для общего отчёта за 2 филиала: сбой одной таблицы не должен ронять весь
    отчёт — показываем данные хотя бы по второй."""
    try:
        return fn(*args)
    except Exception:
        log.exception('sheets call failed: %s', getattr(fn, '__name__', fn))
        return None


def _sum_optional(a, b):
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def _combined_report(period, today):
    m_id, b_id = FILIALS['murino']['sheet_id'], FILIALS['bugry']['sheet_id']
    if period in DAY_OFFSETS:
        d = today - dt.timedelta(days=DAY_OFFSETS[period])
        merged = _merge_reports(
            _safe(sheets.get_day_report, m_id, d) if m_id else None,
            _safe(sheets.get_day_report, b_id, d) if b_id else None)
        if merged is None:
            return None
        merged['date'] = d
        return _format_day(COMBINED_TITLE, merged, include_kassa=False)
    if period in ('week', 'last_week'):
        d = today - dt.timedelta(days=7) if period == 'last_week' else today
        a = _safe(sheets.get_week_report, m_id, d) if m_id else None
        b = _safe(sheets.get_week_report, b_id, d) if b_id else None
        merged = _merge_reports(a, b)
        if merged is None:
            return None
        merged['label'] = (a or {}).get('label') or (b or {}).get('label') or ''
        merged['kassa_now'] = _sum_optional(_today_kassa_end(m_id, today), _today_kassa_end(b_id, today))
        title_word = 'За прошлую неделю' if period == 'last_week' else 'За неделю'
        return _format_week(COMBINED_TITLE, merged, title_word)
    if period == 'month':
        a = _safe(sheets.get_month_report, m_id, today) if m_id else None
        b = _safe(sheets.get_month_report, b_id, today) if b_id else None
        merged = _merge_reports(a, b)
        if merged is None:
            return None
        merged['sheet_name'] = (a or {}).get('sheet_name') or (b or {}).get('sheet_name') or ''
        merged['kassa_now'] = _sum_optional(_today_kassa_end(m_id, today), _today_kassa_end(b_id, today))
        return _format_month(COMBINED_TITLE, merged)
    return None


def _report_for(filial_key, period, today):
    if filial_key == 'both':
        return _combined_report(period, today)

    filial = FILIALS.get(filial_key)
    if not filial or not filial['sheet_id']:
        return None

    if period in DAY_OFFSETS:
        d = today - dt.timedelta(days=DAY_OFFSETS[period])
        report = sheets.get_day_report(filial['sheet_id'], d)
        return _format_day(filial['title'], report) if report else None

    if period in ('week', 'last_week'):
        d = today - dt.timedelta(days=7) if period == 'last_week' else today
        report = sheets.get_week_report(filial['sheet_id'], d)
        if report:
            report['kassa_now'] = _today_kassa_end(filial['sheet_id'], today)
        title_word = 'За прошлую неделю' if period == 'last_week' else 'За неделю'
        return _format_week(filial['title'], report, title_word) if report else None

    if period == 'month':
        report = sheets.get_month_report(filial['sheet_id'], today)
        if report:
            report['kassa_now'] = _today_kassa_end(filial['sheet_id'], today)
        return _format_month(filial['title'], report) if report else None

    return None


def _range_report_for(filial_key, start, end):
    # Указали один день (в поле периода вписали одну дату) — показываем полноценный
    # отчёт за день, с кассой и админом, а не «период из одного дня».
    if start == end:
        return _report_for(filial_key, 'today', start)

    today = _today()
    if filial_key == 'both':
        m_id, b_id = FILIALS['murino']['sheet_id'], FILIALS['bugry']['sheet_id']
        a = _safe(sheets.get_range_report, m_id, start, end) if m_id else None
        b = _safe(sheets.get_range_report, b_id, start, end) if b_id else None
        merged = _merge_reports(a, b)
        if merged is None:
            return None
        merged['date'], merged['date_end'] = start, end
        merged['kassa_now'] = _sum_optional(_today_kassa_end(m_id, today), _today_kassa_end(b_id, today))
        return _format_range(COMBINED_TITLE, merged)

    filial = FILIALS.get(filial_key)
    if not filial or not filial['sheet_id']:
        return None
    report = sheets.get_range_report(filial['sheet_id'], start, end)
    if report:
        report['kassa_now'] = _today_kassa_end(filial['sheet_id'], today)
    return _format_range(filial['title'], report) if report else None


def _parse_one_date(text, today):
    """«20.08.2026» или «20.08» (тогда берём текущий год). None, если не дата."""
    text = text.strip()
    for fmt in ('%d.%m.%Y', '%d.%m.%y'):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        d = dt.datetime.strptime(text, '%d.%m').date()
        return d.replace(year=today.year)
    except ValueError:
        return None


def _parse_period(text, today):
    """Вернёт (начало, конец) или None. Одна дата = день, две через дефис = период."""
    text = (text or '').replace(' ', '').replace('—', '-').replace('–', '-')
    parts = [p for p in text.split('-') if p]
    if len(parts) == 1:
        d = _parse_one_date(parts[0], today)
        return (d, d) if d else None
    if len(parts) != 2:
        return None
    start, end = _parse_one_date(parts[0], today), _parse_one_date(parts[1], today)
    if not start or not end:
        return None
    return (end, start) if start > end else (start, end)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return

    text_raw = (update.message.text or '').strip()
    if text_raw in BUTTON_TO_FILIAL_KEY:
        context.user_data.pop('awaiting_range_filial', None)
        filial_key = BUTTON_TO_FILIAL_KEY[text_raw]
        title = COMBINED_TITLE if filial_key == 'both' else FILIALS[filial_key]['title']
        await update.message.reply_text(f'{title} — выберите период:', reply_markup=_period_menu(filial_key))
        return

    filial_key = context.user_data.get('awaiting_range_filial')
    if not filial_key:
        return  # не ждём ввода периода — игнорируем произвольные сообщения

    parsed = _parse_period(update.message.text, _today())
    if parsed is None:
        await update.message.reply_text(
            'Не понял. Можно двумя способами:\n'
            '• один день — 20.08.2026 (или просто 20.08)\n'
            '• период — 01.08.2026-10.08.2026'
        )
        return
    start, end = parsed
    if (end - start).days > 92:
        await update.message.reply_text('Слишком большой период (максимум ~3 месяца). Введите период короче.')
        return

    if context.user_data.get('report_busy'):
        await update.message.reply_text('⏳ Уже собираю предыдущий отчет — подождите чуть-чуть…')
        return
    context.user_data.pop('awaiting_range_filial', None)
    context.user_data['report_busy'] = True
    try:
        loading_msg = await update.message.reply_text('⏳ Секунду, собираю данные…')
        try:
            text_out = await asyncio.wait_for(asyncio.to_thread(_range_report_for, filial_key, start, end), REPORT_TIMEOUT)
        except asyncio.TimeoutError:
            log.error('range report generation timed out')
            text_out = None
        except Exception:
            log.exception('range report failed')
            text_out = None
        if text_out is None:
            text_out = 'Не удалось получить данные (таблица долго не отвечает). Попробуйте еще раз через минуту.'
        try:
            await loading_msg.edit_text(
                text_out, parse_mode='HTML', reply_markup=_report_footer(filial_key, 'range', _today(), start, end)
            )
        except Exception:
            log.exception('failed to edit final range report message, sending as a new message instead')
            await update.message.reply_text(
                text_out, parse_mode='HTML', reply_markup=_report_footer(filial_key, 'range', _today(), start, end)
            )
    finally:
        context.user_data.pop('report_busy', None)


async def health(request):
    return web.Response(text='OK')


async def set_commands(app: Application):
    await app.bot.set_my_commands([
        ('menu', 'Открыть меню отчетов'),
        ('start', 'Открыть меню отчетов'),
    ])


async def on_startup(app: Application):
    await set_commands(app)
    if EXTERNAL_URL:
        await app.bot.set_webhook(url=f'{EXTERNAL_URL}/webhook/{WEBHOOK_SECRET}')
        log.info('Webhook set to %s/webhook/...', EXTERNAL_URL)
    else:
        log.warning('RENDER_EXTERNAL_URL not set — webhook not configured (local run?)')


def build_application():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('menu', start))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return application


async def main():
    application = build_application()

    if os.environ.get('USE_POLLING'):
        # для локального теста: без публичного URL, без webhook
        log.info('Running in POLLING mode (local test)')
        await application.initialize()
        await application.start()
        await set_commands(application)
        await application.updater.start_polling()
        await asyncio.Event().wait()
        return

    await application.initialize()
    await application.start()
    await on_startup(application)

    async def process_update_safely(update):
        try:
            await application.process_update(update)
        except Exception:
            # Одно упавшее обновление (например Flood control от Telegram при
            # повторной доставке) не должно ронять весь процесс на Render.
            log.exception('update processing failed')

    async def webhook_handler(request):
        try:
            data = await request.json()
            update = Update.de_json(data, application.bot)
        except Exception:
            log.exception('bad webhook payload — ignoring')
            return web.Response()
        # Отвечаем Telegram сразу, а обрабатываем в фоне — иначе долгий отчёт
        # (неделя/месяц/оба филиала, несколько запросов к таблицам подряд)
        # не укладывается в таймаут вебхука, и Telegram шлёт то же обновление
        # повторно — получаются дублирующиеся правки одного сообщения и
        # ошибка "Flood control exceeded".
        asyncio.create_task(process_update_safely(update))
        return web.Response()

    web_app = web.Application()
    web_app.router.add_post(f'/webhook/{WEBHOOK_SECRET}', webhook_handler)
    web_app.router.add_get('/health', health)
    web_app.router.add_get('/', health)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    log.info('Listening on port %s', PORT)

    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
