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

import llm_selector

import os

if (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip() and (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip():
    try:
        from langfuse import observe
    except ImportError:
        def observe(*args, **kwargs):
            def decorator(f):
                return f
            return decorator
else:
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

ACTION_ACTIONS = {
    # cart mutations
    "add_item",
    "add_all_to_cart",
    "remove_item",
    "edit_quantity",
    "clear_cart",
    "swap_item",
    # checkout & payment
    "initiate_payment",
    "confirm_payment_otp",
    "save_payment_credentials",
    # order modification
    "cancel_order",
    "edit_address",
    "request_order_return",
    "return_order",
    # alerts & scheduled tasks
    "create_instruction",
    # claims & account
    "submit_claim",
    "register",
    "login",
    "logout",
    "forgot_password",
}

CONVERSATIONAL_ACTIONS = {
    "search_products",
    "check_availability",
    "get_recommendations",
    "recommend_products",
    "assistant_help",
    "answer_policy_question",
    "track_order",
    "list_recent_orders",
    "get_my_orders",
    "view_cart",
    "nudge_abandoned_cart",
    "check_claim_status",
    "list_my_alerts",
    "check_return_eligibility",
    "clarify",
}


def determine_message_type(intent: str, action: str, user_message: str = "") -> str:
    """Classifies user message as either 'action' or 'normal' (conversational).
    - 'action': User commands a state mutation (add/remove from cart, checkout, cancel, update address, subscribe alert, initiate return).
    - 'normal': User is chatting, asking questions, checking product availability, checking return eligibility, browsing, or asking policies."""
    msg_low = str(user_message or "").strip().lower()

    # Product availability & return eligibility questions are always normal conversational messages
    if action in ("check_availability", "check_return_eligibility") or any(w in msg_low for w in [
        "available or not", "whether this product is available", "is it available", "in stock", "is this available",
        "can i return", "whether i can return", "eligible for return", "is my order returnable", "return policy for",
        "check return", "return eligibility"
    ]):
        return "normal"

    if action in ("request_order_return", "return_order"):
        return "action"

    if action in ACTION_ACTIONS:
        return "action"
    if action in CONVERSATIONAL_ACTIONS:
        return "normal"

    if action == "subscribe_restock":
        if any(w in msg_low for w in ["notify me", "alert me", "email me when", "tell me when back", "subscribe"]):
            return "action"
        return "normal"

    if intent in ("cart_management", "payment_checkout", "order_management"):
        return "action"
    return "normal"


def is_action_message(intent: str, action: str, user_message: str = "") -> bool:
    """Returns True if the message represents an executable action, False if conversational/normal."""
    return determine_message_type(intent, action, user_message) == "action"


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
        "Judge by MEANING, not exact wording: shoppers use slang, typos, short or indirect phrasing, and may mix languages (for example Hinglish). Pick the closest action even when the wording differs from every example.",
        "Engage the shopper: Every user message in an online store reflects an intent to browse, buy, track, or ask. Greetings, store questions, and 'what can you do' map to 'general_chat.assistant_help'. Interest in any product, category, or 'tell me about X' maps to 'product_search.search_products'. Questions checking if an item is available or in stock map to 'product_search.check_availability'. Recommendation requests map to 'general_chat.recommend_products' or 'product_search.get_recommendations'.",
        "Determine the message_type: Flag 'action' (is_action=true) ONLY when the user explicitly commands a state mutation (add/remove from cart, checkout, cancel order, update address, subscribe alert, initiate return). Flag 'normal' (is_action=false) for conversational inquiries, checking product availability, checking return eligibility or policy, browsing products, asking policies, chatting, or asking general questions.",
        "Use 'fallback.clarify' whenever the message is ambiguous, unclear, or does not map confidently to any intent — do not guess. Reserve it for both uninterpretable gibberish AND short vague messages that could mean many things.",
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
        elif key in ("query", "product_name_or_id"):
            val_str = str(value).strip()
            val_cleaned = re.sub(
                r"^(?:tell\s+me\s+about\s+(?:some\s+|a\s+|an\s+|the\s+)?|do\s+you\s+have\s+(?:any\s+|some\s+)?|show\s+me\s+(?:some\s+|a\s+|an\s+|the\s+)?|find\s+me\s+|search\s+for\s+|looking\s+for\s+)",
                "",
                val_str,
                flags=re.IGNORECASE,
            ).strip()
            qty_match = re.match(r"^(\d+)\s*(?:x|units?|pieces?|items?|pcs?|of)?\s+(.+)$", val_cleaned, re.IGNORECASE)
            if qty_match and "quantity" in allowed and not out.get("quantity") and not raw.get("quantity"):
                try:
                    num = int(qty_match.group(1))
                    if num > 0:
                        out["quantity"] = num
                        val_cleaned = qty_match.group(2).strip()
                except (ValueError, TypeError):
                    pass
            value = val_cleaned or val_str
        if value is None:
            continue
        out[key] = value
    return out


def _is_availability_inquiry(msg_low: str) -> bool:
    if any(k in msg_low for k in ["notify", "alert", "email me", "remind me"]):
        return False
    return (
        "available" in msg_low
        or "in stock" in msg_low
        or "out of stock" in msg_low
        or any(w in msg_low for w in ["check availability", "whether this product is available", "is it available"])
    )


def _extract_availability_product(user_message: str) -> str:
    clean_p = re.sub(
        r"\b(tell me whether|tell me if|is it|is this|are these|whether this|check availability|available or not|in stock|out of stock|available in stock|is|are|available|product|this item|this|it|that|the|do you have|got)\b",
        "",
        user_message,
        flags=re.IGNORECASE,
    ).strip("?.,! ")
    if clean_p.lower() in ("this", "it", "that", "item", "product", ""):
        return ""
    return clean_p


def _extract_order_number(text: str) -> str | None:
    m = re.search(r"#?\s*(\d{3,8})\b", text)
    return m.group(1) if m else None


def _is_return_inquiry(msg_low: str) -> bool:
    return any(p in msg_low for p in [
        "can i return", "whether i can return", "eligible for return", "is my order returnable",
        "return policy for", "check return", "return eligibility", "returnable or not", "can we return",
        "allowed to return", "am i allowed to return", "possible to return"
    ]) or ("return" in msg_low and any(w in msg_low for w in ["whether", "can i", "if i can", "possible", "policy", "allowed", "eligible", "or not"]))


def _is_return_request(msg_low: str) -> bool:
    if _is_return_inquiry(msg_low):
        return False
    return any(p in msg_low for p in [
        "return my order", "return order", "start return", "initiate return", "request return",
        "submit return", "want to return", "please return", "process return", "mujhe return karna hai"
    ])


def _is_active_orders_query(msg_low: str) -> bool:
    return any(p in msg_low for p in [
        "active order", "active orders", "orders in progress", "pending order", "pending orders",
        "open order", "open orders", "in progress orders"
    ])


def _is_cancel_order_query(msg_low: str) -> bool:
    return any(p in msg_low for p in [
        "cancel order", "cancel my order", "want to cancel", "cancel #", "please cancel"
    ])


def _is_add_to_cart_query(msg_low: str) -> bool:
    if any(q in msg_low for q in ["how to", "how do i", "can i add", "what did i add", "when will", "why"]):
        return False
    has_cart_kw = bool(re.search(r"\b(cart|bag|basket)\b", msg_low))
    starts_add = bool(re.match(r"^(?:can you\s+)?(?:could you\s+)?(?:please\s+)?(?:add|put|place|insert)\s+", msg_low))
    if not (has_cart_kw or starts_add):
        return False
    return bool(re.search(r"\b(add|put|place|insert|buy)\b", msg_low))


_CART_REMOVE_LEAD_IN = re.compile(
    r"^(?:can you\s+)?(?:could you\s+)?(?:please\s+)?(?:remove|delete|take\s+out|take|drop|discard)\s+",
    re.IGNORECASE,
)
_CART_REMOVE_TRAILING = re.compile(
    r"\s+(?:from|out\s+of|off|out|in)?\s*(?:my|the)?\s*(?:cart|bag|basket)\s*$",
    re.IGNORECASE,
)
_SEARCH_LEAD_IN_PATTERN = re.compile(
    r"^(?:can you\s+)?(?:could you\s+)?(?:please\s+)?(?:show\s+me|find(?:\s+me)?|search(?:\s+for)?|look(?:\s+up|\s+for)?|"
    r"get(?:\s+me)?|i want(?:\s+to\s+see)?|i'm looking for|i am looking for|do you have(?:\s+any|\s+some)?|"
    r"tell me about(?:\s+any|\s+some)?|are there(?:\s+any)?|browse|where can i find|what(?:'s|\s+is) your cheapest)\s+",
    re.IGNORECASE,
)


def _is_remove_from_cart_query(msg_low: str) -> bool:
    if any(q in msg_low for q in ["how to", "how do i", "can i remove", "why"]):
        return False
    if not re.search(r"\b(remove|delete|take\s+out|take|drop|discard)\b", msg_low):
        return False
    has_cart_kw = bool(re.search(r"\b(cart|bag|basket)\b", msg_low))
    starts_remove = bool(_CART_REMOVE_LEAD_IN.match(msg_low))
    return has_cart_kw or starts_remove


def _extract_remove_cart_product_and_qty(user_message: str) -> tuple[str, int | None]:
    msg = (user_message or "").strip()
    msg_low = msg.lower()
    qty = None
    m_qty = re.search(r"\b(\d+)\s*(?:x|units?|pieces?|pcs?|items?|of)?\b", msg_low)
    cleaned = _CART_REMOVE_LEAD_IN.sub("", msg)
    cleaned = _CART_REMOVE_TRAILING.sub("", cleaned)
    m_lead_qty = re.match(r"^\s*(\d+)\s*(?:x|units?|pieces?|pcs?|items?|of)?\s+(.+)$", cleaned, re.IGNORECASE)
    if m_lead_qty:
        qty = int(m_lead_qty.group(1))
        cleaned = m_lead_qty.group(2).strip()
    elif m_qty:
        m_verb_qty = re.search(r"^(?:can you\s+)?(?:please\s+)?(?:remove|delete|take\s+out|take)\s+(\d+)\s+(.+)$", msg_low)
        if m_verb_qty:
            qty = int(m_verb_qty.group(1))
            cleaned = _CART_REMOVE_TRAILING.sub("", m_verb_qty.group(2)).strip()
    cleaned = re.sub(r"^\s*(?:a|an|the)\s+", "", cleaned, flags=re.IGNORECASE).strip(" .,!?")
    if not cleaned or cleaned.lower() in ("from cart", "from my cart", "from", "cart", "it", "this", "item", "that"):
        return "", qty
    return cleaned, qty


def _is_clear_cart_query(msg_low: str) -> bool:
    if any(q in msg_low for q in ["how to", "how do i", "why"]):
        return False
    return bool(re.search(r"\b(clear|empty|reset)\s+(?:all\s+items\s+in\s+)?(?:my\s+|the\s+)?(?:cart|bag|basket)\b", msg_low)) or msg_low in {
        "clear cart", "clear my cart", "empty cart", "empty my cart", "delete cart", "delete my cart", "reset cart", "remove all from cart"
    }


def _is_product_search_query(msg_low: str) -> bool:
    if any(q in msg_low for q in ["how to", "how do i", "when will", "why", "who are you", "what can you do"]):
        return False
    if re.search(r"\b(cart|bag|basket|checkout|order|track|refund|return|cancel|warranty|claim|login|register|logout|sign in|sign up)\b", msg_low):
        return False
    if _SEARCH_LEAD_IN_PATTERN.search(msg_low):
        return True
    return bool(re.match(r"^(?:search|find|show|look\s+for|looking\s+for|do\s+you\s+have)\s+(?:for\s+)?(.+)$", msg_low))


def _extract_search_product_query(user_message: str) -> str:
    msg = (user_message or "").strip()
    msg_low = msg.lower()
    if _SEARCH_LEAD_IN_PATTERN.search(msg_low):
        cleaned = _SEARCH_LEAD_IN_PATTERN.sub("", msg).strip()
    else:
        m = re.match(r"^(?:search|find|show|look\s+for|looking\s+for|do\s+you\s+have)\s+(?:for\s+)?(.+)$", msg, re.IGNORECASE)
        cleaned = m.group(1).strip() if m else msg
    cleaned = re.sub(r"^\s*(?:a|an|the)\s+", "", cleaned, flags=re.IGNORECASE).strip(" .,!?")
    return cleaned


def _extract_cart_product_and_qty(user_message: str) -> tuple[str, int]:
    msg = (user_message or "").strip()
    msg_low = msg.lower()
    qty = 1
    m_qty = re.search(r"\b(\d+)\s*(?:x|units?|pieces?|pcs?|items?|of)?\b", msg_low)
    cleaned = re.sub(
        r"^(?:can you\s+)?(?:could you\s+)?(?:please\s+)?(?:add\s+to\s+(?:my\s+|the\s+)?cart|add|put|place|insert|buy|get)\s+",
        "", msg, flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+(?:to|in|into|for)?\s*(?:my|the)?\s*(?:cart|bag|basket)\s*$", "", cleaned, flags=re.IGNORECASE)
    m_lead_qty = re.match(r"^\s*(\d+)\s*(?:x|units?|pieces?|pcs?|items?|of)?\s+(.+)$", cleaned, re.IGNORECASE)
    if m_lead_qty:
        qty = int(m_lead_qty.group(1))
        cleaned = m_lead_qty.group(2).strip()
    elif m_qty:
        m_verb_qty = re.search(r"^(?:can you\s+)?(?:please\s+)?(?:add|put)\s+(\d+)\s+(.+)$", msg_low)
        if m_verb_qty:
            qty = int(m_verb_qty.group(1))
            cleaned = re.sub(r"\s+(?:to|in|into|for)?\s*(?:my|the)?\s*(?:cart|bag|basket)\s*$", "", m_verb_qty.group(2), flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^\s*(?:a|an|the)\s+", "", cleaned, flags=re.IGNORECASE).strip(" .,!?")
    return cleaned, qty


def _fallback(confidence: float = 0.0, language: str = "en", user_message: str = "") -> dict:
    msg_low = str(user_message or "").strip().lower()
    if _is_add_to_cart_query(msg_low):
        prod, qty = _extract_cart_product_and_qty(user_message)
        entities = {"product_name_or_id": prod, "quantity": qty}
        return {
            "intent": "cart_management",
            "action": "add_item",
            "entities": entities,
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": language,
            "message_type": "action",
            "is_action": True,
        }

    if _is_remove_from_cart_query(msg_low):
        prod, qty = _extract_remove_cart_product_and_qty(user_message)
        entities = {"product_name_or_id": prod}
        if qty is not None:
            entities["quantity"] = qty
        return {
            "intent": "cart_management",
            "action": "remove_item",
            "entities": entities,
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": language,
            "message_type": "action",
            "is_action": True,
        }

    if _is_clear_cart_query(msg_low):
        return {
            "intent": "cart_management",
            "action": "clear_cart",
            "entities": {},
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": language,
            "message_type": "action",
            "is_action": True,
        }

    if _is_product_search_query(msg_low):
        query = _extract_search_product_query(user_message)
        return {
            "intent": "product_search",
            "action": "search_products",
            "entities": {"query": query},
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": language,
            "message_type": "normal",
            "is_action": False,
        }

    if _is_availability_inquiry(msg_low):
        clean_p = _extract_availability_product(user_message)
        entities = {"product_title": clean_p} if clean_p else {}
        return {
            "intent": "product_search",
            "action": "check_availability",
            "entities": entities,
            "confidence": 0.90,
            "requires_confirmation": False,
            "language": language,
            "message_type": "normal",
            "is_action": False,
        }

    if _is_return_inquiry(msg_low):
        ord_no = _extract_order_number(user_message)
        entities = {"order_number": ord_no} if ord_no else {}
        return {
            "intent": "order_management",
            "action": "check_return_eligibility",
            "entities": entities,
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": language,
            "message_type": "normal",
            "is_action": False,
        }

    if _is_return_request(msg_low):
        ord_no = _extract_order_number(user_message)
        entities = {"order_number": ord_no} if ord_no else {}
        return {
            "intent": "order_management",
            "action": "request_order_return",
            "entities": entities,
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": language,
            "message_type": "action",
            "is_action": True,
        }

    if _is_active_orders_query(msg_low):
        return {
            "intent": "order_tracking",
            "action": "list_recent_orders",
            "entities": {"filter": "active", "active_only": True},
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": language,
            "message_type": "normal",
            "is_action": False,
        }

    if _is_cancel_order_query(msg_low):
        ord_no = _extract_order_number(user_message)
        entities = {"order_number": ord_no} if ord_no else {}
        return {
            "intent": "order_management",
            "action": "cancel_order",
            "entities": entities,
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": language,
            "message_type": "action",
            "is_action": True,
        }

    return {
        "intent": "fallback",
        "action": "clarify",
        "entities": {},
        "confidence": confidence,
        "requires_confirmation": False,
        "language": language,
        "message_type": "normal",
        "is_action": False,
    }


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------
@observe(name="classify_intent")
def classify_intent(user_message: str, schema: dict | None = None, history: list | None = None) -> dict:
    """Classify a single user message. Returns a dict matching
    classification_output_format from the schema, with requires_confirmation
    filled in from the schema (never trusted from the model's own output),
    and message_type/is_action tagging whether it's an action or normal message."""
    schema = schema or load_schema()

    msg_low = str(user_message or "").strip().lower()

    # Fast heuristic check for direct add-to-cart commands
    if _is_add_to_cart_query(msg_low):
        prod, qty = _extract_cart_product_and_qty(user_message)
        entities = {"product_name_or_id": prod, "quantity": qty}
        return {
            "intent": "cart_management",
            "action": "add_item",
            "entities": entities,
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": "en",
            "message_type": "action",
            "is_action": True,
        }

    # Fast heuristic check for direct remove-from-cart commands
    if _is_remove_from_cart_query(msg_low):
        prod, qty = _extract_remove_cart_product_and_qty(user_message)
        entities = {"product_name_or_id": prod}
        if qty is not None:
            entities["quantity"] = qty
        return {
            "intent": "cart_management",
            "action": "remove_item",
            "entities": entities,
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": "en",
            "message_type": "action",
            "is_action": True,
        }

    # Fast heuristic check for clear cart commands
    if _is_clear_cart_query(msg_low):
        return {
            "intent": "cart_management",
            "action": "clear_cart",
            "entities": {},
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": "en",
            "message_type": "action",
            "is_action": True,
        }

    # Fast heuristic check for product search queries
    if _is_product_search_query(msg_low):
        query = _extract_search_product_query(user_message)
        return {
            "intent": "product_search",
            "action": "search_products",
            "entities": {"query": query},
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": "en",
            "message_type": "normal",
            "is_action": False,
        }

    # Fast heuristic check for product availability inquiries
    if _is_availability_inquiry(msg_low):
        clean_p = _extract_availability_product(user_message)
        entities = {"product_title": clean_p} if clean_p else {}
        return {
            "intent": "product_search",
            "action": "check_availability",
            "entities": entities,
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": "en",
            "message_type": "normal",
            "is_action": False,
        }

    # Contextual order heuristics to avoid greedy/blind tracking
    if _is_return_inquiry(msg_low):
        ord_no = _extract_order_number(user_message)
        entities = {"order_number": ord_no} if ord_no else {}
        return {
            "intent": "order_management",
            "action": "check_return_eligibility",
            "entities": entities,
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": "en",
            "message_type": "normal",
            "is_action": False,
        }

    if _is_return_request(msg_low):
        ord_no = _extract_order_number(user_message)
        entities = {"order_number": ord_no} if ord_no else {}
        return {
            "intent": "order_management",
            "action": "request_order_return",
            "entities": entities,
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": "en",
            "message_type": "action",
            "is_action": True,
        }

    if _is_active_orders_query(msg_low):
        return {
            "intent": "order_tracking",
            "action": "list_recent_orders",
            "entities": {"filter": "active", "active_only": True},
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": "en",
            "message_type": "normal",
            "is_action": False,
        }

    if _is_cancel_order_query(msg_low):
        ord_no = _extract_order_number(user_message)
        entities = {"order_number": ord_no} if ord_no else {}
        return {
            "intent": "order_management",
            "action": "cancel_order",
            "entities": entities,
            "confidence": 0.95,
            "requires_confirmation": False,
            "language": "en",
            "message_type": "action",
            "is_action": True,
        }

    # Optional context: the last few (role, text) turns, so follow-ups like
    # "the second one" can be resolved. Only the latest message is classified.
    user_content = user_message
    if history:
        convo = "\n".join(
            f"{'Shopper' if role == 'user' else 'Assistant'}: {str(text)[:200]}" for role, text in history[-6:]
        )
        user_content = f"Recent conversation (context only):\n{convo}\n\nLatest message to classify:\n{user_message}"

    try:
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
    except Exception as e:
        print(f"intent_classifier: LLM classification failed: {e!r}")
        return _fallback(user_message=user_message)

    result = _parse_json(raw)
    if result is None:
        print(f"intent_classifier: model returned unparseable content; raw={raw!r}")
        return _fallback(user_message=user_message)

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
        return _fallback(confidence, language, user_message=user_message)

    msg_type = result.get("message_type")
    if msg_type not in ("action", "normal", "conversational"):
        msg_type = determine_message_type(intent["name"], action["name"], user_message)
    else:
        msg_type = "normal" if msg_type == "conversational" else msg_type

    is_action = (msg_type == "action")

    return {
        "intent": intent["name"],
        "action": action["name"],
        "entities": _clean_entities(result.get("entities"), intent, action),
        "confidence": confidence,
        "requires_confirmation": bool(action.get("requires_confirmation", False)),
        "language": language,
        "message_type": msg_type,
        "is_action": is_action,
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
