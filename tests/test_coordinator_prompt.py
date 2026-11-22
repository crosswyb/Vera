"""Guards the coordinator's detection breadth.

On 2026-11-18 a rule saying internal-context questions "只能由现场人员确认，不创建公开查证题"
made the model return empty actions for nearly every batch of a real meeting
(1119: 3 actions in 1176 batches, versus 80/435 and 109/333 in earlier meetings).
"""

import unittest

from live.workbench_runtime import COORDINATOR_SYSTEM


class CoordinatorPromptTest(unittest.TestCase):
    def test_inferred_public_questions_are_still_requested_without_explicit_delegation(self):
        self.assertIn("没人说“查一下”时，只在讨论正围绕某个判断展开", COORDINATOR_SYSTEM)
        self.assertIn("讨论发生在内部项目或客户业务背景下也照此判断", COORDINATOR_SYSTEM)
        self.assertIn("同一段讨论里相近角度的问题合并为一题", COORDINATOR_SYSTEM)

    def test_only_facts_held_by_attendees_are_excluded(self):
        self.assertIn("只有答案完全掌握在现场人员手中的事项不建题", COORDINATOR_SYSTEM)
        self.assertNotIn("不创建公开查证题", COORDINATOR_SYSTEM)
        self.assertNotIn("疑问句数量", COORDINATOR_SYSTEM)

    def test_reworded_topics_are_matched_against_existing_questions(self):
        self.assertIn("只是换了措辞或说法时视为已有问题", COORDINATOR_SYSTEM)

    def test_long_meetings_keep_every_question_visible_to_the_coordinator(self):
        from live.workbench_runtime import TextModel
        seen = {}
        model = TextModel.__new__(TextModel)
        def call(system, content, **kwargs):
            seen.update(content)
            return {'actions': []}
        model.call = call
        questions = [{"id": f"q{n}", "title": f"问题{n}", "version": 1, "state": "completed",
                      "public_query": "q", "source_refs": []} for n in range(80)]
        model.decisions({"meeting": {"id": "m", "coordinator_cursor": 0}, "cursor": 1, "segments": [], "questions": questions})
        listed = seen["existing_questions"]
        self.assertEqual([q["id"] for q in listed], [q["id"] for q in questions])
        self.assertNotIn("public_query", listed[0])
        self.assertIn("public_query", listed[-1])


if __name__ == "__main__":
    unittest.main()
