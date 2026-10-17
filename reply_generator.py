"""
Turns structured action results (order data, product lists, policy text,
etc.) into a natural-sounding reply in the user's own language.

Keeps the two concerns separate on purpose:
  - intent_classifier.py decides WHAT the user wants and detects their language
  - reply_generator.py decides HOW to phrase the answer, in that language

This means your Shopify data layer never needs to know or care about
language at all — it just returns structured data, and this module handles
the human-facing phrasing.

RAG integration: when rag_context is passed in (a list of {"content", "score",
"document"} dicts from rag_retriever.retrieve_context), it's injected into the
prompt as grounding material, and the model is instructed to answer only from
it — this keeps knowledge-base-backed answers factual instead of relying on
the model's own training data.
"""

import os
import json
import re
import llm_selector
try:
    import rag_retriever
except ImportError:
    rag_retriever = None

try:
    from langfuse import observe, get_client
except ImportError:
    def observe(*args, **kwargs):
        def decorator(f):
            return f
        return decorator
    def get_client(*args, **kwargs):
        return None



_client = None

# Qwen3-family models can emit an internal reasoning block wrapped in
# <think>...</think> before the actual answer when "thinking mode" is on.
# Strip it out so only the final, user-facing reply ever reaches the widget.
#  primary model : Qwen/Qwen3-8B-AWQ 
_THINK_RE = re.compile(r"<(?:think|thought)>.*?</(?:think|thought)>", re.DOTALL | re.IGNORECASE)    # regular expression to handle text in think tag as well as thought tag

def _strip_thinking(text: str) -> str:
    if not text:
        return ""
    return _THINK_RE.sub("", text).strip()


LANGUAGE_NAMES = {
    "en": "English",
    "hi": "Hindi",
    "pa": "Punjabi",
    "ta": "Tamil",
    "mr": "Marathi",
    "bn": "Bengali",
    "te": "Telugu",
    "gu": "Gujarati",
    "kn": "Kannada",
    "es": "Spanish",
    "fr": "French",
    "ar": "Arabic",
    "de": "German",
    "zh": "Chinese",
    "ja": "Japanese",
    "pt": "Portuguese",
}


def _format_rag_context(rag_context: list[dict]) -> str:
    """Turns retrieved chunks into a numbered block for the prompt."""
    if not rag_context:
        return ""
    parts = []
    for i, chunk in enumerate(rag_context, start=1):
        content = chunk.get("content", "").strip()
        if content:
            parts.append(f"[{i}] {content}")
    return "\n\n".join(parts)

