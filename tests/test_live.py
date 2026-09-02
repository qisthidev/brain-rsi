from __future__ import annotations

import json
import unittest

from brain_rsi.journal import CandidateNode, Journal
from brain_rsi.live import CcxClient, CcxError, CcxMaker, CcxRunner, extract_json, model_family
from brain_rsi.loader import load_eval_cases
from brain_rsi.search import ProposalRequest, StageConfig
from brain_rsi.types import ScoreResult
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = "gemini-3-flash\ngrok-4.5\ndeepseek-v4-flash\ngpt-5.6-luna\n"


class FakeTransport:
    """Records argv and returns scripted ccx --json payloads."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, timeout_s):
        self.calls.append(list(argv))
        if argv[1] == "--models":
            return 0, MODELS, ""
        if not self.answers:
            return 0, json.dumps({"is_error": False, "result": "", "usage": {}}), ""
        answer = self.answers.pop(0)
        if isinstance(answer, tuple):
            return answer
        return 0, json.dumps({"is_error": False, "result": answer, "usage": {"input_tokens": 10, "output_tokens": 5},
                              "total_cost_usd": 0.001, "duration_ms": 7}), ""


def make_client(answers, **kw):
    transport = FakeTransport(answers)
    return CcxClient(transport=transport, **kw), transport


class ClientTests(unittest.TestCase):
    def test_call_parses_json_and_keeps_ledger(self) -> None:
        client, transport = make_client(["hello"])
        response = client.call("gemini-3-flash", "hi", purpose="t")
        self.assertEqual(response.text, "hello")
        self.assertEqual(response.input_tokens, 10)
        argv = transport.calls[-1]
        self.assertEqual(argv[:4], ["ccx", "--direct", "--json", "--no-auto-add-dir"])
        self.assertNotIn("--add-dir", argv)
        self.assertEqual(client.usage_summary()["calls"], 1)
        self.assertEqual(client.usage_summary()["by_family"]["gemini"]["input_tokens"], 10)

    def test_refuses_gpt_unknown_models_and_budget(self) -> None:
        client, _ = make_client(["a", "b"], max_calls=1)
        with self.assertRaises(CcxError):
            client.call("gpt-5.6-luna", "x")
        with self.assertRaises(CcxError):
            client.call("mystery-model", "x")
        client.call("gemini-3-flash", "x")
        with self.assertRaises(CcxError):
            client.call("gemini-3-flash", "x")
        self.assertEqual(client.calls, 1)

    def test_token_cap(self) -> None:
        client, _ = make_client(["a", "b"], max_tokens=20)
        client.call("gemini-3-flash", "x")  # 15 tokens
        client.call("gemini-3-flash", "x")  # 30 tokens >= cap afterwards
        with self.assertRaises(CcxError):
            client.call("gemini-3-flash", "x")
        self.assertEqual(client.usage_summary()["max_tokens"], 20)

    def test_timeout_refusal_and_error_payloads(self) -> None:
        client, _ = make_client([(124, "", "timeout"), (77, "", "refused"), (0, json.dumps({"is_error": True, "result": "boom"}), ""), (1, "garbage", "err")])
        for _ in range(4):
            with self.assertRaises(CcxError):
                client.call("grok-4.5", "x")

    def test_messages_api_payload_is_understood(self) -> None:
        payload = {"type": "message", "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "A"}, {"type": "text", "text": "B"}],
                   "usage": {"input_tokens": 3, "output_tokens": 4}}
        client, _ = make_client([(0, json.dumps(payload), "")])
        response = client.call("deepseek-v4-flash", "x")
        self.assertEqual(response.text, "AB")
        self.assertEqual((response.input_tokens, response.output_tokens), (3, 4))
        client2, _ = make_client([(0, json.dumps({"type": "error", "error": {"message": "quota"}}), "")])
        with self.assertRaises(CcxError):
            client2.call("deepseek-v4-flash", "x")
        agent_client, transport = make_client(["ok"], direct=False)
        agent_client.call("grok-4.5", "x")
        self.assertNotIn("--direct", transport.calls[-1])

    def test_extract_json_tolerates_fences_and_prose(self) -> None:
        self.assertEqual(extract_json('```json\n{"a": 1}\n```')["a"], 1)
        self.assertEqual(extract_json('Sure! {"a": {"b": 2}} done')["a"]["b"], 2)
        with self.assertRaises(CcxError):
            extract_json("no json here")
        self.assertEqual(model_family("cmc-kimi-k3"), "cmc")


class RunnerAndMakerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = load_eval_cases(ROOT / "eval" / "cases.json")

    def test_runner_answers_from_files_and_honours_budget(self) -> None:
        client, transport = make_client(["I answer the question; no code change."])
        runner = CcxRunner(client, "gemini-3-flash", {"agent/PROMPT.md": "Answer first.\n"}, candidate_id="c1")
        out = runner.run(self.cases[0], {"budget_steps": 5, "budget_seconds": 10})
        self.assertEqual(out.steps, 1)
        self.assertIn("no code change", out.text)
        self.assertIn("Answer first.", transport.calls[-1][-1])
        self.assertIn(self.cases[0].prompt, transport.calls[-1][-1])
        starved = runner.run(self.cases[0], {"budget_steps": 0, "budget_seconds": 10})
        self.assertIn("budget", starved.error)
        client2, _ = make_client([(124, "", "t")])
        failed = CcxRunner(client2, "gemini-3-flash", {}, candidate_id="c2").run(self.cases[0], {"budget_steps": 1, "budget_seconds": 1})
        self.assertTrue(failed.error and failed.error.startswith("ccx:"))

    def _request(self, journal: Journal, kind="debug") -> ProposalRequest:
        root = journal.nodes[0]
        parent = journal.nodes[-1]
        return ProposalRequest(
            kind=kind,
            stage=StageConfig("s1_working", "working", 4),
            parent=parent,
            parent_files={"agent/PROMPT.md": "Be helpful.\n"},
            feedback=("never-close-foreign-task [COORDINATION]: FAILED — the answer contained forbidden behaviour 'closed the task'",),
            journal=journal,
        )

    def test_maker_prompt_never_leaks_the_answer_key(self) -> None:
        client, transport = make_client([json.dumps({"rationale": "add rule", "changes": {"agent/PROMPT.md": "Be helpful.\nLeave others' tasks open."}})])
        maker = CcxMaker(client, maker_model="gemini-3-flash", runner_model="deepseek-v4-flash", feedback_model="grok-4.5", cases=self.cases)
        journal = Journal("t")
        journal.append(CandidateNode(id="r", kind="baseline", stage="root", candidate_id="b", parent_id=None, total=1, max_points=2))
        journal.append(CandidateNode(id="d", kind="draft", stage="s1", candidate_id="draft-01", parent_id="r", total=0.5, max_points=2,
                                     regressions=("never-close-foreign-task",), meta={"rationale": "tried X"}))
        proposal = maker.propose(self._request(journal))
        prompt = transport.calls[-1][-1]
        all_prompts = " ".join(c.prompt.casefold() for c in self.cases)
        for case in self.cases:
            self.assertIn(case.prompt, prompt)
            for phrase in case.expected:  # forbidden hits are surfaced on purpose (the agent's own bad behaviour)
                # multi-word expected phrases must not appear unless a case prompt itself contains them
                if len(phrase.split()) >= 2 and phrase.casefold() not in all_prompts:
                    self.assertNotIn(phrase.casefold(), prompt.casefold(), f"grading phrase leaked: {phrase}")
        self.assertIn("tried X", prompt)  # history of earlier attempts
        self.assertIn("forbidden behaviour", prompt)  # sanitised feedback
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.candidate_id, "debug-01")
        self.assertTrue(proposal.changes["agent/PROMPT.md"].endswith("\n"))
        runner = maker.runner("x", {"agent/PROMPT.md": "p\n"})
        self.assertIsInstance(runner, CcxRunner)

    def test_maker_returns_none_on_bad_answers_and_rejects_same_family_feedback(self) -> None:
        client, _ = make_client(["not json", json.dumps({"changes": {}}), json.dumps({"changes": {"a": 5}})])
        maker = CcxMaker(client, maker_model="gemini-3-flash", runner_model="gemini-3-flash", cases=self.cases)
        journal = Journal("t")
        journal.append(CandidateNode(id="r", kind="baseline", stage="root", candidate_id="b", parent_id=None, total=1, max_points=2))
        for _ in range(3):
            self.assertIsNone(maker.propose(self._request(journal, kind="draft")))
        with self.assertRaises(CcxError):
            CcxMaker(client, maker_model="gemini-3-flash", runner_model="x", feedback_model="gemini-pro-agent")

    def test_analyze_is_advisory_and_survives_errors(self) -> None:
        client, _ = make_client(["- missing rule about tasks", (124, "", "t")])
        maker = CcxMaker(client, maker_model="gemini-3-flash", runner_model="gemini-3-flash", feedback_model="grok-4.5")
        node = CandidateNode(id="n", kind="draft", stage="s", candidate_id="c", parent_id=None, total=0, max_points=1)
        self.assertIn("missing rule", maker.analyze(node, {}, ["f"]))
        self.assertIn("feedback unavailable", maker.analyze(node, {}, ["f"]))
        self.assertEqual(maker.analyze(node, {}, []), "")
