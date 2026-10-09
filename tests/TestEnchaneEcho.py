import time
import unittest
from config import config
from ok.test.TaskTestCase import TaskTestCase
from src.task.AutoCombatTask import AutoCombatTask
from src.task.EnhanceEchoTask import EnhanceEchoTask

config['debug'] = True


class TestEnchaneEcho(TaskTestCase):
    task_class = EnhanceEchoTask
    config = config

    def test_0_level(self):
        self.set_image('tests/images/echo_enhance.png')
        is_0_level = self.task.is_0_level()
        self.assertTrue(is_0_level)

    def test_find_add_mat(self):
        self.set_image('tests/images/find_add_mat.png')
        find_add_mat = self.task.find_add_mat()
        self.assertTrue(find_add_mat)

    def test_find_confirm(self):
        self.set_image('tests/images/find_confirm.png')
        find_confirm = self.task.find_confirm()
        self.assertEqual(len(find_confirm), 1)

    def test_reads_fresh_candidate_identity_from_details(self):
        self.set_image('tests/images/echo_enhance.png')

        identity = self.task._read_current_echo_candidate_identity()

        self.assertEqual(identity.name, '影烁者')
        self.assertEqual(identity.cost, 1)
        self.assertEqual(identity.compatibility, 'cost_1:attack_percent')


if __name__ == '__main__':
    unittest.main()
