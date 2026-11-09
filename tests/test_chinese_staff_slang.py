"""Narrow equipment phrase guards; no real chat, windows or inference."""
import io
import json
import unittest
from unittest.mock import Mock

from game_slang import game_glossary
from translator_core import OllamaClient


class ChineseStaffSlangTests(unittest.TestCase):
    def test_trade_equipment_phrases_keep_ordinary_receiving_and_roles_unforced(self):
        for text in ("收個 普頂 可以吃蛋的杖", "收个普顶能吃蛋的杖，10kk，不卖。", "收把弓，不賣，只出10kk。"):
            self.assertIn("looking to buy", game_glossary(text, "English"))
        for text in ("收個包裹", "收個裝著法杖的包裹", "收个弓箭手进队，不收法师", "收个武器店的包裹"):
            self.assertNotIn("looking to buy", game_glossary(text, "English"))

    def test_shots_hint_requires_weapon_capability_phrase(self):
        for text in ("可以吃蛋的杖", "能吃蛋的武器", "這把杖不能吃蛋，不買。"):
            self.assertIn("use shots", game_glossary(text, "English"))
        for text in ("早餐可以吃蛋嗎？", "我拿著法杖吃雞蛋", "拿著法杖吃蛋，早餐還沒吃。", "吃蛋", "我拿著法杖，可以吃蛋嗎？"):
            self.assertNotIn("use shots", game_glossary(text, "English"))
        self.assertNotIn("top no-grade", game_glossary("普頂", "English"))
        self.assertIn("top no-grade", game_glossary("普顶杖", "English"))

    def client(self, outputs):
        client = OllamaClient()
        client._opener = Mock()
        client._opener.open.side_effect = [io.BytesIO(json.dumps(value).encode()) for value in [
            {}, *({"message": {"content": text}} for text in outputs)]]
        return client

    def payloads(self, client):
        return [json.loads(call.args[0].data) for call in client._opener.open.call_args_list
                if call.args[0].full_url.endswith("/api/chat")]

    def test_protected_titles_supply_no_terms_and_recovery_keeps_context(self):
        title = "[收個普頂可以吃蛋的杖]"
        client = self.client(["See [[L2ITEM0]] for 10kk."])
        client.translate(f"See {title} for 10kk.", item_links=(title,))
        system = self.payloads(client)[0]["messages"][0]["content"]
        for phrase in ("looking to buy", "top no-grade", "use shots"):
            self.assertNotIn(phrase, system)
        client = self.client(["Missing linked item", '{"part0":"Buying a top no-grade staff that can use shots"}'])
        result = client.translate("收個 普頂 可以吃蛋的杖 [Club]", item_links=("[Club]",))
        self.assertEqual(result, "Buying a top no-grade staff that can use shots [Club]")
        for payload in self.payloads(client):
            for phrase in ("looking to buy", "top no-grade", "use shots"):
                self.assertIn(phrase, payload["messages"][0]["content"])
            self.assertNotIn("[Club]", payload["messages"][1]["content"])

    def test_non_english_targets_unchanged(self):
        for target in ("Chinese", "Russian", "Spanish", "Portuguese"):
            self.assertEqual(game_glossary("收個 普頂 可以吃蛋的杖", target), "")


if __name__ == "__main__":
    unittest.main()
