"""
Intent classifier for the Shopify AI agent.

Reads intent_schema.json (the single source of truth for supported intents,
actions, and entities) and uses it to build a system prompt for the LLM.
Every user message is classified into one intent AND one action of that
intent before the backend decides which Shopify action to run.

Schema shape (v1.1): each ACTION has its own description, required/optional
entities and example utterances, so similar actions (track_order vs
list_recent_orders, search_products vs get_recommendations, ...) can be told
apart. Older schemas that only define these at intent level still work.

Everything the model returns is treated as untrusted input: the action must
belong to the intent, entities are limited to the keys declared for that
action, and numbers / order numbers / policy types are normalised, so
downstream code (float(price_min), int(quantity), ...) can't crash on
"$50" or "#1042".
"""

import json
import re
from pathlib import Path

try:
    from langfuse import observe
except ImportError:
    def observe(*args, **kwargs):
        def decorator(f):
            return f
        return decorator

SCHEMA_PATH = Path(__file__).parent / "intent_schema.json"

# Qwen3-family models can emit a reasoning block before the answer when
# "thinking mode" is on. Strip it so it never breaks json parsing.
_THINK_RE = re.compile(r"<(?:think|thought)>.*?</(?:think|thought)>", re.DOTALL | re.IGNORECASE)

_MAX_EXAMPLES_PER_ACTION = 4

# Entity keys that must be numbers / have a fixed format.
_FLOAT_ENTITIES = ("price_min", "price_max")
_INT_ENTITIES = ("quantity",)
_ORDER_ENTITIES = ("order_number", "order_id")
_EMAIL_RE = re.compile(r"^[\w.+\-]+@[\w\-]+\.[\w.\-]+$")


def _strip_thinking(text: str) -> str:
    if not text:
        return ""
    return _THINK_RE.sub("", text).strip()


def load_schema() -> dict:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------
def _action_fields(intent: dict, action: dict) -> tuple[list, list, list]:
    """(required, optional, examples) for an action. Falls back to the
    intent-level lists when the schema is the older, intent-level shape."""
    def pick(key):
        return action[key] if key in action else intent.get(key, [])
    return pick("required_entities"), pick("optional_entities"), pick("example_utterances")


def build_system_prompt(schema: dict) -> str:
    """Turn the schema into a system prompt: every intent, and under it every
    action with its own description, entities and example phrasing."""
    lines = [
        "You are an intent classifier for a Shopify shopping assistant.",
        "Classify the user's message into exactly one intent and exactly one action of that intent.",
        "Pick the action whose description fits best; descriptions say when NOT to use an action.",
        "If the message doesn't clearly match anything, or you're not confident, use the 'fallback' intent with the 'clarify' action.",
        "Judge by MEANING, not exact wording: shoppers use slang, typos, short or indirect phrasing, and may mix languages (for example Hinglish). Pick the closest action even when the wording differs from every example.",
        "If a 'Recent conversation' block is given, use it only to resolve references such as 'that one' or 'the second one'; classify ONLY the latest message.",
        "",
        "Supported intents and actions:",
    ]

    for intent in schema["intents"]:
        lines.append(f"- {intent['name']}: {intent['description']}")
        per_action = any(("example_utterances" in a or "required_entities" in a) for a in intent["actions"])

        for action in intent["actions"]:
            desc = action.get("description")
            lines.append(f"    * {action['name']}" + (f": {desc}" if desc else ""))
            if per_action:
                req, opt, examples = _action_fields(intent, action)
                if req:
                    lines.append(f"        required entities: {', '.join(req)}")
                if opt:
                    lines.append(f"        optional entities: {', '.join(opt)}")
                if examples:
                    lines.append("        examples: " + " | ".join(f'"{e}"' for e in examples[:_MAX_EXAMPLES_PER_ACTION]))

        if not per_action:  # legacy schema: entities / examples only exist per intent
            ents = ", ".join(intent.get("required_entities", []) + intent.get("optional_entities", []))
            examples = " | ".join(intent.get("example_utterances", [])[:3])
            if ents:
                lines.append(f"    entities to extract if present: {ents}")
            if examples:
                lines.append(f"    example phrasings: {examples}")

        if intent.get("policy_types"):
            lines.append(f"    policy_type must be exactly one of: {', '.join(intent['policy_types'])}")

    lines += [
        "",
        "Entity rules:",
        "- Only use entity keys listed for the action you chose. Omit anything the user didn't say; never invent values.",
        "- Keep text entities (product names, colors, issue descriptions) in the language the user wrote them in.",
        "- order_number: digits only, without '#'. price_min, price_max, quantity: plain numbers, no currency symbols.",
        "- For product names and search terms, return the shopper's words with obvious typos fixed (for example 'tshirt' -> 't-shirt').",
        "",
        "Always detect the language the user wrote or spoke in, and return its ISO 639-1 code as 'language' — even if it's not English. Do not translate the user's message; only report what language it's in.",
        "",
        "Respond ONLY with a JSON object in this exact shape, no other text:",
        json.dumps(schema["classification_output_format"], indent=2),
    ]
    return "\n".join(lines)


# The prompt only depends on the schema, so build it once per schema object.
# (The schema is stored alongside the prompt so its id() can't be reused.)
_PROMPT_CACHE: dict[int, tuple[dict, str]] = {}


