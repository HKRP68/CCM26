"""Guard rails on the two admin actions that are hard to undo.

Deleting a player from the Players tab purges the card from every roster, market
slot and pack, refunding its sell value to each owner. It sat behind a one-tap
``confirm()`` next to the Edit button. It now needs the player's name typed back,
checked BOTH in the browser (so the admin is told before anything happens) and on
the server (so a bare POST — a bookmarked URL, a double-submit, a re-sent form —
can't fire it either).

Marking a giveaway participant as a guaranteed winner is the other one: it
decides a payout, so it is refused once the draw has run.

These are source-level checks: importing ``admin`` pulls in the whole Flask app,
its login layer and a live database, which is far more than the assertion needs.
"""

import os
import unittest


def _read(path):
    with open(os.path.join(os.path.dirname(__file__), "..", path)) as fh:
        return fh.read()


class PlayerDeleteConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.admin = _read("admin.py")
        self.route = (self.admin.split("def player_delete(player_id):")[1]
                      .split("\n@app.route")[0])
        self.template = _read("templates/players.html")

    def test_the_server_requires_the_typed_name(self):
        self.assertIn('request.form.get("confirm_name")', self.route)
        self.assertIn("casefold()", self.route)

    def test_the_server_checks_before_it_purges(self):
        # Order matters: the refund/purge must not have run by the time the
        # check fails, or a mistyped name would still have emptied the rosters.
        check = self.route.index("confirm_name")
        purge = self.route.index("_purge_player_references")
        delete = self.route.index("db.delete(player)")
        self.assertLess(check, purge)
        self.assertLess(check, delete)

    def test_a_mismatch_redirects_without_deleting(self):
        guard = self.route.split("confirm_name.casefold()")[1].split("\n\n")[0]
        self.assertIn("return redirect", guard)
        self.assertNotIn("db.delete", guard)

    def test_the_players_tab_prompts_for_the_name(self):
        self.assertIn("confirmPlayerDelete", self.template)
        self.assertIn('name="confirm_name"', self.template)
        self.assertIn('name="expected_name"', self.template)
        # The old one-tap confirm is gone from the delete form.
        delete_form = self.template.split("url_for('player_delete'")[1][:400]
        self.assertNotIn("confirm('Delete", delete_form)

    def test_the_prompt_refuses_a_mismatch_client_side_too(self):
        js = self.template.split("function confirmPlayerDelete(form)")[1][:800]
        self.assertIn("return false", js)
        self.assertIn("trim() !== expected.trim()", js)


class GiveawayPriorityGuardTest(unittest.TestCase):
    def setUp(self):
        self.admin = _read("admin.py")
        self.route = (self.admin.split("def admin_giveaway_toggle_priority(")[1]
                      .split("\n@app.route")[0])
        self.template = _read("templates/admin_giveaway_detail.html")

    def test_priority_cannot_be_changed_after_the_draw(self):
        self.assertIn("winners_drawn_at is not None", self.route)
        guard = self.route.split("winners_drawn_at is not None")[1][:400]
        self.assertIn("return redirect", guard)

    def test_the_entry_must_belong_to_this_giveaway(self):
        # /giveaways/1/entries/<id>/priority must not reach into giveaway 2.
        self.assertIn("entry.giveaway_id != gid", self.route)

    def test_the_action_is_audited(self):
        self.assertIn('log_admin(db, "giveaway_priority"', self.route)

    def test_the_participants_tab_exposes_the_toggle(self):
        self.assertIn("admin_giveaway_toggle_priority", self.template)
        self.assertIn("csrf_token()", self.template)
        # And hides it once the giveaway is decided.
        self.assertIn("draw_open", self.template)


if __name__ == "__main__":
    unittest.main()
