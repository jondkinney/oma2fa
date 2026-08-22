from __future__ import annotations

import unittest
from unittest.mock import patch

import oma2fa.detector as detector
from oma2fa.detector import detect_otp, label_service
from oma2fa.util import normalize_text, parse_timestamp


class DetectorTests(unittest.TestCase):
    def test_detects_six_digit_verification_code(self) -> None:
        result = detect_otp("Example", "Your verification code is 123456")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.code, "123456")
        self.assertEqual(result.service, "Example")
        self.assertGreaterEqual(result.confidence, 0.8)

    def test_normalizes_unicode_digits_width_spacing_and_controls(self) -> None:
        result = detect_otp(
            "Example",
            "Use \u202e\uff11\uff12\uff13\u2009\u0664\u0665\u0666\u202c "
            "to sign in. Do not share it.",
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.code, "123456")

    def test_detects_supported_lengths(self) -> None:
        for code in ("1234", "12345", "123456", "1234567", "12345678"):
            with self.subTest(code=code):
                result = detect_otp("Example", f"Your verification code is {code}")
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result.code, code)

    def test_detects_alphanumeric_and_grouped_codes(self) -> None:
        examples = {
            "Your login code: A1B2C3": "A1B2C3",
            "Use code AB12-CD34 to verify your account": "AB12CD34",
            "Your login code is aB3dE7": "aB3dE7",
        }
        for body, expected in examples.items():
            with self.subTest(body=body):
                result = detect_otp("Example", body)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result.code, expected)

    def test_rejects_common_non_otp_numbers(self) -> None:
        bodies = (
            "Your order 123456 has shipped",
            "Call 555-1234 for delivery details",
            "Invoice 123456 is due for $45",
            "Your appointment confirmation number is 123456",
            "The coupon code A1B2C3 saves 20 percent",
            "Visit https://example.test/verify/123456",
            "The meeting is on 2026-08-21",
            "Random identifier 123456",
            "Your debit card PIN is 4827",
            "Use code 482731 to redeem your gift",
            "Enter code 482731 at checkout",
            "Your locker code is 482731",
            "Your verification code expires on 08/21/2026",
            "Your verification code is Ж123456Ж",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertIsNone(detect_otp("Example", body))

    def test_service_label_prefers_message_prefix_then_sender(self) -> None:
        self.assertEqual(
            label_service("12345", "Acme Cloud: Your verification code is 123456"),
            "Acme Cloud",
        )
        self.assertEqual(
            label_service("ACME", "Your verification code: 123456"),
            "Acme",
        )
        self.assertEqual(
            label_service("Example", "Your login code: A1B2C3"),
            "Example",
        )
        self.assertEqual(label_service("+1 555 0100", "Code: 123456"), "SMS")
        for body in (
            "Use code: 123456",
            "Enter code: 123456",
            "This code: 123456",
            "The code: 123456",
            "Input code: 123456",
            "Submit code: 123456",
            "Copy code: 123456",
        ):
            with self.subTest(body=body):
                self.assertEqual(label_service("Example", body), "Example")

    def test_url_scan_is_precomputed_once_for_dense_input(self) -> None:
        body = "Verification codes: " + " ".join(
            f"https://example.test/{index:06d}" for index in range(300)
        )
        with patch("oma2fa.detector._url_mask", wraps=detector._url_mask) as url_mask:
            self.assertIsNone(detect_otp("Example", body))
        url_mask.assert_called_once()

    def test_timestamp_parses_epoch_millis_iso_and_map(self) -> None:
        default = 1_700_000_000.0
        self.assertEqual(parse_timestamp(None, default=default), default)
        self.assertEqual(parse_timestamp(1_700_000_000_000, default=default), default)
        self.assertEqual(
            parse_timestamp("2023-11-14T22:13:20Z", default=0),
            default,
        )
        self.assertIsInstance(parse_timestamp("20260821T120000", default=0), float)

    def test_normalization_does_not_fold_letter_confusables(self) -> None:
        # Changing Greek/Cyrillic lookalikes could silently alter a real code.
        self.assertEqual(normalize_text("A\u0391123"), "A\u0391123")


if __name__ == "__main__":
    unittest.main()
