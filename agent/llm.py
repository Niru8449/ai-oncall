import json
import os
import sys

import requests

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY"},
    "ollama": {"base_url": "http://127.0.0.1:11434/v1", "key_env": None},
}
TIMEOUT_S = 120


class LLMError(Exception):
    pass


class Client:
    def __init__(self, provider=None, model=None, base_url=None, api_key=None):
        self.provider = provider or os.environ.get("ONCALL_LLM_PROVIDER", "ollama")
        if self.provider not in PROVIDERS:
            raise LLMError(f"unknown provider {self.provider!r}; use one of {sorted(PROVIDERS)}")
        preset = PROVIDERS[self.provider]
        self.base_url = (base_url or os.environ.get("ONCALL_LLM_BASE_URL") or preset["base_url"]).rstrip("/")
        self.model = model or os.environ.get("ONCALL_LLM_MODEL")
        if not self.model:
            raise LLMError("no model: set ONCALL_LLM_MODEL")
        key_env = preset["key_env"]
        self.api_key = api_key or (os.environ.get(key_env) if key_env else None) or "none"
        if key_env and self.api_key == "none":
            raise LLMError(f"{self.provider} needs {key_env}")

    def describe(self):
        return f"{self.provider} {self.model} at {self.base_url}"

    def chat(self, messages, tools=None, temperature=0):
        body = {"model": self.model, "messages": messages, "temperature": temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            data=json.dumps(body),
            timeout=TIMEOUT_S,
        )
        if response.status_code != 200:
            raise LLMError(f"{self.provider} returned {response.status_code}: {response.text[:300]}")
        data = response.json()
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError):
            raise LLMError(f"unexpected reply: {json.dumps(data)[:300]}")
        usage = data.get("usage") or {}
        return message, usage


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        client = Client()
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(client.describe())
    if argv[:1] == ["--ping"]:
        message, usage = client.chat([{"role": "user", "content": "Reply with the single word: ready"}])
        print(f"reply: {message.get('content', '').strip()!r} usage: {usage}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