def _system_prompt(schema: dict) -> str:
    cached = _PROMPT_CACHE.get(id(schema))
    if cached is None:
        cached = (schema, build_system_prompt(schema))
        _PROMPT_CACHE[id(schema)] = cached
    return cached[1]


# ---------------------------------------------------------------------
# Lookup + normalising the model's (untrusted) output
# ---------------------------------------------------------------------
def _find_intent(schema: dict, intent_name: str) -> dict | None:
    return next((i for i in schema["intents"] if i["name"] == intent_name), None)


def _find_action(schema: dict, intent_name: str, action_name: str) -> dict | None:
    intent = _find_intent(schema, intent_name)
    if not intent:
        return None
    return next((a for a in intent["actions"] if a["name"] == action_name), None)


def _parse_json(raw: str) -> dict | None:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)  # JSON wrapped in prose / code fences
        if not m:
            return None
        try:
            parsed = json.loads(m.group())
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _confidence(value) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.0
    if 1 < c <= 100:  # model answered with a percentage
        c /= 100
    return max(0.0, min(1.0, c))


def _language(value) -> str:
    lang = str(value or "en").strip().lower().replace("_", "-").split("-")[0]
    return lang if re.fullmatch(r"[a-z]{2,3}", lang) else "en"


def _to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", str(value))  # 1,299 -> 1299
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def _normalise_policy(value, allowed: list[str]) -> str | None:
    if not allowed:
        return str(value)
    key = re.sub(r"[\s\-]+", "_", str(value).strip().lower())
    if key in allowed:
        return key
    for candidate in allowed:  # "refund" / "returns" -> refund_policy
        if key and (key in candidate or candidate.replace("_policy", "") in key):
            return candidate
    return None


def _clean_entities(raw, intent: dict, action: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    req, opt, _ = _action_fields(intent, action)
    allowed = set(req) | set(opt)
    out = {}
    for key, value in raw.items():
        if key not in allowed or value in (None, "", [], {}):
            continue
        if key in _FLOAT_ENTITIES:
            value = _to_number(value)
        elif key in _INT_ENTITIES:
            n = _to_number(value)
            value = max(1, int(n)) if n is not None else None
        elif key in _ORDER_ENTITIES:
            digits = re.sub(r"\D", "", str(value))
            value = digits or None
        elif key == "email":
            value = str(value).strip().lower()
            value = value if _EMAIL_RE.match(value) else None
        elif key == "policy_type":
            value = _normalise_policy(value, intent.get("policy_types", []))
        elif key == "otp_code":
            digits = re.sub(r"\D", "", str(value))
            value = digits if len(digits) >= 4 else str(value).strip()
        elif key == "card_number":
            digits = re.sub(r"\D", "", str(value))
            value = digits if len(digits) >= 12 else str(value).strip()
        if value is None:
            continue
        out[key] = value
    return out


def _fallback(confidence: float = 0.0, language: str = "en") -> dict:
    return {
        "intent": "fallback",
        "action": "clarify",
        "entities": {},
        "confidence": confidence,
        "requires_confirmation": False,
        "language": language,
    }


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------
@observe(name="classify_intent")
def classify_intent(user_message: str, schema: dict | None = None, history: list | None = None) -> dict:
    """Classify a single user message. Returns a dict matching
    classification_output_format from the schema, with requires_confirmation
    filled in from the schema (never trusted from the model's own output)."""
    schema = schema or load_schema()

    # Optional context: the last few (role, text) turns, so follow-ups like
    # "the second one" can be resolved. Only the latest message is classified.
    user_content = user_message
    if history:
        convo = "\n".join(
            f"{'Shopper' if role == 'user' else 'Assistant'}: {str(text)[:200]}" for role, text in history[-6:]
        )
        user_content = f"Recent conversation (context only):\n{convo}\n\nLatest message to classify:\n{user_message}"

    response = llm_selector.create_chat_completion(
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _system_prompt(schema)},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    raw = _strip_thinking(response.choices[0].message.content)

    result = _parse_json(raw)
    if result is None:
        print(f"intent_classifier: model returned unparseable content; raw={raw!r}")
        return _fallback()

    confidence = _confidence(result.get("confidence"))
    language = _language(result.get("language"))
    intent_name = str(result.get("intent") or "fallback")
    action_name = str(result.get("action") or "")

    intent = _find_intent(schema, intent_name)
    action = _find_action(schema, intent_name, action_name)

    # Intent is valid but the action is missing/wrong: if the intent has only
    # one action (e.g. policy_query) there's nothing to guess.
    if intent and action is None and len(intent["actions"]) == 1:
        action = intent["actions"][0]

    if confidence < schema.get("confidence_threshold", 0.6) or intent is None or action is None:
        return _fallback(confidence, language)

    return {
        "intent": intent["name"],
        "action": action["name"],
        "entities": _clean_entities(result.get("entities"), intent, action),
        "confidence": confidence,
        "requires_confirmation": bool(action.get("requires_confirmation", False)),
        "language": language,
    }


if __name__ == "__main__":
    # Quick manual test — run: python intent_classifier.py
    print(build_system_prompt(load_schema()))
    for msg in [
        "Where is my order #1042?",
        "Show me my recent orders",
        "Recommend something for me",
        "Add the red hoodie to my cart",
        "What's your refund policy?",
        "asdkfj",
    ]:
        print(f"\n> {msg}")
        print(json.dumps(classify_intent(msg), indent=2))
