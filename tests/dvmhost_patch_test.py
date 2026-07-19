import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH = (ROOT / "patches" / "dvmhost.patch").read_text(encoding="utf-8")


class DvmhostPatchTest(unittest.TestCase):
    def test_motorola_tms_fragment_ack_timeout_retries_current_pdu(self):
        self.assertIn("kMotorolaTmsP25FragmentAckTimeoutMs = 5000U", PATCH)
        self.assertIn(
            "writeRF_PDU(m_retryPDUData, m_retryPDUBitLength, false, true);",
            PATCH,
        )
        self.assertIn(
            "Motorola TMS fragment acknowledgement timed out; retrying",
            PATCH,
        )

    def test_motorola_tms_fragment_ack_timer_is_reset_on_ack_and_delivery_reset(self):
        self.assertGreaterEqual(PATCH.count("pendingTmsFragmentAckMs = 0U;"), 3)
        self.assertIn(
            "pendingTmsFragmentAckMs = kMotorolaTmsP25FragmentAckTimeoutMs;",
            PATCH,
        )


if __name__ == "__main__":
    unittest.main()
