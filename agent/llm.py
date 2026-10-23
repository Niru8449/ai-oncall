import json
import os
import re
import sys
import time

import requests

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY"},
    "ollama": {"base_url": "http://127.0.0.1:11434/v1", "key_env": None},
}
CLAUDE = "claude"
CLAUDE_BASE_URL = "https://api.anthropic.com/v1"
CLAUDE_KEY_ENV = "ANTHROPIC_API_KEY"
CLAUDE_VERSION = "2023-06-01"
CLAUDE_BETA = "thinking-binding-controls-2026-08-01"
CLAUDE_MAX_TOKENS = 16384
ERROR_DUMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "runs")
SCRIPTED = "scripted"
SCRIPT_ENV = "ONCALL_LLM_SCRIPT"
TIMEOUT_S = 120
RETRY_STATUSES = (408, 429, 500, 502, 503, 504, 529)
MAX_RETRIES = 4
RETRY_WAIT_MAX_S = 90
RETRY_WAIT_DEFAULT_S = 10
_WAIT_IN_TEXT = re.compile(r"try again in\s*([0-9.]+)\s*(ms|s)", re.IGNORECASE)


class LLMError(Exception):
    pass


def retry_wait_s(response):
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(max(float(header), 1.0), RETRY_WAIT_MAX_S)
        except ValueError:
            pass
    match = _WAIT_IN_TEXT.search(response.text or "")
    if match:
        value = float(match.group(1))
        if match.group(2).lower() == "ms":
            value /= 1000
        return min(max(value, 1.0), RETRY_WAIT_MAX_S)
    return RETRY_WAIT_DEFAULT_S


def post_with_retry(provider, url, headers, body):
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(url, headers=headers, data=json.dumps(body), timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise LLMError(f"{provider} unreachable after {attempt + 1} attempts: {exc}")
            print(f"  llm: {provider} request failed ({exc.__class__.__name__}); retry {attempt + 1} of {MAX_RETRIES} in {RETRY_WAIT_DEFAULT_S}s", flush=True)
            time.sleep(RETRY_WAIT_DEFAULT_S)
            continue
        if response.status_code == 200:
            return response.json()
        if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
            wait = retry_wait_s(response)
            print(f"  llm: {provider} returned {response.status_code}; retry {attempt + 1} of {MAX_RETRIES} in {wait:.0f}s", flush=True)
            time.sleep(wait)
            continue
        dump = dump_failed_request(provider, response, body)
        raise LLMError(f"{provider} returned {response.status_code}: {response.text[:300]}" + (f" (request saved to {dump})" if dump else ""))
    raise LLMError(f"{provider}: retries exhausted")


def dump_failed_request(provider, response, body):
    """Save the request that was refused (no key in it) so the shape can be inspected later."""
    try:
        os.makedirs(ERROR_DUMP_DIR, exist_ok=True)
        path = os.path.join(ERROR_DUMP_DIR, f"llm-error-{provider}-{time.strftime('%Y%m%d-%H%M%S')}.json")
        with open(path, "w") as handle:
            json.dump({"status": response.status_code, "response": response.text[:2000], "request": body}, handle, indent=1)
        return path
    except OSError:
        return None


class ScriptedClient:
    def __init__(self, path=None):
        self.path = path or os.environ.get(SCRIPT_ENV)
        if not self.path:
            raise LLMError(f"the scripted provider needs {SCRIPT_ENV}=<file.json>")
        try:
            self.steps = json.loads(open(self.path).read())
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMError(f"cannot read {self.path}: {exc}")
        if not isinstance(self.steps, list):
            raise LLMError(f"{self.path} must hold a JSON list of steps")
        self.provider = SCRIPTED
        self.model = os.path.basename(self.path)
        self.calls = 0

    def describe(self):
        return f"scripted {self.path}"

    def chat(self, messages, tools=None, temperature=0):
        self.calls += 1
        while self.steps:
            step = self.steps[0]
            if "tool" in step and not tools:
                self.steps.pop(0)
                continue
            self.steps.pop(0)
            if "tool" in step:
                call = {"id": f"call{self.calls}", "type": "function", "function": {"name": step["tool"], "arguments": json.dumps(step.get("args") or {})}}
                return {"role": "assistant", "content": None, "tool_calls": [call]}, {"prompt_tokens": 0, "completion_tokens": 0}
            return {"role": "assistant", "content": step.get("content", "")}, {"prompt_tokens": 0, "completion_tokens": 0}
        raise LLMError(f"the script {self.path} ran out of steps")


def Client(provider=None, model=None, base_url=None, api_key=None):
    chosen = provider or os.environ.get("ONCALL_LLM_PROVIDER")
    if chosen == SCRIPTED:
        return ScriptedClient()
    if chosen == CLAUDE:
        return ClaudeClient(model, base_url, api_key)
    return HttpClient(provider, model, base_url, api_key)


class HttpClient:
    def __init__(self, provider=None, model=None, base_url=None, api_key=None):
        self.provider = provider or os.environ.get("ONCALL_LLM_PROVIDER", "ollama")
        if self.provider not in PROVIDERS:
            raise LLMError(f"unknown provider {self.provider!r}; use one of {sorted(PROVIDERS)}, {CLAUDE} or {SCRIPTED}")
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
        data = post_with_retry(
            self.provider,
            f"{self.base_url}/chat/completions",
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            body,
        )
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError):
            raise LLMError(f"unexpected reply: {json.dumps(data)[:300]}")
        usage = data.get("usage") or {}
        return message, usage


