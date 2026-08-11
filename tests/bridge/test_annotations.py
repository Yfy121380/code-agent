"""Response annotation tests cover source validation and prompt rendering."""

import pytest

from codemate.bridge.annotations import (
    render_annotated_request,
    response_content_hash,
    validate_response_annotations,
)


def final_message(content="The compact threshold is 90%."):
    return {
        "id": "final-1",
        "role": "assistant",
        "kind": "final",
        "conversation_id": "turn-1",
        "content": content,
    }


def annotation_for(message, **overrides):
    value = {
        "id": "annotation-1",
        "source_message_id": message["id"],
        "source_content_hash": response_content_hash(message["content"]),
        "selected_text": "90%",
        "surrounding_text": message["content"],
        "comment": "Would 85% leave more room?",
    }
    value.update(overrides)
    return value


def test_annotation_must_reference_an_unchanged_final_answer():
    message = final_message()

    normalized = validate_response_annotations([annotation_for(message)], [message])

    assert normalized[0]["conversation_id"] == "turn-1"
    with pytest.raises(ValueError, match="source message changed"):
        validate_response_annotations(
            [annotation_for(message, source_content_hash="stale")],
            [message],
        )
    with pytest.raises(ValueError, match="does not reference a final answer"):
        validate_response_annotations(
            [annotation_for(message)],
            [{**message, "kind": "commentary"}],
        )


def test_annotation_comment_and_latest_request_are_optional():
    message = final_message()
    annotation = validate_response_annotations(
        [annotation_for(message, comment="")],
        [message],
    )

    rendered = render_annotated_request("", annotation)

    assert "Annotation 1:" in rendered
    assert "The user did not add a comment" in rendered
    assert "The user provided no additional request" in rendered
    assert message["content"] in rendered


def test_annotated_request_uses_real_values_and_requires_numbered_answers():
    message = final_message("Earlier context with a real value.")
    annotation = validate_response_annotations(
        [
            annotation_for(
                message,
                selected_text="real value",
                surrounding_text=message["content"],
                comment="Explain this value.",
            )
        ],
        [message],
    )

    rendered = render_annotated_request("Continue with the design.", annotation)

    assert "use exactly these labels" in rendered
    assert "Annotation 1:" in rendered
    assert "[Response to annotation 1]" not in rendered
    assert "> Earlier context with a real value." in rendered
    assert "> real value" in rendered
    assert "> Explain this value." in rendered
    assert "Latest user request:\n\nContinue with the design." in rendered