@observe(name="generate_reply")
def generate_reply(
    action_name: str,
    data: dict,
    language: str,
    original_message: str,
    store_identifier=None, needs_rag=False,
    rag_context: list[dict] | None = None,
    rag_query: str | None = None,   # NEW — defaults to original_message if not given

) -> str:
    """
    action_name: e.g. "track_order", "search_products"
    data: whatever your Shopify data layer returned (dict/list/etc.)
    language: ISO 639-1 code from the classifier, e.g. "hi"
    original_message: the user's original message, for tone/context
    rag_context: optional list of retrieved chunks from rag_retriever.retrieve_context(),
                 e.g. [{"content": "...", "score": 0.87, "document": "policy.txt"}, ...]
    """
    if rag_context is None and needs_rag:
        if not store_identifier:
            print("reply_generator: needs_rag=True but no store_identifier given, skipping retrieval")
        elif rag_retriever:
            try:
                rag_context = rag_retriever.retrieve_context(store_identifier, rag_query or original_message)
            except Exception as e:
                print(f"reply_generator: rag retrieval failed: {e!r}")
                rag_context = None
            
    language_name = LANGUAGE_NAMES.get(language, language)

    rag_context_text = _format_rag_context(rag_context) if rag_context else ""
    rag_clause = ""
    if rag_context_text:
        rag_clause = (
            " You have been given reference material retrieved from the store's knowledge base "
            "below. Base your answer ONLY on that material — do not use outside knowledge or make "
            "assumptions beyond what it states. If the material doesn't contain enough information "
            "to answer, say so honestly and suggest the user contact support instead of guessing."
        )

    tracking_clause = ""
    if action_name == "track_order":
        tracking_clause = (
            " This is an order tracking reply — explicitly and clearly state three "
            "things when they're present in the data: the shipment's current status "
            "(e.g. in transit, out for delivery, delivered), its current location "
            "(current_location), and the estimated delivery date (estimated_delivery). "
            "Don't bury these in vague phrasing — name them plainly, each in its own "
            "clause. If current_location is missing or null, say the status clearly "
            "anyway and mention that a precise location isn't available yet, offering "
            "the tracking link (tracking_url) so the shopper can check directly with "
            "the carrier. Never state a location, status, or delivery date that isn't "
            "literally present in the data."
        )

    extra_clause = ""
    if action_name in ("list_recent_orders", "get_my_orders"):
        extra_clause = (
            " When listing orders, summarize them clearly with order number (e.g. #1001), "
            "date, status, items with quantities, and total price. If no orders exist, mention that politely."
        )
    elif action_name == "submit_claim":
        extra_clause = (
            " State the warranty claim ID (claim_id), the order number, the recorded issue, "
            "and reassure the customer that the claim is under review with a 24-48 business hours response window."
        )
    elif action_name == "check_claim_status":
        extra_clause = (
            " Clearly state the current warranty claim status and review details from the data."
        )
    elif action_name == "answer_policy_question":
        extra_clause = (
            " Answer the customer's policy question directly, concisely, and accurately based on the policy body provided."
        )
    elif action_name == "initiate_payment":
        extra_clause = (
            " Confirm that the shopper is being taken to the secure checkout with their cart items. "
            "Never ask for card numbers or payment credentials in chat."
        )
    elif action_name == "confirm_payment_otp":
        extra_clause = (
            " Enthusiastically confirm that the payment was successful, mentioning the order number, total amount paid, "
            "payment method, and estimated 3-5 business days delivery timeframe."
        )
    elif action_name == "search_products":
        extra_clause = (
            " If products are listed in the results, present them enthusiastically with their names and prices. "
            "If 'no_exact_match' is true in the data, gently explain that the exact term wasn't found, but warmly "
            "showcase the featured products provided from the store catalog. Never claim the store has no items "
            "when products are present in the results."
        )
    elif action_name == "assistant_help":
        extra_clause = (
            " Warmly welcome the shopper to the store, introduce what you can do (search products, manage cart, "
            "track orders, file warranty claims, check store policies, and fast OTP checkout), and invite them to explore."
        )
    elif action_name in ("recommend_products", "get_recommendations"):
        extra_clause = (
            " Enthusiastically recommend the products provided in the data, highlighting their names and prices, "
            "and offer to add any item to their cart."
        )
    elif action_name == "add_item":
        extra_clause = (
            " Confirm clearly that the requested product and exact quantity (quantity) have been added to the cart."
        )
    elif action_name == "remove_item":
        extra_clause = (
            " When removing an item, if a specific quantity was removed (quantity_removed) and units still remain in the cart (remaining_quantity > 0), "
            "explicitly confirm that you removed that specific quantity and state how many units remain. "
            "Only claim the item was completely removed or that the cart is empty if remaining_quantity is 0."
        )
    elif action_name == "clarify":
        extra_clause = (
            " Keep the shopper engaged and interested! Warmly ask how you can assist them today with browsing items, "
            "tracking an order, managing their cart, or checking store policies."
        )
    elif action_name == "subscribe_restock":
        extra_clause = (
            " Confirm cheerfully that the shopper is subscribed to restock / price alerts for the product, "
            "mentioning their email and that they'll be notified the moment it's available."
        )
    elif action_name == "search_by_image":
        extra_clause = (
            " Enthusiastically introduce the matching products found for the shopper's uploaded photo, "
            "highlighting their styles, colors, and prices."
        )
    elif action_name == "build_goal_bundle":
        extra_clause = (
            " Present the curated multi-item look/bundle breakdown with items, total price, and budget comparison clearly, "
            "and invite them to add the complete look to their cart with 1 click."
        )

    system_prompt = (
        f"You are a friendly Shopify store assistant. Reply ONLY in {language_name} "
        f"({language}), regardless of what language this instruction is written in. "
        "Keep the reply short, warm, and easy to understand for a non-technical user. "
        "Use the structured data given to you as the source of truth — do not invent "
        f"details that aren't in it.{rag_clause}{tracking_clause}{extra_clause} If the data indicates "
        "an error or empty result, say so gently and suggest what the user could try next."
    )

    user_prompt_parts = [
        f"User's original message: {original_message}",
        f"Action performed: {action_name}",
        f"Result data:\n{json.dumps(data, ensure_ascii=False, indent=2)}",
    ]
    if rag_context_text:
        user_prompt_parts.append(f"Retrieved reference material:\n{rag_context_text}")
    user_prompt_parts.append(f"Write the reply in {language_name}.")
    user_prompt = "\n\n".join(user_prompt_parts)

    response = llm_selector.create_chat_completion(
        temperature=0.4,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )

    #  Before adding llm_selector.py and its create_chat_completion function

    # response = _get_client().chat.completions.create(
    #     model=MODEL,
    #     temperature=0.4,
    #     messages=[
    #         {"role": "system", "content": system_prompt},
    #         {"role": "user", "content": user_prompt},
    #     ],
    #     extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    # )

    return _strip_thinking(response.choices[0].message.content)


if __name__ == "__main__":
    # Quick manual test — run: python reply_generator.py
    sample_data = {
        "order_id": "1042",
        "status": "shipped",
        "carrier": "FedEx",
        "estimated_delivery": "Thursday",
    }
    for lang in ["en", "hi", "es"]:
        reply = generate_reply("track_order", sample_data, lang, "Where is my order?")
        print(f"\n[{lang}] {reply}")

    # Example of a RAG-backed policy answer:
    # from rag_retriever import retrieve_context
    # chunks = retrieve_context("what is your return policy")
    # reply = generate_reply(
    #     "answer_policy_question", {}, "en",
    #     "What is your return policy?", rag_context=chunks
    # )
    # print(reply)
