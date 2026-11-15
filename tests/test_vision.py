"""The camera reader and the vision loop: frames out of a stream, and Gemini's answer as boxes.

No camera and no Gemini are used: the stream is bytes made here, and the model's answer is given by the test.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from app import camera, config, llm, state, vision

MILK = "milk_lactose_free"
MYSTERY = "mystery_soda"


def jpeg(body: bytes) -> bytes:
    return camera.SOI + body + camera.EOI


def part(picture: bytes) -> bytes:
    """One picture as the camera sends it, with the stream's own headers around it."""
    return b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + picture + b"\r\n"


def box(product_key: str = MILK, corners: Any = (250, 100, 750, 500), confidence: Any = 0.9,
        count: Any = 1) -> dict[str, Any]:
    return {"product_key": product_key, "box_2d": list(corners) if isinstance(corners, tuple) else corners,
            "count": count, "confidence": confidence}


def boxes(*items: dict[str, Any]) -> list[state.Detection]:
    return vision.to_detections({"detections": list(items)}, {MILK: "Milk", MYSTERY: "Mystery soda"}, state.now())


def test_pictures_are_taken_out_of_the_stream_even_when_cut_across_chunks():
    first, second = jpeg(b"one"), jpeg(b"two")
    stream = part(first) + part(second)
    buffer, found = bytearray(), []
    for start in range(0, len(stream), 7):  # the network cuts the stream wherever it likes
        buffer.extend(stream[start:start + 7])
        found += camera.split_frames(buffer)
    assert found == [first, second]


def test_an_unfinished_picture_waits_for_the_rest():
    buffer = bytearray(part(jpeg(b"whole")) + b"--frame\r\n\r\n" + camera.SOI + b"half")
    assert camera.split_frames(buffer) == [jpeg(b"whole")]
    buffer.extend(b" done" + camera.EOI)
    assert camera.split_frames(buffer) == [jpeg(b"half done")]


def test_the_models_box_becomes_fractions_of_the_picture():
    (found,) = boxes(box(corners=(250, 100, 750, 500), count=3))  # [ymin, xmin, ymax, xmax] out of 1000
    assert found.product_key == MILK and found.name == "Milk" and found.quantity == 3
    assert found.box.model_dump() == {"x": 0.1, "y": 0.25, "w": 0.4, "h": 0.5}


def test_a_box_that_runs_off_the_picture_is_cut_at_the_edge():
    (found,) = boxes(box(corners=(-50, 800, 400, 1200)))
    assert found.box.model_dump() == {"x": 0.8, "y": 0.0, "w": 0.2, "h": 0.4}


@pytest.mark.parametrize("bad", [
    box(product_key="butter"),               # not on the encounter list
    box(confidence=0.2),                     # the model is not sure
    box(corners=(250, 500, 750, 500)),       # no width
    box(corners=[250, 100, 750]),            # not four corners
    box(corners=["a", "b", "c", "d"]),       # not numbers
    box(confidence=None),
    box(count=0),                            # no units in the box
    "not an object",
])
def test_an_answer_that_is_not_a_real_box_is_dropped(bad):
    assert boxes(bad) == []
    assert len(boxes(bad, box())) == 1  # and it does not take a good box down with it


def test_a_look_writes_visible_counts_onto_every_encounter_food(monkeypatch):
    monkeypatch.setattr(camera, "latest_frame", lambda: jpeg(b"shelf"))
    monkeypatch.setattr(llm, "complete_json_with_image",
                        lambda *_: {"detections": [box(count=4), box(corners=(200, 600, 800, 900), count=1)]})
    vision.set_reorder_handler(None)
    for _ in range(vision.COUNT_AGREE):
        vision.look_once()
    by_key = {row["product_key"]: row for row in state.snapshot()["shelf"]}
    assert by_key[MILK]["quantity"] == 5 and by_key[MILK]["level"] == "full" and by_key[MILK]["device_id"] == "vision"
    assert by_key[MILK]["product_id"] == "1001"
    assert by_key[MYSTERY]["quantity"] == 0 and by_key[MYSTERY]["level"] == "empty"
    assert by_key[MYSTERY]["product_id"] is None


def test_a_fractional_count_is_rounded_to_a_whole_number():
    (found,) = boxes(box(count=3.5))
    assert found.quantity == 4  # drinks are whole units
    vision.set_reorder_handler(None)
    foods = config.load_camera_foods()
    for _ in range(vision.COUNT_AGREE):
        vision.apply_shelf_counts([found], foods, state.now())
    by_key = {row["product_key"]: row for row in state.snapshot()["shelf"]}
    assert by_key[MILK]["quantity"] == 4


