from django.test import SimpleTestCase

from apps.conversations.normalization.phone import digits_only, normalize_e164


class NormalizeE164Tests(SimpleTestCase):
    def test_already_e164_us(self):
        self.assertEqual(normalize_e164('+18135551234'), '+18135551234')

    def test_ten_digit_us(self):
        self.assertEqual(normalize_e164('8135551234'), '+18135551234')

    def test_eleven_digit_with_leading_one(self):
        self.assertEqual(normalize_e164('18135551234'), '+18135551234')

    def test_parenthesized_us(self):
        self.assertEqual(normalize_e164('(813) 555-1234'), '+18135551234')

    def test_dashed_us(self):
        self.assertEqual(normalize_e164('813-555-1234'), '+18135551234')

    def test_dotted_us(self):
        self.assertEqual(normalize_e164('813.555.1234'), '+18135551234')

    def test_spaced_us(self):
        self.assertEqual(normalize_e164('813 555 1234'), '+18135551234')

    def test_extension_stripped(self):
        self.assertEqual(normalize_e164('813-555-1234 x99'), '+18135551234')
        self.assertEqual(normalize_e164('(813) 555-1234 ext. 42'), '+18135551234')
        self.assertEqual(normalize_e164('813 555 1234 extension 7'), '+18135551234')

    def test_international_kept_as_is(self):
        self.assertEqual(normalize_e164('+442071838750'), '+442071838750')
        self.assertEqual(normalize_e164('+44 20 7183 8750'), '+442071838750')

    def test_seven_digit_local_rejected(self):
        # No area code = no way to safely normalize.
        self.assertIsNone(normalize_e164('555-1234'))

    def test_letters_only_rejected(self):
        self.assertIsNone(normalize_e164('call me maybe'))

    def test_empty_and_none(self):
        self.assertIsNone(normalize_e164(None))
        self.assertIsNone(normalize_e164(''))
        self.assertIsNone(normalize_e164('   '))

    def test_no_silent_country_guessing_for_non_us(self):
        # 8 digits, no +, default_country='UK' — we don't guess, we refuse.
        self.assertIsNone(normalize_e164('20718387', default_country='UK'))

    def test_bogus_length_e164_rejected(self):
        self.assertIsNone(normalize_e164('+1234'))          # too short
        self.assertIsNone(normalize_e164('+' + '1' * 20))    # too long

    def test_twelve_digit_no_plus_rejected(self):
        # Not our job to guess. 12 digits w/o + is unparseable under US rules.
        self.assertIsNone(normalize_e164('123456789012'))


class DigitsOnlyTests(SimpleTestCase):
    def test_extracts_all_digits(self):
        self.assertEqual(digits_only('(813) 555-1234'), '8135551234')
        self.assertEqual(digits_only('+1-813-555-1234'), '18135551234')

    def test_empty_input(self):
        self.assertEqual(digits_only(None), '')
        self.assertEqual(digits_only(''), '')
        self.assertEqual(digits_only('no digits here'), '')
