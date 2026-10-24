"""Unit tests for the scorer, validator, usage report and custom cases.

    python3 -m unittest discover -s code/evaluation -p "test_*.py" -v
"""

import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import custom_cases  # noqa: E402
import scorer  # noqa: E402
import usage  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(HERE))
DATASET = os.path.join(ROOT, "dataset")
GOLD_PATH = os.path.join(DATASET, "sample_output.csv")


def step(n, phase, op, system, resource, evidence=""):
    return {
        "step_no": n,
        "phase": phase,
        "system_id": system,
        "resource_id": resource,
        "operation": op,
        "state": scorer.OP_STATE[op],
        "evidence_id": evidence,
    }


def renumber(steps):
    for i, s in enumerate(steps, start=1):
        s["step_no"] = i
    return steps


def pred(case, status, steps):
    return scorer.Prediction(case, status, steps, None, json.dumps(steps))


def incomplete_steps():
    return [
        step(1, "FINDING", "REPORT_REMAINING_RECORD", "search_service", "search_1", "evidence_01"),
        step(2, "FINDING", "REPORT_EXPOSED_PII", "support_platform", "support_1", "evidence_01"),
        step(3, "REMEDIATION", "DELETE_RECORD", "search_service", "search_1"),
        step(4, "REMEDIATION", "REDACT_RECORD", "support_platform", "support_1"),
        step(5, "VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_1"),
        step(6, "VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_1"),
        step(7, "VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_1"),
        step(8, "VERIFICATION", "VERIFY_RECORD_REDACTED", "support_platform", "support_1"),
    ]


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.gold = {
            "c1": pred("c1", "INCOMPLETE", incomplete_steps()),
            "c2": pred("c2", "NOT_APPLICABLE", []),
            "c3": pred("c3", "COMPLETE", [step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_9", "evidence_02")]),
        }

    def test_perfect(self):
        m = scorer.score(copy.deepcopy(self.gold), self.gold)
        self.assertEqual(m["case_level"]["status_accuracy"], 1.0)
        self.assertEqual(m["row_level"]["exact_row_accuracy"], 1.0)
        self.assertEqual(m["step_level"]["micro_f1_with_evidence"], 1.0)
        self.assertEqual(m["field_level"]["mean_field_accuracy"], 1.0)
        self.assertEqual(m["sequence_level"]["exact_sequence_accuracy"], 1.0)

    def test_wrong_status_only(self):
        p = copy.deepcopy(self.gold)
        p["c3"].status = "INCOMPLETE"
        m = scorer.score(p, self.gold)
        self.assertAlmostEqual(m["case_level"]["status_accuracy"], 2 / 3)
        self.assertEqual(m["step_level"]["micro_f1"], 1.0)  # steps untouched
        self.assertAlmostEqual(m["row_level"]["exact_row_accuracy"], 2 / 3)
        self.assertEqual(m["case_level"]["confusion"]["COMPLETE->INCOMPLETE"], 1)

    def test_missing_and_extra_step(self):
        p = copy.deepcopy(self.gold)
        p["c1"].steps = renumber([s for s in p["c1"].steps if s["operation"] != "VERIFY_RECORD_PRESENT"])
        m = scorer.score(p, self.gold)
        st = m["step_level"]
        self.assertEqual(st["gold_steps"], 9)
        self.assertAlmostEqual(st["micro_recall"], 8 / 9)
        self.assertEqual(st["micro_precision"], 1.0)
        p["c1"].steps = renumber(p["c1"].steps + [step(0, "VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "integration_service", "data_flow_1")])
        m = scorer.score(p, self.gold)
        self.assertLess(m["step_level"]["micro_precision"], 1.0)

    def test_order_swap_is_sequence_error_not_set_error(self):
        p = copy.deepcopy(self.gold)
        s = p["c1"].steps
        s[2], s[3] = s[3], s[2]  # swap two remediation steps
        renumber(s)
        m = scorer.score(p, self.gold)
        self.assertEqual(m["step_level"]["micro_f1"], 1.0)
        self.assertEqual(m["sequence_level"]["exact_sequence_accuracy"], 2 / 3)
        self.assertLess(m["sequence_level"]["pairwise_order_concordance"], 1.0)
        self.assertLess(m["sequence_level"]["canonical_order_compliance"], 1.0)
        self.assertEqual(m["row_level"]["exact_row_accuracy"], 2 / 3)

    def test_wrong_evidence_only_hits_strict_metrics(self):
        p = copy.deepcopy(self.gold)
        p["c3"].steps[0]["evidence_id"] = "evidence_99"
        m = scorer.score(p, self.gold)
        self.assertEqual(m["step_level"]["micro_f1"], 1.0)
        self.assertLess(m["step_level"]["micro_f1_with_evidence"], 1.0)
        self.assertEqual(m["row_level"]["exact_row_accuracy_ignoring_evidence_id"], 1.0)
        self.assertLess(m["row_level"]["exact_row_accuracy"], 1.0)
        self.assertLess(m["field_level"]["accuracy_by_field"]["evidence_id"], 1.0)
        self.assertEqual(m["field_level"]["accuracy_by_field"]["operation"], 1.0)

    def test_wrong_resource_gets_partial_field_credit(self):
        p = copy.deepcopy(self.gold)
        p["c3"].steps[0]["resource_id"] = "account_8"
        m = scorer.score(p, self.gold)
        f = m["field_level"]["accuracy_by_field"]
        self.assertLess(f["resource_id"], 1.0)
        self.assertEqual(f["system_id"], 1.0)  # still aligned via the other 4 identity fields
        self.assertEqual(m["step_level"]["micro_f1"] < 1.0, True)

    def test_not_applicable_confusion(self):
        p = copy.deepcopy(self.gold)
        p["c2"] = pred("c2", "INCOMPLETE", incomplete_steps())
        m = scorer.score(p, self.gold)
        self.assertEqual(m["case_level"]["per_class"]["NOT_APPLICABLE"]["recall"], 0.0)
        self.assertEqual(m["case_level"]["per_class"]["INCOMPLETE"]["precision"], 0.5)
        self.assertGreater(m["step_level"]["pred_steps"], m["step_level"]["gold_steps"])

    def test_both_empty_counts_as_perfect_case_f1(self):
        m = scorer.score({"c2": pred("c2", "NOT_APPLICABLE", [])}, {"c2": pred("c2", "NOT_APPLICABLE", [])})
        self.assertEqual(m["row_level"]["mean_case_step_f1"], 1.0)
        self.assertEqual(m["sequence_level"]["mean_lcs_ratio"], 1.0)

    def test_missing_rows_and_subset_flag(self):
        p = {"c1": copy.deepcopy(self.gold["c1"])}
        m = scorer.score(p, self.gold)
        self.assertEqual(m["case_level"]["total"], 3)
        self.assertAlmostEqual(m["case_level"]["status_accuracy"], 1 / 3)
        self.assertEqual(m["coverage"]["missing_in_pred"], ["c2", "c3"])
        m2 = scorer.score(p, self.gold, subset_ok=True)
        self.assertEqual(m2["case_level"]["status_accuracy"], 1.0)

    def test_unparseable_row_does_not_crash(self):
        p = copy.deepcopy(self.gold)
        p["c1"] = scorer.Prediction("c1", "INCOMPLETE", None, "invalid steps_json", "[oops")
        m = scorer.score(p, self.gold)
        self.assertEqual(m["coverage"]["unparseable_rows"], 1)
        self.assertEqual(m["case_level"]["correct"], 3)  # status string alone is still right
        self.assertLess(m["row_level"]["exact_row_accuracy"], 1.0)

    def test_duplicate_steps_use_multiset_semantics(self):
        p = copy.deepcopy(self.gold)
        p["c3"].steps = renumber(p["c3"].steps + copy.deepcopy(p["c3"].steps))
        m = scorer.score(p, self.gold)
        self.assertEqual(m["step_level"]["micro_recall"], 1.0)
        self.assertLess(m["step_level"]["micro_precision"], 1.0)


class ValidateTests(unittest.TestCase):
    def test_valid_incomplete(self):
        self.assertEqual(scorer.validate_row(pred("c", "INCOMPLETE", incomplete_steps())), [])

    def test_complete_with_remediation_is_flagged(self):
        steps = incomplete_steps()
        issues = scorer.validate_row(pred("c", "COMPLETE", steps))
        self.assertTrue(any("only contain VERIFICATION" in i for i in issues))

    def test_incomplete_remediation_must_not_cite_evidence(self):
        steps = incomplete_steps()
        steps[2]["evidence_id"] = "evidence_01"
        issues = scorer.validate_row(pred("c", "INCOMPLETE", steps))
        self.assertTrue(any("empty evidence_id" in i for i in issues))

    def test_finding_needs_evidence_and_complete_verification_too(self):
        steps = incomplete_steps()
        steps[0]["evidence_id"] = ""
        self.assertTrue(any("finding must cite" in i for i in scorer.validate_row(pred("c", "INCOMPLETE", steps))))
        v = [step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_1", "")]
        self.assertTrue(any("must cite evidence_id" in i for i in scorer.validate_row(pred("c", "COMPLETE", v))))

    def test_not_applicable_must_be_empty(self):
        self.assertTrue(scorer.validate_row(pred("c", "NOT_APPLICABLE", incomplete_steps())))
        self.assertEqual(scorer.validate_row(pred("c", "NOT_APPLICABLE", [])), [])

    def test_bad_state_phase_stepno_and_extra_key(self):
        steps = incomplete_steps()
        steps[0]["state"] = "ABSENT"
        steps[1]["phase"] = "REMEDIATION"
        steps[2]["step_no"] = 9
        steps[3]["extra"] = 1
        text = " | ".join(scorer.validate_row(pred("c", "INCOMPLETE", steps)))
        self.assertIn("requires state PRESENT", text)
        self.assertIn("belongs to phase", text)
        self.assertIn("step_no", text)
        self.assertIn("keys", text)

    def test_ordering_rule(self):
        steps = incomplete_steps()
        steps[4], steps[5] = steps[5], steps[4]  # account_service must precede search_service
        renumber(steps)
        self.assertTrue(any("order" in i for i in scorer.validate_row(pred("c", "INCOMPLETE", steps))))

    def test_resource_order_accepts_lexical_or_natural(self):
        a = step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_898", "e1")
        b = step(2, "VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_1007", "e1")
        self.assertEqual(scorer.order_issues([a, b]), [])  # natural order 898 < 1007
        self.assertEqual(scorer.order_issues([b, a]), [])  # lexical order '1007' < '898'

    def test_evidence_after_audit_at_is_flagged(self):
        times = {"evidence_01": scorer.parse_ts("2026-09-16T00:00:00Z")}
        issues = scorer.validate_row(pred("c", "INCOMPLETE", incomplete_steps()), times, scorer.parse_ts("2026-09-15T12:00:00Z"))
        self.assertTrue(any("after audit_at" in i for i in issues))
        issues = scorer.validate_row(pred("c", "INCOMPLETE", incomplete_steps()), times, scorer.parse_ts("2026-09-17T00:00:00Z"))
        self.assertEqual(issues, [])
        self.assertTrue(any("unknown evidence_id" in i for i in scorer.validate_row(pred("c", "INCOMPLETE", incomplete_steps()), {}, None)))


@unittest.skipUnless(os.path.exists(GOLD_PATH), "dataset not present")
class DatasetTests(unittest.TestCase):
    def test_sample_gold_satisfies_our_contract_checks(self):
        gold, info = scorer.load_output(GOLD_PATH)
        ev, audit = scorer.load_dataset_context(DATASET)
        self.assertEqual(len(gold), 32)
        for cid, p in gold.items():
            self.assertEqual(scorer.validate_row(p, ev, audit.get(cid)), [], cid)

    def test_gold_vs_gold(self):
        gold, _ = scorer.load_output(GOLD_PATH)
        m = scorer.score(gold, gold)
        self.assertEqual(m["row_level"]["exact_row_accuracy"], 1.0)

    def test_blank_template_fails_submission_check(self):
        pred_rows, info = scorer.load_output(os.path.join(DATASET, "output.csv"))
        ids = scorer.read_request_ids(DATASET)
        self.assertEqual(len(ids), 518)
        rep = scorer.validate_submission(pred_rows, info, ids)
        self.assertFalse(rep["ok"])

    def test_deletion_requests_are_in_natural_order(self):
        ids = scorer.read_request_ids(DATASET)
        self.assertEqual(ids, sorted(ids, key=scorer.natural_key))

    def test_custom_cases_build_and_cover_na_and_invariance(self):
        reqs, gold = custom_cases.build(DATASET)
        self.assertGreaterEqual(len(reqs), 3)
        self.assertEqual([r["case_id"] for r in reqs], [g["case_id"] for g in gold])
        statuses = {g["deletion_status"] for g in gold}
        self.assertIn("NOT_APPLICABLE", statuses)
        for g in gold:
            if g["deletion_status"] == "NOT_APPLICABLE":
                self.assertEqual(g["steps_json"], "[]")
        with tempfile.TemporaryDirectory() as d:
            rp, gp = custom_cases.write_files(DATASET, d)
            self.assertTrue(os.path.exists(rp) and os.path.exists(gp))


class UsageTests(unittest.TestCase):
    def test_report_math(self):
        entries = [
            {"provider": "p", "model": "m1", "input_tokens": 1000, "output_tokens": 200, "cached_input_tokens": 0, "case_id": "a", "purpose": "x"},
            {"provider": "p", "model": "m1", "input_tokens": 3000, "output_tokens": 800, "cached_input_tokens": 0, "case_id": "b", "purpose": "x"},
            {"provider": "q", "model": "m2", "input_tokens": 500, "output_tokens": 100, "cached_input_tokens": 0, "case_id": "b", "purpose": "y"},
        ]
        table = {"m1": {"input": 1.0, "output": 2.0}}
        rows, total, by_purpose = usage.summarize(entries, 10, table)
        self.assertEqual(total["calls"], 3)
        self.assertEqual(total["tokens"], 5600)
        self.assertAlmostEqual(total["tokens_per_request"], 560.0)
        self.assertAlmostEqual(total["cost"], (4000 * 1.0 + 1000 * 2.0) / 1e6)
        self.assertEqual(total["unpriced"], ["m2"])
        text = usage.render_usage_report(entries, 10, table=table)
        self.assertIn("unpriced", text)
        self.assertIn("Average tokens per request", text)

    def test_empty_log_report_states_zero(self):
        text = usage.render_usage_report([], 518)
        self.assertIn("**0**", text)
        self.assertIn("518", text)

    def test_record_roundtrip_stores_no_content(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "u.jsonl")
            usage.record_usage("prov", "mod", 10, 5, case_id="case_1", purpose="t", path=path)
            e = usage.load_log(path)[0]
            self.assertEqual(sorted(e), sorted(["ts", "provider", "model", "input_tokens", "output_tokens", "cached_input_tokens", "case_id", "purpose"]))


if __name__ == "__main__":
    unittest.main()
