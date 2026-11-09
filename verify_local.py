"""Exercise the installed local model without opening or controlling the game."""
import json
import re
import time
from translator_app import load_settings
from translator_core import OllamaClient, read_recent_chat


def main():
    settings = load_settings()
    client = OllamaClient(settings["ollama_url"], settings["model"], "English")
    assert client.health(), "Ollama did not report a version"
    assert settings["model"] in client.list_models(), "Configured model is missing"
    records = read_recent_chat(settings["log_path"], limit=1000)
    print(json.dumps({"local_server": settings["ollama_url"], "model": settings["model"],
                      "saved_chat_records": len(records)}, ensure_ascii=True), flush=True)
    samples = [
        "Привет! Ищем хилера в пати.",
        "Продам меч за 10kk адены, пишите в ПМ.",
        "Buscamos un healer para la party, somos nivel 40.",
        "WTS sword 10kk adena. PM me.",
    ]
    for index, original in enumerate(samples):
        start = time.perf_counter()
        translation = client.translate(original)
        assert translation.strip(), "Empty translation"
        assert len(translation) < 300, "Expected a short translation, not commentary"
        assert not re.search(r"[\u0400-\u04ff]", translation), "Sample should be fully translated into English"
        if index in (0, 2):
            assert "healer" in translation.lower(), "Lost the healer request"
            assert "party" in translation.lower() or "group" in translation.lower(), "Lost the party context"
        if index == 1:
            assert "10kk" in translation and "sword" in translation.lower(), "Changed the item or price"
        if index == 2:
            assert "40" in translation, "Changed the party level"
        if index == 3:
            assert translation == original, "Already-English chat should stay unchanged"
        print(json.dumps({"original": original, "translation": translation,
                          "seconds": round(time.perf_counter() - start, 3)}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
