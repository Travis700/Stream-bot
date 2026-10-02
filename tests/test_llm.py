import asyncio
import dataclasses
import json

from aiohttp import web

from shared import config, llm

SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"]}


def test_provider_selection(monkeypatch):
    s = dataclasses.replace(config.settings, llm_provider="ollama", rater_llm_provider="anthropic",
                            anthropic_api_key="")
    assert s.provider_for("clips") == "ollama" and s.llm_available("clips")
    assert s.provider_for("rating") == "anthropic"
    assert not s.llm_available("rating")  # no key
    assert not dataclasses.replace(s, llm_provider="none").llm_available()


def test_to_ollama_messages(tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"\xff\xd8fake")
    content = [llm.text_block("library", cache=True), llm.image_block(img), llm.text_block("rate this")]
    messages, has_images = llm.to_ollama_messages("sys", content)
    assert has_images
    assert messages[0] == {"role": "system", "content": "sys"}
    assert messages[1]["content"] == "library\n\nrate this"
    assert len(messages[1]["images"]) == 1


def test_ollama_pulls_model_and_retries_bad_json(monkeypatch):
    calls = {"chat": [], "pull": []}
    answers = iter(["not json", json.dumps({"other": 1}), json.dumps({"score": 7})])

    async def chat(request):
        body = await request.json()
        calls["chat"].append(body)
        if not calls["pull"]:
            return web.Response(status=404, text='{"error":"model \\"x\\" not found, try pulling it first"}')
        return web.json_response({"message": {"role": "assistant", "content": next(answers)}})

    async def pull(request):
        calls["pull"].append(await request.json())
        return web.json_response({"status": "success"})

    async def run():
        app = web.Application()
        app.router.add_post("/api/chat", chat)
        app.router.add_post("/api/pull", pull)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(llm, "settings", dataclasses.replace(
            config.settings, llm_provider="ollama", ollama_url=f"http://127.0.0.1:{port}",
            ollama_text_model="text-model", ollama_vision_model="vision-model"))
        try:
            return await llm.ask_json("sys", [llm.text_block("hi")], SCHEMA)
        finally:
            await runner.cleanup()

    # 404 -> pull (not counted as a try), then bad JSON, missing key, and finally a good answer.
    assert asyncio.run(run()) == {"score": 7}
    assert calls["pull"] == [{"model": "text-model", "stream": False}]
    assert len(calls["chat"]) == 4
    assert calls["chat"][0]["model"] == "text-model"
    assert calls["chat"][0]["format"] == SCHEMA
    assert calls["chat"][0]["options"]["num_ctx"] == config.settings.ollama_num_ctx
