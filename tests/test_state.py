"""Tests for persistent watch state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from solana_monitor.domain.errors import StateError
from solana_monitor.monitoring.state import BoundedOrderedSet, WatchState, WatchStore
from tests import factories as f

CHAT_ID = -1001234567890


# -- BoundedOrderedSet -------------------------------------------------------
def test_bounded_set_reports_novelty_and_evicts_oldest() -> None:
    bounded = BoundedOrderedSet(2)

    assert bounded.add("a") is True
    assert bounded.add("a") is False
    bounded.add("b")
    bounded.add("c")

    assert "a" not in bounded
    assert bounded.to_list() == ["b", "c"]
    assert len(bounded) == 2


def test_bounded_set_rejects_nonsense_sizes() -> None:
    with pytest.raises(ValueError, match="max_size"):
        BoundedOrderedSet(0)


# -- WatchState --------------------------------------------------------------
def test_mark_seen_reports_first_occurrence() -> None:
    state = WatchState(chat_id=CHAT_ID, wallet=f.WATCHED_WALLET)

    assert state.mark_seen(f.TOKEN_MINT) is True
    assert state.mark_seen(f.TOKEN_MINT) is False
    assert state.is_seen(f.TOKEN_MINT) is True


def test_prime_seeds_baseline_without_alerting() -> None:
    state = WatchState(chat_id=CHAT_ID, wallet=f.WATCHED_WALLET)

    state.prime([f.TOKEN_MINT, f.SECOND_MINT], "cursor")

    assert state.primed is True
    assert state.cursor_signature == "cursor"
    assert state.is_seen(f.TOKEN_MINT) is True
    assert state.mark_seen(f.TOKEN_MINT) is False
    assert len(state.baseline_mints) == 2


def test_reset_for_wallet_clears_per_wallet_history() -> None:
    state = WatchState(chat_id=CHAT_ID, wallet=f.WATCHED_WALLET, alerts_sent=7, monitoring=True)
    state.mark_seen(f.TOKEN_MINT)
    state.prime([f.TOKEN_MINT], "cursor")

    state.reset_for_wallet(f.OTHER_WALLET)

    assert state.wallet == f.OTHER_WALLET
    assert state.monitoring is False
    assert state.primed is False
    assert state.cursor_signature is None
    assert state.alerts_sent == 0
    assert len(state.seen_mints) == 0
    assert len(state.baseline_mints) == 0


def test_state_round_trips_through_a_dict() -> None:
    state = WatchState(chat_id=CHAT_ID, wallet=f.WATCHED_WALLET, monitoring=True)
    state.mark_seen(f.TOKEN_MINT)
    state.mark_processed("sig-1")
    state.prime([f.SECOND_MINT], "cursor")
    state.alerts_sent = 4

    restored = WatchState.from_dict(state.to_dict(), max_seen=5000, max_processed=2000)

    assert restored.chat_id == CHAT_ID
    assert restored.wallet == f.WATCHED_WALLET
    assert restored.monitoring is True
    assert restored.cursor_signature == "cursor"
    assert restored.alerts_sent == 4
    assert restored.is_seen(f.TOKEN_MINT) is True
    assert restored.is_processed("sig-1") is True
    assert restored.baseline_mints == {f.SECOND_MINT}


def test_from_dict_tolerates_junk_fields() -> None:
    state = WatchState.from_dict(
        {
            "chat_id": CHAT_ID,
            "wallet": f.WATCHED_WALLET,
            "seen_mints": "not-a-list",
            "processed_signatures": [1, "sig-2"],
            "created_at": "not-a-date",
        },
        max_seen=10,
        max_processed=10,
    )

    assert state.is_processed("sig-2") is True
    assert state.is_processed("1") is False
    assert state.created_at.tzinfo is not None


# -- WatchStore --------------------------------------------------------------
async def test_store_persists_watches(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    store = WatchStore(path)

    await store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    await store.set_monitoring(CHAT_ID, True)

    reloaded = WatchStore(path)
    reloaded.load()

    state = reloaded.require(CHAT_ID)
    assert state.wallet == f.WATCHED_WALLET
    assert state.monitoring is True
    assert reloaded.monitoring_chat_ids == (CHAT_ID,)


async def test_store_survives_a_restart_without_re_alerting(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = WatchStore(path)
    state = await store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    state.mark_seen(f.TOKEN_MINT)
    state.prime([f.SECOND_MINT], "cursor")
    await store.save()

    reloaded = WatchStore(path)
    reloaded.load()

    restored = reloaded.require(CHAT_ID)
    assert restored.is_seen(f.TOKEN_MINT) is True
    assert restored.mark_seen(f.TOKEN_MINT) is False
    assert restored.cursor_signature == "cursor"


async def test_setting_a_new_wallet_resets_state(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "state.json")
    first = await store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    first.mark_seen(f.TOKEN_MINT)
    first.prime([f.TOKEN_MINT], "cursor")

    second = await store.set_wallet(CHAT_ID, f.OTHER_WALLET)

    assert second.wallet == f.OTHER_WALLET
    assert second.is_seen(f.TOKEN_MINT) is False


def test_missing_state_file_is_not_an_error(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "absent.json")

    store.load()

    assert store.all() == ()


def test_corrupt_state_file_is_quarantined_and_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = WatchStore(path)

    store.load()

    assert store.all() == ()
    assert not path.exists()
    assert (tmp_path / "state.json.corrupt").exists()


def test_structurally_wrong_state_file_is_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    store = WatchStore(path)

    store.load()

    assert store.all() == ()


def test_individual_broken_entries_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "watches": [
                    {"chat_id": CHAT_ID, "wallet": f.WATCHED_WALLET},
                    {"chat_id": "not-an-int", "wallet": "x"},
                ],
            }
        ),
        encoding="utf-8",
    )
    store = WatchStore(path)

    store.load()

    assert [state.chat_id for state in store.all()] == [CHAT_ID]


async def test_require_raises_for_unknown_chats(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "state.json")

    with pytest.raises(StateError, match="no wallet configured"):
        store.require(CHAT_ID)


async def test_remove_deletes_a_watch(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "state.json")
    await store.set_wallet(CHAT_ID, f.WATCHED_WALLET)

    assert await store.remove(CHAT_ID) is True
    assert await store.remove(CHAT_ID) is False
    assert store.get(CHAT_ID) is None


async def test_saving_leaves_no_temporary_files(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "state.json")
    await store.set_wallet(CHAT_ID, f.WATCHED_WALLET)

    assert [path.name for path in tmp_path.iterdir()] == ["state.json"]


def test_unwritable_location_raises_a_state_error(tmp_path: Path) -> None:
    # A directory can never be replaced by a state file, so the write must fail loudly.
    store = WatchStore(tmp_path)

    with pytest.raises(StateError, match="cannot write state file"):
        store._write_atomic("{}")


def test_caps_are_applied_on_load(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "watches": [
                    {
                        "chat_id": CHAT_ID,
                        "wallet": f.WATCHED_WALLET,
                        "seen_mints": ["a", "b", "c", "d"],
                        "processed_signatures": [f"sig-{index}" for index in range(5)],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = WatchStore(path, max_seen_mints=2, max_processed_signatures=2)
    store.load()

    state = store.require(CHAT_ID)
    assert state.seen_mints.to_list() == ["c", "d"]
    assert len(state.processed_signatures) == 2


def test_timestamps_are_serialised_as_iso_strings() -> None:
    state = WatchState(
        chat_id=CHAT_ID,
        wallet=f.WATCHED_WALLET,
        created_at=datetime(2026, 9, 27, tzinfo=UTC),
        updated_at=datetime(2026, 9, 27, tzinfo=UTC),
    )

    serialised = state.to_dict()

    assert serialised["created_at"] == "2026-09-27T00:00:00+00:00"