def test_a_single_missed_look_does_not_change_the_confirmed_count(monkeypatch):
    """Counts only update when several consecutive looks agree on the same restock band."""
    vision.set_reorder_handler(None)
    frames = iter([
        {"detections": [box(count=4)]},
        {"detections": [box(count=4)]},
        {"detections": [box(count=4)]},  # confirmed full at 4
        {"detections": []},               # one miss → still show 4
        {"detections": [box(count=4)]},
    ])
    monkeypatch.setattr(camera, "latest_frame", lambda: jpeg(b"shelf"))
    monkeypatch.setattr(llm, "complete_json_with_image", lambda *_: next(frames))
    for _ in range(3):
        vision.look_once()
    by_key = {row["product_key"]: row for row in state.snapshot()["shelf"]}
    assert by_key[MILK]["quantity"] == 4 and by_key[MILK]["level"] == "full"
    vision.look_once()  # miss
    by_key = {row["product_key"]: row for row in state.snapshot()["shelf"]}
    assert by_key[MILK]["quantity"] == 4 and by_key[MILK]["level"] == "full"
    vision.look_once()  # back to 4; buffer not yet agreed on empty
    by_key = {row["product_key"]: row for row in state.snapshot()["shelf"]}
    assert by_key[MILK]["quantity"] == 4 and by_key[MILK]["level"] == "full"


def test_empty_and_low_counts_agree_so_the_streak_can_advance(monkeypatch):
    """0 (empty) and 1 (low) are the same restock band — they confirm and can reorder."""
    milk_started: list[str] = []

    def capture(records: list) -> None:
        milk_started.extend(record.product_key for record in records if record.product_key == MILK)

    # Alternate empty/low readings: previously these never agreed and the counter stuck.
    frames = iter([
        {"detections": []},
        {"detections": [box(count=1)]},
        {"detections": []},
    ])
    monkeypatch.setattr(camera, "latest_frame", lambda: jpeg(b"shelf"))
    monkeypatch.setattr(llm, "complete_json_with_image", lambda *_: next(frames))
    vision.set_reorder_handler(capture)
    vision.look_once()  # buffer [0]
    assert milk_started == []
    vision.look_once()  # [0,1] agree restock → streak 1 → reorder (CONFIRM_LOOKS=1)
    assert MILK in milk_started
    by_key = {row["product_key"]: row for row in state.snapshot()["shelf"]}
    assert by_key[MILK]["level"] in {"low", "empty"}
    assert by_key[MILK]["armed"] is False


def test_an_answer_in_the_wrong_shape_gives_no_boxes():
    assert vision.to_detections({"detections": "none"}, {MILK: "Milk"}, state.now()) == []
    assert vision.to_detections({}, {MILK: "Milk"}, state.now()) == []


def test_a_look_puts_the_boxes_in_the_dashboard_state(monkeypatch):
    seen = SimpleNamespace(image=None, question="")

    def answer(system: str, user: str, schema: dict[str, Any], image: bytes) -> dict[str, Any]:
        seen.image, seen.question = image, user
        return {"detections": [box(count=4)]}

    monkeypatch.setattr(camera, "latest_frame", lambda: jpeg(b"shelf"))
    monkeypatch.setattr(llm, "complete_json_with_image", answer)
    vision.set_reorder_handler(None)
    vision.look_once()
    assert seen.image == jpeg(b"shelf") and MILK in seen.question and MYSTERY in seen.question
    (shown,) = state.snapshot()["detections"]
    assert shown["product_key"] == MILK and shown["quantity"] == 4
    assert shown["box"] == {"x": 0.1, "y": 0.25, "w": 0.4, "h": 0.5}


def test_a_drop_from_full_needs_several_low_looks_before_reorder(monkeypatch):
    milk_started: list[str] = []

    def capture(records: list) -> None:
        milk_started.extend(record.product_key for record in records if record.product_key == MILK)

    # COUNT_AGREE full looks to confirm full, then COUNT_AGREE lows for the first confirmed
    # low (streak 1), then CONFIRM_LOOKS-1 more low looks to reach the reorder threshold.
    lows_needed = vision.COUNT_AGREE + vision.CONFIRM_LOOKS - 1
    frames = iter(
        [{"detections": [box(count=4)]}] * vision.COUNT_AGREE
        + [{"detections": [box(count=1)]}] * (lows_needed + 1)  # +1 still-low after reorder
    )
    monkeypatch.setattr(camera, "latest_frame", lambda: jpeg(b"shelf"))
    monkeypatch.setattr(llm, "complete_json_with_image", lambda *_: next(frames))
    vision.set_reorder_handler(capture)
    looks_before_fire = vision.COUNT_AGREE + lows_needed  # fires on this many-th look
    for _ in range(looks_before_fire - 1):
        vision.look_once()
        assert milk_started == []
    vision.look_once()
    assert milk_started == [MILK]
    milk_started.clear()
    vision.look_once()
    assert milk_started == []


