"""reconcile_from_legacy must never move a card OUT of done.

Regression test for the live case observed on 2026-08-17: card b24c71b5 had a
real `complete` event in the store while legacy still said `ready`. Converging
the store onto legacy would have un-completed finished work, and the parity gate
would have gone green because the completion was destroyed. See card be8d5561.
"""

from skcoord import card_store
from skcoord.card_store import _would_uncomplete, reconcile_from_legacy

# diff maps field -> [legacy_value, store_value]
B24C71B5 = {"status": ["ready", "done"], "owner": ["lumina", None]}


class TestWouldUncomplete:
    def test_done_in_store_but_not_legacy_is_an_uncomplete(self):
        assert _would_uncomplete(B24C71B5) is True

    def test_ordinary_forward_drift_is_not_an_uncomplete(self):
        # Legacy is ahead: converging COMPLETES the card, which is fine.
        assert _would_uncomplete({"status": ["done", "doing"]}) is False

    def test_movement_between_open_columns_is_not_an_uncomplete(self):
        assert _would_uncomplete({"status": ["backlog", "ready"]}) is False

    def test_owner_only_diff_is_not_an_uncomplete(self):
        assert _would_uncomplete({"owner": ["lumina", None]}) is False

    def test_both_sides_done_is_not_an_uncomplete(self):
        assert _would_uncomplete({"status": ["done", "done"]}) is False


class TestReconcileSkipsIt:
    def _patch(self, monkeypatch, mismatches):
        monkeypatch.setattr(
            card_store, "parity_check", lambda home: {"mismatches": mismatches}
        )

    def test_skips_the_card_and_reports_it(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, [{"id": "b24c71b5", "diff": B24C71B5}])
        res = reconcile_from_legacy(tmp_path, dry_run=True)
        assert res["would_fix"] == 0
        assert res["skipped_uncomplete"] == ["b24c71b5"]

    def test_skips_the_WHOLE_card_not_just_its_status(self, monkeypatch, tmp_path):
        """The owner half must not be applied either.

        Rewriting owner while leaving status alone would leave the card in a
        state neither side ever held, and would still not converge parity.
        """
        appended = []
        monkeypatch.setattr(
            card_store.CardStore,
            "append_event",
            lambda self, cid, action, writer, **kw: appended.append(action),
        )
        self._patch(monkeypatch, [{"id": "b24c71b5", "diff": B24C71B5}])
        reconcile_from_legacy(tmp_path, dry_run=False)
        assert appended == []

    def test_allow_uncomplete_opts_back_in(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, [{"id": "b24c71b5", "diff": B24C71B5}])
        res = reconcile_from_legacy(tmp_path, dry_run=True, allow_uncomplete=True)
        assert res["would_fix"] == 1
        assert res["skipped_uncomplete"] == []

    def test_safe_cards_still_reconcile_alongside_a_skipped_one(
        self, monkeypatch, tmp_path
    ):
        """One poisoned card must not stall the rest of the repair."""
        self._patch(
            monkeypatch,
            [
                {"id": "b24c71b5", "diff": B24C71B5},
                {"id": "01848907", "diff": {"owner": ["swarm-a21-identity", None]}},
            ],
        )
        res = reconcile_from_legacy(tmp_path, dry_run=True)
        assert res["would_fix"] == 1
        assert res["skipped_uncomplete"] == ["b24c71b5"]
