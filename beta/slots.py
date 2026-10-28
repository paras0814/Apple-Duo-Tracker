"""Choose the earliest current, unrestricted Apple pickup window."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date, datetime, timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PREFIX = 'checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.'
TIME_ZONE = 'America/New_York'


class SlotError(ValueError):
    """Fixed diagnostic messages, without opaque server slot identifiers."""


class PickupSlotFields(dict):
    def __repr__(self):
        return 'PickupSlotFields(<private server fields redacted>)'
    __str__ = __repr__


@dataclass(frozen=True)
class PickupSlot:
    store_id: str
    date: str
    start: str
    end: str
    time_zone: str
    _day: str = field(repr=False)
    _slot_id: str = field(repr=False)
    _sign_key: str = field(repr=False)
    _value: str = field(repr=False)
    _type: str = field(repr=False)
    _start_at: datetime = field(repr=False)
    _end_at: datetime = field(repr=False)

    def safe_facts(self):
        return {'store_id': self.store_id, 'date': self.date, 'start': self.start,
                'end': self.end, 'time_zone': self.time_zone}

    def form_fields(self):
        fields = {'date': self.date, 'dayRadio': self._day, 'timeSlotId': self._slot_id,
                  'signKey': self._sign_key, 'timeZone': self.time_zone,
                  'timeSlotValue': self._value, 'startTime': self.start, 'endTime': self.end,
                  'displayStartTime': '', 'displayEndTime': '',
                  'isRecommended': 'false', 'isRestricted': ''}
        if self._type:
            fields['timeSlotType'] = self._type
        return PickupSlotFields({PREFIX + key: value for key, value in fields.items()})

    to_form_fields = form_fields


def _ny_zone():
    try:
        return ZoneInfo(TIME_ZONE)
    except ZoneInfoNotFoundError:
        # The private API installer verifies and extracts the official tzdata
        # wheel. Direct TZif loading avoids global Python import-path changes.
        libraries = Path(__file__).resolve().parents[1] / '.runtime/api-libs'
        resource = libraries / 'tzdata/zoneinfo/America/New_York'
        try:
            if not resource.resolve().is_relative_to(libraries.resolve()) or not (libraries / '.pins.json').is_file():
                raise ValueError()
            with resource.open('rb') as handle:
                return ZoneInfo.from_file(handle, key=TIME_ZONE)
        except (OSError, ValueError):
            raise SlotError('IANA time zone data is missing. Run Setup-API.cmd before pickup selection.') from None


def _opaque(value, *, optional=False):
    if optional and value in (None, ''):
        return ''
    if (not isinstance(value, str) or not 1 <= len(value) <= 8192
            or value.strip() != value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
            or value.lower() in {'unknown', 'undefined', 'null', 'none', 'missing'}):
        raise SlotError('The current pickup window has missing or invalid server fields.')
    return value


def _day(value):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]{1,2}', str(value)):
        raise SlotError('The pickup model has an invalid day mapping.')
    day = int(value)
    if not 1 <= day <= 31:
        raise SlotError('The pickup model has an invalid day mapping.')
    return str(day)


def _clock(value):
    if not isinstance(value, str):
        raise SlotError('The pickup window has an invalid start or end time.')
    match = re.fullmatch(r'(0?[1-9]|1[0-2]):([0-5][0-9])\s*(AM|PM)', value, re.I)
    if not match:
        raise SlotError('The pickup window has an invalid start or end time.')
    hour, minute = int(match[1]), int(match[2])
    meridiem = match[3].upper()
    hour24 = hour % 12 + (12 if meridiem == 'PM' else 0)
    return hour24, minute, value


def _available(record):
    restricted = record.get('isRestricted')
    if restricted is not None and restricted is not False:
        return False
    if any(record.get(key) is not False for key in ('disabled', 'unavailable') if key in record):
        return False
    return not any(record.get(key) is not True for key in ('enabled', 'available', 'isAvailable') if key in record)


def _instant(day, hour, minute, zone):
    # Reject DST gaps and ambiguous repeated wall times instead of selecting an
    # unproven instant. Ordinary daytime pickup windows are unaffected.
    naive = datetime(day.year, day.month, day.day, hour, minute)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise SlotError('The pickup window has an ambiguous or nonexistent local time.')
    utc = first.astimezone(timezone.utc)
    if utc.astimezone(zone).replace(tzinfo=None) != naive:
        raise SlotError('The pickup window has an invalid local time.')
    return utc


def _time_slot_node(model):
    try:
        if 'body' in model:
            model = model['body']
        return model['checkout']['fulfillment']['pickupTab']['pickup']['timeSlot']
    except (KeyError, TypeError):
        raise SlotError('The current Apple pickup model is missing.') from None


def choose_earliest_slot(model, expected_store_id, now=None) -> PickupSlot:
    """Sort all valid current windows by actual local date and start time.

    Opaque IDs/signatures are taken solely from the supplied current model. This
    validates their presence and format, not Apple's cryptographic signature.
    Malformed, unavailable, restricted, stale or unsupported windows are skipped;
    missing store/date structure fails closed before choosing anything.
    """
    if not isinstance(model, dict) or not isinstance(expected_store_id, str) or not re.fullmatch(r'R[0-9]{3}', expected_store_id):
        raise SlotError('A current model and valid expected pickup store are required.')
    slot_node = _time_slot_node(model)
    try:
        data = slot_node['dateTimeSlots']['d']
        if slot_node['d']['storeId'] != expected_store_id or data['storeSelected'] != expected_store_id:
            raise SlotError('Apple pickup store does not match the selected store.')
        dates, groups = data['pickUpDates'], data['timeSlotWindows']
        if not isinstance(dates, list) or not isinstance(groups, list) or not dates or not groups:
            raise SlotError('The current pickup model has no dated windows.')
    except (KeyError, TypeError):
        raise SlotError('The current pickup store or dated-window model is incomplete.') from None
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise SlotError('Pickup selection requires an aware current time.')
    now = now.astimezone(timezone.utc)
    zone = _ny_zone()
    windows = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        for day_key, records in group.items():
            try:
                normalized = _day(day_key)
            except SlotError:
                continue
            if isinstance(records, list):
                windows.setdefault(normalized, []).extend(records)
    choices = []
    date_for_day = {}
    for record in dates:
        if not isinstance(record, dict) or not _available(record):
            continue
        try:
            iso = record['date']
            if not isinstance(iso, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', iso):
                continue
            day = Date.fromisoformat(iso)
            day_key = _day(record['dayOfMonth'])
            if int(day_key) != day.day:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        if day_key in date_for_day and date_for_day[day_key] != iso:
            raise SlotError('The current pickup model ambiguously maps one day to multiple dates.')
        date_for_day[day_key] = iso
        for window in windows.get(day_key, []):
            if not isinstance(window, dict) or not _available(window):
                continue
            try:
                if window.get('timeZone') != TIME_ZONE:
                    continue
                start_h, start_m, start = _clock(window.get('checkInStart'))
                end_h, end_m, end = _clock(window.get('checkInEnd'))
                start_at, end_at = _instant(day, start_h, start_m, zone), _instant(day, end_h, end_m, zone)
                if start_at <= now or end_at <= start_at:
                    continue
                selected = PickupSlot(expected_store_id, iso, start, end, TIME_ZONE, day_key,
                    _opaque(window.get('SlotId')), _opaque(window.get('signKey')),
                    _opaque(window.get('timeSlotValue')), _opaque(window.get('timeSlotType'), optional=True),
                    start_at, end_at)
                choices.append(selected)
            except SlotError:
                continue
    if not choices:
        raise SlotError('No valid future unrestricted pickup window is available for this store.')
    return min(choices, key=lambda slot: (slot._start_at, slot._end_at))