def test_a_full_look_mid_streak_resets_before_reorder(monkeypatch):
    milk_started: list[str] = []

    def capture(records: list) -> None:
        milk_started.extend(record.product_key for record in records if record.product_key == MILK)

    # Confirm full, one low (buffer disagrees → stay full), full again, then confirmed lows reorder.
    lows_needed = vision.COUNT_AGREE + vision.CONFIRM_LOOKS - 1
    frames = iter(
        [{"detections": [box(count=4)]}] * vision.COUNT_AGREE
        + [{"detections": [box(count=1)]}]  # one low — does not confirm yet
        + [{"detections": [box(count=4)]}] * vision.COUNT_AGREE  # full again
        + [{"detections": [box(count=1)]}] * lows_needed
    )
    monkeypatch.setattr(camera, "latest_frame", lambda: jpeg(b"shelf"))
    monkeypatch.setattr(llm, "complete_json_with_image", lambda *_: next(frames))
    vision.set_reorder_handler(capture)
    before_reorder = vision.COUNT_AGREE + 1 + vision.COUNT_AGREE + lows_needed
    for _ in range(before_reorder - 1):
        vision.look_once()
        assert milk_started == []
    vision.look_once()
    assert milk_started == [MILK]


def test_empty_from_the_start_orders_after_confirmed_lows(monkeypatch):
    started: list[str] = []

    def capture(records: list) -> None:
        started.extend(record.product_key for record in records)

    # Mystery soda is never in frame → empty. No prior full is required; confirmed lows reorder.
    # Milk stays full in every frame so only mystery should fire.
    lows_needed = vision.COUNT_AGREE + vision.CONFIRM_LOOKS - 1
    frames = iter([{"detections": [box(count=4)]}] * (lows_needed + 1))
    monkeypatch.setattr(camera, "latest_frame", lambda: jpeg(b"shelf"))
    monkeypatch.setattr(llm, "complete_json_with_image", lambda *_: next(frames))
    vision.set_reorder_handler(capture)
    for _ in range(lows_needed - 1):
        vision.look_once()
        assert MYSTERY not in started
    vision.look_once()
    assert MYSTERY in started
    by_key = {row["product_key"]: row for row in state.snapshot()["shelf"]}
    assert by_key[MYSTERY]["level"] == "empty" and by_key[MYSTERY]["armed"] is False


def test_no_video_means_no_boxes_and_no_question_to_the_model(monkeypatch):
    state.set_detections(boxes(box()))
    monkeypatch.setattr(camera, "latest_frame", lambda: None)
    monkeypatch.setattr(llm, "complete_json_with_image", lambda *_: pytest.fail("the model was asked without a picture"))
    vision.look_once()
    assert state.snapshot()["detections"] == []


def test_a_failed_answer_leaves_the_last_boxes_in_place(monkeypatch):
    state.set_detections(boxes(box()))

    def refuse(*_: Any) -> dict[str, Any]:
        raise llm.LLMError("The language model took too long to answer.")

    monkeypatch.setattr(camera, "latest_frame", lambda: jpeg(b"shelf"))
    monkeypatch.setattr(llm, "complete_json_with_image", refuse)
    with pytest.raises(llm.LLMError):
        vision.look_once()
    assert len(state.snapshot()["detections"]) == 1


def test_the_picture_is_sent_to_the_model_with_the_question(monkeypatch):
    sent = {}

    def generate_content(**call: Any) -> SimpleNamespace:
        sent.update(call)
        return SimpleNamespace(text='{"detections": []}')

    monkeypatch.setattr(llm, "_gemini", lambda: SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    assert llm.complete_json_with_image("system", "Where is the milk?", vision.SCHEMA, jpeg(b"shelf")) == {"detections": []}
    picture, question = sent["contents"]
    assert isinstance(picture, types.Part) and picture.inline_data.data == jpeg(b"shelf")
    assert picture.inline_data.mime_type == "image/jpeg" and question == "Where is the milk?"


def test_reorder_below_controls_when_a_count_is_low_enough_to_order(monkeypatch):
    """A higher reorder_below means a count that used to be 'full' now triggers a reorder."""
    from decimal import Decimal

    milk_started: list[str] = []

    def capture(records: list) -> None:
        milk_started.extend(record.product_key for record in records if record.product_key == MILK)

    state.shelf[MILK].reorder_below = Decimal(5)
    # Persist so _ensure_shelf_row does not overwrite the live threshold from the file.
    config.update_camera_food_reorder(MILK, reorder_below=Decimal(5), reorder_quantity=Decimal(24))

    frames = iter([{"detections": [box(count=4)]}] * vision.COUNT_AGREE)
    monkeypatch.setattr(camera, "latest_frame", lambda: jpeg(b"shelf"))
    monkeypatch.setattr(llm, "complete_json_with_image", lambda *_: next(frames))
    vision.set_reorder_handler(capture)
    for _ in range(vision.COUNT_AGREE):
        vision.look_once()
    assert MILK in milk_started
    by_key = {row["product_key"]: row for row in state.snapshot()["shelf"]}
    assert by_key[MILK]["level"] == "low" and by_key[MILK]["quantity"] == 4
