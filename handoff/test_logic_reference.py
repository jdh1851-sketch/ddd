import unittest
from logic_reference import screen_action, screen_stage


class ConfirmedBehaviorTests(unittest.TestCase):
    def test_party_routes_on_same_result_screen(self):
        result={'retry':True,'other_dungeon':True}
        self.assertEqual(screen_action(result,'1인'),'retry')
        self.assertEqual(screen_action(result,'2~3인'),'other_dungeon')
        self.assertEqual(screen_action(result,'4인',party_role='파티장'),'retry')
        self.assertIsNone(screen_action(result,'4인',party_role='파티원'))

    def test_member_ready_and_cancel_priority(self):
        self.assertIsNone(screen_action({'enter':True},'4인',party_role='파티원'))
        self.assertEqual(screen_action({'retry':True,'partyready':True},'4인',party_role='파티원'),'partyready')
        self.assertIsNone(screen_action({'partyready':True,'partycancel':True},'4인',party_role='파티원'))

    def test_no_fallback_to_wrong_retry_route(self):
        self.assertIsNone(screen_action({'retry':True},'2~3인'))
        self.assertIsNone(screen_action({'other_dungeon':True},'1인'))
        self.assertIsNone(screen_action({'other_dungeon':True},'4인'))

    def test_configured_dungeon_only_after_list_evidence(self):
        full={'dungeon_dock':True,'dungeon_cave':True,'dungeon_water':True}
        self.assertEqual(screen_action(full,'2~3인','허상의 정박지'),'dungeon_dock')
        self.assertEqual(screen_action(full,'2~3인','광기의 동굴'),'dungeon_cave')
        self.assertEqual(screen_action(full,'2~3인','흩어진 물길'),'dungeon_water')
        self.assertIsNone(screen_action(full,'1인'))
        self.assertIsNone(screen_action({'dungeon_dock':True},'2~3인'))
        self.assertIsNone(screen_action({'dungeon_cave':True,'dungeon_water':True},'2~3인','허상의 정박지'))

    def test_obsolete_queue_does_not_block_observed_screens(self):
        for marker,stage,action in [('skip','battle','skip'),('touch','result','touch'),('retry','result','retry')]:
            with self.subTest(marker=marker):
                self.assertEqual(screen_stage({marker:True},'queue'),stage)
                self.assertEqual(screen_action({marker:True}),action)
        self.assertEqual(screen_stage({'break':True},'search'),'battle')
        self.assertEqual(screen_stage({'hud':True},'search'),'battle')

    def test_unknown_or_incomplete_modal_does_not_trigger_other_buttons(self):
        self.assertIsNone(screen_action({}))
        self.assertIsNone(screen_action({'challenge_title':True,'enter':True,'retry':True}))
        self.assertEqual(screen_action({'challenge_title':True,'challenge':True,'retry':True}),'challenge')


if __name__=='__main__':
    unittest.main()
