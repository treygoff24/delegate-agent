import json
import sys
import unittest

from delegate_agent.harness_events import StreamAccumulator


class HarnessJsonLimitTests(unittest.TestCase):
    def test_json_integer_limit_cannot_abort_omp_output_recovery(self):
        previous = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(4300)
            accumulator = StreamAccumulator(harness="omp")
            accumulator.ingest_line('{"unexpected":' + "1" * 5000 + "}")
            accumulator.ingest_line(
                json.dumps(
                    {
                        "type": "turn_end",
                        "message": {
                            "role": "assistant",
                            "stopReason": "stop",
                            "content": [{"type": "text", "text": "recovered"}],
                        },
                    }
                )
            )
            self.assertEqual(accumulator.terminal_status, "succeeded")
            self.assertEqual(accumulator.assistant_text, "recovered")
        finally:
            sys.set_int_max_str_digits(previous)


if __name__ == "__main__":
    unittest.main()
