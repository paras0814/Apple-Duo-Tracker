import copy
from datetime import datetime, timezone, timedelta
import json
import unittest
from unittest.mock import patch

from beta.slots import PREFIX, SlotError, choose_earliest_slot


def window(start='09:15 AM', end='09:30 AM', **changes):
    return {'SlotId': 'PRIVATE_SLOT_FIXTURE', 'signKey': 'PRIVATE_SIGNATURE_FIXTURE',
            'isRestricted': None, 'checkInStart': start, 'checkInEnd': end,
            'timeZone': 'America/New_York', 'timeSlotValue': 'PRIVATE_VALUE_FIXTURE', **changes}


def fixture(dates=None, groups=None):
    return {'body': {'checkout': {'fulfillment': {'pickupTab': {'pickup': {'timeSlot': {
        'd': {'storeId': 'R654'}, 'dateTimeSlots': {'d': {'storeSelected': 'R654',
        'pickUpDates': dates or [{'date': '2026-10-28', 'dayOfMonth': '28'}],
        'timeSlotWindows': groups or [{'28': [window()]}]}}}}}}}}}


NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)


class SlotTests(unittest.TestCase):
    def setUp(self):
        # Fixed test clock/zone for October fixtures; production uses IANA data.
        self.zone = patch('beta.slots._ny_zone', return_value=timezone(timedelta(hours=-4)))
        self.zone.start(); self.addCleanup(self.zone.stop)

    def choose(self, model=None, **kwargs):
        return choose_earliest_slot(model or fixture(), 'R654', now=kwargs.get('now', NOW))

    def test_earliest_across_unsorted_dates_and_unsorted_windows(self):
        model = fixture(dates=[{'date': '2026-10-29', 'dayOfMonth': '29'},
                               {'date': '2026-10-28', 'dayOfMonth': '28'}],
            groups=[{'29': [window('08:00 AM', '08:15 AM')]},
                    {'28': [window('02:00 PM', '02:15 PM'), window('09:15 AM', '09:30 AM')]}])
        selected = self.choose(model)
        self.assertEqual(selected.safe_facts(), {'store_id': 'R654', 'date': '2026-10-28',
            'start': '09:15 AM', 'end': '09:30 AM', 'time_zone': 'America/New_York'})
        self.assertNotIn('PRIVATE_', repr(selected))
        self.assertNotIn('PRIVATE_', json.dumps(selected.safe_facts()))
        fields = selected.form_fields()
        self.assertNotIn('PRIVATE_', repr(fields))
        self.assertEqual(fields[PREFIX + 'timeSlotId'], 'PRIVATE_SLOT_FIXTURE')
        self.assertEqual(fields[PREFIX + 'signKey'], 'PRIVATE_SIGNATURE_FIXTURE')
        self.assertEqual(fields[PREFIX + 'isRecommended'], 'false')
        self.assertEqual(fields[PREFIX + 'displayStartTime'], '')
        self.assertEqual(fields[PREFIX + 'displayEndTime'], '')
        self.assertNotIn(PREFIX + 'timeSlotType', fields)

    def test_optional_type_is_server_value_only(self):
        model = fixture(groups=[{'28': [window(timeSlotType='SERVER_FIXTURE_TYPE')]}])
        self.assertEqual(self.choose(model).form_fields()[PREFIX + 'timeSlotType'], 'SERVER_FIXTURE_TYPE')
        no_space = self.choose(fixture(groups=[{'28': [window('9:15AM', '9:30AM')]}]))
        self.assertEqual(no_space.form_fields()[PREFIX + 'startTime'], '9:15AM')

    def test_skip_restricted_disabled_unavailable_and_bad_opaque_fields(self):
        bad = [window('08:00 AM', '08:15 AM', **change) for change in (
            {'isRestricted': True}, {'isRestricted': 'false'}, {'disabled': True},
            {'unavailable': True}, {'enabled': False}, {'available': False},
            {'SlotId': ''}, {'signKey': None}, {'signKey': 'unknown'},
            {'signKey': 'PRIVATE\nINVALID'}, {'timeSlotValue': ''})]
        selected = self.choose(fixture(groups=[{'28': bad + [window()]}]))
        self.assertEqual(selected.start, '09:15 AM')

    def test_skip_stale_window_and_select_next_future(self):
        now = datetime(2026, 10, 28, 13, 15, tzinfo=timezone.utc)
        selected = self.choose(fixture(groups=[{'28': [window(), window('09:30 AM', '09:45 AM')]}]), now=now)
        self.assertEqual(selected.start, '09:30 AM')
        with self.assertRaises(SlotError):
            self.choose(now=datetime(2026, 10, 30, tzinfo=timezone.utc))

    def test_mismatched_store_or_missing_store_proof_rejected(self):
        for key, value in [('storeId', 'R999'), ('storeSelected', 'R999'), ('storeSelected', None)]:
            model = fixture(); node = model['body']['checkout']['fulfillment']['pickupTab']['pickup']['timeSlot']
            (node['d'] if key == 'storeId' else node['dateTimeSlots']['d'])[key] = value
            with self.assertRaises(SlotError):
                self.choose(model)

    def test_unknown_zone_bad_clock_and_end_before_start_rejected(self):
        for change in ({'timeZone': 'UTC'}, {'timeZone': None}, {'checkInStart': '25:15'},
                       {'checkInEnd': '09:00 AM'}, {'checkInEnd': '09:15 AM'},
                       {'checkInStart': '09:15 AM PRIVATE'}):
            with self.assertRaises(SlotError):
                self.choose(fixture(groups=[{'28': [window(**change)]}]))

    def test_invalid_date_and_day_map_cannot_select_window(self):
        for dates in ([{'date': '2026-10-28', 'dayOfMonth': '29'}],
                      [{'date': '2026-02-30', 'dayOfMonth': '30'}],
                      [{'date': '2026-10-28', 'dayOfMonth': True}]):
            with self.assertRaises(SlotError):
                self.choose(fixture(dates=dates))
        with self.assertRaises(SlotError):
            self.choose(now=datetime(2026, 10, 27))
        with self.assertRaisesRegex(SlotError, 'ambiguously maps'):
            self.choose(fixture(dates=[{'date': '2026-10-28', 'dayOfMonth': '28'},
                                       {'date': '2026-11-28', 'dayOfMonth': '28'}]))

    def test_unwrapped_current_body_supported_and_input_not_mutated(self):
        model = fixture()['body']; original = copy.deepcopy(model)
        self.assertEqual(self.choose(model).date, '2026-10-28')
        self.assertEqual(model, original)

    def test_missing_zone_data_is_actionable_without_private_values(self):
        with patch('beta.slots._ny_zone', side_effect=SlotError('IANA time zone data is missing.')):
            with self.assertRaisesRegex(SlotError, 'IANA time zone data'):
                self.choose()

    def test_actual_iana_winter_summer_offsets_and_dst_gaps(self):
        # Exercise real bundled IANA rules independently of the October fixture
        # replacement. These checks require Setup-API's pinned tzdata install.
        from datetime import date
        self.zone.stop()
        from beta.slots import _instant, _ny_zone
        zone = _ny_zone()
        self.assertEqual(_instant(date(2027, 1, 15), 9, 15, zone).hour, 14)
        self.assertEqual(_instant(date(2027, 7, 15), 9, 15, zone).hour, 13)
        with self.assertRaises(SlotError):
            _instant(date(2027, 3, 14), 2, 15, zone)  # DST spring gap.
        with self.assertRaises(SlotError):
            _instant(date(2026, 11, 1), 1, 15, zone)  # DST repeated hour.


if __name__ == '__main__':
    unittest.main()
