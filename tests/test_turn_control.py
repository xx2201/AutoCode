import threading

import pytest

from autocode.state import TurnController


def test_turn_controller_validates_expected_turn_and_drains_steer_thread_safely():
    controller = TurnController()
    controller.start_turn("turn-active")
    barrier = threading.Barrier(3)
    received = []

    def send(content):
        barrier.wait()
        received.append(
            controller.steer(content, expected_turn_id="turn-active").message_id
        )

    threads = [threading.Thread(target=send, args=(f"guide-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    items = controller.drain_steer("turn-active")
    assert {item.content for item in items} == {"guide-0", "guide-1"}
    assert {item.message_id for item in items} == set(received)

    with pytest.raises(ValueError, match="active turn is 'turn-active'"):
        controller.steer("wrong target", expected_turn_id="turn-stale")


def test_turn_controller_queue_is_fifo_and_supports_edit_delete():
    controller = TurnController()
    controller.start_turn("turn-active")
    first = controller.queue("first", expected_turn_id="turn-active")
    second = controller.queue("second", expected_turn_id="turn-active")

    updated = controller.update_queued(second.message_id, "second edited")
    assert updated.message_id == second.message_id
    assert [item.content for item in controller.queued()] == ["first", "second edited"]
    assert controller.pop_queued().message_id == first.message_id

    controller.delete_queued(second.message_id)
    assert controller.pop_queued() is None


def test_turn_controller_atomically_finishes_only_without_pending_steer():
    controller = TurnController()
    controller.start_turn("turn-active")
    controller.steer("one more thing", expected_turn_id="turn-active")

    items, finished = controller.drain_steer_or_finish("turn-active")
    assert [item.content for item in items] == ["one more thing"]
    assert finished is False
    assert controller.active_turn_id == "turn-active"

    items, finished = controller.drain_steer_or_finish("turn-active")
    assert items == []
    assert finished is True
    assert controller.active_turn_id == ""


def test_turn_controller_restores_persisted_queue_without_changing_ids():
    first = TurnController()
    first.start_turn("turn-active")
    queued = first.queue("later", expected_turn_id="turn-active")

    restored = TurnController()
    restored.restore_queued([queued.to_dict()])

    item = restored.pop_queued()
    assert item == queued