class ClaudeClient:
    """Speaks the Claude Messages API; the loop keeps using the chat-completions shape."""

    def __init__(self, model=None, base_url=None, api_key=None):
        self.provider = CLAUDE
        self.base_url = (base_url or os.environ.get("ONCALL_LLM_BASE_URL") or CLAUDE_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("ONCALL_LLM_MODEL")
        if not self.model:
            raise LLMError("no model: set ONCALL_LLM_MODEL")
        self.api_key = api_key or os.environ.get(CLAUDE_KEY_ENV)
        if not self.api_key:
            raise LLMError(f"{CLAUDE} needs {CLAUDE_KEY_ENV}")

    def describe(self):
        return f"{self.provider} {self.model} at {self.base_url}"

    def chat(self, messages, tools=None, temperature=0):
        system, converted = self._convert(messages)
        body = {
            "model": self.model,
            "max_tokens": CLAUDE_MAX_TOKENS,
            "messages": converted,
            # Thinking blocks are bound to the conversation prefix; if one no longer verifies, drop it
            # and carry on instead of failing the whole incident with a 400.
            "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
        }
        if system:
            body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if tools:
            body["tools"] = [
                {"name": t["function"]["name"], "description": t["function"].get("description", ""), "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                for t in tools
            ]
            body["tool_choice"] = {"type": "auto"}
        data = post_with_retry(
            self.provider,
            f"{self.base_url}/messages",
            {"x-api-key": self.api_key, "anthropic-version": CLAUDE_VERSION, "anthropic-beta": CLAUDE_BETA, "Content-Type": "application/json"},
            body,
        )
        if data.get("input_transformations"):
            print(f"  llm: {CLAUDE} changed the input: {json.dumps(data['input_transformations'])[:400]}", flush=True)
        if data.get("stop_reason") == "refusal":
            raise LLMError(f"{CLAUDE} refused the request: {json.dumps(data.get('stop_details'))[:200]}")
        if data.get("stop_reason") == "max_tokens":
            raise LLMError(f"{CLAUDE} hit max_tokens ({CLAUDE_MAX_TOKENS}) before finishing")
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        calls = [
            {"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
            for b in blocks if b.get("type") == "tool_use"
        ]
        message = {"role": "assistant", "content": text or None, "blocks": blocks}
        if calls:
            message["tool_calls"] = calls
        u = data.get("usage") or {}
        usage = {
            "prompt_tokens": int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0),
            "completion_tokens": int(u.get("output_tokens") or 0),
            "cache_read_input_tokens": int(u.get("cache_read_input_tokens") or 0),
            "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens") or 0),
        }
        return message, usage

    @staticmethod
    def _convert(messages):
        system = []
        out = []

        def add(role, blocks):
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in messages:
            role = m.get("role")
            if role == "system":
                system.append(m.get("content") or "")
            elif role == "user":
                add("user", [{"type": "text", "text": m.get("content") or ""}])
            elif role == "assistant":
                if m.get("blocks"):
                    add("assistant", [dict(b) for b in m["blocks"]])
                    continue
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for call in m.get("tool_calls") or []:
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": args})
                add("assistant", blocks or [{"type": "text", "text": ""}])
            elif role == "tool":
                add("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id"), "content": m.get("content") or ""}])
        if out:
            last = out[-1]["content"][-1]
            if last.get("type") in ("text", "tool_result"):
                last["cache_control"] = {"type": "ephemeral"}
        return "\n\n".join(s for s in system if s), out


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
        print(f"reply: {(message.get('content') or '').strip()!r} usage: {usage}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
