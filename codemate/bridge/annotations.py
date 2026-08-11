"""Response annotation validation and model-visible request rendering.

The editor identifies an earlier final answer by its durable transcript message
ID. This module keeps those transport details away from the model: it verifies
the source message, then renders the selected excerpt and optional user comment
as ordinary natural-language context.
"""

from __future__ import annotations

import hashlib


MAX_RESPONSE_ANNOTATIONS = 10
MAX_SELECTED_TEXT_CHARS = 2_000
MAX_SURROUNDING_TEXT_CHARS = 3_000
MAX_ANNOTATION_COMMENT_CHARS = 2_000


def response_content_hash(content):
    """Return the stable digest used to bind an editor selection to a message."""
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def validate_response_annotations(raw_annotations, transcript):
    """Validate editor annotations against durable assistant final messages."""
    if raw_annotations is None:
        return []
    if not isinstance(raw_annotations, list):
        raise ValueError("ask.response_annotations must be an array")
    if len(raw_annotations) > MAX_RESPONSE_ANNOTATIONS:
        raise ValueError(
            f"at most {MAX_RESPONSE_ANNOTATIONS} response annotations are allowed"
        )

    final_messages = {
        str(item.get("id") or ""): item
        for item in transcript or []
        if isinstance(item, dict)
        and str(item.get("role") or "") == "assistant"
        and str(item.get("kind") or "") == "final"
        and str(item.get("id") or "")
    }
    normalized = []
    for index, raw in enumerate(raw_annotations, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"response annotation {index} must be an object")
        message_id = str(raw.get("source_message_id") or "").strip()
        source = final_messages.get(message_id)
        if source is None:
            raise ValueError(
                f"response annotation {index} does not reference a final answer"
            )

        expected_hash = response_content_hash(source.get("content"))
        supplied_hash = str(raw.get("source_content_hash") or "").strip()
        if supplied_hash != expected_hash:
            raise ValueError(f"response annotation {index} source message changed")

        selected_text = str(raw.get("selected_text") or "").strip()
        surrounding_text = str(raw.get("surrounding_text") or "").strip()
        comment = str(raw.get("comment") or "").strip()
        if not selected_text:
            raise ValueError(f"response annotation {index} selection is empty")
        if len(selected_text) > MAX_SELECTED_TEXT_CHARS:
            raise ValueError(
                f"response annotation {index} selection exceeds "
                f"{MAX_SELECTED_TEXT_CHARS} characters"
            )
        if not surrounding_text:
            surrounding_text = selected_text
        if len(surrounding_text) > MAX_SURROUNDING_TEXT_CHARS:
            raise ValueError(
                f"response annotation {index} context exceeds "
                f"{MAX_SURROUNDING_TEXT_CHARS} characters"
            )
        if len(comment) > MAX_ANNOTATION_COMMENT_CHARS:
            raise ValueError(
                f"response annotation {index} comment exceeds "
                f"{MAX_ANNOTATION_COMMENT_CHARS} characters"
            )
        normalized.append(
            {
                "id": str(raw.get("id") or f"annotation_{index}").strip()
                or f"annotation_{index}",
                "source_message_id": message_id,
                "conversation_id": str(source.get("conversation_id") or ""),
                "source_content_hash": expected_hash,
                "selected_text": selected_text,
                "surrounding_text": surrounding_text,
                "comment": comment,
            }
        )
    return normalized


def render_annotated_request(user_request, annotations):
    """Build one user request that models can understand without protocol lore."""
    text = str(user_request or "").strip()
    annotations = list(annotations or [])
    if not annotations:
        return text

    labels = "\n\n".join(
        f"Annotation {index}:" for index in range(1, len(annotations) + 1)
    )
    sections = [
        "The user has added annotations to text selected from your earlier responses.\n\n"
        "Each annotation contains an excerpt from an earlier response, the exact "
        "text selected by the user, and optionally a user comment.\n\n"
        "Use every annotation when answering. Address the annotations in their "
        "original order and use exactly these labels:\n\n"
        f"{labels}\n\n"
        "Keep the numbering unchanged. Do not omit an annotation. If an annotation "
        "has no user comment, treat the selected text itself as the point the user "
        "wants you to reconsider, clarify, or respond to.\n\n"
        "Response annotations:"
    ]
    for index, annotation in enumerate(annotations, start=1):
        section = [
            f"Annotation {index}",
            "In an earlier response, you wrote:",
            _markdown_quote(annotation["surrounding_text"]),
            "The user selected:",
            _markdown_quote(annotation["selected_text"]),
        ]
        if annotation.get("comment"):
            section.extend(
                [
                    "The user's comment is:",
                    _markdown_quote(annotation["comment"]),
                ]
            )
        else:
            section.append(
                "The user did not add a comment. The selected text itself is the "
                "point to address."
            )
        sections.append("\n\n".join(section))

    if text:
        sections.append(f"Latest user request:\n\n{text}")
    else:
        sections.append(
            "The user provided no additional request. Respond directly to the annotations."
        )
    return "\n\n".join(sections)


def _markdown_quote(text):
    return "\n".join(f"> {line}" if line else ">" for line in str(text).splitlines())
