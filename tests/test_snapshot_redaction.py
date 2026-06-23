import io
import unittest

from tests.snapshot_commands_test_base import SnapshotCommandTestBase


class SnapshotRedactionTests(SnapshotCommandTestBase):
    def test_redact_string_preserves_separator_format(self):
        self.assertIn(":", self.redaction.redact_string("API_KEY: secret-value"))
        self.assertIn("=", self.redaction.redact_string("token=secret-value"))

    def test_redact_string_covers_common_credential_shapes(self):
        jwt_like = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        anthropic_sample = "sk-ant-api03-" + "abcdefgh12345678"
        google_sample = "AIza" + "SyDaGmWKa4JsXZ-HjGw7ISLn_3namBGewQe"
        aws_sample = "wJalr" + "XUtnFEMI/K7MDENG/bPxRf"
        pem_private_key = (
            "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
        )
        keyed_pem_private_key = (
            "SECRET_KEY=-----BEGIN " + "PRIVATE KEY-----\nABCDEFSECRETBODY\n"
            "-----END PRIVATE KEY-----"
        )
        stripe_sample = "sk_live_" + "abcdef1234567890XYZ"
        npm_sample = "npm_" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
        slack_webhook = "https://hooks.slack.com/services/T00000000/B11111111/" + "abcdEFGH1234"
        slack_gov_webhook = (
            "https://hooks.slack-gov.com/services/T00000000/B11111111/" + "abcdEFGH1234"
        )
        cases = [
            ("Authorization: Bearer bearer-token-12345", "bearer-token-12345"),
            ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
            ("Authorization: ApiKey api-key-token-12345", "api-key-token-12345"),
            ("Authorization: opaque-authorization-token-12345", "opaque-authorization-token-12345"),
            ("Bearer whitespace-token-12345", "whitespace-token-12345"),
            ("token=secret-token-value", "secret-token-value"),
            ("api_key=secret-api-key", "secret-api-key"),
            ("password: super-secret", "super-secret"),
            (f"jwt {jwt_like}", jwt_like),
            # JSON-quoted keys (the value sits after a closing quote on the key).
            ('{"secret": "topsecretvalue123456"}', "topsecretvalue123456"),
            ('"password": "hunter2hunter2"', "hunter2hunter2"),
            ('{"Authorization": "Bearer abcdef123456"}', "abcdef123456"),
            # Provider token shapes.
            ("sk-proj-aBcD1234efGH5678ijKL", "aBcD1234efGH5678ijKL"),
            (anthropic_sample, "abcdefgh12345678"),
            ("ghs_1234567890abcdefghijklmnopqrstuvwx", "1234567890abcdefghijklmnopqrstuvwx"),
            (
                "github_pat_11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz",
                "11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz",
            ),
            ("AKIAIOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
            (google_sample, "SyDaGmWKa4JsXZ-HjGw7ISLn_3namBGewQe"),
            # Connection-string passwords (with and without a username).
            ("postgres://admin:supersecret@db.example.com/x", "supersecret"),
            ("redis://:redacted-pass-9999@localhost:6379", "redacted-pass-9999"),
            # Env-style credential keys joined to a prefix by "_" (\b misses these).
            ("OPENAI_API_KEY=plainvalue1234567890", "plainvalue1234567890"),
            ("DB_PASSWORD=hunter2hunter2", "hunter2hunter2"),
            ("export GITHUB_TOKEN=ghxxxxplainsecretvalue", "ghxxxxplainsecretvalue"),
            ("MY_SECRET=topsecretpayload", "topsecretpayload"),
            ("aws_secret_access_key=" + aws_sample, "wJalrXUtnFEMI"),
            ("SECRET_KEY=django-insecure-abcdef123456", "django-insecure-abcdef123456"),
            # Private-key env names can hold base64 or escaped key material without
            # literal PEM markers, so the key name itself must trigger redaction.
            ("PRIVATE_KEY=base64privatekeypayload123456", "base64privatekeypayload123456"),
            (
                "JWT_PRIVATE_KEY=base64jwtprivatekeypayload123456",
                "base64jwtprivatekeypayload123456",
            ),
            (
                'GOOGLE_PRIVATE_KEY="base64googleprivatekeypayload123456"',
                "base64googleprivatekeypayload123456",
            ),
            (
                "SSH_PRIVATE_KEY=base64sshprivatekeypayload123456",
                "base64sshprivatekeypayload123456",
            ),
            # Bracketed env assignment forms.
            (
                'os.environ["OPENAI_API_KEY"] = "openai-secret-from-python"',
                "openai-secret-from-python",
            ),
            ("env['DB_PASSWORD']='db-secret-from-python'", "db-secret-from-python"),
            # PEM private key block.
            (
                pem_private_key,
                "MIIEpAIBAAKCAQEA",
            ),
            # A PEM block assigned to a named key must not leak its body past the
            # first line (the key matcher stops at the newline; PEM redaction runs
            # first to catch the whole block).
            (
                keyed_pem_private_key,
                "ABCDEFSECRETBODY",
            ),
            # Additional provider prefixes.
            ("stripe " + stripe_sample, "abcdef1234567890XYZ"),
            ("whsec_aBcDeF1234567890ghIJKL", "aBcDeF1234567890ghIJKL"),
            (npm_sample, "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"),
            ("SG.aBcDeFgHiJkLmNoP.qRsTuVwXyZ0123456789abcd", "qRsTuVwXyZ0123456789abcd"),
            (
                "//registry.npmjs.org/:_authToken=deadbeefdeadbeefdeadbeefdeadbeef",
                "deadbeefdeadbeefdeadbeefdeadbeef",
            ),
            (
                slack_webhook,
                "abcdEFGH1234",
            ),
            (
                slack_gov_webhook,
                "abcdEFGH1234",
            ),
        ]
        for raw, secret in cases:
            with self.subTest(raw=raw):
                redacted = self.redaction.redact_string(raw)
                self.assertIn("***", redacted)
                self.assertNotIn(secret, redacted)

    def test_redact_string_preserves_ordinary_dotted_identifiers(self):
        # The JWT matcher is anchored on the eyJ header so it must not shred
        # tracebacks, module paths, or dotted filenames the parent agent reads.
        survivors = [
            "No module named delegate_agent.run_registry.parser_helper",
            "see file_one_long.module_two_long.module_three_x",
            "2026-06-02.error_log_file.backup_archive_v2",
            "the secret to success is hard work",
            "git@github.com:user/repo.git",
            # Credential-shaped words without an assignment separator are prose.
            "mytokenfield holds the parsed value",
            "the access token grants entry to the room",
            # Stripe publishable keys are public by design; never redacted.
            "pk_live_publishable000000key is shown in the client bundle",
        ]
        for text in survivors:
            with self.subTest(text=text):
                self.assertEqual(self.redaction.redact_string(text), text)

    def test_redact_string_leaves_pathological_dotted_non_secret_unchanged(self):
        # Regression guard: a dotted non-secret string should not be mistaken for
        # a connection string merely because it is long and separator-heavy.
        payload = ("a" * 10 + ".") * 1000
        self.assertEqual(self.redaction.redact_string(payload), payload)

    def test_redact_string_collapses_unterminated_pem_near_misses(self):
        # Unterminated private-key blocks are treated as secret from the first
        # marker onward; this guards the behavior without a host-speed budget.
        payload = "-----BEGIN PRIVATE KEY-----\n" * 20
        self.assertEqual(
            self.redaction.redact_string(payload),
            "***PRIVATE KEY REDACTED***",
        )

    def test_snapshot_redacts_secrets_by_default(self):
        _, alias = self.write_run(  # pragma: allowlist secret
            assistant_text="export API_KEY=sk-abcdefghijklmnopqrstuvwxyz"
        )
        stdout = io.StringIO()
        self.delegate.main(["--cwd", str(self.workspace), "snapshot", alias], stdout=stdout)
        output = stdout.getvalue()
        self.assertIn("***", output)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", output)  # pragma: allowlist secret

    def test_snapshot_no_redact_preserves_secret_like_text(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz"  # pragma: allowlist secret
        _, alias = self.write_run(assistant_text=f"token {secret}")
        stdout = io.StringIO()
        self.delegate.main(
            ["--cwd", str(self.workspace), "snapshot", "--no-redact", alias],
            stdout=stdout,
        )
        self.assertIn(secret, stdout.getvalue())

    def test_snapshot_warns_on_large_logs(self):
        large = self.registry.LARGE_LOG_WARN_BYTES + 1
        _, alias = self.write_run(stdout_bytes=large)
        stdout = io.StringIO()
        self.delegate.main(["--cwd", str(self.workspace), "snapshot", alias], stdout=stdout)
        self.assertIn("stdout.log > 50 MiB", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
