import unittest

from tests import _discord_stub  # noqa: F401  (installs stub if needed)
from kitt.config import Config, ConfigError, load_config

GOOD = {"DISCORD_TOKEN": "abc.def.ghi", "GUILD_ID": "123", "VOICE_CHANNEL_ID": "456"}


class ConfigTests(unittest.TestCase):
    def test_valid(self):
        cfg = load_config(GOOD)
        self.assertEqual((cfg.token, cfg.guild_id, cfg.voice_channel_id), ("abc.def.ghi", 123, 456))

    def test_whitespace_is_stripped(self):
        cfg = load_config({"DISCORD_TOKEN": "  tok  ", "GUILD_ID": " 1 ", "VOICE_CHANNEL_ID": "2\n"})
        self.assertEqual((cfg.token, cfg.guild_id, cfg.voice_channel_id), ("tok", 1, 2))

    def test_all_missing_reports_every_problem(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config({})
        text = str(ctx.exception)
        for name in ("DISCORD_TOKEN", "GUILD_ID", "VOICE_CHANNEL_ID"):
            self.assertIn(name, text)

    def test_blank_values_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({**GOOD, "DISCORD_TOKEN": "   "})

    def test_non_integer_ids_rejected_without_echoing_value(self):
        secret_like = "MTIzNDU2.secret.value"
        with self.assertRaises(ConfigError) as ctx:
            load_config({**GOOD, "GUILD_ID": secret_like})
        self.assertIn("GUILD_ID", str(ctx.exception))
        self.assertNotIn(secret_like, str(ctx.exception))

    def test_zero_and_negative_ids_rejected(self):
        for bad in ("0", "-5"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load_config({**GOOD, "VOICE_CHANNEL_ID": bad})

    def test_token_with_inner_whitespace_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({**GOOD, "DISCORD_TOKEN": "abc def"})

    def test_repr_hides_token(self):
        self.assertNotIn("abc.def.ghi", repr(load_config(GOOD)))
        self.assertIsInstance(load_config(GOOD), Config)


if __name__ == "__main__":
    unittest.main()
