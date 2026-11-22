"""Meeting execution scheduling with synthetic input and no public requests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live.model_budget import ModelBudget
from live.provider_settings import ModelService
from live.source_reader import DemoResearcher
from live.store import LiveError
from live.workbench_contracts import Decisions, NewMeeting, NewQuestion, QuestionAction, Utterance
from live.workbench_runtime import ANSWER_SYSTEM, SUMMARY_SYSTEM, TextModel
from live_api import create_app


class Response:
    status_code = 200
    headers = {}

    def __init__(self, content='{"actions":[]}', finish="stop"):
        self.content, self.finish = content, finish

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, _size):
        yield json.dumps({"choices": [{"message": {"content": self.content},
                                       "finish_reason": self.finish}]}).encode()


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wb = create_app(self.root / "state.sqlite3", {"TESTING": True,
            "WORKBENCH_ROOT": self.root / "meetings", "WORKBENCH_CAPABILITIES": {
                "model_enabled": True, "search_enabled": True, "debounce_seconds": 0}}).extensions["workbench"]
        members = self.wb.store.seed(fixture=True)["members"]
        self.owner, self.agent = [self.wb.store.authenticate(m["token"]) for m in (members[0], members[-1])]
        self.mid = self.wb.create(self.owner, NewMeeting(title="执行优化合成验证", data_scope="synthetic"))["id"]

    def say(self, n, text=None):
        self.wb.utterance(self.owner, self.mid, Utterance(text=text or f"合成发言 {n}", segment_id=str(n)))

    def decision(self, work, kind="create_question", title="合成工作流问题", **extra):
        s = work["segments"][-1]
        return Decisions.model_validate({"actions": [{"kind": kind, "title": title,
            "public_query": "synthetic workflow", "source_refs": [{"segment_id": s["id"], "revision": s["revision"]}],
            **extra}]})

    def discover(self, n, kind="candidate", title=None):
        self.say(n)
        work = self.wb.coordinator_work(self.agent)
        title = title or f"合成问题 {n}"
        self.wb.commit_decisions(self.agent, work, self.decision(work, kind, title))
        return next(q for q in self.wb.snapshot(self.owner, self.mid)["questions"] if q["title"] == title)

    def complete(self, work):
        result = DemoResearcher().run({**work["meeting"], "mode": "demo"}, work["question"], lambda _: None)
        self.wb.finish(self.agent, work, result)

    def model(self, **kwargs):
        config = ModelService(provider="openai_compatible", base_url="https://fixture.invalid/v1",
            name="synthetic", api_key="fixture-secret", reasoning_effort="max", max_tokens=65536,
            token_parameter="max_completion_tokens", **kwargs)
        model = TextModel(ModelBudget(self.root / "usage.sqlite3", limit="unlimited"), config)
        self.addCleanup(model.close)
        return model

    def test_only_detection_uses_fast_profile_and_does_not_mutate_main_config(self):
        self.say(1)
        work = self.wb.coordinator_work(self.agent)
        model = self.model()
        with patch("requests.Session.post", return_value=Response()) as post:
            model.decisions(work)
            detection = post.call_args.kwargs["json"]
            model.call(ANSWER_SYSTEM, {})
            answer = post.call_args.kwargs["json"]
            model.call(SUMMARY_SYSTEM, {})
            summary = post.call_args.kwargs["json"]
        self.assertEqual(detection["reasoning_effort"], "medium")
        self.assertEqual(model.config.payload("route", {}, routing=True)["reasoning_effort"], "low")
        self.assertEqual(detection["max_completion_tokens"], 16384)
        self.assertEqual(answer["reasoning_effort"], "max")
        self.assertEqual(summary["reasoning_effort"], "max")
        self.assertEqual(answer["max_completion_tokens"], 65536)
        self.assertEqual(model.config.reasoning_effort, "max")

    def test_inherit_profile_can_restore_provider_compatibility(self):
        self.say(1)
        model = self.model(detection_mode="inherit")
        with patch("requests.Session.post", return_value=Response()) as post:
            model.decisions(self.wb.coordinator_work(self.agent))
        self.assertEqual(post.call_args.kwargs["json"]["reasoning_effort"], "max")
        self.assertEqual(post.call_args.kwargs["json"]["max_completion_tokens"], 65536)

    def test_fast_profile_never_increases_explicit_lower_reasoning_effort(self):
        cases = (("default", "default", "default"), ("none", "none", "none"),
                 ("minimal", "minimal", "minimal"), ("low", "low", "low"),
                 ("medium", "medium", "low"), ("high", "medium", "low"),
                 ("xhigh", "medium", "low"), ("max", "medium", "low"))
        for effort, detection_effort, routing_effort in cases:
            config = ModelService(provider="openai_compatible", reasoning_effort=effort)
            for kind, expected in (("detection", detection_effort), ("routing", routing_effort)):
                with self.subTest(effort=effort, kind=kind):
                    payload = config.payload("synthetic", {}, **{kind: True})
                    if expected == "default":
                        self.assertNotIn("reasoning_effort", payload)
                    else:
                        self.assertEqual(payload["reasoning_effort"], expected)
            self.assertEqual(config.reasoning_effort, effort)
            if effort != "default":
                self.assertEqual(config.payload("synthetic", {})["reasoning_effort"], effort)

    def test_fast_native_deepseek_disables_thinking_but_generic_default_omits_vendor_fields(self):
        deepseek = ModelService(thinking="enabled", reasoning_effort="max", max_tokens=65536)
        payload = deepseek.payload("synthetic", {}, detection=True)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", payload)
        generic = ModelService(provider="openai_compatible").payload("synthetic", {}, detection=True)
        self.assertNotIn("thinking", generic)
        self.assertNotIn("reasoning_effort", generic)

    def test_old_settings_load_without_migration_and_fast_mode_is_public(self):
        saved = self.model().config.model_dump()
        saved.pop("detection_mode", None)
        reopened = ModelService.model_validate(saved)
        self.assertEqual(reopened.detection_mode, "fast")

    def test_backlog_is_drained_in_bounded_batches_without_skipping_or_replay(self):
        for n in range(41):
            self.say(n)
        seen, calls, limits = [], 0, []
        while (work := self.wb.coordinator_work(self.agent)) is not None:
            new = [s for s in work["segments"] if s["event_seq"] > work["meeting"]["coordinator_cursor"]]
            seen.extend(s["text"] for s in new)
            self.assertLessEqual(len(new), 16)
            self.assertLessEqual(sum(len(s["text"]) for s in work["segments"]), 12000)
            limits.append(work.get("action_limit", 4))
            self.wb.commit_decisions(self.agent, work, Decisions(actions=[]))
            calls += 1
        self.assertEqual(seen, [f"合成发言 {n}" for n in range(41)])
        self.assertLessEqual(calls, 4, "backlog still advances just four utterances per call")
        self.assertGreater(max(limits), 4)

    def test_small_fresh_batch_keeps_existing_size_and_action_limit(self):
        for n in range(5):
            self.say(n)
        work = self.wb.coordinator_work(self.agent)
        self.assertEqual(len(work["segments"]), 4)
        self.assertEqual(work.get("action_limit", 4), 4)

    def test_catchup_reserves_recent_context_and_eventually_processes_every_new_segment(self):
        self.say(0, "前文定义。" * 600)
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, Decisions(actions=[]))
        for n in range(1, 17):
            self.say(n, f"新发言{n}。" + "合成" * 497)
        seen = []
        while (work := self.wb.coordinator_work(self.agent)) is not None:
            old = [s for s in work["segments"] if s["event_seq"] <= work["meeting"]["coordinator_cursor"]]
            self.assertTrue(old, "catchup batch pushed all recent context out of the prompt")
            self.assertLessEqual(sum(len(s["text"]) for s in work["segments"]), 12000)
            seen.extend(s["segment_id"].rsplit(":", 1)[-1] for s in work["segments"]
                        if s["event_seq"] > work["meeting"]["coordinator_cursor"])
            self.wb.commit_decisions(self.agent, work, Decisions(actions=[]))
        self.assertEqual(seen, [str(n) for n in range(1, 17)])

    def test_saturated_catchup_retains_cursor_and_continues_same_unread_input(self):
        for n in range(16):
            self.say(n)
        work = self.wb.coordinator_work(self.agent)
        limit = work.get("action_limit", 4)
        self.assertEqual(limit, 12)
        ref = {"segment_id": work["segments"][-1]["id"], "revision": 1}
        actions = [{"kind": "create_question", "title": f"独立问题 {n}",
                    "public_query": "synthetic reference", "source_refs": [ref]} for n in range(limit)]
        self.wb.commit_decisions(self.agent, work, Decisions.model_validate({"actions": actions}))
        next_work = self.wb.coordinator_work(self.agent)
        self.assertEqual(next_work["meeting"]["coordinator_cursor"], 0)
        self.assertEqual(len(next_work["questions"]), 12)
        self.wb.commit_decisions(self.agent, next_work, self.decision(next_work, title="第十三个独立问题"))
        self.assertEqual(len(self.wb.snapshot(self.owner, self.mid)["questions"]), 13)
        self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_small_batch_cannot_commit_more_actions_than_its_declared_limit(self):
        self.say(1)
        work = self.wb.coordinator_work(self.agent)
        ref = {"segment_id": work["segments"][-1]["id"], "revision": 1}
        actions = [{"kind": "candidate", "title": str(n), "source_refs": [ref]} for n in range(5)]
        with self.assertRaises((LiveError, ValueError)):
            self.wb.commit_decisions(self.agent, work, Decisions.model_validate({"actions": actions}))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["questions"], [])

    def test_explicit_request_passes_old_inferred_work_and_full_backlog(self):
        for n in range(21):
            self.discover(n)
        urgent = self.discover(21, "create_question", "现在明确要求查证")
        with self.wb.store.transaction() as db:
            for q in self.wb._items(db, "wb_question", self.mid):
                if q["id"] != urgent["id"]:
                    q["created_at"] -= 180
                    self.wb.store._put(db, "wb_question", q["id"], q)
        claimed = self.wb.claim(self.agent)
        self.assertEqual(claimed["question"]["id"], urgent["id"])
        self.assertEqual(claimed["question"].get("request_kind"), "explicit")

    def test_manual_retry_is_ahead_of_automatic_explicit_requests(self):
        q = self.discover(1)
        self.wb.fail(self.agent, self.wb.claim(self.agent), "synthetic_failure")
        self.discover(2, "create_question")
        self.wb.action(self.owner, q["id"], QuestionAction(action="retry", expected_version=1))
        self.assertEqual(self.wb.claim(self.agent)["question"]["id"], q["id"])

    def test_aged_inferred_work_gets_a_turn_after_three_priority_starts(self):
        background = self.discover(0)
        with self.wb.store.transaction() as db:
            q = self.wb.store._get(db, "wb_question", background["id"])
            q["created_at"] -= 180
            self.wb.store._put(db, "wb_question", q["id"], q)
        for n in range(1, 6):
            self.discover(n, "create_question")
        for _ in range(3):
            work = self.wb.claim(self.agent)
            self.assertNotEqual(work["question"]["id"], background["id"])
            self.complete(work)
        work = self.wb.claim(self.agent)
        self.assertEqual(work["question"]["id"], background["id"])

    def test_blocked_oldest_task_does_not_starve_other_aged_inferred_work(self):
        self.mid = self.wb.create(self.owner, NewMeeting(title="合成检索权限验证"))["id"]
        blocked = self.discover(0)
        available = self.discover(1)
        with self.wb.store.transaction() as db:
            for saved in self.wb._items(db, "wb_question", self.mid):
                saved["created_at"] -= 180
                if saved["id"] == blocked["id"]:
                    saved["public_query"] = ""
                self.wb.store._put(db, "wb_question", saved["id"], saved)
        for n in range(2, 7):
            self.discover(n, "create_question")
        for _ in range(3):
            self.wb.fail(self.agent, self.wb.claim(self.agent), "synthetic_failure")
        work = self.wb.claim(self.agent)
        self.assertEqual(work["question"]["id"], available["id"])

    def test_evidence_need_survives_mentions_and_claim_and_is_reset_on_owner_edit(self):
        from live.workbench_contracts import EditQuestion
        self.say(1)
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, evidence_need='public'))
        q = self.wb.snapshot(self.owner, self.mid)['questions'][0]
        self.say(2)
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, 'revise_question', question_id=q['id'],
                                 scope_changed=False, evidence_need='explanation'))
        claimed = self.wb.claim(self.agent)['question']
        self.assertEqual(claimed['evidence_need'], 'public')
        edited = self.wb.edit(self.owner, q['id'], EditQuestion(title='只计算给定数字', expected_version=q['version']))
        self.assertFalse(edited.get('evidence_need'))

    def test_grounded_public_fact_survives_aside_but_not_owner_rewrite(self):
        from live.workbench_contracts import EditQuestion
        quote = '现在两端价格都在挤压加工利润吗？'
        fact = '原油和产品当前价差是否收窄'
        self.say(1, quote)
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, evidence_need='public',
                                  grounding_quote=quote, external_fact=fact))
        q = self.wb.snapshot(self.owner, self.mid)['questions'][0]
        self.say(2, '价差指的是什么？')
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, 'revise_question', question_id=q['id'],
                                  scope_changed=False, evidence_need='explanation', grounding_quote='价差指的是什么？'))
        claimed = self.wb.claim(self.agent)['question']
        self.assertEqual((claimed['grounding_quote'], claimed['external_fact']), (quote, fact))
        edited = self.wb.edit(self.owner, q['id'], EditQuestion(title='请解释价差', expected_version=q['version']))
        self.assertNotIn('grounding_quote', edited)
        self.assertNotIn('external_fact', edited)

    def test_same_scope_mentions_preserve_inflight_answer_but_record_new_evidence(self):
        question = self.discover(1, "create_question", "合成工作流问题")
        inflight = self.wb.claim(self.agent)
        self.say(2, "仍然是刚才同一个工作流定义问题。")
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, "revise_question",
            question_id=question["id"], expected_version=1, scope_changed=False))
        self.complete(inflight)
        q = self.wb.snapshot(self.owner, self.mid)["questions"][0]
        self.assertEqual(q["version"], 1)
        self.assertEqual(q["source_refs"], inflight["question"]["source_refs"])
        self.assertTrue(q.get("last_mention_refs"))
        self.assertIsNone(self.wb.claim(self.agent), "same-scope mention should not trigger a second paid run")

    def test_waiting_supplement_is_merged_but_changed_scope_fences_running_result(self):
        q = self.discover(1, "create_question", "合成工作流问题")
        self.say(2)
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, "revise_question",
            question_id=q["id"], expected_version=1, scope_changed=False))
        inflight = self.wb.claim(self.agent)
        self.assertEqual(len(inflight["question"]["source_refs"]), 2)
        self.say(3, "增加一个条件，只看离线多智能体框架。")
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, "revise_question", "离线多智能体框架",
            question_id=q["id"], expected_version=inflight["question"]["version"], scope_changed=True))
        with self.assertRaises(LiveError):
            self.complete(inflight)
        self.assertEqual(len(self.wb.snapshot(self.owner, self.mid)["questions"]), 1)

    def test_creation_latency_uses_initial_refs_after_later_mention(self):
        from tools.measure_workbench_frequency import measure

        q = self.discover(1, "create_question", "合成工作流问题")
        self.say(2, "请接着查同一份材料。")
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, "revise_question",
            question_id=q["id"], expected_version=1, scope_changed=False))
        with self.wb.store.transaction() as db:
            creation, = db.execute("SELECT data FROM events WHERE meeting=? AND data->>'$.type'='question.created'",
                                   (self.mid,)).fetchone()
            self.assertEqual(json.loads(creation)["payload"]["source_refs"], q["source_refs"])
            report = measure(db, self.mid)
            self.assertEqual((report['automatic_questions'], report['final_to_creation']['n']), (1, 1))
            self.assertEqual(report['events']['question.mentioned'], 1)
            # An old event lacking its original refs cannot safely use the changed question's current refs.
            db.execute("UPDATE events SET data=json_remove(data,'$.payload.source_refs') WHERE meeting=? AND data->>'$.type'='question.created'", (self.mid,))
            self.assertEqual(measure(db, self.mid)['final_to_creation']['n'], 0)

    def test_no_scope_change_flag_cannot_silently_replace_title_or_query(self):
        q = self.discover(1, "create_question", "合成工作流问题")
        self.say(2)
        work = self.wb.coordinator_work(self.agent)
        with self.assertRaises(LiveError):
            self.wb.commit_decisions(self.agent, work, self.decision(work, "revise_question", "全新对象",
                question_id=q["id"], expected_version=1, scope_changed=False))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["questions"][0]["title"], q["title"])

    def test_same_title_with_different_query_is_not_silently_dropped(self):
        self.discover(1, "create_question", "框架部署比较")
        self.say(2, "还需要单独查另一个条件下的框架部署。")
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, title="框架部署比较",
                                public_query="framework offline deployment"))
        questions = self.wb.snapshot(self.owner, self.mid)["questions"]
        self.assertEqual(len(questions), 2)
        self.assertEqual({q["public_query"] for q in questions},
                         {"synthetic workflow", "framework offline deployment"})

    def test_terminal_punctuation_merges_only_same_query_and_keeps_other_clauses(self):
        title = 'Q/DEMO 1001-2026 第 5.2 条怎么说'
        q = self.discover(1, 'create_question', title + '？')
        self.say(2)
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, title=title + '?'))
        items = self.wb.snapshot(self.owner, self.mid)['questions']
        self.assertEqual([item['id'] for item in items], [q['id']])
        self.assertEqual(len(items[0]['source_refs']), 2)
        self.discover(3, 'create_question', title.replace('5.2', '5.3'))
        self.discover(4, 'create_question', title.replace('2026', '2025'))
        self.say(5)
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, title=title, public_query='distinct application scope'))
        self.assertEqual(len(self.wb.snapshot(self.owner, self.mid)['questions']), 4)

    def test_material_query_change_with_same_title_and_basis_increments_version(self):
        self.say(1)
        work = self.wb.coordinator_work(self.agent)
        ref = {"segment_id": work["segments"][-1]["id"], "revision": 1}
        q = self.wb.question(self.owner, self.mid, NewQuestion(title="合成工作流问题",
            public_query="synthetic workflow", source_refs=[ref]))
        # The manual question was added after the last read, before detection.
        work["questions"] = [q]
        self.wb.commit_decisions(self.agent, work, self.decision(work, "revise_question",
            question_id=q["id"], expected_version=1, public_query="workflow offline deployment", scope_changed=True))
        updated = self.wb.snapshot(self.owner, self.mid)["questions"][0]
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["public_query"], "workflow offline deployment")

    def test_repeated_detection_does_not_resume_paused_cancelled_or_unknown_work(self):
        for n, status in enumerate(("paused", "cancelled", "outcome_unknown"), start=1):
            q = self.discover(n, "create_question", f"保留人工状态{n}")
            with self.wb.store.transaction() as db:
                saved = self.wb.store._get(db, "wb_question", q["id"])
                saved.update(state=status)
                self.wb.store._put(db, "wb_question", q["id"], saved)
            self.say(n + 10)
            work = self.wb.coordinator_work(self.agent)
            self.wb.commit_decisions(self.agent, work, self.decision(work, title=q["title"]))
            updated = next(x for x in self.wb.snapshot(self.owner, self.mid)["questions"] if x["id"] == q["id"])
            self.assertEqual(updated["state"], status)
            self.assertEqual(updated["source_refs"], q["source_refs"])
            self.assertEqual(len(self.wb.snapshot(self.owner, self.mid)["questions"]), n)

    def test_stopped_same_title_does_not_swallow_an_independent_scope(self):
        for n, status in enumerate(("paused", "cancelled", "outcome_unknown"), start=1):
            q = self.discover(n, "create_question", f"同名独立范围{n}")
            with self.wb.store.transaction() as db:
                saved = self.wb.store._get(db, "wb_question", q["id"])
                saved["state"] = status
                self.wb.store._put(db, "wb_question", q["id"], saved)
            self.say(n + 10, "另一种部署条件下的独立查证请求。")
            work = self.wb.coordinator_work(self.agent)
            self.wb.commit_decisions(self.agent, work, self.decision(work, title=q["title"],
                                    public_query="independent offline scope"))
            questions = self.wb.snapshot(self.owner, self.mid)["questions"]
            self.assertEqual(next(x for x in questions if x["id"] == q["id"])["state"], status)
            self.assertEqual(len(questions), n * 2)

    def test_deferred_detection_still_uses_fast_parameters(self):
        self.say(1)
        model = self.model()
        with patch("requests.Session.post", side_effect=[Response('{"actions":null}'), Response()]) as post:
            work = self.wb.coordinator_work(self.agent)
            batch = model.decisions(work)
            self.assertEqual(post.call_count, 1)
            self.wb.commit_detection(self.agent, work, batch)
            retry = self.wb.detection_recovery_work(self.agent)
            self.assertIsNotNone(retry)
            self.wb.commit_detection(self.agent, retry, model.decisions(retry))
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertEqual(call.kwargs["json"]["reasoning_effort"], "medium")
            self.assertEqual(call.kwargs["json"]["max_completion_tokens"], 16384)

    def test_truncated_detection_keeps_an_unresolved_range_and_no_questions(self):
        self.say(1)
        work = self.wb.coordinator_work(self.agent)
        model = self.model()
        with patch("requests.Session.post", return_value=Response(finish="length")) as post:
            batch = model.decisions(work)
            self.wb.commit_detection(self.agent, work, batch)
        self.assertEqual(post.call_count, 1)
        snapshot = self.wb.snapshot(self.owner, self.mid)
        self.assertEqual(snapshot["meeting"]["coordinator_cursor"], work['cursor'])
        self.assertEqual(snapshot["questions"], [])
        self.assertTrue(snapshot['detection_tasks'])
        self.assertFalse(snapshot['automation_progress']['complete'])


if __name__ == "__main__":
    unittest.main()
