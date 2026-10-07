import unittest

from app import auth


class AuthorizationCodeTests(unittest.TestCase):
    def setUp(self):
        auth.clients.clear()
        auth.tokens.clear()
        auth.authorization_codes.clear()
        self.original_allowed_redirect_uris = auth.ALLOWED_OAUTH_REDIRECT_URIS
        auth.ALLOWED_OAUTH_REDIRECT_URIS = {"https://example.test/callback"}

    def tearDown(self):
        auth.ALLOWED_OAUTH_REDIRECT_URIS = self.original_allowed_redirect_uris

    def test_registration_requires_https_redirect_uri(self):
        with self.assertRaises(ValueError):
            auth.register_dynamic_client(
                "test-client",
                redirect_uris=["http://example.test/callback"],
            )

    def test_registration_rejects_unapproved_redirect_uri(self):
        with self.assertRaises(ValueError):
            auth.register_dynamic_client(
                "test-client",
                redirect_uris=["https://unapproved.example.test/callback"],
            )

    def test_public_client_registration_does_not_create_a_secret(self):
        client = auth.register_dynamic_client(
            "test-client",
            redirect_uris=["https://example.test/callback"],
            token_endpoint_auth_method="none",
        )

        self.assertIsNone(client["client_secret"])
        self.assertEqual("none", client["token_endpoint_auth_method"])
        self.assertEqual(client, auth.get_client(client["client_id"]))
        self.assertFalse(auth.validate_client_credentials(client["client_id"], "secret"))

    def test_confidential_client_registration_requires_matching_secret(self):
        client = auth.register_dynamic_client(
            "test-client",
            redirect_uris=["https://example.test/callback"],
            token_endpoint_auth_method="client_secret_basic",
        )

        self.assertIsNotNone(client["client_secret"])
        self.assertTrue(
            auth.validate_client_credentials(
                client["client_id"],
                client["client_secret"],
            )
        )
        self.assertFalse(auth.validate_client_credentials(client["client_id"], "wrong-secret"))

    def test_registration_rejects_unsupported_token_authentication_method(self):
        with self.assertRaises(ValueError):
            auth.register_dynamic_client(
                "test-client",
                redirect_uris=["https://example.test/callback"],
                token_endpoint_auth_method="private_key_jwt",
            )

    def test_code_is_bound_to_client_and_redirect_uri(self):
        client = auth.register_dynamic_client(
            "test-client",
            redirect_uris=["https://example.test/callback"],
        )
        code = auth.create_authorization_code(
            client["client_id"],
            "https://example.test/callback",
            "challenge",
            "S256",
        )

        self.assertIsNone(
            auth.consume_authorization_code(
                code,
                "different-client",
                "https://example.test/callback",
            )
        )
        self.assertIsNotNone(
            auth.consume_authorization_code(
                code,
                client["client_id"],
                "https://example.test/callback",
            )
        )
        self.assertIsNone(
            auth.consume_authorization_code(
                code,
                client["client_id"],
                "https://example.test/callback",
            )
        )