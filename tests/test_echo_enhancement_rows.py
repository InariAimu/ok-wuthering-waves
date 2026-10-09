import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
from ok.feature.Box import Box

from src.echo.api import EnhancementAction
from src.echo.optimizer import EchoIdentity
from src.echo.simulation import EchoRoll, EchoState, EchoStat
from src.task.EnhanceEchoTask import EnhanceEchoTask


def token(text, x, y):
    return dict(text=text, x=x, y=y, width=70, height=14)


class TestEnhancementRows(unittest.TestCase):
    def setUp(self):
        self.previous = EchoState((
            EchoRoll.from_value(EchoStat.CRIT_RATE, 10.5),
            EchoRoll.from_value(EchoStat.HP_PERCENT, 10.1),
            EchoRoll.from_value(EchoStat.ENERGY_REGEN, 9.2),
        ))
        self.tokens = [
            token('暴击', 180, 250), token('10.5%', 410, 250),
            token('生命', 180, 280), token('10.1%', 410, 280),
            token('共鸣效率', 180, 310), token("'9.2%", 410, 310),
            token('暴击伤害', 180, 340), token('16.2%', 410, 340),
            token('强化至+25并激活辅音属性', 180, 370),
        ]

    def test_leading_noise_does_not_shift_substat_values(self):
        state = EnhanceEchoTask._model_state_from_ocr_tokens(self.tokens, self.previous)
        self.assertEqual(state, self.previous.add(EchoRoll.from_value(EchoStat.CRIT_DAMAGE, 16.2)))

    def test_missing_value_is_rejected_without_borrowing_next_row(self):
        del self.tokens[5]
        with self.assertRaisesRegex(ValueError, 'exactly one new substat'):
            EnhanceEchoTask._model_state_from_ocr_tokens(self.tokens, self.previous)

    def test_previous_roll_change_is_rejected(self):
        self.tokens[1]['text'] = '9.3%'
        with self.assertRaisesRegex(ValueError, 'does not extend'):
            EnhanceEchoTask._model_state_from_ocr_tokens(self.tokens, self.previous)

    def test_resume_uses_verified_state_without_selecting_a_fresh_echo(self):
        identity = EchoIdentity('融躯战士', 1, 'cost_1:attack_percent')
        state = self.previous.add(EchoRoll.from_value(EchoStat.CRIT_DAMAGE, 16.2)).add(
            EchoRoll.from_value(EchoStat.ATTACK_PERCENT, 7.9))
        session = SimpleNamespace(candidate_pool=(identity,),
            advise=Mock(return_value=SimpleNamespace(action=EnhancementAction.KEEP_AND_LOCK, replacement_slot='1C-2')),
            record_keep=Mock())
        task = SimpleNamespace(info_set=Mock(), _prepare_enhancement_runtime=Mock(),
            _echo_enhancement_session=session, _publish_enhancement_decision=Mock(),
            lock_and_esc=Mock(), _candidate_limit_reached=lambda: True,
            info_get=lambda _: 1, log_info=Mock(),
            find_echo_enhance=Mock(side_effect=AssertionError('must not reselect')))
        EnhanceEchoTask.run(task, resume_candidate=(identity, state))
        session.advise.assert_called_once_with(identity, state)
        session.record_keep.assert_called_once_with(identity, state, '1C-2')
        task.lock_and_esc.assert_called_once()

    def test_fresh_level_merged_with_status_icon_is_canonicalized(self):
        task = SimpleNamespace(frame=np.zeros((768, 1366, 3), dtype=np.uint8),
            next_frame=Mock(), info_set=Mock(), log_warning=Mock(),
            _echo_inventory_capture_y_offset=lambda frame: 0,
            ocr=Mock(side_effect=[
                [Box(1069, 104, 100, 24, name='心傀·喜'),
                 Box(1265, 103, 57, 24, name='+0⑧'),
                 Box(1070, 134, 64, 18, name='COST 1')],
                [Box(1070, 165, 60, 24, name='攻击'), Box(1270, 165, 50, 24, name='3.6%'),
                 Box(1070, 195, 60, 24, name='生命'), Box(1270, 195, 50, 24, name='456')],
            ]))
        item = EnhanceEchoTask._read_selected_echo_inventory_item(task, cost=1, page_index=0, cell_index=0)
        self.assertEqual(item.level, 0)
        self.assertEqual(item.identity.compatibility, 'cost_1:attack_percent')
        self.assertEqual(item.identity.name, '心傀·喜')


if __name__ == '__main__':
    unittest.main()
